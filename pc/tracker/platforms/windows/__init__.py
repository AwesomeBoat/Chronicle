"""Plateforme Windows (10 et 11).

Tout passe par ctypes et par les outils livres avec Windows (wevtutil,
netsh, schtasks) : rien a compiler. Seule dependance optionnelle : les
paquets winrt-* pour le collecteur media.
"""

from __future__ import annotations

import ctypes
import logging
import threading

from pc.tracker.collectors.base import Collector
from pc.tracker.platforms.windows import win32 as w
from pc.tracker.platforms.windows.hooks import WindowsHooks

NAME = "windows"
logger = logging.getLogger(__name__)


def hooks(config) -> WindowsHooks:
    return WindowsHooks()


def collectors(ctx, tracker) -> list[Collector]:
    """Les collecteurs propres a Windows, selon la configuration."""
    from pc.tracker.platforms.windows.desktop import WindowsDesktop

    cfg = ctx.config
    liste: list[Collector] = []

    if any(cfg.enabled(n) for n in ("focus", "idle", "input", "session")) \
            or cfg.enabled("browser"):
        liste.append(WindowsDesktop(ctx, tracker))

    optionnels = [
        ("processes", "pc.tracker.platforms.windows.processes",
         "ProcessCollector"),
        ("system_events", "pc.tracker.platforms.windows.eventlog",
         "EventLogCollector"),
        ("files", "pc.tracker.platforms.windows.files", "WindowsFileWatcher"),
        ("media", "pc.tracker.platforms.windows.media", "MediaCollector"),
        ("notifications", "pc.tracker.platforms.windows.notifications",
         "NotificationCollector"),
    ]

    for nom, module, classe in optionnels:
        if not cfg.enabled(nom):
            continue

        try:
            mod = __import__(module, fromlist=[classe])
            liste.append(getattr(mod, classe)(ctx))
        except (ImportError, OSError) as erreur:
            # Paquet optionnel absent, base introuvable : le collecteur est
            # declare indisponible, le tracker continue.
            logger.warning("collecteur %s indisponible : %s", nom, erreur)
            ctx.status(nom, "unavailable", str(erreur)[:200])

    return liste


# ------------------------------------------------------ instance, arret

def _nom(prefixe: str, device_id: str) -> str:
    return f"Local\\ChroniclePC-{prefixe}-{device_id}"


def single_instance(device_id: str):
    """Un mutex nomme : un deuxieme tracker sur la meme session s'arrete.

    Renvoie le handle (a garder vivant) ou None si un tracker tourne deja.
    """
    handle = w.kernel32.CreateMutexW(None, False, _nom("run", device_id))

    if ctypes.get_last_error() == w.ERROR_ALREADY_EXISTS:
        w.kernel32.CloseHandle(handle)
        return None

    return handle


def is_running(device_id: str) -> bool:
    handle = single_instance(device_id)

    if handle is None:
        return True

    w.kernel32.CloseHandle(handle)
    return False


def stop_listener(tracker) -> threading.Thread:
    """Attend l'evenement nomme que `python -m pc.tracker stop` declenche."""
    evenement = w.kernel32.CreateEventW(None, True, False,
                                        _nom("stop", tracker.config.device_id))

    def attendre() -> None:
        while not tracker.stopping:
            if w.kernel32.WaitForSingleObject(evenement, 1000) == \
                    w.WAIT_OBJECT_0:
                tracker.request_stop("tracker_stop")
                break

        w.kernel32.CloseHandle(evenement)

    fil = threading.Thread(target=attendre, name="stop-listener", daemon=True)
    fil.start()
    return fil


def signal_stop(device_id: str) -> bool:
    """Demande l'arret du tracker en cours. False s'il ne tourne pas."""
    EVENT_MODIFY_STATE = 0x0002
    evenement = w.kernel32.OpenEventW(EVENT_MODIFY_STATE, False,
                                      _nom("stop", device_id))

    if not evenement:
        return False

    w.kernel32.SetEvent(evenement)
    w.kernel32.CloseHandle(evenement)
    return True
