"""Charger la base dans pandas : le pont entre SQL et l'analyse.

Un seul module sait ouvrir la base pour toute la couche analytics. Les
autres (quality, describe, plots, correlate) recoivent des DataFrame et
ignorent d'ou ils viennent - c'est ce qui permettra de les tester un jour
sur des donnees fabriquees, sans PostgreSQL.

Trois notions portent tout le reste :

1. DataFrame  = un tableau nomme. Colonnes typees, index explicite.
2. Index temporel = l'index n'est pas une colonne comme les autres. Avec
   un DatetimeIndex, pandas debloque .rolling('7D'), .resample('W'),
   .shift(), et df.loc['2026-08'] pour trancher un mois.
3. Fuseau     = PostgreSQL renvoie des timestamptz (instants absolus, en
   UTC). Une FC "a 23h" n'a de sens qu'en heure locale : on convertit ici,
   une fois, et plus jamais ailleurs.

Sur les valeurs manquantes : elles ne sont PAS remplacees. Une nuit sans
score de sommeil reste NaN. Boucher un trou par une moyenne, c'est
inventer une mesure - et la faire compter comme une vraie dans le calcul
suivant.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from config import LOCAL_TZ
from database.connection import engine


# ------------------------------------------------------------- internes

def _read(requete: str, params: dict | None = None) -> pd.DataFrame:
    """Execute une requete et renvoie un DataFrame.

    read_sql accepte l'engine directement : SQLAlchemy emprunte une
    connexion au pool et la rend. Pas de with, pas de fuite.
    """
    return pd.read_sql(text(requete), engine, params=params or {})


def _index_journalier(df: pd.DataFrame, colonne: str) -> pd.DataFrame:
    """Met une colonne de dates en index, et comble les jours absents.

    Deux operations, deux raisons distinctes :

    - set_index : sans index temporel, .rolling(7) compte 7 LIGNES. Avec,
      on peut demander 7 JOURS - et la difference est enorme des qu'un
      jour manque.

    - reindex sur la plage complete : un jour sans aucune donnee n'existe
      pas dans le resultat SQL. Il disparait silencieusement du graphique,
      qui relie alors le 18 au 22 par un trait droit rassurant et faux.
      Reindexer transforme le jour absent en ligne de NaN : le trou
      devient visible, donc discutable.
    """
    df = df.copy()
    df[colonne] = pd.to_datetime(df[colonne])
    df = df.set_index(colonne).sort_index()

    if df.empty:
        return df

    plage = pd.date_range(df.index.min(), df.index.max(), freq="D",
                          name=colonne)
    df = df.reindex(plage)

    # Une colonne dont PostgreSQL n'a renvoye que des NULL arrive en
    # dtype "object", pas en float. Consequence sournoise :
    # select_dtypes("number") l'ignore, et elle disparait des correlations
    # sans un mot. Le cas est reel des qu'une source est declaree mais pas
    # encore alimentee - le capteur de chambre avant son installation.
    #
    # Seules les colonnes ENTIEREMENT vides sont converties : les autres
    # ont deja le bon type, et forcer to_numeric dessus detruirait les
    # colonnes d'heures (premier_repas, dernier_repas) en les passant a
    # NaN.
    vides = [c for c in df.columns
             if df[c].isna().all() and df[c].dtype == "object"]

    for colonne_vide in vides:
        df[colonne_vide] = df[colonne_vide].astype("float64")

    return df


# ------------------------------------------------------------- publiques

def daily() -> pd.DataFrame:
    """La vue v_daily : une ligne par jour, une colonne par grandeur.

    Index : DatetimeIndex journalier, sans trou (voir _index_journalier).
    """
    return _index_journalier(_read("SELECT * FROM v_daily"), "jour")


def sleep() -> pd.DataFrame:
    """La vue v_sleep : une ligne par nuit.

    Attention : l'index est la date de REVEIL. La nuit '2026-08-18' a
    commence le 17 a 23h40. C'est la convention de Polar, conservee telle
    quelle pour ne pas avoir deux conventions dans le projet.
    """
    df = _index_journalier(_read("SELECT * FROM v_sleep"), "nuit")

    # heure_coucher / heure_lever sont des time SQL -> objets datetime.time.
    # Inutilisables en calcul, gardes tels quels pour l'affichage.
    for colonne in ("coucher", "lever"):
        if colonne in df.columns:
            df[colonne] = pd.to_datetime(df[colonne], utc=True)
            df[colonne] = df[colonne].dt.tz_convert(LOCAL_TZ)

    return df


def observations(code: str) -> pd.Series:
    """Toutes les mesures d'une metrique, en serie temporelle.

    Index : instants convertis en heure locale. Sert aux metriques
    'instant' (heart_rate et ses 28 000 points) que v_daily ne peut pas
    representer - une journee de FC ne rentre pas dans une case.
    """
    df = _read(
        """
        SELECT o.observed_at, o.value
        FROM observation o
        JOIN metric m ON m.id = o.metric_id
        WHERE m.code = :code
        ORDER BY o.observed_at
        """,
        {"code": code},
    )

    if df.empty:
        return pd.Series(dtype="float64", name=code)

    # utc=True force un index tz-aware homogene ; tz_convert le ramene en
    # heure locale. Sans utc=True, pandas rend parfois un index "object"
    # de datetimes - et toutes les methodes temporelles disparaissent.
    instants = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(LOCAL_TZ)

    return pd.Series(df["value"].to_numpy(), index=instants, name=code)


def nutrition() -> pd.DataFrame:
    """La vue v_nutrition : une ligne par jour d'alimentation. (V4)"""
    return _index_journalier(_read("SELECT * FROM v_nutrition"), "jour")


