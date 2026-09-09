"""Capteur temperature / humidite de la chambre. (V4, etape 6)

Materiel vise : un ESP32 + un BME280 (~15 EUR), pose dans la chambre.

POURQUOI TIRER, ET NON RECEVOIR
-------------------------------
Le reflexe serait de faire emettre le module vers le PC. C'est un piege
dans ce projet precis : **le PC n'est pas allume en permanence**. Un
capteur qui pousse vers un serveur absent perd ses mesures, et c'est
justement la nuit - quand le PC dort - que les donnees comptent.

Le module garde donc ses dernieres heures en memoire (tampon circulaire)
et c'est `main.py collect` qui vient les chercher. Consequences :

- le PC peut rester eteint trois jours sans rien perdre, tant que la
  duree du tampon depasse l'absence ;
- le curseur `sync_state` de la V2 fonctionne tel quel, sans une ligne
  de code nouvelle : la logique "depuis quand ai-je des donnees" est
  exactement la meme que pour Polar ;
- aucun port a ouvrir sur le PC, aucun service a laisser tourner.

Le prix a payer est reel et il faut le connaitre : **si le module
redemarre, son tampon est perdu**. Un ESP32 alimente sur secteur
redemarre rarement, mais une coupure de courant efface la nuit. Un
stockage sur carte SD leverait la limite ; ce n'est pas fait.

LE PROTOCOLE, VOLONTAIREMENT MINIMAL
------------------------------------
Le module expose une seule route HTTP en JSON :

    GET http://<ip>/readings?since=<epoch>

    {"device": "bedroom", "now": 1756208400, "readings": [
        {"t": 1756208100, "temp": 20.4, "hum": 54.2, "pres": 1013.2},
        ...
    ]}

Pas de TLS, pas d'authentification : le module vit sur le reseau local
et ne publie qu'une temperature. Ajouter de l'OAuth ici serait du
ceremonial sans risque couvert. C'est un choix, et il tient tant que le
capteur reste dans le LAN.

`since` est envoye par le collecteur depuis le curseur : le module ne
renvoie que ce qui manque. Le firmware de reference est dans
`sensors/firmware/bedroom_esp32.ino`.

HORODATAGE : LE PIEGE
---------------------
Un ESP32 n'a pas d'horloge sauvegardee. S'il n'a pas pu joindre un
serveur NTP, il compte a partir du 1er janvier 1970. Une mesure datee de
1970 dans une base temporelle passe tous les controles d'insertion et
ruine toute analyse.

Ce module refuse donc les horodatages hors d'une plage plausible, plutot
que de les corriger : une mesure mal datee est une mesure perdue, pas
une mesure a rapiecer.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from database.records import Batch, MetricSpec, ObservationRecord

SOURCE_CODE = "bedroom_sensor"
SOURCE_LABEL = "Capteur chambre (ESP32 + BME280)"

# Le flux, au sens du curseur sync_state. Une chaine libre : pour Polar
# c'est un chemin d'URL, ici c'est un nom. Le curseur ne s'en soucie pas,
# ce qui est precisement la preuve qu'il etait bien concu.
ENDPOINT = "bedroom/readings"

METRICS = [
    MetricSpec("bedroom.temperature", "degC", "instant",
               "Temperature de la chambre"),
    MetricSpec("bedroom.humidity", "%", "instant",
               "Humidite relative de la chambre"),
    MetricSpec("bedroom.pressure", "hPa", "instant",
               "Pression atmospherique"),
]

# Correspondance clef JSON -> metrique. Les clefs sont courtes parce
# qu'elles voyagent depuis un microcontroleur : chaque octet compte dans
# un tampon de quelques kilo-octets.
CHAMPS = {
    "temp": "bedroom.temperature",
    "hum": "bedroom.humidity",
    "pres": "bedroom.pressure",
}

# Bornes de plausibilite. Une chambre n'est ni a -40 ni a +60 degres ; en
# sortir signale un capteur debranche ou en panne, pas une canicule.
PLAGES = {
    "bedroom.temperature": (-10.0, 50.0),
    "bedroom.humidity": (0.0, 100.0),
    "bedroom.pressure": (850.0, 1100.0),
}

# Un horodatage anterieur a cette date vient d'un module qui n'a pas
# synchronise son horloge. 2026-01-01, arbitraire mais sur.
EPOCH_MINIMUM = 1767225600

TIMEOUT = 10


class SensorError(Exception):
    """Le module n'a pas repondu, ou a repondu autre chose que prevu."""


