"""De bout en bout : Windows -> collecteur -> evenement normalise -> file
locale -> HTTP -> API Chronicle -> tables existantes de Chronicle.

Rien n'est simule sur le chemin :

    - les collecteurs lisent la vraie machine (fenetre au premier plan,
      psutil, PDH, journal systeme, un vrai depot git) ;
    - le tracker passe par sa vraie file SQLite et son vrai client HTTP ;
    - l'API tourne dans un vrai serveur uvicorn, sur un port local ;
    - elle ecrit dans une vraie base PostgreSQL (de TEST, cf. conftest).

Seules les entrees clavier/souris sont injectees dans l'agregateur (un
test ne tape pas au clavier) - par la meme methode que le gestionnaire
Raw Input.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows uniquement"),
    pytest.mark.skipif(
        not all(os.environ.get(f"CHRONICLE_TEST_POSTGRES_{k}")
                for k in ("HOST", "PORT", "DB", "USER", "PASSWORD")),
        reason="base de test non designee (CHRONICLE_TEST_POSTGRES_*)"),
]


def port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def chronicle():
    """L'API Chronicle dans un vrai serveur HTTP, base de test videe."""
    import uvicorn
    from sqlalchemy import text

    from database.connection import create_tables, engine

    create_tables()

    with engine.begin() as connexion:
        connexion.execute(text(
            "TRUNCATE observation, episode, profile_snapshot, raw_payload, "
            "sync_state, metric, source RESTART IDENTITY CASCADE"))

    from api.app import app

    port = port_libre()
    serveur = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                            log_config=None))
    fil = threading.Thread(target=serveur.run, daemon=True)
    fil.start()

    for _ in range(100):
        if serveur.started:
            break
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    serveur.should_exit = True
    fil.join(5)


def sql(requete: str):
    from sqlalchemy import text

    from database.connection import engine

    with engine.connect() as connexion:
        return connexion.execute(text(requete)).all()