def chambre() -> pd.DataFrame:
    """La vue v_chambre : l'environnement de chaque nuit. (V4)"""
    return _index_journalier(_read("SELECT * FROM v_chambre"), "nuit")


def nuits_completes() -> pd.DataFrame:
    """Sommeil + chambre + repas de la VEILLE, sur un index de nuits.

    La table qui repond a la question de la V4 : est-ce que ce que je
    mange et la temperature de ma chambre changent mes nuits ?

    Le decalage sur l'alimentation est le point delicat. La nuit du 18
    (couchee le 17 a 23h40) doit etre confrontee aux repas du **17**,
    pas du 18 : on ne digere pas a rebours. L'index de v_sleep etant la
    date de REVEIL, il faut donc reculer d'un jour cote nutrition.

    Se tromper de sens ici produirait des correlations parfaitement
    calculees et parfaitement absurdes - le petit-dejeuner du lendemain
    expliquant la nuit precedente.
    """
    nuits = sleep()
    env = chambre()
    repas = nutrition()

    if nuits.empty:
        return nuits

    # Prefixes : sans eux, "temp_moy" et "kcal" se melangeraient aux
    # colonnes du sommeil au premier ajout de source.
    env = env.add_prefix("chambre_")
    repas = repas.add_prefix("veille_")

    # Le decalage : les repas du jour J deviennent le contexte de la
    # nuit J+1, qui est indexee sur son matin.
    repas.index = repas.index + pd.Timedelta(days=1)

    return nuits.join(env, how="left").join(repas, how="left")


# ------------------------------------------------------------- lecture

def lecture() -> pd.DataFrame:
    """La vue v_lecture : une ligne par jour de lecture. (Kindle)

    Attention a l'interpretation des zeros. Un jour peut porter des
    surlignements sans aucune session : les annotations ont un historique
    complet, les sessions non - le tampon de la liseuse est vide apres
    envoi a Amazon. Un `minutes` a NaN ne veut donc pas dire "je n'ai pas
    lu", mais "la session n'a pas ete collectee a temps".
    """
    return _index_journalier(_read("SELECT * FROM v_lecture"), "jour")


def livres() -> pd.DataFrame:
    """La vue v_livres : une ligne par livre, index = ASIN."""
    df = _read("SELECT * FROM v_livres")

    if df.empty:
        return df

    df["derniere_lecture"] = pd.to_datetime(df["derniere_lecture"])

    return df.set_index("asin")


def sessions_lecture() -> pd.DataFrame:
    """Une ligne par session, avec le livre et la telemetrie.

    Le grain fin dont v_lecture est le resume. Indispensable des qu'on
    veut l'HEURE de lecture : une moyenne journaliere ne dit pas si on
    lit le matin ou a minuit.

    Les colonnes de telemetrie (mots, tournes) valent NaN quand la
    liseuse ne les a pas relevees - jamais 0. Voir kindle/mapper.py :
    un zero qui n'est pas une mesure fabrique des jours fantomes.
    """
    df = _read(
        """
        SELECT e.started_at,
               e.ended_at,
               EXTRACT(epoch FROM e.ended_at - e.started_at) AS duree_s,
               e.payload->>'asin'                  AS asin,
               e.payload->>'titre'                 AS titre,
               e.payload->>'auteur'                AS auteur,
               (e.payload->>'mots')::float         AS mots,
               (e.payload->>'tournes')::float      AS tournes
        FROM episode e
        JOIN source s ON s.id = e.source_id
        WHERE s.code = 'kindle_paperwhite' AND e.kind = 'reading_session'
        ORDER BY e.started_at
        """
    )

    if df.empty:
        return df

    for colonne in ("started_at", "ended_at"):
        df[colonne] = (pd.to_datetime(df[colonne], utc=True)
                       .dt.tz_convert(LOCAL_TZ))

    df["jour"] = df["started_at"].dt.normalize().dt.tz_localize(None)
    df["heure"] = df["started_at"].dt.hour
    df["jour_semaine"] = df["started_at"].dt.dayofweek
    df["duree_min"] = df["duree_s"] / 60.0

    # Le temps actif est une observation, pas un champ du payload : il
    # se rattache a la session par son instant de debut.
    actif = observations("reading.active_s")

    if not actif.empty:
        df = df.merge(actif.rename("actif_s").reset_index(),
                      left_on="started_at", right_on="observed_at",
                      how="left").drop(columns=["observed_at"])
        df["actif_min"] = df["actif_s"] / 60.0
    else:
        df["actif_s"] = pd.NA
        df["actif_min"] = pd.NA

    return df


