"""Journal alimentaire -> Batch. (V4, etape 5)

Le pendant exact de polar/mapper.py, pour une source qui n'a ni API, ni
JSON, ni horodatage automatique. Si ce fichier tient en produisant les
memes objets que celui de Polar, la promesse de la V1 est verifiee.

CE QUI PRODUIT QUOI
-------------------
Un repas est **a la fois** un point et un evenement, et le schema de la
V1 sait dire les deux :

- `observation` : une ligne par nutriment, horodatee a l'heure du repas.
  C'est ce qui se cumule, se moyenne, se correle - la meme forme que la
  frequence cardiaque ou les pas.

- `episode(kind="meal")` : le repas lui-meme, avec dans son payload la
  ligne EXACTE telle qu'ecrite et le detail de chaque aliment reconnu.
  C'est la tracabilite : dans six mois, "241 kcal a 12h30" ne veut rien
  dire, "100g riz cuit + 55g poulet + 50g brocoli" si.

L'episode a une duree nulle (ended_at = started_at). Un repas dure, mais
sa duree n'est pas mesuree - inventer 20 minutes serait une donnee
fabriquee. Le schema autorise ended_at = started_at, on s'en tient a ce
qu'on sait.

CE QU'IL NE FAUT PAS FAIRE
--------------------------
Le total journalier n'est PAS calcule ici et n'est PAS stocke. Il se
deduit des observations par une vue (v_nutrition, etape 7). Le stocker
creerait deux verites qui divergeraient des la premiere correction du
journal.
"""

from __future__ import annotations

from datetime import date

from database.records import (Batch, EpisodeRecord, MetricSpec,
                              ObservationRecord)
from food import journal
from food.parser import Repas, analyser, nutriments

SOURCE_CODE = "food_journal"
SOURCE_LABEL = "Journal alimentaire"

# Les metriques declarees par ce connecteur.
#
# Prefixe "food." : le catalogue `metric` est partage entre sources (V1),
# et "energy" tout court entrerait un jour en collision avec la depense
# energetique mesuree par la montre. Le prefixe evite d'avoir a arbitrer
# plus tard entre deux sens du meme mot.
#
# Granularite "instant" : une prise alimentaire est un point dans le
# temps. L'agregat par jour est l'affaire d'une vue, pas d'une metrique.
METRICS = [
    MetricSpec("food.energy_kcal", "kcal", "instant",
               "Energie ingeree (CIQUAL, reglement UE 1169/2011)"),
    MetricSpec("food.protein_g", "g", "instant", "Proteines ingerees"),
    MetricSpec("food.carb_g", "g", "instant", "Glucides ingeres"),
    MetricSpec("food.sugar_g", "g", "instant", "Sucres ingeres"),
    MetricSpec("food.fat_g", "g", "instant", "Lipides ingeres"),
    MetricSpec("food.satfat_g", "g", "instant", "Acides gras satures"),
    MetricSpec("food.fiber_g", "g", "instant", "Fibres alimentaires"),
    MetricSpec("food.salt_g", "g", "instant", "Sel (chlorure de sodium)"),
    MetricSpec("food.water_g", "g", "instant", "Eau apportee par les aliments"),
    MetricSpec("food.alcohol_g", "g", "instant", "Alcool"),
    MetricSpec("food.mass_g", "g", "instant", "Masse totale ingeree"),
]


def _repas_vers_batch(repas: Repas) -> Batch:
    """Un repas analyse -> observations + episode."""
    lot = Batch()

    if repas.moment is None or not repas.elements:
        return lot

    totaux: dict[str, float] = {}
    detail = []

    for element in repas.elements:
        valeurs = nutriments(element)

        for cle, valeur in valeurs.items():
            totaux[cle] = totaux.get(cle, 0.0) + valeur

        detail.append({
            "ecrit": element.texte,
            "aliment": element.ciqual_nom,
            "ciqual_code": element.ciqual_code,
            "grammes": element.grammes,
            "poids_estime": element.estime,
            "note": element.note,
            "kcal": valeurs.get("energy_kcal"),
        })

    # --- les observations
    for cle, valeur in totaux.items():
        lot.observations.append(ObservationRecord(
            metric_code=f"food.{cle}",
            observed_at=repas.moment,
            value=round(valeur, 3),
        ))

    # --- l'episode, porteur de la tracabilite
    #
    # "poids_estime" au niveau du repas : vrai des qu'UN aliment a ete
    # pese a la louche. C'est ce drapeau qui permettra de dire quelle
    # part d'une journee repose sur des estimations - une question de
    # qualite de donnee, exactement comme les sentinelles de Polar.
    lot.episodes.append(EpisodeRecord(
        kind="meal",
        started_at=repas.moment,
        ended_at=repas.moment,
        payload={
            "ligne": repas.ligne,
            "elements": detail,
            "kcal": round(totaux.get("energy_kcal", 0.0), 1),
            "poids_estime": any(e.estime for e in repas.elements),
        },
    ))

    return lot


def map_jour(jour: date) -> tuple[Batch, list[Repas]]:
    """Analyse le journal d'un jour. Renvoie (batch, repas analyses).

    Les repas sont renvoyes en plus du batch pour que l'appelant puisse
    signaler les refus. Un batch silencieusement ampute serait le
    contraire de ce que ce connecteur promet.
    """
    lot = Batch()
    analyses = []

    for ligne in journal.lire(jour):
        repas = analyser(ligne, jour)
        analyses.append(repas)

        # Une ligne partiellement comprise n'entre PAS en base, meme
        # pour ses elements valides : un repas ampute de son riz est
        # plus trompeur qu'un repas absent.
        if repas.complet:
            lot.extend(_repas_vers_batch(repas))

    return lot, analyses
