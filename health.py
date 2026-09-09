"""Diagnostic : est-ce que la collecte va bien ? (V2)

Une tache planifiee qui echoue silencieusement est pire que pas de tache
du tout : on croit avoir un historique, et on decouvre le trou le jour ou
on veut s'en servir - trop tard, Polar a efface.

Cette commande repond a quatre questions, dans l'ordre ou elles font mal :

    1. le token est-il encore valide ?
    2. chaque endpoint a-t-il reussi recemment ?
    3. la donnee est-elle fraiche (et pas seulement la collecte) ?
    4. manque-t-il des jours dans l'historique ?

Question 2 vs question 3 : une collecte peut reussir tous les jours en ne
ramenant rien (endpoint change, montre non synchronisee). Le succes de la
collecte ne prouve pas l'arrivee de donnees.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from database.connection import session_scope
from database.models import Metric, Observation
from config import LOCAL_TZ
from database.repository import all_sync_states
from logging_setup import get_logger

logger = get_logger(__name__)

LOCAL = ZoneInfo(LOCAL_TZ)

# Au-dela, la collecte est consideree en retard. 36 h laisse passer une
# execution ratee sans crier, mais pas deux.
STALE_HOURS = 36

# Metriques temoins : si celles-la sont fraiches, la chaine fonctionne.
WITNESS_METRICS = ["heart_rate", "sleep.score", "activity.steps"]

GAP_WINDOW_DAYS = 14


def _age(moment: datetime | None) -> str:
    if moment is None:
        return "jamais"

    delta = datetime.now(timezone.utc) - moment

    if delta.days > 0:
        return f"il y a {delta.days} j"

    return f"il y a {delta.seconds // 3600} h"


def check_token(tokens: dict | None) -> list[str]:
    """Le token est-il present et valide ?"""
    if tokens is None:
        return ["CRITIQUE  aucun token - lancer : python main.py auth"]

    obtenu = datetime.fromisoformat(tokens["obtained_at"])
    expire = obtenu + timedelta(seconds=tokens["expires_in"])
    restant = (expire - datetime.now(timezone.utc)).days

    if restant < 0:
        return ["CRITIQUE  token expire - lancer : python main.py auth"]

    if restant < 30:
        return [f"ATTENTION token expire dans {restant} j"]

    return [f"OK        token valide encore {restant} j"]


def check_sync(session) -> list[str]:
    """Chaque endpoint a-t-il reussi recemment ?"""
    lignes = []
    etats = all_sync_states(session)

    if not etats:
        return ["ATTENTION aucune collecte enregistree - lancer : "
                "python main.py collect"]

    limite = datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)

    for etat in etats:
        nom = etat.endpoint

        if etat.consecutive_failures:
            lignes.append(f"CRITIQUE  {nom} : {etat.consecutive_failures} "
                          f"echec(s) d'affilee - {etat.last_error}")

        elif etat.last_success_at is None:
            lignes.append(f"ATTENTION {nom} : jamais collecte avec succes")

        elif etat.last_success_at < limite:
            lignes.append(f"ATTENTION {nom} : dernier succes "
                          f"{_age(etat.last_success_at)}")

        else:
            lignes.append(f"OK        {nom} : {_age(etat.last_success_at)}")

    return lignes


def check_freshness(session) -> list[str]:
    """La donnee elle-meme est-elle recente ?"""
    lignes = []

    for code in WITNESS_METRICS:
        dernier = session.scalar(
            select(func.max(Observation.observed_at))
            .join(Metric, Metric.id == Observation.metric_id)
            .where(Metric.code == code))

        if dernier is None:
            lignes.append(f"ATTENTION {code} : aucune donnee en base")
        elif (datetime.now(timezone.utc) - dernier) > timedelta(days=2):
            lignes.append(f"ATTENTION {code} : derniere mesure {_age(dernier)}")
        else:
            lignes.append(f"OK        {code} : derniere mesure {_age(dernier)}")

    return lignes


def check_gaps(session) -> list[str]:
    """Manque-t-il des jours dans l'historique recent ?

    C'est le controle qui rattrape les pannes passees : la collecte peut
    aller bien aujourd'hui et avoir manque le week-end dernier.
    """
    lignes = []
    debut = date.today() - timedelta(days=GAP_WINDOW_DAYS)

    for code in WITNESS_METRICS:
        # UNE seule expression, reutilisee dans le SELECT et le GROUP BY.
        # En construire deux identiques ne suffit pas : SQLAlchemy leur
        # donne deux parametres distincts, et PostgreSQL ne les reconnait
        # alors plus comme la meme expression ("must appear in the GROUP
        # BY clause").
        #
        # timezone(LOCAL_TZ, ...) AVANT date_trunc : sans conversion, la
        # troncature se fait en UTC. Une valeur nocturne horodatee a
        # minuit local (00:00+02:00) vaut 22:00 UTC la VEILLE, et le jour
        # serait compte en trop d'un cote, manquant de l'autre.
        jour = func.date_trunc(
            "day", func.timezone(LOCAL_TZ, Observation.observed_at)
        ).label("jour")

        jours = session.execute(
            select(jour)
            .join(Metric, Metric.id == Observation.metric_id)
            .where(Metric.code == code,
                   Observation.observed_at >= debut)
            .group_by(jour)).all()

        presents = {ligne[0].date() for ligne in jours}

        # Ne pas reclamer des jours anterieurs au debut de l'historique :
        # avant l'enregistrement de la montre, il n'y a rien a trouver.
        # Sans cette borne, health crierait tous les jours pour du neant -
        # et une alerte permanente est une alerte qu'on cesse de lire.
        premier = session.scalar(
            select(func.min(Observation.observed_at))
            .join(Metric, Metric.id == Observation.metric_id)
            .where(Metric.code == code))

        if premier is None:
            continue

        # astimezone(LOCAL) pour la meme raison : premier arrive en UTC.
        plancher = max(debut, premier.astimezone(LOCAL).date())
        attendus = {debut + timedelta(days=n) for n in range(GAP_WINDOW_DAYS)}
        attendus = {jour for jour in attendus if jour >= plancher}
        manquants = sorted(attendus - presents)

        if not manquants:
            lignes.append(f"OK        {code} : {len(attendus)} jour(s) "
                          f"couvert(s) depuis {plancher}")
        else:
            apercu = ", ".join(jour.isoformat() for jour in manquants[:5])
            suite = " ..." if len(manquants) > 5 else ""
            lignes.append(f"ATTENTION {code} : {len(manquants)} jour(s) "
                          f"manquant(s) sur {len(attendus)} - {apercu}{suite}")

    return lignes


def run_health(tokens: dict | None) -> int:
    """Affiche le diagnostic. Renvoie le nombre de problemes trouves."""
    sections = [("Token", check_token(tokens))]

    with session_scope() as session:
        sections.append(("Collecte", check_sync(session)))
        sections.append(("Fraicheur des donnees", check_freshness(session)))
        sections.append((f"Trous sur {GAP_WINDOW_DAYS} jours",
                         check_gaps(session)))

    problemes = 0

    for titre, lignes in sections:
        print(f"\n--- {titre} ---")

        for ligne in lignes:
            print("  " + ligne)

            if ligne.startswith(("CRITIQUE", "ATTENTION")):
                problemes += 1

    print(f"\n{problemes} probleme(s) detecte(s)" if problemes
          else "\nTout va bien.")

    return problemes
