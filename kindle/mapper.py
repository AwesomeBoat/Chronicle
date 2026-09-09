"""Connecteur Kindle : instantane -> Batch.

Quatrieme source du projet, apres Polar, le journal alimentaire et le
capteur de chambre. Comme les trois autres, ce module ne connait ni
SQLAlchemy, ni les tables : il produit des Batch.

LA SOURCE QUI POUSSE - ET POURQUOI ELLE RESTE TIREE QUAND MEME
--------------------------------------------------------------
Le ROADMAP notait la question comme ouverte : "un capteur qui emet
(MQTT, webhook) inverse le sens - hors perimetre V4, mais l'interface ne
doit pas l'interdire". La Kindle est ce cas : on appuie sur un item de la
bibliotheque, et elle televerse.

`sensors/bedroom.py` argumente contre le push, avec raison pour lui : un
capteur qui emet vers un PC eteint perd ses mesures. Le cas de la Kindle
est different sur les trois points qui comptent :

    ESP32                             Kindle
    declenche par le temps            declenchee par MOI
    PC eteint = mesures perdues       je reappuie plus tard
    interrogeable a tout moment       endormie, Wi-Fi coupe, non adressable

On ne PEUT pas tirer une Kindle. Mais on peut refuser que le push
traverse le programme : `receiver.py` ne fait qu'ecrire des fichiers dans
data/raw/kindle/, et l'ingestion les rejoue ensuite. Le connecteur reste
donc en pull sur un dossier, le curseur `sync_state` de la V2 fonctionne
tel quel, et rien ne va en base pendant que Docker est eteint.

LE ZERO QUI N'EST PAS UNE MESURE
--------------------------------
Lecon reprise telle quelle de la V3 : `cardio.strain` fabriquait a lui
seul vingt jours fantomes en ecrivant trente zeros qui n'etaient pas des
mesures. Le meme piege existe ici.

La telemetrie fine (mots lus, tournes de page, temps actif) n'accompagne
PAS toutes les sessions : sur les trois sessions observees, une seule en
avait. Ecrire `reading.words = 0` pour les deux autres ferait croire a
deux sessions sans lecture, alors qu'il s'agit de deux sessions sans
mesure.

Ces trois metriques ne sont donc emises que lorsqu'elles ont ete
reellement relevees. La duree, elle, est toujours vraie : elle se deduit
des deux horodatages.

LA PROGRESSION NE VIENT PAS DES SESSIONS - VERIFIE, PAS SUPPOSE
---------------------------------------------------------------
Une session declare `start_reading_location` et `end_reading_location`.
Le reflexe est d'en tirer un pourcentage d'avancement. C'est faux, et le
constater a demande de regarder les vraies valeurs :

    duree 340 s   positions 1 -> 3911   (max 3911)
    duree  60 s   positions 1 -> 3911   (max 3911)
    duree  20 s   positions 1 -> 3911   (max 3911)

Trois sessions de durees tres differentes, des bornes identiques et
egales a l'etendue totale du livre. Ces champs decrivent le CONTENU
ouvert, pas le chemin parcouru. Une progression calculee dessus vaudrait
100 % en permanence.

C'est le piege `cardio.strain` de la V3, a l'identique : une colonne bien
remplie, qui passe toutes les contraintes, et qui ne mesure rien. La
seule facon de le voir est de comparer la colonne a ce qu'elle pretend
decrire.

La vraie position lue est ailleurs - dans la LPR (last reading position)
de la base d'annotations, qui donnait 2071 pour ce meme livre. Elle
decrit un ETAT du livre, pas un evenement : elle part donc en
`profile_snapshot`, dont le dedoublonnage par empreinte de contenu est
fait pour ca. Collecter cent fois une progression inchangee cree une
ligne ; le jour ou elle bouge, une deuxieme apparait.

Consequence : il n'y a PAS de metrique `reading.progress`. Une
observation ne porte pas l'identite du livre, et un pourcentage sans son
livre ne veut rien dire.

LES SURLIGNEMENTS SONT DES EPISODES, ET C'EST DISCUTABLE
--------------------------------------------------------
`episode` est defini dans SCHEMA.md comme "ce qui a une duree", et un
surlignement n'en a pas. Il y est quand meme, pour une raison qui n'est
pas un contournement : le texte doit vivre en base (decision d'Ernest),
et `observation.value` est un Float. Des trois formes du schema - point
numerique, intervalle, etat -, l'intervalle est la seule qui accepte du
texte dans son payload.

L'honnetete demande de le dire : ce qu'un surlignement possede n'est pas
une etendue temporelle mais une etendue DANS LE LIVRE, de
start_position a end_position. `ended_at` reste NULL, ce que la colonne
autorise explicitement.

L'idempotence tient grace a la precision milliseconde des horodatages
Kindle : la contrainte porte sur (source, kind, started_at), et personne
ne cree deux surlignements dans la meme milliseconde.
"""

