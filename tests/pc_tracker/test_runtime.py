"""Le tracker entier : arret propre, reprise apres arret brutal, redemarrage."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from pc import schema
from pc.tracker.config import COLLECTEURS, Config
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.runtime import Tracker

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


def config(tmp_path) -> Config:
    # Port 9 (discard) : un test ne doit JAMAIS parler a une vraie API.
    return Config(device_id="windows-main", data_dir=tmp_path,
                  config_dir=tmp_path, chronicle_url="http://127.0.0.1:9",
                  collectors={nom: False for nom in COLLECTEURS})


def lancer(cfg: Config, secondes: float = 1.0) -> Tracker:
    tracker = Tracker(cfg, platform=None)
    threading.Timer(secondes, tracker.request_stop,
                    args=("tracker_stop",)).start()
    assert tracker.run() == 0
    assert tracker.stopped.is_set()
    return tracker


def evenements(chemin) -> list[dict]:
    connexion = sqlite3.connect(chemin)
    corps = [json.loads(b) for (b,) in connexion.execute(
        "SELECT body FROM outbox ORDER BY seq")]
    connexion.close()
    return corps


def test_arret_propre(tmp_path):
    cfg = config(tmp_path)
    lancer(cfg)
    tous = evenements(cfg.db_path)
    types = [e["event_type"] for e in tous]
    assert "device_info" in types and types[-1] == "tracker_run"
    assert tous[-1]["payload"]["end_reason"] == "tracker_stop"
    assert all(schema.validate_event(e) == [] for e in tous)

    boite = Outbox(cfg.db_path)
    assert boite.get_state("run") is None           # lancement clos
    assert boite.state_keys("open:") == []
    assert boite.counts()["pending"] == len(tous)   # rien envoye : port mort
    boite.close()


def test_reprise_apres_arret_brutal(tmp_path):
    """Plantage simule : l'etat dit qu'un lancement a commence a T0 et que
    son dernier battement date de T0 + 10 min. Le redemarrage doit fermer
    ce lancement a T0 + 10 min, marque recovered."""
    cfg = config(tmp_path)
    boite = Outbox(cfg.db_path)
    boite.set_state("run", {"start": schema.format_ts(T0), "version": "0.1.0",
                            "platform": "windows", "collectors": ["desktop"]})
    boite.set_state("heartbeat", schema.format_ts(T0 + timedelta(minutes=10)))
    boite.close()

    lancer(cfg)
    runs = [e for e in evenements(cfg.db_path)
            if e["event_type"] == "tracker_run"]
    recupere, normal = runs
    assert recupere["ts"] == schema.format_ts(T0)
    assert recupere["payload"]["ended_at"] == schema.format_ts(
        T0 + timedelta(minutes=10))
    assert recupere["payload"]["end_reason"] == "recovered"
    assert normal["payload"]["end_reason"] == "tracker_stop"


def test_redemarrage_ne_duplique_pas_la_reprise(tmp_path):
    cfg = config(tmp_path)
    boite = Outbox(cfg.db_path)
    boite.set_state("run", {"start": schema.format_ts(T0)})
    boite.set_state("heartbeat", schema.format_ts(T0 + timedelta(minutes=1)))
    boite.close()

    lancer(cfg)
    lancer(cfg)
    recuperes = [e for e in evenements(cfg.db_path)
                 if e["event_type"] == "tracker_run"
                 and e["payload"]["end_reason"] == "recovered"]
    assert len(recuperes) == 1


def test_deux_lancements_gardent_le_meme_etat_machine(tmp_path):
    """Le sel de hachage (SSID) est cree une fois et ne change plus."""
    cfg = config(tmp_path)
    lancer(cfg, 0.5)
    premier = Outbox(cfg.db_path).get_state("salt")
    lancer(cfg, 0.5)
    assert Outbox(cfg.db_path).get_state("salt") == premier and premier
