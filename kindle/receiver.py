"""Recepteur HTTP des envois de la Kindle.

La liseuse televerse ici quand on appuie sur "Envoyer au PC" dans sa
bibliotheque. Ce module ECRIT DES FICHIERS, un point c'est tout.

POURQUOI IL NE TOUCHE PAS A LA BASE
-----------------------------------
Il serait tentant d'ecrire directement en PostgreSQL a la reception. Ce
serait une erreur, pour la meme raison qui a fait choisir un tampon dans
sensors/bedroom.py : le conteneur Docker n'est pas toujours demarre.

En separant les deux gestes :

    recevoir  ->  ecrire des fichiers      marche toujours
    ingerer   ->  rejouer ces fichiers     quand la base est la

...un envoi fait Docker eteint n'est pas perdu, il attend sur le disque.
C'est exactement le role que data/raw/ joue depuis la V1 pour Polar.

Consequence secondaire mais utile : ce module n'important ni SQLAlchemy
ni database.*, la frontiere verifiee par connectors.base reste nette.

PROTOCOLE
---------
    GET  /ping             -> "pong"   (la liseuse teste la connectivite)
    PUT  /u/<token>/<nom>  -> depose un fichier
    POST /done/<token>     -> cloture l'instantane et declenche le rappel

Le token est un secret partage, pas une authentification serieuse : le
recepteur n'ecoute que sur le reseau local et ne recoit que des fichiers
dont il connait la liste. Ajouter TLS ici serait du ceremonial sans
risque couvert - meme raisonnement que pour le capteur de chambre.
"""

from __future__ import annotations

import datetime as dt
import http.server
import os
import socket
import socketserver
import threading
from typing import Callable

# Les seuls noms acceptes. Tout autre nom est refuse : un recepteur qui
# ecrit un fichier arbitraire choisi par le client est une porte ouverte.
FICHIERS_ATTENDUS = {"fmcache.db", "vocab.db", "annotations.db", "catalog.txt"}

# Garde-fou : les trois bases pesent moins d'un Mo en pratique.
TAILLE_MAX = 64 * 1024 * 1024

# Etat partage du serveur. Un dict plutot qu'un global par valeur : le
# handler est instancie par requete, il ne peut pas porter l'etat.
CONFIG: dict = {"token": "kindle", "dossier": None, "rappel": None,
                "journal": print, "dossier_courant": None}

_verrou = threading.Lock()


def ip_locale() -> str:
    """L'IP de cette machine sur le reseau local, pour l'afficher au demarrage."""
    try:
        prise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        prise.connect(("8.8.8.8", 80))
        adresse = prise.getsockname()[0]
        prise.close()

        return adresse

    except OSError:
        return "127.0.0.1"


def _journaliser(message: str) -> None:
    horodatage = dt.datetime.now().strftime("%H:%M:%S")
    CONFIG["journal"](f"[{horodatage}] {message}")


def _dossier_courant() -> str:
    """Le dossier de l'instantane en cours, cree au premier fichier recu."""
    if CONFIG["dossier_courant"] is None:
        estampille = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        chemin = os.path.join(CONFIG["dossier"], estampille)
        os.makedirs(chemin, exist_ok=True)
        CONFIG["dossier_courant"] = chemin

    return CONFIG["dossier_courant"]


