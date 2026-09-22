"""Fabrique d'evenements : un fait -> une enveloppe valide.

Tout collecteur passe par ici. C'est le seul endroit qui connait
l'enveloppe, et le seul qui valide : un evenement invalide n'entre JAMAIS
dans la file. Il est journalise et jete, comme les sentinelles de Polar -
une donnee fausse en base est pire qu'une donnee absente.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Callable

from pc import schema
from pc.tracker.core.clock import UniqueClock, local_iso, uuid7

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]


def dedup_key(device_id: str, event_type: str, naturelle: str) -> str:
    """Cle naturelle : le meme fait vu deux fois donne la meme cle.

    32 caracteres hexadecimaux (128 bits) : largement assez pour ne jamais
    collisionner par hasard, et sous la limite de 64 de l'enveloppe.
    """
    texte = f"{device_id}|{event_type}|{naturelle}"
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()[:32]


class EventFactory:
    """Construit, valide et emet les evenements d'une machine."""

    def __init__(self, device_id: str, emit: Emit, tz_name: str | None = None,
                 clock: UniqueClock | None = None) -> None:
        if not schema.valid_device_id(device_id):
            raise ValueError(f"device_id invalide : {device_id!r} "
                             f"(minuscules, chiffres, tirets, 2 a 40)")

        self.device_id = device_id
        self.tz_name = tz_name
        self._emit = emit
        self._clock = clock or UniqueClock()
        self.rejetes = 0

    # ------------------------------------------------------------ interne

    def _envelope(self, event_type: str, producer: str, ts: datetime,
                  payload: dict[str, Any], naturelle: str | None) -> dict:
        ts_texte = schema.format_ts(ts)

        return {
            "event_id": uuid7(),
            "source": schema.SOURCE,
            "producer": producer,
            "event_type": event_type,
            "ts": ts_texte,
            "ts_local": local_iso(ts),
            "tz": self.tz_name,
            "device_id": self.device_id,
            "dedup_key": dedup_key(self.device_id, event_type,
                                   naturelle or ts_texte),
            "schema_v": schema.SCHEMA_VERSION,
            "payload": payload,
        }

    def _publier(self, event: dict) -> dict | None:
        erreurs = schema.validate_event(event)

        if erreurs:
            self.rejetes += 1
            logger.error("evenement %s rejete avant la file : %s",
                         event.get("event_type"), "; ".join(erreurs))
            return None

        self._emit(event)
        return event

    # ------------------------------------------------------------- public

    def point(self, event_type: str, producer: str, ts: datetime,
              payload: dict[str, Any], naturelle: str | None = None,
              historique: bool = False) -> dict | None:
        """Un evenement ponctuel, un echantillon ou un etat.

        `historique` : l'instant vient du systeme (commit, journal,
        creation de processus) et doit rester EXACT pour que le meme fait
        relu deux fois donne la meme cle. Sinon il passe par UniqueClock.
        """
        if not historique:
            ts = self._clock.unique(event_type, ts)

        return self._publier(self._envelope(event_type, producer, ts,
                                            dict(payload), naturelle))

    def interval(self, event_type: str, producer: str, start: datetime,
                 end: datetime, payload: dict[str, Any],
                 naturelle: str | None = None,
                 historique: bool = False) -> dict | None:
        """Un intervalle : ts = debut, ended_at et duration_s dans le payload.

        Une fin anterieure au debut (horloge reculee par NTP pendant
        l'intervalle) est ramenee au debut : duree nulle plutot que negative.
        """
        if not historique:
            start = self._clock.unique(event_type, start)

        if end < start:
            end = start

        complet = dict(payload)
        complet["ended_at"] = schema.format_ts(end)
        complet["duration_s"] = round((end - start).total_seconds(), 6)

        return self._publier(self._envelope(event_type, producer, start,
                                            complet, naturelle))
