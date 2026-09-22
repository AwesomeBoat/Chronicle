"""L'API d'ingestion contre une vraie base PostgreSQL DE TEST.

Saute si aucune base de test n'est designee (voir tests/conftest.py).
Chaque test part de tables vides.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from pc.tracker.core.clock import uuid7
from pc.tracker.core.events import EventFactory

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(f"CHRONICLE_TEST_POSTGRES_{k}")
            for k in ("HOST", "PORT", "DB", "USER", "PASSWORD")),
    reason="base de test non designee (CHRONICLE_TEST_POSTGRES_*)")

CLE = {"X-API-Key": "test-key"}
# Avant-hier a 10 h UTC : loin de minuit (les vues groupent par jour
# local) et dans la fenetre plausible de l'API.
T0 = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
    hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from database.connection import create_tables
    create_tables()

    from api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def tables_vides(client):
    from sqlalchemy import text

    from database.connection import engine

    with engine.begin() as connexion:
        connexion.execute(text(
            "TRUNCATE observation, episode, profile_snapshot, raw_payload, "
            "sync_state, metric, source RESTART IDENTITY CASCADE"))


def sql(requete: str, **params):
    from sqlalchemy import text

    from database.connection import engine

    with engine.connect() as connexion:
        return connexion.execute(text(requete), params).all()


def compter() -> dict[str, int]:
    return {t: sql(f"SELECT count(*) FROM {t}")[0][0]
            for t in ("observation", "episode", "profile_snapshot",
                      "raw_payload")}


def journee(machine: str = "windows-main") -> list[dict]:
    """Une petite journee : 3 fenetres, une inactivite, des mesures."""
    emis: list[dict] = []
    f = EventFactory(machine, emis.append, "Europe/Brussels")

    def focus(debut, fin, app, raison="switch", actif=None):
        f.interval("app_focus", "windows.foreground", T0 + timedelta(
            minutes=debut), T0 + timedelta(minutes=fin),
            {"app": app, "app_name": app.title(), "end_reason": raison,
             "input_active_s": actif})

    focus(0, 29, "vscode", actif=1500)
    focus(29, 32, "chrome", actif=60)
    focus(32, 39, "discord", actif=100)
    focus(39, 80, "vscode", raison="lock", actif=1800)
    f.interval("idle", "windows.idle", T0 + timedelta(minutes=50),
               T0 + timedelta(minutes=55), {"threshold_s": 120,
                                              "end_reason": "input"})
    f.interval("browser_page", "browser_ext", T0 + timedelta(minutes=29),
               T0 + timedelta(minutes=32), {"browser": "chrome",
                                              "domain": "youtube.com",
                                              "end_reason": "blur",
                                              "input_active_s": 30})
    f.point("system_metrics", "psutil", T0, {"interval_s": 60,
                                             "cpu_pct": 20.0,
                                             "ram_pct": 50.0,
                                             "net_down_bytes": 2_000_000})
    f.point("input_activity", "windows.rawinput", T0, {
        "interval_s": 60, "keys": 120, "clicks": 4, "scroll": 2,
        "active_s": 48}, historique=True)
    f.point("device_info", "tracker", T0, {"hostname": "pc", "os": "windows"})
    return emis


def lot(evenements: list[dict], machine: str = "windows-main",
        batch_id: str | None = None) -> dict:
    return {"batch_id": batch_id or uuid7(), "device_id": machine,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "events": evenements}


# ================================================================ acces

def test_sante_sans_cle(client):
    reponse = client.get("/health")
    assert reponse.status_code == 200
    assert reponse.json()["database"] == "ok"


@pytest.mark.parametrize("entetes", [{}, {"X-API-Key": "mauvaise"}])
def test_cle_obligatoire(client, entetes):
    assert client.post("/api/v1/events", json=lot([]),
                       headers=entetes).status_code == 401


def test_ping_lot_vide(client):
    reponse = client.post("/api/v1/events", json=lot([]), headers=CLE)
    assert reponse.status_code == 200 and reponse.json()["stored"] == 0
    assert compter()["raw_payload"] == 0


# ============================================================ ingestion

def test_un_lot_arrive_dans_les_tables_existantes(client):
    evenements = journee()
    reponse = client.post("/api/v1/events", json=lot(evenements), headers=CLE)
    corps = reponse.json()

    assert reponse.status_code == 200, corps
    assert corps["stored"] == len(evenements) and corps["rejected"] == []
    # 3 mesures systeme + 4 comptes d'entrees ; 4 fenetres + 1 inactivite
    # + 1 page ; 1 etat de la machine.
    assert compter() == {"observation": 7, "episode": 6,
                         "profile_snapshot": 1, "raw_payload": 1}
    assert sql("SELECT code, label FROM source") == [("pc:windows-main",
                                                      "PC windows-main")]
    (flux,) = sql("SELECT endpoint, cursor_at FROM sync_state")
    assert flux[0] == "pc/windows-main"
    assert flux[1] == T0 + timedelta(minutes=80)


def test_lot_rejoue_a_l_identique(client):
    """Recu, reponse perdue, renvoye : rien n'est retraite."""
    corps = lot(journee())
    client.post("/api/v1/events", json=corps, headers=CLE)
    avant = compter()
    corps["sent_at"] = datetime.now(timezone.utc).isoformat()
    reponse = client.post("/api/v1/events", json=corps, headers=CLE).json()
    assert reponse["replayed"] is True
    assert compter() == avant


