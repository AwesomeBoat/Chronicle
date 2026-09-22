"""Le tracker en marche : demarrage, battement de coeur, reprise, arret.

    demarrage   reprise de l'arret precedent, device_info, collecteurs,
                synchro
    toutes 30s  battement de coeur + sauvegarde des intervalles ouverts
    arret       intervalles fermes, file ecrite, dernier envoi (5 s max)

L'ARRET BRUTAL N'EST PAS UNE EXCEPTION
--------------------------------------
Plantage, coupure de courant, `kill` : le tracker n'a pas pu fermer ses
intervalles. Au demarrage suivant, il les retrouve dans son etat et les
ferme au dernier battement de coeur (30 s de precision au pire), marques
`end_reason = recovered`. Le lancement precedent lui-meme devient un
`tracker_run` recupere : la chronologie dit ou la collecte s'est arretee,
au lieu de laisser un trou muet.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import secrets
import sys
import threading
import time
from datetime import datetime

from pc import schema
from pc.tracker import VERSION
from pc.tracker.collectors.base import (Bus, Collector, Context,
                                        PlatformHooks, PollingCollector,
                                        Scheduler)
from pc.tracker.config import Config
from pc.tracker.core.clock import detect_timezone, utc_now
from pc.tracker.core.events import EventFactory, dedup_key
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.spans import BrowserPages, Page
from pc.tracker.core.sync import ChronicleClient, Syncer

logger = logging.getLogger("pc.tracker")

BATTEMENT_S = 30.0
PURGE_S = 6 * 3600.0


# ============================================================ journal

def setup_logging(config: Config, console: bool = True) -> None:
    """Fichier tournant (2 Mo x 5) + console si elle existe.

    Sous pythonw (tache planifiee, pas de console), sys.stderr vaut None :
    la console est alors simplement omise.
    """
    racine = logging.getLogger()

    if getattr(setup_logging, "_fait", False):
        return

    config.log_dir.mkdir(parents=True, exist_ok=True)
    racine.setLevel(logging.DEBUG)

    fichier = logging.handlers.RotatingFileHandler(
        config.log_dir / "tracker.log", maxBytes=2 * 1024 * 1024,
        backupCount=5, encoding="utf-8")
    fichier.setLevel(getattr(logging, config.log_level, logging.INFO))
    fichier.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"))
    racine.addHandler(fichier)

    if console and sys.stderr is not None:
        flux = logging.StreamHandler(sys.stderr)
        flux.setLevel(getattr(logging, config.log_level, logging.INFO))
        flux.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        racine.addHandler(flux)

    setup_logging._fait = True


# ============================================================ ecriture

class EventSink:
    """File memoire -> SQLite, dans un fil a part.

    Les collecteurs (dont le fil des fenetres, qui traite aussi les
    entrees clavier/souris) ne touchent jamais le disque : ils deposent
    l'evenement ici et repartent.
    """

    def __init__(self, outbox: Outbox, on_write=None) -> None:
        self.outbox = outbox
        self.on_write = on_write
        self._file: queue.Queue = queue.Queue()
        self._fil = threading.Thread(target=self._boucle, name="sink",
                                     daemon=True)
        self._arret = threading.Event()
        self.ecrits = 0

    def start(self) -> None:
        self._fil.start()

    def put(self, event: dict) -> None:
        self._file.put(event)

    def _vider(self, lot: list | None = None) -> int:
        """Ecrit un lot (et tout ce qui attend derriere, jusqu'a 500)."""
        lot = lot or []

        while len(lot) < 500:
            try:
                lot.append(self._file.get_nowait())
            except queue.Empty:
                break

        if not lot:
            return 0

        # SQLite peut etre momentanement verrouille (un `status` qui lit en
        # meme temps) : quelques essais avant d'abandonner le lot.
        for essai in range(5):
            try:
                self.outbox.enqueue(lot)
                break
            except Exception:                            # noqa: BLE001
                logger.warning("file locale occupee, essai %d/5", essai + 1)
                time.sleep(0.5 * (essai + 1))
        else:
            logger.error("%d evenement(s) perdus : file locale inaccessible",
                         len(lot))
            return 0

        self.ecrits += len(lot)

        if self.on_write:
            self.on_write(len(lot))

        return len(lot)

    def _boucle(self) -> None:
        while not self._arret.is_set():
            try:
                premier = self._file.get(timeout=1.0)
            except queue.Empty:
                continue

            self._vider([premier])

    def flush(self, timeout: float = 10.0) -> None:
        """Ecrit tout ce qui attend (arret)."""
        self._arret.set()
        self._fil.join(timeout)
        fin = time.monotonic() + timeout

        while not self._file.empty() and time.monotonic() < fin:
            self._vider()


# ============================================================ tracker

class Tracker:
    """Un lancement du tracker sur une machine."""

    def __init__(self, config: Config, platform=None) -> None:
        self.config = config
        self.platform = platform
        self.collectors: list[Collector] = []
        self._arret = threading.Event()
        # Pose a la toute fin de l'arret (file ecrite, dernier envoi tente) :
        # la fin de session Windows l'attend avant de rendre la main.
        self.stopped = threading.Event()
        self._raison = "tracker_stop"
        self.run_start: datetime | None = None

    # --------------------------------------------------------- arret

    def request_stop(self, raison: str = "tracker_stop") -> None:
        """Demande l'arret (thread-safe). La raison ferme les intervalles."""
        if not self._arret.is_set():
            self._raison = raison
            logger.info("arret demande (%s)", raison)
            self._arret.set()

    @property
    def stopping(self) -> bool:
        return self._arret.is_set()

    # --------------------------------------------------------- montage

    def _contexte(self) -> Context:
        cfg = self.config
        self.outbox = Outbox(cfg.db_path)

        sel = self.outbox.get_state("salt")

        if not sel:
            sel = secrets.token_hex(16)
            self.outbox.set_state("salt", sel)

        self.syncer: Syncer | None = None

        def ecrit(n: int) -> None:
            if self.syncer and self.outbox.counts()["pending"] >= cfg.batch_size:
                self.syncer.wake()

        self.sink = EventSink(self.outbox, ecrit)
        hooks = self.platform.hooks(cfg) if self.platform else PlatformHooks()
        tz = cfg.timezone or hooks.timezone() or detect_timezone()
        factory = EventFactory(cfg.device_id, self.sink.put, tz)

        precedent = self.outbox.get_state("heartbeat")
        ctx = Context(
            config=cfg, factory=factory, outbox=self.outbox,
            stop=threading.Event(), bus=Bus(), salt=sel,
            platform=self.platform.NAME if self.platform else "unknown",
            hooks=hooks,
            previous_heartbeat=schema.parse_ts(precedent) if precedent
            else None)

        if cfg.enabled("browser"):
            ctx.browser = BrowserPages(self._emettre_page)

        self.ctx = ctx
        return ctx

    def _emettre_page(self, page: Page, debut: datetime, fin: datetime,
                      raison: str) -> None:
        self.ctx.factory.interval("browser_page", "browser_ext", debut, fin, {
            **page.payload(), "end_reason": raison,
            "input_active_s": self.ctx.input_seconds(debut, fin)})

    def _collecteurs(self) -> list[Collector]:
        from pc.tracker.collectors.browser import BrowserCollector
        from pc.tracker.collectors.device import DeviceCollector
        from pc.tracker.collectors.git import GitCollector
        from pc.tracker.collectors.metrics import MetricsCollector
        from pc.tracker.collectors.network import NetworkCollector
        from pc.tracker.collectors.terminal import TerminalCollector

        cfg, ctx = self.config, self.ctx
        liste: list[Collector] = [DeviceCollector(ctx)]
        communs = [("metrics", MetricsCollector), ("network", NetworkCollector),
                   ("git", GitCollector), ("terminal", TerminalCollector),
                   ("browser", BrowserCollector)]

        for nom, classe in communs:
            if cfg.enabled(nom):
                liste.append(classe(ctx))

        if self.platform:
            liste.extend(self.platform.collectors(ctx, self))

        for collecteur in liste:
            if isinstance(collecteur, PollingCollector) and \
                    hasattr(collecteur, "resync"):
                ctx.bus.subscribe("resume", collecteur.resync)

        return liste

    def _reprendre(self) -> None:
        """Ferme ce que l'arret brutal precedent a laisse ouvert."""
        precedent = self.outbox.get_state("run")

        if precedent is None:
            return

        fin = self.ctx.previous_heartbeat
        debut = schema.parse_ts(precedent["start"])

        if fin is None or fin < debut:
            fin = debut

        cle = dedup_key(self.config.device_id, "tracker_run",
                        schema.format_ts(debut))

        if not self.outbox.has_dedup_key(cle):
            self.ctx.factory.interval("tracker_run", "tracker", debut, fin, {
                "version": precedent.get("version", "?"),
                "platform": precedent.get("platform"),
                "collectors": precedent.get("collectors", []),
                "end_reason": "recovered"}, historique=True)
            logger.warning("arret brutal detecte : lancement du %s ferme au "
                           "dernier battement (%s)", debut, fin)

        self.outbox.delete_state("run")

    # --------------------------------------------------------- boucle

    def _battement(self) -> None:
        maintenant = utc_now()
        self.outbox.set_state("heartbeat", schema.format_ts(maintenant))

        for collecteur in self.collectors:
            try:
                etat = collecteur.snapshot()
            except Exception:                            # noqa: BLE001
                logger.exception("snapshot de %s", collecteur.name)
                continue

            cle = f"open:{collecteur.name}"

            if etat is None:
                self.outbox.delete_state(cle)
            else:
                self.outbox.set_state(cle, etat)

        self.ctx.inputs.prune(time.time())

    def run(self) -> int:
        """Tourne jusqu'a request_stop(). Renvoie le code de sortie."""
        cfg = self.config
        ctx = self._contexte()
        self.sink.start()
        self._reprendre()

        self.run_start = utc_now()
        self.collectors = self._collecteurs()
        noms = [c.name for c in self.collectors]
        self.outbox.set_state("run", {
            "start": schema.format_ts(self.run_start), "version": VERSION,
            "platform": ctx.platform, "collectors": noms})
        self.outbox.set_state("heartbeat", schema.format_ts(self.run_start))

        logger.info("tracker %s demarre - device_id=%s, collecteurs : %s",
                    VERSION, cfg.device_id, ", ".join(noms))

        scheduler = Scheduler(ctx)

        for collecteur in self.collectors:
            try:
                if isinstance(collecteur, PollingCollector):
                    scheduler.add(collecteur)

                collecteur.start()
            except Exception as erreur:                  # noqa: BLE001
                logger.exception("demarrage de %s impossible", collecteur.name)
                ctx.status(collecteur.name, "error",
                           f"{type(erreur).__name__}: {erreur}")

        scheduler.start()

        self.syncer = Syncer(self.outbox, ChronicleClient(cfg.chronicle_url,
                                                          cfg.api_key),
                             cfg.device_id, cfg.batch_size, cfg.sync_interval_s)
        self.syncer.start()

        derniere_purge = 0.0

        while not self._arret.wait(BATTEMENT_S):
            try:
                self._battement()

                if time.monotonic() - derniere_purge > PURGE_S:
                    derniere_purge = time.monotonic()
                    supprimes = self.outbox.purge(cfg.keep_sent_days,
                                                  cfg.keep_dead_days)
                    if supprimes:
                        logger.info("file locale : %d ligne(s) purgee(s)",
                                    supprimes)
            except Exception:                            # noqa: BLE001
                logger.exception("battement de coeur")

        return self._arreter(scheduler)

    def _arreter(self, scheduler: Scheduler) -> int:
        raison = self._raison
        maintenant = utc_now()
        self.ctx.stop.set()

        for collecteur in reversed(self.collectors):
            try:
                collecteur.stop(raison)
            except Exception:                            # noqa: BLE001
                logger.exception("arret de %s", collecteur.name)

        scheduler.join(5)

        self.ctx.factory.interval("tracker_run", "tracker", self.run_start,
                                  maintenant, {
                                      "version": VERSION,
                                      "platform": self.ctx.platform,
                                      "collectors": [c.name for c in
                                                     self.collectors],
                                      "end_reason": raison},
                                  historique=True)

        for cle in self.outbox.state_keys("open:"):
            self.outbox.delete_state(cle)

        self.outbox.delete_state("run")
        self.outbox.set_state("heartbeat", schema.format_ts(utc_now()))
        self.sink.flush()

        if self.syncer is not None:
            self.syncer.stop()
            # Dernier envoi, borne : a l'arret de la machine, Windows ne
            # laisse que quelques secondes.
            budget = 2.0 if raison in ("shutdown", "logoff") else 5.0

            try:
                self.syncer.sync_once(budget_s=budget)
            except Exception:                            # noqa: BLE001
                logger.exception("dernier envoi")

        self.ctx.hooks.close()
        comptes = self.outbox.counts()
        self.outbox.close()
        logger.info("tracker arrete (%s) - %d en attente d'envoi",
                    raison, comptes["pending"])
        self.stopped.set()
        return 0
