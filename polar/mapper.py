"""Traduit le JSON brut de Polar vers le modele generique de la base.

C'est ici, et NULLE PART AILLEURS, que vit la connaissance du format
Polar. Le repository ne connait que des Observation/Episode/Snapshot ;
ajouter une balance connectee en V4, ce sera ecrire un mapper voisin,
sans toucher ni au schema ni au repository.

Deux pieges permanents du format Polar :

1. Beaucoup d'horodatages sont livres SANS fuseau ("2026-08-16T22:56:30")
   ou sous forme d'heure d'horloge seule ("23:40"). Ce sont des heures
   locales de la montre. Sans fuseau declare (LOCAL_TZ), impossible d'en
   faire un instant absolu.

2. Certaines valeurs sont des sentinelles : cardio_load = -1.0 signifie
   "pas de donnee", pas "charge de -1". Les stocker empoisonnerait toute
   moyenne. Elles sont ecartees ici.
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from config import LOCAL_TZ
from database.records import (Batch, EpisodeRecord, MetricSpec,
                              ObservationRecord, SnapshotRecord)

SOURCE_CODE = "polar_accesslink"
SOURCE_LABEL = "Polar AccessLink"

LOCAL = ZoneInfo(LOCAL_TZ)

# Valeurs que Polar utilise pour dire "pas de donnee".
SENTINELS = {-1, -1.0}


class UnmappedEndpoint(Exception):
    """Endpoint sans regle de traduction : la reponse brute est archivee."""


# --------------------------------------------------------------- temps

def to_datetime(value: str) -> datetime:
    """Horodatage Polar -> datetime AVEC fuseau.

    Polar melange trois formes :
        2026-08-17T23:40:59.163+02:00   (complete, avec offset)
        2026-08-16T22:56:30             (naive = heure locale de la montre)
        2026-08-16T23:01                (naive, sans les secondes)
    fromisoformat gere les trois depuis Python 3.11 ; le fuseau local
    n'est ajoute que si la valeur n'en porte pas.
    """
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if moment.tzinfo is None:
        return moment.replace(tzinfo=LOCAL)

    return moment


def day_start(value: str | date) -> datetime:
    """Debut de journee locale ("2026-08-18" -> 2026-08-18T00:00+02:00).

    Sert d'horodatage aux mesures journalieres et nocturnes : une valeur
    par jour, donc un instant conventionnel et surtout REPRODUCTIBLE -
    c'est ce qui rend la clef d'unicite stable d'une collecte a l'autre.
    """
    jour = date.fromisoformat(value) if isinstance(value, str) else value
    return datetime.combine(jour, time.min, tzinfo=LOCAL)


_DUREE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def duration_seconds(value: str | None) -> float | None:
    """Duree ISO-8601 -> secondes. "PT13M" -> 780.0, "PT8H" -> 28800.0.

    Stockees en secondes plutot qu'en interval SQL : elles se moyennent
    et se tracent directement (cf. docs/SCHEMA.md).
    """
    if not value:
        return None

    trouve = _DUREE.fullmatch(value)

    if trouve is None:
        return None

    jours, heures, minutes, secondes = trouve.groups()

    return (int(jours or 0) * 86400 + int(heures or 0) * 3600
            + int(minutes or 0) * 60 + float(secondes or 0))


def clock_series(samples: dict[str, Any],
                 anchor: str | date) -> Iterator[tuple[datetime, Any]]:
    """Serie horodatee par heure d'horloge -> vrais instants.

    Polar livre le sommeil et la recharge nocturne ainsi :
        {"23:40": 0, "23:41": 3, "00:00": 0, "00:02": 3, ...}
    Le passage de minuit n'est marque nulle part. Deux regles suffisent :

    - point de depart : une heure >= 12:00 en tete de serie appartient au
      SOIR precedant la date de reference (Polar date une nuit par son
      jour de reveil) ;
    - ensuite, tout retour en arriere de l'horloge = un jour de plus.

    Les cles sont parcourues dans l'ordre du fichier : json.load preserve
    l'ordre d'insertion, c'est ce qui rend la reconstruction possible.
    """
    if not samples:
        return

    jour = date.fromisoformat(anchor) if isinstance(anchor, str) else anchor
    premiere = time.fromisoformat(next(iter(samples)))

    if premiere.hour >= 12:
        jour = jour - timedelta(days=1)

    precedente: time | None = None

    for horloge, valeur in samples.items():
        courante = time.fromisoformat(horloge)

        if precedente is not None and courante < precedente:
            jour = jour + timedelta(days=1)

        precedente = courante
        yield datetime.combine(jour, courante, tzinfo=LOCAL), valeur


def _clean(value: Any) -> float | None:
    """Ecarte None et les sentinelles Polar (-1)."""
    if value is None or isinstance(value, bool):
        return None

    if not isinstance(value, (int, float)):
        return None

    if value in SENTINELS:
        return None

    return float(value)


# ------------------------------------------------------------ catalogue

METRICS = [
    # Profil (change lentement, horodate par le champ "modified")
    MetricSpec("body.weight", "kg", "instant", "Poids declare ou pese"),
    MetricSpec("body.height", "cm", "instant", "Taille"),
    MetricSpec("body.vo2max", "ml/kg/min", "instant", "VO2max estimee"),
    MetricSpec("body.resting_heart_rate", "bpm", "instant", "FC de repos"),
    MetricSpec("body.maximum_heart_rate", "bpm", "instant", "FC maximale"),
    MetricSpec("body.aerobic_threshold", "bpm", "instant", "Seuil aerobie"),
    MetricSpec("body.anaerobic_threshold", "bpm", "instant", "Seuil anaerobie"),
    MetricSpec("body.sleep_goal", "s", "instant", "Objectif de sommeil"),

    # Activite : totaux journaliers
    MetricSpec("activity.steps", "count", "daily", "Pas sur la journee"),
    MetricSpec("activity.calories", "kcal", "daily", "Calories totales"),
    MetricSpec("activity.active_calories", "kcal", "daily", "Calories actives"),
    MetricSpec("activity.distance", "m", "daily", "Distance estimee (pas)"),
    MetricSpec("activity.goal_completion", "ratio", "daily",
               "Fraction de l'objectif d'activite atteinte"),
    MetricSpec("activity.active_duration", "s", "daily", "Temps actif"),
    MetricSpec("activity.inactive_duration", "s", "daily", "Temps inactif"),
    MetricSpec("activity.inactivity_alerts", "count", "daily",
               "Alertes d'inactivite"),

    # Series fines
    MetricSpec("activity.steps_interval", "count", "instant",
               "Pas sur un intervalle d'echantillonnage (1 min)"),
    MetricSpec("heart_rate", "bpm", "instant",
               "Frequence cardiaque continue (24h)"),

    # Sommeil : une valeur par nuit, horodatee au debut du jour de reveil
    MetricSpec("sleep.score", "index", "nightly", "Score de sommeil /100"),
    MetricSpec("sleep.continuity", "index", "nightly", "Continuite 1-5"),
    MetricSpec("sleep.duration_light", "s", "nightly", "Sommeil leger"),
    MetricSpec("sleep.duration_deep", "s", "nightly", "Sommeil profond"),
    MetricSpec("sleep.duration_rem", "s", "nightly", "Sommeil paradoxal"),
    MetricSpec("sleep.duration_unknown", "s", "nightly", "Stade non reconnu"),
    MetricSpec("sleep.interruptions_total", "s", "nightly",
               "Duree totale des interruptions"),
    MetricSpec("sleep.interruptions_short", "s", "nightly",
               "Interruptions courtes"),
    MetricSpec("sleep.interruptions_long", "s", "nightly",
               "Interruptions longues"),
    MetricSpec("sleep.charge", "index", "nightly", "Sleep charge"),
    MetricSpec("sleep.rating", "index", "nightly", "Ressenti declare"),
    MetricSpec("sleep.goal", "s", "nightly", "Objectif de la nuit"),
    MetricSpec("sleep.cycles", "count", "nightly", "Cycles de sommeil"),
    MetricSpec("sleep.score_duration", "index", "nightly", "Sous-score duree"),
    MetricSpec("sleep.score_solidity", "index", "nightly", "Sous-score solidite"),
    MetricSpec("sleep.score_regeneration", "index", "nightly",
               "Sous-score regeneration"),
    # Codes 0, 1, 3, 4 et 5 observes dans les donnees reelles ; la
    # correspondance exacte n'est pas documentee cote Polar - a verifier
    # avant toute analyse par stade.
    MetricSpec("sleep.stage", "code", "instant",
               "Stade d'hypnogramme, code Polar brut"),
    MetricSpec("sleep.heart_rate", "bpm", "instant", "FC pendant le sommeil"),

    # Nightly Recharge
    MetricSpec("recharge.heart_rate_avg", "bpm", "nightly", "FC nocturne moyenne"),
    MetricSpec("recharge.beat_to_beat_avg", "ms", "nightly",
               "Intervalle battement a battement moyen"),
    MetricSpec("recharge.hrv_avg", "ms", "nightly", "HRV nocturne moyenne"),
    MetricSpec("recharge.breathing_rate_avg", "resp/min", "nightly",
               "Frequence respiratoire moyenne"),
    MetricSpec("recharge.hrv", "ms", "instant", "HRV echantillonnee"),
    MetricSpec("recharge.breathing_rate", "resp/min", "instant",
               "Frequence respiratoire echantillonnee"),

    # Charge cardio
    MetricSpec("cardio.load", "index", "daily", "Charge cardio du jour"),
    MetricSpec("cardio.strain", "index", "daily", "Contrainte (charge court terme)"),
    MetricSpec("cardio.tolerance", "index", "daily", "Tolerance (long terme)"),
    MetricSpec("cardio.load_ratio", "ratio", "daily", "strain / tolerance"),

    # SleepWise
    MetricSpec("sleepwise.alertness_grade", "index", "period",
               "Note de vigilance predite"),

    # Entrainements
    MetricSpec("exercise.duration", "s", "period", "Duree de la seance"),
    MetricSpec("exercise.distance", "m", "period", "Distance de la seance"),
    MetricSpec("exercise.calories", "kcal", "period", "Calories de la seance"),
    MetricSpec("exercise.heart_rate_avg", "bpm", "period", "FC moyenne"),
    MetricSpec("exercise.heart_rate_max", "bpm", "period", "FC maximale"),
]


# ------------------------------------------------------- traducteurs

def _account(data: dict, fetched_at: datetime) -> Batch:
    """/users/{id} - identite du compte.

    Aucune observation : le poids et la taille de ce payload font double
    emploi avec /users/physical-info, qui les horodate correctement via
    son champ "modified". Deux sources pour une meme mesure creeraient
    des valeurs contradictoires au meme instant.
    """
    return Batch(snapshots=[SnapshotRecord("polar_account", fetched_at, data)])


def _physical_info(data: dict, fetched_at: datetime) -> Batch:
    """/users/physical-info - profil physiologique.

    PIEGE : cet endpoint renvoie un OBJET, pas une liste (contrairement a
    la plupart des autres). Et la cle est "vo2_max" avec un underscore,
    pas "vo2-max" comme le laisse croire le reste de l'API.
    """
    batch = Batch()

    # "modified" plutot que la date de collecte : une valeur inchangee
    # garde le meme horodatage d'une collecte a l'autre, donc la meme
    # clef d'unicite. C'est ce qui rend l'operation idempotente.
    mesure_at = to_datetime(data["modified"]) if data.get("modified") else fetched_at

    champs = {
        "weight": "body.weight",
        "height": "body.height",
        "vo2_max": "body.vo2max",
        "resting_heart_rate": "body.resting_heart_rate",
        "maximum_heart_rate": "body.maximum_heart_rate",
        "aerobic_threshold": "body.aerobic_threshold",
        "anaerobic_threshold": "body.anaerobic_threshold",
    }

    for cle, metrique in champs.items():
        valeur = _clean(data.get(cle))

        if valeur is not None:
            batch.observations.append(
                ObservationRecord(metrique, mesure_at, valeur))

    objectif = duration_seconds(data.get("sleep_goal"))

    if objectif is not None:
        batch.observations.append(
            ObservationRecord("body.sleep_goal", mesure_at, objectif))

    batch.snapshots.append(SnapshotRecord("polar_physical", mesure_at, data))

    return batch


def _activities(data: list, fetched_at: datetime) -> Batch:
    """/users/activities - periodes d'activite, totaux et echantillons."""
    batch = Batch()

    for periode in data:
        echantillons = periode.get("samples") or {}
        jour = echantillons.get("date") or periode["start_time"][:10]
        debut_jour = day_start(jour)

        # L'episode garde la periode brute, sans les series : celles-ci
        # partent en observations, ou elles sont exploitables.
        entete = {cle: valeur for cle, valeur in periode.items()
                  if cle != "samples"}

        batch.episodes.append(EpisodeRecord(
            kind="activity_period",
            started_at=to_datetime(periode["start_time"]),
            ended_at=to_datetime(periode["end_time"]) if periode.get("end_time") else None,
            payload=entete))

        totaux = {
            "activity.steps": periode.get("steps"),
            "activity.calories": periode.get("calories"),
            "activity.active_calories": periode.get("active_calories"),
            "activity.distance": periode.get("distance_from_steps"),
            "activity.goal_completion": periode.get("daily_activity"),
            "activity.inactivity_alerts": periode.get("inactivity_alert_count"),
            "activity.active_duration": duration_seconds(periode.get("active_duration")),
            "activity.inactive_duration": duration_seconds(periode.get("inactive_duration")),
        }

        for metrique, brut in totaux.items():
            valeur = _clean(brut)

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord(metrique, debut_jour, valeur))

        # Pas minute par minute.
        for mesure in (echantillons.get("steps") or {}).get("samples", []):
            valeur = _clean(mesure.get("steps"))

            if valeur is not None and mesure.get("timestamp"):
                batch.observations.append(ObservationRecord(
                    "activity.steps_interval",
                    to_datetime(mesure["timestamp"]), valeur))

        # Zones d'activite : une valeur TEXTUELLE (SEDENTARY, LIGHT...)
        # qui vaut jusqu'a l'echantillon suivant. C'est un intervalle,
        # pas un point : donc un episode, pas une observation.
        zones = (echantillons.get("activity_zones") or {}).get("samples", [])

        for indice, mesure in enumerate(zones):
            if not mesure.get("timestamp"):
                continue

            suivant = zones[indice + 1] if indice + 1 < len(zones) else None
            fin = (to_datetime(suivant["timestamp"]) if suivant
                   else to_datetime(periode["end_time"]) if periode.get("end_time")
                   else None)

            batch.episodes.append(EpisodeRecord(
                kind="activity_zone",
                started_at=to_datetime(mesure["timestamp"]),
                ended_at=fin,
                payload={"zone": mesure.get("zone")}))

        for mesure in (echantillons.get("inactivity_stamps") or {}).get("samples", []):
            if mesure.get("timestamp"):
                batch.episodes.append(EpisodeRecord(
                    kind="inactivity_stamp",
                    started_at=to_datetime(mesure["timestamp"]),
                    payload=mesure))

    return batch


