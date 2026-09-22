"""Machines a etats communes : fenetre active, inactivite, verrou, pages,
entrees. Le temps est injecte : aucun test n'attend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pc.tracker.core.inputs import InputAggregator
from pc.tracker.core.privacy import PrivacyRules
from pc.tracker.core.spans import (BrowserPages, FocusTracker, IdleTracker,
                                   LockTracker, Window, apply_privacy,
                                   make_page)

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def t(secondes: float) -> datetime:
    return T0 + timedelta(seconds=secondes)


VSCODE = Window("vscode", "Visual Studio Code", "Code.exe", pid=10,
                title="main.py - Chronicle")
CHROME = Window("chrome", "Google Chrome", "chrome.exe", pid=20,
                title="GitHub - Google Chrome")


@pytest.fixture
def focus():
    emis = []
    tracker = FocusTracker(lambda w, a, b, r: emis.append((w.app, w.title, a,
                                                           b, r)),
                           settle_s=2, min_span_s=1)
    return tracker, emis


# ======================================================== fenetre active

def test_changement_d_application(focus):
    f, emis = focus
    f.foreground(t(0), VSCODE)
    f.foreground(t(60), CHROME)
    f.close(t(90), "tracker_stop")
    assert emis == [("vscode", "main.py - Chronicle", t(0), t(60), "switch"),
                    ("chrome", "GitHub - Google Chrome", t(60), t(90),
                     "tracker_stop")]


def test_titre_valide_seulement_s_il_tient(focus):
    f, emis = focus
    f.foreground(t(0), VSCODE)
    f.title(t(10), "a.py")
    f.title(t(10.5), "b.py")          # clignote : pas encore valide
    f.tick(t(11))
    assert emis == []
    f.tick(t(13))                     # b.py a tenu 2 s
    # Le nouvel intervalle commence au PREMIER changement (10 s).
    assert emis == [("vscode", "main.py - Chronicle", t(0), t(10),
                     "title_change")]
    assert f.current.title == "b.py" and f.start == t(10)


def test_titre_qui_revient_annule_l_attente(focus):
    f, emis = focus
    f.foreground(t(0), VSCODE)
    f.title(t(5), "Enregistrement...")
    f.title(t(5.5), VSCODE.title)
    f.tick(t(20))
    assert emis == []


def test_alt_tab_trop_court_ignore(focus):
    f, emis = focus
    f.foreground(t(0), VSCODE)
    f.foreground(t(30), CHROME)
    f.foreground(t(30.4), VSCODE)     # 0,4 s dans Chrome
    f.close(t(40), "tracker_stop")
    assert [e[0] for e in emis] == ["vscode", "vscode"]


def test_verrou_ferme_et_reprend(focus):
    f, emis = focus
    f.foreground(t(0), VSCODE)
    f.pause(t(100), "lock")
    f.foreground(t(150), CHROME)      # ignore : session verrouillee
    f.resume(t(200), CHROME)
    f.close(t(260), "tracker_stop")
    assert emis == [("vscode", "main.py - Chronicle", t(0), t(100), "lock"),
                    ("chrome", "GitHub - Google Chrome", t(200), t(260),
                     "tracker_stop")]


def test_snapshot_pour_reprise(focus):
    f, _ = focus
    f.foreground(t(0), VSCODE)
    etat = f.snapshot()
    assert Window.from_state(etat["window"]) == VSCODE
    assert etat["start"] == t(0).isoformat()


def test_vie_privee_sur_fenetre():
    regles = PrivacyRules()
    coffre = Window("keepassxc", None, "KeePassXC.exe", pid=3, title="Banque")
    assert apply_privacy(coffre, regles) == Window("private", private=True)

    privee = Window("edge", "Edge", "msedge.exe", pid=4,
                    title="Accueil - [InPrivate] - Microsoft Edge")
    masquee = apply_privacy(privee, regles)
    assert masquee.title is None and masquee.private

    assert apply_privacy(coffre, PrivacyRules(mask_mode="drop")) is None
    assert apply_privacy(Window("vscode", title="(2) x.py"),
                         regles).title == "x.py"


# ============================================================ inactivite

def test_inactivite_commence_a_la_derniere_entree():
    emis = []
    idle = IdleTracker(lambda a, b, r: emis.append((a, b, r)), threshold_s=120)
    idle.update(t(100), last_input=t(0))      # 100 s : pas encore
    assert idle.idle_since is None
    idle.update(t(130), last_input=t(0))      # 130 s : inactif depuis 0
    assert idle.idle_since == t(0)
    idle.input_now(t(500))
    assert emis == [(t(0), t(500), "input")]


def test_inactivite_fin_par_sondage():
    emis = []
    idle = IdleTracker(lambda a, b, r: emis.append((a, b, r)), threshold_s=60)
    idle.update(t(70), last_input=t(0))
    idle.update(t(300), last_input=t(290))
    assert emis == [(t(0), t(290), "input")]


def test_inactivite_fermee_a_la_veille():
    emis = []
    idle = IdleTracker(lambda a, b, r: emis.append(r), threshold_s=60)
    idle.update(t(70), last_input=t(0))
    idle.close(t(80), "suspend")
    assert emis == ["suspend"] and idle.idle_since is None


def test_verrou():
    emis = []
    verrou = LockTracker(lambda a, b, r: emis.append((a, b, r)))
    verrou.lock(t(0))
    verrou.lock(t(5))                          # deja verrouille
    verrou.unlock(t(600))
    verrou.unlock(t(700))                      # deja deverrouille
    assert emis == [(t(0), t(600), "unlock")]


# ======================================================= pages navigateur

def page(url, titre, tab=1, incognito=False, regles=None):
    return make_page("chrome", url, titre, incognito, tab,
                     regles or PrivacyRules())


def test_pages_comptees_seulement_navigateur_au_premier_plan():
    emis = []
    pages = BrowserPages(lambda p, a, b, r: emis.append((p.domain, a, b, r)))
    pages.tab(t(0), page("https://github.com/x", "GitHub"), "chrome")
    assert emis == [] and pages.current is None      # Chrome pas devant
    pages.foreground(t(10), "chrome")
    pages.tab(t(40), page("https://youtube.com/watch?v=1", "Video"), "chrome")
    pages.foreground(t(100), "vscode")
    assert emis == [("github.com", t(10), t(40), "navigate"),
                    ("youtube.com", t(40), t(100), "blur")]


def test_page_identique_ne_coupe_pas():
    emis = []
    pages = BrowserPages(lambda p, a, b, r: emis.append(r))
    pages.foreground(t(0), "chrome")
    pages.tab(t(0), page("https://a.be/", "A"), "chrome")
    pages.tab(t(5), page("https://a.be/", "A"), "chrome")
    assert emis == []


def test_changement_d_onglet():
    emis = []
    pages = BrowserPages(lambda p, a, b, r: emis.append(r))
    pages.foreground(t(0), "chrome")
    pages.tab(t(0), page("https://a.be/", "A", tab=1), "chrome")
    pages.tab(t(30), page("https://b.be/", "B", tab=2), "chrome")
    assert emis == ["tab_switch"]


def test_page_privee_et_domaine_exclu():
    prive = page("https://secret.be", "Secret", incognito=True)
    assert (prive.domain, prive.url, prive.title, prive.private) == \
        (None, None, None, True)
    banque = page("https://www.belfius.be/particuliers", "Belfius")
    assert banque.private and banque.domain is None and banque.title is None


# =============================================================== entrees

def test_minutes_et_secondes_actives():
    entrees = InputAggregator()
    base = T0.timestamp()
    entrees.key(base + 1)
    entrees.key(base + 1.5)
    entrees.click(base + 2)
    entrees.scroll(base + 3, 2)
    entrees.move(base + 3, 150.0)
    entrees.key(base + 61)                     # minute suivante

    finies = entrees.pop_complete(base + 60)
    assert len(finies) == 1
    debut, minute = finies[0]
    assert debut == base
    assert minute.payload() == {"interval_s": 60, "keys": 2, "clicks": 1,
                                "scroll": 2, "mouse_distance": 150.0,
                                "active_s": 3}
    assert entrees.active_seconds(base, base + 70) == 4
    assert [m for m, _ in entrees.pop_all()] == [base + 60]


def test_secondes_actives_sur_un_intervalle():
    entrees = InputAggregator()
    base = T0.timestamp()

    for seconde in (0, 1, 2, 10, 50):
        entrees.key(base + seconde)

    assert entrees.active_seconds(base, base + 3) == 3
    assert entrees.active_seconds(base + 5, base + 11) == 1
    assert entrees.active_seconds(base + 60, base + 120) == 0


def test_seconde_a_cheval_comptee_une_seule_fois():
    """Deux fenetres voisines ne se partagent jamais une seconde active."""
    entrees = InputAggregator()
    base = T0.timestamp()

    for instant in (44.2, 44.9, 45.5, 46.1):
        entrees.key(base + instant)

    a = entrees.active_seconds(base + 43.0, base + 44.5)
    b = entrees.active_seconds(base + 44.5, base + 47.0)
    assert (a, b) == (1, 2)          # 44 dans A ; 45 et 46 dans B
    assert a + b == 3                # 3 secondes distinctes au total


def test_aucune_touche_n_est_memorisee():
    """PAS DE KEYLOGGER : l'API ne recoit meme pas la touche."""
    import inspect
    assert list(inspect.signature(InputAggregator.key).parameters) == \
        ["self", "t"]
