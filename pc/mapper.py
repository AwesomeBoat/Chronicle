"""Connecteur PC : evenements du schema commun -> Batch. (cinquieme source)

Comme polar/mapper.py, food/mapper.py, sensors/bedroom.py et
kindle/mapper.py, ce module ne connait ni SQLAlchemy ni les tables : il
produit des Batch. `python main.py sources` le verifie.

UNE SOURCE PAR MACHINE
----------------------
La cle d'unicite d'`observation` est (source, metric, observed_at). Deux
PC qui mesurent pc.cpu_pct a la meme seconde entreraient en collision
dans une source unique. Chaque machine est donc sa propre ligne `source` :

    pc:windows-main       PC windows-main
    pc:omarchy-desktop    PC omarchy-desktop

SOURCE_CODE ("pc") nomme le connecteur ; source_code(device_id) nomme la
ligne en base. Le catalogue `metric`, lui, est partage : pc.cpu_pct est la
meme grandeur sur toutes les machines, ce qui permet de les comparer.

CE QUI VA OU
------------
    intervalles, points  -> episode   (kind = event_type)
    system_metrics       -> observation pc.*
    input_activity       -> observation pc.input.*
    device_info          -> profile_snapshot "pc_device"

Seules les grandeurs NUMERIQUES et COMPARABLES deviennent des
observations. "Sur secteur" (un booleen) reste dans le brut : une
observation constante pendant des semaines serait signalee a tort comme
un capteur mort par `python main.py quality`.

Les noms d'episodes evitent ceux des autres sources : `system_sleep` et
non `sleep`, que v_sleep et v_chambre lisent sans filtrer la source.
"""

from __future__ import annotations

from datetime import datetime

from database.records import (Batch, EpisodeRecord, MetricSpec,
                              ObservationRecord, SnapshotRecord)
from pc import schema

SOURCE_CODE = "pc"
SOURCE_LABEL = "Activite PC (une source par machine)"

# Le flux au sens de raw_payload : un lot recu par l'API.
ENDPOINT = "pc/events"


def source_code(device_id: str) -> str:
    return f"pc:{device_id}"


def source_label(device_id: str) -> str:
    return f"PC {device_id}"


def sync_endpoint(device_id: str) -> str:
    """Le curseur sync_state d'une machine : visible dans `health`."""
    return f"pc/{device_id}"


# ------------------------------------------------------------ catalogue

# champ du payload -> (metrique, unite, granularite, description)
SYSTEM_METRICS = {
    "cpu_pct": ("pc.cpu_pct", "%", "instant", "Utilisation CPU sur l'intervalle"),
    "ram_pct": ("pc.ram_pct", "%", "instant", "Memoire vive utilisee"),
    "ram_used_mb": ("pc.ram_used_mb", "MB", "instant", "Memoire vive utilisee"),
    "swap_pct": ("pc.swap_pct", "%", "instant", "Fichier d'echange utilise"),
    "gpu_pct": ("pc.gpu_pct", "%", "instant",
                "Utilisation GPU (moteur le plus charge)"),
    "vram_used_mb": ("pc.vram_used_mb", "MB", "instant",
                     "Memoire video dediee utilisee"),
    "cpu_temp_c": ("pc.cpu_temp_c", "degC", "instant", "Temperature CPU"),
    "disk_used_pct": ("pc.disk_used_pct", "%", "instant",
                      "Occupation du disque systeme"),
    "disk_read_bytes": ("pc.disk_read_bytes", "B", "period",
                        "Octets lus sur l'intervalle"),
    "disk_write_bytes": ("pc.disk_write_bytes", "B", "period",
                         "Octets ecrits sur l'intervalle"),
    "net_up_bytes": ("pc.net_up_bytes", "B", "period",
                     "Octets envoyes sur l'intervalle (interfaces reelles)"),
    "net_down_bytes": ("pc.net_down_bytes", "B", "period",
                       "Octets recus sur l'intervalle (interfaces reelles)"),
    "battery_pct": ("pc.battery_pct", "%", "instant", "Charge de la batterie"),
}