SLEEP_FIELDS = {
    "sleep_score": "sleep.score",
    "continuity": "sleep.continuity",
    "light_sleep": "sleep.duration_light",
    "deep_sleep": "sleep.duration_deep",
    "rem_sleep": "sleep.duration_rem",
    "unrecognized_sleep_stage": "sleep.duration_unknown",
    "total_interruption_duration": "sleep.interruptions_total",
    "short_interruption_duration": "sleep.interruptions_short",
    "long_interruption_duration": "sleep.interruptions_long",
    "sleep_charge": "sleep.charge",
    "sleep_rating": "sleep.rating",
    "sleep_goal": "sleep.goal",
    "sleep_cycles": "sleep.cycles",
    "group_duration_score": "sleep.score_duration",
    "group_solidity_score": "sleep.score_solidity",
    "group_regeneration_score": "sleep.score_regeneration",
}


def _sleep(data: dict, fetched_at: datetime) -> Batch:
    """/users/sleep - nuits completes avec hypnogramme."""
    batch = Batch()

    for nuit in data.get("nights", []):
        jour = nuit["date"]
        debut_jour = day_start(jour)

        entete = {cle: valeur for cle, valeur in nuit.items()
                  if cle not in ("hypnogram", "heart_rate_samples")}

        batch.episodes.append(EpisodeRecord(
            kind="sleep",
            started_at=to_datetime(nuit["sleep_start_time"]),
            ended_at=to_datetime(nuit["sleep_end_time"]) if nuit.get("sleep_end_time") else None,
            payload=entete))

        for cle, metrique in SLEEP_FIELDS.items():
            valeur = _clean(nuit.get(cle))

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord(metrique, debut_jour, valeur))

        # Hypnogramme : le stade est deja code en entier (0 eveil, 1 REM,
        # 3 leger, 4 profond) - donc numerique, donc une observation.
        for moment, stade in clock_series(nuit.get("hypnogram") or {}, jour):
            valeur = _clean(stade)

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord("sleep.stage", moment, valeur))

        for moment, fc in clock_series(nuit.get("heart_rate_samples") or {}, jour):
            valeur = _clean(fc)

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord("sleep.heart_rate", moment, valeur))

    return batch


