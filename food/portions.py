"""« 1 banane » -> combien de grammes ? (V4, etape 3)

Le chainon que CIQUAL ne fournit pas. La table de l'ANSES donne des
valeurs pour 100 g et s'arrete la : elle ne dit nulle part ce que pese
une banane. Or personne n'ecrit "118 g de banane" dans son journal - on
ecrit "1 banane".

Ce fichier comble ce trou, et il faut voir ce qu'il est vraiment :
**une source d'erreur assumee et documentee**. Une banane pese entre 90
et 200 g. Retenir 120 g, c'est accepter +/- 30% sur cette ligne.

C'est un compromis, pas un defaut. L'alternative serait de peser chaque
aliment, ce qui garantit qu'on tiendra le journal trois jours. Une
mesure approximative mais reguliere vaut mieux qu'une mesure exacte
qu'on abandonne - a condition de savoir laquelle on a.

D'ou la regle : chaque quantite issue de cette table est **marquee comme
estimee** dans le payload de l'episode. Une pesee reelle ("120g banane")
ne l'est pas. On pourra donc, plus tard, mesurer combien du total d'une
journee repose sur des estimations.

Les valeurs viennent des portions usuelles CIQUAL/CALNUT et de bon sens
menager. Elles sont a ajuster : c'est un fichier fait pour etre edite.
"""

from __future__ import annotations

from food.ciqual import normaliser

# Poids moyen d'une unite, en grammes.
#
# Les clefs sont normalisees (sans accent, minuscules) au chargement :
# les ecrire lisiblement ici, la normalisation s'occupe du reste.
UNITES = {
    # --- fruits
    "banane": 120,          # pulpe seule, epluchee
    "pomme": 150,
    "poire": 150,
    "orange": 150,
    "clementine": 70,
    "kiwi": 75,
    "peche": 130,
    "abricot": 45,
    "prune": 35,
    "fraise": 12,
    "raisin": 5,            # un grain
    "avocat": 150,          # chair seule
    "citron": 100,

    # --- feculents et pain
    "tranche de pain": 30,
    "tranche de pain de mie": 25,
    "baguette": 250,
    "biscotte": 8,
    "pomme de terre": 150,
    "patate douce": 200,

    # --- proteines
    "oeuf": 55,             # un oeuf moyen, sans coquille
    "blanc de poulet": 150,
    "steak": 120,
    "tranche de jambon": 40,
    "boite de thon": 110,   # poids egoutte

    # --- laitages
    "yaourt": 125,
    "pot de yaourt": 125,
    "tranche de fromage": 25,
    "verre de lait": 200,

    # --- divers
    "poignee": 30,          # fruits secs, oleagineux
    "carre de chocolat": 6,
    "gousse d ail": 5,
    "oignon": 110,
    "tomate": 120,
    "carotte": 90,
    "courgette": 200,
}

# Unites de volume et de cuisine, en grammes.
#
# Approximation deliberee : 1 ml = 1 g. Vrai pour l'eau, faux pour
# l'huile (0,92) et le miel (1,4). L'erreur reste sous les 10% et ne
# justifie pas de trainer une densite par aliment.
MESURES = {
    "g": 1.0,
    "gr": 1.0,
    "gramme": 1.0,
    "grammes": 1.0,
    "kg": 1000.0,
    "ml": 1.0,
    "cl": 10.0,
    "dl": 100.0,
    "l": 1000.0,
    "litre": 1000.0,

    # Cuilleres : valeurs conventionnelles, pour des ingredients secs
    # ou pateux. Une cuillere a soupe rase de farine ne pese pas une
    # cuillere bombee de miel.
    "cs": 15.0,
    "cuillere a soupe": 15.0,
    "cuilleres a soupe": 15.0,
    "c a soupe": 15.0,
    "cc": 5.0,
    "cuillere a cafe": 5.0,
    "cuilleres a cafe": 5.0,
    "c a cafe": 5.0,

    "bol": 250.0,
    "assiette": 300.0,
    "verre": 200.0,
    "tasse": 150.0,
    "portion": 100.0,
    "tranche": 30.0,
}

_UNITES = {normaliser(nom): poids for nom, poids in UNITES.items()}
_MESURES = {normaliser(nom): poids for nom, poids in MESURES.items()}


def poids_mesure(unite: str) -> float | None:
    """Grammes pour une unite de mesure ("g", "cl", "cs"). None sinon."""
    return _MESURES.get(normaliser(unite))


def poids_unite(aliment: str) -> tuple[float, str] | None:
    """Poids d'UNE unite de cet aliment. Renvoie (grammes, clef utilisee).

    Cherche du plus specifique au plus general : "tranche de pain de
    mie" doit l'emporter sur "tranche de pain", qui doit l'emporter sur
    "tranche". Sans cet ordre, la premiere clef qui correspond gagne, et
    c'est presque toujours la plus courte donc la plus fausse.
    """
    besoin = normaliser(aliment)

    if besoin in _UNITES:
        return _UNITES[besoin], besoin

    # Clefs les plus longues d'abord = les plus specifiques d'abord.
    for clef in sorted(_UNITES, key=len, reverse=True):
        if clef in besoin or besoin in clef:
            return _UNITES[clef], clef

    return None
