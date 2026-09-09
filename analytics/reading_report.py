"""Tableau de bord de lecture : un fichier HTML autonome.

Meme contrat que analytics/report.py - date, auto-suffisant, rejouable -
avec une difference de format assumee : du HTML plutot que du Markdown.

POURQUOI DU HTML ICI
--------------------
report.py produit du Markdown parce que ses sorties se relisent dans
Obsidian et se versionnent. Ce tableau de bord a un autre usage : on
l'ouvre, on regarde, on ferme. Les figures y sont **encastrees en
base64**, donc le fichier se deplace, s'envoie et s'archive d'un bloc,
sans dossier d'images a trainer derriere lui.

CE QU'IL AFFICHE QUAND IL N'Y A RIEN
------------------------------------
Le piege d'un tableau de bord est de paraitre credible quel que soit le
nombre de points. Celui-ci fait l'inverse : chaque chiffre porte son
effectif, les constats sous le seuil sont grises, et le bandeau du haut
dit en toutes lettres ce qui manque. Un tableau de bord vide doit se
lire comme vide.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
from pathlib import Path

import pandas as pd

from analytics import correlate, reading, reading_plots

RACINE = Path("reports")

# Ordre d'apparition et legende de chaque figure. Une figure absente est
# simplement sautee - voir reading_plots.tout().
LEGENDES = {
    "lecture_temps_quotidien":
        "Minutes lues chaque jour. La courbe ambre est une moyenne sur "
        "7 jours, calculee seulement a partir de 4 points reels : mieux "
        "vaut une courbe courte qu'une courbe inventee. Les jours sans "
        "donnee interrompent la ligne au lieu d'etre combles.",
    "lecture_carte_horaire":
        "Minutes lues par jour de la semaine et par heure. C'est la figure "
        "qui revele les habitudes : lecture du soir, du week-end, du trajet.",
    "lecture_profil_horaire":
        "Le meme signal, replie sur 24 heures. Les heures sans lecture "
        "restent visibles en gris clair.",
    "lecture_profil_hebdo":
        "Minutes par jour de la semaine, DIVISEES par le nombre de fois ou "
        "ce jour apparait dans la fenetre. Sans cette division, un lundi de "
        "plus dans la periode suffirait a faire paraitre les lundis plus "
        "studieux.",
    "lecture_duree_sessions":
        "Distribution des durees. Les ouvertures de moins d'une minute sont "
        "ecartees : la liseuse enregistre une session des qu'un livre "
        "s'ouvre, meme trois secondes.",
    "lecture_vitesse":
        "Mots par minute, session par session. Calcule uniquement sur les "
        "sessions ou la liseuse a releve le compte de mots - il n'accompagne "
        "pas toutes les sessions.",
    "lecture_rythme_annotations":
        "Annotations par mois. C'est la seule serie longue de cette source : "
        "les annotations ont un historique complet, les sessions non.",
    "lecture_top_livres":
        "Les livres qui ont declenche le plus d'annotations, tous genres "
        "confondus.",
    "lecture_densite":
        "Surlignements rapportes a 10 000 positions. Sans cette "
        "normalisation, on classerait la longueur des livres plutot que "
        "l'interet qu'ils suscitent.",
    "lecture_mots":
        "Les mots cherches au dictionnaire, avec leur nombre de recherches.",
    "lecture_progression":
        "Avancement dans chaque livre. La courbe se remplit d'elle-meme a "
        "chaque synchronisation qui fait bouger une position.",
    "lecture_vs_sommeil":
        "Temps de lecture d'une journee contre score de sommeil de la nuit "
        "suivante. A lire avec le n : la V3 a montre qu'a n = 7, un |r| de "
        "0,96 s'obtient sur du bruit pur.",
}

STYLE = """
:root {
  --fond: #f7fafc; --carte: #ffffff; --texte: #1a202c; --doux: #4a5568;
  --tres-doux: #718096; --bord: #e2e8f0; --bleu: #2b6cb0; --ambre: #c05621;
  --vert: #2f855a; --rouge: #c53030;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 4rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--fond); color: var(--texte); line-height: 1.6;
}
.enveloppe { max-width: 1080px; margin: 0 auto; padding: 0 1.5rem; }
header {
  background: linear-gradient(135deg, #1a365d 0%, #2b6cb0 100%);
  color: #fff; padding: 2.5rem 0 2rem; margin-bottom: 2rem;
}
header h1 { margin: 0 0 .3rem; font-size: 1.9rem; letter-spacing: -.02em; }
header p { margin: 0; opacity: .82; font-size: .95rem; }
h2 {
  font-size: 1.15rem; margin: 2.5rem 0 1rem; padding-bottom: .5rem;
  border-bottom: 2px solid var(--bord); letter-spacing: -.01em;
}
.tuiles {
  display: grid; gap: 1rem; margin-bottom: 1rem;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
}
.tuile {
  background: var(--carte); border: 1px solid var(--bord); border-radius: 10px;
  padding: 1.1rem 1.2rem; box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
.tuile .valeur { font-size: 1.75rem; font-weight: 700; letter-spacing: -.02em; }
.tuile .unite { font-size: .95rem; font-weight: 500; color: var(--doux); }
.tuile .titre {
  font-size: .78rem; text-transform: uppercase; letter-spacing: .05em;
  color: var(--tres-doux); margin-bottom: .35rem;
}
.tuile .note { font-size: .78rem; color: var(--tres-doux); margin-top: .3rem; }
.tuile.faible { opacity: .55; }
.tuile.faible .valeur { color: var(--doux); }
.badge {
  display: inline-block; font-size: .68rem; font-weight: 600; padding: .1rem .45rem;
  border-radius: 4px; text-transform: uppercase; letter-spacing: .04em;
  margin-top: .4rem;
}
.badge.exploitable { background: #c6f6d5; color: #22543d; }
.badge.indicatif   { background: #feebc8; color: #7b341e; }
.badge.insuffisant { background: #fed7d7; color: #742a2a; }
.badge.aucune      { background: var(--bord); color: var(--doux); }
.alerte {
  background: #fffaf0; border-left: 4px solid var(--ambre); border-radius: 6px;
  padding: 1rem 1.2rem; margin-bottom: 1rem;
}
.alerte ul { margin: .4rem 0 0; padding-left: 1.2rem; }
.alerte li { margin-bottom: .45rem; font-size: .92rem; color: var(--doux); }
figure {
  background: var(--carte); border: 1px solid var(--bord); border-radius: 10px;
  padding: 1.2rem; margin: 0 0 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
figure img { width: 100%; height: auto; display: block; }
figcaption {
  font-size: .87rem; color: var(--doux); margin-top: .9rem;
  padding-top: .9rem; border-top: 1px solid var(--bord);
}
.tableau-enveloppe { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .88rem;
        background: var(--carte); border-radius: 10px; overflow: hidden; }
th, td { padding: .55rem .8rem; text-align: left; border-bottom: 1px solid var(--bord); }
th { background: #edf2f7; font-weight: 600; font-size: .78rem;
     text-transform: uppercase; letter-spacing: .04em; color: var(--doux); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #f7fafc; }
.vide {
  background: var(--carte); border: 1px dashed var(--bord); border-radius: 10px;
  padding: 2rem; text-align: center; color: var(--tres-doux); font-size: .92rem;
}
footer { margin-top: 3rem; font-size: .82rem; color: var(--tres-doux);
         border-top: 1px solid var(--bord); padding-top: 1rem; }
code { background: #edf2f7; padding: .1rem .35rem; border-radius: 4px;
       font-size: .86em; }
"""


def _echapper(valeur) -> str:
    return html.escape("" if valeur is None else str(valeur))


def _image_encastree(chemin: Path) -> str:
    """Encode une figure en base64 pour rendre le HTML autonome."""
    donnees = base64.b64encode(chemin.read_bytes()).decode("ascii")

    return f"data:image/png;base64,{donnees}"


def _tuile(constat: reading.Constat) -> str:
    classe = "" if constat.n >= reading.N_INSUFFISANT else " faible"
    badge = constat.confiance.replace("aucune donnee", "aucune")

    if constat.valeur is None:
        valeur = "--"
    elif isinstance(constat.valeur, float):
        valeur = f"{constat.valeur:,.1f}".replace(",", " ")
    else:
        valeur = f"{constat.valeur:,}".replace(",", " ")

    unite = (f' <span class="unite">{_echapper(constat.unite)}</span>'
             if constat.unite else "")
    note = (f'<div class="note">{_echapper(constat.note)}</div>'
            if constat.note else "")

    return (f'<div class="tuile{classe}">'
            f'<div class="titre">{_echapper(constat.titre)}</div>'
            f'<div class="valeur">{valeur}{unite}</div>'
            f'{note}'
            f'<span class="badge {badge.split()[0]}">{_echapper(badge)}'
            f' &middot; n={constat.n}</span>'
            f'</div>')


def _table_html(df: pd.DataFrame, colonnes: dict[str, str],
                lignes: int = 15) -> str:
    """Un tableau HTML depuis un DataFrame, colonnes renommees."""
    if df is None or df.empty:
        return '<div class="vide">Aucune donnee.</div>'

    presentes = [c for c in colonnes if c in df.columns]

    if not presentes:
        return '<div class="vide">Aucune colonne exploitable.</div>'

    extrait = df[presentes].head(lignes)
    entetes = "".join(
        f'<th class="num">{_echapper(colonnes[c])}</th>'
        if pd.api.types.is_numeric_dtype(df[c])
        else f"<th>{_echapper(colonnes[c])}</th>"
        for c in presentes)

    corps = []

    for _, ligne in extrait.iterrows():
        cellules = []

        for colonne in presentes:
            valeur = ligne[colonne]

            if pd.isna(valeur):
                cellules.append('<td class="num">--</td>')
            elif isinstance(valeur, float):
                cellules.append(f'<td class="num">{valeur:,.1f}</td>'
                                .replace(",", " "))
            elif isinstance(valeur, (int,)) and not isinstance(valeur, bool):
                cellules.append(f'<td class="num">{valeur}</td>')
            else:
                cellules.append(f"<td>{_echapper(valeur)}</td>")

        corps.append("<tr>" + "".join(cellules) + "</tr>")

    return ('<div class="tableau-enveloppe"><table><thead><tr>'
            + entetes + "</tr></thead><tbody>"
            + "".join(corps) + "</tbody></table></div>")


def _section_croisements(resultats: list) -> str:
    """Les correlations, avec un refus explicite quand n est trop petit."""
    if not resultats:
        return ('<div class="vide">Aucun croisement calculable : il faut des '
                'jours ou lecture ET sommeil (ou activite, ou alimentation) '
                'sont tous deux mesures.</div>')

    lignes = []

    for resultat in resultats:
        question = reading.question_de(resultat.x, resultat.y)
        assez = resultat.n >= reading.N_INDICATIF

        verdict = ("a interpreter" if assez
                   else "indiscernable du hasard")
        couleur = "exploitable" if assez else "insuffisant"

        decalage = (f" (J-{resultat.decalage})" if resultat.decalage
                    else "")

        lignes.append(
            f"<tr><td>{_echapper(question)}</td>"
            f"<td>{_echapper(resultat.x)}{decalage} &rarr; "
            f"{_echapper(resultat.y)}</td>"
            f'<td class="num">{resultat.r:+.2f}</td>'
            f'<td class="num">{resultat.n}</td>'
            f'<td class="num">{resultat.p:.3f}</td>'
            f'<td><span class="badge {couleur}">{verdict}</span></td></tr>')

    return ('<div class="tableau-enveloppe"><table><thead><tr>'
            "<th>Question</th><th>Paire</th>"
            '<th class="num">r</th><th class="num">n</th>'
            '<th class="num">p</th><th>Verdict</th>'
            "</tr></thead><tbody>" + "".join(lignes)
            + "</tbody></table></div>")


def generer(racine: Path = RACINE) -> Path:
    """Produit le tableau de bord. Renvoie le chemin du HTML."""
    racine = Path(racine)
    horodatage = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    dossier = racine / f"lecture-{horodatage}"
    figures_dir = dossier / "figures"
    dossier.mkdir(parents=True, exist_ok=True)

    rapport = reading.resume()
    figures = reading_plots.tout(figures_dir)
    resultats = reading.croisements()

    sans_texte = reading.surlignements_sans_texte(
        rapport.tables.get("annotations"))
    rapport.ajouter(sans_texte)

    # --- bandeau
    parties = [
        "<header><div class=\"enveloppe\">",
        "<h1>Lecture &mdash; tableau de bord</h1>",
        f"<p>Genere le {dt.datetime.now():%d/%m/%Y a %H:%M} "
        "&middot; source <code>kindle_paperwhite</code></p>",
        "</div></header>",
        '<div class="enveloppe">',
    ]

    # --- avertissements d'abord : ils conditionnent la lecture du reste
    if rapport.avertissements:
        parties.append('<div class="alerte"><strong>A lire avant les '
                       "chiffres</strong><ul>")
        parties += [f"<li>{_echapper(m)}</li>"
                    for m in rapport.avertissements]
        parties.append("</ul></div>")

    # --- tuiles
    parties.append("<h2>Vue d'ensemble</h2>")
    parties.append('<div class="tuiles">')
    parties += [_tuile(c) for c in rapport.constats]
    parties.append("</div>")

    # --- figures
    if figures:
        parties.append("<h2>Figures</h2>")

        for chemin in figures:
            legende = LEGENDES.get(chemin.stem, "")
            parties.append(
                "<figure>"
                f'<img alt="{_echapper(chemin.stem)}" '
                f'src="{_image_encastree(chemin)}">'
                + (f"<figcaption>{_echapper(legende)}</figcaption>"
                   if legende else "")
                + "</figure>")
    else:
        parties.append("<h2>Figures</h2>")
        parties.append('<div class="vide">Aucune figure produite : il n\'y a '
                       "pas encore assez de donnees. Les figures "
                       "apparaitront d'elles-memes.</div>")

    # --- livres
    parties.append("<h2>Livres</h2>")
    parties.append(_table_html(
        reading.classement_livres(rapport.tables.get("livres")),
        {"titre": "Titre", "auteur": "Auteur", "progression_pct": "%",
         "minutes": "Minutes", "sessions": "Sessions",
         "surlignements": "Surlign.", "notes": "Notes",
         "mots_cherches": "Mots cherches"},
        lignes=20))

    # --- croisements
    parties.append("<h2>Croisements avec les autres sources</h2>")
    parties.append(
        "<p style=\"color:var(--doux);font-size:.92rem;margin-top:-.4rem\">"
        "C'est ici que la centralisation en une seule base paie : ces "
        "colonnes viennent de sources qui ne se connaissent pas, et seule "
        "la date les rapproche.</p>")
    parties.append(_section_croisements(resultats))

    # --- mots
    parties.append("<h2>Vocabulaire</h2>")
    parties.append(_table_html(
        reading.mots_frequents(rapport.tables.get("annotations"), top=20),
        {"occurrences": "Recherches", "livres": "Livres"},
        lignes=20))

    parties.append(
        "<footer>Produit par <code>python main.py lecture</code>. "
        "Chaque execution cree un dossier date : aucun rapport n'ecrase le "
        "precedent, ce qui permet de comparer et de voir grandir le n. "
        "Les figures sont encastrees dans ce fichier, qui se deplace donc "
        "d'un bloc.</footer>")
    parties.append("</div>")

    page = ("<!doctype html><html lang=\"fr\"><head>"
            "<meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\">"
            "<title>Lecture - tableau de bord</title>"
            f"<style>{STYLE}</style></head><body>"
            + "".join(parties) + "</body></html>")

    chemin = dossier / "index.html"
    chemin.write_text(page, encoding="utf-8")

    return chemin


def run_reading_report(racine: str = "reports") -> Path:
    """Commande `lecture` : genere et affiche le chemin."""
    rapport = reading.resume()

    print("=" * 62)
    print("LECTURE")
    print("=" * 62)

    for constat in rapport.constats:
        print(f"  {constat}")

    if rapport.avertissements:
        print("\n  A savoir :")

        for message in rapport.avertissements:
            print(f"   [!] {message}")

    chemin = generer(Path(racine))

    print(f"\nTableau de bord : {chemin}")
    print(f"  {len(list(chemin.parent.glob('figures/*.png')))} figure(s)")

    return chemin
