"""Lecture des bases SQLite envoyees par la Kindle.

Ce module ne connait ni la base du projet, ni le format Batch : il
traduit trois fichiers SQLite en dictionnaires Python ordinaires.
mapper.py fait le reste.

CE QUE LA KINDLE STOCKE, ET OU
------------------------------
La liseuse tourne sous son firmware d'origine (pas de KOReader). Trois
fichiers portent tout ce qui est exploitable :

    system/fmcache/fmcache.db
        Les sessions de lecture : debut, fin, positions, et une
        telemetrie fine (mots lus, tournes de page, temps actif).

    system/ksdk/.annotations/<compte>/ksdk_annotation_v1.db
        Surlignements, notes, marque-pages, et la derniere position lue
        par appareil.

    system/vocabulary/vocab.db
        Les mots cherches au dictionnaire, avec la phrase qui les
        entourait, et un catalogue ASIN -> titre / auteur.

LE PIEGE QUI DICTE TOUTE L'ARCHITECTURE
---------------------------------------
`fmcache.db` n'est PAS un journal : c'est un tampon d'envoi. La Kindle y
ecrit ses sessions, les televerse chez Amazon, puis les EFFACE. Au moment
de l'analyse il ne restait que trois sessions, toutes du jour meme.

C'est exactement le probleme de la retention Polar a 28 jours, en plus
brutal - et la reponse est la meme : recuperer souvent, archiver le brut,
rejouer depuis l'archive. Une session non collectee est perdue pour
toujours.

`My Clippings.txt` n'existe plus : les livres sont au format KFX, et le
lecteur KFX ecrit ses annotations dans la base SQLite, plus dans le
fichier texte.

HORODATAGE : POUR UNE FOIS, AUCUNE AMBIGUITE
--------------------------------------------
Contrairement a Polar (qui envoie des heures d'horloge sans fuseau) et a
l'ESP32 (qui peut compter depuis 1970), la Kindle donne des epochs en
MILLISECONDES - des instants absolus. La conversion est directe et sans
hypothese de fuseau.

La precision milliseconde est conservee volontairement : c'est elle qui
rend deux annotations distinctes impossibles a confondre, puisque la
contrainte d'unicite d'un episode porte sur (source, kind, started_at).
Personne ne cree deux surlignements dans la meme milliseconde.

Un controle de plausibilite reste applique - meme lecon que le capteur de
chambre : une date aberrante passe toutes les contraintes d'insertion et
ruine ensuite chaque analyse.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# L'ASIN d'un document personnel est un hexadecimal de 32 caracteres, et
# il figure dans le nom du fichier envoye par Send-to-Kindle.
ASIN_DANS_NOM = re.compile(
    r"[_-](?:cdeKey_)?([0-9A-Fa-f]{32})\.(?:kfx|azw3|azw|pdf|mobi)$")

# Bornes de plausibilite d'un horodatage, en epoch millisecondes.
# Avant 2010 : aucune liseuse concernee n'existait. Au-dela d'un an dans
# le futur : horloge fausse.
MS_MINIMUM = 1262304000000          # 2010-01-01
MS_MARGE_FUTUR = 365 * 24 * 3600 * 1000

# Les fichiers attendus dans un instantane, et leur role.
FICHIERS = {
    "fmcache.db": "sessions de lecture",
    "annotations.db": "surlignements, notes, marque-pages",
    "vocab.db": "mots cherches au dictionnaire",
    "catalog.txt": "noms de fichiers des livres",
}


class SnapshotError(Exception):
    """Instantane inutilisable (dossier absent, aucun fichier lisible)."""


@dataclass(slots=True)
class Snapshot:
    """Le contenu d'un envoi de la Kindle, en objets Python simples."""

    sessions: list[dict] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    lookups: list[dict] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)
    livres: dict[str, dict] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    dossier: str = ""

    def __len__(self) -> int:
        return (len(self.sessions) + len(self.annotations)
                + len(self.lookups) + len(self.progress))


# ------------------------------------------------------------- utilitaires

