"""Point d'entree de Chronicle.

Usage : python main.py <commande>

    collecte : auth, status, fetch, collect, health
    base     : initdb, store, dbstats, resetdb
    analyse  : quality, describe, plots, correlate, report
    sources  : sources, ciqual, eat, food, bedroom, kindle, kindle-serve,
              kindle-import
"""

# Imports auth
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

from auth.oauth import build_authorize_url, start_callback_server
from auth.token_manager import load_tokens
from collector import collect_all
from zoneinfo import ZoneInfo

from config import BASE_DIR, CALLBACK_PORT, LOCAL_TZ, RAW_DATA_DIR
from health import run_health

# Imports analytics (V3). La couche LIT la base, elle n'y ecrit
# jamais - aucun de ces modules n'importe repository.py.
from analytics.correlate import run_correlate
from analytics.describe import run_describe
from analytics.plots import run_plots
from analytics.quality import run_quality
from analytics.reading_report import run_reading_report
from analytics.report import run_report

# Imports V4 - nouvelles sources. Aucun de ces modules ne connait
# SQLAlchemy : ils produisent des Batch, comme polar/mapper.py.
from food import journal as food_journal
from food import mapper as food_mapper
from food.ciqual import run_ciqual
from food.parser import analyser as analyser_repas, nutriments
from sensors import bedroom
from kindle import amazon_export as kindle_amazon
from kindle import mapper as kindle_mapper
from kindle import reader as kindle_reader
from kindle import receiver as kindle_receiver
from connectors.base import run_sources
from logging_setup import setup_logging

# Imports base de donnees
from database.connection import (check_connection, create_tables,
                                 dispose_engine, drop_tables, server_version,
                                 session_scope, table_counts)
from database.repository import (earliest_episode_start, get_or_create_source,
                                 get_sync_state, insert_raw_payload,
                                 mark_sync_success, store_batch, sync_metrics)

# Imports Polar
from polar.activity import get_activity_infos
from polar.bodytemp import get_body_temperature, get_skin_temperature
from polar.cardio_load import get_cardio_load
from polar.client import PolarClient
from polar.continuous_heartrate import get_continuous_heartRate_range
from polar.exercises import get_exercises_data
from polar.mapper import (METRICS, SOURCE_CODE, SOURCE_LABEL, UnmappedEndpoint,
                          map_envelope, to_datetime)
from polar.nightly_recharge import get_nightly_recharge
from polar.physical import get_physical_info
from polar.sleep import (get_circadian_bedtime, get_sleep_available,
                         get_sleep_data, get_sleepWise_data)
from polar.user import get_user_info

# Nombre de jours affiches pour les sources qui renvoient de longues listes
RECENT_DAYS = 7


# ---------------------------------------------------------------- helpers

def require_tokens() -> dict:
    """Charge les tokens, ou termine le programme si aucun n'existe.

    Utilise par les commandes qui ne peuvent rien faire sans token.
    cmd_status, elle, sait repondre "aucun token" - c'est sa raison d'etre.
    """
    tokens = load_tokens()

    if tokens is None:
        print("Aucun token - lance : python main.py auth")
        sys.exit(1)

    return tokens


# ------------------------------------------------------------- commandes

def cmd_auth() -> None:
    """
    Lance le flux Oauth2 complet et sauvegarde les tokens.
    """
    try:
        authorize_url, state = build_authorize_url()
        print("Open this URL in your browser:")
        print(authorize_url)
        print("state:", state)

        start_callback_server(state)

    except OSError as error:
        print(f"Impossible d'ouvrir le port {CALLBACK_PORT} : {error}")
        print(f"Verifier qui l'occupe : Get-NetTCPConnection -LocalPort {CALLBACK_PORT}")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\nAuthorisation annulee - aucun token enregistre.")
        sys.exit(1)


def cmd_status() -> None:
    """
    Affiche l'etat du token : present ? user_id ? obtenu quand ?
    """
    tokens = load_tokens()

    if tokens is None:
        print("Aucun token - lance : python main.py auth")
        return

    print("X_user_id: ", tokens["x_user_id"])
    print("obtained_at: ", tokens["obtained_at"])

    date_of_obtention = datetime.fromisoformat(tokens["obtained_at"])
    expires_at = date_of_obtention + timedelta(seconds=tokens["expires_in"])
    now = datetime.now(timezone.utc)
    time_left = expires_at - now

    if time_left.days < 0:
        print("Token EXPIRE - relance : python main.py auth")
        return

    print(f"Token valide jusqu'au {expires_at.strftime('%Y-%m-%d')}"
          f" ({time_left.days} jours restants)")


