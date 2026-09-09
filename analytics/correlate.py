"""Correlations, et surtout : pourquoi ne pas y croire. (V3, etape 6)

Le module le plus dangereux du projet. Calculer un coefficient de
correlation est trivial - pandas le fait en un appel. L'interpreter est
la partie difficile, et tout ce fichier existe pour rendre l'erreur plus
difficile que la prudence.

Quatre pieges, dans l'ordre ou ils font des degats :

1. LE r SEUL NE VEUT RIEN DIRE. Il faut TOUJOURS lire (r, n, p)
   ensemble. r = 0,75 sur 7 points n'est pas plus convaincant que
   r = 0,20 sur 500 - il est moins convaincant. Un r seul dans un
   tableau est une phrase sans verbe.

2. LES COMPARAISONS MULTIPLES. Tester 20 grandeurs deux a deux fait 190
   paires. Au seuil habituel de 5%, une paire sur vingt ressort "par
   hasard" : environ 10 faux positifs, garantis, sur des donnees
   entierement aleatoires. Chercher partout et publier ce qui sort est
   la definition du p-hacking, et c'est involontaire neuf fois sur dix.
   D'ou la correction de Bonferroni ci-dessous.

3. LE TEMPS A UN SENS. "sommeil et pas correlent" ne dit pas qui precede
   quoi. La question interessante est orientee : la nuit d'hier
   influence-t-elle l'activite d'aujourd'hui ? D'ou le parametre
   decalage. Attention : multiplier les decalages multiplie aussi les
   tests, donc le piege n°2.

4. L'AUTOCORRELATION. Deux series qui montent chacune sur la periode
   correleront fortement sans aucun lien entre elles. Sur 7 jours, une
   tendance de fond suffit a fabriquer un r de 0,8. Ce module signale la
   tendance, il ne sait pas la retirer.

5. LES VARIABLES DERIVEES. Piege decouvert en lisant le premier tableau
   produit par ce module : ses cinq meilleurs resultats etaient
   pas ~ calories_actives (r = 1,00), pas ~ distance (r = 0,99),
   distance ~ objectif_atteint (r = 0,99)... Polar ne MESURE pas ces
   grandeurs separement, il les CALCULE a partir du nombre de pas. Leur
   correlation est une definition, pas une decouverte.

   Aucun test statistique ne peut detecter ca : le calcul est
   impeccable, r = 1,00 est exact, la p-value est minuscule a juste
   titre. Seule la connaissance de la source le sait. D'ou FAMILLES,
   plus bas - une liste ecrite a la main, la seule solution possible.

Sur l'absence de scipy : les .pyd de scipy sont refuses au chargement par
la strategie de controle d'application de Windows sur cette machine. Les
p-values sont donc calculees ici, avec math seul. C'est une trentaine de
lignes, et elles montrent ce qu'une p-value EST - l'aire dans la queue
d'une loi de Student - plutot que de la recevoir d'une boite noire.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import exp, lgamma, log, sqrt

import pandas as pd

from analytics import load
from analytics.quality import N_MINIMUM, sans_bords

# Seuil de significativite AVANT correction. 0,05 n'a rien de sacre :
# c'est une convention de Fisher, pas un resultat mathematique.
ALPHA = 0.05

# En dessous, aucun test n'est calcule. Avec n = 3, deux points suffisent
# a tracer une droite parfaite : r = 1 y est un artefact geometrique.
N_TEST_MINIMUM = 5


# ---------------------------------------------------------------------
# Le cinquieme piege, celui qu'aucune statistique ne detecte.
#
# Polar ne MESURE qu'une chose cote activite : le mouvement du poignet.
# Les pas, la distance, les calories, les minutes actives et le taux
# d'objectif en sont tous DERIVES, par des formules fixes. Les correler
# entre eux revient a correler une grandeur avec elle-meme.
#
# Ces paires ont tout pour convaincre : n confortable, r proche de 1,
# p ecrasant, intervalle etroit. Elles passent Bonferroni sans effort.
# Et elles n'apprennent rien.
#
# Aucun test ne peut les ecarter : il faut savoir ce que fabrique le
# capteur. C'est de la connaissance du domaine, pas des mathematiques -
# et c'est pour ca qu'un tableau de correlations ne se lit jamais sans
# quelqu'un qui connait les donnees.
# ---------------------------------------------------------------------

# Grandeurs qui n'existent que si la montre a mesure une nuit. Les plus
# rares de la base (7 nuits contre 10 jours), et celles sur lesquelles
# portent toutes les questions du projet.
NOCTURNES = {"sommeil_score", "hrv", "fc_nocturne", "respiration",
             "sommeil_profond_min", "sommeil_rem_min", "sommeil_leger_min"}


# Grandeurs qu'une meme mesure produit, ou que Polar derive l'une de
# l'autre. Les correler revient a verifier une formule, pas a apprendre
# quelque chose - voir le piege n°5.
#
# Cette liste ne peut pas etre deduite des donnees : c'est de la
# connaissance du capteur, elle s'ecrit et se maintient a la main. Se
# tromper ici, c'est soit polluer le tableau (oubli), soit y masquer un
# vrai resultat (exces de zele). En cas de doute, ne pas grouper.
FAMILLES = {
    # Le podometre et tout ce que Polar en calcule : la distance est un
    # nombre de pas multiplie par une foulee, les calories actives une
    # fonction de la distance, l'objectif un pourcentage de la charge.
    "activite": {"pas", "distance_m", "calories", "calories_actives",
                 "actif_min", "objectif_atteint", "inactif_min"},

    # Statistiques du meme echantillon de FC : correler la mediane et la
    # moyenne d'une meme serie ne teste que la symetrie de sa
    # distribution.
    "fc_journee": {"fc_mediane", "fc_moyenne", "fc_min", "fc_max",
                   "fc_mesures", "fc_minutes_couvertes"},

    # Sous-scores et duree du meme algorithme de sommeil : le score
    # global EST une combinaison des trois autres.
    "sommeil": {"sommeil_score", "sommeil_profond_min", "sommeil_rem_min",
                "sommeil_leger_min", "duree_h", "score_duree",
                "score_solidite", "score_regeneration"},

    # HRV et FC nocturne sortent du meme signal : la FC est l'inverse de
    # l'intervalle moyen entre battements, la HRV sa dispersion. Leur
    # anticorrelation quasi parfaite (r = -0,99 ici) est arithmetique
    # avant d'etre physiologique.
    "recuperation": {"hrv", "fc_nocturne", "respiration"},

    # (V4) Tout ce qui se mange varie ensemble par construction : manger
    # davantage augmente simultanement kcal, proteines, glucides et
    # lipides. Correler kcal et glucides ne mesure pas un lien, ca
    # mesure que j'ai mange.
    "nutrition": {"veille_kcal", "veille_proteines_g", "veille_glucides_g",
                  "veille_sucres_g", "veille_lipides_g", "veille_ag_satures_g",
                  "veille_fibres_g", "veille_sel_g", "veille_masse_g",
                  "kcal", "proteines_g", "glucides_g", "sucres_g",
                  "lipides_g", "ag_satures_g", "fibres_g", "sel_g",
                  "masse_g"},

    # (V4) Statistiques de la meme serie de temperature sur la meme
    # nuit. temp_moy et temp_max d'une nuit ne sont pas deux mesures,
    # c'est une mesure resumee deux fois.
    "chambre_temp": {"chambre_temp_moy", "chambre_temp_min",
                     "chambre_temp_max", "chambre_temp_debut",
                     "chambre_temp_fin"},
    "chambre_hum": {"chambre_hum_moy", "chambre_hum_min", "chambre_hum_max"},
}


# Colonnes qui decrivent la MESURE, pas le mesure. Exclues d'office.
#
# Second piege trouve en lisant la sortie de ce module : apres avoir
# ecarte les tautologies, deux des trois resultats "retenus" etaient
#     fc_minutes_couvertes ~ distance_m        r = 0,94
#     fc_minutes_couvertes ~ objectif_atteint  r = 0,94
# La montre echantillonne davantage quand on bouge, et on ne la porte
# que lorsqu'on est actif. Ces correlations sont donc reelles, fortes,
# reproductibles - et ne disent rien d'autre que "j'ai porte ma montre
# les jours ou j'ai marche".
#
# Une metadonnee de collecte correle avec l'activite par construction.
# Elle a sa place dans quality.py, jamais dans une analyse.
COLONNES_METADONNEES = {"fc_mesures", "fc_minutes_couvertes",
                        # (V4) Combien de mesures le capteur a
                        # prises, et combien de repas j'ai
                        # pense a noter : des faits sur MOI en
                        # train de mesurer, pas sur ce qui est
                        # mesure.
                        "chambre_mesures", "chambre_couverture_pct",
                        "veille_nb_repas", "veille_repas_estimes"}


def famille(colonne: str) -> str | None:
    """La famille d'une colonne, ou None si elle n'appartient a aucune."""
    for nom, membres in FAMILLES.items():
        if colonne in membres:
            return nom

    return None


def meme_famille(a: str, b: str) -> bool:
    """Deux colonnes issues de la meme mesure ou du meme calcul ?"""
    famille_a = famille(a)

    return famille_a is not None and famille_a == famille(b)


def redondante(x: str, y: str) -> bool:
    """Les deux grandeurs sortent-elles du meme calcul ?

    Se teste AVANT toute statistique : une tautologie le reste quels que
    soient son r, son n et son p.

    Simple alias de meme_famille : le nom dit l'intention au moment de
    lire un resultat, meme_famille dit le mecanisme au moment de filtrer.
    """
    return meme_famille(x, y)


# --------------------------------------------- la loi de Student, a la main

def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Fraction continue de Lentz pour la fonction beta incomplete.

    Recette classique (Numerical Recipes, section 6.4). On n'a pas besoin
    de la comprendre pour s'en servir, mais il faut savoir ce qu'elle
    calcule : la convergence de la fraction continue qui evalue
    l'integrale de la loi beta entre 0 et x.
    """
    MAX_ITER, EPS, MINUSCULE = 300, 3e-16, 1e-300

    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap

    if abs(d) < MINUSCULE:
        d = MINUSCULE

    d = 1.0 / d
    h = d

    for m in range(1, MAX_ITER + 1):
        m2 = 2 * m

        for numerateur in (m * (b - m) * x / ((qam + m2) * (a + m2)),
                           -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + numerateur * d
            if abs(d) < MINUSCULE:
                d = MINUSCULE

            c = 1.0 + numerateur / c
            if abs(c) < MINUSCULE:
                c = MINUSCULE

            d = 1.0 / d
            h *= d * c

        # Convergence : le dernier facteur n'apporte plus rien.
        if abs(d * c - 1.0) < EPS:
            break

    return h


def beta_incomplete(a: float, b: float, x: float) -> float:
    """Fonction beta incomplete regularisee I_x(a, b).

    lgamma plutot que gamma : gamma(100) deborde un float, log(gamma)
    non. On additionne des logarithmes puis on repasse a l'exponentielle
    une seule fois, a la fin.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    facteur = exp(lgamma(a + b) - lgamma(a) - lgamma(b)
                  + a * log(x) + b * log(1.0 - x))

    # La fraction continue ne converge vite que d'un cote ; de l'autre on
    # utilise la symetrie I_x(a,b) = 1 - I_(1-x)(b,a).
    if x < (a + 1.0) / (a + b + 2.0):
        return facteur * _beta_continued_fraction(a, b, x) / a

    return 1.0 - facteur * _beta_continued_fraction(b, a, 1.0 - x) / b


def p_bilaterale(t: float, ddl: int) -> float:
    """Probabilite d'obtenir |t| ou plus sous l'hypothese nulle.

    C'est la definition d'une p-value, et elle merite d'etre relue :
    "si AUCUN lien n'existait, quelle serait la probabilite de voir un
    resultat au moins aussi marque que le mien, par pur hasard ?"

    Ce n'est PAS la probabilite que l'hypothese soit fausse. Ce n'est PAS
    la force du lien. Une p-value de 0,04 sur 7 points reste une
    coincidence tres ordinaire quand on a teste 190 paires.
    """
    if ddl <= 0:
        return float("nan")

    return beta_incomplete(ddl / 2.0, 0.5, ddl / (ddl + t * t))


# --------------------------------------------------------- les coefficients

@dataclass
class Correlation:
    """Un test, avec tout ce qui permet de le juger."""

    x: str
    y: str
    decalage: int
    methode: str
    r: float
    n: int
    p: float

    @property
    def significatif_brut(self) -> bool:
        """Avant correction. A ne jamais citer seul - voir le piege n°2."""
        return self.p < ALPHA

    def significatif(self, tests: int) -> bool:
        """Apres Bonferroni : le seuil est divise par le nombre de tests.

        Correction volontairement severe. Elle repond exactement a la
        question "quelle chance que le MEILLEUR de mes 190 resultats soit
        un hasard ?", et c'est bien la question qu'on se pose quand on
        regarde un tableau trie par r decroissant.
        """
        return self.p < ALPHA / max(tests, 1)


def _coefficient(x: pd.Series, y: pd.Series, methode: str) -> tuple[float, int]:
    """r de Pearson ou de Spearman, et le n effectif.

    Pearson mesure un lien LINEAIRE sur les valeurs.
    Spearman fait exactement le meme calcul sur les RANGS : il detecte
    donc tout lien monotone, meme courbe, et se moque des valeurs
    extremes (le 13 004 pas du samedi devient "le rang 10", pas
    "trois fois la normale").

    Sur des donnees de capteur, Spearman est le choix par defaut.
    """
    paires = pd.concat([x, y], axis=1).dropna()
    paires.columns = ["x", "y"]
    n = len(paires)

    if n < N_TEST_MINIMUM:
        return float("nan"), n

    a, b = paires["x"], paires["y"]

    if methode == "spearman":
        a, b = a.rank(), b.rank()

    # Ecart-type nul = serie constante : le r n'existe pas (0/0). Le cas
    # est reel dans cette base (cardio.strain), et sans ce garde-fou il
    # ressortirait en NaN silencieux au milieu du tableau.
    if a.std() == 0 or b.std() == 0:
        return float("nan"), n

    return float(a.corr(b)), n


def tester(x: pd.Series, y: pd.Series, nom_x: str, nom_y: str,
           decalage: int = 0, methode: str = "spearman") -> Correlation | None:
    """Un test complet : r, n, p. None si le calcul n'a pas de sens.

    Le decalage s'applique a x : decalage=1 compare x de la veille a y du
    jour. shift() decale sur l'INDEX, ce qui n'est juste que parce que
    load.py a reindexe la plage en jours consecutifs - sur un index a
    trous, "la ligne precedente" et "la veille" ne sont pas la meme
    chose.
    """
    if decalage:
        x = x.shift(decalage)

    r, n = _coefficient(x, y, methode)

    if pd.isna(r):
        return None

    # t de Student associe au coefficient. La formule vaut pour Pearson ;
    # appliquee aux rangs elle est une approximation, correcte des que
    # n depasse ~10 et prudente en dessous.
    if abs(r) >= 1.0:
        p = 0.0
    else:
        t = r * sqrt((n - 2) / (1 - r * r))
        p = p_bilaterale(t, n - 2)

    return Correlation(nom_x, nom_y, decalage, methode, r, n, p)


def toutes(df: pd.DataFrame, colonnes: list[str] | None = None,
           decalages: tuple[int, ...] = (0, 1),
           methode: str = "spearman",
           garder_familles: bool = False
           ) -> tuple[list[Correlation], int]:
    """Toutes les paires utiles. Renvoie (resultats, paires_ecartees).

    Trie par |r| decroissant.

    Le nombre de tests grimpe vite : k colonnes donnent k*(k-1)/2 paires
    au decalage 0, puis k*(k-1) de plus par decalage supplementaire (car
    "x precede y" et "y precede x" sont deux questions distinctes).
    C'est ce nombre qui alimente la correction de Bonferroni.

    Ecarter les paires d'une meme famille AVANT de tester, et non apres,
    n'est pas un detail de presentation : le seuil de Bonferroni est
    alpha / nombre de tests. Tester 513 paires puis en cacher 100 a
    l'affichage laisserait un seuil calcule sur des tests qu'on ne
    regarde plus - trop severe, donc des vrais resultats perdus. On ne
    corrige que pour ce qu'on examine reellement.
    """
    if colonnes is None:
        colonnes = [c for c in df.select_dtypes("number").columns
                    if c not in COLONNES_METADONNEES
                    and df[c].notna().sum() >= N_TEST_MINIMUM
                    and df[c].std() > 0]

    resultats: list[Correlation] = []
    ecartees = 0

    for decalage in decalages:
        if decalage == 0:
            paires = combinations(colonnes, 2)
        else:
            # Ordre significatif : au decalage 1, (a, b) demande "a de la
            # veille explique-t-il b ?", (b, a) pose la question inverse.
            paires = ((a, b) for a in colonnes for b in colonnes if a != b)

        for a, b in paires:
            # Exception : au decalage non nul, une meme famille redevient
            # interessante. "mes pas d'hier annoncent-ils mes pas
            # d'aujourd'hui ?" est une vraie question (de la persistance),
            # pas une tautologie - contrairement a la meme paire du jour.
            if not garder_familles and decalage == 0 and meme_famille(a, b):
                ecartees += 1
                continue

            test = tester(df[a], df[b], a, b, decalage, methode)
            if test is not None:
                resultats.append(test)

    return (sorted(resultats, key=lambda c: abs(c.r), reverse=True), ecartees)


def tendance(serie: pd.Series) -> float:
    """Correlation de la serie avec le temps. Detecte le piege n°4.

    Une valeur elevee signale que la grandeur derive sur la periode. Deux
    grandeurs qui derivent toutes les deux correleront entre elles sans
    qu'aucune n'explique l'autre - c'est le mecanisme de la correlation
    fallacieuse, et sur 7 points il suffit de peu pour le declencher.
    """
    valeurs = serie.dropna()

    if len(valeurs) < N_TEST_MINIMUM:
        return float("nan")

    temps = pd.Series(range(len(valeurs)), index=valeurs.index)
    r, _ = _coefficient(temps, valeurs, "spearman")

    return r


# ------------------------------------------------------------------ sortie

def run_correlate(top: int = 15) -> int:
    """Affiche le tableau des correlations. Renvoie le nombre de retenues.

    "Retenues" = significatives APRES Bonferroni. Sur les donnees
    actuelles, ce nombre doit valoir 0 : c'est le resultat attendu, pas
    un echec du module.
    """
    quotidien = sans_bords(load.daily())
    resultats, ecartees = toutes(quotidien)
    tests = len(resultats)

    print("=" * 78)
    print("CORRELATIONS")
    print("=" * 78)

    if not resultats:
        print("Pas assez de donnees pour un seul test.")
        return 0

    n_max = max(c.n for c in resultats)
    n_min = min(c.n for c in resultats)
    seuil = ALPHA / tests

    print(f"{tests} tests calcules sur {len(quotidien)} jours "
          f"(n effectif : {n_min} a {n_max})")
    print(f"{ecartees} paires ecartees d'office : meme famille de mesure "
          f"(voir FAMILLES)")
    print(f"Seuil brut       : p < {ALPHA}")
    print(f"Seuil corrige    : p < {seuil:.2e}   (Bonferroni : {ALPHA} / "
          f"{tests} tests)")
    print()
    print(f"{'x':<20} {'y':<18} {'dec':>4} {'r':>7} {'n':>4} {'p':>9}  ")
    print("-" * 78)

    for correlation in resultats[:top]:
        if redondante(correlation.x, correlation.y):
            marque = "  [tautologie]"
        elif correlation.significatif(tests):
            # Passer Bonferroni ne suffit pas : sur 7 points, l'intervalle
            # de confiance d'un r reste enorme. Le marqueur le rappelle a
            # l'endroit exact ou la tentation d'y croire se presente.
            marque = ("  <-- retenu" if correlation.n >= N_MINIMUM
                      else f"  <-- retenu MAIS n={correlation.n}")
        elif correlation.significatif_brut:
            marque = "  (brut seul)"
        else:
            marque = ""

        print(f"{correlation.x:<20} {correlation.y:<18} "
              f"{correlation.decalage:>4} {correlation.r:>7.2f} "
              f"{correlation.n:>4} {correlation.p:>9.4f}{marque}")

    # Les tautologies sortent des comptages : les garder ferait annoncer
    # "20 correlations retenues" la ou il n'y a qu'un capteur et cinq
    # facons d'ecrire sa sortie.
    utiles = [c for c in resultats if not redondante(c.x, c.y)]
    tautologiques = [c for c in resultats
                     if redondante(c.x, c.y) and c.significatif(tests)]

    retenues = sum(1 for c in utiles if c.significatif(tests))
    bruts = sum(1 for c in utiles if c.significatif_brut)

    # --- tendances
    print("\nTendance sur la periode (correlation avec le temps)")
    print("-" * 52)

    for colonne in ("sommeil_score", "hrv", "fc_nocturne", "pas", "calories"):
        if colonne in quotidien.columns:
            valeur = tendance(quotidien[colonne])
            if pd.notna(valeur):
                alerte = "  <-- derive" if abs(valeur) > 0.6 else ""
                print(f"  {colonne:<20} {valeur:>6.2f}{alerte}")

    # --- verdict
    print()
    print("-" * 78)

    if tautologiques:
        print(f"{len(tautologiques)} paires passent la correction en etant "
              f"des TAUTOLOGIES :")
        for c in tautologiques[:5]:
            print(f"    {c.x} / {c.y}   r={c.r:+.2f}  p={c.p:.4f}")
        print("    Meme grandeur, deux noms. Ecartees des comptages.")
        print()

    accord = "survit" if retenues <= 1 else "survivent"
    print(f"Hors tautologies : {bruts} paires passent le seuil brut, "
          f"{retenues} {accord} a la correction.")

    attendus = len(utiles) * ALPHA
    print(f"Sur des donnees SANS aucun lien, on en attendrait "
          f"{attendus:.0f} au seuil brut : c'est la raison de la correction.")

    # Le n qui compte est celui des paires qu'on avait envie de tester.
    # Toutes les questions du projet portent sur le sommeil ou la
    # recuperation - les grandeurs les plus rares de la base. Annoncer le
    # n maximal toutes paires confondues flatterait le resultat avec des
    # paires que personne ne cherchait a connaitre.
    n_utile = max((c.n for c in utiles), default=0)
    n_nocturne = max((c.n for c in utiles
                      if c.x in NOCTURNES or c.y in NOCTURNES), default=0)

    if n_nocturne < N_MINIMUM:
        print()
        print(f"[!] Sur les grandeurs nocturnes - sommeil, HRV, FC de "
              f"repos, celles qui")
        print(f"    motivent tout le projet - le n effectif est "
              f"{n_nocturne}, pas {n_utile}.")
        print("    Ce tableau est un test du CODE, pas un resultat.")
        print("    Aucune ligne ci-dessus ne doit etre citee comme un fait.")
        print()
        print("    Il manque des semaines de collecte, pas des lignes "
              "de code.")

    return retenues
