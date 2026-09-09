# Schéma de la base — Chronicle V1

> Étape 4 de la V1. À lire **avant** `database/models.py` : le code n'est
> que la traduction de ce document.

## Le problème à résoudre

La tentation naturelle, en regardant l'API Polar, est de créer une table par
endpoint : `sleep`, `nightly_recharge`, `cardio_load`, `activities`…
C'est un piège, pour trois raisons :

1. **Ça ne passe pas à l'échelle.** V4 prévoit balance connectée, alimentation,
   capteurs, temps d'écran. Une table par source = 40 tables dans deux ans,
   et une requête « montre-moi ma fréquence cardiaque et mon poids sur le même
   axe » devient un `JOIN` à rallonge, différent pour chaque paire de sources.
2. **Ça couple le schéma à un fournisseur.** Si Polar change ses champs, ou si
   la montre est remplacée par une Garmin, le schéma est à refaire.
3. **Ça empêche la question centrale du projet.** Chronicle ne sert pas
   à afficher Polar : il sert à corréler des mesures d'origines différentes.
   Le schéma doit rendre cette corrélation *facile*, pas possible.

**Règle retenue : une source de données ≠ une table.**
Ce qui structure la base, ce n'est pas *d'où vient* la donnée, c'est *quelle
forme* elle a dans le temps.

## Les trois formes temporelles

En observant les données réelles de Polar, tout se range dans trois moules :

| Forme | Question | Exemples |
|---|---|---|
| **Point** | « combien, à cet instant ? » | FC à 07:32:15, poids du 16/08, score de sommeil de la nuit, pas de la minute 22:56 |
| **Intervalle** | « qu'est-ce qui a duré, de quand à quand ? » | une nuit, une séance, une zone d'activité, une période d'inactivité |
| **État** | « qu'est-ce qui était vrai à ce moment-là ? » | profil : taille, VO₂max, seuils, date de naissance |

D'où **trois tables porteuses de données** : `observation`, `episode`,
`profile_snapshot`. Plus trois tables de support : `source`, `metric`,
`raw_payload`.

## Les six tables

```
  source ──┬──< observation >── metric
           ├──< episode
           ├──< profile_snapshot
           └──< raw_payload
```

### `source` — d'où vient la donnée

Une ligne par connecteur : `polar_accesslink`, plus tard `withings_scale`,
`myfitnesspal`, `screen_time`.

| colonne | type | rôle |
|---|---|---|
| `id` | `smallint` PK | clé technique |
| `code` | `text` UNIQUE | `polar_accesslink` — identifiant stable, écrit par le code |
| `label` | `text` | nom lisible |
| `created_at` | `timestamptz` | |

Toutes les autres tables portent `source_id`. C'est ce qui permet de dire
« ce poids vient de la balance, celui-là est déclaratif » — et de supprimer
proprement toute une source (`ON DELETE CASCADE`).

### `metric` — le catalogue des grandeurs mesurables

Une ligne par grandeur, **pas** par source. `heart_rate` est une seule
métrique, que la mesure vienne de la Polar ou d'un autre capteur.

| colonne | type | rôle |
|---|---|---|
| `id` | `smallint` PK | |
| `code` | `text` UNIQUE | `sleep.score`, `heart_rate`, `weight` |
| `unit` | `text` | `bpm`, `kg`, `s`, `count`, `%` — sans unité un nombre ne veut rien dire |
| `granularity` | `text` | `instant` / `daily` / `nightly` / `period` |
| `description` | `text` | |

`granularity` est sur la métrique et pas sur l'observation : `cardio_load`
est *toujours* journalier, `heart_rate` est *toujours* instantané. Le mettre
sur chaque ligne dupliquerait la même valeur des millions de fois.

### `observation` — le cœur du modèle

Table **étroite et longue** (*narrow table*) : une ligne = une mesure.