def annotations_lecture() -> pd.DataFrame:
    """Surlignements, notes, marque-pages et mots cherches, a plat.

    Contrairement aux sessions, cette table porte un historique complet :
    c'est la seule serie longue de la source Kindle, et donc la seule sur
    laquelle une tendance est lisible aujourd'hui.
    """
    df = _read(
        """
        SELECT e.kind                              AS genre,
               e.started_at,
               e.payload->>'asin'                  AS asin,
               e.payload->>'titre'                 AS titre,
               e.payload->>'texte'                 AS texte,
               e.payload->>'mot'                   AS mot,
               e.payload->>'phrase'                AS phrase,
               (e.payload->>'position_debut')::bigint AS position
        FROM episode e
        JOIN source s ON s.id = e.source_id
        WHERE s.code = 'kindle_paperwhite'
          AND e.kind IN ('highlight', 'note', 'bookmark', 'word_lookup')
        ORDER BY e.started_at
        """
    )

    if df.empty:
        return df

    df["started_at"] = (pd.to_datetime(df["started_at"], utc=True)
                        .dt.tz_convert(LOCAL_TZ))
    df["jour"] = df["started_at"].dt.normalize().dt.tz_localize(None)
    df["heure"] = df["started_at"].dt.hour

    return df


def progression_livres() -> pd.DataFrame:
    """L'historique de progression, un point par changement de position.

    Vient de profile_snapshot, dont le dedoublonnage par empreinte de
    contenu fait tout le travail : une progression inchangee collectee
    cent fois ne cree qu'une ligne. L'historique existe donc sans qu'on
    ait eu a le construire.
    """
    df = _read(
        """
        SELECT s.captured_at,
               s.payload->>'asin'                     AS asin,
               s.payload->>'titre'                    AS titre,
               (s.payload->>'position')::bigint       AS position,
               (s.payload->>'position_max')::bigint   AS position_max,
               (s.payload->>'progression_pct')::float AS progression_pct
        FROM profile_snapshot s
        JOIN source src ON src.id = s.source_id
        WHERE src.code = 'kindle_paperwhite' AND s.kind = 'book_progress'
        ORDER BY s.captured_at
        """
    )

    if df.empty:
        return df

    df["captured_at"] = (pd.to_datetime(df["captured_at"], utc=True)
                         .dt.tz_convert(LOCAL_TZ))

    return df


def catalogue() -> pd.DataFrame:
    """Le catalogue des metriques, avec ce qu'on en a reellement.

    C'est la table de reference de quality.py : elle dit ce qui EXISTE
    (52 metriques declarees) face a ce qui est MESURE (certaines a zero
    ligne). L'ecart entre les deux est la premiere chose a regarder.
    """
    cat = _read(
        """
        SELECT m.code, m.unit, m.granularity,
               count(o.id)                       AS mesures,
               count(DISTINCT o.value)           AS valeurs_distinctes,
               min(o.observed_at)                AS premiere,
               max(o.observed_at)                AS derniere,
               min(o.value)                      AS valeur_min,
               max(o.value)                      AS valeur_max
        FROM metric m
        LEFT JOIN observation o ON o.metric_id = m.id
        GROUP BY m.code, m.unit, m.granularity
        ORDER BY m.granularity, m.code
        """
    ).set_index("code")

    # Meme piege qu'en V2 dans health.py : PostgreSQL renvoie de l'UTC, et
    # une premiere mesure affichee "a 20:56" alors que la montre l'a prise
    # a 22:56 fait conclure a tort qu'une journee est tronquee. Le fuseau
    # se pose ici, dans le seul module qui parle a la base.
    for colonne in ("premiere", "derniere"):
        cat[colonne] = (pd.to_datetime(cat[colonne], utc=True)
                        .dt.tz_convert(LOCAL_TZ))

    return cat


def episodes() -> pd.DataFrame:
    """Les intervalles (sommeil, zones d'activite...), pour comptage.

    La duree est calculee en SQL : PostgreSQL sait soustraire deux
    timestamptz sans se tromper de fuseau, pandas demanderait deux
    conversions de plus.
    """
    return _read(
        """
        SELECT kind, started_at, ended_at,
               EXTRACT(epoch FROM ended_at - started_at) / 60 AS duree_min
        FROM episode
        ORDER BY started_at
        """
    )
