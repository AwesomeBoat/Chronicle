"""Un rapport daté, rejouable, qui se suffit à lui-même (V3, étape 7).

Les modules précédents affichent dans une console : la sortie défile, on
la lit, elle disparaît. Un rapport est l'inverse — il reste, il se
compare à celui de la semaine dernière, et il porte assez de contexte
pour être lu dans six mois par quelqu'un qui a tout oublié.

Trois propriétés, dans l'ordre où elles comptent :

**Daté.** Un dossier par exécution, nommé par l'instant. Aucun rapport
n'écrase le précédent : le but est justement de pouvoir les empiler pour
voir comment le `n` grandit et à quel moment les conclusions changent.

**Auto-suffisant.** Le rapport rappelle sur quoi il porte — combien de
jours, quelle fenêtre, quelles limites — parce qu'un chiffre lu hors de
son contexte est un chiffre faux. Un `r = 0,82` de la semaine 1 et un
`r = 0,82` de la semaine 12 ne valent pas la même chose.

**Rejouable.** Comme `store` et `collect` en V1/V2 : relancer produit un
nouveau dossier, jamais un état à moitié écrit. Aucune donnée n'est
touchée — la couche analytics ne fait que lire.

Le format est du Markdown : lisible tel quel dans un éditeur, lisible
dans Obsidian, versionnable, et sans dépendance. Les figures sont
référencées en chemins relatifs, donc le dossier se déplace d'un bloc.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from analytics import correlate, describe, load, plots, quality

# Où atterrissent les rapports. Un sous-dossier par exécution.
RACINE = Path("reports")


def _capturer(fonction, *args, **kwargs) -> str:
    """Récupère ce qu'une fonction affiche, au lieu de le laisser défiler.

    redirect_stdout remplace temporairement la sortie standard par un
    tampon mémoire. C'est ce qui permet de réutiliser tels quels les
    `run_*` des étapes 3 à 6 sans les réécrire en deux versions — une
    qui affiche, une qui renvoie du texte.

    Le compromis est assumé : c'est un détournement, et il ne tiendrait
    pas si ces fonctions devaient un jour produire autre chose que du
    texte. À ce moment-là, il faudra les faire renvoyer des objets et
    n'afficher qu'en surface. Tant qu'elles impriment, autant ne pas
    dupliquer leur logique.
    """
    tampon = io.StringIO()

    with redirect_stdout(tampon):
        fonction(*args, **kwargs)

    return tampon.getvalue()


def _entete(quotidien) -> list[str]:
    """Le contexte sans lequel aucun chiffre du rapport n'a de sens."""
    renseignes = quotidien.dropna(how="all")

    lignes = [
        "# Rapport Chronicle",
        "",
        f"Généré le {datetime.now():%Y-%m-%d à %H:%M}.",
        "",
        "## Périmètre",
        "",
    ]

    if len(renseignes):
        lignes += [
            f"- Fenêtre en base : **{len(quotidien)} jours** "
            f"({quotidien.index.min():%Y-%m-%d} → "
            f"{quotidien.index.max():%Y-%m-%d})",
            f"- Jours porteurs d'au moins une mesure : "
            f"**{len(renseignes)}**",
        ]

    # Le n des grandeurs nocturnes : le chiffre qui commande tout le
    # reste du rapport, donc placé avant tout le reste du rapport.
    nocturnes = [c for c in quality.COLONNES_COEUR
                 if c in quotidien.columns]
    complets = quotidien[nocturnes].dropna() if nocturnes else quotidien.iloc[0:0]

    lignes += [
        f"- Jours où **toutes** les grandeurs cœur sont présentes : "
        f"**{len(complets)}**",
        "",
        "> Ce dernier nombre est le `n` réel de toute analyse croisée.",
        "> Il est presque toujours bien plus petit que la fenêtre en base,",
        "> et c'est lui qu'il faut lire à côté de chaque coefficient.",
        "",
    ]

    return lignes


