"""app_launch / app_process : lancement et fermeture des applications.

Une "application" est ici un processus qui possede une fenetre visible de
premier niveau : Chrome, VS Code, Discord. Les centaines de processus
systeme sans fenetre ne sont pas des applications au sens de l'activite.

LA DECOUVERTE EST SONDEE, LES INSTANTS SONT EXACTS
--------------------------------------------------
Toutes les 10 s, la liste des fenetres est parcourue (EnumWindows : une
milliseconde). Un processus nouveau n'est pas date "a la decouverte" :
Windows garde son instant de creation exact, c'est lui qui fait `ts`.

Pour la fermeture, un handle est garde ouvert sur chaque application
suivie. Quand elle se termine, le handle reste valide et GetProcessTimes
donne l'instant de sortie exact, meme decouvert 10 s plus tard.

Un redemarrage du tracker retrouve les memes processus avec les memes
instants de creation : memes cles, aucun doublon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pc.apps import canonical_app
from pc.tracker.collectors.base import PollingCollector
from pc.tracker.core.clock import from_epoch
from pc.tracker.core.privacy import normalize_path
from pc.tracker.platforms.windows import win32 as w
from pc.tracker.platforms.windows.procinfo import (HOTE_UWP, _enfant_uwp,
                                                   file_description,
                                                   image_path, open_process,
                                                   process_times)

CLE_LANCES = "processes:launched"
MAX_SUIVIS = 300


@dataclass(slots=True)
class Suivi:
    pid: int
    handle: int
    creation: int
    exe: str | None
    app: str
    app_name: str | None


def pids_avec_fenetre() -> set[int]:
    """Les processus qui ont au moins une fenetre visible de premier niveau."""
    pids: set[int] = set()

    @w.WNDENUMPROC
    def visiter(hwnd, _param):
        if not w.user32.IsWindowVisible(hwnd) or \
                w.user32.GetWindow(hwnd, w.GW_OWNER):
            return True

        if w.GetWindowLongPtr(hwnd, w.GWL_EXSTYLE) & w.WS_EX_TOOLWINDOW:
            return True

        if not w.user32.GetWindowTextLengthW(hwnd):
            return True

        pid = w.window_pid(hwnd)

        if pid:
            nom = image_path_rapide(pid)

            if nom and nom.lower().endswith(HOTE_UWP):
                pid = _enfant_uwp(hwnd, pid) or pid

            pids.add(pid)

        return True

    w.user32.EnumWindows(visiter, 0)
    return pids


def image_path_rapide(pid: int) -> str | None:
    handle = open_process(pid)

    if not handle:
        return None

    try:
        return image_path(handle)
    finally:
        w.kernel32.CloseHandle(handle)


class ProcessCollector(PollingCollector):
    name = "processes"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.interval_s = ctx.config.processes_interval_s
        self.suivis: dict[int, Suivi] = {}
        # (pid, creation) deja annonces, pour ne pas re-emettre app_launch a
        # chaque redemarrage du tracker. Garde 3 jours.
        self._lances: dict[str, float] = ctx.outbox.get_state(CLE_LANCES, {})

    def _payload(self, suivi: Suivi) -> dict:
        nom = suivi.exe.replace("\\", "/").rsplit("/", 1)[-1] \
            if suivi.exe else None
        return {"app": suivi.app, "app_name": suivi.app_name, "process": nom,
                "exe_path": normalize_path(suivi.exe), "pid": suivi.pid}

    def _nouveau(self, pid: int) -> None:
        handle = open_process(pid, w.PROCESS_QUERY_LIMITED_INFORMATION
                              | w.SYNCHRONIZE)

        if not handle:
            return                          # processus protege

        temps = process_times(handle)
        exe = image_path(handle)

        if temps is None:
            w.kernel32.CloseHandle(handle)
            return

        nom = exe.replace("\\", "/").rsplit("/", 1)[-1] if exe else None
        app, app_name = canonical_app(nom, None, file_description(exe)
                                      if exe else None)
        regles = self.ctx.config.privacy

        if regles.app_excluded(app, nom):
            if regles.mask_mode == "drop":
                w.kernel32.CloseHandle(handle)
                return
            app, app_name, exe = "private", None, None

        suivi = Suivi(pid, handle, temps[0], exe, app, app_name)
        self.suivis[pid] = suivi

        cle = f"{pid}:{temps[0]}"

        if cle not in self._lances:
            creation = from_epoch(w.filetime_to_epoch(temps[0]))
            self.ctx.factory.point("app_launch", "windows.processes", creation,
                                   self._payload(suivi), naturelle=cle,
                                   historique=True)
            self._lances[cle] = creation.timestamp()

    def poll(self, now: datetime) -> None:
        vivants = pids_avec_fenetre()

        for pid in vivants - set(self.suivis):
            if len(self.suivis) < MAX_SUIVIS:
                self._nouveau(pid)

        for pid, suivi in list(self.suivis.items()):
            if w.kernel32.WaitForSingleObject(suivi.handle, 0) != \
                    w.WAIT_OBJECT_0:
                continue

            temps = process_times(suivi.handle)
            w.kernel32.CloseHandle(suivi.handle)
            del self.suivis[pid]

            if temps is None or not temps[1]:
                continue

            debut = from_epoch(w.filetime_to_epoch(suivi.creation))
            fin = from_epoch(w.filetime_to_epoch(temps[1]))
            self.ctx.factory.interval("app_process", "windows.processes",
                                      debut, fin, self._payload(suivi),
                                      naturelle=f"{pid}:{suivi.creation}",
                                      historique=True)

        limite = (now - timedelta(days=3)).timestamp()
        garde = {k: v for k, v in self._lances.items() if v >= limite}

        if garde != self.ctx.outbox.get_state(CLE_LANCES, {}):
            self.ctx.outbox.set_state(CLE_LANCES, garde)

        self._lances = garde

    def stop(self, raison: str = "tracker_stop") -> None:
        for suivi in self.suivis.values():
            w.kernel32.CloseHandle(suivi.handle)

        self.suivis.clear()
