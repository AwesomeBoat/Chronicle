"""Envoi des lots a Chronicle : rejouable, sans doublon, sans perte.

La boucle, reprise du protocole de PhoneTracker (docs/SYNC-PROTOCOL.md) :

    1. prendre le prochain lot (fige : meme batch_id a chaque essai)
    2. POST /api/v1/events
    3. 2xx  -> marquer envoye.       JAMAIS avant la reponse.
    4. sinon -> garder, attendre, recommencer

Ce que chaque reponse veut dire :

    2xx          Chronicle a ecrit le lot (brut archive compris)
    401 / 403    mauvaise cle : rien a faire seul, on attend longtemps
    400/413/422  le lot est refuse tel quel : on le coupe en deux jusqu'a
                 isoler l'evenement fautif, qui passe en 'dead'
    429 / 5xx    Chronicle ou sa base est indisponible : on reessaie
    reseau       Chronicle est eteint : cas normal, on reessaie

Le delai entre deux essais double a chaque echec (10 s, 20 s, 40 s...
jusqu'a 30 min), avec un peu de hasard pour ne pas synchroniser deux
machines sur le meme rythme.

Uniquement la bibliotheque standard (urllib) : rien a installer.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from pc.tracker.core.clock import utc_now, uuid7
from pc.tracker.core.outbox import Outbox
from pc import schema

logger = logging.getLogger(__name__)

EVENTS_PATH = "/api/v1/events"
HEALTH_PATH = "/health"


@dataclass(slots=True)
class Reponse:
    """Le resultat d'un appel HTTP, erreurs reseau comprises."""

    statut: int | None          # None = pas de reponse (reseau)
    corps: Any = None
    erreur: str | None = None

    @property
    def ok(self) -> bool:
        return self.statut is not None and 200 <= self.statut < 300


