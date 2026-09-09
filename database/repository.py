"""Ecriture en base : insertion IDEMPOTENTE.

Idempotent = rejouer la meme collecte dix fois donne exactement le meme
etat de base qu'une seule fois. C'est la propriete centrale de la V1 :
sans elle, une tache planifiee qui se relance (V2) duplique tout.

Mecanisme : INSERT ... ON CONFLICT. PostgreSQL detecte la violation d'une
contrainte UNIQUE au moment de l'insertion et, au lieu de lever une
erreur, applique la strategie demandee :

    ON CONFLICT DO NOTHING  -> la ligne existante est laissee intacte
    ON CONFLICT DO UPDATE   -> la ligne existante est mise a jour (upsert)

Pourquoi pas "SELECT puis INSERT si absent" ? Parce que c'est faux des
qu'il y a deux ecrivains (condition de course entre le SELECT et
l'INSERT) et surtout lent : une requete par ligne au lieu d'une par lot.
ON CONFLICT est atomique et se fait en une seule requete.

Choix par table :
  observation  DO UPDATE : Polar revise ses scores de sommeil apres coup.
                           La derniere valeur connue gagne.
  episode      DO UPDATE : idem, un episode peut etre complete plus tard.
  snapshot     DO NOTHING : dedoublonne par hash de contenu, donc un
                           conflit signifie "rien n'a change".
  raw_payload  DO NOTHING : une reponse d'API archivee est immuable.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models import (Episode, Metric, Observation, ProfileSnapshot,
                             RawPayload, Source, SyncState)
from database.records import Batch, MetricSpec

# Les lignes partent par paquets : une requete de 500 000 valeurs
# saturerait la memoire du serveur, une requete par ligne serait 1000 fois
# plus lente. 1000 est un compromis courant.
CHUNK_SIZE = 1000


def content_hash(payload: Any) -> str:
    """SHA-256 d'un objet JSON, stable quel que soit l'ordre des cles.

    sort_keys=True est indispensable : sans lui, deux dictionnaires
    identiques mais construits dans un ordre different donneraient deux
    empreintes differentes, et le dedoublonnage par contenu ne servirait
    a rien.
    """
    texte = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def _chunks(rows: Sequence[dict], size: int = CHUNK_SIZE) -> Iterable[list[dict]]:
    for start in range(0, len(rows), size):
        yield list(rows[start:start + size])


def _dedupe(rows: Sequence[dict], keys: Sequence[str]) -> list[dict]:
    """Garde la derniere ligne pour chaque clef de conflit.

    PIEGE : PostgreSQL refuse un INSERT ... ON CONFLICT DO UPDATE qui
    toucherait deux fois la meme ligne dans la meme commande :
        "ON CONFLICT DO UPDATE command cannot affect row a second time"
    Or les donnees Polar contiennent bel et bien des doublons (deux
    collectes qui se recouvrent, deux echantillons au meme horodatage).
    Le dedoublonnage doit donc etre fait en Python, avant l'envoi.
    """
    unique: dict[tuple, dict] = {}

    for row in rows:
        unique[tuple(row[key] for key in keys)] = row

    return list(unique.values())


# ------------------------------------------------------------ registres

def get_or_create_source(session: Session, code: str, label: str) -> int:
    """Renvoie l'id de la source, en la creant au besoin."""
    source_id = session.scalar(select(Source.id).where(Source.code == code))

    if source_id is not None:
        return source_id

    session.execute(
        insert(Source)
        .values(code=code, label=label)
        .on_conflict_do_nothing(index_elements=["code"]))
    session.flush()

    return session.scalar(select(Source.id).where(Source.code == code))


def sync_metrics(session: Session, specs: Iterable[MetricSpec]) -> dict[str, int]:
    """Cree les metriques manquantes et renvoie {code: id}.

    Appele une fois par execution : le catalogue tient en quelques
    dizaines de lignes, on le charge en memoire plutot que de faire un
    SELECT par observation.
    """
    rows = [{"code": spec.code,
             "unit": spec.unit,
             "granularity": spec.granularity,
             "description": spec.description}
            for spec in specs]

    if rows:
        # DO UPDATE et non DO NOTHING : le catalogue appartient au code.
        # Corriger une unite ou une description dans METRICS doit se
        # propager en base au prochain passage.
        statement = insert(Metric).values(_dedupe(rows, ["code"]))
        session.execute(statement.on_conflict_do_update(
            index_elements=["code"],
            set_={"unit": statement.excluded.unit,
                  "granularity": statement.excluded.granularity,
                  "description": statement.excluded.description}))
        session.flush()

    return dict(session.execute(select(Metric.code, Metric.id)).all())


