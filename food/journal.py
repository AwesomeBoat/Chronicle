"""Le journal alimentaire : un fichier texte par jour. (V4, etape 5)

    data/food/2026-08-26.txt

        # petit-dejeuner
        8h05, 2 oeufs, 30g flocons avoine, 200ml lait
        12h30, 100g riz cuit, 55g poulet, 50g brocoli
        16h. 1 banane 1/2 pomme

POURQUOI DU TEXTE, ET PAS UNE COMMANDE QUI ECRIT EN BASE
--------------------------------------------------------
Meme raison que `data/raw/` en V1 : ce qui entre dans la base doit
pouvoir etre **rejoue**. Un fichier texte donne trois choses qu'une
saisie directe ne donne pas :

- **relecture** : je peux corriger "50g brocoli" en "150g brocoli" le
  lendemain, et rejouer. L'idempotence de la V1 fait le reste.
- **portabilite** : ca se tape dans Obsidian, dans le bloc-notes, sur
  telephone. Aucun outil requis.
- **verite conservee** : le fichier reste ce que j'ai ECRIT ; la base
  contient ce que le programme en a COMPRIS. Les deux peuvent etre
  compares, ce qui est precieux quand le parseur evolue.

Le format est deliberement pauvre - une ligne par prise alimentaire,
les lignes vides et celles commencant par # sont ignorees. Tout ce qui
ressemble a une syntaxe serait une syntaxe a retenir, donc une raison
d'arreter de tenir le journal.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from config import BASE_DIR

JOURNAL_DIR = BASE_DIR / "data" / "food"


def chemin(jour: date) -> Path:
    """Le fichier du jour. Nom ISO : il se trie tout seul."""
    return JOURNAL_DIR / f"{jour.isoformat()}.txt"


def lire(jour: date) -> list[str]:
    """Les lignes utiles du journal d'un jour. Vide si pas de fichier."""
    fichier = chemin(jour)

    if not fichier.exists():
        return []

    lignes = fichier.read_text(encoding="utf-8").splitlines()

    return [ligne.strip() for ligne in lignes
            if ligne.strip() and not ligne.strip().startswith("#")]


def jours_disponibles() -> list[date]:
    """Tous les jours ayant un fichier, du plus ancien au plus recent."""
    if not JOURNAL_DIR.exists():
        return []

    jours = []

    for fichier in JOURNAL_DIR.glob("*.txt"):
        try:
            jours.append(date.fromisoformat(fichier.stem))
        except ValueError:
            # Un fichier au nom non conforme est ignore, pas fatal :
            # le dossier appartient a l'utilisateur, il peut y deposer
            # des notes.
            continue

    return sorted(jours)


def ajouter(ligne: str, jour: date | None = None) -> Path:
    """Ajoute une ligne au journal du jour. Renvoie le fichier ecrit.

    Le fichier est cree avec un en-tete date : ouvert trois mois plus
    tard, il doit se comprendre sans son nom de fichier.
    """
    jour = jour or date.today()
    fichier = chemin(jour)
    fichier.parent.mkdir(parents=True, exist_ok=True)

    if not fichier.exists():
        entete = (f"# Journal alimentaire - {jour:%A %d %B %Y}\n"
                  f"# Une ligne par prise : heure, puis les aliments.\n"
                  f"#   12h30, 100g riz cuit, 55g poulet\n\n")
        fichier.write_text(entete, encoding="utf-8")

    with fichier.open("a", encoding="utf-8") as sortie:
        sortie.write(ligne.strip() + "\n")

    return fichier