def cmd_fetch() -> None:
    """
    Appelle chaque module Polar et affiche un resume.

    Une source en echec n'interrompt pas les autres : chaque appel est
    isole. Polar efface apres ~28 jours, mieux vaut recuperer 7 sources
    sur 8 que rien du tout.
    """
    tokens = require_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    for titre, afficher in SOURCES:
        print(f"\n--- {titre} ---")
        try:
            afficher(client)
        except requests.HTTPError as error:
            print(f"  [echec] {error.response.status_code} - source ignoree")


def cmd_collect() -> None:
    """Collecte Polar -> archive -> base, en un seul passage. (V2)

    C'est la commande de la tache planifiee. Elle sort avec un code != 0
    si quelque chose a echoue : c'est ce que le planificateur Windows
    affiche dans sa colonne "Dernier resultat".
    """
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)

    tokens = require_tokens()
    echecs = collect_all(tokens)

    if echecs:
        sys.exit(1)


def cmd_health() -> None:
    """Diagnostic de la collecte. Code de sortie = nombre de problemes."""
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)

    if run_health(load_tokens()):
        sys.exit(1)


def _enregistrer(source_code: str, source_label: str, metrics, lot) -> dict:
    """Ecrit un Batch en base. Le meme chemin que Polar, sans exception.

    C'est ici que se joue le critere de la V4 : cette fonction n'appelle
    que des helpers ecrits en V1/V2 (get_or_create_source, sync_metrics,
    store_batch). Aucune requete nouvelle, aucune table nouvelle.
    """
    with session_scope() as session:
        source_id = get_or_create_source(session, source_code, source_label)
        metric_ids = sync_metrics(session, metrics)

    with session_scope() as session:
        return store_batch(session, source_id, metric_ids, lot)


def _resume(totaux: dict) -> str:
    """Une ligne lisible a partir du compte-rendu de store_batch."""
    return "  ".join(f"{cle.replace('_', ' ')} {valeur}"
                     for cle, valeur in totaux.items() if valeur)


def cmd_sources() -> None:
    """Liste les connecteurs et verifie qu'aucun ne connait la base.

    Le controle du critere de la V4, sous forme de commande : il doit
    rester vert a chaque source ajoutee.
    """
    if run_sources():
        sys.exit(1)


def cmd_ciqual() -> None:
    """Importe la table CIQUAL, ou y cherche un aliment.

        python main.py ciqual                 -> (re)construit le JSON
        python main.py ciqual "riz blanc"     -> cherche
    """
    requete = " ".join(a for a in sys.argv[2:] if not a.startswith("-"))

    if run_ciqual(requete or None):
        sys.exit(1)


def cmd_eat() -> None:
    """Ajoute une ligne au journal alimentaire du jour, et la relit.

        python main.py eat "12h30, 100g riz cuit, 55g poulet"

    La ligne est ecrite dans le journal AVANT d'etre analysee : meme mal
    comprise, elle n'est pas perdue. C'est le fichier qui fait foi, la
    base n'en est qu'une lecture - relancer `food` apres correction
    suffit a rattraper.
    """
    ligne = " ".join(sys.argv[2:]).strip()

    if not ligne:
        print('usage : python main.py eat "12h30, 100g riz cuit, 55g poulet"')
        sys.exit(1)

    jour = date.today()
    fichier = food_journal.ajouter(ligne, jour)
    print(f"ecrit dans {fichier.relative_to(BASE_DIR)}\n")

    repas = analyser_repas(ligne, jour)

    if repas.moment:
        print(f"{repas.moment:%H:%M}")

    total = 0.0

    for element in repas.elements:
        valeurs = nutriments(element)
        total += valeurs.get("energy_kcal", 0.0)
        marque = " [estime]" if element.estime else ""
        print(f"  OK  {element.texte:<24} -> {element.ciqual_nom}"
              f"  {element.grammes:.0f} g  "
              f"{valeurs.get('energy_kcal', 0):.0f} kcal{marque}")

        if element.note:
            print(f"        {element.note}")

    for refus in repas.refus:
        print(f"  KO  {refus.texte:<24} -> {refus.raison}")

    if repas.elements:
        print(f"\n  total de la ligne : {total:.0f} kcal")

    if repas.refus:
        print("\nLigne conservee dans le journal mais NON enregistree en base.")
        print("Corriger le fichier, puis relancer : python main.py food")
        sys.exit(1)


