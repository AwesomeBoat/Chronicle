"""Le recepteur local de l'extension navigateur : securite et nettoyage."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from pc.tracker.collectors.base import Bus, Context, PlatformHooks
from pc.tracker.collectors.browser import BrowserCollector
from pc.tracker.config import Config
from pc.tracker.core.events import EventFactory
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.spans import BrowserPages


def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def recepteur(tmp_path):
    pages: list = []
    cfg = Config(device_id="windows-main", data_dir=tmp_path,
                 config_dir=tmp_path, browser_port=port_libre(),
                 browser_token="jeton-test")
    boite = Outbox(tmp_path / "t.db")
    ctx = Context(cfg, EventFactory("windows-main", lambda e: None), boite,
                  threading.Event(), Bus(), "sel", "windows", PlatformHooks())
    ctx.browser = BrowserPages(lambda *a: pages.append(a))
    collecteur = BrowserCollector(ctx)
    collecteur.start()
    yield collecteur, cfg.browser_port, pages, ctx
    collecteur.stop()
    boite.close()


def appel(port, chemin, corps=None, jeton="jeton-test", origine=None):
    entetes = {"Content-Type": "application/json"}

    if jeton:
        entetes["X-Chronicle-Token"] = jeton
    if origine:
        entetes["Origin"] = origine

    donnees = json.dumps(corps).encode() if corps is not None else None
    requete = urllib.request.Request(f"http://127.0.0.1:{port}{chemin}",
                                     data=donnees, headers=entetes,
                                     method="POST" if corps is not None
                                     else "GET")
    try:
        with urllib.request.urlopen(requete, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as erreur:
        return erreur.code, None


ONGLET = {"browser": "chrome", "tab_id": 7, "incognito": False,
          "url": "https://github.com/AwesomeBoat/Chronicle?token=abc#x",
          "title": "(3) Chronicle"}


def test_ping_donne_la_machine(recepteur):
    _, port, _, _ = recepteur
    assert appel(port, "/browser/ping") == (200, {"ok": True,
                                                  "device_id": "windows-main"})


def test_jeton_obligatoire(recepteur):
    _, port, _, _ = recepteur
    assert appel(port, "/browser/tab", ONGLET, jeton="faux")[0] == 403
    assert appel(port, "/browser/tab", ONGLET, jeton=None)[0] == 403


def test_une_page_web_ne_peut_pas_injecter(recepteur):
    """Une page web qui viserait 127.0.0.1 porte un Origin http(s)."""
    _, port, _, _ = recepteur
    assert appel(port, "/browser/tab", ONGLET,
                 origine="https://site-malveillant.com")[0] == 403
    assert appel(port, "/browser/tab", ONGLET,
                 origine="chrome-extension://abcdef")[0] == 200


def test_url_nettoyee_avant_tout(recepteur):
    collecteur, port, pages, ctx = recepteur
    from pc.tracker.core.clock import utc_now

    ctx.browser.foreground(utc_now(), "chrome")
    assert appel(port, "/browser/tab", ONGLET)[0] == 200
    courante = ctx.browser.current
    assert courante.domain == "github.com"
    assert courante.url is None                  # mode "domain" par defaut
    assert courante.title == "Chronicle"         # compteur retire
    assert collecteur.recus == 1


def test_navigateur_inconnu_ignore(recepteur):
    collecteur, port, _, _ = recepteur
    appel(port, "/browser/tab", {**ONGLET, "browser": "netscape"})
    assert collecteur.recus == 0