| colonne | type | rôle |
|---|---|---|
| `id` | `bigint` PK | `bigint` : on dépassera les 2,1 milliards de lignes d'un `int` |
| `source_id` | `smallint` FK | |
| `metric_id` | `smallint` FK | |
| `observed_at` | `timestamptz` | **quand la mesure a eu lieu** |
| `value` | `double precision` | la valeur, toujours numérique |
| `ingested_at` | `timestamptz` | quand *nous* l'avons écrite (≠ `observed_at`) |

`UNIQUE (source_id, metric_id, observed_at)` ← **c'est la clé de l'idempotence**
(étape 7). La même mesure rejouée dix fois occupe une ligne.

Pourquoi une table étroite plutôt que des colonnes `steps`, `hr`, `poids` ?

- ajouter une métrique = insérer une ligne dans `metric`, **zéro migration** ;
- pas de colonnes majoritairement `NULL` ;
- une seule requête répond pour n'importe quelle grandeur ;
- c'est la forme qu'attendent pandas (`pivot`) et les modèles de la V5.

Le prix à payer : un `JOIN` sur `metric` pour lire un code, et des valeurs
toutes forcées en `double precision`. Assumé.

### `episode` — ce qui a une durée

| colonne | type | rôle |
|---|---|---|
| `id` | `bigint` PK | |
| `source_id` | `smallint` FK | |
| `kind` | `text` | `sleep`, `exercise`, `activity_period`, `activity_zone`, `alertness_hour`… |
| `started_at` | `timestamptz` NOT NULL | |
| `ended_at` | `timestamptz` NULL | null = épisode ouvert / fin inconnue |
| `payload` | `jsonb` | tous les champs bruts de l'épisode |

`UNIQUE (source_id, kind, started_at)` → idempotence.

**Pourquoi du `jsonb` ici et pas partout ?** Parce qu'un épisode transporte des
attributs qualitatifs propres à sa source (`device_id`, `sleep_rating`,
`continuity_class`, `zone: SEDENTARY`) qu'on ne veut ni perdre, ni figer en
colonnes. Les grandeurs *comparables entre sources* sont extraites dans
`observation` ; le reste dort dans `payload`, interrogeable au besoin
(`payload->>'device_id'`) et indexable via GIN plus tard.

C'est le compromis assumé du schéma : **structuré là où on veut comparer,
semi-structuré là où on veut simplement ne rien perdre.**

### `profile_snapshot` — ce qui change lentement

Poids, taille, VO₂max, seuils, objectif de sommeil : ni un point de mesure,
ni un intervalle. Ce sont des **états** qui restent vrais jusqu'au changement
suivant (une *slowly changing dimension*).

| colonne | type | rôle |
|---|---|---|
| `id` | `int` PK | |
| `source_id` | `smallint` FK | |
| `kind` | `text` | `polar_account`, `polar_physical` |
| `captured_at` | `timestamptz` | date du champ `modified` quand il existe, sinon date de collecte |
| `content_hash` | `char(64)` | SHA-256 du payload |
| `payload` | `jsonb` | |

`UNIQUE (source_id, kind, content_hash)` — dédoublonnage **par contenu**, pas
par date. Collecter 300 fois un profil inchangé crée **une** ligne ; le jour
où le poids change, une deuxième apparaît. On obtient gratuitement l'historique
des changements, sans savoir à l'avance quels champs surveiller.

Les grandeurs numériques du profil (poids, VO₂max…) sont **aussi** écrites dans
`observation`, horodatées avec `modified`. Le snapshot conserve le contexte,
l'observation rend la valeur comparable dans le temps.

### `raw_payload` — le registre de collecte

Une ligne par réponse d'API archivée dans `data/raw/`.

| colonne | type | rôle |
|---|---|---|
| `id` | `bigint` PK | |
| `source_id` | `smallint` FK | |
| `endpoint` | `text` | `/users/sleep` |
| `params` | `jsonb` | sans eux, un fichier `continuous-heart-rate` ne dit pas quelle plage il couvre |
| `fetched_at` | `timestamptz` | |
| `content_hash` | `char(64)` UNIQUE | SHA-256 du corps |
| `payload` | `jsonb` | la réponse intacte |

