"""Vues de lecture : le confort, sans toucher au modele.

Principe : on normalise a l'ECRITURE, on denormalise a la LECTURE.

Les tables (observation, episode) restent la verite : etroites, sans
redondance, extensibles sans migration. Mais une table etroite se lit mal -
correler HRV et pas demandait 14 lignes de sous-requetes imbriquees. Ces
vues font le pivot une fois pour toutes.

Une vue ne stocke RIEN. C'est une requete enregistree sous un nom : elle
est recalculee a chaque appel, donc toujours juste, et ne peut pas se
desynchroniser des donnees. A l'echelle du projet (34 000 lignes) le cout
est nul ; si un jour il compte, la reponse sera MATERIALIZED VIEW +
REFRESH, au prix d'une donnee qui peut dater.

Les vues sont detruites puis recreees a chaque initdb : sans donnees a
perdre, c'est l'approche la plus simple et la plus sure.
Rejouer initdb est donc sans effet de bord, comme le reste du projet.

ATTENTION : le fuseau est fige dans le SQL au moment de la creation.
Changer LOCAL_TZ dans .env impose de relancer `python main.py initdb`.
"""

from sqlalchemy import text

from config import LOCAL_TZ

# ---------------------------------------------------------------------
# v_daily - une ligne par jour, une colonne par grandeur
#
# Ne prend que les metriques 'daily' et 'nightly' : ce sont celles qui ont
# deja UNE valeur par jour. Y meler des metriques 'instant' n'aurait pas
# de sens (28 559 mesures de FC ne rentrent pas dans une case).
#
# La FC est donc traitee a part, et en DEUX temps. Le premier jet de
# cette vue prenait la mediane des echantillons bruts, en pensant qu'une
# mediane "resiste". Verifie sur la journee du 2026-08-21 :
#
#     mediane des 9 175 echantillons bruts .......... 108 bpm
#     mediane des 412 moyennes par minute ........... 75,5 bpm
#
# 32 bpm d'ecart. La mediane protege des valeurs ABERRANTES ; elle ne
# protege pas d'un echantillonnage IRREGULIER. La Pacer releve a 1 Hz en
# activite et une fois par 5 min au repos : une heure d'effort pese donc
# 3 600 lignes contre 12 pour une heure de calme. Les rafales ne sont pas
# des valeurs extremes qu'on ecarte, elles sont la majorite des lignes -
# et aucune statistique de position ne repare ca.
#
# La reponse n'est pas statistique mais temporelle : ramener d'abord
# chaque minute a UNE valeur (fc_minutes), puis agreger ces minutes. Une
# minute d'effort pese alors autant qu'une minute de repos.
#
# fc_minutes_couvertes dit combien de minutes de la journee ont ete
# mesurees. Sur la journee la mieux couverte : 412 sur 1 440. Un chiffre
# a garder sous les yeux avant de parler de "la FC de la journee".
# ---------------------------------------------------------------------

