"""Machines a etats des intervalles : fenetre active, inactivite, verrou,
page de navigateur.

Elles ne connaissent AUCUN systeme. Le collecteur Windows les nourrit avec
SetWinEventHook et GetLastInputInfo, le collecteur Linux les nourrira avec
l'IPC de Hyprland et logind : la logique qui decide QUAND un intervalle
commence et finit est ecrite une fois, donc identique sur les deux machines.
C'est ce qui garantit que `app_focus` veut dire la meme chose partout.

Toutes les methodes recoivent l'instant `now` en argument au lieu de lire
l'horloge : c'est ce qui les rend testables sans attendre.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from pc.apps import is_browser
from pc.tracker.core.privacy import (PrivacyRules, is_private_window,
                                     normalize_title, sanitize_url)


# ===================================================== fenetre active

@dataclass(frozen=True, slots=True)
class Window:
    """Ce qu'on sait de la fenetre au premier plan, deja passe au filtre
    de la vie privee."""

    app: str
    app_name: str | None = None
    process: str | None = None
    exe_path: str | None = None
    pid: int | None = None
    title: str | None = None
    window_class: str | None = None
    private: bool = False

    def same_window(self, autre: "Window") -> bool:
        """Meme application, meme processus. Le titre peut differer."""
        return (self.app == autre.app and self.pid == autre.pid
                and self.process == autre.process)

    def payload(self) -> dict:
        donnees = {"app": self.app, "app_name": self.app_name,
                   "process": self.process, "exe_path": self.exe_path,
                   "pid": self.pid, "window_title": self.title,
                   "window_class": self.window_class}

        if self.private:
            donnees["private"] = True

        return donnees

    def to_state(self) -> dict:
        return asdict(self)

    @classmethod
    def from_state(cls, donnees: dict) -> "Window":
        return cls(**donnees)


PRIVATE_APP = "private"


def apply_privacy(window: Window | None, rules: PrivacyRules) -> Window | None:
    """Applique les regles a une fenetre brute. None = ne rien emettre."""
    if window is None:
        return None

    if rules.app_excluded(window.app, window.process):
        if rules.mask_mode == "drop":
            return None

        return Window(app=PRIVATE_APP, private=True)

    titre = normalize_title(window.title)

    if is_private_window(titre) or rules.title_redacted(window.app,
                                                         window.process):
        return replace(window, title=None, private=True)

    return replace(window, title=titre)


EmitFocus = Callable[[Window, datetime, datetime, str], None]


class FocusTracker:
    """Decoupe le temps en intervalles de premier plan.

    Regles :
      - changer d'application ferme l'intervalle ("switch") ;
      - changer de titre dans la meme application aussi ("title_change"),
        mais seulement quand le nouveau titre a TENU `settle_s` secondes :
        un titre qui clignote (chargement, compteur) ne coupe rien. Le
        nouvel intervalle commence au PREMIER changement, pas une fois
        le delai ecoule : aucune seconde n'est perdue ;
      - verrou, veille, fin de session ferment l'intervalle ; le suivant
        s'ouvre au retour ;
      - un intervalle de moins de `min_span_s` n'est pas emis : ce sont
        les fenetres traversees pendant un Alt+Tab.
    """

    def __init__(self, emit: EmitFocus, settle_s: float = 2.0,
                 min_span_s: float = 1.0) -> None:
        self._emit = emit
        self.settle = timedelta(seconds=settle_s)
        self.min_span = timedelta(seconds=min_span_s)
        self.current: Window | None = None
        self.start: datetime | None = None
        self._attente: list | None = None      # [titre, premier, dernier]
        self.paused: str | None = None

    # ------------------------------------------------------------ entrees

    def foreground(self, now: datetime, window: Window | None) -> None:
        """La fenetre au premier plan a (peut-etre) change."""
        if self.paused:
            return

        if window is None:
            self._fermer(now, "switch")
            return

        if self.current is None:
            self._ouvrir(window, now)
        elif self.current.same_window(window):
            self.title(now, window.title)
        else:
            self._fermer(now, "switch")
            self._ouvrir(window, now)

    def title(self, now: datetime, titre: str | None) -> None:
        """Le titre de la fenetre active a change."""
        if self.current is None or self.paused:
            return

        if titre == self.current.title:
            self._attente = None
        elif self._attente is None:
            self._attente = [titre, now, now]
        else:
            self._attente[0] = titre
            self._attente[2] = now

    def tick(self, now: datetime) -> None:
        """Appele regulierement : valide un titre qui a tenu."""
        if self._attente is None or self.current is None:
            return

        titre, premier, dernier = self._attente

        if now - dernier >= self.settle:
            fenetre = replace(self.current, title=titre)
            self._fermer(premier, "title_change")
            self._ouvrir(fenetre, premier)

    def pause(self, now: datetime, raison: str) -> None:
        """Verrou, veille, fin de session : plus personne ne regarde."""
        self._fermer(now, raison)
        self.paused = raison

    def resume(self, now: datetime, window: Window | None) -> None:
        self.paused = None
        self.foreground(now, window)

    def close(self, now: datetime, raison: str) -> None:
        self._fermer(now, raison)

    # ------------------------------------------------------------ interne

    def _ouvrir(self, window: Window, now: datetime) -> None:
        self.current = window
        self.start = now
        self._attente = None

    def _fermer(self, fin: datetime, raison: str) -> None:
        if self.current is None or self.start is None:
            return

        if fin - self.start >= self.min_span:
            self._emit(self.current, self.start, fin, raison)

        self.current = None
        self.start = None
        self._attente = None

    # ------------------------------------------------------ reprise

    def snapshot(self) -> dict | None:
        """L'intervalle ouvert, pour le reprendre apres un arret brutal."""
        if self.current is None or self.start is None:
            return None

        return {"window": self.current.to_state(),
                "start": self.start.isoformat()}


