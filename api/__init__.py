"""API d'ingestion de Chronicle : les sources qui POUSSENT.

Jusqu'ici Chronicle allait chercher ses donnees (Polar, capteur) ou les
lisait sur disque (journal, Kindle). Le PC est la premiere source qui
envoie elle-meme, depuis plusieurs machines, en continu.

    POST /api/v1/events     un lot d'evenements (enveloppe commune)
    GET  /health            etat, sans authentification
    GET  /api/v1/devices    machines connues et fraicheur

Le protocole est celui du Data Lake de PhoneTracker (docs/SYNC-PROTOCOL.md)
: meme enveloppe, memes regles de rejeu. Voir docs/PC_TRACKING.md.

    python main.py serve
"""