class ChronicleClient:
    """Le client HTTP de l'API d'ingestion."""

    def __init__(self, url: str, api_key: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _appel(self, methode: str, chemin: str, corps: Any = None,
               timeout: float | None = None) -> Reponse:
        donnees = None
        entetes = {"Accept": "application/json",
                   "User-Agent": "chronicle-pc-tracker"}

        if corps is not None:
            donnees = json.dumps(corps, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
            entetes["Content-Type"] = "application/json"

        if self.api_key:
            entetes["X-API-Key"] = self.api_key

        requete = urllib.request.Request(self.url + chemin, data=donnees,
                                         headers=entetes, method=methode)

        try:
            with urllib.request.urlopen(requete,
                                        timeout=timeout or self.timeout) as r:
                return Reponse(r.status, _lire_json(r.read()))

        except urllib.error.HTTPError as erreur:
            return Reponse(erreur.code, _lire_json(erreur.read()),
                           f"HTTP {erreur.code}")

        except (urllib.error.URLError, OSError, TimeoutError) as erreur:
            raison = getattr(erreur, "reason", erreur)
            return Reponse(None, None, f"reseau : {raison}")

    def post_batch(self, batch_id: str, device_id: str, events: list[dict],
                   timeout: float | None = None) -> Reponse:
        return self._appel("POST", EVENTS_PATH, {
            "batch_id": batch_id,
            "device_id": device_id,
            "sent_at": schema.format_ts(utc_now()),
            "events": events,
        }, timeout)

    def health(self) -> Reponse:
        return self._appel("GET", HEALTH_PATH, timeout=10)

    def ping(self, device_id: str) -> Reponse:
        """Un lot vide : verifie l'adresse ET la cle sans rien ecrire."""
        return self.post_batch(uuid7(), device_id, [], timeout=10)


def _lire_json(brut: bytes) -> Any:
    try:
        return json.loads(brut.decode("utf-8")) if brut else None
    except (UnicodeDecodeError, ValueError):
        return brut[:300].decode("utf-8", "replace")


class Backoff:
    """Attente croissante entre deux echecs, avec gigue."""

    def __init__(self, base: float = 10.0, plafond: float = 1800.0,
                 gigue: float = 0.2) -> None:
        self.base = base
        self.plafond = plafond
        self.gigue = gigue
        self.echecs = 0

    def reset(self) -> None:
        self.echecs = 0

    def prochain(self) -> float:
        self.echecs += 1
        attente = min(self.plafond, self.base * 2 ** (self.echecs - 1))
        return attente * random.uniform(1 - self.gigue, 1 + self.gigue)


class Syncer:
    """La boucle d'envoi, dans son propre fil."""

    def __init__(self, outbox: Outbox, client: ChronicleClient, device_id: str,
                 batch_size: int = 500, interval_s: float = 60.0) -> None:
        self.outbox = outbox
        self.client = client
        self.device_id = device_id
        self.batch_size = batch_size
        self.interval_s = interval_s
        self.backoff = Backoff()
        self._limite = batch_size
        self._reveil = threading.Event()
        self._stop = threading.Event()
        self._fil: threading.Thread | None = None
        self.derniere_reussite: float | None = None
        self.derniere_erreur: str | None = None

    # ------------------------------------------------------------ cycle

    def start(self) -> None:
        self._fil = threading.Thread(target=self._boucle, name="sync",
                                     daemon=True)
        self._fil.start()

    def stop(self, attente_s: float = 3.0) -> None:
        """Arrete la boucle. N'attend pas un envoi en cours au-dela de
        `attente_s` : le fil est un demon, et les lignes non confirmees
        restent 'pending' dans la file."""
        self._stop.set()
        self._reveil.set()

        if self._fil is not None:
            self._fil.join(timeout=attente_s)

    def wake(self) -> None:
        """Demande un envoi sans attendre l'intervalle (file pleine)."""
        self._reveil.set()

    def _boucle(self) -> None:
        attente = 5.0

        while not self._stop.is_set():
            self._reveil.wait(attente)
            self._reveil.clear()

            if self._stop.is_set():
                break

            try:
                attente = self.sync_once()
            except Exception:                            # noqa: BLE001
                # La synchro ne doit jamais tuer le tracker : les donnees
                # restent dans la file, on reessaiera.
                logger.exception("erreur inattendue pendant la synchro")
                attente = self.backoff.prochain()

    # ------------------------------------------------------------ envoi

    def sync_once(self, budget_s: float | None = None) -> float:
        """Envoie tout ce qui attend. Renvoie le delai avant le prochain essai.

        `budget_s` borne la duree totale (arret de la machine : on essaie
        vite, on n'attend pas).
        """
        debut = time.monotonic()

        while not self._stop.is_set() or budget_s is not None:
            if budget_s is not None and time.monotonic() - debut > budget_s:
                return self.interval_s

            lot = self.outbox.next_batch(self._limite)

            if lot is None:
                self.backoff.reset()
                return self.interval_s

            reponse = self.client.post_batch(
                lot.batch_id, self.device_id, lot.events,
                timeout=min(30.0, budget_s) if budget_s else None)

            if reponse.ok:
                self.outbox.mark_sent(lot.batch_id)
                self.derniere_reussite = time.time()
                self.derniere_erreur = None
                self.outbox.set_state("sync:last_ok", self.derniere_reussite)
                self.backoff.reset()
                self._limite = min(self.batch_size, self._limite * 2)
                self._signaler_rejets(reponse.corps)
                logger.info("lot %s envoye : %d evenement(s)",
                            lot.batch_id[:8], len(lot))
                continue

            self.derniere_erreur = reponse.erreur or f"HTTP {reponse.statut}"
            self.outbox.set_state("sync:last_error", {
                "at": time.time(), "error": self.derniere_erreur})

            if reponse.statut in (400, 413, 422):
                self._refus(lot, reponse)
                continue

            self.outbox.mark_failed(lot.batch_id, self.derniere_erreur)
            attente = self.backoff.prochain()

            if reponse.statut in (401, 403):
                logger.error("Chronicle refuse la cle d'API (%s) - verifier "
                             "[chronicle] api_key. Nouvel essai dans %.0f s",
                             reponse.statut, attente)
            else:
                logger.warning("Chronicle injoignable (%s) - %d evenement(s) "
                               "gardes, nouvel essai dans %.0f s",
                               self.derniere_erreur,
                               self.outbox.counts()["pending"], attente)
            return attente

        return self.interval_s

    def _refus(self, lot, reponse: Reponse) -> None:
        """Lot refuse : on coupe en deux jusqu'a isoler le fautif."""
        detail = json.dumps(reponse.corps, ensure_ascii=False)[:400]

        if len(lot) == 1:
            self.outbox.mark_dead([lot.events[0]["event_id"]],
                                  f"HTTP {reponse.statut} : {detail}")
            logger.error("evenement %s refuse definitivement : %s",
                         lot.events[0]["event_id"], detail)
            self._limite = self.batch_size
            return

        self.outbox.release(lot.batch_id)
        self._limite = max(1, len(lot) // 2)
        logger.warning("lot de %d refuse (HTTP %s), redecoupe en %d : %s",
                       len(lot), reponse.statut, self._limite, detail)

    @staticmethod
    def _signaler_rejets(corps: Any) -> None:
        """Chronicle a garde le brut mais n'a pas su traduire certains."""
        if not isinstance(corps, dict):
            return

        rejets = corps.get("rejected") or []

        if rejets:
            logger.warning("%d evenement(s) archives mais non traduits par "
                           "Chronicle : %s", len(rejets),
                           json.dumps(rejets[:3], ensure_ascii=False)[:400])
