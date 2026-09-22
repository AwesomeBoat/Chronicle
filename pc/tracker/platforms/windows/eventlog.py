"""system_boot / system_shutdown / system_sleep, lus dans le journal Windows.

Pourquoi le journal plutot que les messages en direct : il a les instants
EXACTS, et il les a meme quand le tracker ne tournait pas (arret brutal,
demarrage avant l'ouverture de session). Relu au demarrage, toutes les
10 minutes et apres chaque reveil.

    Kernel-General 12            demarrage       Data StartTime
    Kernel-General 13            arret           Data StopTime
    User32 1074                  arret demande   par quel processus, arret
                                                 ou redemarrage (texte
                                                 TRADUIT : voir _genre)
    EventLog 6008                arret brutal    heure dans Binary (UTC)
    Power-Troubleshooter 1       veille          Data SleepTime / WakeTime

`wevtutil` est l'outil en ligne de commande livre avec Windows : aucune
dependance, et la lecture du journal Systeme ne demande pas les droits
d'administrateur.

Le curseur (dernier instant lu) est garde : un evenement n'est lu qu'une
fois, et la cle naturelle (l'instant exact) empeche tout doublon si le
curseur etait perdu.
"""

from __future__ import annotations

import struct
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from pc import schema
from pc.tracker.collectors.base import PollingCollector
from pc.tracker.core.clock import utc_now

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
CREATE_NO_WINDOW = 0x08000000
CLE_CURSEUR = "eventlog:cursor"
# Premier lancement : trois jours d'historique, deja dans le journal.
RATTRAPAGE = timedelta(days=3)

_REQUETE = (
    "*[System[("
    "(Provider[@Name='Microsoft-Windows-Kernel-General'] and "
    "(EventID=12 or EventID=13)) or "
    "(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)"
    " or (Provider[@Name='User32'] and EventID=1074)"
    " or (Provider[@Name='EventLog'] and EventID=6008)"
    ") and TimeCreated[@SystemTime>'{depuis}']]]")

# 1074, param5 : l'action demandee, dans la langue du systeme.
_REDEMARRAGE = ("restart", "redémarrer", "redemarrer", "neu starten",
                "reiniciar", "herstarten", "riavviare")
_EXTINCTION = ("power off", "shutdown", "hors tension", "arrêter",
               "arreter", "ausschalten", "apagar", "uitschakelen", "spegnere")


def _genre(texte: str | None) -> str:
    t = (texte or "").lower()

    if any(m in t for m in _REDEMARRAGE):
        return "restart"

    if any(m in t for m in _EXTINCTION):
        return "power_off"

    return "unknown"


def _instant(texte: str | None) -> datetime | None:
    if not texte:
        return None

    try:
        return schema.parse_ts(texte[:26].rstrip("Z") + "Z"
                               if "." in texte else texte)
    except ValueError:
        return None


def _systemtime_utc(binaire: str | None) -> datetime | None:
    """6008 : deux SYSTEMTIME dans Binary, heure locale PUIS heure UTC."""
    if not binaire or len(binaire) < 64:
        return None

    try:
        donnees = bytes.fromhex(binaire[32:64])
        annee, mois, _jour_sem, jour, h, m, s, ms = struct.unpack("<8H",
                                                                  donnees)
        return datetime(annee, mois, jour, h, m, s, ms * 1000,
                        tzinfo=timezone.utc)
    except (ValueError, struct.error):
        return None


def parse_events(xml: str) -> list[dict]:
    """Sortie XML de wevtutil (/e:root) -> liste de faits."""
    try:
        racine = ET.fromstring(xml)
    except ET.ParseError:
        return []

    faits = []

    for evenement in racine.findall("e:Event", NS):
        systeme = evenement.find("e:System", NS)
        fournisseur = systeme.find("e:Provider", NS).get("Name", "")
        identifiant = int(systeme.find("e:EventID", NS).text or 0)
        cree = _instant(systeme.find("e:TimeCreated", NS).get("SystemTime"))
        donnees = {}
        anonymes = []

        for d in evenement.findall("e:EventData/e:Data", NS):
            if d.get("Name"):
                donnees[d.get("Name")] = d.text
            else:
                anonymes.append(d.text)

        binaire = evenement.find("e:EventData/e:Binary", NS)
        faits.append({"provider": fournisseur, "id": identifiant,
                      "created": cree, "data": donnees, "anon": anonymes,
                      "binary": binaire.text if binaire is not None else None})

    return faits