def _sleep_available(data: dict, fetched_at: datetime) -> Batch:
    """/users/sleep/available - ce que Polar detient encore.

    Utile en V2 : c'est la fenetre de retention (~28 jours) qui dit ce
    qu'il reste a rapatrier avant effacement definitif.
    """
    return Batch(episodes=[
        EpisodeRecord(kind="sleep_availability",
                      started_at=to_datetime(nuit["start_time"]),
                      ended_at=to_datetime(nuit["end_time"]) if nuit.get("end_time") else None,
                      payload=nuit)
        for nuit in data.get("available", []) if nuit.get("start_time")])


def _nightly_recharge(data: dict, fetched_at: datetime) -> Batch:
    """/users/nightly-recharge - recuperation nocturne (HRV, respiration)."""
    batch = Batch()

    moyennes = {
        "heart_rate_avg": "recharge.heart_rate_avg",
        "beat_to_beat_avg": "recharge.beat_to_beat_avg",
        "heart_rate_variability_avg": "recharge.hrv_avg",
        "breathing_rate_avg": "recharge.breathing_rate_avg",
    }

    for recharge in data.get("recharges", []):
        jour = recharge["date"]
        debut_jour = day_start(jour)

        for cle, metrique in moyennes.items():
            valeur = _clean(recharge.get(cle))

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord(metrique, debut_jour, valeur))

        entete = {cle: valeur for cle, valeur in recharge.items()
                  if not cle.endswith("samples")}

        batch.episodes.append(EpisodeRecord(
            kind="nightly_recharge", started_at=debut_jour, payload=entete))

        # Ces series n'ont pas de sleep_start_time pour se caler : la
        # reconstruction repose entierement sur la regle "avant midi =
        # jour de reveil, apres midi = veille au soir".
        for moment, hrv in clock_series(recharge.get("hrv_samples") or {}, jour):
            valeur = _clean(hrv)

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord("recharge.hrv", moment, valeur))

        for moment, respiration in clock_series(
                recharge.get("breathing_samples") or {}, jour):
            valeur = _clean(respiration)

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord("recharge.breathing_rate", moment, valeur))

    return batch


