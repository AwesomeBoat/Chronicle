"""browser_page : recepteur local de l'extension navigateur.

L'extension (pc/tracker/browser_extension/) signale l'onglet actif a
chaque changement. Ce module l'ecoute sur 127.0.0.1 uniquement, nettoie
l'URL AVANT toute autre chose (domaine seul par defaut), et passe la page
a BrowserPages, qui ne compte le temps que lorsque le navigateur est
au premier plan.

    POST /browser/tab    {"browser", "url", "title", "incognito", "tab_id"}
    GET  /browser/ping   test depuis la page d'options de l'extension

SECURITE
--------
- 127.0.0.1 : rien n'est joignable depuis le reseau.
- Jeton partage (en-tete X-Chronicle-Token), recopie dans les options de
  l'extension.
- Toute requete portant un en-tete Origin http(s) est refusee : une page
  web ne peut pas injecter de fausses visites.
- L'URL brute ne quitte jamais la memoire : elle est nettoyee ici.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pc import schema
from pc.apps import NAVIGATEURS
from pc.tracker.collectors.base import Collector
from pc.tracker.core.clock import utc_now
from pc.tracker.core.events import dedup_key
from pc.tracker.core.spans import Page, make_page

TAILLE_MAX = 16 * 1024


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    collecteur: "BrowserCollector"

    def log_message(self, format_, *args):         # noqa: A002
        """Journal HTTP par defaut coupe : il ecrirait chaque URL."""

    def _repondre(self, code: int, corps: dict) -> None:
        donnees = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(donnees)))
        self.end_headers()
        self.wfile.write(donnees)

    def _autorise(self) -> bool:
        origine = (self.headers.get("Origin") or "").lower()

        if origine.startswith(("http://", "https://")):
            return False

        return self.headers.get("X-Chronicle-Token") == \
            self.collecteur.jeton and bool(self.collecteur.jeton)

    def do_GET(self) -> None:                        # noqa: N802
        if self.path.split("?")[0] != "/browser/ping":
            return self._repondre(404, {"error": "route inconnue"})

        if not self._autorise():
            return self._repondre(403, {"error": "jeton invalide"})

        self._repondre(200, {"ok": True,
                             "device_id": self.collecteur.ctx.device_id})

    def do_POST(self) -> None:                       # noqa: N802
        if self.path.split("?")[0] != "/browser/tab":
            return self._repondre(404, {"error": "route inconnue"})

        if not self._autorise():
            return self._repondre(403, {"error": "jeton invalide"})

        try:
            taille = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._repondre(411, {"error": "Content-Length"})

        if not 0 < taille <= TAILLE_MAX:
            return self._repondre(413, {"error": "taille"})

        try:
            rapport = json.loads(self.rfile.read(taille))
        except ValueError:
            return self._repondre(400, {"error": "json"})

        if not isinstance(rapport, dict):
            return self._repondre(400, {"error": "objet attendu"})

        self.collecteur.recevoir(rapport)
        self._repondre(200, {"ok": True})


class BrowserCollector(Collector):
    name = "browser"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.jeton = ctx.config.browser_token
        self.port = ctx.config.browser_port
        self._serveur: ThreadingHTTPServer | None = None
        self._fil: threading.Thread | None = None
        self.recus = 0

    def recevoir(self, rapport: dict) -> None:
        navigateur = str(rapport.get("browser") or "").lower()

        if navigateur not in NAVIGATEURS or self.ctx.browser is None:
            return

        tab_id = rapport.get("tab_id")
        page = make_page(
            navigateur,
            rapport.get("url") if isinstance(rapport.get("url"), str) else None,
            rapport.get("title") if isinstance(rapport.get("title"), str)
            else None,
            bool(rapport.get("incognito")),
            tab_id if isinstance(tab_id, int) else None,
            self.ctx.config.privacy)

        self.recus += 1
        self.ctx.browser.tab(utc_now(), page, navigateur)

    def _reprendre(self) -> None:
        """La page restee ouverte lors d'un arret brutal, fermee au dernier
        battement de coeur - comme les fenetres (desktop.py)."""
        etat = self.recover()
        fin = self.ctx.previous_heartbeat

        if not etat or fin is None:
            return

        debut = datetime.fromisoformat(etat["start"])
        cle = dedup_key(self.ctx.device_id, "browser_page",
                        schema.format_ts(debut))

        if fin > debut and not self.ctx.outbox.has_dedup_key(cle):
            page = Page(**etat["page"])
            self.ctx.factory.interval("browser_page", "browser_ext", debut,
                                      fin, {**page.payload(),
                                            "end_reason": "recovered",
                                            "input_active_s": None},
                                      historique=True)

    def start(self) -> None:
        self._reprendre()

        if not self.jeton:
            self.log.warning("browser.token vide : extension desactivee")
            self.ctx.status(self.name, "disabled", "browser.token vide")
            return

        handler = type("Handler", (_Handler,), {"collecteur": self})

        try:
            self._serveur = ThreadingHTTPServer(("127.0.0.1", self.port),
                                                handler)
        except OSError as erreur:
            self.log.error("port %d indisponible : %s", self.port, erreur)
            self.ctx.status(self.name, "unavailable",
                            f"port {self.port} : {erreur}")
            return

        self._serveur.daemon_threads = True
        self._fil = threading.Thread(target=self._serveur.serve_forever,
                                     name="browser", daemon=True)
        self._fil.start()
        self.log.info("extension navigateur : ecoute sur 127.0.0.1:%d",
                      self.port)

    def stop(self, raison: str = "tracker_stop") -> None:
        if self.ctx.browser is not None:
            self.ctx.browser.close(utc_now(), raison)

        if self._serveur is not None:
            self._serveur.shutdown()
            self._serveur.server_close()

    def snapshot(self) -> dict | None:
        return self.ctx.browser.snapshot() if self.ctx.browser else None
