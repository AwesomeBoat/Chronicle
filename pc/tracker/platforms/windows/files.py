"""Surveillance de fichiers Windows : ReadDirectoryChangesW.

Un fil par dossier racine, bloque dans l'appel systeme : il ne consomme
rien tant que rien ne bouge. Windows remplit un tampon de 64 Ko de
notifications FILE_NOTIFY_INFORMATION ; un renommage arrive en deux
morceaux (ancien nom, nouveau nom), recolles ici en un seul evenement.

Si le tampon deborde (des milliers de changements d'un coup), Windows
renvoie 0 octet : les notifications perdues sont signalees
(collector_status), jamais inventees.

Pour arreter : CancelIoEx depuis un autre fil debloque l'appel.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes as wt

from pc.tracker.collectors.files import FileWatcherBase
from pc.tracker.platforms.windows import win32 as w

FILE_LIST_DIRECTORY = 0x0001
PARTAGE = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILTRE = 0x1 | 0x2 | 0x10       # nom de fichier, nom de dossier, ecriture

ACTIONS = {1: "created", 2: "deleted", 3: "modified", 4: "renamed_old",
           5: "renamed_new"}

TAILLE_TAMPON = 64 * 1024


def decode_notifications(donnees: bytes) -> list[tuple[str, str]]:
    """Tampon FILE_NOTIFY_INFORMATION -> [(action, chemin relatif)]."""
    sortie = []
    position = 0

    while position + 12 <= len(donnees):
        suivant, action, longueur = struct.unpack_from("<III", donnees,
                                                       position)
        nom = donnees[position + 12:position + 12 + longueur].decode(
            "utf-16-le", "replace")
        sortie.append((ACTIONS.get(action, "modified"), nom))

        if not suivant:
            break

        position += suivant

    return sortie


class WindowsFileWatcher(FileWatcherBase):
    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._handles: dict[str, int] = {}
        self._fils: list[threading.Thread] = []
        self._arret = threading.Event()

    def start(self) -> None:
        for racine in self.racines:
            fil = threading.Thread(target=self._surveiller, args=(racine,),
                                   name=f"files:{os.path.basename(racine)}",
                                   daemon=True)
            fil.start()
            self._fils.append(fil)

        self.log.info("%d dossier(s) surveille(s) : %s", len(self.racines),
                      ", ".join(self.racines))

    def stop(self, raison: str = "tracker_stop") -> None:
        self._arret.set()

        for handle in list(self._handles.values()):
            w.kernel32.CancelIoEx(handle, None)

        for fil in self._fils:
            fil.join(2)

    def alive(self) -> bool:
        return all(f.is_alive() for f in self._fils) or self._arret.is_set()

    def _surveiller(self, racine: str) -> None:
        handle = w.kernel32.CreateFileW(racine, FILE_LIST_DIRECTORY, PARTAGE,
                                        None, OPEN_EXISTING,
                                        FILE_FLAG_BACKUP_SEMANTICS, None)

        if not handle or handle == w.INVALID_HANDLE_VALUE:
            self.log.error("impossible de surveiller %s (%d)", racine,
                           ctypes.get_last_error())
            return

        self._handles[racine] = handle
        tampon = ctypes.create_string_buffer(TAILLE_TAMPON)
        lus = wt.DWORD()

        try:
            while not self._arret.is_set():
                ok = w.kernel32.ReadDirectoryChangesW(
                    handle, tampon, TAILLE_TAMPON, True, FILTRE,
                    ctypes.byref(lus), None, None)

                if not ok:
                    erreur = ctypes.get_last_error()

                    if erreur != w.ERROR_OPERATION_ABORTED:
                        self.log.error("surveillance de %s interrompue (%d)",
                                       racine, erreur)
                    break

                if lus.value == 0:
                    self.ctx.status(self.name, "rate_limited",
                                    f"tampon deborde sous {racine} : "
                                    "changements perdus")
                    continue

                self._traiter(racine, tampon.raw[:lus.value])
        finally:
            w.kernel32.CloseHandle(handle)
            self._handles.pop(racine, None)

    def _traiter(self, racine: str, donnees: bytes) -> None:
        ancien: str | None = None

        for action, relatif in decode_notifications(donnees):
            chemin = os.path.join(racine, relatif)

            if action == "renamed_old":
                ancien = chemin
                continue

            try:
                if action == "renamed_new":
                    self.notify("renamed", ancien or chemin, racine,
                                dest=chemin)
                    ancien = None
                else:
                    self.notify(action, chemin, racine)
            except Exception:                            # noqa: BLE001
                self.log.exception("notification de fichier")