def _cardio_load(data: list, fetched_at: datetime) -> Batch:
    """/users/cardio-load - charge, contrainte, tolerance.

    PIEGE : quand cardio_load_status vaut LOAD_STATUS_NOT_AVAILABLE, les
    valeurs sont a -1.0. Ce n'est pas une mesure, c'est un trou. _clean
    les ecarte ; le status reste dans le payload de l'episode.
    """
    batch = Batch()

    champs = {"cardio_load": "cardio.load",
              "strain": "cardio.strain",
              "tolerance": "cardio.tolerance",
              "cardio_load_ratio": "cardio.load_ratio"}

    for jour_charge in data:
        debut_jour = day_start(jour_charge["date"])

        for cle, metrique in champs.items():
            valeur = _clean(jour_charge.get(cle))

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord(metrique, debut_jour, valeur))

        batch.episodes.append(EpisodeRecord(
            kind="cardio_load_day", started_at=debut_jour,
            payload=jour_charge))

    return batch


def _continuous_heart_rate(data: Any, fetched_at: datetime) -> Batch:
    """/users/continuous-heart-rate - FC toutes les ~5 minutes, 24h/24.

    Deux formes selon l'endpoint appele : une liste sous "heart_rates"
    pour une plage, un objet seul pour une date precise.
    """
    batch = Batch()

    jours = data.get("heart_rates", []) if isinstance(data, dict) else data

    if isinstance(jours, dict):
        jours = [jours]

    for jour_data in jours:
        jour = date.fromisoformat(jour_data["date"])

        for mesure in jour_data.get("heart_rate_samples", []):
            valeur = _clean(mesure.get("heart_rate"))

            if valeur is None or not mesure.get("sample_time"):
                continue

            # sample_time est une heure seule ("00:04:03") relative a la
            # date du bloc : pas de passage de minuit a gerer ici.
            horloge = time.fromisoformat(mesure["sample_time"])

            batch.observations.append(ObservationRecord(
                "heart_rate",
                datetime.combine(jour, horloge, tzinfo=LOCAL), valeur))

    return batch


