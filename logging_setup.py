"""Configuration du journal.

Pourquoi remplacer print() : une tache planifiee n'a pas d'ecran. Un print
part dans le vide, et le matin ou la collecte a echoue, il ne reste rien
pour comprendre. Un log ecrit sur disque, horodate, avec un niveau.

Les cinq niveaux, et quand les employer ici :

    DEBUG    detail de mise au point (chaque requete HTTP)
    INFO     deroulement normal ("5 nuits recuperees")
    WARNING  anomalie surmontee ("429, nouvelle tentative dans 4 s")
    ERROR    operation echouee mais programme vivant ("source ignoree")
    CRITICAL le programme ne peut pas continuer ("token expire")

Regle : si un humain doit agir, c'est au moins WARNING. Sinon INFO.

Deux sorties, deux niveaux differents :
    - la console recoit INFO et au-dessus, en format court et lisible ;
    - le fichier recoit tout depuis DEBUG, en format complet.
C'est le reglage qui permet de lancer la commande a la main sans etre noye,
tout en gardant de quoi enqueter apres coup.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "digitaltwin.log"

# 2 Mo par fichier, 5 archives : environ 10 Mo au total, borne fixe. Sans
# rotation, un log de collecte horaire grossit indefiniment - c'est une
# fuite lente, mais c'est une fuite.
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_CONSOLE_FORMAT = "%(levelname)-8s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-22s %(message)s"

_configured = False


def setup_logging(verbose: bool = False) -> None:
    """Installe les handlers sur le logger racine. Idempotent.

    Idempotent parce que configurer deux fois ajoute deux handlers, et
    chaque message s'afficherait en double - un classique du module
    logging.
    """
    global _configured

    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    racine = logging.getLogger()

    # Le logger racine laisse tout passer ; ce sont les handlers qui
    # filtrent. L'inverse (filtrer a la racine) empecherait le fichier de
    # recevoir le DEBUG.
    racine.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))

    fichier = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES,
                                  backupCount=BACKUP_COUNT,
                                  encoding="utf-8")
    fichier.setLevel(logging.DEBUG)
    fichier.setFormatter(logging.Formatter(_FILE_FORMAT))

    racine.addHandler(console)
    racine.addHandler(fichier)

    # urllib3 (sous requests) parle beaucoup en DEBUG : une ligne par
    # connexion reutilisee. Sans ce reglage, le fichier est illisible.
    logging.getLogger("urllib3").setLevel(logging.INFO)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Logger nomme d'apres le module appelant.

    Le nom apparait dans le fichier ("polar.client", "database.repository")
    et permet de regler le niveau module par module. D'ou l'usage :

        logger = get_logger(__name__)
    """
    return logging.getLogger(name)
