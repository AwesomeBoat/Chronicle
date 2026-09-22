"""Le bureau Windows : fenetre active, inactivite, entrees, verrou, veille.

UN SEUL FIL, UNE FENETRE INVISIBLE
----------------------------------
Windows livre ce qui se passe sur le bureau sous forme de MESSAGES, a une
fenetre, dans la boucle de messages du fil qui l'a creee. Ce collecteur
cree donc une fenetre invisible et tout arrive au meme endroit :

    SetWinEventHook      la fenetre au premier plan change, son titre change
    WTS                  verrouillage / deverrouillage de la session
    WM_POWERBROADCAST    mise en veille / reveil
    WM_ENDSESSION        arret de la machine, fermeture de session
    Raw Input            chaque entree clavier/souris (COMPTEE, jamais lue)
    WM_TIMER (1 s)       titres a valider, sondage d'inactivite

Tout vit dans ce fil : les machines a etats (pc/tracker/core/spans.py) n'ont
besoin d'aucun verrou.

POURQUOI RAW INPUT ET PAS UN CROCHET CLAVIER
-------------------------------------------
Un crochet bas niveau (WH_KEYBOARD_LL) est appele AVANT que la touche
n'atteigne l'application : si Python tarde (ramasse-miettes, GIL occupe),
tout le clavier de la machine rame. Raw Input, lui, depose une COPIE du
message dans notre file : si on est lent, on est seul a l'etre. Et le
code de touche contenu dans le message n'est jamais decode.

POURQUOI PAS UN SERVICE WINDOWS
-------------------------------
Un service tourne dans la session 0, sans bureau : il ne verrait ni
fenetre, ni clavier, ni souris. Ce collecteur doit tourner dans la session
de l'utilisateur (tache planifiee a l'ouverture de session).
"""

from __future__ import annotations

import ctypes
import math
import struct
import threading
import time
from ctypes import wintypes as wt
from dataclasses import replace
from datetime import datetime, timedelta

from pc import schema
from pc.tracker.collectors.base import Collector
from pc.tracker.core.clock import from_epoch, utc_now
from pc.tracker.core.events import dedup_key
from pc.tracker.core.spans import (FocusTracker, IdleTracker, LockTracker,
                                   Window, apply_privacy)
from pc.tracker.platforms.windows import win32 as w
from pc.tracker.platforms.windows.procinfo import describe_window

WM_APP_STOP = w.WM_APP + 1
TIMER_TICK = 1

_EN_TETE = ctypes.sizeof(w.RAWINPUTHEADER)
_TAILLE_RAW = _EN_TETE + 24          # RAWMOUSE (24 octets) > RAWKEYBOARD (16)


