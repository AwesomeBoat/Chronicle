"""Figures de lecture (Kindle).

Memes regles que analytics/plots.py, pour les memes raisons :

- backend "Agg" avant pyplot, sinon matplotlib cherche une fenetre ;
- API objet (fig, ax), jamais plt.plot() qui travaille sur un etat global ;
- plt.close(fig) apres chaque sauvegarde ;
- **le n est ecrit sur chaque figure**. Une figure sans son effectif
  laisse croire que 5 points valent 500.

Une regle de plus, propre a cette source : quand il n'y a rien a
montrer, la figure n'est PAS produite. Un graphique vide dans un
tableau de bord se lit comme une donnee nulle, alors qu'il signale une
donnee absente - deux choses tres differentes ici, puisque les sessions
non collectees a temps sont perdues et non nulles.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from analytics import load, reading  # noqa: E402

# Palette : un bleu profond pour la lecture, un ambre pour le lissage et
# les annotations. Assez contrastes en niveaux de gris pour rester
# lisibles imprimes.
BLEU = "#2b6cb0"
AMBRE = "#c05621"
VERT = "#2f855a"
POURPRE = "#6b46c1"
GRIS = "#718096"
GRIS_CLAIR = "#e2e8f0"

GENRES = {"highlight": ("Surlignements", BLEU),
          "note": ("Notes", AMBRE),
          "bookmark": ("Marque-pages", VERT),
          "word_lookup": ("Mots cherches", POURPRE)}


def _figure(titre: str, n: int, hauteur: float = 3.6, largeur: float = 9.0):
    """Une figure titree, avec son effectif en sous-titre."""
    fig, ax = plt.subplots(figsize=(largeur, hauteur))
    fig.suptitle(titre, fontsize=12, fontweight="bold", x=0.02, ha="left")
    ax.set_title(f"n = {n}", fontsize=9, color=GRIS, loc="left")
    ax.grid(True, alpha=0.25, linewidth=0.6)
    ax.set_axisbelow(True)

    for cote in ("top", "right"):
        ax.spines[cote].set_visible(False)

    return fig, ax


def _sauver(fig, dossier: Path, nom: str) -> Path:
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{nom}.png"
    fig.savefig(chemin, dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    return chemin


# --------------------------------------------------------------- series

def temps_quotidien(dossier: Path,
                    quotidien: pd.DataFrame | None = None) -> Path | None:
    """Minutes lues par jour, avec moyenne glissante 7 jours."""
    if quotidien is None:
        quotidien = load.lecture()

    if quotidien.empty or "minutes" not in quotidien.columns:
        return None

    serie = pd.to_numeric(quotidien["minutes"], errors="coerce")

    if serie.notna().sum() == 0:
        return None

    n = int(serie.notna().sum())
    fig, ax = _figure("Temps de lecture par jour", n)

    ax.bar(serie.index, serie.fillna(0), color=BLEU, width=0.8, alpha=0.85,
           label="minutes lues")

    # min_periods : une moyenne 7 jours calculee sur 2 points produirait
    # quand meme une courbe, et cette courbe serait un mensonge.
    if n >= 4:
        lisse = serie.rolling("7D", min_periods=4).mean()
        ax.plot(lisse.index, lisse, color=AMBRE, linewidth=2.2,
                label="moyenne 7 jours")

    ax.set_ylabel("minutes")
    ax.legend(frameon=False, fontsize=9)
    fig.autofmt_xdate()

    return _sauver(fig, dossier, "lecture_temps_quotidien")


def progression_livres(dossier: Path,
                       historique: pd.DataFrame | None = None) -> Path | None:
    """Avancement dans chaque livre au fil du temps."""
    if historique is None:
        historique = load.progression_livres()

    if historique.empty or historique["progression_pct"].notna().sum() == 0:
        return None

    suivis = historique[historique["progression_pct"].notna()]

    if suivis["asin"].nunique() == 0:
        return None

    fig, ax = _figure("Progression dans les livres", len(suivis))

    for asin, groupe in suivis.groupby("asin"):
        titre = (groupe["titre"].iloc[0] or asin)[:38]
        ax.plot(groupe["captured_at"], groupe["progression_pct"],
                marker="o", markersize=5, linewidth=1.8, label=titre)

    ax.set_ylabel("progression (%)")
    ax.set_ylim(0, 105)
    ax.legend(frameon=False, fontsize=8, loc="best")
    fig.autofmt_xdate()

    return _sauver(fig, dossier, "lecture_progression")


# -------------------------------------------------------------- rythmes

def profil_horaire(dossier: Path,
                   sessions: pd.DataFrame | None = None) -> Path | None:
    """A quelles heures je lis."""
    profil = reading.profil_horaire(sessions)

    if profil.empty or profil["minutes"].sum() == 0:
        return None

    n = int(profil["sessions"].sum())
    fig, ax = _figure("Heures de lecture", n)

    couleurs = [AMBRE if v > 0 else GRIS_CLAIR for v in profil["minutes"]]
    ax.bar(profil.index, profil["minutes"], color=couleurs, width=0.85)

    ax.set_xlabel("heure de la journee")
    ax.set_ylabel("minutes cumulees")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}h" for h in range(0, 24, 2)])

    return _sauver(fig, dossier, "lecture_profil_horaire")


def profil_hebdomadaire(dossier: Path,
                        sessions: pd.DataFrame | None = None) -> Path | None:
    """Minutes par jour de la semaine, normalisees par occurrence.

    Normaliser est indispensable : une fenetre de 10 jours contient deux
    lundis et un seul mercredi. Sans division, le lundi parait toujours
    plus studieux.
    """
    profil = reading.profil_hebdomadaire(sessions)

    if profil.empty or profil["minutes"].sum() == 0:
        return None

    n = int(profil["sessions"].sum())
    fig, ax = _figure("Lecture par jour de la semaine", n)

    valeurs = pd.to_numeric(profil["minutes_par_occurrence"],
                            errors="coerce").fillna(0)

    ax.bar(profil.index, valeurs, color=BLEU, width=0.7)
    ax.set_ylabel("minutes par jour observe")
    ax.tick_params(axis="x", rotation=30)

    return _sauver(fig, dossier, "lecture_profil_hebdo")


def carte_horaire(dossier: Path,
                  sessions: pd.DataFrame | None = None) -> Path | None:
    """Heatmap jour de la semaine x heure."""
    table = reading.carte_heures_jours(sessions)

    if table.empty or table.to_numpy().sum() == 0:
        return None

    n = int((table > 0).to_numpy().sum())
    fig, ax = plt.subplots(figsize=(11, 3.6))
    fig.suptitle("Quand je lis", fontsize=12, fontweight="bold", x=0.02,
                 ha="left")
    ax.set_title(f"{n} creneaux actifs", fontsize=9, color=GRIS, loc="left")

    image = ax.imshow(table.to_numpy(), aspect="auto", cmap="YlOrBr",
                      interpolation="nearest")

    ax.set_yticks(range(len(table.index)))
    ax.set_yticklabels(table.index, fontsize=9)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}h" for h in range(0, 24, 2)], fontsize=9)
    ax.set_xlabel("heure")

    barre = fig.colorbar(image, ax=ax, pad=0.02)
    barre.set_label("minutes", fontsize=9)

    for cote in ("top", "right"):
        ax.spines[cote].set_visible(False)

    return _sauver(fig, dossier, "lecture_carte_horaire")


def duree_sessions(dossier: Path,
                   sessions: pd.DataFrame | None = None) -> Path | None:
    """Distribution des durees de session."""
    sessions = reading.sessions_utiles(sessions)

    if sessions.empty:
        return None

    n = len(sessions)
    fig, ax = _figure("Duree des sessions", n)

    paniers = min(max(n // 2, 5), 30)
    ax.hist(sessions["duree_min"], bins=paniers, color=BLEU, alpha=0.85,
            edgecolor="white")

    mediane = float(sessions["duree_min"].median())
    ax.axvline(mediane, color=AMBRE, linewidth=2,
               label=f"mediane {mediane:.0f} min")

    ax.set_xlabel("minutes")
    ax.set_ylabel("sessions")
    ax.legend(frameon=False, fontsize=9)

    return _sauver(fig, dossier, "lecture_duree_sessions")


def vitesse(dossier: Path,
            sessions: pd.DataFrame | None = None) -> Path | None:
    """Vitesse de lecture en mots par minute, session par session."""
    avec = reading.vitesse_par_session(sessions)

    if avec is None or len(avec) == 0:
        return None

    n = len(avec)
    fig, ax = _figure("Vitesse de lecture", n)

    ax.scatter(avec["started_at"], avec["mots_par_min"], s=70, color=BLEU,
               alpha=0.85, edgecolor="white", zorder=3)

    moyenne = float(avec["mots_par_min"].mean())
    ax.axhline(moyenne, color=AMBRE, linewidth=1.8, linestyle="--",
               label=f"moyenne {moyenne:.0f} mots/min")

    ax.set_ylabel("mots / minute")
    ax.legend(frameon=False, fontsize=9)
    fig.autofmt_xdate()

    return _sauver(fig, dossier, "lecture_vitesse")


# ---------------------------------------------------------- annotations

def rythme_annotations(dossier: Path,
                       annot: pd.DataFrame | None = None) -> Path | None:
    """Annotations par mois et par genre. La seule serie vraiment longue."""
    compte = reading.rythme_annotations(annot)

    if compte.empty:
        return None

    n = int(compte.to_numpy().sum())
    fig, ax = _figure("Annotations par mois", n, hauteur=3.8)

    bas = pd.Series(0.0, index=compte.index)

    for genre, (libelle, couleur) in GENRES.items():
        if genre not in compte.columns:
            continue

        ax.bar(compte.index, compte[genre], bottom=bas, width=22,
               color=couleur, label=libelle, alpha=0.9)
        bas = bas + compte[genre]

    ax.set_ylabel("annotations")
    ax.legend(frameon=False, fontsize=9, ncol=4)
    fig.autofmt_xdate()

    return _sauver(fig, dossier, "lecture_rythme_annotations")


def top_livres(dossier: Path,
               livres: pd.DataFrame | None = None,
               top: int = 12) -> Path | None:
    """Les livres les plus annotes, en barres horizontales."""
    classement = reading.classement_livres(livres)

    if classement.empty:
        return None

    retenus = classement.nlargest(top, "annotations")
    retenus = retenus[retenus["annotations"] > 0]

    if retenus.empty:
        return None

    n = int(retenus["annotations"].sum())
    hauteur = max(3.2, 0.42 * len(retenus) + 1.4)
    fig, ax = _figure("Livres les plus annotes", n, hauteur=hauteur)

    titres = [(t or "")[:46] for t in retenus["titre"]]
    y = range(len(retenus))
    gauche = [0.0] * len(retenus)

    for colonne, (libelle, couleur) in (
            ("surlignements", ("Surlignements", BLEU)),
            ("notes", ("Notes", AMBRE)),
            ("marque_pages", ("Marque-pages", VERT))):
        valeurs = retenus[colonne].fillna(0).to_numpy()
        ax.barh(list(y), valeurs, left=gauche, color=couleur, label=libelle,
                height=0.7)
        gauche = [g + v for g, v in zip(gauche, valeurs)]

    ax.set_yticks(list(y))
    ax.set_yticklabels(titres, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("annotations")
    ax.legend(frameon=False, fontsize=9, ncol=3)

    return _sauver(fig, dossier, "lecture_top_livres")


def densite_annotation(dossier: Path,
                       livres: pd.DataFrame | None = None,
                       top: int = 10) -> Path | None:
    """Surlignements pour 10 000 positions : quels livres font reagir."""
    densite = reading.densite_annotation(livres)

    if densite is None or densite.empty:
        return None

    retenus = densite[densite["densite"] > 0].head(top)

    if retenus.empty:
        return None

    hauteur = max(3.0, 0.42 * len(retenus) + 1.4)
    fig, ax = _figure("Densite de surlignement", len(retenus),
                      hauteur=hauteur)

    titres = [(t or "")[:46] for t in retenus["titre"]]
    ax.barh(range(len(retenus)), retenus["densite"], color=POURPRE,
            height=0.7)
    ax.set_yticks(range(len(retenus)))
    ax.set_yticklabels(titres, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("surlignements pour 10 000 positions")

    return _sauver(fig, dossier, "lecture_densite")


def mots_frequents(dossier: Path,
                   annot: pd.DataFrame | None = None,
                   top: int = 20) -> Path | None:
    """Les mots les plus cherches au dictionnaire."""
    compte = reading.mots_frequents(annot, top=top)

    if compte.empty:
        return None

    hauteur = max(3.0, 0.32 * len(compte) + 1.4)
    fig, ax = _figure("Mots cherches au dictionnaire",
                      int(compte["occurrences"].sum()), hauteur=hauteur)

    ax.barh(range(len(compte)), compte["occurrences"], color=VERT,
            height=0.7)
    ax.set_yticks(range(len(compte)))
    ax.set_yticklabels(compte.index, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("recherches")

    return _sauver(fig, dossier, "lecture_mots")


def lecture_vs_sommeil(dossier: Path,
                       table: pd.DataFrame | None = None) -> Path | None:
    """Nuage temps de lecture x score de sommeil du lendemain.

    Produite seulement si les deux colonnes existent ET se recouvrent.
    Un nuage de deux points ne se distingue pas d'un nuage de deux cents
    quand on le regarde vite - d'ou le n en sous-titre, et le message
    d'avertissement dans le titre en dessous de 15 paires.
    """
    if table is None:
        table = reading.table_croisee()

    if table.empty:
        return None

    if "minutes" not in table.columns or "sommeil_score" not in table.columns:
        return None

    paires = table[["minutes", "sommeil_score"]].dropna()

    if len(paires) < 3:
        return None

    n = len(paires)
    fig, ax = _figure("Lecture et sommeil de la nuit suivante", n)

    ax.scatter(paires["minutes"], paires["sommeil_score"], s=70,
               color=BLEU, alpha=0.85, edgecolor="white", zorder=3)

    ax.set_xlabel("minutes lues dans la journee")
    ax.set_ylabel("score de sommeil")

    if n < reading.N_INSUFFISANT:
        ax.text(0.5, 0.5, f"n = {n} : indiscernable du hasard",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=13, color=AMBRE, alpha=0.55, fontweight="bold")

    return _sauver(fig, dossier, "lecture_vs_sommeil")


# ------------------------------------------------------------------ tout

def tout(dossier: Path) -> list[Path]:
    """Produit toutes les figures possibles. Les impossibles sont omises.

    Les donnees sont chargees UNE fois et passees a chaque fonction :
    sans cela, dix figures declenchent dix fois les memes requetes.
    """
    dossier = Path(dossier)

    sessions = load.sessions_lecture()
    annot = load.annotations_lecture()
    quotidien = load.lecture()
    catalogue = load.livres()
    historique = load.progression_livres()
    croisee = reading.table_croisee()

    candidats = [
        temps_quotidien(dossier, quotidien),
        carte_horaire(dossier, sessions),
        profil_horaire(dossier, sessions),
        profil_hebdomadaire(dossier, sessions),
        duree_sessions(dossier, sessions),
        vitesse(dossier, sessions),
        rythme_annotations(dossier, annot),
        top_livres(dossier, catalogue),
        densite_annotation(dossier, catalogue),
        mots_frequents(dossier, annot),
        progression_livres(dossier, historique),
        lecture_vs_sommeil(dossier, croisee),
    ]

    return [chemin for chemin in candidats if chemin is not None]