def cmd_food() -> None:
    """Rejoue le journal alimentaire vers PostgreSQL.

    Meme contrat que `store` : rejouable, idempotent. Corriger une ligne
    du journal et relancer met la base a jour sans creer de doublon -
    c'est l'ON CONFLICT de la V1 qui s'en charge, sans une ligne de code
    supplementaire ici.
    """
    _exiger_base()

    jours = food_journal.jours_disponibles()

    if not jours:
        print(f"Aucun journal dans {food_journal.JOURNAL_DIR}")
        print('En creer un : python main.py eat "12h30, 100g riz cuit"')
        return

    print(f"{len(jours)} jour(s) de journal\n")

    total_lot = 0
    refuses = 0

    for jour in jours:
        lot, repas = food_mapper.map_jour(jour)
        problemes = [r for r in repas if not r.complet]

        if len(lot):
            totaux = _enregistrer(food_mapper.SOURCE_CODE,
                                  food_mapper.SOURCE_LABEL,
                                  food_mapper.METRICS, lot)
            total_lot += len(lot)
            print(f"  {jour}  {len(repas) - len(problemes)}/{len(repas)} "
                  f"repas  |  {_resume(totaux)}")
        else:
            print(f"  {jour}  aucun repas exploitable")

        for repas_refuse in problemes:
            refuses += 1
            print(f"      [refuse] {repas_refuse.ligne}")
            for refus in repas_refuse.refus:
                print(f"               {refus.raison}")

    print(f"\n{total_lot} enregistrement(s) traites.")

    if refuses:
        print(f"{refuses} ligne(s) refusee(s) - corriger le journal et relancer.")
        sys.exit(1)


def cmd_bedroom() -> None:
    """Ramene les mesures du capteur de chambre.

        python main.py bedroom              -> interroge le module
        python main.py bedroom --simulate   -> donnees FABRIQUEES

    Le mode simulation existe pour developper sans materiel. Il est
    bruyant a dessein : des donnees inventees dans une base personnelle
    sont pires que pas de donnees du tout.
    """
    _exiger_base()

    simulation = "--simulate" in sys.argv

    if simulation:
        print("!" * 62)
        print("MODE SIMULATION - les mesures ci-dessous sont FABRIQUEES.")
        print("Elles n'ont aucune valeur et ne doivent pas etre analysees.")
        print("!" * 62)
        charge = bedroom.simuler()
    else:
        url = os.getenv("BEDROOM_SENSOR_URL")

        if not url:
            print("BEDROOM_SENSOR_URL absent du .env")
            print("  1. televerser sensors/firmware/bedroom_esp32.ino")
            print("  2. relever l'IP affichee sur le moniteur serie")
            print("  3. .env : BEDROOM_SENSOR_URL=http://192.168.1.42")
            print("\nSans materiel : python main.py bedroom --simulate")
            sys.exit(1)

        # Le curseur de la V2, reutilise tel quel pour une source qui
        # n'a rien a voir avec Polar. C'est exactement ce que la V4
        # cherchait a verifier.
        with session_scope() as session:
            source_id = get_or_create_source(session, bedroom.SOURCE_CODE,
                                             bedroom.SOURCE_LABEL)
            curseur = get_sync_state(session, source_id, bedroom.ENDPOINT)
            depuis = curseur.last_success_at if curseur else None

        try:
            charge = bedroom.lire(url, depuis)
        except bedroom.SensorError as erreur:
            print(f"Capteur : {erreur}")
            sys.exit(1)

    lot, anomalies = bedroom.mapper(charge)

    for anomalie in anomalies:
        print(f"  [ecarte] {anomalie}")

    if not len(lot):
        print("Aucune mesure exploitable.")
        return

    totaux = _enregistrer(bedroom.SOURCE_CODE, bedroom.SOURCE_LABEL,
                          bedroom.METRICS, lot)

    instants = [o.observed_at for o in lot.observations]
    print(f"{len(lot.observations)} mesures  "
          f"({min(instants):%Y-%m-%d %H:%M} -> {max(instants):%H:%M} UTC)")
    print(f"  {_resume(totaux)}")

    if not simulation:
        with session_scope() as session:
            source_id = get_or_create_source(session, bedroom.SOURCE_CODE,
                                             bedroom.SOURCE_LABEL)
            mark_sync_success(session, source_id, bedroom.ENDPOINT,
                              max(instants), len(lot.observations))


