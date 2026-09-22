"""Ce qui depend du systeme : un module par plateforme.

Chaque module expose la meme interface :

    NAME                         "windows" | "linux"
    hooks(config)                PlatformHooks (GPU, SSID, details)
    collectors(ctx, tracker)     les collecteurs propres au systeme
    single_instance(device_id)   empeche deux trackers en meme temps
    stop_listener(tracker)       ecoute la demande d'arret (`stop`)
    signal_stop(device_id)       envoie cette demande
"""

from __future__ import annotations

import sys


def load():
    if sys.platform == "win32":
        from pc.tracker.platforms import windows
        return windows

    if sys.platform.startswith("linux"):
        from pc.tracker.platforms import linux
        return linux

    raise RuntimeError(f"plateforme non geree : {sys.platform}")