class Handler(http.server.BaseHTTPRequestHandler):
    """Trois routes, aucune surprise."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format_, *args):
        """Coupe le journal par defaut : on a le notre."""

    def _repondre(self, code: int, corps: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()

        try:
            self.wfile.write(corps)
        except OSError:
            # La liseuse coupe parfois la connexion des qu'elle a son code.
            pass

    def _segments(self) -> list[str]:
        return [s for s in self.path.split("?")[0].split("/") if s]

    def do_GET(self) -> None:
        if self._segments()[:1] == ["ping"]:
            _journaliser(f"ping de {self.client_address[0]}")
            self._repondre(200, b"pong")
        else:
            self._repondre(404, b"route inconnue")

    def do_PUT(self) -> None:
        segments = self._segments()

        if len(segments) != 3 or segments[0] != "u":
            return self._repondre(404, b"route inconnue")

        if segments[1] != CONFIG["token"]:
            _journaliser(f"token refuse depuis {self.client_address[0]}")
            return self._repondre(403, b"token invalide")

        nom = os.path.basename(segments[2])

        if nom not in FICHIERS_ATTENDUS:
            return self._repondre(400, f"fichier inattendu : {nom}".encode())

        try:
            taille = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._repondre(411, b"Content-Length requis")

        if taille <= 0 or taille > TAILLE_MAX:
            return self._repondre(413, b"taille invalide")

        with _verrou:
            cible = os.path.join(_dossier_courant(), nom)

        reste = taille

        with open(cible, "wb") as fichier:
            while reste > 0:
                morceau = self.rfile.read(min(65536, reste))

                if not morceau:
                    break

                fichier.write(morceau)
                reste -= len(morceau)

        if reste:
            _journaliser(f"! {nom} incomplet, {reste} octets manquants")
            return self._repondre(400, b"televersement incomplet")

        _journaliser(f"recu {nom:<16} {taille / 1024:8.1f} Ko")
        self._repondre(200, b"ok")

    def do_POST(self) -> None:
        segments = self._segments()

        if len(segments) != 2 or segments[0] != "done":
            return self._repondre(404, b"route inconnue")

        if segments[1] != CONFIG["token"]:
            return self._repondre(403, b"token invalide")

        # Vider le corps meme s'il est vide : sans ca, HTTP/1.1 garde la
        # connexion desynchronisee pour la requete suivante.
        try:
            taille = int(self.headers.get("Content-Length", 0) or 0)

            if taille:
                self.rfile.read(taille)

        except ValueError:
            pass

        with _verrou:
            dossier = CONFIG["dossier_courant"]
            CONFIG["dossier_courant"] = None

        if dossier is None:
            return self._repondre(400, b"aucun fichier recu")

        _journaliser(f"instantane clos : {dossier}")

        rappel: Callable[[str], str] | None = CONFIG["rappel"]

        if rappel is None:
            return self._repondre(200, b"ok")

        try:
            resultat = rappel(dossier)
            self._repondre(200, str(resultat).encode("utf-8", "replace"))

        except Exception as erreur:                  # noqa: BLE001
            # Une ingestion qui echoue ne doit PAS faire perdre l'envoi :
            # les fichiers restent sur le disque et seront rejoues par
            # "python main.py kindle".
            _journaliser(f"! ingestion impossible : {erreur}")
            _journaliser(f"  fichiers conserves dans {dossier}")
            self._repondre(500, str(erreur).encode("utf-8", "replace"))


class _Serveur(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def servir(dossier: str, token: str, port: int,
           rappel: Callable[[str], str] | None = None,
           journal: Callable[[str], None] = print) -> None:
    """Ecoute jusqu'a Ctrl+C. `rappel(dossier)` est appele a chaque envoi."""
    os.makedirs(dossier, exist_ok=True)

    CONFIG.update({"token": token, "dossier": dossier, "rappel": rappel,
                   "journal": journal, "dossier_courant": None})

    adresse = ip_locale()

    journal("=" * 64)
    journal("  Recepteur Kindle")
    journal(f"  Adresse   : http://{adresse}:{port}")
    journal(f"  Instantanes : {dossier}")
    journal("")
    journal("  Dans 'Envoyer au PC.sh' sur la liseuse :")
    journal(f"      PC_HOST={adresse}")
    journal(f"      PC_PORT={port}")
    journal(f"      TOKEN={token}")
    journal("")
    journal("  Ctrl+C pour arreter.")
    journal("=" * 64)

    try:
        _Serveur(("0.0.0.0", port), Handler).serve_forever()

    except KeyboardInterrupt:
        journal("Arret.")