V_DAILY = f"""
CREATE VIEW v_daily AS
WITH jours AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           max(o.value) FILTER (WHERE m.code = 'sleep.score')             AS sommeil_score,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_deep')  / 60 AS sommeil_profond_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_rem')   / 60 AS sommeil_rem_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_light') / 60 AS sommeil_leger_min,
           max(o.value) FILTER (WHERE m.code = 'recharge.hrv_avg')         AS hrv,
           max(o.value) FILTER (WHERE m.code = 'recharge.heart_rate_avg')  AS fc_nocturne,
           max(o.value) FILTER (WHERE m.code = 'recharge.breathing_rate_avg') AS respiration,
           max(o.value) FILTER (WHERE m.code = 'activity.steps')           AS pas,
           max(o.value) FILTER (WHERE m.code = 'activity.distance')        AS distance_m,
           max(o.value) FILTER (WHERE m.code = 'activity.calories')        AS calories,
           max(o.value) FILTER (WHERE m.code = 'activity.active_calories') AS calories_actives,
           max(o.value) FILTER (WHERE m.code = 'activity.active_duration') / 60 AS actif_min,
           max(o.value) FILTER (WHERE m.code = 'activity.goal_completion') AS objectif_atteint,
           max(o.value) FILTER (WHERE m.code = 'cardio.load')              AS charge_cardio,
           max(o.value) FILTER (WHERE m.code = 'cardio.strain')            AS contrainte,
           max(o.value) FILTER (WHERE m.code = 'cardio.tolerance')         AS tolerance
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.granularity IN ('daily', 'nightly')
    GROUP BY 1
),
-- Temps 1 : une valeur par MINUTE. C'est cette etape qui corrige la
-- ponderation - apres elle, une minute d'effort et une minute de repos
-- pesent pareil, quel que soit le nombre de releves qu'elles contiennent.
fc_minutes AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           date_trunc('minute', o.observed_at)              AS minute,
           avg(o.value)   AS bpm,
           min(o.value)   AS bpm_min,
           max(o.value)   AS bpm_max,
           count(*)       AS releves
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.code = 'heart_rate'
    GROUP BY 1, 2
),
-- Temps 2 : agreger les minutes, pas les releves.
fc AS (
    SELECT jour,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY bpm) AS fc_mediane,
           avg(bpm)        AS fc_moyenne,
           -- min/max restent pris sur les releves BRUTS : un extreme est
           -- un extreme, le moyenner par minute l'effacerait.
           min(bpm_min)    AS fc_min,
           max(bpm_max)    AS fc_max,
           sum(releves)    AS fc_mesures,
           count(*)        AS fc_minutes_couvertes
    FROM fc_minutes
    GROUP BY 1
)
SELECT COALESCE(j.jour, f.jour) AS jour,
       j.sommeil_score, j.sommeil_profond_min, j.sommeil_rem_min,
       j.sommeil_leger_min, j.hrv, j.fc_nocturne, j.respiration,
       f.fc_mediane, f.fc_moyenne, f.fc_min, f.fc_max,
       f.fc_mesures, f.fc_minutes_couvertes,
       j.pas, j.distance_m, j.calories, j.calories_actives, j.actif_min,
       j.objectif_atteint, j.charge_cardio, j.contrainte, j.tolerance
FROM jours j
FULL OUTER JOIN fc f ON f.jour = j.jour
ORDER BY 1
"""

# ---------------------------------------------------------------------
# v_sleep - une ligne par nuit
#
# Croise deux tables qui parlent de la meme nuit sans partager de clef :
#   - episode(kind='sleep')  porte le debut, la fin, et le JSON brut ;
#   - observation            porte les grandeurs, horodatees a minuit
#                            local du jour de REVEIL.
#
# D'ou la jointure sur la date de FIN de l'episode ramenee en heure
# locale : c'est la seule facon de faire coincider une nuit commencee le
# 17 a 23h40 avec ses mesures datees du 18.
# ---------------------------------------------------------------------

