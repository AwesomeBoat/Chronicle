"""Tables de la base, en SQLAlchemy 2.0 (style Mapped / mapped_column).

Traduction directe de docs/SCHEMA.md. Six tables :

    source ---+--< observation >-- metric
              +--< episode
              +--< profile_snapshot
              +--< raw_payload

Regle du schema : une source de donnees n'est PAS une table. Ce qui
structure la base, c'est la forme temporelle de la donnee :
point (observation), intervalle (episode), etat (profile_snapshot).
"""

from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, Float,
                        ForeignKey, Index, Integer, SmallInteger, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# timestamptz : instant absolu, pas une heure d'horloge. Voir SCHEMA.md.
TZDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Ancetre commun. Base.metadata connait toutes les tables declarees."""


class Source(Base):
    """Un connecteur : polar_accesslink, plus tard withings_scale, food...

    C'est la table qui rend le modele extensible : ajouter une source de
    donnees, c'est ajouter une LIGNE ici, pas une table.
    """

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    # unique=True cree implicitement un index : les recherches par code
    # (get_or_create a chaque ingestion) sont donc gratuites.
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)

    # server_default=func.now() : c'est PostgreSQL qui date, pas Python.
    # L'horloge du serveur est la meme pour tout le monde.
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<Source {self.code}>"


class Metric(Base):
    """Une grandeur mesurable : heart_rate, sleep.score, weight...

    Catalogue partage entre sources : heart_rate reste heart_rate, que la
    mesure vienne de la Polar ou d'un autre capteur. C'est ce qui permettra
    de comparer deux sources sur le meme axe.
    """

    __tablename__ = "metric"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # Sans unite, un nombre ne veut rien dire : 55 bpm ou 55 kg ?
    unit: Mapped[str] = mapped_column(String(16), nullable=False)

    # instant / daily / nightly / period. Sur la metrique et non sur chaque
    # observation : cardio_load est TOUJOURS journalier, inutile de repeter
    # l'information des millions de fois.
    granularity: Mapped[str] = mapped_column(String(16), nullable=False,
                                             default="instant")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "granularity IN ('instant', 'daily', 'nightly', 'period')",
            name="ck_metric_granularity"),
    )

    def __repr__(self) -> str:
        return f"<Metric {self.code} ({self.unit})>"


class Observation(Base):
    """Une mesure ponctuelle. Table etroite et longue : le coeur du modele.

    Une ligne = une valeur numerique, une metrique, un instant.
    Toute la base repose sur cette forme.
    """

    __tablename__ = "observation"

    # BigInteger : un Integer plafonne a 2,1 milliards. Une seule journee de
    # FC continue fait deja ~1000 lignes ; sur 10 ans et 10 sources, la
    # marge d'un int n'est pas confortable.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ondelete="CASCADE" : supprimer une source efface ses donnees.
    # Sans cette clause, PostgreSQL refuserait la suppression (RESTRICT).
    source_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False)

    # Pas de CASCADE ici : supprimer une metrique par erreur ne doit pas
    # emporter silencieusement des millions de mesures.
    metric_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("metric.id"), nullable=False)

    observed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    # observed_at = quand la mesure a eu lieu.
    # ingested_at = quand on l'a ecrite. Les deux different de plusieurs
    # jours lors d'un backfill : sans la seconde, impossible de savoir
    # quand une valeur est apparue dans la base.
    ingested_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False)

    source: Mapped["Source"] = relationship()
    metric: Mapped["Metric"] = relationship()

    __table_args__ = (
        # LA contrainte d'idempotence (etape 7). Rejouer dix fois le meme
        # fichier ne peut pas creer dix lignes : la deuxieme insertion
        # entre en conflit sur cette clef, et ON CONFLICT decide quoi faire.
        UniqueConstraint("source_id", "metric_id", "observed_at",
                         name="uq_observation_point"),

        # "la serie X sur la periode Y" : on filtre d'abord par metrique,
        # puis on balaye le temps. L'ordre des colonnes suit cette lecture.
        Index("ix_observation_metric_time", "metric_id",
              observed_at.desc()),

        # "tout ce qui s'est passe ce jour-la", toutes metriques confondues.
        Index("ix_observation_time", "observed_at"),
    )

    def __repr__(self) -> str:
        return f"<Observation m={self.metric_id} {self.observed_at} {self.value}>"


class Episode(Base):
    """Ce qui a une duree : une nuit, une seance, une zone d'activite.

    Les grandeurs comparables entre sources sont extraites dans
    Observation ; tout le reste du JSON d'origine dort dans payload.
    Structure la ou on veut comparer, semi-structure la ou on veut
    simplement ne rien perdre.
    """

    __tablename__ = "episode"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False)

    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    # Nullable : un episode peut etre en cours, ou de fin inconnue
    # (derniere zone d'activite d'une periode, par exemple).
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    # JSONB (et pas JSON) : stockage binaire decompose, donc interrogeable
    # (payload->>'device_id') et indexable en GIN. Le type JSON garderait
    # le texte brut, illisible pour le moteur.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    ingested_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False)

    source: Mapped["Source"] = relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "kind", "started_at",
                         name="uq_episode_start"),
        Index("ix_episode_kind_time", "kind", started_at.desc()),
        Index("ix_episode_span", "started_at", "ended_at"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at",
                        name="ck_episode_order"),
    )

    def __repr__(self) -> str:
        return f"<Episode {self.kind} {self.started_at}>"


class ProfileSnapshot(Base):
    """Un etat qui change lentement : taille, VO2max, seuils, objectifs.

    Dedoublonnage PAR CONTENU (content_hash), pas par date : collecter
    300 fois un profil inchange cree une seule ligne, et le jour ou une
    valeur bouge, une deuxieme apparait. On obtient l'historique des
    changements sans avoir a decider d'avance quels champs surveiller.
    """

    __tablename__ = "profile_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False)

    kind: Mapped[str] = mapped_column(String(48), nullable=False)

    # Date du champ "modified" quand la source en fournit un, date de
    # collecte sinon.
    captured_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    # String(64) et pas Text : un SHA-256 en hexadecimal fait exactement
    # 64 caracteres. La contrainte de longueur documente le format.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    ingested_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False)

    source: Mapped["Source"] = relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "kind", "content_hash",
                         name="uq_snapshot_content"),
        Index("ix_snapshot_kind_time", "kind", captured_at.desc()),
    )

    def __repr__(self) -> str:
        return f"<ProfileSnapshot {self.kind} {self.captured_at}>"


class RawPayload(Base):
    """Registre des reponses d'API archivees dans data/raw/.

    Tracabilite et rejouabilite : si un parseur est bogue, on corrige le
    code et on rejoue depuis la base, sans redemander a Polar - qui aura
    efface au bout de ~28 jours.
    """

    __tablename__ = "raw_payload"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False)

    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)

    # Nullable : beaucoup d'appels n'ont pas de parametres. Sans eux, un
    # fichier continuous-heart-rate ne dit pas quelle plage il couvre.
    params: Mapped[dict | None] = mapped_column(JSONB)

    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    # UNIQUE global : deux fichiers au contenu identique (meme reponse
    # collectee deux fois) ne creent qu'une ligne.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                              unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Nom du fichier d'origine : utile pour retrouver la trace sur disque.
    filename: Mapped[str | None] = mapped_column(String(255))

    ingested_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False)

    source: Mapped["Source"] = relationship()

    __table_args__ = (
        Index("ix_raw_endpoint_time", "endpoint", fetched_at.desc()),
    )

    def __repr__(self) -> str:
        return f"<RawPayload {self.endpoint} {self.fetched_at}>"


class SyncState(Base):
    """Ou en est la collecte, endpoint par endpoint. (V2)

    Sans cette table, chaque execution redemanderait toute la fenetre de
    retention de Polar (~28 jours) : 28 fois le travail, et un quota d'API
    brule pour rien.

    Avec elle, la question devient "qu'est-ce qui a change depuis la
    derniere fois ?". Deux dates, et elles ne disent PAS la meme chose :

      last_success_at  quand la collecte a reussi (sante du systeme)
      cursor_at        jusqu'ou la donnee est consideree comme recuperee
                       (avancement de la collecte)

    Elles divergent des qu'une execution echoue : la sante recule, mais le
    curseur, lui, ne doit pas avancer.
    """

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False)

    # L'endpoint normalise ("/users/sleep"), pas l'URL complete : le
    # curseur suit une FAMILLE de donnees, pas un appel particulier.
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)

    cursor_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    # Remis a zero a chaque succes. Un compteur qui grimpe = une panne
    # installee, pas un incident : c'est ce que "health" surveille.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)

    source: Mapped["Source"] = relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "endpoint", name="uq_sync_endpoint"),
    )

    def __repr__(self) -> str:
        return f"<SyncState {self.endpoint} cursor={self.cursor_at}>"


# Ordre de creation/suppression gere par SQLAlchemy grace aux ForeignKey :
# source et metric d'abord, le reste ensuite.
ALL_TABLES = (Source, Metric, Observation, Episode, ProfileSnapshot,
              RawPayload, SyncState)