def test_memes_evenements_autre_lot_aucun_doublon(client):
    evenements = journee()
    client.post("/api/v1/events", json=lot(evenements), headers=CLE)
    avant = compter()
    client.post("/api/v1/events", json=lot(evenements), headers=CLE)
    apres = compter()
    assert apres["observation"] == avant["observation"]
    assert apres["episode"] == avant["episode"]
    assert apres["raw_payload"] == avant["raw_payload"] + 1


def test_deux_machines_au_meme_instant(client):
    """La raison d'une source par machine : aucune collision."""
    client.post("/api/v1/events", json=lot(journee("windows-main")),
                headers=CLE)
    client.post("/api/v1/events",
                json=lot(journee("omarchy-desktop"), "omarchy-desktop"),
                headers=CLE)
    assert compter()["episode"] == 12 and compter()["observation"] == 14
    assert {r[0] for r in sql("SELECT code FROM source")} == \
        {"pc:windows-main", "pc:omarchy-desktop"}


def test_evenement_invalide_refuse_brut_garde(client):
    evenements = journee()
    evenements[0]["payload"]["duration_s"] = "longtemps"
    corps = client.post("/api/v1/events", json=lot(evenements),
                        headers=CLE).json()
    assert len(corps["rejected"]) == 1
    assert corps["stored"] == len(evenements) - 1
    (brut,) = sql("SELECT payload FROM raw_payload")
    assert len(brut[0]["events"]) == len(evenements)     # rien de perdu


def test_evenement_d_une_autre_machine_refuse(client):
    evenements = journee("omarchy-desktop")
    corps = client.post("/api/v1/events", json=lot(evenements),
                        headers=CLE).json()
    assert len(corps["rejected"]) == len(evenements)


@pytest.mark.parametrize("corps, code", [
    ({"batch_id": "court", "device_id": "windows-main", "events": []}, 422),
    ({"batch_id": uuid7(), "device_id": "Windows", "events": []}, 422),
    ({"batch_id": uuid7(), "device_id": "windows-main", "events": "x"}, 422)])
def test_enveloppe_de_lot_invalide(client, corps, code):
    assert client.post("/api/v1/events", json=corps,
                       headers=CLE).status_code == code


def test_source_inconnue(client):
    evenement = journee()[0]
    evenement["source"] = "phone"
    assert client.post("/api/v1/events", json=lot([evenement]),
                       headers=CLE).status_code == 422


def test_lot_trop_gros(client, monkeypatch):
    from api import settings
    monkeypatch.setattr(settings, "MAX_EVENTS", 2)
    assert client.post("/api/v1/events", json=lot(journee()),
                       headers=CLE).status_code == 413


def test_base_indisponible_503(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import api.app

    def en_panne(*_a, **_k):
        raise OperationalError("SELECT 1", {}, Exception("connexion refusee"))

    monkeypatch.setattr(api.app, "ingest_batch", en_panne)
    assert client.post("/api/v1/events", json=lot(journee()),
                       headers=CLE).status_code == 503


def test_machines_connues(client):
    client.post("/api/v1/events", json=lot(journee()), headers=CLE)
    (machine,) = client.get("/api/v1/devices", headers=CLE).json()
    assert machine["device_id"] == "windows-main" and machine["data_until"]


# =========================================================== derive, rejeu

def test_vues_derivees(client):
    from database.connection import create_tables
    create_tables()                                  # vues a jour
    client.post("/api/v1/events", json=lot(journee()), headers=CLE)

    apps = {r[0]: (r[1], r[2]) for r in sql(
        "SELECT app, minutes_premier_plan, minutes_actives "
        "FROM v_pc_apps_daily")}
    assert apps["vscode"] == (70.0, 55.0)
    assert apps["chrome"][0] == 3.0

    changements = sql("SELECT de, vers FROM v_pc_context_switches "
                      "ORDER BY instant")
    assert changements == [("vscode", "chrome"), ("chrome", "discord"),
                           ("discord", "vscode")]

    (jour,) = sql("SELECT minutes_premier_plan, minutes_inactif, "
                  "changements_contexte, touches, reseau_recu_mo "
                  "FROM v_pc_daily")
    assert jour == (80.0, 5.0, 3, 120, 2.0)

    assert sql("SELECT domaine, minutes FROM v_pc_domains_daily") == \
        [("youtube.com", 3.0)]


def test_rejeu_depuis_l_archive(client):
    """`python main.py pc` : on rejoue le brut sans rien dupliquer."""
    from api.ingest import replay_archived

    client.post("/api/v1/events", json=lot(journee()), headers=CLE)
    sql_avant = compter()
    totaux = replay_archived()
    assert totaux == {"lots": 1, "evenements": 9, "refus": 0}
    assert compter() == sql_avant


def test_les_autres_sources_restent_intactes(client):
    """Une ligne Polar existante ne bouge pas quand le PC ecrit."""
    from database.connection import session_scope
    from database.records import Batch, MetricSpec, ObservationRecord
    from database.repository import (get_or_create_source, store_batch,
                                     sync_metrics)

    with session_scope() as session:
        source = get_or_create_source(session, "polar_accesslink", "Polar")
        metriques = sync_metrics(session, [MetricSpec("heart_rate", "bpm")])
        store_batch(session, source, metriques, Batch(observations=[
            ObservationRecord("heart_rate", T0, 61.0)]))

    client.post("/api/v1/events", json=lot(journee()), headers=CLE)
    assert sql("SELECT o.value FROM observation o JOIN metric m "
               "ON m.id = o.metric_id WHERE m.code = 'heart_rate'") == [(61.0,)]