# ------------------------------------------------------------ Kindle

def _ingerer_instantane(dossier: str) -> str:
    """Lit un instantane Kindle et l'ecrit en base. Renvoie un resume.

    Cette fonction est le pont entre le recepteur (qui ne connait que des
    fichiers) et la base. Elle est passee en rappel a receiver.servir,
    ce qui permet a un appui sur la liseuse d'aller jusqu'en base d'un
    seul geste - sans que receiver.py n'importe quoi que ce soit de
    database/.
    """
    snap = kindle_reader.lire(dossier)
    lot, anomalies = kindle_mapper.map_snapshot(snap)

    for anomalie in anomalies:
        print(f"  [anomalie] {anomalie}")

    if not len(lot):
        return "instantane vide"

    totaux = _enregistrer(kindle_mapper.SOURCE_CODE,
                          kindle_mapper.SOURCE_LABEL,
                          kindle_mapper.METRICS, lot)

    # Le brut est archive APRES l'ingestion, et sous le meme
    # dedoublonnage par empreinte que Polar : reenvoyer un instantane
    # inchange ne cree pas une seconde ligne.
    with session_scope() as session:
        source_id = get_or_create_source(session, kindle_mapper.SOURCE_CODE,
                                         kindle_mapper.SOURCE_LABEL)
        insert_raw_payload(
            session, source_id, kindle_mapper.ENDPOINT,
            {"dossier": os.path.basename(dossier)},
            datetime.now(timezone.utc),
            kindle_reader.resume_brut(snap),
            filename=os.path.basename(dossier))

        curseur = kindle_mapper.curseur(lot)

        if curseur is not None:
            mark_sync_success(session, source_id, kindle_mapper.ENDPOINT,
                              curseur)

    return (f"{len(snap.sessions)} sessions, {len(snap.annotations)} "
            f"annotations, {len(snap.lookups)} mots  |  {_resume(totaux)}")


def cmd_kindle() -> None:
    """Rejoue les instantanes Kindle deja recus vers PostgreSQL.

        python main.py kindle           -> rejoue TOUT data/raw/kindle/
        python main.py kindle <dossier> -> rejoue un instantane precis

    Rejouable a volonte, comme `store` : l'idempotence de la V1 fait le
    reste. C'est cette commande qui rattrape les envois faits pendant que
    le conteneur etait eteint.
    """
    _exiger_base()

    racine = RAW_DATA_DIR / "kindle"
    argument = [a for a in sys.argv[2:] if not a.startswith("-")]

    if argument:
        dossiers = [argument[0]]
    elif racine.is_dir():
        dossiers = sorted(str(chemin) for chemin in racine.iterdir()
                          if chemin.is_dir())
    else:
        dossiers = []

    if not dossiers:
        print(f"Aucun instantane dans {racine}")
        print("Lance le recepteur : python main.py kindle-serve")
        return

    for dossier in dossiers:
        nom = os.path.basename(dossier)

        try:
            print(f"{nom} : {_ingerer_instantane(dossier)}")

        except kindle_reader.SnapshotError as erreur:
            print(f"{nom} : ignore ({erreur})")


