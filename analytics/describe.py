"""Statistique descriptive : de quoi ont l'air ces donnees ? (V3, etape 4)

Descriptif, et rien de plus. Ce module ne teste aucune hypothese et ne
conclut sur rien : il resume. La difference compte - un resume reste vrai
sur 7 jours, une conclusion non.

Trois idees, dans l'ordre d'importance :

1. AVANT DE CHOISIR ENTRE MOYENNE ET MEDIANE, REGARDER LA PONDERATION.
   La moyenne est le centre de gravite : une valeur extreme la deplace.
   La mediane coupe l'echantillon en deux : elle resiste aux valeurs
   aberrantes. Mais NI L'UNE NI L'AUTRE ne resiste a un echantillonnage
   irregulier - et c'est le cas de toute donnee de capteur.

   Sur la FC continue de la Pacer, mediane des releves bruts = 108 bpm,
   mediane des moyennes par minute = 75,5 bpm. 32 bpm d'ecart, et c'est
   la seconde qui est juste. Choisir une statistique de position ne
   repare pas un probleme de ponderation ; seul un reechantillonnage
   temporel le fait (par_minute).

2. UN CENTRE SANS DISPERSION NE VEUT RIEN DIRE. "Score de sommeil moyen :
   70" est faux tout seul. 70 +/- 3 et 70 +/- 25 decrivent deux vies
   differentes. D'ou l'ecart-type et les quartiles a cote, toujours.

3. UNE VALEUR EXTREME N'EST PAS UNE ERREUR. La regle de l'ecart
   interquartile (IQR) signale ce qui sort du lot ; elle ne dit jamais
   pourquoi. 13 004 pas un samedi n'est pas une faute de capteur, c'est
   une randonnee. On signale, on n'efface pas.
"""

from __future__ import annotations

import pandas as pd

from analytics import load
from analytics.quality import sans_bords

# Sous ce seuil, meme un resume est fragile : la mediane d'un echantillon
# de 3 vaut la valeur du milieu, ni plus ni moins.
N_FRAGILE = 5

# Multiplicateur de l'IQR. 1.5 est la convention de Tukey : sur une
# distribution normale, elle laisse passer environ 0,7% des points.
# Ce n'est pas une loi de la nature, c'est un reglage.
TUKEY = 1.5


def resume(df: pd.DataFrame, colonnes: list[str] | None = None
           ) -> pd.DataFrame:
    """Le tableau de base : n, centre, dispersion, extremes.

    n est calcule par colonne, pas pour le tableau : chaque grandeur a sa
    propre couverture (7 nuits de sommeil pour 10 jours d'activite). Un n
    global masquerait cet ecart derriere un chiffre unique.

    ecart_moy_med est la colonne a lire en premier : quand elle est
    grande, la moyenne ment.
    """
    if colonnes:
        df = df[[c for c in colonnes if c in df.columns]]

    numeriques = df.select_dtypes("number")

    if numeriques.empty:
        return pd.DataFrame()

    table = pd.DataFrame({
        "n": numeriques.count(),
        "moyenne": numeriques.mean(),
        "mediane": numeriques.median(),
        "ecart_type": numeriques.std(),
        "min": numeriques.min(),
        "q1": numeriques.quantile(0.25),
        "q3": numeriques.quantile(0.75),
        "max": numeriques.max(),
    })

    # L'ecart moyenne/mediane rapporte a la dispersion : un ecart de 3
    # bpm ne pese pas pareil sur une serie qui varie de 2 ou de 40.
    table["ecart_moy_med"] = (table["moyenne"] - table["mediane"])

    # Colonnes sans variation : signalees ici aussi, parce qu'un lecteur
    # qui saute l'audit doit quand meme le voir.
    table["constante"] = table["ecart_type"].fillna(0).eq(0)

    return table.dropna(subset=["n"]).query("n > 0").round(2)


