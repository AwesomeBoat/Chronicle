"""Lire une ligne de journal ecrite par un humain. (V4, etape 4)

L'etape la plus delicate du projet, et pas pour des raisons techniques.

Un parseur de JSON qui se trompe leve une exception. Un parseur de texte
humain, lui, **reussit quand meme** : il comprend "pomme" la ou j'ai
ecrit "pomme de terre", produit 52 kcal au lieu de 120, et personne
n'est prevenu. L'erreur entre en base, se moyenne, se trace, et ressort
six mois plus tard dans une correlation.

    REGLE DE LA VERSION : ce qui n'est pas compris avec certitude est
    REFUSE, jamais devine.

Concretement, ce module ne renvoie jamais "a peu pres ca". Il renvoie
soit un element resolu, soit un refus portant la raison exacte. C'est
`main.py eat` qui decide quoi en faire - refuser la ligne, demander une
precision - mais rien d'ambigu n'atteint la base.

CE QU'IL FAUT SAVOIR LIRE
-------------------------
Deux lignes reelles, et tout ce qui les separe :

    12h30, 100g de riz cuit, 55g de poulet, 50g de broccoli
    16h. 1 banane 1/2 pomme

- l'heure s'ecrit "12h30" ou "16h", suivie d'une virgule OU d'un point
- les aliments sont separes par des virgules... ou par rien du tout
  ("1 banane 1/2 pomme" est une seule sequence sans separateur)
- la quantite est un poids ("100g"), un compte ("1"), ou une fraction
  ("1/2")
- "de" est du bruit : "100g de riz" et "100g riz" sont la meme chose
- "broccoli" n'est pas dans CIQUAL, qui ecrit "Brocoli". Une faute de
  frappe ne doit pas faire echouer une ligne entiere.

La partie la moins evidente est le decoupage SANS separateur. La regle
retenue : une nouvelle quantite commence un nouvel element. C'est ce qui
permet de couper "1 banane 1/2 pomme" en deux, et c'est aussi pourquoi
"1/2" doit etre reconnu comme UNE quantite et non comme "1" suivi de
"/2".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from config import LOCAL_TZ
from food import portions
from food.aliases import resoudre_alias
from food.ciqual import chercher, normaliser

LOCAL = ZoneInfo(LOCAL_TZ)

# Ecart d'energie tolere entre deux candidats a egalite avant de refuser.
# 25% : au-dela, la ligne du journal serait fausse d'un quart, ce qui
# depasse largement l'imprecision deja acceptee sur les portions.
ECART_TOLERE = 0.25

# Mots vides : presents dans le texte, sans valeur pour la recherche.
# "de" est le plus frequent ("100g DE riz"), et le retirer evite d'aller
# chercher un aliment nomme "de riz".
LIAISONS = {"de", "du", "des", "d", "la", "le", "les", "l", "un", "une",
            "et", "avec", "a", "au", "aux", "en"}

# Fractions ecrites en un seul caractere. Un journal tape sur telephone
# en contient, et "1/2" saisi dans Word devient parfois "1⁄2" (avec un
# separateur different de la barre oblique ASCII).
FRACTIONS_UNICODE = {
    "½": 0.5, "¼": 0.25, "¾": 0.75,
    "⅓": 1 / 3, "⅔": 2 / 3,
}

# --- l'heure en tete de ligne : "12h30", "16h", "8:05", "07h05"
#
# Le (?![\dA-Za-z]) apres les minutes n'est pas une precaution : sans lui
# le parseur produit une donnee fausse en silence.
#
#     "9h 100g riz cuit"
#
# Les minutes optionnelles avalaient le "10" de "100g" -> 09:10, et il
# restait "0g riz cuit", soit un repas a ZERO gramme. Aucune exception,
# aucun refus : 0 kcal entraient en base.
#
# La lettre est aussi interdite, pas seulement le chiffre : sinon
# "9h 30g riz" se lirait 09:30 avec un aliment sans quantite. Des
# minutes ne sont des minutes que si rien ne les prolonge.
HEURE = re.compile(r"^\s*(\d{1,2})\s*[h:]\s*(\d{2})(?![\dA-Za-z])\s*[.,;-]?\s*"
                   r"|^\s*(\d{1,2})\s*[h:]\s*[.,;-]?\s*",
                   re.IGNORECASE)

# --- une quantite : fraction ASCII, decimal, ou entier
QUANTITE = re.compile(r"""
    (?P<fraction>\d+\s*/\s*\d+)          # 1/2, 3 / 4
    | (?P<decimal>\d+[.,]\d+)            # 1,5   0.5
    | (?P<entier>\d+)                    # 100   1
