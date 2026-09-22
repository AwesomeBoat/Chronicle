"""File locale et synchronisation : lots, rejeu, retry, doublons, refus.

Le serveur est un vrai serveur HTTP local (http.server) dont on choisit
les reponses : c'est le client reel (urllib) qui est teste, pas une
imitation.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pc.tracker.core.events import EventFactory
from pc.tracker.core.outbox import Outbox
from pc.tracker.core.sync import Backoff, ChronicleClient, Syncer

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def fabriquer(n: int) -> list[dict]:
    evenements: list[dict] = []
    factory = EventFactory("windows-main", evenements.append)

    for i in range(n):
        factory.point("file_change", "test", T0 + timedelta(seconds=i),
                      {"action": "created", "path": f"~/f{i}.txt",
                       "is_dir": False})

    return evenements


@pytest.fixture
def boite(tmp_path):
    b = Outbox(tmp_path / "tracker.db")
    yield b
    b.close()


# =============================================================== file

def test_enqueue_ignore_un_event_id_deja_present(boite):
    evenements = fabriquer(3)
    assert boite.enqueue(evenements) == 3
    assert boite.enqueue(evenements) == 0
    assert boite.counts() == {"pending": 3, "sent": 0, "dead": 0}


def test_lot_fige_meme_batch_id_meme_contenu(boite):
    boite.enqueue(fabriquer(5))
    premier = boite.next_batch(3)
    boite.mark_failed(premier.batch_id, "reseau")
    boite.enqueue(fabriquer(2))
    second = boite.next_batch(3)
    assert second.batch_id == premier.batch_id
    assert [e["event_id"] for e in second.events] == \
        [e["event_id"] for e in premier.events]


def test_lots_dans_l_ordre_de_creation(boite):
    evenements = fabriquer(7)
    boite.enqueue(evenements)
    lot = boite.next_batch(4)
    boite.mark_sent(lot.batch_id)
    suivant = boite.next_batch(4)
    ordre = [e["event_id"] for e in lot.events + suivant.events]
    assert ordre == [e["event_id"] for e in evenements]


def test_persistance_apres_redemarrage(tmp_path):
    chemin = tmp_path / "tracker.db"
    b = Outbox(chemin)
    b.enqueue(fabriquer(4))
    b.set_state("heartbeat", "2026-09-22T12:00:00.000000Z")
    b.close()

    rouverte = Outbox(chemin)
    assert rouverte.counts()["pending"] == 4
    assert rouverte.get_state("heartbeat") == "2026-09-22T12:00:00.000000Z"
    rouverte.close()


def test_purge_ne_touche_pas_l_attente(boite):
    boite.enqueue(fabriquer(3))
    lot = boite.next_batch(2)
    boite.mark_sent(lot.batch_id)
    assert boite.purge(garder_envoyes_jours=-1, garder_morts_jours=30) == 2
    assert boite.counts() == {"pending": 1, "sent": 0, "dead": 0}


def test_resend_remet_en_attente(boite):
    boite.enqueue(fabriquer(2))
    boite.mark_sent(boite.next_batch(2).batch_id)
    assert boite.requeue_sent(0) == 2
    assert boite.counts()["pending"] == 2


def test_dedup_key_retrouvable(boite):
    evenements = fabriquer(1)
    boite.enqueue(evenements)
    assert boite.has_dedup_key(evenements[0]["dedup_key"])
    assert not boite.has_dedup_key("inconnue")


def test_etat_cle_valeur_et_prefixes(boite):
    boite.set_state("open:desktop", {"a": 1})
    boite.set_state("open:media", {"b": 2})
    boite.set_state("openx", 3)
    assert boite.state_keys("open:") == ["open:desktop", "open:media"]
    boite.delete_state("open:media")
    assert boite.get_state("open:media", "absent") == "absent"


# ======================================================= faux serveur

class Serveur:
    """Serveur HTTP local ; `reponses` = codes a renvoyer dans l'ordre."""

    def __init__(self, reponses: list[int] | None = None,
                 refuser_si=None) -> None:
        self.reponses = list(reponses or [])
        self.refuser_si = refuser_si
        self.lots: list[dict] = []
        self.cles: list[str | None] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                corps = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])))
                parent.cles.append(self.headers.get("X-API-Key"))
                code = parent.reponses.pop(0) if parent.reponses else 200

                if parent.refuser_si and parent.refuser_si(corps):
                    code = 422

                if code == 200:
                    parent.lots.append(corps)

                donnees = json.dumps({"stored": len(corps["events"])}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(donnees)))
                self.end_headers()
                self.wfile.write(donnees)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serveur():
    instances = []

    def creer(*args, **kwargs):
        s = Serveur(*args, **kwargs)
        instances.append(s)
        return s

    yield creer

    for s in instances:
        s.close()


