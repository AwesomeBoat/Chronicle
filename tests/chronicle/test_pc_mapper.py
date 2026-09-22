"""Le connecteur PC : evenements -> Batch. Aucune base requise."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pc import mapper, schema
from pc.tracker.core.events import EventFactory

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def evenements():
    emis: list[dict] = []
    f = EventFactory("windows-main", emis.append, "Europe/Brussels")

    f.interval("app_focus", "windows.foreground", T0, T0 + timedelta(
        minutes=5), {"app": "vscode", "app_name": "Visual Studio Code",
                     "process": "Code.exe", "window_title": "main.py",
                     "end_reason": "switch", "input_active_s": 180})
    f.point("git_commit", "git.reflog", T0, {"repo": "Chronicle",
                                             "commit": "a" * 40,
                                             "insertions": 12},
            historique=True)
    f.point("system_metrics", "psutil", T0, {
        "interval_s": 60, "cpu_pct": 12.5, "ram_pct": 61.0,
        "cpu_temp_c": None, "battery_pct": None, "on_ac": True})
    f.point("input_activity", "windows.rawinput", T0, {
        "interval_s": 60, "keys": 40, "clicks": 3, "scroll": 0,
        "mouse_distance": 1200.0, "active_s": 37}, historique=True)
    f.point("device_info", "tracker", T0, {"hostname": "desktop00107",
                                           "os": "windows"})
    return emis


def test_contrat_du_connecteur():
    assert mapper.SOURCE_CODE == "pc"
    assert mapper.source_code("windows-main") == "pc:windows-main"
    assert mapper.sync_endpoint("omarchy-desktop") == "pc/omarchy-desktop"
    codes = [m.code for m in mapper.METRICS]
    assert len(codes) == len(set(codes)) == 18
    assert all(c.startswith("pc.") for c in codes)


def test_chaque_forme_va_dans_sa_table(evenements):
    lot, refus = mapper.map_events(evenements, "windows-main", now=T0)
    assert refus == []

    kinds = sorted(e.kind for e in lot.episodes)
    assert kinds == ["app_focus", "git_commit"]

    focus = next(e for e in lot.episodes if e.kind == "app_focus")
    assert focus.ended_at - focus.started_at == timedelta(minutes=5)
    assert focus.payload["event_id"] and focus.payload["device_id"] == \
        "windows-main"

    commit = next(e for e in lot.episodes if e.kind == "git_commit")
    assert commit.ended_at == commit.started_at          # un point

    observations = {o.metric_code: o.value for o in lot.observations}
    assert observations == {"pc.cpu_pct": 12.5, "pc.ram_pct": 61.0,
                            "pc.input.keys": 40, "pc.input.clicks": 3,
                            "pc.input.scroll": 0,
                            "pc.input.mouse_distance": 1200.0,
                            "pc.input.active_s": 37}

    (instantane,) = lot.snapshots
    assert instantane.kind == "pc_device"


def test_null_n_est_jamais_un_zero(evenements):
    lot, _ = mapper.map_events(evenements, "windows-main", now=T0)
    codes = {o.metric_code for o in lot.observations}
    assert "pc.cpu_temp_c" not in codes and "pc.battery_pct" not in codes


def test_evenement_invalide_refuse_le_reste_passe(evenements):
    evenements[0]["payload"]["duration_s"] = -5
    lot, refus = mapper.map_events(evenements, "windows-main", now=T0)
    assert len(refus) == 1 and refus[0]["event_type"] == "app_focus"
    assert [e.kind for e in lot.episodes] == ["git_commit"]


def test_device_id_different_du_lot_refuse(evenements):
    _, refus = mapper.map_events(evenements, "omarchy-desktop", now=T0)
    assert len(refus) == len(evenements)


def test_meme_modele_pour_windows_et_linux():
    """Deux machines, meme evenement : seuls device_id et producer changent,
    et le Batch produit est identique a la source pres."""
    lots = []

    for machine, producteur in (("windows-main", "windows.foreground"),
                                ("omarchy-desktop", "linux.hyprland")):
        emis: list[dict] = []
        EventFactory(machine, emis.append).interval(
            "app_focus", producteur, T0, T0 + timedelta(minutes=1),
            {"app": "vscode", "end_reason": "switch"})
        lot, _ = mapper.map_events(emis, machine, now=T0)
        lots.append(lot.episodes[0])

    windows, linux = lots
    assert (windows.kind, windows.started_at, windows.ended_at) == \
        (linux.kind, linux.started_at, linux.ended_at)
    commun = {k: v for k, v in windows.payload.items()
              if k not in ("event_id", "device_id", "producer")}
    assert commun == {k: v for k, v in linux.payload.items()
                      if k not in ("event_id", "device_id", "producer")}


def test_curseur_fin_la_plus_recente(evenements):
    assert mapper.cursor(evenements) == T0 + timedelta(minutes=5)


def test_aucun_nom_d_episode_ne_collisionne_avec_les_autres_sources():
    """v_sleep et v_chambre lisent kind='sleep' sans filtrer la source."""
    autres = {"sleep", "exercise", "activity_period", "activity_zone",
              "inactivity_stamp", "sleep_availability", "nightly_recharge",
              "cardio_load_day", "alertness_period", "alertness_hour",
              "circadian_bedtime", "meal", "reading_session", "highlight",
              "note", "bookmark", "word_lookup"}
    assert not (set(schema.EVENT_TYPES) & autres)