Rôle : **traçabilité et rejouabilité**. Si un parseur est bogué (cf. les deux
bugs `vo2_max` / objet-vs-liste), on corrige le code et on rejoue depuis la
base, sans redemander à Polar — qui aura effacé au bout de 28 jours.

## Décisions de conception, et pourquoi

**`timestamptz` partout, jamais `timestamp`.** `timestamptz` stocke un instant
absolu (UTC) et le rend dans le fuseau de la session. `timestamp` stocke un
texte d'horloge : deux mesures prises à la même seconde à Paris et à Tokyo y
seraient indistinguables. Corollaire : Polar livre beaucoup d'horodatages
*sans* fuseau (`2026-08-16T22:56:30`) — heure locale de la montre. Le code les
localise avec `LOCAL_TZ` (`config.py`) avant insertion. C'est une hypothèse,
pas une vérité : un vol Paris→Tokyo la met en défaut. Elle est explicite.

**`double precision` plutôt que `numeric`.** `numeric` est exact mais lent ;
il sert à l'argent. Des mesures physiques déjà bruitées se contentent d'un
flottant, et les bibliothèques de calcul de la V5 travaillent en `float64`.

**Les durées en secondes (`int`), pas en `interval`.** Polar envoie
`PT13M`, `PT8H`. Converties en secondes, elles se moyennent et se tracent
directement. `metric.unit = 's'` porte l'information.

**Les valeurs sentinelles ne sont pas stockées.** `cardio_load: -1.0` avec
`cardio_load_status: LOAD_STATUS_NOT_AVAILABLE` ne signifie pas « charge de
-1 » mais « pas de donnée ». Insérer -1 empoisonnerait toute moyenne. Ces
lignes sont écartées à l'ingestion ; le `status` reste dans le `payload`.

**Pas de table `person`.** Une seule personne, un seul jumeau. Ajouter un
`person_id` partout serait de l'anticipation gratuite (règle du projet :
une version à la fois).

**Pas de FK `episode_id` sur `observation`.** Rattacher chaque échantillon de
FC nocturne à sa nuit serait élégant mais force à insérer les épisodes avant
les observations et à relire leurs `id`. Repoussé ; le rapprochement se fait
très bien par intervalle de temps :
`WHERE observed_at BETWEEN episode.started_at AND episode.ended_at`.

## Index

Au-delà des index créés automatiquement par les PK et les contraintes UNIQUE :

| index | pourquoi |
|---|---|
| `observation (metric_id, observed_at DESC)` | la requête de base : « la série X sur la période Y ». L'ordre des colonnes compte : filtrer d'abord sur la métrique, puis balayer le temps |
| `observation (observed_at)` | « tout ce qui s'est passé ce jour-là », toutes métriques confondues |
| `episode (kind, started_at DESC)` | « mes 10 dernières nuits » |
| `episode (started_at, ended_at)` | recouvrement temporel avec les observations |

Un index accélère la lecture et ralentit l'écriture (il faut le tenir à jour).
Quatre index sur des tables d'insertion en lot, c'est le bon équilibre à ce
stade. On n'indexe pas « au cas où ».

## Ce que ça donne à l'usage

```sql
-- Score de sommeil des 30 derniers jours
SELECT o.observed_at::date, o.value
FROM observation o JOIN metric m ON m.id = o.metric_id
WHERE m.code = 'sleep.score' AND o.observed_at > now() - interval '30 days'
ORDER BY 1;

-- Corréler HRV nocturne et pas de la veille : deux métriques, une requête,
-- et rien à changer le jour ou les pas viendront d'une autre source.
SELECT d.jour, hrv.value AS hrv, pas.value AS pas
FROM generate_series(...) AS d(jour)
LEFT JOIN ... ;
```

Le point à retenir : **ajouter la balance connectée en V4 ne demandera aucune
migration.** Une ligne dans `source`, une ligne dans `metric`, un mapper qui
produit des `observation` — le schéma ne bouge pas.

