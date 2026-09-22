"""file_change : la partie commune (filtres, anti-rebond, plafond).

La SURVEILLANCE est propre au systeme (ReadDirectoryChangesW sous
Windows, inotify sous Linux). Ce qu'on fait des notifications, non :

    1. ignorer le bruit : .git, node_modules, __pycache__, fichiers
       temporaires d'editeurs, telechargements partiels...
    2. appliquer les chemins exclus de la vie privee ;
    3. anti-rebond : un editeur qui sauvegarde ecrit souvent 2-3 fois le
       meme fichier en 100 ms. Une action par chemin toutes les 2 s ;
    4. plafond par minute : un `npm install` hors des dossiers ignores
       produirait des milliers de lignes. Au-dela, on COMPTE ce qui est
       ecarte et on le dit (collector_status rate_limited) plutot que de
       se taire.

Jamais le contenu d'un fichier : le chemin, l'extension, l'action.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from fnmatch import fnmatch

from pc.tracker.collectors.base import Collector
from pc.tracker.core.clock import utc_now
from pc.tracker.core.privacy import extension, normalize_path

ANTI_REBOND_S = 2.0


class FileEventFilter:
    """Decide si une notification devient un evenement."""

    def __init__(self, ignores: list[str], regles, max_par_minute: int) -> None:
        self.ignores = [m.lower() for m in ignores]
        self.regles = regles
        self.max_par_minute = max_par_minute
        self._recents: dict[tuple[str, str], float] = {}
        self._minute = -1
        self._compte = 0
        self.ecartes = 0
        self._verrou = threading.Lock()

    def ignore(self, chemin_normalise: str) -> bool:
        for partie in chemin_normalise.lower().split("/"):
            # "~" (le dossier personnel) et "c:" (un lecteur) ne sont pas des
            # noms de fichiers : sans cette exception, le motif "*~" (copies
            # de sauvegarde des editeurs) ignorerait TOUT ce qui est sous ~.
            if partie == "~" or partie.endswith(":"):
                continue

            if any(fnmatch(partie, m) for m in self.ignores):
                return True

        return self.regles.path_excluded(chemin_normalise)

    def accept(self, action: str, chemin: str, instant: float) -> bool:
        """True si l'evenement doit partir. Met a jour les compteurs."""
        with self._verrou:
            cle = (action, chemin)
            precedent = self._recents.get(cle)

            if precedent is not None and instant - precedent < ANTI_REBOND_S:
                return False

            self._recents[cle] = instant

            if len(self._recents) > 5000:
                limite = instant - ANTI_REBOND_S
                self._recents = {k: v for k, v in self._recents.items()
                                 if v >= limite}

            minute = int(instant // 60)

            if minute != self._minute:
                self._minute = minute
                self._compte = 0

            self._compte += 1

            if self._compte > self.max_par_minute:
                self.ecartes += 1
                return False

            return True

    def pop_dropped(self) -> int:
        with self._verrou:
            n, self.ecartes = self.ecartes, 0
            return n


class FileWatcherBase(Collector):
    """Base des surveillants de fichiers : la plateforme appelle notify()."""

    name = "files"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        cfg = ctx.config
        self.filtre = FileEventFilter(cfg.files_ignore, cfg.privacy,
                                      cfg.files_max_per_minute)
        self.racines = [os.path.abspath(os.path.expanduser(r))
                        for r in cfg.files_roots
                        if os.path.isdir(os.path.expanduser(r))]
        self._dernier_signal = time.monotonic()

    def notify(self, action: str, chemin: str, racine: str,
               dest: str | None = None, is_dir: bool | None = None,
               moment: datetime | None = None) -> None:
        moment = moment or utc_now()
        chemin_n = normalize_path(chemin)
        dest_n = normalize_path(dest) if dest else None

        if self.filtre.ignore(chemin_n) and \
                (dest_n is None or self.filtre.ignore(dest_n)):
            return

        if is_dir is None and action != "deleted":
            try:
                is_dir = os.path.isdir(dest or chemin)
            except OSError:
                is_dir = None

        # Un dossier "modifie" veut dire qu'un de ses enfants a change :
        # l'enfant a deja son propre evenement.
        if action == "modified" and is_dir:
            return

        if not self.filtre.accept(action, chemin_n, moment.timestamp()):
            self._signaler_plafond()
            return

        self.ctx.factory.point("file_change", f"{self.ctx.platform}.files",
                               moment, {
                                   "action": action,
                                   "path": chemin_n,
                                   "dest_path": dest_n,
                                   "is_dir": is_dir,
                                   "extension": None if is_dir
                                   else extension(dest or chemin),
                                   "root": normalize_path(racine),
                               })

    def _signaler_plafond(self) -> None:
        # Au plus un signalement par minute.
        if time.monotonic() - self._dernier_signal < 60:
            return

        ecartes = self.filtre.pop_dropped()

        if ecartes:
            self._dernier_signal = time.monotonic()
            self.ctx.status(self.name, "rate_limited",
                            f"{ecartes} evenement(s) de fichiers ecartes "
                            f"(plafond {self.filtre.max_par_minute}/min)")
