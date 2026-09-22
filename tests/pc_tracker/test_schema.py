"""Le contrat commun : enveloppe, types, validation, temps, device_id."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pc import schema
from pc.tracker.core.clock import uuid7

T0 = datetime(2026, 9, 22, 12, 0, 0, 123456, tzinfo=timezone.utc)


def evenement(event_type="app_focus", payload=None, **enveloppe) -> dict:
    base = {
        "event_id": uuid7(), "source": "pc", "producer": "windows.foreground",
        "event_type": event_type, "ts": schema.format_ts(T0),
        "ts_local": "2026-09-22T14:00:00.123456+02:00", "tz": "Europe/Brussels",
        "device_id": "windows-main", "dedup_key": "a" * 32, "schema_v": 1,
        "payload": payload if payload is not None else {
            "ended_at": schema.format_ts(T0 + timedelta(seconds=312)),
            "duration_s": 312.0, "app": "vscode", "end_reason": "switch"},
    }
    base.update(enveloppe)
    return base


# ------------------------------------------------------------- temps

def test_format_ts_est_utc_microsecondes_z():
    paris = timezone(timedelta(hours=2))
    moment = datetime(2026, 9, 22, 14, 0, 0, 5, tzinfo=paris)
    assert schema.format_ts(moment) == "2026-09-22T12:00:00.000005Z"


def test_format_ts_refuse_un_instant_naif():
    with pytest.raises(ValueError):
        schema.format_ts(datetime(2026, 9, 22, 12, 0))


def test_parse_ts_aller_retour_et_offsets():
    assert schema.parse_ts(schema.format_ts(T0)) == T0
    assert schema.parse_ts("2026-09-22T14:00:00.123456+02:00") == T0


def test_parse_ts_refuse_naif():
    with pytest.raises(ValueError):
        schema.parse_ts("2026-09-22T12:00:00")


# --------------------------------------------------------- device_id

@pytest.mark.parametrize("device_id", ["windows-main", "omarchy-desktop",
                                       "pc2", "a1"])
def test_device_id_valides(device_id):
    assert schema.valid_device_id(device_id)


@pytest.mark.parametrize("device_id", ["", "a", "Windows-Main", "-pc",
                                       "pc-", "pc main", "pc_main",
                                       "x" * 41, None, 42])
def test_device_id_invalides(device_id):
    assert not schema.valid_device_id(device_id)


# -------------------------------------------------------- validation

def test_evenement_valide():
    assert schema.validate_event(evenement(), now=T0) == []


def test_serialisation_json_aller_retour():
    e = evenement()
    assert schema.validate_event(json.loads(json.dumps(e)), now=T0) == []


@pytest.mark.parametrize("champ, valeur", [
    ("event_id", "pas-un-uuid"), ("source", "phone"), ("producer", "A B"),
    ("device_id", "Windows"), ("schema_v", 2), ("ts", "2026-09-22T12:00:00"),
    ("dedup_key", "x" * 65), ("event_type", "keylogger")])
def test_enveloppe_invalide(champ, valeur):
    assert schema.validate_event(evenement(**{champ: valeur}), now=T0)


def test_horloge_implausible_refusee():
    passe = evenement(ts="2019-12-31T23:59:59.000000Z")
    futur = evenement(ts=schema.format_ts(T0 + timedelta(days=3)))
    assert any("implausible" in e for e in schema.validate_event(passe, T0))
    assert any("implausible" in e for e in schema.validate_event(futur, T0))


def test_intervalle_fin_avant_debut_refuse():
    e = evenement(payload={"ended_at": schema.format_ts(T0 - timedelta(1)),
                           "duration_s": 0, "app": "x", "end_reason": "switch"})
    assert "ended_at anterieur a ts" in schema.validate_event(e, T0)


def test_intervalle_duree_incoherente_refusee():
    e = evenement(payload={"ended_at": schema.format_ts(T0 + timedelta(
        seconds=10)), "duration_s": 500, "app": "x", "end_reason": "switch"})
    assert any("incoherente" in m for m in schema.validate_event(e, T0))


def test_champ_requis_manquant():
    e = evenement(payload={"ended_at": schema.format_ts(T0), "duration_s": 0,
                           "end_reason": "switch"})
    assert "app_focus.app manquant" in schema.validate_event(e, T0)


def test_booleen_n_est_pas_un_nombre():
    e = evenement("input_activity", payload={
        "interval_s": 60, "keys": True, "clicks": 0, "scroll": 0,
        "active_s": 3})
    assert any("keys" in m for m in schema.validate_event(e, T0))


def test_enumeration_fermee():
    e = evenement("file_change", payload={"action": "read", "path": "~/x",
                                          "is_dir": False})
    assert any("hors de" in m for m in schema.validate_event(e, T0))


def test_champ_inconnu_tolere():
    """Ajouter un champ ne demande pas de nouvelle version du schema."""
    e = evenement()
    e["payload"]["nouveau_champ"] = 1
    assert schema.validate_event(e, T0) == []


def test_null_autorise_si_nullable():
    e = evenement("system_metrics", payload={"interval_s": 60,
                                             "cpu_temp_c": None,
                                             "battery_pct": None})
    assert schema.validate_event(e, T0) == []


def test_tous_les_types_ont_une_forme():
    formes = {s.forme for s in schema.EVENT_TYPES.values()}
    assert formes == {schema.INTERVAL, schema.POINT, schema.SAMPLE,
                      schema.SNAPSHOT}
    assert len(schema.EVENT_TYPES) == 21


def test_interval_end():
    e = evenement()
    assert schema.interval_end(e) == T0 + timedelta(seconds=312)
    point = evenement("app_launch", payload={"app": "vscode"})
    assert schema.interval_end(point) == T0