def cmd_kindle_import() -> None:
    """Importe l'export "Request My Data" d'Amazon (Kindle.zip).

        python main.py kindle-import --source "C:/.../Kindle.zip"
        python main.py kindle-import --source "C:/.../dossier_extrait"

    Comble le trou d'historique de session que fmcache.db ne garde pas :
    voir kindle/amazon_export.py pour le detail (deux pipelines mesurent
    la meme session differemment, d'ou une coupure temporelle stricte
    plutot qu'une fusion).

    Rejouable a volonte : la coupure est recalculee depuis la base a
    chaque execution, et upsert_episodes est idempotent.
    """
    _exiger_base()

    argument = [a for a in sys.argv[2:] if not a.startswith("-")]
    apres_flag = "--source" in sys.argv

    if apres_flag:
        position = sys.argv.index("--source") + 1
        chemin = sys.argv[position] if position < len(sys.argv) else None
    else:
        chemin = argument[0] if argument else None

    if not chemin:
        print("Usage : python main.py kindle-import --source "
             "\"chemin/vers/Kindle.zip\"")
        sys.exit(1)

    with session_scope() as session:
        source_id = get_or_create_source(session, kindle_mapper.SOURCE_CODE,
                                         kindle_mapper.SOURCE_LABEL)
        plus_ancienne = earliest_episode_start(
            session, source_id, "reading_session",
            payload_egal={"origine": "device"})

    if plus_ancienne is not None:
        # Frontiere au jour pres, en heure locale : tout ce qui a
        # commence AVANT le jour ou la collecte en direct a demarre vient
        # de l'export ; ce jour-la et apres, l'appareil fait deja foi.
        locale = plus_ancienne.astimezone(ZoneInfo(LOCAL_TZ))
        minuit_local = locale.replace(hour=0, minute=0, second=0,
                                      microsecond=0)
        cutoff = minuit_local.astimezone(timezone.utc)
    else:
        # Aucune session en direct encore collectee : rien a proteger,
        # l'export peut etre importe dans son ensemble.
        cutoff = datetime.now(timezone.utc)

    print(f"Coupure temporelle : avant {cutoff.astimezone(ZoneInfo(LOCAL_TZ)):%Y-%m-%d} "
         f"= export Amazon, a partir de cette date = collecte en direct.")

    try:
        lot, anomalies, stats = kindle_amazon.map_export(chemin, cutoff)
    except kindle_amazon.ExportError as erreur:
        print(f"Export illisible : {erreur}")
        sys.exit(1)

    for anomalie in anomalies[:20]:
        print(f"  [anomalie] {anomalie}")

    if len(anomalies) > 20:
        print(f"  ... et {len(anomalies) - 20} autre(s)")

    print(f"\n{stats['lues']} lignes lues dans l'export")
    print(f"  importees              : {stats['importees']}")
    print(f"  ecartees (deja en direct) : {stats['ecartees_futures']}")
    print(f"  ecartees (ASIN invalide)  : {stats['ecartees_asin']}")
    print(f"  ecartees (incompletes)    : {stats['ecartees_incompletes']}")

    if not len(lot):
        print("\nRien a ecrire.")
        return

    totaux = _enregistrer(kindle_mapper.SOURCE_CODE, kindle_mapper.SOURCE_LABEL,
                          kindle_mapper.METRICS, lot)

    instants = [e.started_at for e in lot.episodes]

    with session_scope() as session:
        source_id = get_or_create_source(session, kindle_mapper.SOURCE_CODE,
                                         kindle_mapper.SOURCE_LABEL)
        insert_raw_payload(
            session, source_id, kindle_amazon.ENDPOINT,
            {"fichier": os.path.basename(chemin), "cutoff": cutoff.isoformat()},
            datetime.now(timezone.utc),
            {"sessions": [
                {"asin": e.payload["asin"], "started_at": e.started_at.isoformat(),
                 "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                 "titre": e.payload.get("titre")}
                for e in lot.episodes],
             "stats": stats},
            filename=os.path.basename(chemin))

    print(f"\n{min(instants):%Y-%m-%d} -> {max(instants):%Y-%m-%d}  "
         f"{_resume(totaux)}")


def cmd_kindle_serve() -> None:
    """Ecoute les envois de la liseuse et les ingere a la volee.

        python main.py kindle-serve
        python main.py kindle-serve --port 8765 --token moncode

    La liseuse televerse quand on appuie sur "Envoyer au PC" dans sa
    bibliotheque. Voir kindle/device/ pour le script a y deposer.
    """
    def option(nom: str, defaut: str) -> str:
        if nom in sys.argv:
            position = sys.argv.index(nom) + 1

            if position < len(sys.argv):
                return sys.argv[position]

        return defaut

    port = int(option("--port", os.getenv("KINDLE_PORT", "8765")))
    token = option("--token", os.getenv("KINDLE_TOKEN", "kindle"))

    # La base n'est PAS exigee ici : recevoir doit marcher meme si
    # Docker est eteint. L'ingestion echouera, les fichiers resteront,
    # et "python main.py kindle" les rattrapera.
    base_prete = check_connection()

    if not base_prete:
        print("Base injoignable - les envois seront conserves sur le disque.")
        print("  Les ingerer plus tard : python main.py kindle")
        print()

    def rappel(dossier: str) -> str:
        if not check_connection():
            return "recu (base injoignable, a rejouer avec : main.py kindle)"

        return _ingerer_instantane(dossier)

    kindle_receiver.servir(dossier=str(RAW_DATA_DIR / "kindle"),
                           token=token, port=port, rappel=rappel)


def _exiger_base() -> None:
    """Les commandes d'analyse ne peuvent rien faire sans la base."""
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)