from __future__ import annotations

from datetime import datetime, timezone

from database.records import (Batch, EpisodeRecord, MetricSpec,
                              ObservationRecord, SnapshotRecord)
from kindle.reader import Snapshot

SOURCE_CODE = "kindle_paperwhite"
SOURCE_LABEL = "Kindle Paperwhite (lecteur natif)"

# Le flux au sens du curseur sync_state. Une chaine libre, comme
# "bedroom/readings" : le curseur ne sait pas ce qu'est un endpoint HTTP.
ENDPOINT = "kindle/sessions"

METRICS = [
    MetricSpec("reading.duration_s", "s", "instant",
               "Duree d'une session de lecture, de l'ouverture a la fermeture"),
    MetricSpec("reading.active_s", "s", "instant",
               "Temps reellement actif pendant la session (hors veille)"),
    MetricSpec("reading.words", "mots", "instant",
               "Mots lus pendant la session, releves par la liseuse"),
    MetricSpec("reading.page_turns", "count", "instant",
               "Tournes de page pendant la session"),
]

# Etat de lecture d'un livre. Un snapshot et non une observation :
# la progression appartient au LIVRE, et `observation` n'a pas de
# dimension livre. Voir l'en-tete du module.
PROGRESSION = "book_progress"

# Genres d'episodes produits. Les surlignements, notes et marque-pages ne
# sont PAS comptes en observations : les compter reviendrait a ecrire une
# valeur 1 par annotation, et deux annotations au meme instant
# s'ecraseraient sous la contrainte d'unicite. Un count(*) dans la vue
# donne le meme chiffre sans ce risque.
GENRE_ANNOTATION = {
    "HIGHLIGHT": "highlight",
    "NOTE": "note",
    "BOOKMARK": "bookmark",
}

SESSION = "reading_session"
LOOKUP = "word_lookup"


def _livre(snap: Snapshot, asin: str) -> dict:
    """Les metadonnees d'un livre, ou un repli si le catalogue l'ignore.

    Un ASIN inconnu n'est pas une anomalie : le catalogue vient des noms
    de fichiers, et un livre supprime de la liseuse garde ses
    surlignements en base d'annotations.
    """
    livre = snap.livres.get(asin) or {}

    return {
        "asin": asin,
        "titre": livre.get("titre") or f"(inconnu {asin[:8]})",
        "auteur": livre.get("auteur"),
        "langue": livre.get("langue"),
        "format": livre.get("format"),
    }


def _progression(position: object, maximum: object) -> float | None:
    """Pourcentage d'avancement, ou None si indeterminable.

    Le KFX ne compte pas en pages mais en positions. `max_position` n'est
    connu qu'apres avoir ouvert le livre au moins une fois.
    """
    if not isinstance(position, int) or not isinstance(maximum, int):
        return None

    if maximum <= 0 or position < 0:
        return None

    return min(100.0, round(100.0 * position / maximum, 2))


def _session_vers_batch(snap: Snapshot, session: dict) -> Batch:
    """Une session -> un episode + ses observations."""
    lot = Batch()

    debut: datetime = session["debut"]
    fin: datetime | None = session["fin"]
    livre = _livre(snap, session["asin"])

    lot.episodes.append(EpisodeRecord(
        kind=SESSION,
        started_at=debut,
        ended_at=fin,
        payload={
            **livre,
            # "device" distingue ces sessions de celles importees depuis
            # l'export Amazon (kindle/amazon_export.py), dont la duree
            # est calculee differemment - voir la docstring de ce
            # dernier module, section "deux facons de mesurer".
            "origine": "device",
            # Noms d'origine de la liseuse, gardes tels quels pour ne pas
            # maquiller la source - mais voir l'en-tete du module : ces
            # bornes decrivent le contenu ouvert, pas le chemin parcouru.
            # Ne PAS en tirer une progression.
            "start_reading_location": session.get("position_debut"),
            "end_reading_location": session.get("position_fin"),
            "max_position": session.get("position_max"),
            "type_contenu": session.get("type_contenu"),
            "mots": session["mots"] or None,
            "tournes": session["tournes"] or None,
        }))

    # Les observations sont ancrees sur le DEBUT de la session, pas sur
    # sa fin : c'est l'instant qui identifie la session (avec l'ASIN), et
    # celui qui porte du sens pour une analyse par heure de la journee.
    if fin is not None:
        duree = (fin - debut).total_seconds()

        if duree >= 0:
            lot.observations.append(
                ObservationRecord("reading.duration_s", debut, duree))

    # --- les trois metriques qui ne s'ecrivent que si elles existent
    if session["actif_ms"]:
        lot.observations.append(ObservationRecord(
            "reading.active_s", debut, session["actif_ms"] / 1000.0))

    if session["mots"]:
        lot.observations.append(ObservationRecord(
            "reading.words", debut, float(session["mots"])))

    if session["tournes"]:
        lot.observations.append(ObservationRecord(
            "reading.page_turns", debut, float(session["tournes"])))

    return lot


