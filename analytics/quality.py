"""Audit de la donnee : est-elle utilisable ? (V3, etape 3)

A ne pas confondre avec health.py (V2), qui surveille la COLLECTE :
"ai-je bien recupere ?". Ici la question est en aval : "ce que j'ai
recupere veut-il dire quelque chose ?".

Une collecte peut etre parfaite - 11/11 endpoints, 0 echec - et produire
une colonne de 30 zeros. health.py sera vert, et la moyenne de cette
colonne vaudra 0,0 sans qu'aucune erreur ne soit levee nulle part.

D'ou l'ordre des etapes : auditer AVANT de decrire, decrire avant de
correler. Une correlation calculee sur une constante ne renvoie meme pas
d'erreur - elle renvoie NaN, qui se glisse discretement dans un tableau.

Les six controles :

    1. metriques declarees mais jamais mesurees
    2. metriques constantes (une seule valeur distincte)
    3. jours partiels aux deux bords de chaque serie
    4. trous dans les series journalieres
    5. taux de valeurs manquantes par colonne de v_daily
    6. fenetre reellement exploitable (l'intersection, pas l'union)

Le controle 6 est le plus important : c'est celui qui donne le vrai "n".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from analytics import load

# Colonnes qui portent l'analyse. C'est leur intersection qui definit la
# fenetre exploitable - pas la plus longue serie de la base.
COLONNES_COEUR = ["sommeil_score", "hrv", "fc_nocturne", "pas", "calories"]

# En dessous, aucune statistique n'est defendable. Le seuil n'est pas
# arbitraire : sous 10 paires, un coefficient de correlation a un
# intervalle de confiance si large qu'il couvre presque [-1, 1].
N_MINIMUM = 10


@dataclass
class Probleme:
    """Un constat, son niveau, et de quoi le verifier soi-meme."""

    niveau: str          # "bloquant" | "attention" | "info"
    titre: str
    details: list[str] = field(default_factory=list)


# --------------------------------------------------------------- controles

def metriques_vides(cat: pd.DataFrame) -> Probleme | None:
    """1. Declarees dans le catalogue, jamais mesurees.

    Pas une erreur en soi : le mapper prevoit des metriques que la montre
    ne produit pas encore (exercise.* tant qu'aucune seance n'est
    enregistree). Ce qui serait une erreur, c'est de les croire presentes.
    """
    vides = cat[cat["mesures"] == 0]

    if vides.empty:
        return None

    return Probleme(
        "info",
        f"{len(vides)} metriques declarees mais jamais mesurees",
        [f"{code:<28} ({ligne.granularity})"
         for code, ligne in vides.iterrows()],
    )


def mesure_unique(cat: pd.DataFrame) -> Probleme | None:
    """2a. Mesuree une seule fois : un etat, pas une serie.

    Taille, poids, VO2max viennent de /physical-info : Polar les renvoie
    comme un etat courant, pas comme un historique. Une seule ligne est
    donc le comportement NORMAL - il ne faut surtout pas la compter comme
    une serie constante (piege 2b), sinon l'audit crie au probleme sur
    des donnees parfaitement saines.

    Elles restent inutilisables en correlation, faute de variation : ce
    sont des constantes de contexte, pas des variables.
    """
    uniques = cat[cat["mesures"] == 1]

    if uniques.empty:
        return None

    return Probleme(
        "info",
        f"{len(uniques)} metriques mesurees une seule fois (etat, pas serie)",
        [f"{code:<28} {ligne.valeur_min:g} {ligne.unit}"
         for code, ligne in uniques.iterrows()],
    )


def metriques_constantes(cat: pd.DataFrame) -> Probleme | None:
    """2b. Mesuree plusieurs fois, toujours la meme valeur.

    Le piege le plus couteux du lot, et le vrai defaut : la montre a bien
    renvoye 30 valeurs, la collecte est verte, la colonne existe - et elle
    ne contient rien. Elle se moyenne sans broncher, se trace sans
    broncher, et fait un trait plat qui ressemble a un resultat.

    En correlation elle est pire : ecart-type nul -> division par zero ->
    NaN, qui ne leve pas d'erreur et disparait dans un tableau.
    """
    repetees = cat[cat["mesures"] >= 2]
    constantes = repetees[repetees["valeurs_distinctes"] <= 1]

    if constantes.empty:
        return None

    return Probleme(
        "bloquant",
        f"{len(constantes)} metriques constantes malgre des mesures repetees",
        [f"{code:<28} {int(ligne.mesures):>5} mesures, toutes a "
         f"{ligne.valeur_min:g} {ligne.unit}"
         for code, ligne in constantes.iterrows()],
    )


def jours_partiels(cat: pd.DataFrame) -> Probleme | None:
    """3. Le premier et le dernier jour de chaque serie sont tronques.

    Une collecte commencee a 14h ne voit pas la matinee. Le total de pas
    du jour est donc juste... pour une demi-journee. Inclure ces jours
    dans une moyenne la tire vers le bas sans que rien ne le signale.

    Regle : bornes exclues de tout calcul agrege. Elles restent dans les
    graphiques, ou l'oeil corrige tout seul.
    """
    instants = cat[(cat["mesures"] > 0) & (cat["granularity"] == "instant")]

    if instants.empty:
        return None

    premiere = instants["premiere"].min()
    derniere = instants["derniere"].max()

    return Probleme(
        "attention",
        "Jours de bord partiels - a exclure des agregats",
        [f"premier jour : {premiere:%Y-%m-%d} (commence a {premiere:%H:%M})",
         f"dernier jour  : {derniere:%Y-%m-%d} (arrete a {derniere:%H:%M})"],
    )


def trous(quotidien: pd.DataFrame) -> Probleme | None:
    """4. Jours sans aucune donnee au milieu d'une serie.

    Rendus visibles par le reindex de load.py : un jour absent du SQL
    devient une ligne de NaN. Sans ce reindex, le trou ne serait pas une
    ligne vide mais une ligne inexistante - et un graphique relierait le
    18 au 22 par un trait droit parfaitement rassurant.
    """
    vides = quotidien.index[quotidien.isna().all(axis=1)]

    if len(vides) == 0:
        return None

    apercu = [f"{jour:%Y-%m-%d}" for jour in vides[:10]]
    if len(vides) > 10:
        apercu.append("...")

    return Probleme(
        "attention",
        f"{len(vides)} jours entierement vides dans la plage",
        apercu,
    )


def manquants(quotidien: pd.DataFrame) -> Probleme | None:
    """5. Taux de NaN par colonne de v_daily.

    Une colonne remplie a 23% n'est pas une colonne : c'est une anecdote.
    Le chiffre sert a decider ce qui entre dans l'analyse, pas a boucher
    les trous - on ne remplace jamais un manquant par une moyenne.
    """
    if quotidien.empty:
        return None

    taux = quotidien.notna().mean().sort_values()
    pauvres = taux[taux < 0.5]

    if pauvres.empty:
        return None

    return Probleme(
        "attention",
        f"{len(pauvres)} colonnes remplies a moins de 50%",
        [f"{colonne:<24} {part:>5.0%} "
         f"({int(round(part * len(quotidien)))}/{len(quotidien)} jours)"
         for colonne, part in pauvres.items()],
    )


def fenetre_exploitable(quotidien: pd.DataFrame) -> Probleme:
    """6. L'intersection des colonnes coeur : le vrai n de l'analyse.

    Le controle qui remet tout a sa place. La base couvre 30 jours ; mais
    un jour n'est analysable que si TOUTES les grandeurs qu'on veut
    croiser y sont presentes. L'intersection est toujours plus courte que
    l'union, souvent beaucoup.

    C'est ce nombre-la qu'il faut ecrire a cote de chaque correlation,
    jamais l'etendue de la base.
    """
    presentes = [c for c in COLONNES_COEUR if c in quotidien.columns]
    complets = quotidien[presentes].dropna()
    n = len(complets)

    details = [f"base            : {len(quotidien)} jours "
               f"({quotidien.index.min():%Y-%m-%d} -> "
               f"{quotidien.index.max():%Y-%m-%d})"]

    if n:
        details.append(f"exploitable     : {n} jours "
                       f"({complets.index.min():%Y-%m-%d} -> "
                       f"{complets.index.max():%Y-%m-%d})")
    else:
        details.append("exploitable     : 0 jour")

    details.append(f"colonnes exigees : {', '.join(presentes)}")

    if n < N_MINIMUM:
        details.append(f"-> n = {n} < {N_MINIMUM} : aucune correlation "
                       f"n'est interpretable. Continuer a collecter.")
        return Probleme("bloquant",
                        f"Fenetre exploitable : {n} jours seulement", details)

    return Probleme("info", f"Fenetre exploitable : {n} jours", details)


def table_aliases() -> Probleme | None:
    """7. (V4) La table food/aliases.py est-elle encore valide ?

    Un referentiel maintenu a la main derive : un code CIQUAL recopie de
    travers, un aliment qui perd sa valeur energetique a la mise a jour
    suivante. La derive est silencieuse - l'alias continue de resoudre,
    vers le mauvais aliment - donc elle se controle mecaniquement.
    """
    try:
        from food.aliases import ALIASES, verifier
    except (ImportError, FileNotFoundError):
        return None

    problemes = verifier()

    if not problemes:
        return Probleme("info",
                        f"Table d'alias alimentaires : {len(ALIASES)} entrees, "
                        f"toutes valides")

    return Probleme("bloquant",
                    f"{len(problemes)} alias alimentaires invalides",
                    problemes)


# ------------------------------------------------- regle partagee : les bords

def bornes_partielles() -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Les deux jours tronques : le premier et le dernier collectes.

    Deduits des metriques 'instant' : ce sont les seules echantillonnees
    en continu, donc les seules capables de dire a quelle heure la
    collecte a reellement commence et fini.
    """
    cat = load.catalogue()
    instants = cat[(cat["mesures"] > 0) & (cat["granularity"] == "instant")]

    if instants.empty:
        return None

    return (pd.Timestamp(instants["premiere"].min()).normalize(),
            pd.Timestamp(instants["derniere"].max()).normalize())