def lire(base_url: str, depuis: datetime | None = None) -> dict:
    """Interroge le module. Renvoie sa reponse JSON brute.

    Aucun retry ici : `collector.py` (V2) sait deja reessayer, et
    dupliquer cette logique creerait deux politiques de reprise
    divergentes. Un capteur injoignable est un incident normal - le
    module est peut-etre debranche - il remonte tel quel.
    """
    params = {}

    if depuis is not None:
        params["since"] = int(depuis.timestamp())

    try:
        reponse = requests.get(f"{base_url.rstrip('/')}/readings",
                               params=params, timeout=TIMEOUT)
        reponse.raise_for_status()
        return reponse.json()

    except requests.RequestException as erreur:
        raise SensorError(f"module injoignable ({base_url}) : {erreur}")
    except ValueError as erreur:
        raise SensorError(f"reponse illisible : {erreur}")


def mapper(charge: dict) -> tuple[Batch, list[str]]:
    """JSON du module -> Batch. Renvoie (batch, anomalies).

    Les anomalies ne sont pas levees en exception : une mesure aberrante
    au milieu de 300 bonnes ne doit pas faire perdre les 299 autres.
    Elles sont ecartees et remontees, comme les sentinelles de Polar.
    """
    lot = Batch()
    anomalies: list[str] = []
    releves = charge.get("readings", [])

    if not isinstance(releves, list):
        raise SensorError("champ 'readings' absent ou mal forme")

    for releve in releves:
        epoch = releve.get("t")

        if not isinstance(epoch, (int, float)):
            anomalies.append(f"horodatage absent : {releve}")
            continue

        # Le piege de l'horloge non synchronisee. On refuse, on ne
        # rapiece pas : une mesure mal datee est inexploitable, la
        # replacer "a peu pres" fabriquerait une donnee.
        if epoch < EPOCH_MINIMUM:
            anomalies.append(
                f"horodatage {datetime.fromtimestamp(epoch, timezone.utc):%Y-%m-%d}"
                f" anterieur a 2026 - horloge du module non synchronisee")
            continue

        instant = datetime.fromtimestamp(epoch, timezone.utc)

        for champ, metrique in CHAMPS.items():
            valeur = releve.get(champ)

            if valeur is None:
                continue

            bas, haut = PLAGES[metrique]

            if not (bas <= valeur <= haut):
                anomalies.append(f"{metrique} = {valeur} hors plage "
                                 f"[{bas}, {haut}] a {instant:%Y-%m-%d %H:%M}")
                continue

            lot.observations.append(ObservationRecord(
                metric_code=metrique,
                observed_at=instant,
                value=float(valeur),
            ))

    return lot, anomalies


def simuler(heures: int = 12, pas_minutes: int = 5) -> dict:
    """Fabrique une reponse plausible, sans materiel.

    Sert a deux choses, et il faut les distinguer :

    1. **Developper et tester** le connecteur avant d'avoir le module
       entre les mains - ce qui est le cas aujourd'hui.
    2. **Ne PAS remplir la base.** Ces donnees sont fausses. La commande
       qui les utilise le dit, et elles ne sont jamais inserees sans
       que ce soit explicitement demande.

    La forme suit une journee credible : la chambre se refroidit la nuit
    et se rechauffe l'apres-midi, l'humidite fait l'inverse. C'est assez
    pour verifier qu'une jointure avec le sommeil produit quelque chose
    de lisible, et pas assez pour etre pris au serieux.
    """
    import math

    maintenant = int(time.time())
    debut = maintenant - heures * 3600
    releves = []

    for decalage in range(0, heures * 3600, pas_minutes * 60):
        epoch = debut + decalage
        heure = datetime.fromtimestamp(epoch).hour + \
            datetime.fromtimestamp(epoch).minute / 60

        # Minimum vers 5h, maximum vers 17h : un dephasage de 12 h.
        cycle = math.cos((heure - 17) / 24 * 2 * math.pi)

        releves.append({
            "t": epoch,
            "temp": round(20.5 + 2.5 * cycle, 2),
            "hum": round(52 - 6 * cycle, 2),
            "pres": round(1013 + 1.5 * math.sin(decalage / 40000), 2),
        })

    return {"device": "bedroom-sim", "now": maintenant,
            "readings": releves, "simule": True}
