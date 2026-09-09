"""Format d'echange entre un connecteur et la base.

Un mapper (polar/mapper.py, et demain food/mapper.py) ne connait ni
SQLAlchemy, ni les tables : il produit ces objets. Le repository ne
connait ni Polar, ni son JSON : il consomme ces objets.

C'est cette frontiere qui rend une nouvelle source ajoutable sans
toucher a la base.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MetricSpec:
    """Declaration d'une grandeur : ce qu'il faut pour creer la ligne metric."""

    code: str
    unit: str
    granularity: str = "instant"
    description: str = ""


@dataclass(slots=True)
class ObservationRecord:
    """Une mesure : une metrique, un instant, une valeur."""

    metric_code: str
    observed_at: datetime
    value: float


@dataclass(slots=True)
class EpisodeRecord:
    """Quelque chose qui a dure."""

    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SnapshotRecord:
    """Un etat qui change lentement (profil, reglages)."""

    kind: str
    captured_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Batch:
    """Tout ce qu'un fichier brut produit, en une fois."""

    observations: list[ObservationRecord] = field(default_factory=list)
    episodes: list[EpisodeRecord] = field(default_factory=list)
    snapshots: list[SnapshotRecord] = field(default_factory=list)

    def extend(self, other: "Batch") -> None:
        self.observations.extend(other.observations)
        self.episodes.extend(other.episodes)
        self.snapshots.extend(other.snapshots)

    def __len__(self) -> int:
        return len(self.observations) + len(self.episodes) + len(self.snapshots)
