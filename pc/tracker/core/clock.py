"""Le temps : instants UTC, heure locale, identifiants UUIDv7.

Trois regles, reprises de Chronicle et du Data Lake :

1. Un instant est TOUJOURS un datetime avec fuseau, en UTC. L'heure locale
   n'est qu'un rendu, ajoute a cote (ts_local).
2. L'heure locale vient du systeme au moment de l'evenement, decalage
   compris : un evenement de 23h30 en ete porte +02:00, en hiver +01:00.
3. Deux evenements du meme type ne partagent jamais le meme instant sur
   une machine (UniqueClock). Chronicle identifie un episode par
   (source, kind, started_at) : deux fichiers modifies dans la meme
   microseconde s'ecraseraient en silence.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

UN_MICRO = timedelta(microseconds=1)


def utc_now() -> datetime:
    """Maintenant, en UTC, a la microseconde."""
    return from_epoch_ns(time.time_ns())


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def from_epoch_ns(nanoseconds: int) -> datetime:
    secondes, reste = divmod(nanoseconds, 1_000_000_000)
    return (datetime.fromtimestamp(secondes, tz=timezone.utc)
            + timedelta(microseconds=reste // 1000))


def local_iso(moment: datetime) -> str:
    """L'heure vecue : "2026-09-22T14:02:11.123456+02:00".

    astimezone() sans argument applique le fuseau du systeme A CET
    INSTANT-LA, heure d'ete comprise. Aucune base tzdata n'est necessaire,
    ce qui compte sous Windows ou Python n'en embarque pas.
    """
    return moment.astimezone().isoformat(timespec="microseconds")


def detect_timezone() -> str | None:
    """Nom IANA du fuseau local, si on peut le connaitre sans deviner.

    Linux : TZ, puis le lien /etc/localtime. Windows : voir
    platforms/windows (le registre ne donne pas un nom IANA). Renvoie
    None plutot qu'un nom invente : `tz` est une metadonnee, ts_local
    porte deja le decalage exact.
    """
    env = os.environ.get("TZ")

    if env and "/" in env:
        return env.lstrip(":")

    try:
        cible = os.path.realpath("/etc/localtime")
    except OSError:
        return None

    marqueur = "zoneinfo/"

    if marqueur in cible:
        return cible.split(marqueur, 1)[1]

    return None


# ---------------------------------------------------------------- UUIDv7

_verrou_uuid = threading.Lock()
_dernier_ms = -1
_compteur = 0


def uuid7() -> str:
    """UUIDv7 (RFC 9562) : 48 bits de millisecondes Unix en tete.

    Trie comme le temps, genere hors ligne sans coordination : c'est ce qui
    permet a la machine de fabriquer ses identifiants sans jamais parler au
    serveur. Dans une meme milliseconde, un compteur de 12 bits garde
    l'ordre de creation (methode 1 de la RFC).
    """
    global _dernier_ms, _compteur

    with _verrou_uuid:
        ms = time.time_ns() // 1_000_000

        if ms <= _dernier_ms:
            ms = _dernier_ms
            _compteur += 1

            if _compteur > 0xFFF:
                # 4096 identifiants dans la meme milliseconde : on emprunte
                # la suivante plutot que de casser l'ordre.
                ms += 1
                _compteur = secrets.randbits(10)
        else:
            _compteur = secrets.randbits(10)

        _dernier_ms = ms
        rand_a = _compteur

    rand_b = secrets.randbits(62)
    valeur = (ms & 0xFFFFFFFFFFFF) << 80
    valeur |= 0x7 << 76
    valeur |= (rand_a & 0xFFF) << 64
    valeur |= 0b10 << 62
    valeur |= rand_b

    hexa = f"{valeur:032x}"
    return f"{hexa[:8]}-{hexa[8:12]}-{hexa[12:16]}-{hexa[16:20]}-{hexa[20:]}"


def uuid7_time(identifiant: str) -> datetime:
    """L'instant de creation lu dans un UUIDv7 (utile en test)."""
    ms = int(identifiant.replace("-", "")[:12], 16)
    return from_epoch(ms / 1000)


# ------------------------------------------------------------ unicite

class UniqueClock:
    """Garantit des instants strictement croissants par cle.

    unique("file_change", t) renvoie t, sauf si t n'est pas posterieur au
    dernier instant rendu pour cette cle : il renvoie alors ce dernier
    + 1 microseconde. L'ecart introduit est d'une microseconde, dix mille
    fois sous la precision reelle d'un horodatage de fichier ou de fenetre.
    Il preserve l'ordre de reception et l'unicite, et il est documente.

    Ne s'applique qu'aux evenements NOUVEAUX : un commit git ou un demarrage
    lu dans le journal garde son instant historique.
    """

    def __init__(self) -> None:
        self._dernier: dict[str, datetime] = {}
        self._verrou = threading.Lock()

    def unique(self, cle: str, moment: datetime) -> datetime:
        with self._verrou:
            precedent = self._dernier.get(cle)

            if precedent is not None and moment <= precedent:
                moment = precedent + UN_MICRO

            self._dernier[cle] = moment
            return moment
