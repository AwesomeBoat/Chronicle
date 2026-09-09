"""Table CIQUAL (ANSES) : la reference nutritionnelle. (V4, etape 2)

CIQUAL est la table officielle francaise de composition des aliments :
3 185 aliments, publiee par l'ANSES, telechargeable une fois puis
utilisable hors ligne. C'est elle qui sait que 100 g de riz blanc cuit
font 131 kcal.

DONNEE DE REFERENCE, PAS DONNEE PERSONNELLE
-------------------------------------------
CIQUAL ne va PAS dans PostgreSQL. La base est reservee a ce qui me
concerne - mes mesures, mes repas, mes nuits. Une table publique que
n'importe qui peut retelecharger n'a rien a y faire : elle la ferait
grossir de 3 185 lignes identiques pour tout le monde, et melangerait
deux choses de nature differente.

Elle est donc parsee une fois vers data/ciqual/aliments.json, un fichier
regenerable qu'on peut effacer sans rien perdre.

Consequence importante : les valeurs d'un repas sont **figees a
l'ingestion**. Le mapper calcule les kcal au moment ou il lit le journal
et les stocke comme observations. Si CIQUAL 2024 revise le riz, mes
repas de 2026 gardent les valeurs de 2020 - c'est le comportement
correct : une observation ne se reecrit pas.

FORMAT DU XML
-------------
Trois fichiers dans l'archive, en base relationnelle deguisee :

    alim   : alim_code -> nom francais, groupe
    const  : const_code -> nom du constituant ("Proteines (g/100 g)")
    compo  : (alim_code, const_code) -> teneur

Deux pieges du fichier :

1. ENCODAGE windows-1252, declare comme tel dans l'en-tete XML. Le lire
   en UTF-8 donne "Prot\\xe9ines" et casse toutes les recherches sur les
   accents.

2. TENEURS NON NUMERIQUES. Le champ contient parfois "traces", "-", ou
   "< 0,5". Ce sont des informations, pas des nombres : "traces" veut
   dire mesure et negligeable, "-" veut dire jamais mesure. Les
   confondre avec 0 fausse les totaux vers le bas. Ici : "traces" et
   "< x" deviennent 0.0, "-" devient None (absent du resultat).
"""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from pathlib import Path

from config import BASE_DIR

CIQUAL_DIR = BASE_DIR / "data" / "ciqual"
ARCHIVE = CIQUAL_DIR / "ciqual_2020_xml.zip"
ALIMENTS_JSON = CIQUAL_DIR / "aliments.json"

URL = ("https://ciqual.anses.fr/cms/sites/default/files/inline-files/"
       "XML_2020_07_07.zip")

# Les constituants retenus. CIQUAL en publie plus de 60 (vitamines,
# mineraux, acides gras detailles) ; ceux-la suffisent a un journal
# alimentaire et gardent le fichier leger.
#
# Energie : le code 328 est celui du reglement UE 1169/2011, celui qui
# figure sur les etiquettes. Le 333 (facteur de Jones) donne des valeurs
# legerement differentes - prendre les deux serait une source de
# confusion permanente.
CONSTITUANTS = {
    "328":   ("energy_kcal", "kcal"),
    "25000": ("protein_g",   "g"),
    "31000": ("carb_g",      "g"),
    "32000": ("sugar_g",     "g"),
    "40000": ("fat_g",       "g"),
    "40302": ("satfat_g",    "g"),
    "34100": ("fiber_g",     "g"),
    "10004": ("salt_g",      "g"),
    "400":   ("water_g",     "g"),
    "60000": ("alcohol_g",   "g"),
}


# ----------------------------------------------------------------- texte

def normaliser(texte: str) -> str:
    """Minuscules, sans accents, sans ponctuation : la forme de recherche.

    "Brocoli, cuit a l'eau" et "brocoli cuit" doivent pouvoir se
    rencontrer. NFD decompose "e" en "e" + accent, et la comprehension
    jette les accents (categorie Mn = Mark, nonspacing).

    Sans ca, tout le systeme de recherche depend de la facon dont on
    tape ses accents a 23h dans un journal alimentaire.
    """
    texte = unicodedata.normalize("NFD", texte.lower())
    texte = "".join(c for c in texte
                    if unicodedata.category(c) != "Mn")

    return " ".join(re.sub(r"[^a-z0-9]+", " ", texte).split())


# ----------------------------------------------------------------- parsing

