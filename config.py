from dotenv import load_dotenv
import os
from pathlib import Path
from urllib.parse import quote_plus

def _raise_error(message: str):
    raise ValueError(f"ERROR {message}")

# Load from .env
load_dotenv()

POLAR_CLIENT = (os.getenv("POLAR_CLIENT_ID") 
                or _raise_error("POLAR_CLIENT not found in .env"))

POLAR_SECRET = (os.getenv("POLAR_CLIENT_SECRET")
                or _raise_error("POLAR_SECRET not found in .env"))

POLAR_REDIRECT_URI = (os.getenv("POLAR_REDIRECT_URI")
                      or _raise_error("POLAR_REDIRECT_URI not found in .env"))


CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "8000"))


# Chemins ancres sur le dossier du projet, pas sur le repertoire courant :
# le script doit marcher meme lance depuis ailleurs (tache planifiee en V2).
BASE_DIR = Path(__file__).parent

TOKEN_FILE = BASE_DIR / "tokens.json"
RAW_DATA_DIR = BASE_DIR / "data" / "raw"


# ----------------------------------------------------------- PostgreSQL

# Les memes variables alimentent docker-compose.yml : une seule source de
# verite. Changer le mot de passe la-bas le change ici, et inversement.
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "digitaltwin")
POSTGRES_USER = os.getenv("POSTGRES_USER", "twin")

# Defaut pour host/port/db/user : ce sont les memes valeurs que celles de
# docker-compose.yml. Les repeter ici est sans risque et rend le projet
# utilisable sans .env complet.
#
# Le mot de passe, lui, leve : un secret ecrit en dur dans un fichier
# versionne est une faute, meme sur une base locale. L'habitude compte
# plus que le risque du jour ou le serveur est sur la machine.
POSTGRES_PASSWORD = (os.getenv("POSTGRES_PASSWORD")
                     or _raise_error("POSTGRES_PASSWORD not found in .env"))

# postgresql+psycopg = dialecte SQL + driver reseau.
# Sans "+psycopg", SQLAlchemy cherche psycopg2 (la v2), absente du projet,
# et echoue sur "ModuleNotFoundError: No module named 'psycopg2'" - une
# erreur d'import, pas une erreur de connexion.
#
# quote_plus sur les trois parties variables : un @ dans le mot de passe
# OU dans l'utilisateur casse la chaine de la meme facon (l'analyseur
# d'URL coupe au dernier @, mais le premier @ decale tout le reste).
#
# SQLAlchemy sait le faire seul via URL.create(...), qui echappe tout et
# masque le mot de passe quand on affiche l'objet. La chaine est ecrite a
# la main ici parce qu'elle rend l'anatomie de l'URL visible.
DATABASE_URL = (
    f"postgresql+psycopg://{quote_plus(POSTGRES_USER)}"
    f":{quote_plus(POSTGRES_PASSWORD)}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{quote_plus(POSTGRES_DB)}"
)

# SQL_ECHO=1 -> SQLAlchemy affiche chaque requete envoyee (debug).
SQL_ECHO = os.getenv("SQL_ECHO", "").strip().lower() in {"1", "true", "yes"}

# Polar renvoie beaucoup d'horodatages sans fuseau ("2026-08-16T22:56:30").
# Ce sont des heures locales de la montre : sans fuseau declare, impossible
# d'en faire un instant absolu.
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Paris")

