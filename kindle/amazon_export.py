"""Import de l'export "Request My Data" d'Amazon (Kindle.zip).

Comble le trou que `kindle/reader.py` documente depuis le debut : le
tampon de sessions de la liseuse (`fmcache.db`) est vide apres chaque
envoi a Amazon, et ne garde que quelques heures d'historique. Amazon,
lui, garde tout. L'export personnel ("Compte et informations de
connexion" > "Demander mes donnees") le restitue sous forme d'une
archive ZIP contenant plusieurs centaines de fichiers CSV/JSON.

CE QUE CE MODULE IMPORTE, ET CE QU'IL IGNORE VOLONTAIREMENT
-------------------------------------------------------------
L'archive contient ~700 fichiers. La quasi-totalite est de la
telemetrie produit (recherches, clics sur des cartes de recommandation,
ouvertures de menu...) sans rapport avec l'objectif du projet. Deux
fichiers seulement portent une information que la liseuse elle-meme ne
garde pas :

    Kindle.reading-insights-sessions_with_adjustments.csv
        L'historique complet des sessions de lecture. 1394 lignes,
        19 mois, 100% des `start_time` renseignes. C'est la piece qui
        manquait.

    Kindle.KindleDocs.DocumentMetadata.csv
        Titre et auteur reels de chaque document personnel, bien plus
        fiables que le parsing de nom de fichier utilise ailleurs dans
        le connecteur.

Trois autres fichiers ont ete inspectes et ecartes explicitement, pour
qu'un futur lecteur ne les redecouvre pas en croyant a un oubli :

    whispersync.csv                  colonne Highlight = uniquement la
                                     couleur ({mchl_color:dark_blue}),
                                     jamais le texte du passage
    kindleHighlightActions.csv       journal d'actions UI (Add/Remove/
                                     Undo Highlight), pas de texte ;
                                     number_of_words_in_highlight n'est
                                     JAMAIS rempli malgre son nom
    Kindle.Devices.ReadingSession.csv  source alternative de sessions,
                                     mais 507/1437 lignes sans
                                     start_timestamp - moins complete
                                     que le fichier "with_adjustments"

**Le texte des surlignements reste donc introuvable, y compris dans cet
export.** Ni la liseuse, ni Amazon ne l'exposent par cette voie.
L'extraction KFX documentee dans le README reste la seule route.

LE PIEGE : DEUX FACONS DE MESURER LA MEME SESSION
--------------------------------------------------
La session du 2026-09-01 16h43 est enregistree DEUX FOIS, par deux
pipelines qui ne sont pas d'accord :

    fmcache.db (l'appareil)     16:43:15 -> 16:45:15   =  120 s
    export Amazon ("adjusted")  16:43:15.9 -> 16:44:15.4 = 59,5 s

Rapport de pres de 2x sur la MEME session physique. Le nom du fichier
("_with_adjustments") suggere qu'Amazon retranche le temps d'inactivite
du temps d'ouverture brut ; l'appareil, lui, semble mesurer l'ouverture
au sens large. Aucune des deux n'est fausse - elles repondent a des
questions differentes - mais les MELANGER produirait une serie dont
l'unite change silencieusement d'un jour a l'autre. C'est exactement le
piege que la V3 a nomme pour `fc_mediane` : une metrique dont la
definition varie sans le dire.

La reponse retenue n'est pas une nouvelle source, mais une **coupure
temporelle stricte** : cet import ne touche jamais aux jours deja
couverts par la collecte en direct. Tout ce qui precede le debut de
cette collecte vient d'Amazon ; tout ce qui suit vient de l'appareil.
Aucun jour n'est mesure deux fois. Chaque episode garde neanmoins un
`payload.origine` ("amazon_export" ou "device") pour qu'une analyse
future puisse le verifier elle-meme plutot que de le supposer.

CE QUE CE MODULE N'EST PAS
---------------------------
Un connecteur au sens de `connectors/base.py` : il ne s'execute pas en
continu, n'a pas de METRICS/SOURCE_CODE de module (le Protocol ne
l'exige que pour les connecteurs enregistres dans CONNECTEURS), et n'a
pas de curseur sync_state - le rejouer est sans risque grace a la
coupure temporelle et a l'idempotence de `upsert_episodes`, ce qui rend
un curseur inutile. Meme statut que `food/ciqual.py` : un import
occasionnel, pas une collecte.

Il respecte neanmoins la meme regle d'isolation que les connecteurs :
aucun import de sqlalchemy ni de database.*. Il produit un `Batch`,
c'est tout - `main.py` fait le pont vers la base.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import datetime, timezone

from database.records import Batch, EpisodeRecord, ObservationRecord

# Reutilise la source du connecteur en direct : voir la docstring,
# section "deux facons de mesurer la meme session". Ce n'est pas une
# nouvelle source, seulement une autre PERIODE de la meme activite.
SOURCE_CODE = "kindle_paperwhite"

ENDPOINT = "kindle/amazon_export"

# Chemins internes a l'archive, relatifs a sa racine. Recherches par
# SUFFIXE (voir _trouver) plutot que par chemin exact : la structure de
# dossiers d'un export "Request My Data" est versionnee par Amazon et a
# deja change d'une demande a l'autre pour d'autres utilisateurs.
FICHIER_SESSIONS = "Kindle.reading-insights-sessions_with_adjustments.csv"
FICHIER_DOCUMENTS = "Kindle.KindleDocs.DocumentMetadata.csv"

ASIN_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


class ExportError(Exception):
    """Archive introuvable ou fichier attendu absent."""


# --------------------------------------------------------------- acces

class _Source:
    """Ouvre soit un .zip, soit un dossier deja extrait, avec la meme API.

    Accepter les deux evite de forcer une extraction de 700 fichiers sur
    le disque pour n'en lire que deux - l'archive se lit directement.
    """

    def __init__(self, chemin: str):
        self.chemin = chemin
        self.zip = (zipfile.ZipFile(chemin) if os.path.isfile(chemin)
                   and chemin.lower().endswith(".zip") else None)

        if self.zip is None and not os.path.isdir(chemin):
            raise ExportError(f"ni un .zip ni un dossier : {chemin}")

    def trouver(self, nom_fichier: str) -> str | None:
        """Cherche un fichier par son NOM SEUL, ignore son chemin interne."""
        if self.zip is not None:
            for info in self.zip.infolist():
                if os.path.basename(info.filename) == nom_fichier:
                    return info.filename
            return None

        for racine, _dirs, fichiers in os.walk(self.chemin):
            if nom_fichier in fichiers:
                return os.path.join(racine, nom_fichier)

        return None

    def lignes_csv(self, nom_fichier: str):
        """Genere les lignes (dict) d'un CSV trouve par nom. Leve ExportError si absent."""
        chemin_interne = self.trouver(nom_fichier)

        if chemin_interne is None:
            raise ExportError(f"'{nom_fichier}' absent de l'export")

        if self.zip is not None:
            with self.zip.open(chemin_interne) as brut:
                # utf-8-sig : Amazon prefixe ses CSV d'un BOM. Sans le
                # gerer, la premiere colonne de l'entete s'appelle
                # "﻿start_time" et ne matche plus jamais "start_time".
                texte = io.TextIOWrapper(brut, encoding="utf-8-sig",
                                        newline="")
                yield from csv.DictReader(texte)
        else:
            with open(chemin_interne, encoding="utf-8-sig", newline="") as f:
                yield from csv.DictReader(f)

    def close(self) -> None:
        if self.zip is not None:
            self.zip.close()