""", re.VERBOSE)

# Unites reconnues, les plus longues d'abord pour que "cuillere a soupe"
# soit essaye avant "c". Construit une seule fois a l'import.
_UNITES_CONNUES = sorted(
    set(portions.MESURES) | {normaliser(u) for u in portions.MESURES},
    key=len, reverse=True)


@dataclass(slots=True)
class Element:
    """Un aliment d'un repas, tel qu'ecrit puis tel que compris."""

    texte: str                    # ce qui etait ecrit
    quantite: float
    unite: str | None             # "g", "cl", None si c'est un compte
    aliment: str                  # le nom nettoye, pour la recherche

    # Rempli par resoudre()
    ciqual_nom: str | None = None
    ciqual_code: str | None = None
    grammes: float | None = None
    estime: bool = False          # poids venu de portions.py, pas d'une pesee
    note: str | None = None       # ce qu'il faut savoir sur cette ligne


@dataclass(slots=True)
class Refus:
    """Un morceau non compris. Porte sa raison, pour etre corrige."""

    texte: str
    raison: str


@dataclass(slots=True)
class Repas:
    """Une ligne de journal, analysee."""

    ligne: str
    moment: datetime | None = None
    elements: list[Element] = field(default_factory=list)
    refus: list[Refus] = field(default_factory=list)

    @property
    def complet(self) -> bool:
        """Rien n'a ete refuse, et il y a quelque chose a enregistrer."""
        return not self.refus and bool(self.elements) and self.moment is not None


# ------------------------------------------------------------------ heure

def lire_heure(ligne: str, jour: date) -> tuple[datetime | None, str]:
    """Detache l'heure de tete. Renvoie (instant, reste de la ligne).

    L'instant est ancre dans LOCAL_TZ - comme partout dans le projet
    depuis la V1 : "12h30" est une heure d'horloge, elle ne devient un
    instant absolu qu'avec un fuseau.
    """
    trouve = HEURE.match(ligne)

    if trouve is None:
        return None, ligne

    # L'expression a deux alternatives (avec et sans minutes), donc deux
    # jeux de groupes. Une seule branche s'applique, l'autre est None.
    heures = int(trouve.group(1) or trouve.group(3))
    minutes = int(trouve.group(2) or 0)

    if heures > 23 or minutes > 59:
        return None, ligne

    moment = datetime.combine(jour, time(heures, minutes), tzinfo=LOCAL)

    return moment, ligne[trouve.end():]


# -------------------------------------------------------------- quantites

def _valeur(trouve: re.Match) -> float:
    """Convertit une quantite reconnue en nombre."""
    if trouve.group("fraction"):
        haut, bas = trouve.group("fraction").split("/")
        return float(haut.strip()) / float(bas.strip())

    if trouve.group("decimal"):
        return float(trouve.group("decimal").replace(",", "."))

    return float(trouve.group("entier"))


def _detacher_unite(reste: str) -> tuple[str | None, str]:
    """Detache une unite collee ou espacee : "100g riz" -> ("g", "riz")."""
    nu = reste.lstrip()

    for unite in _UNITES_CONNUES:
        if not nu.lower().startswith(unite):
            continue

        suite = nu[len(unite):]

        # L'unite doit finir le mot : sinon "g" avalerait le "g" de
        # "gateau", et "l" celui de "lait".
        if suite and (suite[0].isalnum()):
            continue

        return unite, suite

    return None, nu


def _nettoyer(texte: str) -> str:
    """Retire les liaisons et la ponctuation : le nom a chercher."""
    mots = [mot for mot in normaliser(texte).split() if mot not in LIAISONS]

    return " ".join(mots)


def decouper(texte: str) -> list[tuple[float, str | None, str]]:
    """Coupe la partie aliments en (quantite, unite, nom).

    Le decoupage se fait sur les QUANTITES, pas sur la ponctuation :
    c'est la seule facon de traiter "1 banane 1/2 pomme", ou aucun
    separateur n'existe. Chaque quantite rencontree ouvre un element et
    ferme le precedent.
    """
    # Les fractions d'un seul caractere sont remplacees par leur ecriture
    # ASCII avant tout le reste : la suite du code n'a alors qu'une seule
    # forme a connaitre.
    for signe, valeur in FRACTIONS_UNICODE.items():
        texte = texte.replace(signe, f" {valeur:g} ")

    debuts = list(QUANTITE.finditer(texte))

    if not debuts:
        return []

    elements = []

    for rang, trouve in enumerate(debuts):
        fin = debuts[rang + 1].start() if rang + 1 < len(debuts) else len(texte)
        quantite = _valeur(trouve)
        unite, reste = _detacher_unite(texte[trouve.end():fin])
        nom = _nettoyer(reste)

        if nom:
            elements.append((quantite, unite, nom))

    return elements