def _instant(ms: Any) -> datetime | None:
    """Epoch millisecondes -> datetime UTC. None si invalide ou aberrant."""
    try:
        valeur = int(ms)
    except (TypeError, ValueError):
        return None

    if valeur < MS_MINIMUM:
        return None

    maintenant = datetime.now(timezone.utc).timestamp() * 1000

    if valeur > maintenant + MS_MARGE_FUTUR:
        return None

    return datetime.fromtimestamp(valeur / 1000, tz=timezone.utc)


def _ouvrir(chemin: str) -> sqlite3.Connection:
    """Ouvre une base Kindle en LECTURE SEULE.

    Le mode ro est une precaution reelle : SQLite ecrit dans le fichier
    des qu'il ouvre en lecture-ecriture (journal, verrous). Ces bases
    sont des copies, mais l'habitude protege le jour ou le chemin pointe
    sur la liseuse elle-meme.
    """
    return sqlite3.connect(f"file:{chemin.replace(os.sep, '/')}?mode=ro",
                           uri=True)


def _titre_lisible(nom_fichier: str) -> str:
    """Nettoie un nom de fichier Send-to-Kindle pour en tirer un titre."""
    titre = re.sub(r"[_-](?:cdeKey_)?[0-9A-Fa-f]{32}\.\w+$", "", nom_fichier)
    titre = re.sub(r"\s*-\s*libgen\.li.*$", "", titre)
    titre = re.sub(r"\s*\(\d{4},[^)]*\)\s*", " ", titre)
    titre = titre.replace("_", " ").strip(" -_")

    return re.sub(r"\s{2,}", " ", titre)


# ------------------------------------------------------------- catalogue

def _lire_catalogue(dossier: str, snap: Snapshot) -> None:
    """ASIN -> titre / auteur, depuis catalog.txt puis vocab.db.

    Deux sources volontairement combinees : le nom de fichier donne un
    titre pour TOUS les livres presents sur la liseuse, vocab.db n'en
    connait que ceux ou un mot a ete cherche - mais lui seul donne
    l'auteur et la langue.
    """
    catalogue = os.path.join(dossier, "catalog.txt")

    if os.path.exists(catalogue):
        with open(catalogue, encoding="utf-8", errors="replace") as fichier:
            for ligne in fichier:
                nom = os.path.basename(ligne.strip())
                trouve = ASIN_DANS_NOM.search(nom)

                if not trouve:
                    continue

                snap.livres[trouve.group(1).upper()] = {
                    "titre": _titre_lisible(nom),
                    "auteur": None,
                    "langue": None,
                    "format": os.path.splitext(nom)[1].lstrip(".").upper(),
                }

    vocab = os.path.join(dossier, "vocab.db")

    if not os.path.exists(vocab):
        return

    try:
        connexion = _ouvrir(vocab)
        requete = "SELECT asin, lang, title, authors FROM BOOK_INFO"

        for asin, langue, titre, auteurs in connexion.execute(requete):
            if not asin:
                continue

            livre = snap.livres.setdefault(asin.upper(), {
                "titre": None, "auteur": None, "langue": None, "format": None})
            livre["auteur"] = auteurs or livre.get("auteur")
            livre["langue"] = (langue or None) or livre.get("langue")
            livre["titre"] = livre.get("titre") or titre

        connexion.close()

    except sqlite3.Error as erreur:
        snap.anomalies.append(f"vocab.db (catalogue) illisible : {erreur}")


# --------------------------------------------------------------- sessions

def _telemetrie(connexion: sqlite3.Connection) -> dict[int, dict]:
    """Agrege les evenements de `records` par session locale.

    fmcache contient, a cote des sessions, ~1200 evenements de telemetrie
    dont trois nous interessent :

        ereader_book_consume_content   words_count : les mots lus
        ereader_book_page_turn         une ligne par tourne de page
        ereader_device_active_usage_time  time_ms : le temps actif reel

    Le temps actif differe de la duree de session : une session de six
    minutes dont la liseuse s'est mise en veille au bout de quatre ne
    represente pas six minutes de lecture.
    """
    agregat: dict[int, dict] = {}

    try:
        lignes = connexion.execute(
            "SELECT reading_session_id, schema_name, record FROM records "
            "WHERE reading_session_id IS NOT NULL").fetchall()
    except sqlite3.Error:
        return agregat

    for session_id, schema, brut in lignes:
        compteurs = agregat.setdefault(
            session_id, {"mots": 0, "tournes": 0, "actif_ms": 0})

        try:
            evenement = json.loads(brut)
        except (TypeError, ValueError):
            continue

        if schema == "ereader_book_consume_content":
            compteurs["mots"] += int(evenement.get("words_count") or 0)

        elif schema == "ereader_book_page_turn":
            compteurs["tournes"] += 1

        elif schema == "ereader_device_active_usage_time":
            # max et non somme : la liseuse envoie un cumul, pas un delta.
            compteurs["actif_ms"] = max(compteurs["actif_ms"],
                                        int(evenement.get("time_ms") or 0))

    return agregat


