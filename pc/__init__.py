"""Le PC comme source de Chronicle. Voir docs/PC_TRACKING.md.

    pc/schema.py    le contrat commun a toutes les machines
    pc/apps.py      noms canoniques des applications
    pc/mapper.py    connecteur Chronicle : evenements -> Batch
    pc/tracker/     l'agent qui tourne sur chaque PC

Ce fichier reste vide d'imports : `python -m pc.tracker` doit pouvoir
tourner sur une machine ou Chronicle (SQLAlchemy, .env, PostgreSQL) n'est
pas installe.
"""