V_SLEEP = f"""
CREATE VIEW v_sleep AS
WITH nuits AS (
    SELECT (e.ended_at AT TIME ZONE '{LOCAL_TZ}')::date AS nuit,
           e.started_at,
           e.ended_at,
           e.payload
    FROM episode e
    WHERE e.kind = 'sleep'
),
mesures AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS nuit,
           max(o.value) FILTER (WHERE m.code = 'sleep.score')                AS score,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_deep')  / 60  AS profond_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_rem')   / 60  AS rem_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_light') / 60  AS leger_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.duration_unknown') / 60 AS inconnu_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.interruptions_total') / 60 AS interruptions_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.continuity')           AS continuite,
           max(o.value) FILTER (WHERE m.code = 'sleep.cycles')               AS cycles,
           max(o.value) FILTER (WHERE m.code = 'sleep.charge')               AS charge,
           max(o.value) FILTER (WHERE m.code = 'sleep.rating')               AS ressenti,
           max(o.value) FILTER (WHERE m.code = 'sleep.goal') / 3600.0        AS objectif_h,
           max(o.value) FILTER (WHERE m.code = 'sleep.score_duration')       AS score_duree,
           max(o.value) FILTER (WHERE m.code = 'sleep.score_solidity')       AS score_solidite,
           max(o.value) FILTER (WHERE m.code = 'sleep.score_regeneration')   AS score_regeneration,
           max(o.value) FILTER (WHERE m.code = 'recharge.hrv_avg')           AS hrv,
           max(o.value) FILTER (WHERE m.code = 'recharge.heart_rate_avg')    AS fc_moyenne,
           max(o.value) FILTER (WHERE m.code = 'recharge.breathing_rate_avg') AS respiration
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.granularity = 'nightly'
    GROUP BY 1
),
-- Les echantillons de la nuit, resumes. Ils restent dans observation :
-- on n'en tire ici qu'un comptage et les extremes.
echantillons AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS nuit_approx,
           count(*) FILTER (WHERE m.code = 'sleep.heart_rate') AS fc_mesures,
           min(o.value) FILTER (WHERE m.code = 'sleep.heart_rate') AS fc_min,
           max(o.value) FILTER (WHERE m.code = 'sleep.heart_rate') AS fc_max
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.code = 'sleep.heart_rate'
    GROUP BY 1
)
SELECT n.nuit,
       -- Deux rendus du meme instant : l'heure d'horloge locale, lisible
       -- ("je me couche a 22h03"), et l'instant absolu, calculable. psql
       -- et pgAdmin affichent un timestamptz dans le fuseau de la SESSION
       -- (UTC par defaut), d'ou l'interet d'exposer le local explicitement.
       (n.started_at AT TIME ZONE '{LOCAL_TZ}')::time(0) AS heure_coucher,
       (n.ended_at   AT TIME ZONE '{LOCAL_TZ}')::time(0) AS heure_lever,
       n.started_at AS coucher,
       n.ended_at   AS lever,
       round((EXTRACT(epoch FROM n.ended_at - n.started_at) / 3600)::numeric, 2) AS duree_h,
       m.score, m.continuite, m.cycles,
       m.profond_min, m.rem_min, m.leger_min, m.inconnu_min,
       m.interruptions_min,
       m.score_duree, m.score_solidite, m.score_regeneration,
       m.hrv, m.fc_moyenne, m.respiration,
       e.fc_min, e.fc_max, e.fc_mesures,
       m.objectif_h, m.charge, m.ressenti,
       -- Ce que le mapper n'extrait pas reste accessible dans le JSON.
       n.payload ->> 'device_id' AS montre
FROM nuits n
LEFT JOIN mesures m ON m.nuit = n.nuit
LEFT JOIN echantillons e ON e.nuit_approx = n.nuit
ORDER BY n.nuit DESC
"""


# ---------------------------------------------------------------------
# v_nutrition - une ligne par jour d'alimentation  (V4)
#
# Le total journalier n'est stocke NULLE PART : il est recalcule ici a
# partir des observations de chaque repas. C'est deliberé - stocker un
# total creerait une seconde verite, qui divergerait de la premiere des
# la premiere correction du journal.
#
# `repas_estimes` est la colonne de qualite : elle compte les repas dont
# au moins un aliment a ete pese "a la louche" (1 banane = 120 g). Un
# jour a 2500 kcal dont 4 repas sur 5 estimes ne vaut pas un jour a
# 2500 kcal tous peses, et rien d'autre ne le dirait.
# ---------------------------------------------------------------------