def _teneur(brut: str) -> float | None:
    """Convertit une teneur CIQUAL en nombre, ou None si non mesuree.

    Les cas reels rencontres dans le fichier :
        "131"      -> 131.0
        "1,5"      -> 1.5      (virgule decimale francaise)
        "traces"   -> 0.0      (mesure, et negligeable)
        "< 0,5"    -> 0.0      (sous le seuil de quantification)
        "-"        -> None     (jamais mesure : ce n'est PAS zero)

    Confondre le dernier cas avec 0 sous-estimerait silencieusement les
    totaux d'un repas.
    """
    brut = brut.strip()

    if not brut or brut == "-":
        return None

    if brut.lower().startswith("traces"):
        return 0.0

    nombre = re.search(r"-?\d+(?:[.,]\d+)?", brut)

    if nombre is None:
        return None

    valeur = float(nombre.group().replace(",", "."))

    # "< 0,5" : la valeur exacte est inconnue mais bornee. La compter
    # comme 0 sous-estime ; la compter comme 0,5 surestime. Sur des
    # constituants a l'etat de trace, 0 est l'erreur la plus faible.
    return 0.0 if brut.startswith("<") else valeur


def _extraire(xml: str, motif: str) -> list[tuple[str, ...]]:
    """Petit extracteur par expression reguliere.

    Un vrai parseur XML (ElementTree) serait plus propre. Il est evite
    ici parce que compo_2020_07_07.xml fait 57 Mo : ElementTree en
    construirait l'arbre complet en memoire (plusieurs centaines de Mo)
    la ou une expression reguliere le balaie en un passage.

    C'est un compromis assume et localise : le format est fige, le
    fichier vient d'une source unique, et la structure est plate.
    """
    return re.findall(motif, xml, re.S)


def construire(archive: Path = ARCHIVE) -> list[dict]:
    """Lit l'archive et renvoie la liste des aliments avec leurs valeurs."""
    if not archive.exists():
        raise FileNotFoundError(
            f"Archive CIQUAL absente : {archive}\n"
            f"La telecharger depuis {URL}")

    with zipfile.ZipFile(archive) as zip_ciqual:
        noms = zip_ciqual.namelist()
        fichier_alim = next(n for n in noms if n.startswith("alim_"))
        fichier_compo = next(n for n in noms if n.startswith("compo_"))

        # cp1252 = windows-1252. Declare dans l'en-tete du XML.
        alim_xml = zip_ciqual.read(fichier_alim).decode("cp1252")
        compo_xml = zip_ciqual.read(fichier_compo).decode("cp1252")

    # --- les aliments
    aliments: dict[str, dict] = {}

    for code, nom in _extraire(
            alim_xml,
            r"<alim_code>(.*?)</alim_code>.*?<alim_nom_fr>(.*?)</alim_nom_fr>"):
        code, nom = code.strip(), nom.strip()
        aliments[code] = {
            "code": code,
            "nom": nom,
            "recherche": normaliser(nom),
            "valeurs": {},
        }

    # --- les teneurs
    for alim_code, const_code, teneur in _extraire(
            compo_xml,
            r"<alim_code>(.*?)</alim_code>\s*"
            r"<const_code>(.*?)</const_code>\s*"
            r"<teneur>(.*?)</teneur>"):

        const_code = const_code.strip()

        if const_code not in CONSTITUANTS:
            continue

        aliment = aliments.get(alim_code.strip())

        if aliment is None:
            continue

        valeur = _teneur(teneur)

        if valeur is not None:
            aliment["valeurs"][CONSTITUANTS[const_code][0]] = valeur

    return list(aliments.values())


def importer(archive: Path = ARCHIVE, cible: Path = ALIMENTS_JSON) -> int:
    """Parse l'archive et ecrit le JSON. Renvoie le nombre d'aliments."""
    aliments = construire(archive)

    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(json.dumps(aliments, ensure_ascii=False),
                     encoding="utf-8")

    return len(aliments)


# --------------------------------------------------------------- recherche

_CACHE: list[dict] | None = None


def charger() -> list[dict]:
    """Les aliments, charges une seule fois par execution."""
    global _CACHE

    if _CACHE is None:
        if not ALIMENTS_JSON.exists():
            raise FileNotFoundError(
                f"{ALIMENTS_JSON} absent - lancer : python main.py ciqual")

        _CACHE = json.loads(ALIMENTS_JSON.read_text(encoding="utf-8"))

    return _CACHE


