"""network_change : la connectivite change (Wi-Fi, cable, VPN, coupure).

Sondage toutes les 30 s de psutil (quelques microsecondes). Un evenement
n'est emis que si l'etat CHANGE ; l'etat precedent est garde sur disque,
donc un redemarrage du tracker ne recree pas un changement fictif.

Le nom du reseau Wi-Fi (SSID) dit ou l'on est. Il est hache avec le sel
de la machine : deux sessions sur le meme reseau ont la meme empreinte,
mais l'empreinte ne redonne pas le nom.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime

import psutil

from pc.tracker.collectors.base import PollingCollector
from pc.tracker.collectors.metrics import interface_virtuelle
from pc.tracker.core.privacy import hash_identifier

CLE_ETAT = "network:last"


def type_interface(nom: str) -> str:
    """Devine le type d'apres le nom, quand le systeme ne le dit pas."""
    n = nom.lower()

    if any(m in n for m in ("vpn", "wireguard", "openvpn", "tun", "tap",
                            "nordlynx", "proton", "mullvad")) \
            or n.startswith("wg"):
        return "vpn"

    if any(m in n for m in ("wi-fi", "wifi", "wlan", "wireless", "sans fil")) \
            or n.startswith("wl"):
        return "wifi"

    if any(m in n for m in ("ethernet", "local area", "reseau local",
                            "réseau local")) or n.startswith(("eth", "en")):
        return "ethernet"

    if n.startswith(("wwan", "ww", "mobile", "cellular")):
        return "mobile"

    return "other"


def _adresse_utile(adresse) -> bool:
    if adresse.family != socket.AF_INET:
        return False

    try:
        ip = ipaddress.ip_address(adresse.address)
    except ValueError:
        return False

    return not (ip.is_loopback or ip.is_link_local)


class NetworkCollector(PollingCollector):
    name = "network"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.network_interval_s

    def etat(self) -> dict:
        stats = psutil.net_if_stats()
        adresses = psutil.net_if_addrs()
        types = self.ctx.hooks.interface_kinds()
        actives = []

        for nom, stat in sorted(stats.items()):
            if not stat.isup or interface_virtuelle(nom):
                continue

            if not any(_adresse_utile(a) for a in adresses.get(nom, [])):
                continue

            actives.append({"name": nom,
                            "type": types.get(nom) or type_interface(nom)})

        ssid_hash = None

        if any(i["type"] == "wifi" for i in actives):
            ssid = self.ctx.hooks.wifi_ssid()

            if ssid:
                ssid_hash = hash_identifier(ssid, self.ctx.salt)

        return {"connected": bool(actives), "interfaces": actives,
                "wifi_ssid_hash": ssid_hash}

    def poll(self, now: datetime) -> None:
        etat = self.etat()

        if etat == self.ctx.outbox.get_state(CLE_ETAT):
            return

        self.ctx.factory.point("network_change", "psutil", now, etat)
        self.ctx.outbox.set_state(CLE_ETAT, etat)