# ------------------------------------------------------------- lecture

def _instant(texte: str) -> datetime | None:
    """ISO 8601 Amazon ('...Z' ou vide/'Not Available') -> datetime UTC."""
    if not texte or texte in ("Not Available", "INVALID-ASIN"):
        return None

    try:
        return datetime.fromisoformat(texte.replace("Z", "+00:00"))
    except ValueError:
        return None


def charger_documents(source: _Source) -> dict[str, dict]:
    """ASIN -> {titre, auteur}, depuis DocumentMetadata.csv.

    Bien plus fiable que le parsing de nom de fichier : c'est Amazon
    lui-meme qui separe titre et auteur, la ou reader.py doit deviner
    depuis "Auteur, Prenom - Titre (annee, editeur) - libgen.li.kfx".
    """
    catalogue: dict[str, dict] = {}

    for ligne in source.lignes_csv(FICHIER_DOCUMENTS):
        asin = (ligne.get("DocumentId") or "").upper()

        if not ASIN_RE.match(asin):
            continue

        catalogue[asin] = {
            "titre": ligne.get("Title") or None,
            "auteur": (ligne.get("DocumentProvider") or "").strip() or None,
        }

    return catalogue


def _livre(catalogue: dict[str, dict], asin: str) -> dict:
    entree = catalogue.get(asin) or {}

    return {
        "asin": asin,
        "titre": entree.get("titre") or f"(inconnu {asin[:8]})",
        "auteur": entree.get("auteur"),
    }