class WindowsDesktop(Collector):
    name = "desktop"

    def __init__(self, ctx, tracker) -> None:
        super().__init__(ctx)
        cfg = ctx.config
        self.tracker = tracker
        self.regles = cfg.privacy

        self.focus = FocusTracker(self._emit_focus, cfg.focus_settle_s,
                                  cfg.focus_min_span_s) \
            if cfg.enabled("focus") else None
        self.idle = IdleTracker(self._emit_idle, cfg.idle_threshold_s) \
            if cfg.enabled("idle") else None
        self.lock = LockTracker(self._emit_lock) \
            if cfg.enabled("session") else None
        self.compter_entrees = cfg.enabled("input")
        self.session = cfg.enabled("session")

        self._fil: threading.Thread | None = None
        self._pret = threading.Event()
        self._hwnd = None
        self._thread_id = None
        self._raison_arret = "tracker_stop"
        self._ferme = False

        self._fg_hwnd = None
        self._fg_brut: Window | None = None
        self._hook_titre = None
        self._hooks: list = []
        self._suspendu_a: datetime | None = None
        self._tick = 0
        self._tampon = ctypes.create_string_buffer(64)
        self._absolu: tuple[int, int] | None = None

        # Les callbacks ctypes doivent survivre : si Python les ramasse,
        # Windows appelle une adresse liberee.
        self._wndproc = w.WNDPROC(self._window_proc)
        self._eventproc = w.WINEVENTPROC(self._win_event)

    # ======================================================== emission

    def _emit_focus(self, fenetre: Window, debut: datetime, fin: datetime,
                    raison: str) -> None:
        self.ctx.factory.interval("app_focus", "windows.foreground", debut,
                                  fin, {
                                      **fenetre.payload(),
                                      "end_reason": raison,
                                      "input_active_s":
                                          self.ctx.input_seconds(debut, fin)
                                          if self.compter_entrees else None,
                                  })

    def _emit_idle(self, debut: datetime, fin: datetime, raison: str) -> None:
        self.ctx.factory.interval("idle", "windows.idle", debut, fin, {
            "threshold_s": self.ctx.config.idle_threshold_s,
            "end_reason": raison})

    def _emit_lock(self, debut: datetime, fin: datetime, raison: str) -> None:
        self.ctx.factory.interval("session_locked", "windows.wts", debut, fin,
                                  {"end_reason": raison})

    def _vider_entrees(self, tout: bool = False) -> None:
        if not self.compter_entrees:
            return

        minutes = self.ctx.inputs.pop_all() if tout \
            else self.ctx.inputs.pop_complete(time.time())

        for debut, minute in minutes:
            # Instant exact et unique (une minute), donc historique : relire
            # la meme minute donnerait la meme cle.
            self.ctx.factory.point("input_activity", "windows.rawinput",
                                   from_epoch(debut), minute.payload(),
                                   historique=True)

    # ======================================================== cycle de vie

    def start(self) -> None:
        self._reprendre()
        self._fil = threading.Thread(target=self._boucle, name="desktop",
                                     daemon=True)
        self._fil.start()

        if not self._pret.wait(10):
            raise RuntimeError("la fenetre de messages n'a pas demarre")

    def stop(self, raison: str = "tracker_stop") -> None:
        self._raison_arret = raison

        if self._ferme or self._hwnd is None:
            return

        w.user32.PostMessageW(self._hwnd, WM_APP_STOP, 0, 0)

        if self._fil is not None and \
                threading.current_thread() is not self._fil:
            self._fil.join(5)

    def alive(self) -> bool:
        return self._fil is not None and self._fil.is_alive()

    def snapshot(self) -> dict | None:
        etat = {
            "focus": self.focus.snapshot() if self.focus else None,
            "idle_since": self.idle.idle_since.isoformat()
            if self.idle and self.idle.idle_since else None,
            "locked_since": self.lock.locked_since.isoformat()
            if self.lock and self.lock.locked_since else None,
        }
        return etat if any(etat.values()) else None

    def _deja_emis(self, type_: str, debut: datetime) -> bool:
        """L'intervalle a-t-il ete ferme normalement juste avant l'arret ?

        L'etat sauvegarde a 30 s de retard sur la realite : un intervalle
        ferme entre deux battements y figure encore comme ouvert. Le
        re-emettre avec une fin plus precoce ECRASERAIT la bonne version
        dans Chronicle (upsert sur la meme cle). On verifie donc la file.
        """
        cle = dedup_key(self.ctx.device_id, type_, schema.format_ts(debut))
        return self.ctx.outbox.has_dedup_key(cle)

    def _reprendre(self) -> None:
        """Ferme au dernier battement ce que l'arret brutal a laisse."""
        etat = self.recover()
        fin = self.ctx.previous_heartbeat

        if not etat or fin is None:
            return

        focus = etat.get("focus")

        if focus and self.focus:
            debut = datetime.fromisoformat(focus["start"])

            if fin > debut and not self._deja_emis("app_focus", debut):
                self.ctx.factory.interval(
                    "app_focus", "windows.foreground", debut, fin, {
                        **Window.from_state(focus["window"]).payload(),
                        "end_reason": "recovered", "input_active_s": None},
                    historique=True)

        if etat.get("idle_since") and self.idle:
            debut = datetime.fromisoformat(etat["idle_since"])

            if fin - debut >= self.idle.threshold and \
                    not self._deja_emis("idle", debut):
                self.ctx.factory.interval("idle", "windows.idle", debut, fin, {
                    "threshold_s": self.ctx.config.idle_threshold_s,
                    "end_reason": "recovered"}, historique=True)

        if etat.get("locked_since") and self.lock:
            debut = datetime.fromisoformat(etat["locked_since"])

            if fin > debut and not self._deja_emis("session_locked", debut):
                self.ctx.factory.interval("session_locked", "windows.wts",
                                          debut, fin,
                                          {"end_reason": "recovered"},
                                          historique=True)

    # ======================================================== le fil

    def _boucle(self) -> None:
        instance = w.kernel32.GetModuleHandleW(None)
        nom_classe = f"ChroniclePCTracker-{id(self)}"
        classe = w.WNDCLASSEXW()
        classe.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        classe.lpfnWndProc = self._wndproc
        classe.hInstance = instance
        classe.lpszClassName = nom_classe

        if not w.user32.RegisterClassExW(ctypes.byref(classe)):
            self.log.error("RegisterClassExW : %d", ctypes.get_last_error())
            self._pret.set()
            return

        # Fenetre de premier niveau (PAS HWND_MESSAGE) et jamais affichee :
        # les messages de veille et de fin de session ne sont diffuses qu'aux
        # fenetres de premier niveau.
        self._hwnd = w.user32.CreateWindowExW(
            0, nom_classe, "Chronicle PC tracker", 0, 0, 0, 0, 0,
            None, None, instance, None)
        self._thread_id = w.kernel32.GetCurrentThreadId()

        try:
            self._installer()
        finally:
            self._pret.set()

        message = w.MSG()

        while w.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(message))
            w.user32.DispatchMessageW(ctypes.byref(message))

        self._desinstaller()
        w.user32.DestroyWindow(self._hwnd)
        w.user32.UnregisterClassW(nom_classe, instance)

    def _installer(self) -> None:
        if self.session:
            if not w.wtsapi32.WTSRegisterSessionNotification(
                    self._hwnd, w.NOTIFY_FOR_THIS_SESSION):
                self.log.warning("verrouillage non suivi (WTS : %d)",
                                 ctypes.get_last_error())

        if self.compter_entrees or self.idle:
            peripheriques = (w.RAWINPUTDEVICE * 2)(
                w.RAWINPUTDEVICE(0x01, 0x02, w.RIDEV_INPUTSINK, self._hwnd),
                w.RAWINPUTDEVICE(0x01, 0x06, w.RIDEV_INPUTSINK, self._hwnd))

            if not w.user32.RegisterRawInputDevices(
                    peripheriques, 2, ctypes.sizeof(w.RAWINPUTDEVICE)):
                self.log.warning("Raw Input indisponible (%d) : pas de "
                                 "comptage des entrees",
                                 ctypes.get_last_error())
                self.compter_entrees = False

        if self.focus:
            crochet = w.user32.SetWinEventHook(
                w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_FOREGROUND, None,
                self._eventproc, 0, 0,
                w.WINEVENT_OUTOFCONTEXT | w.WINEVENT_SKIPOWNPROCESS)
            if crochet:
                self._hooks.append(crochet)

            # Restaurer une fenetre minimisee ne declenche pas toujours
            # EVENT_SYSTEM_FOREGROUND.
            crochet = w.user32.SetWinEventHook(
                w.EVENT_SYSTEM_MINIMIZEEND, w.EVENT_SYSTEM_MINIMIZEEND, None,
                self._eventproc, 0, 0,
                w.WINEVENT_OUTOFCONTEXT | w.WINEVENT_SKIPOWNPROCESS)
            if crochet:
                self._hooks.append(crochet)

            self._premier_plan(w.user32.GetForegroundWindow())

        w.user32.SetTimer(self._hwnd, TIMER_TICK, 1000, None)

    def _desinstaller(self) -> None:
        w.user32.KillTimer(self._hwnd, TIMER_TICK)

        for crochet in self._hooks + ([self._hook_titre]
                                      if self._hook_titre else []):
            w.user32.UnhookWinEvent(crochet)

        self._hooks.clear()
        self._hook_titre = None

        if self.session:
            w.wtsapi32.WTSUnRegisterSessionNotification(self._hwnd)

    # ======================================================== fenetre active

    def _fenetre_courante(self) -> Window | None:
        hwnd = w.user32.GetForegroundWindow()
        brut = describe_window(hwnd) if hwnd else None
        self._fg_hwnd, self._fg_brut = hwnd, brut
        return apply_privacy(brut, self.regles)

    def _premier_plan(self, hwnd) -> None:
        maintenant = utc_now()
        brut = describe_window(hwnd) if hwnd else None
        fenetre = apply_privacy(brut, self.regles)

        pid_avant = self._fg_brut.pid if self._fg_brut else None
        self._fg_hwnd, self._fg_brut = hwnd, brut

        if self.focus:
            self.focus.foreground(maintenant, fenetre)

        if self.ctx.browser is not None and not (self.lock and
                                                 self.lock.locked_since):
            self.ctx.browser.foreground(maintenant,
                                        fenetre.app if fenetre else None)

        pid = brut.pid if brut else None

        if pid != pid_avant:
            self._suivre_titres(pid)

    def _suivre_titres(self, pid: int | None) -> None:
        """Changements de titre : crochet limite au processus au premier plan.

        EVENT_OBJECT_NAMECHANGE a l'echelle du systeme se declenche pour
        chaque libelle qui bouge dans chaque application (barres de
        progression, horloges...). Limite au seul processus actif, il ne
        coute presque rien.
        """
        if self._hook_titre:
            w.user32.UnhookWinEvent(self._hook_titre)
            self._hook_titre = None

        if pid:
            self._hook_titre = w.user32.SetWinEventHook(
                w.EVENT_OBJECT_NAMECHANGE, w.EVENT_OBJECT_NAMECHANGE, None,
                self._eventproc, pid, 0, w.WINEVENT_OUTOFCONTEXT) or None

    def _titre_change(self, hwnd) -> None:
        if not self.focus or self._fg_brut is None or hwnd != self._fg_hwnd:
            return

        brut = replace(self._fg_brut, title=w.window_text(hwnd) or None)
        self._fg_brut = brut
        fenetre = apply_privacy(brut, self.regles)

        if fenetre is not None:
            self.focus.title(utc_now(), fenetre.title)

    def _win_event(self, _hook, evenement, hwnd, id_objet, id_enfant,
                   _thread, _temps) -> None:
        try:
            if evenement in (w.EVENT_SYSTEM_FOREGROUND,
                             w.EVENT_SYSTEM_MINIMIZEEND):
                if hwnd and hwnd != self._fg_hwnd:
                    self._premier_plan(hwnd)
            elif evenement == w.EVENT_OBJECT_NAMECHANGE \
                    and id_objet == w.OBJID_WINDOW \
                    and id_enfant == w.CHILDID_SELF:
                self._titre_change(hwnd)
        except Exception:                                # noqa: BLE001
            # Une exception qui remonte dans un callback ctypes est perdue
            # en silence : on la journalise nous-memes.
            self.log.exception("evenement fenetre")

    # ======================================================== entrees

    def _entree(self, lparam) -> None:
        taille = wt.UINT(64)
        lus = w.user32.GetRawInputData(lparam, w.RID_INPUT, self._tampon,
                                       ctypes.byref(taille), _EN_TETE)

        if lus == 0xFFFFFFFF or lus < _EN_TETE:
            return

        brut = self._tampon.raw
        genre = struct.unpack_from("<I", brut, 0)[0]
        t = time.time()

        if genre == w.RIM_TYPEKEYBOARD:
            # Seul le champ Flags est lu (appui ou relachement). MakeCode et
            # VKey, qui diraient QUELLE touche, ne sont jamais decodes.
            drapeaux = struct.unpack_from("<H", brut, _EN_TETE + 2)[0]

            if not drapeaux & w.RI_KEY_BREAK:
                self.ctx.inputs.key(t)
        elif genre == w.RIM_TYPEMOUSE:
            (flags, boutons, donnees, _bruts, x, y) = struct.unpack_from(
                "<H2xHhIii", brut, _EN_TETE)

            if boutons & w.RI_MOUSE_BUTTONS_DOWN:
                self.ctx.inputs.click(t)

            if boutons & (w.RI_MOUSE_WHEEL | w.RI_MOUSE_HWHEEL):
                self.ctx.inputs.scroll(t, abs(donnees) / 120.0)

            if flags & w.MOUSE_MOVE_ABSOLUTE:
                # Tablette, bureau a distance : position absolue (0-65535).
                if self._absolu is not None:
                    dx, dy = x - self._absolu[0], y - self._absolu[1]
                    self.ctx.inputs.move(t, math.hypot(dx, dy) / 65535 * 1920)
                self._absolu = (x, y)
            elif x or y:
                self.ctx.inputs.move(t, math.hypot(x, y))
        else:
            return

        if self.idle is not None and self.idle.idle_since is not None:
            self.idle.input_now(utc_now())

    def _sonder_inactivite(self) -> None:
        info = w.LASTINPUTINFO(ctypes.sizeof(w.LASTINPUTINFO), 0)

        if not w.user32.GetLastInputInfo(ctypes.byref(info)):
            return

        ecoule_ms = (w.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        maintenant = utc_now()
        derniere = maintenant - timedelta(milliseconds=ecoule_ms)

        # Au reveil, le compteur de Windows peut dater la derniere entree
        # d'avant la veille : la veille n'est pas de l'inactivite.
        if self._suspendu_a is None and self._reveil_a is not None:
            derniere = max(derniere, self._reveil_a)

        self.idle.update(maintenant, derniere)

    _reveil_a: datetime | None = None

    # ======================================================== session

    def _verrou(self) -> None:
        maintenant = utc_now()
        self.lock.lock(maintenant)

        if self.focus:
            self.focus.pause(maintenant, "lock")

        if self.ctx.browser is not None:
            self.ctx.browser.pause(maintenant, "lock")

    def _reactiver(self, maintenant: datetime) -> None:
        """Retour devant l'ecran : on repart de la fenetre REELLEMENT active."""
        fenetre = self._fenetre_courante()

        if self.focus:
            self.focus.resume(maintenant, fenetre)

        if self.ctx.browser is not None:
            self.ctx.browser.resume(maintenant)
            self.ctx.browser.foreground(maintenant,
                                        fenetre.app if fenetre else None)

        self._suivre_titres(self._fg_brut.pid if self._fg_brut else None)

    def _deverrou(self) -> None:
        maintenant = utc_now()
        self.lock.unlock(maintenant)

        if self._suspendu_a is None:        # sinon, le reveil s'en chargera
            self._reactiver(maintenant)

    def _veille(self) -> None:
        maintenant = utc_now()
        self._suspendu_a = maintenant

        if self.focus:
            self.focus.pause(maintenant, "suspend")

        if self.ctx.browser is not None:
            self.ctx.browser.pause(maintenant, "suspend")

        if self.idle:
            self.idle.close(maintenant, "suspend")

        self._vider_entrees(tout=True)
        # La machine peut ne jamais se reveiller (batterie a plat) : la
        # reprise fermera ce qui reste ouvert a CET instant, pas 30 s avant.
        self.ctx.outbox.set_state("suspend_at", schema.format_ts(maintenant))
        self.ctx.outbox.set_state("heartbeat", schema.format_ts(maintenant))
        self.ctx.bus.publish("suspend", maintenant)

    def _reveil(self) -> None:
        if self._suspendu_a is None:
            return                          # deux messages de reveil

        maintenant = utc_now()
        debut, self._suspendu_a = self._suspendu_a, None
        self._reveil_a = maintenant
        self.ctx.outbox.delete_state("suspend_at")

        # Sans le journal systeme (collecteur desactive), la veille est
        # datee par les messages recus ici.
        if not self.ctx.config.enabled("system_events"):
            self.ctx.factory.interval("system_sleep", "windows.power", debut,
                                      maintenant, {"state": "unknown",
                                                   "origin": "live"})

        verrouille = self.lock is not None and self.lock.locked_since

        if not verrouille:
            self._reactiver(maintenant)

        self.ctx.bus.publish("resume", maintenant)

    def _fermer_tout(self, raison: str) -> None:
        if self._ferme:
            return

        maintenant = utc_now()

        if self.focus:
            self.focus.close(maintenant, raison)

        if self.ctx.browser is not None:
            self.ctx.browser.close(maintenant, raison)

        if self.idle:
            self.idle.close(maintenant, raison)

        if self.lock:
            self.lock.close(maintenant, raison)

        self._vider_entrees(tout=True)
        self._ferme = True

    # ======================================================== messages

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == w.WM_INPUT:
                self._entree(lparam)
                # Obligatoire : Windows fait le menage du message ici.
                return w.user32.DefWindowProcW(hwnd, message, wparam, lparam)

            if message == w.WM_TIMER and wparam == TIMER_TICK:
                self._tic()
                return 0

            if message == w.WM_WTSSESSION_CHANGE and self.lock:
                if wparam == w.WTS_SESSION_LOCK:
                    self._verrou()
                elif wparam == w.WTS_SESSION_UNLOCK:
                    self._deverrou()
                return 0

            if message == w.WM_POWERBROADCAST:
                if wparam == w.PBT_APMSUSPEND:
                    self._veille()
                elif wparam in (w.PBT_APMRESUMEAUTOMATIC,
                                w.PBT_APMRESUMESUSPEND):
                    self._reveil()
                return 1

            if message == w.WM_QUERYENDSESSION:
                return 1

            if message == w.WM_ENDSESSION:
                if wparam:
                    raison = "logoff" if lparam & w.ENDSESSION_LOGOFF \
                        else "shutdown"
                    self._fin_de_session(raison)
                return 0

            if message == WM_APP_STOP:
                self._fermer_tout(self._raison_arret)
                w.user32.PostQuitMessage(0)
                return 0

            if message == w.WM_CLOSE:
                return 0                    # jamais fermee de l'exterieur

        except Exception:                                # noqa: BLE001
            self.log.exception("message %#x", message)

        return w.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _fin_de_session(self, raison: str) -> None:
        """Windows s'arrete : fermer, ecrire, envoyer, en quelques secondes.

        Apres le retour de ce message, Windows peut tuer le processus a tout
        moment. On ferme donc tout ICI, puis on attend (4 s au plus) que le
        fil principal ait ecrit la file et tente un dernier envoi.
        """
        self.log.info("fin de session Windows (%s)", raison)
        self._fermer_tout(raison)
        self.tracker.request_stop(raison)
        fin = time.monotonic() + 4

        while time.monotonic() < fin and not self.tracker.stopped.is_set():
            time.sleep(0.05)

    def _tic(self) -> None:
        self._tick += 1
        maintenant = utc_now()

        if self.focus and not self.focus.paused:
            self.focus.tick(maintenant)

            # Filet de securite toutes les 5 s : un evenement de fenetre
            # perdu ne doit pas fausser l'intervalle.
            if self._tick % 5 == 0:
                hwnd = w.user32.GetForegroundWindow()

                if hwnd != self._fg_hwnd:
                    self._premier_plan(hwnd)
                elif self._fg_brut is not None and hwnd and \
                        w.window_text(hwnd) != (self._fg_brut.title or ""):
                    self._titre_change(hwnd)

        if self.idle and self._suspendu_a is None and self._tick % 2 == 0:
            self._sonder_inactivite()

        self._vider_entrees()