# ------------------------------------------------------- resolution CIQUAL

def _distance_un(a: str, b: str) -> bool:
    """Les deux mots different-ils d'au plus une lettre ?

    Rattrape les fautes de frappe courantes - "broccoli" pour "brocoli",
    "brocolli", "yaourth". Volontairement limite a UNE difference : au
    dela, le risque de confondre deux aliments reels ("pomme"/"gomme",
    "riz"/"ris") depasse le service rendu.

    Implementation naive en O(n*m) : les mots font moins de 20 lettres.
    """
    if abs(len(a) - len(b)) > 1:
        return False

    if a == b:
        return True

    # Substitution : meme longueur, une seule position differente.
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1

    # Insertion / suppression : le court doit etre le long prive d'une
    # lettre.
    court, long = (a, b) if len(a) < len(b) else (b, a)

    for position in range(len(long)):
        if long[:position] + long[position + 1:] == court:
            return True

    return False


def _corriger(nom: str) -> str | None:
    """Corrige une faute de frappe en s'appuyant sur CIQUAL.

    Ne cherche que sur le PREMIER mot, celui qui porte l'aliment
    ("broccoli cuit" -> "brocoli cuit"). Corriger les qualificatifs
    ("cuit", "cru") apporterait peu et multiplierait les faux positifs.
    """
    from food.ciqual import charger

    mots = nom.split()

    if not mots or len(mots[0]) < 5:
        return None

    cible = mots[0]
    candidats: set[str] = set()

    for aliment in charger():
        premier = aliment["recherche"].split()[0]

        if _distance_un(cible, premier):
            candidats.add(premier)

    # Une seule correction possible, sinon on ne devine pas.
    if len(candidats) != 1:
        return None

    return " ".join([candidats.pop()] + mots[1:])