def cmd_quality() -> None:
    """Audit de la donnee. Code de sortie = nombre de constats bloquants."""
    _exiger_base()

    if run_quality():
        sys.exit(1)


def cmd_describe() -> None:
    """Statistique descriptive. Ne juge pas, ne peut pas echouer."""
    _exiger_base()
    run_describe()


def cmd_plots() -> None:
    """Ecrit les figures PNG dans reports/figures/."""
    _exiger_base()
    run_plots()


def cmd_correlate() -> None:
    """Tableau des correlations, avec n, p et correction de Bonferroni."""
    _exiger_base()
    run_correlate()


def cmd_report() -> None:
    """Rapport date complet : figures + Markdown, dans reports/AAAA-MM-JJ/."""
    _exiger_base()
    run_report()


def cmd_lecture() -> None:
    """Tableau de bord de lecture : figures + HTML autonome.

        python main.py lecture

    Produit reports/lecture-<date>/index.html, avec les figures
    encastrees en base64 - le fichier se deplace donc d'un bloc.
    """
    _exiger_base()
    run_reading_report()


def cmd_initdb() -> None:
    """Cree les tables manquantes a partir de database/models.py."""
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)

    create_tables()
    print(f"PostgreSQL {server_version()} - tables pretes.")

    for table, lignes in table_counts().items():
        print(f"  {table:<18} {lignes:>8} lignes")


def cmd_dbstats() -> None:
    """Compte les lignes de chaque table. Le juge de paix de l'idempotence."""
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)

    for table, lignes in table_counts().items():
        print(f"  {table:<18} {lignes:>8} lignes")


def cmd_resetdb() -> None:
    """Supprime toutes les tables. Destructif, demande confirmation."""
    print("Cette commande DETRUIT toutes les tables et leurs donnees.")

    if input("Taper 'oui' pour confirmer : ").strip().lower() != "oui":
        print("Annule.")
        return

    drop_tables()
    create_tables()
    print("Tables recreees, vides.")