def syncer(boite, url, taille=500) -> Syncer:
    s = Syncer(boite, ChronicleClient(url, "cle-test", timeout=5),
               "windows-main", batch_size=taille)
    s.backoff = Backoff(base=0.01, plafond=0.02)
    return s


# ======================================================== synchronisation

def test_envoi_par_lots_et_marquage(boite, serveur):
    s = serveur()
    boite.enqueue(fabriquer(1200))
    syncer(boite, s.url, 500).sync_once()
    assert [len(l["events"]) for l in s.lots] == [500, 500, 200]
    assert boite.counts() == {"pending": 0, "sent": 1200, "dead": 0}
    assert set(s.cles) == {"cle-test"}
    assert all(l["device_id"] == "windows-main" for l in s.lots)


def test_chronicle_eteint_rien_n_est_perdu(boite):
    """Scenario PC portable ferme : l'envoi echoue, tout reste en file."""
    boite.enqueue(fabriquer(10))
    s = syncer(boite, "http://127.0.0.1:9")
    attente = s.sync_once()
    assert attente > 0
    assert boite.counts()["pending"] == 10
    assert "reseau" in s.derniere_erreur


def test_retry_apres_503_meme_lot(boite, serveur):
    s = serveur([503, 200])
    boite.enqueue(fabriquer(3))
    sync = syncer(boite, s.url)
    sync.sync_once()                          # 503 : garde, attend
    assert boite.counts()["pending"] == 3
    sync.sync_once()                          # 200 : envoye
    assert boite.counts()["sent"] == 3
    assert len(s.lots) == 1


def test_reponse_perdue_le_lot_repart_identique(boite, serveur):
    """Le serveur a ecrit mais la reponse est un 500 : le renvoi porte le
    MEME batch_id et les memes evenements (Chronicle le reconnaitra)."""
    s = serveur([500, 200])
    boite.enqueue(fabriquer(4))
    sync = syncer(boite, s.url)
    lot = boite.next_batch(500)
    sync.sync_once()
    sync.sync_once()
    assert s.lots[0]["batch_id"] == lot.batch_id
    assert [e["event_id"] for e in s.lots[0]["events"]] == \
        [e["event_id"] for e in lot.events]


def test_cle_refusee_garde_les_donnees(boite, serveur):
    s = serveur([401])
    boite.enqueue(fabriquer(2))
    syncer(boite, s.url).sync_once()
    assert boite.counts() == {"pending": 2, "sent": 0, "dead": 0}


def test_refus_422_isole_l_evenement_fautif(boite, serveur):
    """Un lot refuse est coupe en deux jusqu'a isoler le fautif."""
    evenements = fabriquer(8)
    fautif = evenements[5]["event_id"]
    s = serveur(refuser_si=lambda corps: any(
        e["event_id"] == fautif for e in corps["events"]))
    boite.enqueue(evenements)
    syncer(boite, s.url).sync_once()
    assert boite.counts() == {"pending": 0, "sent": 7, "dead": 1}
    envoyes = [e["event_id"] for l in s.lots for e in l["events"]]
    assert fautif not in envoyes and len(envoyes) == 7


def test_budget_borne_l_envoi_a_l_arret(boite):
    boite.enqueue(fabriquer(3))
    s = syncer(boite, "http://10.255.255.1:9")      # adresse qui ne repond pas
    debut = datetime.now()
    s.sync_once(budget_s=1.0)
    assert (datetime.now() - debut).total_seconds() < 5


def test_backoff_double_et_plafonne():
    b = Backoff(base=10, plafond=100, gigue=0)
    assert [b.prochain() for _ in range(6)] == [10, 20, 40, 80, 100, 100]
    b.reset()
    assert b.prochain() == 10


def test_ping_sans_evenement(serveur):
    s = serveur()
    assert ChronicleClient(s.url, "k").ping("windows-main").ok
    assert s.lots[0]["events"] == []