def chercher(requete: str, limite: int = 8) -> list[tuple[int, dict]]:
    """Cherche un aliment. Renvoie [(score, aliment)], meilleur en tete.

    Le score n'a pas de sens absolu : il ne sert qu'a ordonner, et a
    permettre au parseur de dire "je ne suis pas sur" (voir parser.py).

    Le classement raisonne en MOTS, pas en sous-chaines. La premiere
    version comparait des sous-chaines et classait "Vermicelle de riz,
    cuite" avant "Riz blanc, cuit" sur la requete "riz cuit" : la suite
    de caracteres "riz cuit" apparait bien dans "...de riz, cuite...".
    Un utilisateur qui tape "riz cuit" veut du riz.

      1000  le nom normalise est exactement la requete
       600  le nom commence par la requete entiere
       400  le PREMIER mot du nom repond au premier mot cherche,
             et tous les autres mots cherches sont presents
       200  tous les mots cherches sont presents, n'importe ou

    Un mot "repond" a un autre si l'un prefixe l'autre (4 lettres au
    moins) : c'est ce qui fait tenir "cuit" avec "cuite", et "pomme"
    avec "pommes", sans embarquer un vrai analyseur morphologique.

    Puis une penalite de longueur : a categorie egale, "Pomme, crue"
    bat "Pomme de terre, vapeur, avec peau". Elle reste inferieure a
    l'ecart entre deux categories, donc ne peut jamais en inverser une.
    """
    besoin = normaliser(requete)

    if not besoin:
        return []

    mots = besoin.split()
    resultats: list[tuple[int, dict]] = []

    for aliment in charger():
        nom = aliment["recherche"]
        mots_nom = nom.split()

        if nom == besoin:
            score = 1000
        elif _prefixe_de_mots(mots, mots_nom):
            score = 600
        elif all(_repond(mot, mots_nom) for mot in mots):
            score = 400 if _mots_compatibles(mots[0], mots_nom[0]) else 200
        else:
            continue

        resultats.append((score - min(len(mots_nom) * 4, 60), aliment))

    resultats.sort(key=lambda paire: (-paire[0], len(paire[1]["nom"])))

    return resultats[:limite]


def _mots_compatibles(cherche: str, candidat: str) -> bool:
    """Un mot en prefixe-t-il un autre ? Sert de racinisation pauvre.

    Le seuil de 4 lettres evite que "ri" ouvre sur tout le dictionnaire,
    tout en laissant passer les paires qui comptent : cuit/cuite,
    pomme/pommes, poulet/poulets.
    """
    if cherche == candidat:
        return True

    court, long = sorted((cherche, candidat), key=len)

    return len(court) >= 4 and long.startswith(court)


def _repond(mot: str, mots_nom: list[str]) -> bool:
    """Le mot cherche trouve-t-il un correspondant dans le nom ?"""
    return any(_mots_compatibles(mot, candidat) for candidat in mots_nom)


def _prefixe_de_mots(mots: list[str], mots_nom: list[str]) -> bool:
    """La requete est-elle le debut du nom, mot a mot ?

    Compare des LISTES DE MOTS, et non deux chaines de caracteres. La
    premiere version testait nom.startswith(requete) et se faisait
    piegier par les pluriels : sur "oeufs", "Oeufs de lompe,
    semi-conserve" commence litteralement par "oeufs " et raflait 600
    points, tandis que "Oeuf, cru" - le resultat attendu - plafonnait a
    400 faute de commencer par la meme suite de caracteres.

    Resultat : deux oeufs au petit-dejeuner devenaient des oeufs de
    lompe. Exactement l'echec silencieux que ce connecteur est cense
    rendre impossible.
    """
    if len(mots) > len(mots_nom):
        return False

    return all(_mots_compatibles(mot, mots_nom[rang])
               for rang, mot in enumerate(mots))


def run_ciqual(requete: str | None = None) -> int:
    """Importe la table, ou cherche dedans si une requete est donnee."""
    if requete:
        trouves = chercher(requete)

        if not trouves:
            print(f"Aucun aliment pour : {requete}")
            return 1

        print(f"{len(trouves)} resultat(s) pour : {requete}\n")
        print(f"{'score':>6}  {'kcal':>6}  {'prot':>5}  {'gluc':>5}  "
              f"{'lip':>5}  nom")
        print("-" * 78)

        def cellule(valeurs: dict, cle: str, largeur: int, decimales: int
                    ) -> str:
            """Affiche la valeur, ou '-' si CIQUAL ne l'a jamais mesuree.

            La distinction n'est pas cosmetique : "Brocoli, cru" n'a
            AUCUNE valeur energetique dans CIQUAL. L'afficher a 0 kcal
            laisserait croire que le brocoli cru est sans calories.
            """
            if cle not in valeurs:
                return f"{'-':>{largeur}}"

            return f"{valeurs[cle]:>{largeur}.{decimales}f}"

        for score, aliment in trouves:
            v = aliment["valeurs"]
            print(f"{score:>6}  {cellule(v, 'energy_kcal', 6, 0)}  "
                  f"{cellule(v, 'protein_g', 5, 1)}  "
                  f"{cellule(v, 'carb_g', 5, 1)}  "
                  f"{cellule(v, 'fat_g', 5, 1)}  {aliment['nom']}")

        print("\nValeurs pour 100 g.  '-' = jamais mesure par CIQUAL "
              "(ce n'est pas zero).")
        return 0

    if not ARCHIVE.exists():
        print(f"Archive absente : {ARCHIVE}")
        print(f"La telecharger depuis :\n  {URL}")
        return 1

    print("Parsing de l'archive CIQUAL (57 Mo de XML, ~15 s)...")
    total = importer()

    print(f"{total} aliments ecrits dans {ALIMENTS_JSON.name} "
          f"({ALIMENTS_JSON.stat().st_size // 1024} Ko)")
    print("Donnee de reference : regenerable, hors PostgreSQL.")

    return 0