# ======================================================== inactivite

EmitSpan = Callable[[datetime, datetime, str], None]


class IdleTracker:
    """Inactivite : aucune entree pendant au moins `threshold_s`.

    L'intervalle commence a la DERNIERE entree, pas au franchissement du
    seuil : sinon chaque pause serait amputee de deux minutes. Il finit a
    la premiere entree qui suit (exacte si le collecteur la signale via
    `input_now`, sinon a la precision du sondage).
    """

    def __init__(self, emit: EmitSpan, threshold_s: float = 120.0) -> None:
        self._emit = emit
        self.threshold = timedelta(seconds=threshold_s)
        self.idle_since: datetime | None = None

    def update(self, now: datetime, last_input: datetime) -> None:
        """Sondage : quand a eu lieu la derniere entree ?"""
        if self.idle_since is None:
            if now - last_input >= self.threshold:
                self.idle_since = last_input
        elif last_input > self.idle_since + timedelta(milliseconds=1):
            self._fermer(last_input, "input")

    def input_now(self, now: datetime) -> None:
        """Une entree vient d'arriver (signalee en temps reel)."""
        if self.idle_since is not None:
            self._fermer(now, "input")

    def close(self, now: datetime, raison: str) -> None:
        if self.idle_since is not None:
            self._fermer(now, raison)

    def _fermer(self, fin: datetime, raison: str) -> None:
        debut = self.idle_since
        self.idle_since = None

        if debut is not None and fin - debut >= self.threshold:
            self._emit(debut, fin, raison)


class LockTracker:
    """Session verrouillee, du verrou au deverrouillage."""

    def __init__(self, emit: EmitSpan) -> None:
        self._emit = emit
        self.locked_since: datetime | None = None

    def lock(self, now: datetime) -> None:
        if self.locked_since is None:
            self.locked_since = now

    def unlock(self, now: datetime) -> None:
        self.close(now, "unlock")

    def close(self, now: datetime, raison: str) -> None:
        debut = self.locked_since
        self.locked_since = None

        if debut is not None:
            self._emit(debut, now, raison)


# ================================================== pages de navigateur