V_NUTRITION = f"""
CREATE VIEW v_nutrition AS
WITH par_jour AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           sum(o.value) FILTER (WHERE m.code = 'food.energy_kcal') AS kcal,
           sum(o.value) FILTER (WHERE m.code = 'food.protein_g')   AS proteines_g,
           sum(o.value) FILTER (WHERE m.code = 'food.carb_g')      AS glucides_g,
           sum(o.value) FILTER (WHERE m.code = 'food.sugar_g')     AS sucres_g,
           sum(o.value) FILTER (WHERE m.code = 'food.fat_g')       AS lipides_g,
           sum(o.value) FILTER (WHERE m.code = 'food.satfat_g')    AS ag_satures_g,
           sum(o.value) FILTER (WHERE m.code = 'food.fiber_g')     AS fibres_g,
           sum(o.value) FILTER (WHERE m.code = 'food.salt_g')      AS sel_g,
           sum(o.value) FILTER (WHERE m.code = 'food.mass_g')      AS masse_g
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.code LIKE 'food.%'
    GROUP BY 1
),
repas AS (
    SELECT (e.started_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           count(*)                                        AS nb_repas,
           count(*) FILTER (WHERE (e.payload ->> 'poids_estime')::boolean)
                                                           AS repas_estimes,
           -- L'heure du dernier repas : c'est LA variable qui interesse
           -- le sommeil. Manger a 22h30 et manger a 19h ne produisent
           -- pas la meme nuit, a calories identiques.
           max((e.started_at AT TIME ZONE '{LOCAL_TZ}')::time(0)) AS dernier_repas,
           min((e.started_at AT TIME ZONE '{LOCAL_TZ}')::time(0)) AS premier_repas
    FROM episode e
    WHERE e.kind = 'meal'
    GROUP BY 1
)
SELECT j.jour,
       round(j.kcal::numeric, 0)        AS kcal,
       round(j.proteines_g::numeric, 1) AS proteines_g,
       round(j.glucides_g::numeric, 1)  AS glucides_g,
       round(j.sucres_g::numeric, 1)    AS sucres_g,
       round(j.lipides_g::numeric, 1)   AS lipides_g,
       round(j.ag_satures_g::numeric, 1) AS ag_satures_g,
       round(j.fibres_g::numeric, 1)    AS fibres_g,
       round(j.sel_g::numeric, 2)       AS sel_g,
       round(j.masse_g::numeric, 0)     AS masse_g,
       r.nb_repas, r.repas_estimes,
       r.premier_repas, r.dernier_repas,
       -- Fenetre alimentaire : la duree entre premiere et derniere
       -- prise. Interessante en soi (jeune intermittent), et surtout
       -- non deductible des totaux.
       r.dernier_repas - r.premier_repas AS fenetre_alimentaire
FROM par_jour j
LEFT JOIN repas r ON r.jour = j.jour
ORDER BY j.jour DESC
"""


# ---------------------------------------------------------------------
# v_chambre - l'environnement de chaque nuit  (V4)
#
# LA vue de la V4 : elle croise deux sources qui ne se connaissent pas.
# Le capteur ignore tout du sommeil, la montre ignore tout de la
# chambre, et rien dans le schema ne les relie - ni cle etrangere, ni
# colonne commune. Seul le TEMPS les rapproche.
#
# La jointure se fait donc sur un INTERVALLE et non sur une egalite :
# une mesure de temperature appartient a une nuit si son instant tombe
# entre le coucher et le lever. C'est ce que la V1 avait prevu en
# separant `episode` (un intervalle) de `observation` (un point) - ici
# la separation paie.
#
# Attention a la lecture : ces colonnes decrivent la chambre PENDANT la
# nuit, pas avant. Une chambre chaude au coucher qui refroidit ensuite
# donne la meme moyenne qu'une chambre tiede tout du long. D'ou temp_min
# et temp_max a cote de la moyenne, et temp_debut / temp_fin qui disent
# le sens de la derive.
# ---------------------------------------------------------------------

