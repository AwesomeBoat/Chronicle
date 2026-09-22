"""Le dashboard PC : routes de lecture, contre une base PostgreSQL DE TEST.

Les donnees passent par le vrai chemin (POST /api/v1/events), puis on
relit ce que la page affichera. Saute sans base de test designee (voir
tests/conftest.py) - sauf les tests purs en bas de fichier.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pc.tracker.core.clock import uuid7
from pc.tracker.core.events import EventFactory

requires_db = pytest.mark.skipif(
    not all(os.environ.get(f"CHRONICLE_TEST_POSTGRES_{k}")
            for k in ("HOST", "PORT", "DB", "USER", "PASSWORD")),
    reason="base de test non designee (CHRONICLE_TEST_POSTGRES_*)")

CLE = {"X-API-Key": "test-key"}
MACHINE = "windows-main"


def fuseau() -> ZoneInfo:
    from config import LOCAL_TZ
    return ZoneInfo(LOCAL_TZ)


# Un jour fixe dans le passe, loin de tout changement d'heure.
JOUR = date.today() - timedelta(days=10)
LENDEMAIN = JOUR + timedelta(days=1)


def local(jour: date, heure: int, minute: int = 0) -> datetime:
    """Une heure LOCALE (celle du dashboard), en UTC."""
    return datetime.combine(jour, time(heure, minute),
                            fuseau()).astimezone(timezone.utc)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from database.connection import create_tables
    create_tables()

    from api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def donnees(client):
    """Vide les tables, puis envoie une journee connue par l'API."""
    from sqlalchemy import text

    from database.connection import engine

    with engine.begin() as connexion:
        connexion.execute(text(
            "TRUNCATE observation, episode, profile_snapshot, raw_payload, "
            "sync_state, metric, source RESTART IDENTITY CASCADE"))

    emis: list[dict] = []
    f = EventFactory(MACHINE, emis.append, "Europe/Paris")

    def focus(debut, fin, app, actif):
        f.interval("app_focus", "windows.foreground", debut, fin,
                   {"app": app, "app_name": app.title(), "end_reason": "switch",
                    "window_title": f"titre {app}", "input_active_s": actif},
                   historique=True)

    # 10:00-10:20 navigateur, 10:20-10:30 calculatrice (inconnue : "autre"),
    # puis 23:30 -> 00:30 VS Code, a cheval sur minuit.
    focus(local(JOUR, 10), local(JOUR, 10, 20), "chrome", 600)
    focus(local(JOUR, 10, 20), local(JOUR, 10, 30), "calculator", 60)
    focus(local(JOUR, 23, 30), local(LENDEMAIN, 0, 30), "vscode", 1800)

    f.interval("idle", "windows.idle", local(JOUR, 23, 40), local(JOUR, 23, 45),
               {"threshold_s": 120, "end_reason": "input"}, historique=True)
    f.interval("system_sleep", "windows.power", local(LENDEMAIN, 1),
               local(LENDEMAIN, 7), {"state": "sleep", "origin": "live"},
               historique=True)
    f.interval("tracker_run", "tracker", local(JOUR, 9, 55),
               local(LENDEMAIN, 0, 35), {"end_reason": "shutdown",
                                         "version": "test"}, historique=True)
    f.interval("browser_page", "browser_ext", local(JOUR, 10),
               local(JOUR, 10, 20), {"browser": "chrome",
                                     "domain": "github.com",
                                     "end_reason": "blur"}, historique=True)

    # 3 minutes d'entrees a 10 h (40 s actives chacune), 1 a 23 h 50.
    for minute, actif in ((0, 40), (1, 40), (2, 40)):
        f.point("input_activity", "windows.rawinput",
                local(JOUR, 10, minute), {"interval_s": 60, "keys": 50,
                                          "clicks": 2, "scroll": 0,
                                          "active_s": actif},
                historique=True)
    f.point("input_activity", "windows.rawinput", local(JOUR, 23, 50),
            {"interval_s": 60, "keys": 10, "clicks": 0, "scroll": 0,
             "active_s": 30}, historique=True)
    f.point("system_metrics", "windows.metrics", local(JOUR, 10),
            {"interval_s": 60, "cpu_pct": 20.0, "ram_pct": 50.0,
             "net_down_bytes": 3_000_000, "net_up_bytes": 1_000_000},
            historique=True)
    f.point("git_commit", "git", local(JOUR, 23, 50),
            {"repo": "Chronicle", "commit": "a" * 40, "branch": "main",
             "message": "Ajoute le dashboard", "insertions": 120,
             "deletions": 7, "files_changed": 4}, historique=True)
    f.interval("terminal_command", "shell.pwsh", local(JOUR, 23, 51),
               local(JOUR, 23, 52), {"shell": "pwsh", "command": "git status",
                                     "redacted": False, "program": "git"},
               historique=True)
    f.point("file_change", "windows.files", local(JOUR, 23, 53),
            {"action": "modified", "path": "C:/projets/x.py", "is_dir": False,
             "extension": ".py"}, historique=True)
    f.point("collector_status", "tracker", local(JOUR, 10, 5),
            {"collector": "media", "status": "error", "detail": "test"},
            historique=True)

    reponse = client.post("/api/v1/events", headers=CLE, json={
        "batch_id": uuid7(), "device_id": MACHINE,
        "sent_at": datetime.now(timezone.utc).isoformat(), "events": emis})
    assert reponse.status_code == 200 and not reponse.json()["rejected"]