def aberrantes(serie: pd.Series, k: float = TUKEY) -> pd.Series:
    """Les points hors [q1 - k*IQR ; q3 + k*IQR].

    IQR = q3 - q1, l'etendue de la moitie centrale de l'echantillon.
    Contrairement a l'ecart-type, elle n'est pas calculee a partir de la
    moyenne : une valeur extreme ne peut donc pas elargir le critere
    cense la detecter. C'est tout l'interet de la methode.

    Renvoie les valeurs signalees, avec leur date. Vide si rien ne sort.
    """
    valeurs = serie.dropna()

    if len(valeurs) < 4:            # moins de 4 points : pas de quartiles
        return pd.Series(dtype="float64", name=serie.name)

    q1, q3 = valeurs.quantile([0.25, 0.75])
    iqr = q3 - q1

    if iqr == 0:
        return pd.Series(dtype="float64", name=serie.name)

    bas, haut = q1 - k * iqr, q3 + k * iqr

    return valeurs[(valeurs < bas) | (valeurs > haut)]


def par_minute(code: str = "heart_rate") -> pd.Series:
    """Ramene une metrique instantanee a UNE valeur par minute.

    La brique qui corrige la ponderation, et le prealable a tout resume
    d'une metrique 'instant'. Voir moyenne_vs_mediane pour le pourquoi.

    resample("1min").mean() decoupe le temps en tranches d'une minute et
    moyenne ce qui tombe dedans. Les minutes sans aucun releve sortent en
    NaN et sont retirees : on ne les invente pas, on constate juste
    qu'elles n'ont pas ete mesurees.
    """
    serie = load.observations(code)

    if serie.empty:
        return serie

    return serie.resample("1min").mean().dropna()


def moyenne_vs_mediane(code: str = "heart_rate") -> dict:
    """La demonstration par l'exemple, sur la FC continue.

    L'erreur que ce module a d'abord commise, gardee ici parce qu'elle
    est instructive : croire qu'une mediane suffit.

    La Pacer n'echantillonne pas a intervalle fixe : ~1 Hz pendant un
    effort, un releve toutes les 5 minutes au repos. Une heure d'effort
    pese donc 3 600 lignes contre 12 pour une heure de calme.

    On pourrait croire la mediane a l'abri - c'est faux, et l'ecart est
    massif. Verifie sur la journee du 2026-08-21 :

        mediane des 9 175 releves bruts .............. 108 bpm
        mediane des 412 moyennes par minute .......... 75,5 bpm

    Une mediane resiste aux valeurs ABERRANTES : quelques points faux ne
    la deplacent pas. Elle ne resiste pas a un echantillonnage
    IRREGULIER, parce que les rafales ne sont pas des points faux - elles
    sont la majorite des lignes. Aucune statistique de position ne repare
    un probleme de ponderation.

    La correction n'est donc pas statistique mais temporelle : donner a
    chaque minute le meme poids AVANT de resumer.
    """
    brut = load.observations(code)

    if brut.empty:
        return {}

    minutes = par_minute(code)

    # L'ecart entre deux mesures consecutives : la preuve de
    # l'echantillonnage irregulier, en une ligne.
    ecarts = brut.index.to_series().diff().dt.total_seconds().dropna()

    return {
        "code": code,
        "n": len(brut),
        "moyenne": float(brut.mean()),
        "mediane": float(brut.median()),
        "minutes": len(minutes),
        "moyenne_min": float(minutes.mean()),
        "mediane_min": float(minutes.median()),
        "ecart_s_median": float(ecarts.median()) if len(ecarts) else float("nan"),
        "rafales_sous_10s": int((ecarts < 10).sum()),
        "pauses_sur_5min": int((ecarts > 300).sum()),
    }



