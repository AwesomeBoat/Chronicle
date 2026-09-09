"""Le contrat qu'un connecteur doit remplir. (V4, etape 1)

HONNETETE SUR L'ORDRE : le ROADMAP annonce ce fichier comme ecrit AVANT
les connecteurs. Il a en realite ete ecrit APRES `food/` et `sensors/`,
et le resultat est meilleur pour cette raison. Une interface deduite
d'une seule source a la forme de cette source ; celle-ci est deduite de
trois sources qui n'ont rien en commun :

    polar_accesslink   API REST, OAuth2, fenetre de dates, JSON structure
    food_journal       fichiers texte ecrits a la main, aucun reseau
    bedroom_sensor     HTTP local sans auth, tampon circulaire, pull

Ce que ces trois-la partagent est court, et c'est precisement ce qui
rend le contrat utile. Ce qu'ils ne partagent PAS est aussi instructif :
il n'y a pas de methode `fetch()` commune, parce que "aller chercher"
n'a pas le meme sens pour une API, un fichier et un microcontroleur.
Forcer une signature unique aurait produit une abstraction qui ment.

PROTOCOL, PAS CLASSE DE BASE
----------------------------
`typing.Protocol` decrit une forme, il ne s'herite pas. Un module qui
expose les bons noms satisfait le contrat sans le savoir ni l'importer -
c'est du typage structurel ("duck typing" verifie).

Concretement : `polar/mapper.py` a ete ecrit en V1, bien avant ce
fichier, et il satisfait deja le contrat. Avec une classe de base, il
aurait fallu le modifier - donc casser le critere de la V4.

CE QUE LE CONTRAT EXIGE
-----------------------
    SOURCE_CODE    str            identifiant stable, en base
    SOURCE_LABEL   str            libelle lisible
    METRICS        list[MetricSpec]  les grandeurs declarees

C'est tout. Trois attributs de module, aucune methode. La production de
`Batch` reste libre : `map_envelope()` pour Polar, `map_jour()` pour le
journal, `mapper()` pour le capteur - chacun prend ce que sa source sait
donner.

L'INVARIANT QUI COMPTE VRAIMENT
-------------------------------
Il n'est pas dans les types, il est dans la dependance :

    Un connecteur produit des `Batch`. Il n'importe jamais SQLAlchemy,
    ni `database.repository`, ni `database.models`.

C'est ce qui garantit le critere de la V4. `verifier_isolation()`
ci-dessous le controle mecaniquement, parce qu'un invariant qu'aucun
code ne verifie finit toujours par etre viole.
"""

from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from database.records import MetricSpec

# Les connecteurs du projet. Ajouter une source = ajouter une ligne ici,
# et c'est le seul endroit du code ou la liste existe.
CONNECTEURS = ["polar.mapper", "food.mapper", "sensors.bedroom",
               "kindle.mapper"]

# Modules qu'un connecteur ne doit jamais importer, meme indirectement
# dans son propre fichier. La frontiere est la, et nulle part ailleurs.
INTERDITS = ("sqlalchemy", "database.repository", "database.models",
             "database.connection")


@runtime_checkable
class Connecteur(Protocol):
    """La forme minimale d'un connecteur."""

    SOURCE_CODE: str
    SOURCE_LABEL: str
    METRICS: list[MetricSpec]


def verifier_isolation() -> list[str]:
    """Controle que chaque connecteur respecte le contrat.

    Deux verifications, la seconde etant la seule qui protege vraiment :

    1. les trois attributs existent et ont le bon type ;
    2. le fichier source ne mentionne aucun module interdit.

    Le controle 2 lit le TEXTE du fichier plutot que ses imports
    resolus. C'est volontairement grossier : un import a l'interieur
    d'une fonction echapperait a l'inspection des modules charges, mais
    pas a une recherche dans le source.
    """
    problemes: list[str] = []

    for nom in CONNECTEURS:
        try:
            module = importlib.import_module(nom)
        except ImportError as erreur:
            problemes.append(f"{nom} : import impossible ({erreur})")
            continue

        for attribut, type_attendu in (("SOURCE_CODE", str),
                                       ("SOURCE_LABEL", str),
                                       ("METRICS", list)):
            valeur = getattr(module, attribut, None)

            if valeur is None:
                problemes.append(f"{nom} : {attribut} manquant")
            elif not isinstance(valeur, type_attendu):
                problemes.append(
                    f"{nom} : {attribut} devrait etre un "
                    f"{type_attendu.__name__}")

        metriques = getattr(module, "METRICS", [])

        for spec in metriques:
            if not isinstance(spec, MetricSpec):
                problemes.append(f"{nom} : METRICS contient un "
                                 f"{type(spec).__name__}, pas un MetricSpec")
                break

        # --- l'invariant de dependance
        fichier = getattr(module, "__file__", None)

        if fichier:
            source = open(fichier, encoding="utf-8").read()

            for interdit in INTERDITS:
                if f"import {interdit}" in source or \
                        f"from {interdit}" in source:
                    problemes.append(
                        f"{nom} : importe {interdit} - un connecteur ne "
                        f"doit connaitre que database.records")

    return problemes


def inventaire() -> list[tuple[str, str, int]]:
    """(code, libelle, nombre de metriques) pour chaque connecteur."""
    lignes = []

    for nom in CONNECTEURS:
        module = importlib.import_module(nom)
        lignes.append((module.SOURCE_CODE, module.SOURCE_LABEL,
                       len(module.METRICS)))

    return lignes


def run_sources() -> int:
    """Liste les connecteurs et verifie le contrat. Renvoie le nb d'erreurs."""
    print("=" * 62)
    print("CONNECTEURS")
    print("=" * 62)

    for code, libelle, nb_metriques in inventaire():
        print(f"  {code:<20} {nb_metriques:>3} metriques   {libelle}")

    problemes = verifier_isolation()

    print()

    if problemes:
        print(f"{len(problemes)} violation(s) du contrat :")
        for probleme in problemes:
            print(f"  [!] {probleme}")
    else:
        print("Contrat respecte : aucun connecteur ne connait la base.")

    return len(problemes)
