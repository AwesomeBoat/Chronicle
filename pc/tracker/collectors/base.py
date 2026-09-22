"""Ce qu'un collecteur doit savoir faire, et ce qu'on lui donne.

Un collecteur observe UNE chose (la fenetre active, git, le reseau...) et
emet des evenements via `ctx.factory`. Il ne connait ni la file, ni le
reseau, ni Chronicle.

Deux familles :

    Collector         demarre ses propres fils (message loop Windows,
                      serveur HTTP, surveillance de dossier)
    PollingCollector  une methode poll() appelee a intervalle fixe par un
                      ordonnanceur partage : un seul fil pour tous

Un collecteur qui leve une exception n'arrete pas le tracker : il est
journalise, un evenement `collector_status` est emis (le trou de
collecte est ENREGISTRE, pas devine), et il est rappele plus tard.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from pc.tracker.config import Config
from pc.tracker.core.clock import utc_now
from pc.tracker.core.events import EventFactory
from pc.tracker.core.inputs import InputAggregator
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.spans import BrowserPages

logger = logging.getLogger(__name__)


class Bus:
    """Signaux entre collecteurs : "suspend", "resume", "repo_activity"...

    Minimal a dessein : un nom, une charge, des abonnes appeles dans le fil
    de l'emetteur. Un abonne lent ralentit l'emetteur - donc les abonnes
    ne font que poser un drapeau ou reveiller leur propre fil.
    """

    def __init__(self) -> None:
        self._abonnes: dict[str, list[Callable[[Any], None]]] = {}
        self._verrou = threading.Lock()

    def subscribe(self, nom: str, rappel: Callable[[Any], None]) -> None:
        with self._verrou:
            self._abonnes.setdefault(nom, []).append(rappel)

    def publish(self, nom: str, charge: Any = None) -> None:
        with self._verrou:
            abonnes = list(self._abonnes.get(nom, []))

        for rappel in abonnes:
            try:
                rappel(charge)
            except Exception:                          # noqa: BLE001
                logger.exception("abonne de %s en erreur", nom)


class PlatformHooks:
    """Ce que les collecteurs communs demandent au systeme.

    Chaque plateforme (platforms/windows, platforms/linux) fournit sa
    version ; par defaut, rien n'est connu et la valeur reste vide - jamais
    inventee.
    """

    def device_details(self) -> dict:
        """os_version, os_build, cpu_model, gpus... (device_info)."""
        return {}

    def gpu_sample(self) -> tuple[float | None, float | None]:
        """(utilisation GPU en %, VRAM utilisee en Mo)."""
        return None, None

    def cpu_temperature(self) -> float | None:
        return None

    def wifi_ssid(self) -> str | None:
        return None

    def interface_kinds(self) -> dict[str, str]:
        """{nom d'interface: wifi | ethernet | vpn | mobile | other}."""
        return {}

    def timezone(self) -> str | None:
        """Nom IANA du fuseau, si le systeme le donne sans ambiguite."""
        return None

    def close(self) -> None:
        """Libere les ressources (requetes PDH...)."""


@dataclass
class Context:
    """Tout ce qu'un collecteur recoit."""

    config: Config
    factory: EventFactory
    outbox: Outbox
    stop: threading.Event
    bus: Bus
    salt: str
    platform: str
    hooks: PlatformHooks = field(default_factory=PlatformHooks)
    inputs: InputAggregator = field(default_factory=InputAggregator)
    browser: BrowserPages | None = None
    # Dernier battement de coeur du lancement PRECEDENT : borne de fin des
    # intervalles restes ouverts apres un arret brutal.
    previous_heartbeat: datetime | None = None

    @property
    def device_id(self) -> str:
        return self.config.device_id

    def status(self, collecteur: str, statut: str, detail: str = "") -> None:
        """Enregistre l'etat d'un collecteur dans la chronologie."""
        self.factory.point("collector_status", "tracker", utc_now(), {
            "collector": collecteur, "status": statut,
            "detail": detail[:300] or None})

    def input_seconds(self, debut: datetime, fin: datetime) -> int:
        return self.inputs.active_seconds(debut.timestamp(), fin.timestamp())


class Collector:
    """Un collecteur qui gere ses propres fils."""

    name = "collector"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.log = logging.getLogger(f"pc.tracker.{self.name}")

    def start(self) -> None:
        """Demarre. Ne bloque pas."""

    def stop(self, raison: str = "tracker_stop") -> None:
        """Arrete et ferme les intervalles ouverts avec `raison`."""

    def snapshot(self) -> dict | None:
        """Intervalles ouverts, pour la reprise apres arret brutal."""
        return None

    def alive(self) -> bool:
        return True

    # ------------------------------------------------ reprise commune

    def recover(self) -> dict | None:
        """L'etat laisse par un arret brutal, retire du stockage."""
        cle = f"open:{self.name}"
        etat = self.ctx.outbox.get_state(cle)

        if etat is not None:
            self.ctx.outbox.delete_state(cle)

        return etat


class PollingCollector(Collector):
    """Un collecteur sonde a intervalle fixe par l'ordonnanceur partage."""

    interval_s = 60.0
    # Delai avant le premier sondage. Court par defaut (l'etat initial est
    # utile tout de suite) ; les mesures de taux attendent un intervalle
    # complet, sinon le premier echantillon couvrirait une seconde.
    first_delay_s = 1.0

    def poll(self, now: datetime) -> None:
        raise NotImplementedError


@dataclass(order=True)
class _Tache:
    echeance: float
    rang: int
    collecteur: PollingCollector = field(compare=False)
    echecs: int = field(default=0, compare=False)


class Scheduler:
    """Un seul fil pour tous les collecteurs sondes.

    Un sondage lent (git, journal systeme : quelques centaines de ms)
    retarde un peu les suivants ; c'est sans consequence a ces rythmes et
    ca evite dix fils qui dorment.
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._taches: list[_Tache] = []
        self._fil: threading.Thread | None = None
        self._rang = 0

    def add(self, collecteur: PollingCollector,
            delai_initial: float | None = None):
        self._rang += 1
        delai = collecteur.first_delay_s if delai_initial is None \
            else delai_initial
        heapq.heappush(self._taches, _Tache(time.monotonic() + delai,
                                            self._rang, collecteur))

    def start(self) -> None:
        self._fil = threading.Thread(target=self._boucle, name="scheduler",
                                     daemon=True)
        self._fil.start()

    def join(self, timeout: float) -> None:
        if self._fil is not None:
            self._fil.join(timeout)

    def _boucle(self) -> None:
        while not self.ctx.stop.is_set():
            if not self._taches:
                self.ctx.stop.wait(1.0)
                continue

            tache = self._taches[0]
            attente = tache.echeance - time.monotonic()

            if attente > 0:
                self.ctx.stop.wait(min(attente, 1.0))
                continue

            heapq.heappop(self._taches)
            intervalle = tache.collecteur.interval_s

            try:
                tache.collecteur.poll(utc_now())
                tache.echecs = 0
            except Exception as erreur:                # noqa: BLE001
                tache.echecs += 1
                logger.exception("collecteur %s en erreur",
                                 tache.collecteur.name)

                # Un seul evenement par serie d'echecs, puis silence : un
                # collecteur casse ne doit pas remplir la file d'erreurs.
                if tache.echecs == 1:
                    self.ctx.status(tache.collecteur.name, "error",
                                    f"{type(erreur).__name__}: {erreur}")

                intervalle = min(3600.0, intervalle * 2 ** min(tache.echecs, 6))

            tache.echeance = time.monotonic() + intervalle
            heapq.heappush(self._taches, tache)
