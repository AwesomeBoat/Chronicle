"""UUIDv7, unicite des instants, fabrique d'evenements, cle de dedoublonnage."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from pc import schema
from pc.tracker.core.clock import (UniqueClock, local_iso, uuid7, uuid7_time,
                                   utc_now)
from pc.tracker.core.events import EventFactory, dedup_key

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def test_uuid7_forme_et_version():
    identifiant = uuid7()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}"
                        r"-[0-9a-f]{12}", identifiant)


def test_uuid7_trie_comme_le_temps_et_unique():
    identifiants = [uuid7() for _ in range(5000)]
    assert identifiants == sorted(identifiants)
    assert len(set(identifiants)) == 5000


def test_uuid7_porte_son_instant():
    assert abs((uuid7_time(uuid7()) - utc_now()).total_seconds()) < 1


def test_unique_clock_decale_d_une_microseconde():
    horloge = UniqueClock()
    a = horloge.unique("file_change", T0)
    b = horloge.unique("file_change", T0)
    c = horloge.unique("file_change", T0 - timedelta(seconds=1))
    assert (a, b, c) == (T0, T0 + timedelta(microseconds=1),
                         T0 + timedelta(microseconds=2))


def test_unique_clock_independant_par_type():
    horloge = UniqueClock()
    assert horloge.unique("idle", T0) == T0
    assert horloge.unique("app_focus", T0) == T0


def test_local_iso_porte_un_decalage():
    assert re.search(r"[+-]\d{2}:\d{2}$", local_iso(T0))
    assert datetime.fromisoformat(local_iso(T0)) == T0


def test_dedup_key_deterministe_et_propre_a_la_machine():
    a = dedup_key("windows-main", "system_boot", "boot|x")
    assert a == dedup_key("windows-main", "system_boot", "boot|x")
    assert a != dedup_key("omarchy-desktop", "system_boot", "boot|x")
    assert len(a) == 32


@pytest.fixture
def usine():
    emis: list[dict] = []
    return EventFactory("windows-main", emis.append, "Europe/Brussels"), emis


def test_factory_point_valide(usine):
    factory, emis = usine
    e = factory.point("app_launch", "windows.processes", T0,
                      {"app": "vscode", "pid": 42})
    assert emis == [e]
    assert e["source"] == "pc" and e["device_id"] == "windows-main"
    assert e["ts"] == "2026-09-22T12:00:00.000000Z"
    assert e["tz"] == "Europe/Brussels"
    assert schema.validate_event(e, T0) == []


def test_factory_intervalle_calcule_fin_et_duree(usine):
    factory, _ = usine
    e = factory.interval("idle", "windows.idle", T0, T0 + timedelta(
        minutes=5), {"threshold_s": 120, "end_reason": "input"})
    assert e["payload"]["duration_s"] == 300
    assert e["payload"]["ended_at"] == "2026-09-22T12:05:00.000000Z"


def test_factory_fin_avant_debut_ramenee_a_zero(usine):
    """Horloge reculee pendant l'intervalle : duree nulle, jamais negative."""
    factory, _ = usine
    e = factory.interval("session_locked", "x", T0, T0 - timedelta(1),
                         {"end_reason": "unlock"})
    assert e["payload"]["duration_s"] == 0


def test_factory_rejette_un_evenement_invalide(usine):
    factory, emis = usine
    assert factory.point("file_change", "x", T0, {"action": "lu",
                                                  "path": "~/a",
                                                  "is_dir": False}) is None
    assert emis == [] and factory.rejetes == 1


def test_factory_instants_uniques_par_type(usine):
    factory, _ = usine
    a = factory.point("file_change", "x", T0, {"action": "created",
                                               "path": "~/a", "is_dir": False})
    b = factory.point("file_change", "x", T0, {"action": "created",
                                               "path": "~/b", "is_dir": False})
    assert a["ts"] != b["ts"]


def test_factory_historique_garde_l_instant_exact(usine):
    """Un commit relu deux fois garde le meme instant, donc la meme cle."""
    factory, _ = usine
    a = factory.point("git_commit", "git", T0, {"repo": "r", "commit": "c"},
                      naturelle="c", historique=True)
    b = factory.point("git_commit", "git", T0, {"repo": "r", "commit": "c"},
                      naturelle="c", historique=True)
    assert a["ts"] == b["ts"] and a["dedup_key"] == b["dedup_key"]
    assert a["event_id"] != b["event_id"]


def test_factory_refuse_un_device_id_invalide():
    with pytest.raises(ValueError):
        EventFactory("Windows Main", lambda e: None)