def _lire_sessions(dossier: str, snap: Snapshot) -> None:
    """Les sessions de lecture de fmcache.db."""
    chemin = os.path.join(dossier, "fmcache.db")

    if not os.path.exists(chemin):
        snap.anomalies.append("fmcache.db absent : aucune session dans l'envoi")
        return

    try:
        connexion = _ouvrir(chemin)
        agregat = _telemetrie(connexion)
        lignes = connexion.execute(
            "SELECT id, session FROM reading_sessions").fetchall()

    except sqlite3.Error as erreur:
        snap.anomalies.append(f"fmcache.db illisible : {erreur}")
        return

    for session_id, brut in lignes:
        try:
            charge = json.loads(brut)["payload"]
        except (TypeError, ValueError, KeyError):
            snap.anomalies.append(f"session {session_id} : charge illisible")
            continue

        debut = _instant(charge.get("start_timestamp"))
        fin = _instant(charge.get("end_timestamp"))
        asin = (charge.get("asin") or "").upper()

        if debut is None or not asin:
            snap.anomalies.append(
                f"session {session_id} : horodatage ou ASIN invalide (ecartee)")
            continue

        if fin is not None and fin < debut:
            snap.anomalies.append(
                f"session {session_id} : fin avant debut (ecartee)")
            continue

        compteurs = agregat.get(session_id,
                                {"mots": 0, "tournes": 0, "actif_ms": 0})

        snap.sessions.append({
            "asin": asin,
            "debut": debut,
            "fin": fin,
            "position_debut": charge.get("start_reading_location"),
            "position_fin": charge.get("end_reading_location"),
            "position_max": charge.get("max_position"),
            "type_contenu": charge.get("content_type"),
            "mots": compteurs["mots"],
            "tournes": compteurs["tournes"],
            "actif_ms": compteurs["actif_ms"],
        })

    connexion.close()


# ------------------------------------------------------------ annotations

def _lire_annotations(dossier: str, snap: Snapshot) -> None:
    """Surlignements, notes, marque-pages, et derniere position lue.

    La table `server_view` melange deux choses que seule la presence de
    la clef "type" distingue :

        avec "type"  -> une annotation (HIGHLIGHT / NOTE / BOOKMARK)
        sans "type"  -> la derniere position lue sur un appareil (LPR)

    LE TEXTE DES SURLIGNEMENTS N'EST PAS DANS CETTE BASE. Seules les
    positions le sont. Les NOTES font exception : leur texte est dans
    json_metadata.note_text - c'est pourquoi elles sortent completes
    alors que les surlignements sortent vides.
    """
    chemin = os.path.join(dossier, "annotations.db")

    if not os.path.exists(chemin):
        snap.anomalies.append("annotations.db absent")
        return

    try:
        connexion = _ouvrir(chemin)
        lignes = connexion.execute(
            "SELECT annotation_id, serialized_payload, created_time, "
            "modified_time FROM server_view").fetchall()

    except sqlite3.Error as erreur:
        snap.anomalies.append(f"annotations.db illisible : {erreur}")
        return

    for identifiant, brut, cree_le, modifie_le in lignes:
        try:
            charge = json.loads(brut)
        except (TypeError, ValueError):
            continue

        asin = ((charge.get("book_data") or {}).get("asin") or "").upper()

        if not asin:
            continue

        # --- derniere position lue, pas une annotation
        if "type" not in charge:
            position = (charge.get("position") or {}).get("shortPosition")

            if position is not None:
                snap.progress.append({
                    "asin": asin,
                    "appareil": charge.get("device_name") or "Inconnu",
                    "position": position,
                })
            continue

        instant = _instant(charge.get("created_time") or cree_le)

        if instant is None:
            snap.anomalies.append(
                f"annotation {identifiant} : horodatage invalide (ecartee)")
            continue

        try:
            meta = json.loads(charge.get("json_metadata") or "{}")
        except (TypeError, ValueError):
            meta = {}

        snap.annotations.append({
            "identifiant": identifiant,
            "asin": asin,
            "genre": (charge.get("type") or "").upper(),
            "position_debut": (charge.get("start_position")
                               or {}).get("shortPosition"),
            "position_fin": (charge.get("end_position")
                             or {}).get("shortPosition"),
            "cree_le": instant,
            "modifie_le": _instant(charge.get("last_modified") or modifie_le),
            "texte": meta.get("note_text"),
            "couleur": meta.get("mchl_color"),
        })

    connexion.close()