def resume(client, debut: date, fin: date, **params) -> dict:
    reponse = client.get("/api/v1/pc/summary", params={
        "start": debut.isoformat(), "end": fin.isoformat(), **params})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


# ================================================================ acces

@requires_db
def test_local_sans_cle(client, donnees):
    assert client.get("/api/v1/pc/meta").status_code == 200


@requires_db
def test_hote_etranger_refuse_sans_cle(client, donnees):
    """Une page web qui se ferait passer pour localhost (DNS rebinding)
    arrive avec son propre nom d'hote : refusee."""
    entetes = {"Host": "evil.example"}
    assert client.get("/api/v1/pc/meta", headers=entetes).status_code == 401
    assert client.get("/api/v1/pc/meta", headers={
        **entetes, "X-API-Key": "mauvaise"}).status_code == 401
    assert client.get("/api/v1/pc/meta", headers={
        **entetes, **CLE}).status_code == 200


@requires_db
def test_page_servie(client):
    reponse = client.get("/", follow_redirects=False)
    assert reponse.status_code in (302, 307)
    assert reponse.headers["location"] == "/dashboard/"

    page = client.get("/dashboard/")
    assert page.status_code == 200 and "Activité PC" in page.text
    assert client.get("/dashboard/dashboard.js").status_code == 200


# ================================================================ /meta

@requires_db
def test_meta(client, donnees):
    corps = client.get("/api/v1/pc/meta").json()

    assert [d["device"] for d in corps["devices"]] == [MACHINE]
    assert corps["devices"][0]["last_batch_at"]
    # L'ordre des categories est l'ordre des couleurs : fixe.
    assert [(c["id"], c["slot"]) for c in corps["categories"]] == [
        ("code", 1), ("web", 2), ("communication", 3), ("documents", 4),
        ("medias", 5), ("systeme", 6), ("autre", None)]


# ============================================================= /summary

@requires_db
def test_intervalle_coupe_a_minuit(client, donnees):
    """23 h 30 -> 00 h 30 : 30 min pour chaque jour, pas 60 pour le
    premier."""
    corps = resume(client, JOUR, LENDEMAIN)
    jours = {b["key"]: b for b in corps["buckets"]}

    assert corps["range"]["bucket"] == "day"
    assert list(jours) == [JOUR.isoformat(), LENDEMAIN.isoformat()]
    assert jours[JOUR.isoformat()]["categories"] == {
        "web": 20.0, "autre": 10.0, "code": 30.0}
    assert jours[LENDEMAIN.isoformat()]["categories"] == {"code": 30.0}

    assert corps["kpis"]["premier_plan_min"] == 90.0
    # Actif : les comptes d'entrees (3 x 40 s + 30 s), pas les fenetres.
    assert corps["kpis"]["actif_min"] == 2.5
    assert corps["kpis"]["inactif_min"] == 5.0
    assert corps["kpis"]["veille_min"] == 360.0
    assert corps["empty"] is False


@requires_db
def test_un_jour_seul_par_heure(client, donnees):
    corps = resume(client, JOUR, JOUR)
    heures = {b["key"]: b for b in corps["buckets"]}

    assert corps["range"]["bucket"] == "hour" and len(heures) == 24
    assert heures["10"]["categories"] == {"web": 20.0, "autre": 10.0}
    assert heures["10"]["actif_min"] == 2.0
    assert heures["23"]["categories"] == {"code": 30.0}
    # Le lendemain n'entre pas dans la journee.
    assert corps["kpis"]["premier_plan_min"] == 60.0


@requires_db
def test_applications_au_prorata(client, donnees):
    """VS Code : 60 min dont 30 dans JOUR ; 1 800 s actives -> la moitie."""
    apps = {a["app"]: a for a in resume(client, JOUR, JOUR)["apps"]}

    assert apps["vscode"]["minutes"] == 30.0
    assert apps["vscode"]["actif_min"] == 15.0
    assert apps["vscode"]["category"] == "code"
    assert apps["calculator"]["category"] == "autre"
    assert list(apps)[0] == "vscode"            # trie par duree


