"""File locale : les evenements attendent ici que Chronicle soit joignable.

Chronicle peut etre eteint (PC portable ferme, Docker arrete, reseau
coupe) : c'est le cas NORMAL, pas une panne. La file est un fichier SQLite
sur le disque de la machine, donc elle survit a un redemarrage.

    pending  -> en attente d'envoi
    sent     -> accepte par Chronicle (garde quelques jours, puis purge)
    dead     -> refuse definitivement par Chronicle (garde pour enquete)

LE LOT EST FIGE AU PREMIER ESSAI
--------------------------------
Quand un lot est forme, ses lignes recoivent un batch_id. Un nouvel essai
renvoie EXACTEMENT les memes lignes sous le meme batch_id. Chronicle
reconnait alors le lot deja archive et ne refait rien : c'est le cas du
"lot recu, reponse perdue", celui qui cree des doublons dans les
synchronisations naives.

Une seule connexion, protegee par un verrou : le volume (quelques
evenements par minute) ne justifie pas mieux, et SQLite en WAL laisse lire
`status` pendant une ecriture.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pc.tracker.core.clock import uuid7

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    NOT NULL UNIQUE,
    event_type  TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    dedup_key   TEXT,
    body        TEXT    NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'pending',
    batch_id    TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL    NOT NULL,
    sent_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_outbox_state_seq ON outbox (state, seq);
CREATE INDEX IF NOT EXISTS ix_outbox_batch ON outbox (batch_id);
CREATE INDEX IF NOT EXISTS ix_outbox_dedup ON outbox (dedup_key);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


@dataclass(slots=True)
class Lot:
    """Un lot pret a partir."""

    batch_id: str
    events: list[dict]

    def __len__(self) -> int:
        return len(self.events)


class Outbox:
    """La file persistante, plus un petit magasin cle/valeur (l'etat)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._verrou = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL : survit a un crash du processus, pas forcement a une
        # coupure de courant pendant l'ecriture. Acceptable : on perdrait
        # au pire les dernieres secondes.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._verrou:
            self._db.close()

    # ------------------------------------------------------------ file

    def enqueue(self, events: Iterable[dict]) -> int:
        """Ajoute des evenements. Un event_id deja present est ignore."""
        maintenant = time.time()
        lignes = [(e["event_id"], e["event_type"], e["ts"], e.get("dedup_key"),
                   json.dumps(e, ensure_ascii=False, separators=(",", ":")),
                   maintenant)
                  for e in events]

        if not lignes:
            return 0

        with self._verrou:
            avant = self._db.total_changes
            self._db.execute("BEGIN")
            self._db.executemany(
                "INSERT OR IGNORE INTO outbox "
                "(event_id, event_type, ts, dedup_key, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", lignes)
            self._db.execute("COMMIT")
            return self._db.total_changes - avant

    def next_batch(self, taille: int) -> Lot | None:
        """Le prochain lot a envoyer, ou None si la file est vide.

        Un lot deja forme et pas encore accepte passe en premier, tel quel.
        """
        with self._verrou:
            fige = self._db.execute(
                "SELECT batch_id FROM outbox WHERE state = 'pending' "
                "AND batch_id IS NOT NULL ORDER BY seq LIMIT 1").fetchone()

            if fige is not None:
                batch_id = fige[0]
            else:
                seqs = [r[0] for r in self._db.execute(
                    "SELECT seq FROM outbox WHERE state = 'pending' "
                    "AND batch_id IS NULL ORDER BY seq LIMIT ?", (taille,))]

                if not seqs:
                    return None

                batch_id = uuid7()
                self._db.execute("BEGIN")
                self._db.executemany(
                    "UPDATE outbox SET batch_id = ? WHERE seq = ?",
                    [(batch_id, s) for s in seqs])
                self._db.execute("COMMIT")

            corps = [json.loads(r[0]) for r in self._db.execute(
                "SELECT body FROM outbox WHERE batch_id = ? "
                "AND state = 'pending' ORDER BY seq", (batch_id,))]

            return Lot(batch_id, corps)

    def mark_sent(self, batch_id: str) -> None:
        with self._verrou:
            self._db.execute(
                "UPDATE outbox SET state = 'sent', sent_at = ?, "
                "attempts = attempts + 1, last_error = NULL "
                "WHERE batch_id = ? AND state = 'pending'",
                (time.time(), batch_id))

    def mark_failed(self, batch_id: str, erreur: str) -> None:
        """Echec temporaire : le lot reste fige, il repartira tel quel."""
        with self._verrou:
            self._db.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE batch_id = ? AND state = 'pending'",
                (erreur[:500], batch_id))

    def release(self, batch_id: str) -> None:
        """Defait un lot (refus 4xx) : ses lignes seront redecoupees."""
        with self._verrou:
            self._db.execute(
                "UPDATE outbox SET batch_id = NULL WHERE batch_id = ? "
                "AND state = 'pending'", (batch_id,))

    def mark_dead(self, event_ids: Iterable[str], erreur: str) -> None:
        """Refus definitif. La ligne est gardee, jamais supprimee en silence."""
        with self._verrou:
            self._db.execute("BEGIN")
            self._db.executemany(
                "UPDATE outbox SET state = 'dead', last_error = ?, "
                "batch_id = NULL WHERE event_id = ?",
                [(erreur[:500], i) for i in event_ids])
            self._db.execute("COMMIT")

    def has_dedup_key(self, cle: str) -> bool:
        with self._verrou:
            return self._db.execute(
                "SELECT 1 FROM outbox WHERE dedup_key = ? LIMIT 1",
                (cle,)).fetchone() is not None

    def counts(self) -> dict[str, int]:
        with self._verrou:
            comptes = dict(self._db.execute(
                "SELECT state, count(*) FROM outbox GROUP BY state").fetchall())

        return {etat: comptes.get(etat, 0)
                for etat in ("pending", "sent", "dead")}

    def oldest_pending(self) -> str | None:
        with self._verrou:
            ligne = self._db.execute(
                "SELECT ts FROM outbox WHERE state = 'pending' "
                "ORDER BY seq LIMIT 1").fetchone()
        return ligne[0] if ligne else None

    def last_error(self) -> str | None:
        with self._verrou:
            ligne = self._db.execute(
                "SELECT last_error FROM outbox WHERE state = 'pending' "
                "AND last_error IS NOT NULL ORDER BY seq LIMIT 1").fetchone()
        return ligne[0] if ligne else None

    def purge(self, garder_envoyes_jours: float, garder_morts_jours: float) -> int:
        """Supprime les lignes envoyees (et mortes) trop anciennes.

        Les lignes envoyees sont gardees quelques jours : si la base de
        Chronicle devait etre restauree, `python -m pc.tracker resend`
        pourrait les renvoyer.
        """
        maintenant = time.time()

        with self._verrou:
            avant = self._db.total_changes
            self._db.execute(
                "DELETE FROM outbox WHERE state = 'sent' AND sent_at < ?",
                (maintenant - garder_envoyes_jours * 86400,))
            self._db.execute(
                "DELETE FROM outbox WHERE state = 'dead' AND created_at < ?",
                (maintenant - garder_morts_jours * 86400,))
            return self._db.total_changes - avant

    def requeue_sent(self, depuis_epoch: float) -> int:
        """Remet en attente ce qui a ete envoye depuis une date (resend)."""
        with self._verrou:
            avant = self._db.total_changes
            self._db.execute(
                "UPDATE outbox SET state = 'pending', batch_id = NULL "
                "WHERE state = 'sent' AND sent_at >= ?", (depuis_epoch,))
            return self._db.total_changes - avant

    # ------------------------------------------------------------- etat

    def get_state(self, cle: str, defaut: Any = None) -> Any:
        with self._verrou:
            ligne = self._db.execute(
                "SELECT value FROM kv WHERE key = ?", (cle,)).fetchone()
        return json.loads(ligne[0]) if ligne else defaut

    def set_state(self, cle: str, valeur: Any) -> None:
        texte = json.dumps(valeur, ensure_ascii=False, default=str)

        with self._verrou:
            self._db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (cle, texte, time.time()))

    def delete_state(self, cle: str) -> None:
        with self._verrou:
            self._db.execute("DELETE FROM kv WHERE key = ?", (cle,))

    def state_keys(self, prefixe: str) -> list[str]:
        # substr plutot que LIKE : "_" est un joker de LIKE, et les cles en
        # contiennent.
        with self._verrou:
            return [r[0] for r in self._db.execute(
                "SELECT key FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key",
                (len(prefixe), prefixe))]