def query(depuis: datetime) -> list[dict]:
    """Les evenements utiles posterieurs a `depuis`, dans l'ordre."""
    requete = _REQUETE.format(
        depuis=depuis.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    resultat = subprocess.run(
        ["wevtutil", "qe", "System", f"/q:{requete}", "/f:xml", "/e:root"],
        capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)

    if resultat.returncode != 0:
        raise RuntimeError(resultat.stderr.decode("utf-8", "replace")[:300])

    return parse_events(resultat.stdout.decode("utf-8", "replace"))


def to_events(faits: list[dict]) -> list[tuple]:
    """Faits bruts -> (type, debut, fin, payload, cle naturelle)."""
    sortie = []
    derniere_demande: dict | None = None

    for f in sorted(faits, key=lambda x: x["created"] or datetime.min.replace(
            tzinfo=timezone.utc)):
        fournisseur, ident, data = f["provider"], f["id"], f["data"]

        if fournisseur == "User32" and ident == 1074:
            derniere_demande = f
            continue

        if fournisseur.endswith("Kernel-General") and ident == 12:
            debut = _instant(data.get("StartTime")) or f["created"]
            sortie.append(("system_boot", debut, None,
                           {"origin": "event_log"},
                           f"boot|{schema.format_ts(debut)}"))

        elif fournisseur.endswith("Kernel-General") and ident == 13:
            debut = _instant(data.get("StopTime")) or f["created"]
            genre, initiateur = "unknown", None

            if derniere_demande is not None and derniere_demande["created"] \
                    and debut - derniere_demande["created"] < timedelta(
                        minutes=15):
                d = derniere_demande["data"]
                genre = _genre(d.get("param5"))
                processus = (d.get("param1") or "").split(" (")[0]
                initiateur = processus.replace("\\", "/").rsplit("/", 1)[-1] \
                    or None

            derniere_demande = None
            sortie.append(("system_shutdown", debut, None,
                           {"kind": genre, "origin": "event_log",
                            "initiator": initiateur},
                           f"shutdown|{schema.format_ts(debut)}"))

        elif fournisseur == "EventLog" and ident == 6008:
            debut = _systemtime_utc(f["binary"]) or f["created"]
            sortie.append(("system_shutdown", debut, None,
                           {"kind": "unexpected", "origin": "event_log",
                            "initiator": None},
                           f"shutdown|{schema.format_ts(debut)}"))

        elif fournisseur.endswith("Power-Troubleshooter") and ident == 1:
            debut = _instant(data.get("SleepTime"))
            fin = _instant(data.get("WakeTime"))

            # Avant chaque hibernation, Windows ecrit un evenement fantome :
            # TargetState 0, duree nulle, meme SleepTime que le vrai. Le
            # garder creerait une veille de 0 s (et, dans le Data Lake, la
            # vraie serait ecartee comme doublon).
            if debut is None or fin is None or fin <= debut \
                    or data.get("TargetState") == "0":
                continue

            etat = {"2": "sleep", "3": "sleep", "4": "sleep",
                    "5": "hibernate", "6": "hibernate"}.get(
                data.get("TargetState") or "", "unknown")
            source = (data.get("WakeSourceText") or "").strip() or None
            sortie.append(("system_sleep", debut, fin,
                           {"state": etat, "origin": "event_log",
                            "wake_source": source},
                           f"sleep|{schema.format_ts(debut)}"))

    return sortie


class EventLogCollector(PollingCollector):
    name = "system_events"
    # Reveil toutes les 30 s pour une verification qui ne coute rien ; le
    # journal n'est vraiment relu que toutes les 10 min, ou 20 s apres un
    # reveil de la machine.
    interval_s = 30.0
    RELECTURE = timedelta(minutes=10)
    APRES_REVEIL = timedelta(seconds=20)

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._derniere_lecture: datetime | None = None
        self._reveil: datetime | None = None
        ctx.bus.subscribe("resume", self._apres_reveil)

    def _apres_reveil(self, moment) -> None:
        # Windows ecrit l'evenement de veille quelques secondes APRES le
        # reveil : on relira un peu plus tard, pas tout de suite.
        self._reveil = moment or utc_now()

    def poll(self, now: datetime) -> None:
        if self._reveil is not None:
            if now - self._reveil < self.APRES_REVEIL:
                return
        elif self._derniere_lecture is not None and \
                now - self._derniere_lecture < self.RELECTURE:
            return

        self._reveil = None
        self._derniere_lecture = now
        curseur = self.ctx.outbox.get_state(CLE_CURSEUR)
        depuis = schema.parse_ts(curseur) if curseur else now - RATTRAPAGE

        faits = query(depuis)
        plus_recent = depuis

        for type_, debut, fin, payload, cle in to_events(faits):
            if fin is None:
                self.ctx.factory.point(type_, "windows.eventlog", debut,
                                       payload, naturelle=cle,
                                       historique=True)
            else:
                self.ctx.factory.interval(type_, "windows.eventlog", debut,
                                          fin, payload, naturelle=cle,
                                          historique=True)

        for fait in faits:
            if fait["created"] and fait["created"] > plus_recent:
                plus_recent = fait["created"]

        # Une demande d'arret (1074) sans son arret (13) n'est pas encore
        # complete : on la relira au prochain passage.
        demandes = [f["created"] for f in faits
                    if f["provider"] == "User32" and f["created"]]

        if demandes and max(demandes) >= plus_recent:
            plus_recent = max(demandes) - timedelta(milliseconds=1)

        self.ctx.outbox.set_state(CLE_CURSEUR, schema.format_ts(plus_recent))
