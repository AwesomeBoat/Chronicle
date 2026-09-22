"""Utilisation GPU et VRAM, par les compteurs de performance (PDH).

Les memes compteurs que le Gestionnaire des taches, valables pour AMD,
Intel et NVIDIA (pilotes WDDM, Windows 10+) :

    \\GPU Engine(*)\\Utilization Percentage     une instance par processus
                                                et par moteur (3D, Copy...)
    \\GPU Adapter Memory(*)\\Dedicated Usage    octets, par carte

Comme le Gestionnaire des taches : pour chaque carte et chaque type de
moteur, on additionne les processus ; l'utilisation GPU est le maximum
de ces sommes.

PdhAddEnglishCounterW et pas PdhAddCounterW : les noms de compteurs sont
TRADUITS sur un Windows francais ("Processor Information" n'y existe
pas). La version "English" accepte les noms anglais partout.
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes as wt

from pc.tracker.platforms.windows import win32 as w

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_OK = 0

_MOTEUR = re.compile(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+).*engtype_(.+)$")


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


def _sig(fonction, *argtypes):
    # DWORD et non LONG : PDH_MORE_DATA (0x800007D2) lu en signe devient
    # negatif, et la comparaison echoue en silence.
    fonction.restype = wt.DWORD
    fonction.argtypes = list(argtypes)


_sig(w.pdh.PdhOpenQueryW, wt.LPCWSTR, ctypes.c_size_t,
     ctypes.POINTER(wt.HANDLE))
_sig(w.pdh.PdhAddEnglishCounterW, wt.HANDLE, wt.LPCWSTR, ctypes.c_size_t,
     ctypes.POINTER(wt.HANDLE))
_sig(w.pdh.PdhCollectQueryData, wt.HANDLE)
_sig(w.pdh.PdhGetFormattedCounterArrayW, wt.HANDLE, wt.DWORD,
     ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.c_void_p)
_sig(w.pdh.PdhCloseQuery, wt.HANDLE)


class GpuSampler:
    """Une requete PDH ouverte une fois, echantillonnee a chaque appel."""

    def __init__(self) -> None:
        self._requete = wt.HANDLE()
        self._moteurs = wt.HANDLE()
        self._memoire = wt.HANDLE()
        self.disponible = False

        if w.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._requete)) != PDH_OK:
            return

        ok_moteurs = w.pdh.PdhAddEnglishCounterW(
            self._requete, "\\GPU Engine(*)\\Utilization Percentage", 0,
            ctypes.byref(self._moteurs)) == PDH_OK
        ok_memoire = w.pdh.PdhAddEnglishCounterW(
            self._requete, "\\GPU Adapter Memory(*)\\Dedicated Usage", 0,
            ctypes.byref(self._memoire)) == PDH_OK

        self.disponible = ok_moteurs or ok_memoire
        self._ok_moteurs, self._ok_memoire = ok_moteurs, ok_memoire

        # Un compteur de taux a besoin de deux lectures : la premiere amorce.
        w.pdh.PdhCollectQueryData(self._requete)

    def _valeurs(self, compteur) -> list[tuple[str, float]]:
        taille, nombre = wt.DWORD(0), wt.DWORD(0)
        statut = w.pdh.PdhGetFormattedCounterArrayW(
            compteur, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(taille),
            ctypes.byref(nombre), None)

        if statut != PDH_MORE_DATA or not taille.value:
            return []

        tampon = ctypes.create_string_buffer(taille.value)
        statut = w.pdh.PdhGetFormattedCounterArrayW(
            compteur, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(taille),
            ctypes.byref(nombre), tampon)

        if statut != PDH_OK:
            return []

        elements = ctypes.cast(tampon, ctypes.POINTER(
            PDH_FMT_COUNTERVALUE_ITEM_W * nombre.value)).contents
        return [(e.szName or "", e.FmtValue.doubleValue) for e in elements
                if e.FmtValue.CStatus in (0, 1)]

    def sample(self) -> tuple[float | None, float | None]:
        if not self.disponible:
            return None, None

        if w.pdh.PdhCollectQueryData(self._requete) != PDH_OK:
            return None, None

        utilisation = None

        if self._ok_moteurs:
            sommes: dict[tuple[str, str], float] = {}

            for nom, valeur in self._valeurs(self._moteurs):
                trouve = _MOTEUR.search(nom)

                if trouve:
                    cle = (trouve.group(1), trouve.group(2).strip())
                    sommes[cle] = sommes.get(cle, 0.0) + valeur

            if sommes:
                utilisation = min(100.0, max(sommes.values()))

        vram = None

        if self._ok_memoire:
            valeurs = self._valeurs(self._memoire)

            if valeurs:
                vram = sum(v for _n, v in valeurs) / 2**20

        return utilisation, vram

    def close(self) -> None:
        if self._requete:
            w.pdh.PdhCloseQuery(self._requete)
            self._requete = wt.HANDLE()
            self.disponible = False
