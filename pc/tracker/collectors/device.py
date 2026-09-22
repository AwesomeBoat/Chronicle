"""device_info : ce qu'est la machine (materiel, systeme, tracker).

Emis au demarrage puis toutes les 6 heures. Chronicle le range dans
profile_snapshot, dedoublonne par CONTENU : rien de neuf = aucune ligne.
Le jour ou la RAM, le systeme ou la version du tracker change, une ligne
apparait - l'historique des changements vient gratuitement, comme pour le
profil Polar.
"""

from __future__ import annotations

import platform
import socket
import sys
from datetime import datetime

import psutil

from pc.tracker import VERSION
from pc.tracker.collectors.base import PollingCollector


class DeviceCollector(PollingCollector):
    name = "device"
    interval_s = 6 * 3600.0
    # Tout de suite : chaque lancement commence par dire QUI mesure.
    first_delay_s = 0.0

    def payload(self) -> dict:
        details = self.ctx.hooks.device_details()
        systeme = {"win32": "windows", "linux": "linux",
                   "darwin": "macos"}.get(sys.platform, "linux")

        return {
            "hostname": socket.gethostname().lower(),
            "os": systeme,
            "os_version": details.get("os_version") or platform.release(),
            "os_build": details.get("os_build") or platform.version(),
            "arch": platform.machine().lower() or None,
            "cpu_model": details.get("cpu_model") or platform.processor()
            or None,
            "cpu_cores": psutil.cpu_count(logical=True),
            "ram_total_mb": round(psutil.virtual_memory().total / 2**20),
            "gpus": details.get("gpus") or [],
            "tracker_version": VERSION,
            "python_version": platform.python_version(),
            "timezone": self.ctx.factory.tz_name,
        }

    def poll(self, now: datetime) -> None:
        # Snapshot : l'instant est celui de la collecte, et c'est le
        # contenu - pas la date - qui dedoublonne cote Chronicle.
        self.ctx.factory.point("device_info", "tracker", now, self.payload())
