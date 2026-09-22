"""Agent de collecte d'activite PC pour Chronicle.

Tourne sur chaque machine (Windows aujourd'hui, Linux en phase 6), produit
des evenements au format de pc/schema.py, les garde dans une file locale
et les envoie par lots a l'API d'ingestion de Chronicle.

Ne depend JAMAIS de `database`, `config` ni SQLAlchemy : la machine qui
l'execute n'a pas forcement Chronicle installe.

    python -m pc.tracker --help
"""

VERSION = "0.1.0"