# ------------------------------------------------------------------ API

def map_export(chemin: str, cutoff_utc: datetime,
               catalogue: dict[str, dict] | None = None
               ) -> tuple[Batch, list[str], dict]:
    """Traduit l'export Amazon en Batch. Renvoie (lot, anomalies, stats).

    `cutoff_utc` : instant a partir duquel la collecte en direct fait
    deja foi. Toute session dont le DEBUT tombe a cet instant ou apres
    est ecartee - c'est la coupure qui empeche de compter deux fois la
    meme journee avec deux definitions differentes de "duree de
    session" (voir la docstring du module).
    """
    source = _Source(chemin)

    try:
        if catalogue is None:
            catalogue = charger_documents(source)

        lot = Batch()
        anomalies: list[str] = []
        stats = {"lues": 0, "importees": 0, "ecartees_futures": 0,
                "ecartees_asin": 0, "ecartees_incompletes": 0}

        for ligne in source.lignes_csv(FICHIER_SESSIONS):
            stats["lues"] += 1

            asin = (ligne.get("personal_document_id") or "").upper()
            debut = _instant(ligne.get("start_time"))
            fin = _instant(ligne.get("end_time"))

            if not ASIN_RE.match(asin):
                stats["ecartees_asin"] += 1
                continue

            if debut is None or fin is None:
                stats["ecartees_incompletes"] += 1
                anomalies.append(
                    f"session {ligne.get('start_time')!r} : horodatage "
                    f"illisible (ecartee)")
                continue

            if debut >= cutoff_utc:
                # Couverte par la collecte en direct : voir la coupure
                # temporelle dans la docstring du module.
                stats["ecartees_futures"] += 1
                continue

            if fin < debut:
                anomalies.append(
                    f"session {debut.isoformat()} : fin avant debut "
                    f"(ecartee)")
                continue

            brut = (ligne.get("total_reading_milliseconds") or "").strip()

            if brut:
                duree_s = float(brut) / 1000.0
            else:
                # 3 lignes sur 1394 dans l'export d'origine : le champ
                # d'Amazon est vide alors que start == end. La duree
                # calculee vaut alors 0 - une vraie mesure, pas un
                # defaut technique, et sessions_utiles() (analytics/
                # reading.py) l'ecartera comme les ouvertures furtives
                # de la collecte en direct.
                duree_s = (fin - debut).total_seconds()

            lot.episodes.append(EpisodeRecord(
                kind="reading_session",
                started_at=debut,
                ended_at=fin,
                payload={
                    **_livre(catalogue, asin),
                    "origine": "amazon_export",
                    "duree_source": "total_reading_milliseconds" if brut
                                    else "end_time - start_time",
                }))

            lot.observations.append(
                ObservationRecord("reading.duration_s", debut, duree_s))

            stats["importees"] += 1

        return lot, anomalies, stats

    finally:
        source.close()
