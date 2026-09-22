"""media_playback : ce qui joue (Spotify, YouTube dans Chrome, VLC...).

Source : les controles multimedias de Windows (la vignette qui apparait
avec les touches de volume). Toute application qui s'y declare est vue,
navigateurs compris, avec titre, artiste, album et type (musique, video).

Dependance OPTIONNELLE : les paquets `winrt-*` (liaisons officielles
Windows Runtime pour Python). Sans eux, le collecteur se declare
"unavailable" et le reste du tracker tourne normalement.

Sondage toutes les 5 s : un intervalle commence quand un titre joue,
finit quand il change, se met en pause ou disparait. Precision : 5 s.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime

from pc.apps import canonical_app
from pc.tracker.collectors.base import PollingCollector

PLAYING = 4
TYPES = {1: "music", 2: "video", 3: "image"}


@dataclass(frozen=True, slots=True)
class Lecture:
    player: str
    title: str | None
    artist: str | None
    album: str | None
    media_type: str

    def payload(self) -> dict:
        return asdict(self)


def _genre(info) -> str:
    """Musique, video ou inconnu.

    L'enumeration MediaPlaybackType vit dans le paquet winrt-Windows.Media.
    S'il manque, la lecture du champ leve une erreur : le type reste
    "unknown" plutot que de faire tomber le collecteur.
    """
    try:
        valeur = info.playback_type
    except (AttributeError, ImportError, RuntimeError):
        return "unknown"

    if valeur is None:
        return "unknown"

    try:
        return TYPES.get(int(valeur), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def player_id(aumid: str | None) -> str:
    """"Spotify.exe" -> spotify ; "MSEdge" -> edge ;
    "SpotifyAB.SpotifyMusic_zpd...!Spotify" -> spotify."""
    if not aumid:
        return "unknown"

    nom = aumid.split("!")[-1] if "!" in aumid else aumid
    return canonical_app(nom)[0]


class MediaCollector(PollingCollector):
    name = "media"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.media_interval_s
        # Import differe : le paquet est optionnel.
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        self._manager_class = Manager
        self.courante: Lecture | None = None
        self.debut: datetime | None = None

    async def _lire(self) -> Lecture | None:
        manager = await self._manager_class.request_async()
        sessions = list(manager.get_sessions())
        courante = manager.get_current_session()

        if courante is not None:
            sessions.insert(0, courante)

        for session in sessions:
            info = session.get_playback_info()

            if info is None or info.playback_status != PLAYING:
                continue

            props = await session.try_get_media_properties_async()
            genre = _genre(info)

            return Lecture(
                player=player_id(session.source_app_user_model_id),
                title=(props.title or None) if props else None,
                artist=(props.artist or None) if props else None,
                album=(props.album_title or None) if props else None,
                media_type=genre)

        return None

    def poll(self, now: datetime) -> None:
        lecture = asyncio.run(self._lire())

        regles = self.ctx.config.privacy

        if lecture is not None and regles.app_excluded(lecture.player, None):
            lecture = None if regles.mask_mode == "drop" else Lecture(
                "private", None, None, None, "unknown")

        if lecture == self.courante:
            return

        raison = "track_change" if lecture is not None else "pause"
        self._fermer(now, raison)

        if lecture is not None:
            self.courante, self.debut = lecture, now

    def _fermer(self, fin: datetime, raison: str) -> None:
        if self.courante is not None and self.debut is not None \
                and fin > self.debut:
            self.ctx.factory.interval("media_playback", "windows.media",
                                      self.debut, fin, {
                                          **self.courante.payload(),
                                          "end_reason": raison})

        self.courante, self.debut = None, None

    def stop(self, raison: str = "tracker_stop") -> None:
        from pc.tracker.core.clock import utc_now
        self._fermer(utc_now(), raison)

    def snapshot(self) -> dict | None:
        if self.courante is None or self.debut is None:
            return None

        return {"lecture": self.courante.payload(),
                "start": self.debut.isoformat()}

    def start(self) -> None:
        etat = self.recover()
        fin = self.ctx.previous_heartbeat

        if etat and fin is not None:
            from pc import schema
            from pc.tracker.core.events import dedup_key

            debut = datetime.fromisoformat(etat["start"])
            cle = dedup_key(self.ctx.device_id, "media_playback",
                            schema.format_ts(debut))

            # Deja ferme normalement juste avant l'arret : ne pas ecraser.
            if fin > debut and not self.ctx.outbox.has_dedup_key(cle):
                self.ctx.factory.interval(
                    "media_playback", "windows.media", debut, fin,
                    {**etat["lecture"], "end_reason": "recovered"},
                    historique=True)
