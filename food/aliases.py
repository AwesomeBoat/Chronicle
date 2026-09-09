"""Ce que je veux dire quand j'ecris « poulet ». (V4, etape 4)

Meme nature que portions.py : de la connaissance qui ne peut pas se
deduire des donnees, ecrite et maintenue a la main.

LE PROBLEME
-----------
CIQUAL est une table scientifique : elle distingue le riz thai du riz
basmati, le poulet en pilon du poulet en filet, la pomme Gala de la
pomme Golden. C'est sa raison d'etre, et c'est precieux.

Mais quand j'ecris "riz cuit" dans mon journal a 12h30, six aliments
correspondent aussi bien, de 102 a 158 kcal aux 100 g. Laisser le
programme en choisir un par ordre alphabetique introduit une erreur de
50% sur la ligne - silencieusement.

DEUX REPONSES, ET NON UNE
-------------------------
1. **Cette table**, pour les aliments du quotidien : "riz" veut dire le
   riz blanc cuit, un point c'est tout. C'est un choix personnel, pas
   une verite - et il est ecrit, donc revisable.

2. **Le refus**, pour tout le reste (voir parser.resoudre) : si
   plusieurs candidats se valent et que leurs energies s'ecartent trop,
   le programme demande de preciser plutot que de trancher a ma place.

La table couvre le frequent, le refus couvre le rare. Sans la table le
journal serait insupportable ; sans le refus il serait faux.

MAINTENANCE
-----------
Pour trouver le code d'un aliment :

    python main.py ciqual "riz blanc cuit"

Ajouter une ligne ici est le bon reflexe des qu'un aliment revient une
deuxieme fois dans le journal.
"""

from __future__ import annotations

from food.ciqual import charger, normaliser

# terme du journal -> code CIQUAL
#
# Le commentaire de fin de ligne donne le nom exact retenu : sans lui,
# relire cette table demande d'aller interroger CIQUAL a chaque ligne.
ALIASES = {
    # --- feculents
    "riz":              "9104",   # Riz blanc, cuit, non sale          145 kcal
    "riz cuit":         "9104",
    "riz blanc":        "9104",
    "riz complet":      "9103",   # Riz complet, cuit, non sale        158
    "riz basmati":      "9125",   # Riz basmati, cuit, non sale        117
    "pates":            "9811",   # Pates seches standard, cuites      126
    "pain":             "7001",   # Pain, baguette, courante           287
    "semoule":          "9611",   # Semoule de ble dur, cuite          122

    # --- proteines animales
    "poulet":           "36018",  # Poulet, filet, sans peau, saute    141
    "poulet cuit":      "36018",
    "blanc de poulet":  "36018",
    "dinde":            "36308",  # Dinde, escalope, rotie au four     128
    "boeuf":            "6251",   # Boeuf, steak hache 5% MG, cuit     155
    "steak":            "6251",
    "saumon":           "26038",  # Saumon, cuit a la vapeur           195
    "thon":             "26039",  # Thon, au naturel, egoutte          111
    "oeuf":             "22010",  # Oeuf, dur                          134
    "oeufs":            "22010",
    "jambon":           "28906",  # Jambon cuit, decouenne degraisse   119

    # --- legumes
    "brocoli":          "20304",  # Brocoli, cuit a la vapeur           38
    "brocolis":         "20304",
    "haricots verts":   "20030",  # Haricot vert, cuit                  29
    "carotte":          "20009",  # Carotte, crue                       40
    "tomate":           "20047",  # Tomate, crue                        19
    "courgette":        "20020",  # Courgette, pulpe et peau, crue      16
    "epinards":         "20336",  # Epinard, bouilli/cuit a l'eau       28
    "salade":           "20123",  # Batavia, crue                       18

    # --- fruits
    "pomme":            "13191",  # Pomme Gala, pulpe, crue             54
    "banane":           "13005",  # Banane, pulpe, crue                 90
    "orange":           "13034",  # Orange, pulpe, crue                 46

    # --- laitages
    "lait":             "19041",  # Lait demi-ecreme, UHT               47
    "yaourt":           "19593",  # Yaourt nature                       46
    "fromage blanc":    "19644",  # Fromage blanc nature, 0% MG         49

    # --- autres
    "huile olive":      "17270",  # Huile d'olive vierge extra         900
    "beurre":           "16400",  # Beurre a 82% MG, doux              753
}

_ALIASES = {normaliser(terme): code for terme, code in ALIASES.items()}


def resoudre_alias(nom: str) -> dict | None:
    """L'aliment vise par ce terme, s'il est dans la table. None sinon.

    Le code est verifie contre CIQUAL a chaque appel : une coquille dans
    la table ci-dessus doit se voir tout de suite, et non produire un
    silencieux "aliment introuvable" trois etapes plus loin.
    """
    code = _ALIASES.get(normaliser(nom))

    if code is None:
        return None

    for aliment in charger():
        if aliment["code"] == code:
            return aliment

    raise KeyError(
        f"food/aliases.py : le code CIQUAL {code} (pour '{nom}') n'existe "
        f"pas dans la table. Verifier avec : python main.py ciqual \"{nom}\"")


def verifier() -> list[str]:
    """Controle toute la table. Renvoie la liste des problemes.

    Appelee par `main.py quality` : une table maintenue a la main derive,
    et une derive silencieuse dans un referentiel contamine tout ce qui
    s'appuie dessus.
    """
    par_code = {aliment["code"]: aliment for aliment in charger()}
    problemes = []

    for terme, code in ALIASES.items():
        aliment = par_code.get(code)

        if aliment is None:
            problemes.append(f"{terme:<18} code {code} inexistant dans CIQUAL")
        elif "energy_kcal" not in aliment["valeurs"]:
            problemes.append(f"{terme:<18} {aliment['nom']} - sans energie")

    return problemes