V_CHAMBRE = f"""
CREATE VIEW v_chambre AS
WITH nuits AS (
    SELECT (e.ended_at AT TIME ZONE '{LOCAL_TZ}')::date AS nuit,
           e.started_at,
           e.ended_at
    FROM episode e
    WHERE e.kind = 'sleep'
),
mesures AS (
    SELECT n.nuit,
           o.observed_at,
           m.code,
           o.value
    FROM nuits n
    JOIN observation o
      ON o.observed_at >= n.started_at
     AND o.observed_at <= n.ended_at
    JOIN metric m ON m.id = o.metric_id
    WHERE m.code LIKE 'bedroom.%'
),
resume AS (
    SELECT nuit,
           avg(value) FILTER (WHERE code = 'bedroom.temperature') AS temp_moy,
           min(value) FILTER (WHERE code = 'bedroom.temperature') AS temp_min,
           max(value) FILTER (WHERE code = 'bedroom.temperature') AS temp_max,
           avg(value) FILTER (WHERE code = 'bedroom.humidity')    AS hum_moy,
           min(value) FILTER (WHERE code = 'bedroom.humidity')    AS hum_min,
           max(value) FILTER (WHERE code = 'bedroom.humidity')    AS hum_max,
           count(*) FILTER (WHERE code = 'bedroom.temperature')   AS mesures
    FROM mesures
    GROUP BY 1
),
-- Premiere et derniere temperature de la nuit. DISTINCT ON garde une
-- seule ligne par nuit, celle qui arrive en tete du ORDER BY : c'est la
-- facon idiomatique en PostgreSQL de prendre "le premier de chaque
-- groupe" sans fenetrage.
bornes AS (
    SELECT DISTINCT ON (nuit) nuit, value AS temp_debut
    FROM mesures WHERE code = 'bedroom.temperature'
    ORDER BY nuit, observed_at ASC
),
fins AS (
    SELECT DISTINCT ON (nuit) nuit, value AS temp_fin
    FROM mesures WHERE code = 'bedroom.temperature'
    ORDER BY nuit, observed_at DESC
)
SELECT n.nuit,
       round(r.temp_moy::numeric, 2) AS temp_moy,
       round(r.temp_min::numeric, 2) AS temp_min,
       round(r.temp_max::numeric, 2) AS temp_max,
       b.temp_debut,
       f.temp_fin,
       round((f.temp_fin - b.temp_debut)::numeric, 2) AS temp_derive,
       round(r.hum_moy::numeric, 1)  AS hum_moy,
       round(r.hum_min::numeric, 1)  AS hum_min,
       round(r.hum_max::numeric, 1)  AS hum_max,
       r.mesures,
       -- Part de la nuit reellement mesuree. Sans elle, une moyenne
       -- calculee sur 3 releves ressemble a une moyenne calculee sur
       -- 100 - le meme piege que fc_minutes_couvertes en V3.
       round((r.mesures * 5.0 * 60
              / NULLIF(EXTRACT(epoch FROM n.ended_at - n.started_at), 0)
              * 100)::numeric, 0) AS couverture_pct
FROM nuits n
LEFT JOIN resume r ON r.nuit = n.nuit
LEFT JOIN bornes b ON b.nuit = n.nuit
LEFT JOIN fins   f ON f.nuit = n.nuit
ORDER BY n.nuit DESC
"""


# ---------------------------------------------------------------------
# v_lecture - une ligne par jour de lecture  (Kindle)
#
# Deux agregats qui ne se comptent PAS de la meme facon, et c'est le
# point interessant :
#
#   les sessions portent leurs chiffres dans `observation`, ancres sur
#   l'instant de DEBUT de session - donc regroupables par date ;
#
#   les annotations n'ont aucune observation. Les compter demande un
#   count(*) sur `episode`. C'est volontaire : ecrire une observation
#   valant 1 par surlignement les ferait s'ecraser entre eux des que
#   deux tombent au meme instant, sous la contrainte d'unicite
#   (source, metric, observed_at).
#
# La date est celle du fuseau local. La Kindle horodate en epoch, donc
# en UTC : sans conversion, une session du soir bascule sur le lendemain.
# Meme piege qu'en V2 dans health.py et en V3 dans load.py.
# ---------------------------------------------------------------------