@requires_db
def test_le_reste_du_resume(client, donnees):
    corps = resume(client, JOUR, LENDEMAIN)

    assert corps["domains"] == [{"domain": "github.com", "pages": 1,
                                 "minutes": 20.0}]
    assert [(t["from"], t["to"], t["n"]) for t in corps["transitions"]] == [
        ("chrome", "calculator", 1)]
    assert corps["commits"][0]["insertions"] == 120
    assert corps["commits"][0]["message"] == "Ajoute le dashboard"
    assert corps["programs"] == [{"label": "git", "n": 1}]
    assert corps["extensions"] == [{"label": ".py", "n": 1}]
    assert corps["kpis"]["commits"] == 1 and corps["kpis"]["lignes_plus"] == 120

    machine = {m["key"]: m for m in corps["machine"]}
    assert machine[JOUR.isoformat()]["cpu"] == 20.0
    assert machine[JOUR.isoformat()]["down_mb"] == 3.0
    assert machine[LENDEMAIN.isoformat()]["cpu"] is None

    sante = corps["health"]
    assert sante["runs"] == 1
    assert [e["collector"] for e in sante["errors"]] == ["media"]
    # 09:55 -> 00:35 : 14 h 40 suivies.
    assert sante["tracked_min"] == 880.0


@requires_db
def test_periode_precedente(client, donnees):
    """Le jour d'apres, compare a JOUR : la comparaison est la."""
    corps = resume(client, LENDEMAIN, LENDEMAIN)
    assert corps["previous"]["premier_plan_min"] == 60.0
    assert corps["kpis"]["premier_plan_min"] == 30.0


@requires_db
def test_machine_inconnue_meme_forme(client, donnees):
    corps = resume(client, JOUR, LENDEMAIN, device="autre-pc")

    assert corps["empty"] is True
    assert corps["kpis"]["premier_plan_min"] == 0
    assert len(corps["buckets"]) == 2 and corps["apps"] == []
    assert corps["health"]["last_event"] is None


@requires_db
@pytest.mark.parametrize("params", [
    {"start": "2026-02-10", "end": "2026-02-01"},      # debut apres fin
    {"start": "10/02/2026"},                           # format
    {"start": "2024-01-01", "end": "2026-01-01"},      # trop long
    {"device": "PC Principal"},                        # device invalide
])
def test_parametres_refuses(client, params):
    assert client.get("/api/v1/pc/summary",
                      params=params).status_code == 422


# ================================================================= /day

@requires_db
def test_journee_decoupee(client, donnees):
    corps = client.get("/api/v1/pc/day",
                       params={"day": JOUR.isoformat()}).json()
    lanes = corps["lanes"]

    assert corps["minutes"] == 1440
    assert [(s["s"], s["e"], s["app"]) for s in lanes["web"]] == [
        (600.0, 620.0, "chrome")]
    # Coupe a minuit : 23:30 -> 24:00.
    assert [(s["s"], s["e"]) for s in lanes["code"]] == [(1410.0, 1440.0)]
    assert lanes["code"][0]["title"] == "titre vscode"
    assert [(s["s"], s["e"]) for s in lanes["idle"]] == [(1420.0, 1425.0)]
    assert "sleep" not in lanes

    lendemain = client.get("/api/v1/pc/day", params={
        "day": LENDEMAIN.isoformat()}).json()["lanes"]
    assert [(s["s"], s["e"]) for s in lendemain["code"]] == [(0.0, 30.0)]
    assert [(s["s"], s["e"], s["state"]) for s in lendemain["sleep"]] == [
        (60.0, 420.0, "sleep")]


@requires_db
def test_journee_par_defaut_aujourd_hui(client, donnees):
    from api.dashboard import _aujourd_hui

    corps = client.get("/api/v1/pc/day").json()
    assert corps["day"] == _aujourd_hui().isoformat()


# ================================================================ purs

@pytest.mark.parametrize("jour, minutes", [
    (date(2026, 9, 22), 1440),
    (date(2026, 3, 29), 1380),          # passage a l'heure d'ete
    (date(2026, 10, 25), 1500),         # retour a l'heure d'hiver
])
def test_duree_du_jour_local(jour, minutes, monkeypatch):
    from api import dashboard

    monkeypatch.setattr(dashboard, "LOCAL_TZ", "Europe/Paris")
    assert dashboard._minutes_du_jour(jour) == minutes


def test_categories():
    from analytics.pc_categories import as_payload, category_of

    assert category_of("vscode") == "code"
    assert category_of("chrome") == "web"
    assert category_of("inconnue") == "autre"
    assert category_of(None) == "autre"
    slots = [c["slot"] for c in as_payload()]
    assert slots == [1, 2, 3, 4, 5, 6, None]