def _annotation_vers_batch(snap: Snapshot, annotation: dict) -> Batch:
    """Un surlignement, une note ou un marque-page -> un episode."""
    lot = Batch()
    genre = GENRE_ANNOTATION.get(annotation["genre"])

    if genre is None:
        return lot

    lot.episodes.append(EpisodeRecord(
        kind=genre,
        started_at=annotation["cree_le"],
        ended_at=None,
        payload={
            **_livre(snap, annotation["asin"]),
            "identifiant": annotation["identifiant"],
            "position_debut": annotation.get("position_debut"),
            "position_fin": annotation.get("position_fin"),
            # Vide pour les surlignements tant que l'extraction du texte
            # depuis le KFX n'est pas faite ; renseigne pour les notes,
            # dont le texte voyage dans json_metadata.
            "texte": annotation.get("texte"),
            "couleur": annotation.get("couleur"),
        }))

    return lot


def _lookup_vers_batch(snap: Snapshot, lookup: dict) -> Batch:
    """Un mot cherche au dictionnaire -> un episode."""
    lot = Batch()
    livre = _livre(snap, lookup["asin"]) if lookup.get("asin") else {}

    lot.episodes.append(EpisodeRecord(
        kind=LOOKUP,
        started_at=lookup["cherche_le"],
        ended_at=None,
        payload={
            **livre,
            "mot": lookup["mot"],
            "radical": lookup.get("radical"),
            "langue_mot": lookup.get("langue"),
            "phrase": lookup.get("phrase"),
        }))

    return lot


def _progression_vers_batch(snap: Snapshot, capture_le: datetime) -> Batch:
    """La derniere position lue de chaque livre -> un profile_snapshot.

    Une seule ligne par livre, meme si plusieurs appareils la declarent :
    on garde la position la plus avancee, qui est celle qui fait foi
    (Whispersync fait le meme choix).

    `captured_at` est l'instant de la COLLECTE et non celui de la lecture :
    la liseuse ne dit pas quand la position a bouge. C'est sans
    consequence grace au dedoublonnage par empreinte - une progression
    inchangee collectee cent fois ne cree qu'une ligne, et seule la
    premiere date compte.
    """
    lot = Batch()
    avancement: dict[str, int] = {}

    for entree in snap.progress:
        asin, position = entree["asin"], entree["position"]

        if not isinstance(position, int):
            continue

        if position > avancement.get(asin, -1):
            avancement[asin] = position

    # La longueur du livre ne se lit que dans les sessions : sans session
    # sur ce livre, on connait la position mais pas le pourcentage.
    longueurs = {s["asin"]: s["position_max"] for s in snap.sessions
                 if isinstance(s.get("position_max"), int)}

    for asin, position in sorted(avancement.items()):
        maximum = longueurs.get(asin)

        lot.snapshots.append(SnapshotRecord(
            kind=PROGRESSION,
            captured_at=capture_le,
            payload={
                **_livre(snap, asin),
                "position": position,
                "position_max": maximum,
                "progression_pct": _progression(position, maximum),
            }))

    return lot


def map_snapshot(snap: Snapshot,
                 capture_le: datetime | None = None) -> tuple[Batch, list[str]]:
    """Traduit un instantane complet. Renvoie (lot, anomalies).

    Les anomalies sont remontees plutot que journalisees ici : le
    connecteur ne decide pas de ce qui merite d'etre affiche. Meme
    principe que `food.mapper.map_jour`, qui rend les repas analyses pour
    que l'appelant puisse signaler les refus.
    """
    lot = Batch()
    anomalies = list(snap.anomalies)

    if capture_le is None:
        capture_le = datetime.now(timezone.utc)

    for session in snap.sessions:
        lot.extend(_session_vers_batch(snap, session))

    for annotation in snap.annotations:
        if annotation["genre"] not in GENRE_ANNOTATION:
            anomalies.append(
                f"annotation de genre inconnu : {annotation['genre']}")
            continue

        lot.extend(_annotation_vers_batch(snap, annotation))

    for lookup in snap.lookups:
        lot.extend(_lookup_vers_batch(snap, lookup))

    lot.extend(_progression_vers_batch(snap, capture_le))

    return lot, anomalies


def curseur(lot: Batch) -> datetime | None:
    """L'instant le plus recent du lot, pour faire avancer sync_state.

    Calcule sur les episodes et non sur les observations : toutes les
    formes de donnees produisent un episode, alors que les observations
    n'existent que pour les sessions.
    """
    instants = [episode.started_at for episode in lot.episodes]

    return max(instants) if instants else None