def _alertness(data: list, fetched_at: datetime) -> Batch:
    """/users/sleepwise/alertness - vigilance predite heure par heure."""
    batch = Batch()

    for prediction in data:
        debut = to_datetime(prediction["period_start_time"])
        entete = {cle: valeur for cle, valeur in prediction.items()
                  if cle != "hourly_data"}

        batch.episodes.append(EpisodeRecord(
            kind="alertness_period",
            started_at=debut,
            ended_at=to_datetime(prediction["period_end_time"]) if prediction.get("period_end_time") else None,
            payload=entete))

        note = _clean(prediction.get("grade"))

        if note is not None:
            batch.observations.append(
                ObservationRecord("sleepwise.alertness_grade", debut, note))

        # alertness_level est textuel (ALERTNESS_LEVEL_LOW/HIGH) et vaut
        # sur un intervalle : episode, pas observation.
        for heure in prediction.get("hourly_data", []):
            if not heure.get("start_time"):
                continue

            batch.episodes.append(EpisodeRecord(
                kind="alertness_hour",
                started_at=to_datetime(heure["start_time"]),
                ended_at=to_datetime(heure["end_time"]) if heure.get("end_time") else None,
                payload=heure))

    return batch


def _circadian_bedtime(data: list, fetched_at: datetime) -> Batch:
    """/users/sleepwise/circadian-bedtime - fenetre de coucher conseillee."""
    return Batch(episodes=[
        EpisodeRecord(kind="circadian_bedtime",
                      started_at=to_datetime(prediction["period_start_time"]),
                      ended_at=to_datetime(prediction["period_end_time"]) if prediction.get("period_end_time") else None,
                      payload=prediction)
        for prediction in data if prediction.get("period_start_time")])


