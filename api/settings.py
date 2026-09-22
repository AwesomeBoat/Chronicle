"""Reglages de l'API : adresse, port, cle.

    CHRONICLE_API_HOST   127.0.0.1 par defaut : la machine seule.
                         0.0.0.0 pour accepter les autres PC du reseau
                         local (le poste Omarchy).
    CHRONICLE_API_PORT   8780 (8000 est pris par le rappel OAuth de Polar,
                         8765 par le recepteur Kindle)
    CHRONICLE_API_KEY    sinon generee au premier lancement et gardee dans
                         data/api_key.txt (hors git)

Une cle generee vaut mieux qu'une valeur par defaut : il n'existe aucun
instant ou l'API ecoute avec un mot de passe ecrit dans le code source.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from config import BASE_DIR

HOST = os.getenv("CHRONICLE_API_HOST", "127.0.0.1")
PORT = int(os.getenv("CHRONICLE_API_PORT", "8780"))

# Plafond dur par requete : un bug cote tracker ne doit pas pouvoir
# pousser un corps de 2 Go. Le tracker envoie 500 par defaut.
MAX_EVENTS = int(os.getenv("CHRONICLE_API_MAX_EVENTS", "5000"))

KEY_FILE = BASE_DIR / "data" / "api_key.txt"


def api_key(fichier: Path = KEY_FILE) -> str:
    """La cle d'API : variable d'environnement, fichier, ou nouvelle."""
    depuis_env = os.getenv("CHRONICLE_API_KEY", "").strip()

    if depuis_env:
        return depuis_env

    if fichier.exists():
        existante = fichier.read_text(encoding="utf-8").strip()

        if existante:
            return existante

    fichier.parent.mkdir(parents=True, exist_ok=True)
    nouvelle = secrets.token_urlsafe(24)
    fichier.write_text(nouvelle, encoding="utf-8")
    return nouvelle
