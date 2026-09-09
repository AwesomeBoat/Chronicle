"""Tracer des figures depuis un script (V3, etape 5).

Un graphique n'est pas une illustration : c'est un instrument de mesure
qu'on lit avec l'oeil. Il peut donc mentir, et les trois mensonges les
plus frequents sont evites ici par construction.

**1. Le trou comble.** Un jour sans donnee absent du resultat SQL
disparait du graphique, qui relie le 18 au 22 par un trait droit
rassurant. load.py reindexe la plage complete : le jour manquant devient
un NaN, et matplotlib INTERROMPT la ligne. Un trou doit se voir.

**2. Le lissage qui invente.** Une moyenne glissante sur 7 jours calculee
sur 4 jours de donnees produit quand meme une courbe. min_periods force
un nombre minimum de points reels, sinon NaN. Mieux vaut une courbe
courte qu'une courbe fausse.

**3. Le n cache.** Chaque figure porte son effectif ecrit dessus. Une
correlation visuelle sur 7 points ne doit pas pouvoir etre confondue avec
la meme sur 700.

Cote technique, deux regles de matplotlib dans un script :

- backend "Agg" AVANT le premier import de pyplot. Sans lui, matplotlib
  cherche une fenetre graphique : lent au demarrage, et bloquant sous
  tache planifiee ou aucun bureau n'est ouvert.
- API objet (fig, ax = plt.subplots()) et jamais plt.plot(). L'API
  pyplot travaille sur "la figure courante", un etat global : deux
  fonctions qui tracent a la suite se marchent dessus.
- plt.close(fig) apres chaque sauvegarde. Une figure non fermee reste en
  memoire, et matplotlib previent au bout de 20.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

# Doit preceder l'import de pyplot. Voir docstring.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from analytics import describe, load  # noqa: E402

# Fenetre de lissage. 7 jours : un cycle hebdomadaire complet, donc
# insensible a l'effet "week-end" qui domine toute serie d'activite.
LISSAGE = 7

# Nombre minimal de points reels dans la fenetre pour qu'un point lisse
# existe. 4 sur 7 : la majorite. En dessous, la moyenne dirait surtout
# quels jours manquent.
LISSAGE_MIN = 4

COULEUR = "#2b6cb0"
COULEUR_LISSE = "#c05621"
GRIS = "#718096"


def _figure(titre: str, n: int, hauteur: float = 3.6):
    """Cree une figure au format commun, avec le n annonce dans le titre."""
    fig, ax = plt.subplots(figsize=(10, hauteur), dpi=110)
    ax.set_title(f"{titre}   (n = {n})", fontsize=11, loc="left")
    ax.grid(True, alpha=0.25, linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    return fig, ax


def _sauver(fig, dossier: Path, nom: str) -> Path:
    """Enregistre, ferme, renvoie le chemin.

    bbox_inches="tight" : sans lui, un label d'axe long est simplement
    coupe hors de l'image, sans aucun avertissement.
    """
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{nom}.png"

    fig.savefig(chemin, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    return chemin


# ------------------------------------------------------------- les figures

def serie(quotidien: pd.DataFrame, colonne: str, dossier: Path,
          unite: str = "") -> Path | None:
    """Une grandeur dans le temps, brute + moyenne glissante.

    rolling(LISSAGE, min_periods=LISSAGE_MIN) compte des LIGNES, et non
    des jours - ce qui n'est juste que parce que l'index a ete reindexe
    en jours consecutifs par load.py. Sur un index a trous, il faudrait
    rolling("7D"), qui raisonne en duree.
    """
    valeurs = quotidien[colonne]
    n = int(valeurs.notna().sum())

    if n < 2:
        return None

    fig, ax = _figure(f"{colonne.replace('_', ' ')}"
                      + (f" [{unite}]" if unite else ""), n)

    ax.plot(valeurs.index, valeurs.to_numpy(), marker="o", markersize=3.5,
            linewidth=1.2, color=COULEUR, label="mesure")

    lisse = valeurs.rolling(LISSAGE, min_periods=LISSAGE_MIN).mean()

    if lisse.notna().any():
        ax.plot(lisse.index, lisse.to_numpy(), linewidth=2.2,
                color=COULEUR_LISSE, label=f"moyenne {LISSAGE} j")

    # Les jours sans mesure, marques en bas : le trou devient une donnee.
    absents = valeurs.index[valeurs.isna()]
    if len(absents):
        ax.plot(absents, [ax.get_ylim()[0]] * len(absents), marker="|",
                linestyle="none", color=GRIS, markersize=8,
                label=f"{len(absents)} jours sans mesure")

    ax.legend(fontsize=8, frameon=False)
    fig.autofmt_xdate()

    return _sauver(fig, dossier, f"serie_{colonne}")


def profil_fc(dossier: Path, code: str = "heart_rate") -> Path | None:
    """FC mediane par heure de la journee, avec l'intervalle Q1-Q3.

    La bande interquartile est le point de la figure : sans elle, une
    mediane a 70 bpm a 15h ressemble a une certitude. Avec, on voit que
    l'heure recouvre aussi bien du repos que de l'effort.
    """
    profil = describe.profil_horaire(code) if hasattr(describe, "profil_horaire") \
        else None

    if profil is None or profil.empty:
        return None

    n = int(profil["minutes"].sum())
    # n = des MINUTES couvertes, pas des releves : c'est l'unite dans
    # laquelle profil_horaire raisonne (voir describe.par_minute).
    fig, ax = _figure("Frequence cardiaque par heure (heure locale) [bpm] - "
                      "mediane des moyennes par minute", n)

    ax.fill_between(profil.index, profil["q1"], profil["q3"], alpha=0.22,
                    color=COULEUR, label="Q1 - Q3")
    ax.plot(profil.index, profil["mediane"], marker="o", markersize=4,
            linewidth=1.8, color=COULEUR, label="mediane")

    ax.set_xlabel("heure")
    ax.set_xticks(range(0, 24, 2))
    ax.legend(fontsize=8, frameon=False)

    return _sauver(fig, dossier, "profil_fc_horaire")


def distribution(quotidien: pd.DataFrame, colonne: str,
                 dossier: Path) -> Path | None:
    """Histogramme + boite a moustaches de la meme grandeur.

    Les deux ensemble parce qu'ils ne montrent pas la meme chose :
    l'histogramme montre la FORME (une bosse ? deux ?), la boite montre
    les REPERES (mediane, quartiles, moustaches de Tukey). Une
    distribution a deux bosses se voit sur l'un et pas sur l'autre.
    """
    valeurs = pd.to_numeric(quotidien[colonne], errors="coerce").dropna()
    n = len(valeurs)

    if n < 4:
        return None

    fig, (ax_h, ax_b) = plt.subplots(
        2, 1, figsize=(8, 4.4), dpi=110,
        sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    fig.suptitle(f"Distribution - {colonne.replace('_', ' ')}   (n = {n})",
                 fontsize=11, x=0.02, ha="left")

    # Regle de Sturges, bornee : avec n = 7, trop de classes ne montre
    # qu'une barre par valeur - un histogramme qui ne resume plus rien.
    classes = max(3, min(12, int(1 + 3.322 * (n ** 0.5))))

    ax_h.hist(valeurs.to_numpy(), bins=classes, color=COULEUR, alpha=0.75,
              edgecolor="white")
    ax_h.axvline(valeurs.median(), color=COULEUR_LISSE, linewidth=2,
                 label=f"mediane {valeurs.median():.1f}")
    ax_h.axvline(valeurs.mean(), color=GRIS, linewidth=2, linestyle="--",
                 label=f"moyenne {valeurs.mean():.1f}")
    ax_h.legend(fontsize=8, frameon=False)
    ax_h.set_ylabel("jours")
    ax_h.grid(True, alpha=0.25, linewidth=0.6)
    ax_h.spines[["top", "right"]].set_visible(False)

    ax_b.boxplot(valeurs.to_numpy(), vert=False, widths=0.5,
                 patch_artist=True,
                 boxprops={"facecolor": COULEUR, "alpha": 0.5},
                 medianprops={"color": COULEUR_LISSE, "linewidth": 2})
    ax_b.set_yticks([])
    ax_b.spines[["top", "right", "left"]].set_visible(False)

    return _sauver(fig, dossier, f"distribution_{colonne}")


def nuage(quotidien: pd.DataFrame, x: str, y: str, dossier: Path,
          decalage: int = 0) -> Path | None:
    """Nuage de points entre deux grandeurs, avec decalage optionnel.

    decalage = 1 : x de la veille contre y du jour. C'est la seule facon
    de poser une question orientee dans le temps ("la nuit d'hier
    influence-t-elle aujourd'hui ?") plutot qu'une simple coincidence.

    Aucune droite de regression n'est tracee : sur 7 points elle
    donnerait a un hasard l'allure d'une loi.
    """
    if x not in quotidien.columns or y not in quotidien.columns:
        return None

    serie_x = quotidien[x].shift(decalage) if decalage else quotidien[x]
    paires = pd.DataFrame({x: serie_x, y: quotidien[y]}).dropna()
    n = len(paires)

    if n < 3:
        return None

    suffixe = f" (J-{decalage})" if decalage else ""
    fig, ax = _figure(f"{y} en fonction de {x}{suffixe}", n, hauteur=4.2)

    ax.scatter(paires[x], paires[y], s=70, color=COULEUR, alpha=0.75,
               edgecolor="white", zorder=3)

    # Chaque point est date : sur 7 points, savoir lequel est quel jour
    # vaut mieux qu'une statistique.
    for jour, ligne in paires.iterrows():
        ax.annotate(f"{jour:%d/%m}", (ligne[x], ligne[y]),
                    textcoords="offset points", xytext=(6, 5),
                    fontsize=7, color=GRIS)

    ax.set_xlabel(x + suffixe)
    ax.set_ylabel(y)

    if n < 10:
        ax.text(0.5, 0.02, f"n = {n} : lecture visuelle uniquement, "
                           f"aucune conclusion",
                transform=ax.transAxes, ha="center", fontsize=8,
                color="#c53030")

    nom = f"nuage_{y}_vs_{x}" + (f"_j{decalage}" if decalage else "")
    return _sauver(fig, dossier, nom)


def hypnogramme(dossier: Path) -> Path | None:
    """Heures de coucher et de lever, nuit par nuit.

    Trace en heures decimales depuis midi : minuit devient 12, ce qui
    evite qu'un coucher a 23h50 et un a 00h10 se retrouvent aux deux
    extremites opposees du graphique alors qu'ils sont a 20 minutes.
    """
    nuits = load.sleep().dropna(subset=["coucher", "lever"])
    n = len(nuits)

    if n < 2:
        return None

    def depuis_midi(instants: pd.Series) -> pd.Series:
        heures = instants.dt.hour + instants.dt.minute / 60
        return (heures - 12) % 24

    fig, ax = _figure("Coucher et lever, par nuit (heures depuis midi)", n)

    debut = depuis_midi(nuits["coucher"])
    fin = depuis_midi(nuits["lever"])

    ax.barh(nuits.index, fin - debut, left=debut, height=0.6,
            color=COULEUR, alpha=0.8)

    for jour, (d, f) in zip(nuits.index, zip(debut, fin)):
        ax.text(d - 0.25, jour, f"{(d + 12) % 24:.0f}h", ha="right",
                va="center", fontsize=7, color=GRIS)
        ax.text(f + 0.25, jour, f"{(f + 12) % 24:.0f}h", ha="left",
                va="center", fontsize=7, color=GRIS)

    ax.set_xlabel("heures depuis midi (12 = minuit)")
    ax.set_xlim(6, 24)
    ax.invert_yaxis()

    return _sauver(fig, dossier, "coucher_lever")


# ------------------------------------------------------------------ lot

# Grandeurs tracees en serie temporelle, avec leur unite.
SERIES = [("sommeil_score", "index"), ("hrv", "ms"), ("fc_nocturne", "bpm"),
          ("pas", "pas"), ("calories", "kcal"), ("fc_mediane", "bpm")]

# Paires posees avec un decalage : la nuit precede le jour.
PAIRES = [("hrv", "pas", 0), ("sommeil_score", "pas", 0),
          ("sommeil_score", "fc_mediane", 0), ("pas", "sommeil_score", 1)]


def tout(dossier: Path) -> list[Path]:
    """Produit le lot complet de figures. Renvoie les chemins ecrits."""
    quotidien = load.daily()
    faites: list[Path] = []

    for colonne, unite in SERIES:
        if colonne in quotidien.columns:
            chemin = serie(quotidien, colonne, dossier, unite)
            if chemin:
                faites.append(chemin)

    for colonne in ("sommeil_score", "hrv", "pas"):
        if colonne in quotidien.columns:
            chemin = distribution(quotidien, colonne, dossier)
            if chemin:
                faites.append(chemin)

    for x, y, decalage in PAIRES:
        chemin = nuage(quotidien, x, y, dossier, decalage)
        if chemin:
            faites.append(chemin)

    for producteur in (profil_fc, hypnogramme):
        chemin = producteur(dossier)
        if chemin:
            faites.append(chemin)

    return faites


def run_plots(dossier: str = "reports/figures") -> None:
    """Trace tout et annonce ce qui a ete ecrit."""
    cible = Path(dossier)
    faites = tout(cible)

    print(f"{len(faites)} figures ecrites dans {cible.resolve()}")

    for chemin in faites:
        print(f"  {chemin.name}")