def cmd_store() -> None:
    """Rejoue data/raw/ vers PostgreSQL.

    Rejouable a volonte : c'est tout l'interet de l'etape 7. Lancer cette
    commande deux fois de suite doit laisser exactement le meme nombre de
    lignes - sinon l'idempotence est cassee quelque part.

    Une session par fichier : un fichier illisible ou un traducteur bogue
    ne doit pas annuler le travail des dix autres.
    """
    if not check_connection():
        print("Base injoignable - demarre le conteneur : docker compose up -d")
        sys.exit(1)

    fichiers = sorted(RAW_DATA_DIR.glob("*.json"))

    if not fichiers:
        print(f"Aucun fichier dans {RAW_DATA_DIR} - lance : python main.py fetch")
        return

    # Registres d'abord, dans leur propre transaction : source et
    # metriques doivent exister avant la moindre observation (contrainte
    # de clef etrangere).
    with session_scope() as session:
        source_id = get_or_create_source(session, SOURCE_CODE, SOURCE_LABEL)
        metric_ids = sync_metrics(session, METRICS)

    print(f"source #{source_id} - {len(metric_ids)} metriques au catalogue")
    print(f"{len(fichiers)} fichier(s) a rejouer\n")

    totaux = {"observations_inserees": 0, "observations_majs": 0,
              "episodes_inseres": 0, "episodes_majs": 0,
              "snapshots_inseres": 0, "bruts_archives": 0}
    ignores = 0

    for chemin in fichiers:
        try:
            with open(chemin, encoding="utf-8") as fichier:
                enveloppe = json.load(fichier)

        except (OSError, json.JSONDecodeError) as error:
            print(f"  [illisible] {chemin.name} : {error}")
            ignores += 1
            continue

        try:
            with session_scope() as session:
                nouveau = insert_raw_payload(
                    session, source_id,
                    endpoint=enveloppe["endpoint"],
                    params=enveloppe.get("params"),
                    fetched_at=to_datetime(enveloppe["fetched_at"]),
                    payload=enveloppe.get("data"),
                    filename=chemin.name)

                if nouveau:
                    totaux["bruts_archives"] += 1

                # Le parsing est refait a CHAQUE passage, meme si la
                # reponse brute etait deja connue : c'est ce qui permet
                # de corriger un traducteur et de rejouer, et c'est aussi
                # ce qui met reellement l'idempotence a l'epreuve.
                resume = store_batch(session, source_id, metric_ids,
                                     map_envelope(enveloppe))

            for cle, valeur in resume.items():
                totaux[cle] += valeur

            print(f"  {chemin.name:<52} "
                  f"obs +{resume['observations_inserees']:<6} "
                  f"~{resume['observations_majs']:<6} "
                  f"epi +{resume['episodes_inseres']:<5} "
                  f"snap +{resume['snapshots_inseres']}")

        except UnmappedEndpoint as error:
            print(f"  [non mappe] {chemin.name} - endpoint {error} "
                  f"(reponse brute archivee, parsing a ecrire)")
            ignores += 1

        except Exception as error:
            print(f"  [echec] {chemin.name} : {type(error).__name__} {error}")
            ignores += 1

    print("\n--- total ---")
    for cle, valeur in totaux.items():
        print(f"  {cle:<24} {valeur:>8}")

    if ignores:
        print(f"  {'fichiers ignores':<24} {ignores:>8}")

    print("\n--- lignes en base ---")
    for table, lignes in table_counts().items():
        print(f"  {table:<18} {lignes:>8}")


# ------------------------------------------------------- affichage source

def show_user(client: PolarClient) -> None:
    """Profil du compte."""
    infos = get_user_info(client)

    if not infos:
        print("  aucune donnee")
        return

    print("  member-id", infos.get("member-id"),
          "| inscrit le", infos.get("registration-date"))


def show_physical(client: PolarClient) -> None:
    """Poids, taille, VO2max, FC de repos."""
    infos = get_physical_info(client)

    if not infos:
        print("  aucune donnee")
        return

    print("  poids", infos.get("weight"), "kg",
          "| taille", infos.get("height"), "cm",
          "| VO2max", infos.get("vo2_max"),
          "| FC repos", infos.get("resting_heart_rate"))


def show_activity(client: PolarClient) -> None:
    """Pas par periode d'activite, echantillons compris.

    Les trois options font passer la reponse de 1.8 Ko a 147 Ko : sans
    elles on n'archive que les totaux journaliers, et les series minute
    par minute sont perdues avec le reste au bout de 28 jours.
    """
    activites = get_activity_infos(client,
                                   steps=True,
                                   activity_zones=True,
                                   inactivity_stamps=True)
    print(f"  {len(activites)} periode(s)")

    for activite in activites[-RECENT_DAYS:]:
        echantillons = activite.get("samples", {})
        mesures = len(echantillons.get("steps", {}).get("samples", []))
        zones = len(echantillons.get("activity_zones", {}).get("samples", []))

        print("  ", activite.get("start_time"), "-", activite.get("steps"), "pas",
              f"| {activite.get('calories')} kcal",
              f"| {mesures} mesures", f"| {zones} zones")


def show_sleep(client: PolarClient) -> None:
    """Nuits et score de sommeil."""
    nuits = get_sleep_data(client)
    print(f"  {len(nuits)} nuit(s)")

    for nuit in nuits[-RECENT_DAYS:]:
        print("  ", nuit.get("date"), "- score", nuit.get("sleep_score"))

    print(f"  {len(get_sleepWise_data(client))} releve(s) sleepwise")

    # Nuits encore detenues par Polar : ce qui va sortir de la fenetre
    # des 28 jours si on ne collecte pas.
    dispos = get_sleep_available(client)
    print(f"  {len(dispos)} nuit(s) encore disponibles chez Polar")

    print(f"  {len(get_circadian_bedtime(client))} prediction(s) de coucher")