# ------------------------------------------------------------- ecriture

def upsert_observations(session: Session, source_id: int,
                        metric_ids: dict[str, int],
                        records: Sequence) -> tuple[int, int]:
    """Insere ou met a jour des mesures. Renvoie (inserees, mises a jour).

    Le comptage utilise une astuce PostgreSQL : la colonne systeme xmax
    vaut 0 sur une ligne reellement inseree, et porte l'identifiant de
    transaction sur une ligne mise a jour par le ON CONFLICT. RETURNING
    (xmax = 0) distingue donc les deux cas sans requete supplementaire.
    """
    rows = []

    for record in records:
        metric_id = metric_ids.get(record.metric_code)

        if metric_id is None:
            raise KeyError(f"metrique inconnue : {record.metric_code}")

        rows.append({"source_id": source_id,
                     "metric_id": metric_id,
                     "observed_at": record.observed_at,
                     "value": float(record.value)})

    rows = _dedupe(rows, ["source_id", "metric_id", "observed_at"])
    inserted = updated = 0

    for chunk in _chunks(rows):
        statement = insert(Observation).values(chunk)
        statement = statement.on_conflict_do_update(
            constraint="uq_observation_point",
            set_={"value": statement.excluded.value,
                  "ingested_at": func.now()},
        ).returning(literal_column("(xmax = 0)").label("inseree"))

        for (was_inserted,) in session.execute(statement).all():
            if was_inserted:
                inserted += 1
            else:
                updated += 1

    return inserted, updated


def upsert_episodes(session: Session, source_id: int,
                    records: Sequence) -> tuple[int, int]:
    """Insere ou met a jour des episodes. Renvoie (inseres, mis a jour)."""
    rows = [{"source_id": source_id,
             "kind": record.kind,
             "started_at": record.started_at,
             "ended_at": record.ended_at,
             "payload": record.payload}
            for record in records]

    rows = _dedupe(rows, ["source_id", "kind", "started_at"])
    inserted = updated = 0

    for chunk in _chunks(rows):
        statement = insert(Episode).values(chunk)
        statement = statement.on_conflict_do_update(
            constraint="uq_episode_start",
            set_={"ended_at": statement.excluded.ended_at,
                  "payload": statement.excluded.payload,
                  "ingested_at": func.now()},
        ).returning(literal_column("(xmax = 0)").label("inseree"))

        for (was_inserted,) in session.execute(statement).all():
            if was_inserted:
                inserted += 1
            else:
                updated += 1

    return inserted, updated


def insert_snapshots(session: Session, source_id: int,
                     records: Sequence) -> int:
    """Insere les etats de profil nouveaux. Renvoie le nombre d'ajouts.

    DO NOTHING : un conflit sur (source, kind, content_hash) signifie que
    ce profil exact est deja connu. Rien a mettre a jour.
    """
    rows = [{"source_id": source_id,
             "kind": record.kind,
             "captured_at": record.captured_at,
             "content_hash": content_hash(record.payload),
             "payload": record.payload}
            for record in records]

    rows = _dedupe(rows, ["source_id", "kind", "content_hash"])
    inserted = 0

    for chunk in _chunks(rows):
        statement = (insert(ProfileSnapshot)
                     .values(chunk)
                     .on_conflict_do_nothing(constraint="uq_snapshot_content")
                     .returning(ProfileSnapshot.id))

        # RETURNING ne renvoie que les lignes reellement inserees :
        # compter les resultats suffit ici.
        inserted += len(session.execute(statement).all())

    return inserted


