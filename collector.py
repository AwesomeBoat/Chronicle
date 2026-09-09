"""Collecte automatisee : appeler Polar, archiver, ecrire en base. (V2)

Difference avec `fetch` (V0) : fetch affiche, collect ENREGISTRE. Et
surtout, collect est concu pour tourner sans personne devant l'ecran :
il journalise, il reessaie, il note ou il en est, et il rend un code de
sortie exploitable par une tache planifiee.

Pourquoi appeler client.get() directement plutot que les modules
polar/*.py : ces modules retirent les enveloppes ("nights", "recharges")
pour l'affichage. Ici on veut la reponse INTACTE, telle qu'elle doit etre
archivee puis traduite par polar/mapper.py.

Le partage des roles :
    collector.py   QUOI collecter, QUAND, et jusqu'ou (les curseurs)
    polar/mapper.py  COMMENT traduire ce qui a ete collecte
    database/       COMMENT l'ecrire sans dupliquer
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from database.connection import session_scope
from database.repository import (get_or_create_source, get_sync_state,
                                 insert_raw_payload, mark_sync_failure,
                                 mark_sync_success, store_batch, sync_metrics)
from logging_setup import get_logger
from polar.client import PolarClient
from polar.mapper import (METRICS, SOURCE_CODE, SOURCE_LABEL, UnmappedEndpoint,
                          map_envelope, normalize_endpoint, to_datetime)

logger = get_logger(__name__)

# Polar conserve environ 28 jours. Demander au-dela ne renvoie rien : la
# fenetre est donc bornee, ce n'est pas une limite qu'on s'impose.
RETENTION_DAYS = 28

# Chevauchement volontaire. Deux raisons de ne pas repartir exactement du
# curseur :
#   - une journee se complete apres coup (la nuit d'hier arrive le matin) ;
#   - une execution a pu tourner a 14h, laissant la fin de journee dehors.
# Redemander deux jours deja connus ne coute rien : l'ecriture est
# idempotente (V1). Les manquer coute la donnee, definitivement.
OVERLAP_DAYS = 2


@dataclass(slots=True)
class Endpoint:
    """Une chose a collecter."""

    label: str
    path: str                                  # chemin appele chez Polar
    windowed: bool = False                     # accepte from / to ?
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def cursor_key(self) -> str:
        """Clef du curseur : l'endpoint normalise, pas l'URL appelee.

        /users/64746286 et /users/{id} doivent partager le meme curseur.
        """
        return normalize_endpoint(self.path)


def build_plan(user_id: int) -> list[Endpoint]:
    """Le plan de collecte : tout ce que Polar sait donner.

    L'ordre compte un peu : le profil d'abord (leger, valide le token),
    les series lourdes ensuite.
    """
    return [
        Endpoint("Compte", f"/users/{user_id}"),
        Endpoint("Physiologie", "/users/physical-info"),
        Endpoint("Activite", "/users/activities", windowed=True,
                 # Sans ces trois options la reponse fait 1,8 Ko au lieu de
                 # 147 Ko : on n'archiverait que les totaux journaliers, et
                 # les series minute par minute seraient perdues avec le
                 # reste au bout de 28 jours.
                 params={"steps": True, "activity_zones": True,
                         "inactivity_stamps": True}),
        Endpoint("Sommeil", "/users/sleep"),
        Endpoint("Sommeil disponible", "/users/sleep/available"),
        Endpoint("SleepWise vigilance", "/users/sleepwise/alertness"),
        Endpoint("SleepWise coucher", "/users/sleepwise/circadian-bedtime"),
        Endpoint("Nightly Recharge", "/users/nightly-recharge"),
        Endpoint("Charge cardio", "/users/cardio-load"),
        Endpoint("FC continue", "/users/continuous-heart-rate", windowed=True),
        Endpoint("Entrainements", "/exercises"),
    ]


def compute_window(cursor_at: datetime | None,
                   today: date | None = None) -> tuple[str, str]:
    """Fenetre de dates a demander, sous forme (from, to) en YYYY-MM-DD.

    Trois cas :
      - jamais collecte      -> toute la retention (premier remplissage)
      - collecte recemment   -> depuis le curseur, moins le chevauchement
      - collecte il y a 3 mois -> borne a la retention, inutile d'aller
                                  au-dela, Polar a efface
    """
    today = today or date.today()
    plancher = today - timedelta(days=RETENTION_DAYS)

    if cursor_at is None:
        debut = plancher
    else:
        debut = max(cursor_at.date() - timedelta(days=OVERLAP_DAYS), plancher)

    return debut.isoformat(), today.isoformat()


def collect_endpoint(session, client: PolarClient, source_id: int,
                     metric_ids: dict[str, int], cible: Endpoint) -> dict:
    """Collecte un endpoint et l'ecrit en base. Renvoie un resume.

    Leve requests.HTTPError si Polar refuse : l'appelant decide si c'est
    fatal (401) ou seulement genant (503 sur une source).
    """
    etat = get_sync_state(session, source_id, cible.cursor_key)
    params = dict(cible.params)

    if cible.windowed:
        debut, fin = compute_window(etat.cursor_at)
        params["from"], params["to"] = debut, fin
        logger.info("%s : fenetre %s -> %s", cible.label, debut, fin)
    else:
        logger.info("%s : collecte complete", cible.label)

    data = client.get(cible.path, params or None)

    # 204 : succes, rien de nouveau. Le curseur avance quand meme - il n'y
    # a rien a rattraper.
    if data is None:
        mark_sync_success(session, source_id, cible.cursor_key,
                          cursor_at=datetime.now(timezone.utc))
        return {"vide": True}

    enveloppe = client.last_envelope

    insert_raw_payload(session, source_id,
                       endpoint=enveloppe["endpoint"],
                       params=enveloppe.get("params"),
                       fetched_at=to_datetime(enveloppe["fetched_at"]),
                       payload=enveloppe.get("data"),
                       filename=None)

    try:
        resume = store_batch(session, source_id, metric_ids,
                             map_envelope(enveloppe))

    except UnmappedEndpoint:
        # La reponse brute est deja en base : le parsing pourra etre
        # rejoue plus tard, sans redemander a Polar.
        logger.warning("%s : endpoint non mappe, reponse brute archivee",
                       cible.label)
        resume = {}

    mark_sync_success(session, source_id, cible.cursor_key,
                      cursor_at=datetime.now(timezone.utc))
    return resume


def collect_all(tokens: dict) -> int:
    """Collecte tout le plan. Renvoie le nombre d'endpoints en echec.

    Une source en echec n'interrompt pas les autres : Polar efface au bout
    de 28 jours, dix sources sur onze valent infiniment mieux que zero.
    L'exception est le 401 - token mort, tout le reste echouera pareil.
    """
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    with session_scope() as session:
        source_id = get_or_create_source(session, SOURCE_CODE, SOURCE_LABEL)
        metric_ids = sync_metrics(session, METRICS)

    plan = build_plan(tokens["x_user_id"])
    echecs = 0
    totaux = {"observations": 0, "episodes": 0, "snapshots": 0}

    logger.info("debut de collecte - %d endpoints", len(plan))

    for cible in plan:
        try:
            # Une transaction par endpoint : un echec sur la FC continue
            # ne doit pas annuler le sommeil deja ecrit.
            with session_scope() as session:
                resume = collect_endpoint(session, client, source_id,
                                          metric_ids, cible)

            totaux["observations"] += resume.get("observations_inserees", 0)
            totaux["episodes"] += resume.get("episodes_inseres", 0)
            totaux["snapshots"] += resume.get("snapshots_inseres", 0)

        except requests.HTTPError as error:
            statut = error.response.status_code

            if statut == 401:
                logger.critical(
                    "token invalide ou expire - collecte interrompue. "
                    "Relancer : python main.py auth")
                return len(plan)

            echecs += 1
            logger.error("%s : HTTP %d - source ignoree", cible.label, statut)

            with session_scope() as session:
                mark_sync_failure(session, source_id, cible.cursor_key,
                                  f"HTTP {statut}")

        except Exception as error:
            echecs += 1
            logger.exception("%s : %s", cible.label, type(error).__name__)

            with session_scope() as session:
                mark_sync_failure(session, source_id, cible.cursor_key,
                                  f"{type(error).__name__}: {error}")

    logger.info("collecte terminee - %d nouvelle(s) observation(s), "
                "%d episode(s), %d snapshot(s), %d echec(s)",
                totaux["observations"], totaux["episodes"],
                totaux["snapshots"], echecs)

    return echecs