@dataclass(frozen=True, slots=True)
class Page:
    """L'onglet actif d'un navigateur, deja nettoye."""

    browser: str
    domain: str | None
    url: str | None
    title: str | None
    tab_id: int | None = None
    private: bool = False

    def same_page(self, autre: "Page") -> bool:
        return (self.browser, self.domain, self.url, self.title) == \
            (autre.browser, autre.domain, autre.url, autre.title)

    def payload(self) -> dict:
        donnees = {"browser": self.browser, "domain": self.domain,
                   "url": self.url, "page_title": self.title}

        if self.private:
            donnees["private"] = True

        return donnees


EmitPage = Callable[[Page, datetime, datetime, str], None]


def make_page(browser: str, url: str | None, title: str | None,
              incognito: bool, tab_id: int | None,
              rules: PrivacyRules) -> Page | None:
    """Rapport brut de l'extension -> Page nettoyee (None = ne rien emettre)."""
    if incognito:
        return Page(browser, None, None, None, tab_id, private=True)

    domaine, propre = sanitize_url(url, rules.url_mode)

    if rules.domain_excluded(domaine):
        if rules.mask_mode == "drop":
            return None
        return Page(browser, None, None, None, tab_id, private=True)

    return Page(browser, domaine, propre, normalize_title(title), tab_id)


class BrowserPages:
    """Temps par page : l'onglet actif, compte seulement quand son
    navigateur est au premier plan.

    Deux fils l'alimentent : celui des fenetres (premier plan) et celui du
    recepteur HTTP (l'extension). D'ou le verrou.
    """

    def __init__(self, emit: EmitPage, min_span_s: float = 1.0) -> None:
        self._emit = emit
        # Les redirections enchainent des pages de quelques millisecondes :
        # meme seuil que pour les fenetres.
        self.min_span = timedelta(seconds=min_span_s)
        self._verrou = threading.Lock()
        self._onglets: dict[str, Page | None] = {}
        self._premier_plan: str | None = None
        self.current: Page | None = None
        self.start: datetime | None = None
        self.paused = False

    def foreground(self, now: datetime, app_id: str | None) -> None:
        navigateur = app_id if is_browser(app_id) else None

        with self._verrou:
            if navigateur == self._premier_plan:
                return

            self._fermer(now, "blur")
            self._premier_plan = navigateur
            self._ouvrir_si_possible(now)

    def tab(self, now: datetime, page: Page | None, browser: str) -> None:
        """L'extension signale l'onglet actif de `browser`."""
        with self._verrou:
            ancien = self._onglets.get(browser)
            self._onglets[browser] = page

            if browser != self._premier_plan or self.paused:
                return

            if self.current is not None and page is not None \
                    and self.current.same_page(page):
                return

            meme_onglet = (ancien is not None and page is not None
                           and ancien.tab_id == page.tab_id)
            self._fermer(now, "navigate" if meme_onglet else "tab_switch")
            self._ouvrir_si_possible(now)

    def pause(self, now: datetime, raison: str) -> None:
        with self._verrou:
            self._fermer(now, raison)
            self.paused = True

    def resume(self, now: datetime) -> None:
        with self._verrou:
            self.paused = False
            self._ouvrir_si_possible(now)

    def close(self, now: datetime, raison: str) -> None:
        with self._verrou:
            self._fermer(now, raison)

    def snapshot(self) -> dict | None:
        with self._verrou:
            if self.current is None or self.start is None:
                return None
            return {"page": asdict(self.current),
                    "start": self.start.isoformat()}

    def _ouvrir_si_possible(self, now: datetime) -> None:
        if self.paused or self._premier_plan is None:
            return

        page = self._onglets.get(self._premier_plan)

        if page is not None:
            self.current = page
            self.start = now

    def _fermer(self, fin: datetime, raison: str) -> None:
        if self.current is not None and self.start is not None \
                and fin - self.start >= self.min_span:
            self._emit(self.current, self.start, fin, raison)

        self.current = None
        self.start = None
