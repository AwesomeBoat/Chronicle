"""Plateforme Linux (Omarchy / Hyprland) : PHASE 6, pas encore faite.

Ce module ne fait que le MINIMUM pour que le tracker demarre sous Linux :
instance unique, arret propre, indices systeme (temperature, GPU AMD,
SSID). Avec lui, les collecteurs COMMUNS tournent deja : git, terminal,
metriques, reseau, etat de la machine, recepteur de l'extension.

Les collecteurs de BUREAU (fenetre active, inactivite, entrees, verrou,
veille, media, notifications, fichiers) restent a ecrire, APRES l'audit
du tracker Omarchy existant : voir README.md dans ce dossier.

Non teste sur Linux a ce jour (ecrit et verifie sous Windows seulement).
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

import psutil

from pc.tracker.collectors.base import Collector, PlatformHooks
from pc.tracker.core.clock import detect_timezone

NAME = "linux"
logger = logging.getLogger(__name__)


def _dossier() -> Path:
    from pc.tracker.config import base_dirs

    dossier = base_dirs()[1]
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier


class LinuxHooks(PlatformHooks):
    def device_details(self) -> dict:
        details: dict = {}

        try:
            texte = Path("/etc/os-release").read_text(encoding="utf-8")
            trouve = re.search(r'^PRETTY_NAME="?(.*?)"?$', texte, re.M)
            details["os_version"] = trouve.group(1) if trouve else None
        except OSError:
            pass

        try:
            texte = Path("/proc/cpuinfo").read_text(encoding="utf-8")
            trouve = re.search(r"^model name\s*:\s*(.+)$", texte, re.M)
            details["cpu_model"] = trouve.group(1).strip() if trouve else None
        except OSError:
            pass

        details["os_build"] = os.uname().release
        return details

    def cpu_temperature(self) -> float | None:
        try:
            capteurs = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None

        for nom in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
            for mesure in capteurs.get(nom, []):
                if mesure.current:
                    return float(mesure.current)

        return None

    def gpu_sample(self) -> tuple[float | None, float | None]:
        """GPU AMD (amdgpu) : lu dans /sys, sans outil externe."""
        for carte in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            appareil = carte / "device"

            try:
                utilisation = float((appareil / "gpu_busy_percent").read_text())
            except (OSError, ValueError):
                continue

            try:
                vram = float((appareil / "mem_info_vram_used").read_text()) \
                    / 2**20
            except (OSError, ValueError):
                vram = None

            return utilisation, vram

        return None, None

    def wifi_ssid(self) -> str | None:
        for commande in (["iwgetid", "-r"],
                         ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
            try:
                sortie = subprocess.run(commande, capture_output=True,
                                        text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                continue

            if commande[0] == "nmcli":
                actives = [l.split(":", 1)[1] for l in sortie.splitlines()
                           if l.startswith("yes:")]
                sortie = actives[0] if actives else ""

            if sortie:
                return sortie

        return None

    def interface_kinds(self) -> dict[str, str]:
        types = {}

        for interface in Path("/sys/class/net").glob("*"):
            if (interface / "wireless").exists():
                types[interface.name] = "wifi"

        return types

    def timezone(self) -> str | None:
        return detect_timezone()


def hooks(config) -> LinuxHooks:
    return LinuxHooks()


def collectors(ctx, tracker) -> list[Collector]:
    logger.warning("collecteurs de bureau Linux non encore ecrits (phase 6) "
                   ": voir pc/tracker/platforms/linux/README.md")
    ctx.status("desktop", "unavailable",
               "phase 6 : pc/tracker/platforms/linux/README.md")
    return []


# ------------------------------------------------------ instance, arret

def _verrou(device_id: str) -> Path:
    return _dossier() / f"tracker-{device_id}.lock"


def _pid(device_id: str) -> Path:
    return _dossier() / f"tracker-{device_id}.pid"


def single_instance(device_id: str):
    """Verrou fcntl : libere par le noyau si le processus meurt."""
    import fcntl

    fichier = open(_verrou(device_id), "w")

    try:
        fcntl.flock(fichier, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fichier.close()
        return None

    return fichier


def is_running(device_id: str) -> bool:
    fichier = single_instance(device_id)

    if fichier is None:
        return True

    fichier.close()
    return False


def stop_listener(tracker) -> threading.Thread:
    """SIGTERM (systemctl stop, `stop`) est deja gere par la CLI ; on ne
    fait qu'ecrire le pid pour que `stop` sache qui prevenir."""
    _pid(tracker.config.device_id).write_text(str(os.getpid()))
    fil = threading.Thread(target=lambda: None, daemon=True)
    fil.start()
    return fil


def signal_stop(device_id: str) -> bool:
    try:
        pid = int(_pid(device_id).read_text().strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False
