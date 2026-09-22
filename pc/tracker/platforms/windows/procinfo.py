"""Qui est derriere une fenetre : processus, executable, nom lisible.

Les lectures couteuses sont mises en cache :

    (pid, date de creation) -> chemin de l'executable
        la date de creation distingue un processus d'un autre qui aurait
        recupere le meme pid apres sa mort ;
    chemin de l'executable  -> nom declare (FileDescription)
        lire les ressources d'un .exe prend quelques millisecondes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass
from functools import lru_cache

from pc.apps import canonical_app
from pc.tracker.core.privacy import normalize_path
from pc.tracker.core.spans import Window
from pc.tracker.platforms.windows import win32 as w

# L'hote des applications du Store : la vraie application est une fenetre
# enfant appartenant a un autre processus.
HOTE_UWP = "applicationframehost.exe"


@dataclass(frozen=True, slots=True)
class Processus:
    pid: int
    exe_path: str | None
    creation: int | None          # FILETIME (100 ns)

    @property
    def nom(self) -> str | None:
        if not self.exe_path:
            return None
        return self.exe_path.replace("\\", "/").rsplit("/", 1)[-1]


def process_times(handle) -> tuple[int, int] | None:
    """(creation, sortie) en FILETIME. Sortie = 0 si toujours vivant."""
    creation, sortie, noyau, utilisateur = (w.FILETIME(), w.FILETIME(),
                                            w.FILETIME(), w.FILETIME())

    if not w.kernel32.GetProcessTimes(handle, ctypes.byref(creation),
                                      ctypes.byref(sortie),
                                      ctypes.byref(noyau),
                                      ctypes.byref(utilisateur)):
        return None

    return creation.value, sortie.value


def image_path(handle) -> str | None:
    taille = wt.DWORD(1024)
    tampon = ctypes.create_unicode_buffer(taille.value)

    if w.kernel32.QueryFullProcessImageNameW(handle, 0, tampon,
                                             ctypes.byref(taille)):
        return tampon.value

    return None


def open_process(pid: int, acces: int = w.PROCESS_QUERY_LIMITED_INFORMATION):
    return w.kernel32.OpenProcess(acces, False, pid) or None


def process(pid: int) -> Processus:
    """Chemin et date de creation d'un processus (vides si refuse)."""
    handle = open_process(pid)

    if not handle:
        return Processus(pid, None, None)

    try:
        temps = process_times(handle)
        return Processus(pid, image_path(handle), temps[0] if temps else None)
    finally:
        w.kernel32.CloseHandle(handle)


@lru_cache(maxsize=512)
def file_description(chemin: str) -> str | None:
    """Le nom que l'executable se donne ("Visual Studio Code")."""
    taille = w.version.GetFileVersionInfoSizeW(chemin, None)

    if not taille:
        return None

    tampon = ctypes.create_string_buffer(taille)

    if not w.version.GetFileVersionInfoW(chemin, 0, taille, tampon):
        return None

    traductions = wt.LPVOID()
    longueur = wt.UINT()

    if not w.version.VerQueryValueW(tampon, "\\VarFileInfo\\Translation",
                                    ctypes.byref(traductions),
                                    ctypes.byref(longueur)) \
            or longueur.value < 4:
        codes = [(0x0409, 0x04B0)]
    else:
        tableau = ctypes.cast(traductions,
                              ctypes.POINTER(wt.WORD * (longueur.value // 2)))
        mots = list(tableau.contents)
        codes = [(mots[i], mots[i + 1]) for i in range(0, len(mots) - 1, 2)]

    for langue, page in codes:
        cle = f"\\StringFileInfo\\{langue:04x}{page:04x}\\FileDescription"
        valeur = wt.LPVOID()

        if w.version.VerQueryValueW(tampon, cle, ctypes.byref(valeur),
                                    ctypes.byref(longueur)) and longueur.value:
            texte = ctypes.wstring_at(valeur, longueur.value).rstrip("\x00")

            if texte.strip():
                return texte.strip()[:80]

    return None


def _enfant_uwp(hwnd, pid_hote: int) -> int | None:
    """Le processus de l'application hebergee par ApplicationFrameHost."""
    trouve: list[int] = []

    @w.WNDENUMPROC
    def visiter(enfant, _param):
        pid = w.window_pid(enfant)

        if pid and pid != pid_hote:
            trouve.append(pid)
            return False

        return True

    w.user32.EnumChildWindows(hwnd, visiter, 0)
    return trouve[0] if trouve else None


def describe_window(hwnd) -> Window | None:
    """Une fenetre -> Window brute (la vie privee s'applique apres)."""
    if not hwnd:
        return None

    pid = w.window_pid(hwnd)

    if not pid:
        return None

    titre = w.window_text(hwnd)
    classe = w.class_name(hwnd)
    proc = process(pid)

    if proc.nom and proc.nom.lower() == HOTE_UWP:
        reel = _enfant_uwp(hwnd, pid)

        if reel:
            proc = process(reel)

    description = file_description(proc.exe_path) if proc.exe_path else None
    app, nom = canonical_app(proc.nom, classe, description)

    return Window(app=app, app_name=nom or description,
                  process=proc.nom,
                  exe_path=normalize_path(proc.exe_path),
                  pid=proc.pid, title=titre or None, window_class=classe)
