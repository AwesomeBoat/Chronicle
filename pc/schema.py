"""Le contrat commun des evenements PC. (source = "pc", schema_v = 1)

Ce module est la SEULE definition du format. Trois lecteurs l'importent :

    pc/tracker/          le produit, sur chaque machine (Windows, Linux)
    pc/mapper.py         le traduit en Batch, cote Chronicle
    api/                 le valide a la reception

Il ne depend que de la bibliotheque standard : il doit pouvoir etre copie
tel quel sur une machine ou rien d'autre n'est installe.

L'ENVELOPPE
-----------
Celle de PhoneTracker/docs/EVENT-SCHEMA.md, a l'identique :

    event_id    UUIDv7, genere sur la machine, jamais par le serveur
    source      toujours "pc"
    producer    le chemin de collecte ("windows.foreground", "git"...)
    event_type  le type, voir EVENT_TYPES
    ts          UTC, a la microseconde, suffixe Z
    ts_local    l'heure vecue, avec son decalage
    tz          fuseau IANA, si connu
    device_id   la machine : "windows-main", "omarchy-desktop"
    dedup_key   le meme fait vu par deux chemins donne la meme cle
    schema_v    1
    payload     les champs du type

UNE MACHINE CHANGE `device_id` ET `producer`, JAMAIS LE RESTE
-------------------------------------------------------------
`event_type` et les champs du payload sont identiques sous Windows et sous
Linux. Il n'existe pas de variante par systeme : une requete ecrite pour
une machine marche pour l'autre.

QUATRE FORMES
-------------
    interval   ts = debut, payload.ended_at = fin, payload.duration_s
    point      ts = l'instant
    sample     des nombres mesures sur payload.interval_s secondes
    snapshot   un etat (le materiel, l'OS)

Les champs inconnus du payload sont TOLERES : ajouter un champ ne demande
pas de nouvelle version. Changer le sens d'un champ, si.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

SOURCE = "pc"
SCHEMA_VERSION = 1

INTERVAL = "interval"
POINT = "point"
SAMPLE = "sample"
SNAPSHOT = "snapshot"

# Minuscules, chiffres, tirets. 2 a 40 caracteres. Le device_id devient le
# code d'une source Chronicle ("pc:windows-main", 64 caracteres max) : il
# doit etre court, stable et sans surprise dans une URL ou un nom de fichier.
DEVICE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])$")

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

PRODUCER_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")

# Motif des valeurs enumerees libres (end_reason...).
TOKEN_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Plausibilite des horloges. Un PC dont l'horloge est fausse produit des
# instants absurdes qui passent toutes les contraintes : meme lecon que
# l'ESP32 de sensors/bedroom.py. On refuse plutot que de rapiecer.
EARLIEST = datetime(2020, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE = timedelta(days=2)

# Garde-fou, pas une regle metier : un titre ou une commande de 4 Ko est
# deja anormal. Le tracker tronque bien avant.
MAX_STRING = 4096

# Tolerance entre duration_s et (ended_at - ts).
DURATION_TOLERANCE_S = 1.0


# --------------------------------------------------------------- temps

def format_ts(moment: datetime) -> str:
    """datetime -> "2026-09-22T12:02:11.123456Z" (UTC, microsecondes).

    Refuse un datetime naif : sans fuseau, un instant n'en est pas un.
    """
    if moment.tzinfo is None:
        raise ValueError("datetime naif : fuseau obligatoire")

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> datetime:
    """Horodatage ISO-8601 -> datetime UTC. Leve ValueError si naif."""
    if not isinstance(value, str):
        raise ValueError(f"horodatage attendu, recu {type(value).__name__}")

    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if moment.tzinfo is None:
        raise ValueError(f"horodatage sans fuseau : {value}")

    return moment.astimezone(timezone.utc)


# ------------------------------------------------------------- catalogue

@dataclass(frozen=True)
class Champ:
    """Un champ de payload : ses types, obligatoire ou non, None permis."""

    types: tuple[type, ...]
    requis: bool = False
    nullable: bool = True
    valeurs: frozenset[str] | None = None     # enumeration fermee


def _c(*types: type, requis: bool = False, nullable: bool = True,
       valeurs: set[str] | None = None) -> Champ:
    return Champ(types, requis, nullable,
                 frozenset(valeurs) if valeurs else None)


# Un nombre : entier ou flottant (jamais un booleen, voir _type_ok).
NUM = (int, float)

# Champs communs a tous les intervalles.
_INTERVALLE = {
    "ended_at": _c(str, requis=True, nullable=False),
    "duration_s": _c(*NUM, requis=True, nullable=False),
}

# L'identite d'une application, partagee par app_focus / app_launch /
# app_process. `app` est canonique (pc/apps.py), `process` est brut.
_APPLICATION = {
    "app": _c(str, requis=True, nullable=False),
    "app_name": _c(str),
    "process": _c(str),
    "exe_path": _c(str),
    "pid": _c(int),
}

_FIN = {"end_reason": _c(str, requis=True, nullable=False)}


@dataclass(frozen=True)
class EventSpec:
    """La definition d'un type d'evenement."""

    forme: str
    champs: dict[str, Champ]
    description: str


EVENT_TYPES: dict[str, EventSpec] = {
    # ------------------------------------------------------ intervalles
    "app_focus": EventSpec(INTERVAL, {
        **_INTERVALLE, **_APPLICATION, **_FIN,
        "window_title": _c(str),
        "window_class": _c(str),
        "input_active_s": _c(*NUM),
        "private": _c(bool),
    }, "Une fenetre au premier plan, de son activation a sa perte."),

    "app_process": EventSpec(INTERVAL, {
        **_INTERVALLE, **_APPLICATION,
    }, "Vie d'un processus fenetre : de sa creation a sa sortie."),

    "browser_page": EventSpec(INTERVAL, {
        **_INTERVALLE, **_FIN,
        "browser": _c(str, requis=True, nullable=False),
        "domain": _c(str),
        "url": _c(str),
        "page_title": _c(str),
        "input_active_s": _c(*NUM),
        "private": _c(bool),
    }, "Un onglet actif pendant que son navigateur est au premier plan."),

    "idle": EventSpec(INTERVAL, {
        **_INTERVALLE, **_FIN,
        "threshold_s": _c(*NUM, requis=True, nullable=False),
    }, "Aucune entree clavier/souris pendant au moins threshold_s. "
       "Commence a la derniere entree, pas au franchissement du seuil."),

    "session_locked": EventSpec(INTERVAL, {
        **_INTERVALLE, **_FIN,
    }, "Session verrouillee, du verrouillage au deverrouillage."),

    "system_sleep": EventSpec(INTERVAL, {
        **_INTERVALLE,
        "state": _c(str, requis=True, nullable=False,
                    valeurs={"sleep", "hibernate", "unknown"}),
        "origin": _c(str, requis=True, nullable=False,
                     valeurs={"live", "event_log"}),
        "wake_source": _c(str),
    }, "Veille ou hibernation, de l'endormissement au reveil."),

    "media_playback": EventSpec(INTERVAL, {
        **_INTERVALLE, **_FIN,
        "player": _c(str, requis=True, nullable=False),
        "title": _c(str),
        "artist": _c(str),
        "album": _c(str),
        "media_type": _c(str, valeurs={"music", "video", "image",
                                       "unknown"}),
    }, "Lecture d'un media (piste, video), tant qu'il joue."),

    "terminal_command": EventSpec(INTERVAL, {
        **_INTERVALLE,
        "shell": _c(str, requis=True, nullable=False),
        "command": _c(str, requis=True),
        "redacted": _c(bool, requis=True, nullable=False),
        "program": _c(str),
        "terminal": _c(str),
        "cwd": _c(str),
        "exit_code": _c(int),
    }, "Une commande de terminal, secrets rediges AVANT le stockage."),

    "tracker_run": EventSpec(INTERVAL, {
        **_INTERVALLE, **_FIN,
        "version": _c(str, requis=True, nullable=False),
        "platform": _c(str),
        "collectors": _c(list),
    }, "Le tracker lui-meme : de son demarrage a son arret. Dit quand "
       "une absence de donnees est une absence de mesure."),

    # ----------------------------------------------------------- points
    "app_launch": EventSpec(POINT, {**_APPLICATION},
                            "Un processus fenetre apparait. ts = sa "
                            "creation, lue dans le systeme."),

    "system_boot": EventSpec(POINT, {
        "origin": _c(str, requis=True, nullable=False,
                     valeurs={"live", "event_log"}),
    }, "Demarrage du systeme."),

    "system_shutdown": EventSpec(POINT, {
        "kind": _c(str, requis=True, nullable=False,
                   valeurs={"power_off", "restart", "unexpected",
                            "unknown"}),
        "origin": _c(str, requis=True, nullable=False,
                     valeurs={"live", "event_log"}),
        "initiator": _c(str),
    }, "Arret du systeme, planifie ou brutal."),

    "network_change": EventSpec(POINT, {
        "connected": _c(bool, requis=True, nullable=False),
        "interfaces": _c(list, requis=True, nullable=False),
        "wifi_ssid_hash": _c(str),
    }, "Changement de connectivite. Le SSID est hache, jamais stocke."),

    "file_change": EventSpec(POINT, {
        "action": _c(str, requis=True, nullable=False,
                     valeurs={"created", "modified", "deleted", "renamed"}),
        "path": _c(str, requis=True, nullable=False),
        # null quand le systeme ne le dit pas (element deja supprime).
        "is_dir": _c(bool, requis=True),
        "dest_path": _c(str),
        "extension": _c(str),
        "root": _c(str),
    }, "Creation, modification, suppression, renommage. Jamais le contenu."),

    "git_commit": EventSpec(POINT, {
        "repo": _c(str, requis=True, nullable=False),
        "commit": _c(str, requis=True, nullable=False),
        "repo_path": _c(str),
        "branch": _c(str),
        "message": _c(str),
        "files_changed": _c(int),
        "insertions": _c(int),
        "deletions": _c(int),
        "amend": _c(bool),
        "merge": _c(bool),
    }, "Un commit fait sur cette machine (lu dans le reflog)."),

    "git_checkout": EventSpec(POINT, {
        "repo": _c(str, requis=True, nullable=False),
        "repo_path": _c(str),
        "from_ref": _c(str),
        "to_ref": _c(str),
        "commit": _c(str),
    }, "Un changement de branche."),

    "notification": EventSpec(POINT, {
        "app": _c(str, requis=True, nullable=False),
        "notification_type": _c(str, requis=True, nullable=False),
        "app_name": _c(str),
    }, "Une notification recue. Jamais son contenu."),

    "collector_status": EventSpec(POINT, {
        "collector": _c(str, requis=True, nullable=False),
        "status": _c(str, requis=True, nullable=False,
                     valeurs={"started", "stopped", "error",
                              "rate_limited", "disabled", "unavailable"}),
        "detail": _c(str),
    }, "La source s'observe elle-meme : un trou de collecte est "
       "enregistre, jamais confondu avec une absence d'activite."),

    # ------------------------------------------------------ echantillons
    "system_metrics": EventSpec(SAMPLE, {
        "interval_s": _c(*NUM, requis=True, nullable=False),
        "cpu_pct": _c(*NUM),
        "ram_pct": _c(*NUM),
        "ram_used_mb": _c(*NUM),
        "swap_pct": _c(*NUM),
        "gpu_pct": _c(*NUM),
        "vram_used_mb": _c(*NUM),
        "cpu_temp_c": _c(*NUM),
        "disk_used_pct": _c(*NUM),
        "disk_read_bytes": _c(*NUM),
        "disk_write_bytes": _c(*NUM),
        "net_up_bytes": _c(*NUM),
        "net_down_bytes": _c(*NUM),
        "battery_pct": _c(*NUM),
        "on_ac": _c(bool),
    }, "Mesures systeme. Une valeur inconnue vaut null, jamais 0."),

    "input_activity": EventSpec(SAMPLE, {
        "interval_s": _c(*NUM, requis=True, nullable=False),
        "keys": _c(int, requis=True, nullable=False),
        "clicks": _c(int, requis=True, nullable=False),
        "scroll": _c(*NUM, requis=True, nullable=False),
        "active_s": _c(int, requis=True, nullable=False),
        "mouse_distance": _c(*NUM),
    }, "Comptes d'entrees sur une minute. PAS DE KEYLOGGER : aucune "
       "touche, aucun texte, seulement des nombres."),

    # ------------------------------------------------------------- etat
    "device_info": EventSpec(SNAPSHOT, {
        "hostname": _c(str, requis=True, nullable=False),
        "os": _c(str, requis=True, nullable=False,
                 valeurs={"windows", "linux", "macos"}),
        "os_version": _c(str),
        "os_build": _c(str),
        "arch": _c(str),
        "cpu_model": _c(str),
        "cpu_cores": _c(int),
        "ram_total_mb": _c(*NUM),
        "gpus": _c(list),
        "tracker_version": _c(str),
        "python_version": _c(str),
        "timezone": _c(str),
    }, "La machine : materiel, systeme, version du tracker."),
}

INTERVAL_TYPES = frozenset(t for t, s in EVENT_TYPES.items()
                           if s.forme == INTERVAL)
POINT_TYPES = frozenset(t for t, s in EVENT_TYPES.items() if s.forme == POINT)
SAMPLE_TYPES = frozenset(t for t, s in EVENT_TYPES.items()
                         if s.forme == SAMPLE)
SNAPSHOT_TYPES = frozenset(t for t, s in EVENT_TYPES.items()
                           if s.forme == SNAPSHOT)


# ------------------------------------------------------------ validation

def valid_device_id(device_id: object) -> bool:
    return isinstance(device_id, str) and bool(DEVICE_ID_RE.match(device_id))


def _type_ok(valeur: Any, champ: Champ) -> bool:
    # bool est un int en Python : sans ce test, True passerait pour un
    # nombre de cles tapees, et 1 pour un booleen.
    if isinstance(valeur, bool):
        return bool in champ.types

    return isinstance(valeur, champ.types)


def validate_payload(event_type: str, payload: object) -> list[str]:
    """Erreurs du payload d'un type donne. Liste vide = valide."""
    spec = EVENT_TYPES.get(event_type)

    if spec is None:
        return [f"event_type inconnu : {event_type!r}"]

    if not isinstance(payload, dict):
        return ["payload doit etre un objet"]

    erreurs: list[str] = []

    for nom, champ in spec.champs.items():
        if nom not in payload:
            if champ.requis:
                erreurs.append(f"{event_type}.{nom} manquant")
            continue

        valeur = payload[nom]

        if valeur is None:
            if not champ.nullable:
                erreurs.append(f"{event_type}.{nom} ne peut pas etre null")
            continue

        if not _type_ok(valeur, champ):
            attendus = "/".join(t.__name__ for t in champ.types)
            erreurs.append(f"{event_type}.{nom} : {attendus} attendu, "
                           f"recu {type(valeur).__name__}")
            continue

        if isinstance(valeur, str) and len(valeur) > MAX_STRING:
            erreurs.append(f"{event_type}.{nom} depasse {MAX_STRING} "
                           f"caracteres")

        if champ.valeurs is not None and valeur not in champ.valeurs:
            erreurs.append(f"{event_type}.{nom} = {valeur!r} hors de "
                           f"{sorted(champ.valeurs)}")

        if isinstance(valeur, float) and valeur != valeur:     # NaN
            erreurs.append(f"{event_type}.{nom} vaut NaN")

    if "end_reason" in payload and isinstance(payload["end_reason"], str) \
            and not TOKEN_RE.match(payload["end_reason"]):
        erreurs.append(f"{event_type}.end_reason mal forme")

    return erreurs