def resoudre(quantite: float, unite: str | None, nom: str
             ) -> Element | Refus:
    """Rattache un element a CIQUAL et calcule son poids en grammes.

    Trois facons d'echouer, toutes explicites :
      - aucun aliment ne correspond
      - l'aliment existe mais on ne sait pas ce que pese "1" de cet
        aliment (absent de portions.py)
      - l'aliment existe mais CIQUAL n'a jamais mesure son energie
    """
    texte = f"{quantite:g}{unite or ''} {nom}".strip()
    element = Element(texte=texte, quantite=quantite, unite=unite,
                      aliment=nom)

    # --- 1. le poids en grammes
    if unite:
        facteur = portions.poids_mesure(unite)

        if facteur is None:
            return Refus(texte, f"unite inconnue : {unite}")

        element.grammes = quantite * facteur

    else:
        # Pas d'unite : c'est un compte d'objets ("1 banane").
        connu = portions.poids_unite(nom)

        if connu is None:
            return Refus(texte,
                         f"poids d'une unite inconnu pour '{nom}' - "
                         f"ecrire un poids (ex: 120g {nom}) ou completer "
                         f"food/portions.py")

        poids, clef = connu
        element.grammes = quantite * poids
        element.estime = True
        element.note = f"poids estime : 1 {clef} = {poids:g} g"

    # --- 2. l'aliment : la table personnelle d'abord
    #
    # Un alias est un choix ecrit une fois pour toutes ("riz" = riz blanc
    # cuit). Il court-circuite la recherche, donc aussi l'ambiguite : les
    # six riz de CIQUAL ne se disputent plus la ligne.
    alias = resoudre_alias(nom)

    if alias is not None:
        element.ciqual_nom = alias["nom"]
        element.ciqual_code = alias["code"]

        if element.note:
            element.note += f" ; alias : '{nom}' = {alias['nom']}"
        else:
            element.note = f"alias : '{nom}' = {alias['nom']}"

        return element

    # --- 3. sinon, la recherche
    candidats = chercher(nom, limite=12)
    corrige = None

    if not candidats:
        corrige = _corriger(nom)

        if corrige:
            candidats = chercher(corrige, limite=12)

    if not candidats:
        return Refus(texte, f"aliment introuvable dans CIQUAL : '{nom}'")

    # --- 3. preferer un aliment dont l'energie est connue
    #
    # 887 des 3 185 aliments de CIQUAL (28%) n'ont AUCUNE valeur
    # energetique - "Brocoli, cuit" en fait partie. Prendre le meilleur
    # nom sans regarder ferait entrer un aliment a 0 kcal dans un total
    # de journee, sans que rien ne le signale.
    complets = [(score, alim) for score, alim in candidats
                if "energy_kcal" in alim["valeurs"]]

    if not complets:
        return Refus(texte,
                     f"'{candidats[0][1]['nom']}' trouve, mais CIQUAL n'a "
                     f"jamais mesure son energie - preciser la preparation")

    # --- 4. refuser l'ambiguite plutot que de tirer au sort
    #
    # C'est ici que la regle de la version s'applique vraiment. Sur
    # "riz cuit" sans alias, six aliments obtiennent le meme score et
    # s'etalent de 102 a 158 kcal : en choisir un revient a introduire
    # 50% d'erreur en silence.
    #
    # Le critere combine les deux conditions - meme score ET energies
    # eloignees. Des candidats a egalite mais nutritionnellement proches
    # (les varietes de pommes, 52 a 55 kcal) ne meritent pas de bloquer
    # une saisie.
    meilleur = complets[0][0]
    exaequo = [alim for score, alim in complets if score >= meilleur - 4]

    if len(exaequo) > 1:
        energies = [alim["valeurs"]["energy_kcal"] for alim in exaequo]
        bas, haut = min(energies), max(energies)

        if bas > 0 and (haut - bas) / bas > ECART_TOLERE:
            propositions = ", ".join(f'"{alim["nom"]}"' for alim in exaequo[:3])
            return Refus(
                texte,
                f"'{nom}' est ambigu : {len(exaequo)} aliments a egalite, "
                f"de {bas:.0f} a {haut:.0f} kcal/100g. Preciser, ou ajouter "
                f"une ligne dans food/aliases.py. Ex: {propositions}")

    _, choisi = complets[0]
    element.ciqual_nom = choisi["nom"]
    element.ciqual_code = choisi["code"]

    notes = [element.note] if element.note else []

    if corrige:
        notes.append(f"faute corrigee : '{nom}' -> '{corrige}'")

    # Si le meilleur nom a ete ecarte faute d'energie, le dire. C'est un
    # choix du programme, pas un fait des donnees : il doit etre visible.
    if choisi is not candidats[0][1]:
        notes.append(f"'{candidats[0][1]['nom']}' correspondait mieux au "
                     f"nom mais n'a pas de valeur energetique")

    element.note = " ; ".join(notes) if notes else None

    return element


# ------------------------------------------------------------------ entree

def analyser(ligne: str, jour: date) -> Repas:
    """Analyse une ligne complete de journal."""
    repas = Repas(ligne=ligne.strip())

    if not repas.ligne or repas.ligne.startswith("#"):
        return repas

    moment, reste = lire_heure(repas.ligne, jour)

    if moment is None:
        repas.refus.append(Refus(repas.ligne,
                                 "pas d'heure en debut de ligne "
                                 "(attendu : '12h30, ...')"))
        return repas

    repas.moment = moment
    morceaux = decouper(reste)

    if not morceaux:
        repas.refus.append(Refus(reste.strip(), "aucun aliment reconnu"))
        return repas

    for quantite, unite, nom in morceaux:
        resultat = resoudre(quantite, unite, nom)

        if isinstance(resultat, Refus):
            repas.refus.append(resultat)
        else:
            repas.elements.append(resultat)

    return repas


def nutriments(element: Element) -> dict[str, float]:
    """Valeurs nutritionnelles de cet element, pour sa masse reelle.

    CIQUAL donne tout pour 100 g : on met a l'echelle. La masse est
    ajoutee comme un nutriment a part entiere (`mass_g`) - c'est elle
    qui permettra plus tard de dire "j'ai mange 1,2 kg aujourd'hui".
    """
    from food.ciqual import charger

    if element.ciqual_code is None or element.grammes is None:
        return {}

    aliment = next(a for a in charger() if a["code"] == element.ciqual_code)
    facteur = element.grammes / 100.0

    valeurs = {cle: round(valeur * facteur, 3)
               for cle, valeur in aliment["valeurs"].items()}
    valeurs["mass_g"] = round(element.grammes, 1)

    return valeurs