# ---------------------------------------------------------------- lookups

def _lire_lookups(dossier: str, snap: Snapshot) -> None:
    """Mots cherches au dictionnaire, avec leur phrase de contexte."""
    chemin = os.path.join(dossier, "vocab.db")

    if not os.path.exists(chemin):
        return

    requete = (
        "SELECT w.word, w.stem, w.lang, b.asin, l.usage, l.timestamp "
        "FROM LOOKUPS l "
        "JOIN WORDS w ON w.id = l.word_key "
        "LEFT JOIN BOOK_INFO b ON b.id = l.book_key")

    try:
        connexion = _ouvrir(chemin)
        lignes = connexion.execute(requete).fetchall()

    except sqlite3.Error as erreur:
        snap.anomalies.append(f"vocab.db (lookups) illisible : {erreur}")
        return

    # Dedoublonnage sur (mot, livre, instant). La base vocab.db contient
    # de vrais doublons - 20 sur 186 dans l'echantillon d'origine : un
    # meme mot cherche est enregistre une fois par dictionnaire installe,
    # et deux dictionnaires anglais le sont.
    #
    # Ce n'est pas une precaution theorique : sans elle, ces 20 lignes
    # entreraient en conflit sur (source, kind, started_at) et seraient
    # ecrasees en silence par le repository. Mieux vaut les ecarter ici,
    # ou le compte affiche reste honnete.
    vus: set[tuple] = set()

    for mot, radical, langue, asin, phrase, horodatage in lignes:
        instant = _instant(horodatage)

        if instant is None or not mot:
            continue

        livre = (asin or "").upper() or None
        clef = (mot, livre, instant)

        if clef in vus:
            continue

        vus.add(clef)

        snap.lookups.append({
            "mot": mot,
            "radical": radical,
            "langue": langue,
            "asin": livre,
            "phrase": phrase,
            "cherche_le": instant,
        })

    connexion.close()


# ------------------------------------------------------------------- API

def lire(dossier: str) -> Snapshot:
    """Lit un instantane complet. Leve SnapshotError si rien d'exploitable."""
    if not os.path.isdir(dossier):
        raise SnapshotError(f"dossier introuvable : {dossier}")

    presents = [nom for nom in FICHIERS
                if os.path.exists(os.path.join(dossier, nom))]

    if not presents:
        raise SnapshotError(
            f"aucun fichier Kindle dans {dossier} "
            f"(attendus : {', '.join(FICHIERS)})")

    snap = Snapshot(dossier=dossier)

    _lire_catalogue(dossier, snap)
    _lire_sessions(dossier, snap)
    _lire_annotations(dossier, snap)
    _lire_lookups(dossier, snap)

    return snap


def resume_brut(snap: Snapshot) -> dict:
    """Version JSON-serialisable de l'instantane, pour raw_payload.

    Archiver le brut est ce qui rend la collecte rejouable : si un jour
    le mapper se revele bogue, on corrige le code et on rejoue depuis la
    base, sans redemander a la Kindle - qui aura efface son tampon.
    """
    def dates_en_texte(element: dict) -> dict:
        return {cle: (valeur.isoformat() if isinstance(valeur, datetime)
                      else valeur)
                for cle, valeur in element.items()}

    return {
        "sessions": [dates_en_texte(s) for s in snap.sessions],
        "annotations": [dates_en_texte(a) for a in snap.annotations],
        "lookups": [dates_en_texte(m) for m in snap.lookups],
        "progress": snap.progress,
        "livres": snap.livres,
    }