def sans_bords(df: pd.DataFrame) -> pd.DataFrame:
    """Retire les deux jours partiels. A appeler avant tout AGREGAT.

    Un total de pas sur une demi-journee reste un nombre parfaitement
    valide - c'est ce qui le rend dangereux. Il ne se distingue d'une
    vraie journee calme par aucun controle automatique.

    Les graphiques, eux, gardent ces jours : l'oeil corrige tout seul un
    creux en bout de courbe, une moyenne non.
    """
    bornes = bornes_partielles()

    if bornes is None or df.empty:
        return df

    return df.drop(index=[b for b in bornes if b in df.index])


# ------------------------------------------------------------------ sortie

def collecter() -> list[Probleme]:
    """Passe les six controles. Renvoie les constats, dans l'ordre."""
    cat = load.catalogue()
    quotidien = load.daily()

    resultats = [metriques_vides(cat),
                 mesure_unique(cat),
                 metriques_constantes(cat),
                 jours_partiels(cat),
                 trous(quotidien),
                 manquants(quotidien),
                 fenetre_exploitable(quotidien),
                 table_aliases()]

    return [p for p in resultats if p is not None]


MARQUEURS = {"bloquant": "[!]", "attention": "[~]", "info": "[i]"}


def run_quality() -> int:
    """Affiche l'audit. Renvoie le nombre de constats bloquants.

    Meme convention que health.py : le code de sortie est exploitable par
    un script, la sortie texte est pour l'humain.
    """
    problemes = collecter()

    print("=" * 62)
    print("AUDIT QUALITE DES DONNEES")
    print("=" * 62)

    for probleme in problemes:
        print(f"\n{MARQUEURS[probleme.niveau]} {probleme.titre}")
        for ligne in probleme.details:
            print(f"      {ligne}")

    bloquants = sum(1 for p in problemes if p.niveau == "bloquant")

    print("\n" + "-" * 62)
    if bloquants:
        print(f"{bloquants} constat(s) bloquant(s) : "
              f"ne pas conclure sur ces donnees.")
    else:
        print("Aucun blocage - les donnees supportent une analyse.")

    return bloquants