def profil_horaire(code: str = "heart_rate") -> pd.DataFrame:
    """Mediane d'une metrique instantanee par heure de la journee.

    Reduit 28 000 points a 24 lignes lisibles. Le groupby porte sur
    l'heure LOCALE de l'index : c'est la conversion faite une fois dans
    load.observations qui rend ce regroupement juste. Sur un index reste
    en UTC, "3h du matin" en designerait 5 en heure d'ete - et le creux
    nocturne apparaitrait decale de deux heures.

    Q1 et Q3 accompagnent la mediane : une heure de la journee recouvre
    aussi bien du repos que de l'effort, un centre seul le cacherait.

    Le regroupement porte sur les moyennes par MINUTE, pas sur les
    releves bruts. Sans cette etape, une seance de course de 20 minutes
    apporte 1 200 lignes a la tranche "18h" contre 12 pour une heure de
    bureau : la mediane de 18h decrirait la course, pas la journee. Le
    prix a payer est visible dans la colonne "minutes" - une heure peu
    couverte reste une heure peu couverte, et il faut le voir.
    """
    minutes = par_minute(code)

    if minutes.empty:
        return pd.DataFrame()

    par_heure = minutes.groupby(minutes.index.hour)

    return pd.DataFrame({
        "mediane": par_heure.median(),
        "q1": par_heure.quantile(0.25),
        "q3": par_heure.quantile(0.75),
        "minutes": par_heure.size(),
    }).rename_axis("heure")


# ------------------------------------------------------------------ sortie

def _afficher(titre: str, table: pd.DataFrame) -> None:
    print(f"\n{titre}")
    print("-" * len(titre))

    if table.empty:
        print("  (rien a decrire)")
        return

    fragiles = table[table["n"] < N_FRAGILE]
    print(table.to_string())

    if not fragiles.empty:
        print(f"\n  n < {N_FRAGILE} sur : {', '.join(fragiles.index)}"
              f" - resume indicatif seulement.")


def run_describe() -> None:
    """Decrit v_daily, v_sleep et la FC continue. Ne renvoie rien.

    Aucun code de sortie : decrire ne peut pas echouer. C'est quality.py
    qui juge, pas ce module.
    """
    quotidien = sans_bords(load.daily())
    nuits = load.sleep()

    print("=" * 62)
    print("DESCRIPTIF")
    print("=" * 62)
    print("Jours de bord partiels exclus des agregats (voir quality).")

    _afficher("Journalier (v_daily)", resume(quotidien))
    _afficher("Nuits (v_sleep)",
              resume(nuits, ["duree_h", "score", "profond_min", "rem_min",
                             "leger_min", "hrv", "fc_moyenne", "respiration",
                             "continuite", "cycles"]))

    # --- moyenne vs mediane, sur la FC continue
    fc = moyenne_vs_mediane()

    if fc:
        print("\nFrequence cardiaque continue")
        print("-" * 28)
        print(f"  {fc['n']} releves bruts, ramenes a {fc['minutes']} minutes")
        print(f"  intervalle median entre deux releves : "
              f"{fc['ecart_s_median']:.0f} s")
        print(f"  {fc['rafales_sous_10s']} intervalles < 10 s "
              f"(rafales d'effort) contre {fc['pauses_sur_5min']} > 5 min "
              f"(repos)")
        print()
        print(f"  {'':<22}{'moyenne':>10}{'mediane':>10}")
        print(f"  {'par releve brut':<22}{fc['moyenne']:>10.1f}"
              f"{fc['mediane']:>10.1f}")
        print(f"  {'par minute':<22}{fc['moyenne_min']:>10.1f}"
              f"{fc['mediane_min']:>10.1f}")
        print(f"  {'ecart':<22}{fc['moyenne'] - fc['moyenne_min']:>+10.1f}"
              f"{fc['mediane'] - fc['mediane_min']:>+10.1f}")
        print()
        print("  -> la mediane ne protege PAS d'un echantillonnage")
        print("     irregulier : seules les colonnes 'par minute' sont")
        print("     interpretables comme 'la FC typique'.")

    # --- valeurs extremes
    print("\nValeurs signalees par la regle de Tukey (IQR x 1.5)")
    print("-" * 50)

    trouve = False

    for colonne in ("pas", "calories", "sommeil_score", "hrv", "fc_nocturne"):
        if colonne not in quotidien.columns:
            continue

        points = aberrantes(quotidien[colonne])

        for jour, valeur in points.items():
            print(f"  {colonne:<16} {jour:%Y-%m-%d}  {valeur:>10.1f}")
            trouve = True

    if not trouve:
        print("  aucune - ce qui, sur 7 a 10 points, ne prouve rien.")

    print("\n" + "-" * 62)
    print("Descriptif seulement : aucune de ces lignes n'est une conclusion.")