V_LECTURE = f"""
CREATE VIEW v_lecture AS
WITH mesures AS (
    SELECT (o.observed_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           o.observed_at,
           m.code,
           o.value
    FROM observation o
    JOIN metric m ON m.id = o.metric_id
    WHERE m.code LIKE 'reading.%'
),
par_jour AS (
    SELECT jour,
           count(DISTINCT observed_at) FILTER (
               WHERE code = 'reading.duration_s')            AS sessions,
           sum(value) FILTER (WHERE code = 'reading.duration_s') AS duree_s,
           sum(value) FILTER (WHERE code = 'reading.active_s')   AS actif_s,
           sum(value) FILTER (WHERE code = 'reading.words')      AS mots,
           sum(value) FILTER (WHERE code = 'reading.page_turns') AS pages
    FROM mesures
    GROUP BY jour
),
livres AS (
    SELECT (e.started_at AT TIME ZONE '{LOCAL_TZ}')::date  AS jour,
           count(DISTINCT e.payload->>'asin')              AS livres,
           string_agg(DISTINCT e.payload->>'titre', ' | ') AS titres
    FROM episode e
    WHERE e.kind = 'reading_session'
    GROUP BY 1
),
annotations AS (
    SELECT (e.started_at AT TIME ZONE '{LOCAL_TZ}')::date AS jour,
           count(*) FILTER (WHERE e.kind = 'highlight')    AS surlignements,
           count(*) FILTER (WHERE e.kind = 'note')         AS notes,
           count(*) FILTER (WHERE e.kind = 'bookmark')     AS marque_pages,
           count(*) FILTER (WHERE e.kind = 'word_lookup')  AS mots_cherches
    FROM episode e
    WHERE e.kind IN ('highlight', 'note', 'bookmark', 'word_lookup')
    GROUP BY 1
),
jours AS (
    SELECT jour FROM par_jour
    UNION SELECT jour FROM livres
    UNION SELECT jour FROM annotations
)
SELECT j.jour,
       COALESCE(p.sessions, 0)                AS sessions,
       round((p.duree_s / 60.0)::numeric, 1)  AS minutes,
       round((p.actif_s / 60.0)::numeric, 1)  AS minutes_actives,
       p.mots::int                            AS mots,
       p.pages::int                           AS pages_tournees,
       COALESCE(l.livres, 0)                  AS livres,
       l.titres,
       COALESCE(a.surlignements, 0)           AS surlignements,
       COALESCE(a.notes, 0)                   AS notes,
       COALESCE(a.marque_pages, 0)            AS marque_pages,
       COALESCE(a.mots_cherches, 0)           AS mots_cherches
FROM jours j
LEFT JOIN par_jour    p ON p.jour = j.jour
LEFT JOIN livres      l ON l.jour = j.jour
LEFT JOIN annotations a ON a.jour = j.jour
ORDER BY j.jour DESC
"""


# ---------------------------------------------------------------------
# v_livres - une ligne par livre  (Kindle)
#
# La progression vient de `profile_snapshot`, jamais des sessions : les
# bornes declarees par une session decrivent le contenu ouvert, pas le
# chemin parcouru (elles valent 1 -> max quelle que soit la duree).
# Voir kindle/mapper.py, section "LA PROGRESSION NE VIENT PAS DES
# SESSIONS".
#
# DISTINCT ON garde, pour chaque livre, le dernier etat connu. Les etats
# precedents restent en base : l'historique de progression est deja la,
# gratuitement, grace au dedoublonnage par empreinte de contenu.
# ---------------------------------------------------------------------

