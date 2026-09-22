"""Ce que Windows fournit aux collecteurs communs (voir PlatformHooks)."""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import winreg
from ctypes import wintypes as wt

from pc.tracker.collectors.base import PlatformHooks
from pc.tracker.platforms.windows import win32 as w
from pc.tracker.platforms.windows.gpu import GpuSampler

CREATE_NO_WINDOW = 0x08000000

# Fuseaux Windows -> IANA, pour les plus courants (table CLDR windowsZones,
# territoire par defaut). Un fuseau absent donne None : `tz` est une
# metadonnee, ts_local porte deja le decalage exact.
FUSEAUX = {
    "Romance Standard Time": "Europe/Paris",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "Etc/UTC",
}


def _registre(chemin: str, valeur: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, chemin) as cle:
            return str(winreg.QueryValueEx(cle, valeur)[0]).strip()
    except OSError:
        return None


# ------------------------------------------------ GetAdaptersAddresses

class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    """Le debut de IP_ADAPTER_ADDRESSES_LH : seuls les champs lus.

    La structure complete est longue ; Windows l'alloue en entier, on n'en
    lit que le prefixe, dont la disposition est stable depuis Windows XP.
    """


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wt.ULONG), ("IfIndex", wt.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.c_void_p),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", wt.LPWSTR), ("Description", wt.LPWSTR),
    ("FriendlyName", wt.LPWSTR),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wt.ULONG), ("Flags", wt.ULONG),
    ("Mtu", wt.ULONG), ("IfType", wt.DWORD), ("OperStatus", ctypes.c_int),
]

w.iphlpapi.GetAdaptersAddresses.restype = wt.ULONG
w.iphlpapi.GetAdaptersAddresses.argtypes = [
    wt.ULONG, wt.ULONG, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(wt.ULONG)]

IF_TYPES = {6: "ethernet", 71: "wifi", 243: "mobile", 244: "mobile",
            53: "vpn", 131: "vpn"}      # 53 = proprietaire virtuel, 131 = tunnel


def interface_kinds() -> dict[str, str]:
    taille = wt.ULONG(16 * 1024)

    for _ in range(3):
        tampon = ctypes.create_string_buffer(taille.value)
        statut = w.iphlpapi.GetAdaptersAddresses(0, 0x0E, None, tampon,
                                                 ctypes.byref(taille))

        if statut == 0:
            break

        if statut != 111:               # ERROR_BUFFER_OVERFLOW
            return {}
    else:
        return {}

    types: dict[str, str] = {}
    courant = ctypes.cast(tampon, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))

    while courant:
        adaptateur = courant.contents

        if adaptateur.FriendlyName:
            types[adaptateur.FriendlyName] = IF_TYPES.get(adaptateur.IfType,
                                                          "other")

        courant = adaptateur.Next

    return types


class WindowsHooks(PlatformHooks):
    def __init__(self) -> None:
        self._gpu: GpuSampler | None = None

    def device_details(self) -> dict:
        cle = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        build = _registre(cle, "CurrentBuild") or str(
            sys.getwindowsversion().build)
        nom = _registre(cle, "ProductName") or "Windows"

        # Windows 11 declare encore "Windows 10" dans ProductName : seul le
        # numero de build fait foi (22000 et au-dela).
        if build.isdigit() and int(build) >= 22000:
            nom = nom.replace("Windows 10", "Windows 11")

        version = _registre(cle, "DisplayVersion")
        cpu = _registre(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                        "ProcessorNameString")

        return {"os_version": f"{nom} {version}".strip() if version else nom,
                "os_build": build, "cpu_model": cpu, "gpus": self._gpus()}

    @staticmethod
    def _gpus() -> list[str]:
        noms: list[str] = []
        indice = 0
        peripherique = w.DISPLAY_DEVICEW()
        peripherique.cb = ctypes.sizeof(w.DISPLAY_DEVICEW)

        while w.user32.EnumDisplayDevicesW(None, indice,
                                           ctypes.byref(peripherique), 0):
            nom = peripherique.DeviceString.strip()

            if nom and nom not in noms and "Basic" not in nom:
                noms.append(nom)

            indice += 1

        return noms

    def gpu_sample(self) -> tuple[float | None, float | None]:
        if self._gpu is None:
            self._gpu = GpuSampler()

        return self._gpu.sample()

    def interface_kinds(self) -> dict[str, str]:
        try:
            return interface_kinds()
        except OSError:
            return {}

    def wifi_ssid(self) -> str | None:
        """Le SSID courant, via netsh (qui peut exiger la localisation)."""
        try:
            sortie = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"], capture_output=True,
                timeout=10, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None

        texte = sortie.stdout.decode("cp850", "replace")
        trouve = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", texte, re.M)
        return trouve.group(1) if trouve else None

    def timezone(self) -> str | None:
        nom = _registre(r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
                        "TimeZoneKeyName")
        return FUSEAUX.get(nom or "")

    def close(self) -> None:
        if self._gpu is not None:
            self._gpu.close()