def _exercises(data: list, fetched_at: datetime) -> Batch:
    """/exercises - seances d'entrainement.

    Aucune seance dans les donnees collectees a ce jour : ce traducteur
    n'a jamais tourne sur du reel. Il extrait defensivement les champs
    documentes, chacun optionnel.
    """
    batch = Batch()

    champs = {"distance": "exercise.distance",
              "calories": "exercise.calories",
              "heart_rate_avg": "exercise.heart_rate_avg",
              "heart_rate_max": "exercise.heart_rate_max"}

    for seance in data:
        if not seance.get("start_time"):
            continue

        debut = to_datetime(seance["start_time"])
        duree = duration_seconds(seance.get("duration"))

        batch.episodes.append(EpisodeRecord(
            kind="exercise",
            started_at=debut,
            ended_at=debut + timedelta(seconds=duree) if duree else None,
            payload=seance))

        if duree is not None:
            batch.observations.append(
                ObservationRecord("exercise.duration", debut, duree))

        for cle, metrique in champs.items():
            valeur = _clean(seance.get(cle))

            if valeur is not None:
                batch.observations.append(
                    ObservationRecord(metrique, debut, valeur))

    return batch


# -------------------------------------------------------------- routage

# La cle est l'endpoint normalise : /users/64746286 -> /users/{id}, et
# /users/continuous-heart-rate/2026-08-22 -> /users/continuous-heart-rate.
HANDLERS = {
    "/users/{id}": _account,
    "/users/physical-info": _physical_info,
    "/users/activities": _activities,
    "/users/sleep": _sleep,
    "/users/sleep/available": _sleep_available,
    "/users/nightly-recharge": _nightly_recharge,
    "/users/cardio-load": _cardio_load,
    "/users/continuous-heart-rate": _continuous_heart_rate,
    "/users/sleepwise/alertness": _alertness,
    "/users/sleepwise/circadian-bedtime": _circadian_bedtime,
    "/exercises": _exercises,
}


def normalize_endpoint(endpoint: str) -> str:
    """Remplace les parties variables par un motif fixe."""
    endpoint = endpoint.rstrip("/")
    endpoint = re.sub(r"^/users/\d+$", "/users/{id}", endpoint)
    endpoint = re.sub(r"^/users/continuous-heart-rate/[\d-]+$",
                      "/users/continuous-heart-rate", endpoint)
    return endpoint


def map_envelope(envelope: dict) -> Batch:
    """Enveloppe de data/raw/ -> Batch pret pour le repository.

    L'enveloppe est celle ecrite par PolarClient._archive :
        {"endpoint", "params", "fetched_at", "data"}

    Leve UnmappedEndpoint si aucun traducteur ne correspond : la reponse
    brute reste archivee en base, le parsing pourra etre rejoue plus tard.
    """
    endpoint = normalize_endpoint(envelope["endpoint"])
    handler = HANDLERS.get(endpoint)

    if handler is None:
        raise UnmappedEndpoint(endpoint)

    data = envelope.get("data")

    if data is None:
        return Batch()

    return handler(data, to_datetime(envelope["fetched_at"]))
