import json
import random
import time
from datetime import datetime, timezone

import requests

from auth.token_manager import load_tokens
from config import RAW_DATA_DIR
from logging_setup import get_logger

logger = get_logger(__name__)

# Codes qui valent la peine d'etre reessayes. Tout le reste est une erreur
# du client (401, 403, 404...) : reessayer produirait exactement la meme
# reponse, en brulant du quota.
RETRYABLE = {429, 500, 502, 503, 504}

MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0      # secondes : 2, 4, 8...
BACKOFF_CAP = 60.0


class PolarClient:
    """
    Communication between the script and Polar.
    Gets user data from Polar
    """
    def __init__(self, access_token : str, user_id : int, timeout : int = 10):
        self.access_token : str = access_token
        self.user_id : int = user_id
        self.base_url = "https://www.polaraccesslink.com/v3"
        self.timeout = timeout
        self.last_envelope: dict | None = None



    def _sleep_before_retry(self, tentative: int, response=None) -> None:
        """Attend avant une nouvelle tentative (backoff exponentiel).

        Deux regles :
        - si le serveur dit combien attendre (Retry-After), on lui obeit ;
        - sinon on double l'attente a chaque echec, plafonnee, avec une
          part aleatoire (jitter).

        Le jitter evite le "troupeau tonitruant" : sans lui, tous les
        clients bloques par un 429 reessaient a la meme seconde et
        provoquent le pic suivant.
        """
        entete = response.headers.get("Retry-After") if response is not None else None

        if entete and entete.isdigit():
            attente = float(entete)
        else:
            attente = min(BACKOFF_BASE ** tentative, BACKOFF_CAP)
            attente += random.uniform(0, attente * 0.25)

        logger.warning("nouvelle tentative dans %.1f s (tentative %d/%d)",
                       attente, tentative, MAX_ATTEMPTS)
        time.sleep(attente)


    def get(self, endpoint: str, params: dict = None):

        # Prepare auth
        headers = {
            "Authorization" : f"Bearer {self.access_token}",
            "Accept": "application/json"
        }

        url = f"{self.base_url}{endpoint}"
        response = None

        for tentative in range(1, MAX_ATTEMPTS + 1):
            try:
                logger.debug("GET %s params=%s", endpoint, params)
                response = requests.get(url, headers=headers,
                                        timeout=self.timeout, params=params)

            # Panne reseau : rien n'a ete recu, donc rien n'a pu etre
            # traite cote serveur. Reessayer est sans risque.
            except (requests.ConnectionError, requests.Timeout) as error:
                if tentative == MAX_ATTEMPTS:
                    logger.error("%s injoignable apres %d tentatives : %s",
                                 endpoint, MAX_ATTEMPTS, error)
                    raise

                logger.warning("%s : %s", endpoint, type(error).__name__)
                self._sleep_before_retry(tentative)
                continue

            if response.status_code in RETRYABLE and tentative < MAX_ATTEMPTS:
                logger.warning("%s a repondu %d", endpoint, response.status_code)
                self._sleep_before_retry(tentative, response)
                continue

            break

        # Return None if request is valid but send no data
        if response.status_code == 204:
            logger.info("%s : 204, aucune donnee nouvelle", endpoint)
            return None

        try :
            # raise error if HTTP status code is : 401, 404,500,..
            response.raise_for_status()

        except requests.HTTPError:
            logger.error("%s a repondu %d : %s", endpoint,
                         response.status_code, response.text[:300])
            raise

        data = response.json()

        # Archiver AVANT tout parsing : les modules polar retirent les
        # enveloppes ("nights", "recharges"...), ici la reponse est intacte.
        #
        # last_envelope garde la meme enveloppe que le fichier ecrit sur
        # disque, pour que la commande collect (V2) puisse l'ecrire en base
        # sans relire le fichier. Valable en mono-thread uniquement : c'est
        # toujours la DERNIERE reponse.
        self.last_envelope = self._archive(endpoint, params, data)

        # Return the json response in a python dict
        return data

    def _archive(self, endpoint: str, params: dict, data) -> dict:
        """Ecrit la reponse brute dans data/raw/.

        Polar efface au bout de ~28 jours : ce qui n'est pas sur le disque
        a ce moment-la est perdu definitivement.

        La reponse est enveloppee avec sa provenance (endpoint, params,
        date de collecte). Sans les params, un fichier continuous-heart-rate
        ne dit pas quelle plage de dates il couvre : la donnee brute seule
        n'est pas interpretable.

        Un echec d'ecriture ne doit pas faire perdre les donnees deja en
        memoire : on avertit et on continue.
        """
        now = datetime.now(timezone.utc)
        name = endpoint.strip("/").replace("/", "_")

        # Millisecondes incluses : deux appels au meme endpoint dans la
        # meme seconde (plages de dates differentes) ne doivent pas
        # s'ecraser l'un l'autre.
        stamp = now.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"

        enveloppe = {
            "endpoint": endpoint,
            "params": params,
            "fetched_at": now.isoformat(),
            "data": data,
        }

        try:
            RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

            with open(RAW_DATA_DIR / f"{name}_{stamp}.json", "w",
                      encoding="utf-8") as raw_file:
                json.dump(enveloppe, raw_file, indent=2, ensure_ascii=False)

        except OSError as error:
            logger.error("archivage impossible pour %s : %s", name, error)

        return enveloppe

    def get_for_user(self, sub_path : str = ""):
        endpoint = "/users/" + str(self.user_id) + sub_path
        return self.get(endpoint)

if __name__ == "__main__":
    tokens = load_tokens()

    client = PolarClient(tokens["access_token"],tokens["x_user_id"])

    response = client.get_for_user()
    print(response)