def validate_event(event: object, now: datetime | None = None) -> list[str]:
    """Erreurs d'un evenement complet (enveloppe + payload)."""
    if not isinstance(event, dict):
        return ["evenement doit etre un objet"]

    erreurs: list[str] = []

    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not UUID_RE.match(event_id):
        erreurs.append("event_id doit etre un UUID en minuscules")

    if event.get("source") != SOURCE:
        erreurs.append(f"source doit valoir {SOURCE!r}")

    producer = event.get("producer")
    if not isinstance(producer, str) or not PRODUCER_RE.match(producer):
        erreurs.append("producer mal forme")

    if not valid_device_id(event.get("device_id")):
        erreurs.append(f"device_id invalide : {event.get('device_id')!r}")

    if event.get("schema_v") != SCHEMA_VERSION:
        erreurs.append(f"schema_v doit valoir {SCHEMA_VERSION}")

    dedup = event.get("dedup_key")
    if dedup is not None and (not isinstance(dedup, str) or len(dedup) > 64):
        erreurs.append("dedup_key : chaine de 64 caracteres max")

    for nom in ("ts_local", "tz"):
        if event.get(nom) is not None and not isinstance(event[nom], str):
            erreurs.append(f"{nom} doit etre une chaine")

    event_type = event.get("event_type")
    payload = event.get("payload")

    try:
        debut = parse_ts(event.get("ts"))
    except (ValueError, TypeError) as erreur:
        erreurs.append(f"ts : {erreur}")
        debut = None

    if debut is not None:
        maintenant = now or datetime.now(timezone.utc)

        if debut < EARLIEST or debut > maintenant + MAX_FUTURE:
            erreurs.append(f"ts implausible ({event.get('ts')}) : horloge "
                           f"de la machine a verifier")

    erreurs.extend(validate_payload(event_type, payload))

    spec = EVENT_TYPES.get(event_type)

    if spec is not None and spec.forme == INTERVAL and debut is not None \
            and isinstance(payload, dict):
        erreurs.extend(_valider_intervalle(debut, payload))

    return erreurs


def _valider_intervalle(debut: datetime, payload: dict) -> list[str]:
    try:
        fin = parse_ts(payload.get("ended_at"))
    except (ValueError, TypeError) as erreur:
        return [f"ended_at : {erreur}"]

    if fin < debut:
        return ["ended_at anterieur a ts"]

    duree = payload.get("duration_s")

    if isinstance(duree, (int, float)) and not isinstance(duree, bool):
        if duree < 0:
            return ["duration_s negative"]

        ecart = abs((fin - debut).total_seconds() - duree)

        if ecart > DURATION_TOLERANCE_S:
            return [f"duration_s ({duree}) incoherente avec ended_at - ts"]

    return []


def interval_end(event: dict) -> datetime | None:
    """La fin d'un evenement : ended_at pour un intervalle, ts sinon."""
    spec = EVENT_TYPES.get(event.get("event_type"))

    if spec is None:
        return None

    if spec.forme == INTERVAL:
        return parse_ts(event["payload"]["ended_at"])

    return parse_ts(event["ts"])
