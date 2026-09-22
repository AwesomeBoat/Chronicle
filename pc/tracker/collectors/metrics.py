"""system_metrics : CPU, RAM, GPU, disque, reseau, batterie.

Un echantillon par intervalle (60 s par defaut). Les compteurs cumulatifs
(octets lus, envoyes) sont convertis en quantites SUR L'INTERVALLE : la
somme des echantillons d'une journee donne le trafic de la journee.

Une valeur inconnue vaut null, jamais 0. Un portable sans capteur de
temperature n'a pas une temperature de 0 degre, et un PC fixe n'a pas une
batterie vide. Le piege `cardio.strain` de la V3 (30 zeros qui n'etaient
pas des mesures) ne se reproduira pas ici.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import psutil

from pc.tracker.collectors.base import PollingCollector

# Interfaces a ne pas compter dans le trafic : boucle locale et reseaux
# virtuels (Hyper-V, WSL, Docker, VPN d'entreprise), qui dupliqueraient le
# trafic reel.
_VIRTUELLES = ("loopback", "vethernet", "virtualbox", "vmware", "vboxnet",
               "docker", "br-", "veth", "lo", "wsl", "hyper-v", "npcap",
               "bluetooth", "tailscale", "zerotier", "isatap", "teredo")


def interface_virtuelle(nom: str) -> bool:
    nom = nom.lower()
    return nom == "lo" or any(nom.startswith(m) or m in nom
                              for m in _VIRTUELLES if m != "lo")


def _arrondi(valeur: float | None, chiffres: int = 1) -> float | None:
    return None if valeur is None else round(float(valeur), chiffres)


class MetricsCollector(PollingCollector):
    name = "metrics"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.metrics_interval_s
        self.first_delay_s = self.interval_s
        self._disque_precedent = None
        self._reseau_precedent = None
        self._instant_precedent: float | None = None
        # Premier appel : psutil mesure le CPU depuis l'appel precedent.
        psutil.cpu_percent(interval=None)
        self._amorcer()

    def _amorcer(self) -> None:
        self._disque_precedent = self._disque()
        self._reseau_precedent = self._reseau()
        self._instant_precedent = time.monotonic()

    @staticmethod
    def _disque():
        try:
            return psutil.disk_io_counters()
        except (RuntimeError, OSError):
            return None

    @staticmethod
    def _reseau() -> tuple[int, int] | None:
        try:
            compteurs = psutil.net_io_counters(pernic=True)
        except (RuntimeError, OSError):
            return None

        envoyes = recus = 0

        for nom, c in compteurs.items():
            if not interface_virtuelle(nom):
                envoyes += c.bytes_sent
                recus += c.bytes_recv

        return envoyes, recus

    @staticmethod
    def _delta(courant: int, precedent: int) -> int | None:
        # Un compteur qui recule (interface reinitialisee, veille) ne donne
        # pas un trafic negatif : il donne une mesure manquante.
        difference = courant - precedent
        return difference if difference >= 0 else None

    def sample(self) -> dict:
        maintenant = time.monotonic()
        ecoule = maintenant - (self._instant_precedent or maintenant)
        memoire = psutil.virtual_memory()

        try:
            swap = psutil.swap_memory().percent
        except (RuntimeError, OSError):
            swap = None

        try:
            racine = os.environ.get("SystemDrive", "C:") + "\\" \
                if os.name == "nt" else "/"
            disque_pct = psutil.disk_usage(racine).percent
        except OSError:
            disque_pct = None

        lecture = ecriture = None
        disque = self._disque()

        if disque is not None and self._disque_precedent is not None:
            lecture = self._delta(disque.read_bytes,
                                  self._disque_precedent.read_bytes)
            ecriture = self._delta(disque.write_bytes,
                                   self._disque_precedent.write_bytes)

        montee = descente = None
        reseau = self._reseau()

        if reseau is not None and self._reseau_precedent is not None:
            montee = self._delta(reseau[0], self._reseau_precedent[0])
            descente = self._delta(reseau[1], self._reseau_precedent[1])

        batterie_pct = secteur = None

        try:
            batterie = psutil.sensors_battery()
        except (RuntimeError, OSError, AttributeError):
            batterie = None

        if batterie is not None:
            batterie_pct = _arrondi(batterie.percent)
            secteur = batterie.power_plugged

        gpu_pct, vram_mb = self.ctx.hooks.gpu_sample()

        self._disque_precedent = disque
        self._reseau_precedent = reseau
        self._instant_precedent = maintenant

        return {
            "interval_s": round(ecoule, 1) if ecoule else self.interval_s,
            "cpu_pct": _arrondi(psutil.cpu_percent(interval=None)),
            "ram_pct": _arrondi(memoire.percent),
            "ram_used_mb": round(memoire.used / 2**20),
            "swap_pct": _arrondi(swap),
            "gpu_pct": _arrondi(gpu_pct),
            "vram_used_mb": _arrondi(vram_mb, 0),
            "cpu_temp_c": _arrondi(self.ctx.hooks.cpu_temperature()),
            "disk_used_pct": _arrondi(disque_pct, 2),
            "disk_read_bytes": lecture,
            "disk_write_bytes": ecriture,
            "net_up_bytes": montee,
            "net_down_bytes": descente,
            "battery_pct": batterie_pct,
            "on_ac": secteur,
        }

    def poll(self, now: datetime) -> None:
        if self._resynchroniser:
            # Premier sondage apres une veille : l'intervalle ecoule couvre
            # la veille et n'a rien de representatif. On repart de zero.
            self._resynchroniser = False
            psutil.cpu_percent(interval=None)
            self._amorcer()
            return

        self.ctx.factory.point("system_metrics", "psutil", now, self.sample())

    _resynchroniser = False

    def resync(self, _charge=None) -> None:
        """Appele (depuis un autre fil) au reveil de la machine."""
        self._resynchroniser = True