def show_nightly_recharge(client: PolarClient) -> None:
    """Recuperation nocturne : HRV, statut."""
    recharges = get_nightly_recharge(client)
    print(f"  {len(recharges)} recharge(s)")

    for recharge in recharges[-RECENT_DAYS:]:
        print("  ", recharge.get("date"),
              "- HRV", recharge.get("heart_rate_variability_avg"),
              "- statut", recharge.get("nightly_recharge_status"))


def show_cardio_load(client: PolarClient) -> None:
    """Charge cardio : strain, tolerance, ratio."""
    charges = get_cardio_load(client)
    print(f"  {len(charges)} jour(s)")

    for charge in charges[-RECENT_DAYS:]:
        print("  ", charge.get("date"),
              "- charge", charge.get("cardio_load"),
              "- statut", charge.get("cardio_load_status"))


def show_exercises(client: PolarClient) -> None:
    """Seances d'entrainement recentes."""
    seances = get_exercises_data(client)
    print(f"  {len(seances)} seance(s)")

    for seance in seances:
        print("  ", seance.get("start_time"), "-", seance.get("sport"),
              "-", seance.get("duration"))


def show_heart_rate(client: PolarClient) -> None:
    """FC continue sur les derniers jours."""
    fin = date.today()
    debut = fin - timedelta(days=RECENT_DAYS - 1)

    jours = get_continuous_heartRate_range(client, debut.isoformat(), fin.isoformat())
    print(f"  {len(jours)} jour(s) entre {debut} et {fin}")

    for jour in jours:
        print("  ", jour.get("date"), "-",
              len(jour.get("heart_rate_samples", [])), "mesures")


def show_body_temperature(client: PolarClient) -> None:
    """Temperatures corporelle et cutanee (capteur biosensing)."""
    print(f"  {len(get_body_temperature(client))} releve(s) corporelle")
    print(f"  {len(get_skin_temperature(client))} releve(s) cutanee")


SOURCES = [
    ("Compte", show_user),
    ("Physiologie", show_physical),
    ("Activite", show_activity),
    ("Sommeil", show_sleep),
    ("Nightly Recharge", show_nightly_recharge),
    ("Charge cardio", show_cardio_load),
    ("Entrainements", show_exercises),
    ("FC continue", show_heart_rate),
    ("Temperature", show_body_temperature),
]

COMMANDS = {"auth": cmd_auth,
            "status": cmd_status,
            "fetch": cmd_fetch,
            "initdb": cmd_initdb,
            "store": cmd_store,
            "collect": cmd_collect,
            "health": cmd_health,
            "quality": cmd_quality,
            "describe": cmd_describe,
            "plots": cmd_plots,
            "correlate": cmd_correlate,
            "report": cmd_report,
            "sources": cmd_sources,
            "ciqual": cmd_ciqual,
            "eat": cmd_eat,
            "food": cmd_food,
            "bedroom": cmd_bedroom,
            "kindle": cmd_kindle,
            "kindle-serve": cmd_kindle_serve,
            "kindle-import": cmd_kindle_import,
            "lecture": cmd_lecture,
            "dbstats": cmd_dbstats,
            "resetdb": cmd_resetdb}

USAGE = "usage : python main.py <" + "|".join(COMMANDS) + ">"


# ------------------------------------------------------------------ main

def main() -> None:
    """
    Lit sys.argv, trouve la commande, l'execute.
    Pas d'argument ou commande inconnue -> usage + sys.exit(1).
    """
    # -v / --verbose : passe la console en DEBUG. Le fichier de log, lui,
    # recoit toujours tout.
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    setup_logging(verbose=verbose)

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    command = sys.argv[1]

    if command not in COMMANDS:
        print(f"{command} is not a valid command")
        print(USAGE)
        sys.exit(1)

    function = COMMANDS[command]

    try:
        function()

    except requests.ConnectionError:
        print("Pas de connexion reseau - verifie ta connexion internet.")
        sys.exit(1)

    except requests.HTTPError as error:
        statut = error.response.status_code
        print(f"Polar a repondu {statut}")
        if statut == 401:
            print("Token invalide ou expire - relance : python main.py auth")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(1)

    finally:
        # Rend les connexions du pool avant de quitter, meme en cas
        # d'erreur : sys.exit passe par ici.
        dispose_engine()


if __name__ == "__main__":
    main()
