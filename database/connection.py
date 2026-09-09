"""Connexion a PostgreSQL : engine, sessions, verification.

Trois objets, trois roles distincts :

- engine   : le pool de connexions. UN seul pour tout le programme. Ouvrir
             une connexion TCP + authentification coute ~10 ms ; le pool
             les garde ouvertes et les prete.
- Session  : une unite de travail (un ensemble de requetes + un commit).
             Courte duree de vie, jetable, JAMAIS partagee entre threads.
- session_scope() : garantit le commit en cas de succes, le rollback en cas
             d'erreur, et la fermeture dans tous les cas.
"""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL, SQL_ECHO

# pool_pre_ping : avant de preter une connexion, le pool envoie un "SELECT 1".
# Sans ca, une connexion tuee entre-temps (conteneur redemarre, timeout
# reseau) remonte une erreur au premier vrai appel, plusieurs minutes apres.
#
# pool_size / max_overflow : 5 connexions permanentes, 5 de plus en pointe.
# Un script mono-thread n'en utilise qu'une ; les valeurs comptent en V2
# quand la collecte tournera en parallele.
#
# Pas de future=True : en SQLAlchemy 2.0 tous les engines sont "future",
# le parametre n'existe plus que pour compatibilite et sera supprime.
engine: Engine = create_engine(
    DATABASE_URL,
    echo=SQL_ECHO,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
)

# sessionmaker = une usine a sessions preconfigurees, pas une session.
# expire_on_commit=False : sans ca, lire un attribut d'un objet apres le
# commit declenche un nouveau SELECT (et plante si la session est fermee).
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Ouvre une session, commit si tout va bien, rollback sinon.

        with session_scope() as session:
            session.add(objet)
        # commit automatique ici

    Le rollback est le point important : sans lui, une erreur au milieu
    d'un lot laisserait une transaction a moitie ecrite et une connexion
    bloquee dans l'etat "idle in transaction".

    SQLAlchemy fournit l'equivalent tout fait depuis la 1.4 :
        with SessionLocal.begin() as session: ...
    La version explicite est gardee ici parce qu'elle montre les trois
    temps (commit / rollback / close) que la version courte cache.
    """
    session = SessionLocal()

    try:
        yield session
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def dispose_engine() -> None:
    """Ferme proprement les connexions du pool.

    Sans cet appel en fin de programme, les connexions du pool sont
    ramassees par le garbage collector a l'arret de l'interpreteur, et
    psycopg le signale :
        ResourceWarning: <Connection> was deleted while still open
    Sans consequence pour un script, mais c'est le genre de fuite qui
    finit par saturer le "max_connections" du serveur en V2.
    """
    engine.dispose()


def check_connection() -> bool:
    """Verifie que le serveur repond. Renvoie True/False, ne leve rien.

    "SELECT 1" est la requete de test universelle : elle ne touche aucune
    table et ne peut echouer que si la connexion elle-meme est en cause.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    except OperationalError as error:
        print(f"[db] connexion impossible : {error.orig}")
        return False


def server_version() -> str:
    """Version du serveur PostgreSQL, pour la commande status."""
    with engine.connect() as connection:
        return connection.execute(text("SHOW server_version")).scalar_one()


def create_tables() -> None:
    """Cree les tables manquantes (DDL genere a partir de models.py).

    create_all ne fait que CREATE TABLE IF NOT EXISTS : il cree ce qui
    manque, mais ne MODIFIE jamais une table existante. Changer un type
    de colonne demande de recreer la table (ou un outil de migration type
    Alembic, hors sujet en V1).
    """
    from database.models import Base
    from database.views import create_views

    Base.metadata.create_all(engine)

    # Les vues sont recreees a chaque initdb (CREATE OR REPLACE) : elles
    # vivent dans le code, pas dans la base. Une vue tapee a la main dans
    # pgAdmin disparaitrait au premier resetdb et n'existerait sur aucune
    # autre machine.
    with engine.begin() as connection:
        create_views(connection)


def drop_tables() -> None:
    """Supprime toutes les tables du modele. Destructif, sur demande."""
    from database.models import Base
    from database.views import drop_views

    # Les vues d'abord : PostgreSQL refuse de supprimer une table dont une
    # vue depend encore.
    with engine.begin() as connection:
        drop_views(connection)

    Base.metadata.drop_all(engine)


def table_counts() -> dict[str, int]:
    """Nombre de lignes par table - le juge de paix de l'idempotence."""
    from database.models import Base

    counts: dict[str, int] = {}

    with engine.connect() as connection:
        for name in Base.metadata.tables:
            counts[name] = connection.execute(
                text(f"SELECT count(*) FROM {name}")).scalar_one()

    return counts