## Les vues de lecture (`database/views.py`)

Le modèle ci-dessus est optimisé pour **écrire** : étroit, sans redondance,
extensible. Il se lit mal — corréler HRV et pas demandait 14 lignes de
sous-requêtes imbriquées. D'où deux vues.

> **On normalise à l'écriture, on dénormalise à la lecture.**
> Les tables restent la vérité ; les vues sont des commodités. Une vue ne
> stocke rien : elle est recalculée à chaque appel, donc ne peut pas se
> désynchroniser. On peut en ajouter dix sans toucher au modèle.

### `v_daily` — une ligne par jour

Pivot des métriques `daily` et `nightly` : une colonne par grandeur.

```sql
SELECT jour, hrv, lag(pas) OVER (ORDER BY jour) AS pas_veille
FROM v_daily ORDER BY jour;
```

Deux lignes au lieu de quatorze.

#### La FC : agrégée en deux temps, et pourquoi

La première version de cette vue prenait la **médiane des échantillons bruts**,
en la croyant robuste. Elle avait tort, et l'erreur valait 32 bpm :

| journée du 2026-08-21 | valeur |
|---|---|
| médiane des 9 175 relevés bruts | **108 bpm** |
| médiane des 412 moyennes par minute | **75,5 bpm** |

La FC continue de la Pacer est échantillonnée par rafales — 1 Hz en activité, un
relevé toutes les 5 min au repos (vérifié : 26 523 intervalles sous 10 s contre
187 au-dessus de 5 min). Une heure d'effort pèse donc 3 600 lignes contre 12 pour
une heure de calme.

> **Une médiane protège des valeurs aberrantes, pas d'un échantillonnage
> irrégulier.** Les rafales ne sont pas des points faux qu'on écarte : elles
> *sont* la majorité des lignes. Aucune statistique de position ne répare un
> problème de pondération.

D'où l'agrégation en deux temps (CTE `fc_minutes` puis `fc`) : ramener d'abord
chaque minute à une valeur, puis agréger les minutes. Une minute d'effort pèse
alors autant qu'une minute de repos.

`fc_min` / `fc_max` restent pris sur les relevés bruts — un extrême est un
extrême, le moyenner par minute l'effacerait.

La colonne `fc_minutes_couvertes` accompagne le tout : sur la journée la mieux
mesurée, **412 minutes sur 1 440**. C'est ce chiffre qu'il faut regarder avant de
parler de « la FC de la journée ».

⚠️ `fc_mesures` et `fc_minutes_couvertes` décrivent la **mesure**, pas le mesuré.
Elles corrèlent mécaniquement avec l'activité (on porte sa montre quand on
bouge) : `analytics/correlate.py` les exclut d'office.

### `v_sleep` — une ligne par nuit

Croise `episode(kind='sleep')` — début, fin, JSON brut — et les 20 métriques
`nightly`. La jointure se fait sur **la date de fin ramenée en heure locale** :
c'est la seule façon de faire coïncider une nuit commencée le 17 à 23h40 avec
ses mesures datées du 18 (Polar date une nuit par son jour de réveil).

Elle expose aussi `heure_coucher` / `heure_lever` en heure d'horloge locale, en
plus des `timestamptz` : `psql` et pgAdmin affichent un `timestamptz` dans le
fuseau de la **session** (UTC par défaut), ce qui rend un coucher à 22h03
illisible.

### Détail d'implémentation : `DROP` puis `CREATE`, pas `CREATE OR REPLACE`

`CREATE OR REPLACE VIEW` ne sait qu'**ajouter** des colonnes à la fin. Renommer
ou réordonner échoue sur `cannot change name of view column "x" to "y"`. Comme
une vue ne contient aucune donnée, la détruire ne coûte rien — c'est ce qui rend
le rejeu de `initdb` réellement idempotent, quelle que soit l'évolution de la
définition.

Les vues sont créées par `initdb` et supprimées avant `drop_tables` : PostgreSQL
refuse de supprimer une table dont une vue dépend encore.