def _section(titre: str, contenu: str, introduction: str = "") -> list[str]:
    """Une section : un titre, une phrase d'intention, la sortie brute.

    La sortie va dans un bloc de code : elle est déjà alignée en colonnes
    à la largeur d'une console, et le Markdown la casserait en tentant de
    la reformater.
    """
    lignes = ["", f"## {titre}", ""]

    if introduction:
        lignes += [introduction, ""]

    lignes += ["```", contenu.rstrip(), "```", ""]

    return lignes


def _galerie(figures: list[Path], dossier: Path) -> list[str]:
    """Insère les figures en chemins RELATIFS au rapport.

    Relatifs et non absolus : le dossier doit rester lisible après avoir
    été déplacé, copié sur une autre machine, ou glissé dans Obsidian.
    Un chemin en C:\\Users\\... ne survit à aucun des trois.
    """
    if not figures:
        return ["", "## Figures", "", "_Aucune figure produite._", ""]

    lignes = ["", "## Figures", ""]

    for chemin in figures:
        titre = chemin.stem.replace("_", " ")
        relatif = chemin.relative_to(dossier).as_posix()
        lignes += [f"### {titre}", "", f"![{titre}]({relatif})", ""]

    return lignes


def generer(racine: Path = RACINE) -> Path:
    """Produit un rapport complet. Renvoie le chemin du fichier écrit."""
    # Secondes comprises : a la minute pres, deux rapports lances coup sur
    # coup tombaient dans le meme dossier et le second ecrasait le premier
    # - exactement ce que la docstring de ce module promet d'eviter.
    # Trouve en validant la V3, pas en la codant.
    horodatage = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dossier = racine / horodatage
    dossier.mkdir(parents=True, exist_ok=True)

    quotidien = load.daily()

    # Les figures d'abord : le Markdown a besoin de leurs chemins.
    figures = plots.tout(dossier / "figures")

    lignes = _entete(quotidien)

    lignes += _section(
        "1. Qualité des données",
        _capturer(quality.run_quality),
        "Avant tout chiffre : ces données sont-elles utilisables ? "
        "Une collecte parfaite peut produire une colonne de zéros.",
    )

    lignes += _section(
        "2. Descriptif",
        _capturer(describe.run_describe),
        "Ce que sont les données, sans rien en conclure. "
        "Chaque centre est accompagné de sa dispersion et de son `n`.",
    )

    lignes += _section(
        "3. Corrélations",
        _capturer(correlate.run_correlate),
        "Les relations entre grandeurs — et les quatre raisons de ne pas "
        "y croire trop vite : tautologies, comparaisons multiples, "
        "petit `n`, dérive temporelle.",
    )

    lignes += _galerie(figures, dossier)

    lignes += [
        "",
        "---",
        "",
        "## Comment relire ce rapport",
        "",
        "1. Commencer par le `n` du périmètre, en haut. Il plafonne tout.",
        "2. Un coefficient sans son `n` et son `p` n'est pas un résultat.",
        "3. Une corrélation entre deux grandeurs de la même famille "
        "(pas / distance / calories) ne dit rien : Polar les dérive "
        "les unes des autres.",
        "4. Comparer avec le rapport précédent : c'est la façon de voir "
        "si une relation tient quand le `n` augmente, ou si elle "
        "s'évapore.",
        "",
        f"_Rapport produit par `python main.py report` — "
        f"{datetime.now():%Y-%m-%d %H:%M}._",
        "",
    ]

    chemin = dossier / "rapport.md"
    chemin.write_text("\n".join(lignes), encoding="utf-8")

    return chemin


def run_report(racine: str = "reports") -> None:
    """Génère le rapport et annonce où il est."""
    chemin = generer(Path(racine))
    figures = list((chemin.parent / "figures").glob("*.png"))

    print(f"Rapport ecrit : {chemin.resolve()}")
    print(f"  {len(figures)} figures dans {chemin.parent.name}/figures/")
    print(f"  {chemin.stat().st_size // 1024} Ko de Markdown")