V_LIVRES = f"""
CREATE VIEW v_livres AS
WITH etat AS (
    SELECT DISTINCT ON (s.payload->>'asin')
           s.payload->>'asin'                        AS asin,
           s.payload->>'titre'                       AS titre,
           s.payload->>'auteur'                      AS auteur,
           (s.payload->>'position')::bigint          AS position,
           (s.payload->>'position_max')::bigint      AS position_max,
           (s.payload->>'progression_pct')::numeric  AS progression_pct,
           s.captured_at
    FROM profile_snapshot s
    WHERE s.kind = 'book_progress'
    ORDER BY s.payload->>'asin', s.captured_at DESC
),
lecture AS (
    SELECT e.payload->>'asin'                                  AS asin,
           count(*)                                            AS sessions,
           min(e.started_at)                                   AS premiere,
           max(e.started_at)                                   AS derniere,
           sum(EXTRACT(epoch FROM e.ended_at - e.started_at))  AS duree_s
    FROM episode e
    WHERE e.kind = 'reading_session'
    GROUP BY 1
),
annot AS (
    SELECT e.payload->>'asin'                           AS asin,
           count(*) FILTER (WHERE e.kind = 'highlight') AS surlignements,
           count(*) FILTER (WHERE e.kind = 'note')      AS notes,
           count(*) FILTER (WHERE e.kind = 'bookmark')  AS marque_pages,
           count(*) FILTER (WHERE e.kind = 'word_lookup') AS mots_cherches
    FROM episode e
    WHERE e.kind IN ('highlight', 'note', 'bookmark', 'word_lookup')
    GROUP BY 1
),
livres AS (
    SELECT asin FROM etat
    UNION SELECT asin FROM lecture
    UNION SELECT asin FROM annot
)
SELECT b.asin,
       COALESCE(e.titre, a2.titre)               AS titre,
       e.auteur,
       e.position,
       e.position_max,
       e.progression_pct,
       COALESCE(l.sessions, 0)                   AS sessions,
       round((l.duree_s / 60.0)::numeric, 1)     AS minutes,
       COALESCE(an.surlignements, 0)             AS surlignements,
       COALESCE(an.notes, 0)                     AS notes,
       COALESCE(an.marque_pages, 0)              AS marque_pages,
       COALESCE(an.mots_cherches, 0)             AS mots_cherches,
       (l.derniere AT TIME ZONE '{LOCAL_TZ}')::date AS derniere_lecture
FROM livres b
LEFT JOIN etat    e  ON e.asin  = b.asin
LEFT JOIN lecture l  ON l.asin  = b.asin
LEFT JOIN annot   an ON an.asin = b.asin
-- Repli de titre : un livre supprime de la liseuse garde ses
-- annotations, donc son titre n'existe plus que dans leur payload.
LEFT JOIN LATERAL (
    SELECT ep.payload->>'titre' AS titre
    FROM episode ep
    WHERE ep.payload->>'asin' = b.asin
      AND ep.payload->>'titre' IS NOT NULL
    LIMIT 1
) a2 ON true
ORDER BY l.derniere DESC NULLS LAST
"""


ALL_VIEWS = {"v_daily": V_DAILY, "v_sleep": V_SLEEP,
             "v_nutrition": V_NUTRITION, "v_chambre": V_CHAMBRE,
             "v_lecture": V_LECTURE, "v_livres": V_LIVRES}


def create_views(connection) -> list[str]:
    """(Re)cree toutes les vues. Renvoie les noms crees.

    DROP puis CREATE, et non CREATE OR REPLACE : ce dernier ne sait
    qu'AJOUTER des colonnes a la fin. Renommer ou reordonner echoue sur
        cannot change name of view column "x" to "y"
    Or une vue ne contient aucune donnee : la detruire ne coute rien.
    C'est ce qui rend le rejeu d'initdb reellement idempotent, quelle que
    soit l'evolution de la definition.
    """
    drop_views(connection)

    for definition in ALL_VIEWS.values():
        connection.execute(text(definition))

    return list(ALL_VIEWS)


def drop_views(connection) -> None:
    """Supprime les vues. Appele avant drop_tables : une vue depend de ses
    tables, PostgreSQL refuserait de supprimer une table encore utilisee.
    """
    for nom in ALL_VIEWS:
        connection.execute(text(f"DROP VIEW IF EXISTS {nom} CASCADE"))