def insert_raw_payload(session: Session, source_id: int, endpoint: str,
                       params: dict | None, fetched_at: datetime,
                       payload: Any, filename: str | None = None) -> bool:
    """Archive une reponse d'API. Renvoie True si elle etait nouvelle."""
    statement = (insert(RawPayload)
                 .values(source_id=source_id,
                         endpoint=endpoint,
                         params=params,
                         fetched_at=fetched_at,
                         content_hash=content_hash(payload),
                         payload=payload,
                         filename=filename)
                 .on_conflict_do_nothing(index_elements=["content_hash"])
                 .returning(RawPayload.id))

    return session.execute(statement).first() is not None


# --------------------------------------------------- curseur de collecte

def get_sync_state(session: Session, source_id: int,
                   endpoint: str) -> SyncState:
    """Renvoie l'etat de collecte d'un endpoint, en le creant au besoin."""
    etat = session.scalar(
        select(SyncState).where(SyncState.source_id == source_id,
                                SyncState.endpoint == endpoint))

    if etat is not None:
        return etat

    session.execute(
        insert(SyncState)
        .values(source_id=source_id, endpoint=endpoint)
        .on_conflict_do_nothing(constraint="uq_sync_endpoint"))
    session.flush()

    return session.scalar(
        select(SyncState).where(SyncState.source_id == source_id,
                                SyncState.endpoint == endpoint))


def mark_sync_success(session: Session, source_id: int, endpoint: str,
                      cursor_at: datetime | None = None) -> None:
    """Enregistre une collecte reussie.

    Le curseur n'avance JAMAIS en cas d'echec : c'est ce qui garantit
    qu'une donnee non recuperee sera redemandee au passage suivant.
    """
    etat = get_sync_state(session, source_id, endpoint)
    maintenant = datetime.now(timezone.utc)

    etat.last_attempt_at = maintenant
    etat.last_success_at = maintenant
    etat.consecutive_failures = 0
    etat.last_error = None

    if cursor_at is not None:
        etat.cursor_at = cursor_at


def mark_sync_failure(session: Session, source_id: int, endpoint: str,
                      error: str) -> None:
    """Enregistre un echec. Le curseur reste ou il est."""
    etat = get_sync_state(session, source_id, endpoint)

    etat.last_attempt_at = datetime.now(timezone.utc)
    etat.consecutive_failures = (etat.consecutive_failures or 0) + 1
    etat.last_error = error[:500]


def all_sync_states(session: Session) -> list[SyncState]:
    """Tous les etats de collecte, pour la commande health."""
    return list(session.scalars(
        select(SyncState).order_by(SyncState.endpoint)).all())


def earliest_episode_start(session: Session, source_id: int, kind: str,
                           payload_egal: dict[str, str] | None = None
                           ) -> datetime | None:
    """Le plus ancien `started_at` d'un genre d'episode pour une source.

    `payload_egal` filtre sur des cles JSONB exactes, ex.
    `{"origine": "device"}`. Sert de frontiere temporelle a l'import
    Amazon du connecteur Kindle (kindle/amazon_export.py) : tout ce qui
    precede cette date vient de l'export, tout ce qui suit vient de la
    collecte en direct.

    Le filtre est necessaire, pas cosmetique : sans lui, la fonction
    renverrait la date de la ligne la plus ANCIENNE toutes origines
    confondues - et des le premier import Amazon reussi, cette date
    devient une session de 2025 importee la veille, ce qui ecraserait la
    frontiere a la prochaine execution et ferait passer tout
    l'historique pour "deja couvert par le direct".
    """
    requete = (select(func.min(Episode.started_at))
              .where(Episode.source_id == source_id, Episode.kind == kind))

    for cle, valeur in (payload_egal or {}).items():
        requete = requete.where(Episode.payload[cle].astext == valeur)

    return session.scalar(requete)


def store_batch(session: Session, source_id: int, metric_ids: dict[str, int],
                batch: Batch) -> dict[str, int]:
    """Ecrit un lot complet et renvoie un resume chiffre."""
    obs_inserted, obs_updated = upsert_observations(
        session, source_id, metric_ids, batch.observations)
    epi_inserted, epi_updated = upsert_episodes(
        session, source_id, batch.episodes)
    snap_inserted = insert_snapshots(session, source_id, batch.snapshots)

    return {"observations_inserees": obs_inserted,
            "observations_majs": obs_updated,
            "episodes_inseres": epi_inserted,
            "episodes_majs": epi_updated,
            "snapshots_inseres": snap_inserted}