INPUT_METRICS = {
    "keys": ("pc.input.keys", "count", "period",
             "Touches enfoncees sur la minute (jamais lesquelles)"),
    "clicks": ("pc.input.clicks", "count", "period",
               "Clics souris sur la minute"),
    "scroll": ("pc.input.scroll", "count", "period",
               "Crans de molette sur la minute"),
    "mouse_distance": ("pc.input.mouse_distance", "units", "period",
                       "Deplacement souris (unites du peripherique)"),
    "active_s": ("pc.input.active_s", "s", "period",
                 "Secondes de la minute avec au moins une entree"),
}

METRICS = [MetricSpec(code, unite, granularite, description)
           for code, unite, granularite, description in
           list(SYSTEM_METRICS.values()) + list(INPUT_METRICS.values())]

SNAPSHOT_KIND = "pc_device"

# Metadonnees de l'enveloppe conservees dans chaque episode : de quoi
# retrouver l'evenement d'origine et juger de sa qualite.
_ENVELOPPE = ("event_id", "producer", "device_id", "ts_local", "tz",
              "schema_v")


# ------------------------------------------------------------ traduction

def _nombre(valeur) -> float | None:
    if valeur is None or isinstance(valeur, bool):
        return None

    if isinstance(valeur, (int, float)) and valeur == valeur:   # pas NaN
        return float(valeur)

    return None


def map_event(event: dict) -> Batch:
    """Un evenement VALIDE -> Batch. (Valider d'abord : validate_event.)"""
    lot = Batch()
    type_ = event["event_type"]
    spec = schema.EVENT_TYPES[type_]
    debut: datetime = schema.parse_ts(event["ts"])
    payload = event.get("payload") or {}

    if spec.forme == schema.SAMPLE:
        table = SYSTEM_METRICS if type_ == "system_metrics" else INPUT_METRICS

        for champ, (code, *_reste) in table.items():
            valeur = _nombre(payload.get(champ))

            # Une valeur inconnue (null) n'est pas ecrite : un zero
            # invente est le piege cardio.strain de la V3.
            if valeur is not None:
                lot.observations.append(ObservationRecord(code, debut, valeur))

        return lot

    meta = {cle: event.get(cle) for cle in _ENVELOPPE}

    if spec.forme == schema.SNAPSHOT:
        lot.snapshots.append(SnapshotRecord(
            kind=SNAPSHOT_KIND, captured_at=debut,
            payload={**payload, "device_id": event["device_id"]}))
        return lot

    fin = schema.parse_ts(payload["ended_at"]) \
        if spec.forme == schema.INTERVAL else debut

    lot.episodes.append(EpisodeRecord(
        kind=type_, started_at=debut, ended_at=fin,
        payload={**payload, **meta}))

    return lot


def map_events(events: list, device_id: str,
               now: datetime | None = None) -> tuple[Batch, list[dict]]:
    """Traduit un lot. Renvoie (batch, refus).

    Un evenement invalide est REFUSE, pas corrige : il est signale a
    l'appelant (qui l'a deja archive en brut) et le reste du lot passe.
    Meme regle que le journal alimentaire : ce qui n'est pas compris avec
    certitude est refuse, jamais devine.
    """
    lot = Batch()
    refus: list[dict] = []

    for event in events:
        identifiant = event.get("event_id") if isinstance(event, dict) \
            else None
        erreurs = schema.validate_event(event, now)

        if not erreurs and event.get("device_id") != device_id:
            erreurs = [f"device_id {event.get('device_id')!r} different de "
                       f"celui du lot ({device_id!r})"]

        if erreurs:
            refus.append({"event_id": identifiant,
                          "event_type": event.get("event_type")
                          if isinstance(event, dict) else None,
                          "errors": erreurs[:5]})
            continue

        lot.extend(map_event(event))

    return lot, refus


def cursor(events: list) -> datetime | None:
    """La fin la plus recente du lot, pour avancer sync_state."""
    fins = []

    for event in events:
        try:
            fins.append(schema.interval_end(event))
        except (KeyError, ValueError, TypeError):
            continue

    fins = [f for f in fins if f is not None]
    return max(fins) if fins else None