def test_windows_vers_chronicle(chronicle, tmp_path):
    from pc.tracker.collectors.base import Bus, Context
    from pc.tracker.collectors.device import DeviceCollector
    from pc.tracker.collectors.git import GitCollector
    from pc.tracker.collectors.metrics import MetricsCollector
    from pc.tracker.config import Config
    from pc.tracker.core.clock import from_epoch, utc_now
    from pc.tracker.core.events import EventFactory
    from pc.tracker.core.inputs import InputAggregator
    from pc.tracker.core.outbox import Outbox
    from pc.tracker.core.privacy import PrivacyRules
    from pc.tracker.core.spans import FocusTracker, apply_privacy
    from pc.tracker.core.sync import ChronicleClient, Syncer
    from pc.tracker.platforms.windows import win32 as w
    from pc.tracker.platforms.windows.eventlog import query, to_events
    from pc.tracker.platforms.windows.hooks import WindowsHooks
    from pc.tracker.platforms.windows.procinfo import describe_window

    # --------------------------------------------- le tracker, en vrai
    boite = Outbox(tmp_path / "tracker.db")
    factory = EventFactory("windows-main", lambda e: boite.enqueue([e]),
                           "Europe/Brussels")
    cfg = Config(device_id="windows-main", data_dir=tmp_path,
                 config_dir=tmp_path, git_roots=[str(tmp_path / "depots")],
                 privacy=PrivacyRules(exclude_paths=[]))
    hooks = WindowsHooks()
    ctx = Context(cfg, factory, boite, threading.Event(), Bus(), "sel",
                  "windows", hooks, InputAggregator())

    # 1. Fenetre au premier plan (vraie) -> intervalle app_focus.
    maintenant = utc_now()
    fenetre = apply_privacy(describe_window(w.user32.GetForegroundWindow()),
                            cfg.privacy)
    focus = FocusTracker(lambda f, a, b, r: factory.interval(
        "app_focus", "windows.foreground", a, b, {
            **f.payload(), "end_reason": r,
            "input_active_s": ctx.input_seconds(a, b)}), min_span_s=0)

    # Entrees : la meme API que le gestionnaire Raw Input.
    debut = maintenant - timedelta(minutes=2)

    for seconde in range(0, 90, 3):
        ctx.inputs.key(debut.timestamp() + seconde)

    ctx.inputs.click(debut.timestamp() + 10)
    focus.foreground(debut, fenetre)
    focus.close(maintenant, "tracker_stop")

    for minute_debut, minute in ctx.inputs.pop_all():
        factory.point("input_activity", "windows.rawinput",
                      from_epoch(minute_debut), minute.payload(),
                      historique=True)

    # 2. Mesures systeme reelles (psutil + GPU par PDH).
    metriques = MetricsCollector(ctx)
    time.sleep(1)
    metriques.poll(utc_now())

    # 3. La machine elle-meme.
    DeviceCollector(ctx).poll(utc_now())

    # 4. Veilles et demarrages lus dans le vrai journal Windows.
    for type_, a, b, payload, cle in to_events(
            query(maintenant - timedelta(days=2))):
        if b is None:
            factory.point(type_, "windows.eventlog", a, payload,
                          naturelle=cle, historique=True)
        else:
            factory.interval(type_, "windows.eventlog", a, b, payload,
                             naturelle=cle, historique=True)

    # 5. Un vrai depot git, un vrai commit.
    depot = tmp_path / "depots" / "projet"
    depot.mkdir(parents=True)

    for commande in (["init", "-q", "-b", "main"],
                     ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(depot), *commande], check=True)

    (depot / "x.py").write_text("print('bonjour')\n")
    subprocess.run(["git", "-C", str(depot), "add", "."], check=True)
    subprocess.run(["git", "-C", str(depot), "commit", "-q", "-m",
                    "Premier commit"], check=True)
    GitCollector(ctx).poll(utc_now())
    hooks.close()

    en_file = boite.counts()["pending"]
    assert en_file >= 5

    # -------------------------------------- synchronisation HTTP reelle
    syncer = Syncer(boite, ChronicleClient(chronicle, "test-key"),
                    "windows-main", batch_size=4)
    syncer.sync_once()
    assert boite.counts() == {"pending": 0, "sent": en_file, "dead": 0}

    # ---------------------------------------- dans les tables Chronicle
    assert sql("SELECT code FROM source") == [("pc:windows-main",)]
    kinds = {r[0] for r in sql("SELECT kind FROM episode")}
    assert {"app_focus", "git_commit"} <= kinds

    (focus_ligne,) = sql("SELECT payload->>'app', "
                         "(payload->>'input_active_s')::int FROM episode "
                         "WHERE kind = 'app_focus'")
    # 30 touches (une toutes les 3 s) + 1 clic a une seconde distincte.
    assert focus_ligne[0] == fenetre.app and focus_ligne[1] == 31

    mesures = {r[0] for r in sql(
        "SELECT m.code FROM observation o JOIN metric m ON m.id = o.metric_id")}
    assert {"pc.cpu_pct", "pc.ram_pct", "pc.input.keys",
            "pc.input.active_s"} <= mesures

    assert sql("SELECT kind FROM profile_snapshot") == [("pc_device",)]
    assert sql("SELECT count(*) FROM raw_payload WHERE endpoint = "
               "'pc/events'")[0][0] >= 2           # plusieurs lots de 4
    assert sql("SELECT endpoint FROM sync_state") == [("pc/windows-main",)]

    (jour,) = sql("SELECT device, commits, touches FROM v_pc_daily "
                  "WHERE commits > 0")
    assert jour == ("windows-main", 1, 30)

    # --------------------- tout renvoyer : aucune ligne ne se duplique
    avant = sql("SELECT (SELECT count(*) FROM observation), "
                "(SELECT count(*) FROM episode)")
    boite.requeue_sent(0)
    syncer.sync_once()
    assert sql("SELECT (SELECT count(*) FROM observation), "
               "(SELECT count(*) FROM episode)") == avant
    boite.close()
