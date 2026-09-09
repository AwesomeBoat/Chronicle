"""Analyse des donnees de lecture Kindle.

Ecrit AVANT d'avoir les donnees, volontairement. L'historique des
sessions se limite aujourd'hui a quelques heures - le tampon de la
liseuse est vide apres chaque envoi a Amazon - et l'export "Request My
Data" d'Amazon viendra le combler. L'outillage doit donc etre pret, et
surtout capable de se taire tant qu'il n'a rien a dire.

CE QUE CE MODULE REFUSE DE FAIRE
--------------------------------
La V3 a etabli le chiffre qui gouverne tout le projet : sur 2 000 paires
de bruit pur a n = 7, le |r| le plus fort obtenu par hasard vaut 0,96.
Une correlation sur une semaine n'est donc pas une correlation faible,
c'est une non-information.

Chaque fonction ici renvoie donc son `n` a cote de son resultat, et
`Constat` porte un niveau de confiance explicite. Un tableau de bord qui
affiche "vous lisez mieux apres une bonne nuit" sur cinq sessions serait
pire qu'un tableau vide.

LES TROIS ASYMETRIES A GARDER EN TETE
-------------------------------------
1. **Sessions vs annotations.** Les annotations ont 19 mois d'historique,
   les sessions quelques heures. Une tendance de surlignements est
   lisible aujourd'hui ; une tendance de temps de lecture, non.

2. **Telemetrie partielle.** Mots lus et tournes de page n'accompagnent
   pas toutes les sessions. Ils valent NaN, jamais 0 - et toute moyenne
   doit donc etre calculee sur les seules sessions qui les portent.

3. **Positions, pas pages.** Le KFX compte en positions. La vitesse de
   lecture se mesure en mots/minute quand la telemetrie est la, et en
   positions/minute sinon. Les deux ne sont pas convertibles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from analytics import correlate, load

# Seuils de confiance. Alignes sur la lecon de la V3 : en dessous de 15
# points, un coefficient ne se distingue pas du hasard ; entre 15 et 30
# il reste indicatif.
N_INSUFFISANT = 15
N_INDICATIF = 30

# Duree en dessous de laquelle une "session" est une ouverture, pas une
# lecture. La liseuse enregistre une session des qu'un livre s'ouvre,
# meme pour verifier une reference trois secondes.
DUREE_MINIMALE_MIN = 1.0


@dataclass(slots=True)
class Constat:
    """Un resultat, avec ce qu'il faut pour savoir s'il vaut quelque chose."""

    titre: str
    valeur: Any = None
    n: int = 0
    unite: str = ""
    note: str = ""

    @property
    def confiance(self) -> str:
        if self.n == 0:
            return "aucune donnee"
        if self.n < N_INSUFFISANT:
            return "insuffisant"
        if self.n < N_INDICATIF:
            return "indicatif"
        return "exploitable"

    @property
    def fiable(self) -> bool:
        return self.n >= N_INDICATIF

    def __str__(self) -> str:
        if self.valeur is None:
            return f"{self.titre} : -- ({self.confiance})"

        valeur = (f"{self.valeur:,.1f}".replace(",", " ")
                  if isinstance(self.valeur, float) else f"{self.valeur}")

        return (f"{self.titre} : {valeur} {self.unite}".rstrip()
                + f"  [n={self.n}, {self.confiance}]"
                + (f"  {self.note}" if self.note else ""))


@dataclass(slots=True)
class Rapport:
    """Tout ce qu'on sait, rassemble - y compris ce qu'on ne sait pas."""

    constats: list[Constat] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)

    def ajouter(self, constat: Constat) -> None:
        self.constats.append(constat)


# --------------------------------------------------------------- socle

def sessions_utiles(sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Les sessions de plus d'une minute.

    Ecarter les ouvertures fugaces n'est pas du nettoyage cosmetique :
    sur l'echantillon de depart, 2 sessions sur 5 duraient moins de
    30 secondes. Les inclure divise la duree moyenne par deux et rend
    toute vitesse de lecture absurde.
    """
    if sessions is None:
        sessions = load.sessions_lecture()

    if sessions.empty:
        return sessions

    return sessions[sessions["duree_min"] >= DUREE_MINIMALE_MIN].copy()


def resume() -> Rapport:
    """Les chiffres de tete, chacun avec son effectif."""
    rapport = Rapport()

    sessions = load.sessions_lecture()
    utiles = sessions_utiles(sessions)
    annot = load.annotations_lecture()
    quotidien = load.lecture()
    catalogue = load.livres()

    rapport.tables["sessions"] = sessions
    rapport.tables["annotations"] = annot
    rapport.tables["quotidien"] = quotidien
    rapport.tables["livres"] = catalogue

    # --- volume
    n_sessions = len(utiles)
    rapport.ajouter(Constat("Sessions de lecture", n_sessions, n_sessions,
                            note=f"({len(sessions) - n_sessions} ouvertures "
                                 f"< {DUREE_MINIMALE_MIN:g} min ecartees)"
                                 if len(sessions) > n_sessions else ""))

    if n_sessions:
        total = utiles["duree_min"].sum()
        rapport.ajouter(Constat("Temps de lecture total", total, n_sessions,
                                "min"))
        rapport.ajouter(Constat("Duree mediane d'une session",
                                float(utiles["duree_min"].median()),
                                n_sessions, "min"))

        jours = utiles["jour"].nunique()
        rapport.ajouter(Constat("Jours de lecture", jours, jours))

        if jours:
            rapport.ajouter(Constat("Moyenne par jour lu", total / jours,
                                    jours, "min"))

    # --- vitesse : seulement sur les sessions qui portent la telemetrie
    avec_mots = utiles[utiles["mots"].notna()] if n_sessions else utiles

    if len(avec_mots):
        vitesse = (avec_mots["mots"].sum()
                   / max(avec_mots["duree_min"].sum(), 1e-9))
        rapport.ajouter(Constat("Vitesse de lecture", float(vitesse),
                                len(avec_mots), "mots/min",
                                note="telemetrie presente sur "
                                     f"{len(avec_mots)}/{n_sessions} sessions"))
    else:
        rapport.ajouter(Constat("Vitesse de lecture", None, 0, "mots/min",
                                note="aucune session ne porte le compte de mots"))

    # --- annotations : la seule serie longue
    if not annot.empty:
        for genre, libelle in (("highlight", "Surlignements"),
                               ("note", "Notes"),
                               ("bookmark", "Marque-pages"),
                               ("word_lookup", "Mots cherches")):
            sous = annot[annot["genre"] == genre]
            rapport.ajouter(Constat(libelle, len(sous), len(sous)))

        etendue = (annot["started_at"].max() - annot["started_at"].min()).days
        rapport.ajouter(Constat("Historique des annotations", etendue,
                                len(annot), "jours"))

    if not catalogue.empty:
        rapport.ajouter(Constat("Livres touches", len(catalogue),
                                len(catalogue)))

    rapport.avertissements = avertissements(sessions, annot)

    return rapport


def avertissements(sessions: pd.DataFrame,
                   annot: pd.DataFrame) -> list[str]:
    """Ce qui empeche de croire les chiffres. A lire AVANT eux."""
    messages: list[str] = []

    if sessions.empty:
        messages.append(
            "Aucune session collectee. Le tampon de la liseuse est vide "
            "apres chaque envoi a Amazon : sans synchronisation reguliere, "
            "les sessions sont perdues definitivement.")
        return messages

    etendue = (sessions["started_at"].max() - sessions["started_at"].min())
    jours = max(etendue.days, 0) + 1

    if jours < N_INSUFFISANT:
        messages.append(
            f"Les sessions ne couvrent que {jours} jour(s). La V3 a montre "
            f"qu'a n = 7, un |r| de 0,96 s'obtient sur du bruit pur : aucune "
            f"correlation impliquant le temps de lecture n'est interpretable "
            f"pour l'instant.")

    sans_mots = sessions["mots"].isna().sum()

    if sans_mots:
        messages.append(
            f"{sans_mots}/{len(sessions)} sessions sans compte de mots. La "
            f"telemetrie fine n'accompagne pas toutes les sessions ; les "
            f"vitesses sont calculees sur les autres uniquement.")

    if not annot.empty:
        etendue_annot = (annot["started_at"].max()
                         - annot["started_at"].min()).days

        if etendue_annot > jours * 3:
            messages.append(
                f"Asymetrie normale : {etendue_annot} jours d'annotations "
                f"contre {jours} de sessions. Les annotations ont un "
                f"historique complet, pas les sessions.")

    courtes = (sessions["duree_min"] < DUREE_MINIMALE_MIN).sum()

    if courtes:
        messages.append(
            f"{courtes} session(s) de moins d'une minute : ce sont des "
            f"ouvertures de livre, pas des lectures. Ecartees des moyennes.")

    return messages


# ------------------------------------------------------------- rythmes

def profil_horaire(sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Minutes lues par heure de la journee, sur 24 lignes completes.

    Reindexe sur les 24 heures : sans cela, une heure sans lecture
    n'existe pas dans le resultat et le graphique la comble en reliant
    ses voisines. Meme raison que le reindex journalier de load.py.
    """
    sessions = sessions_utiles(sessions)

    if sessions.empty:
        return pd.DataFrame({"minutes": [], "sessions": []},
                            index=pd.Index([], name="heure"))

    profil = sessions.groupby("heure").agg(
        minutes=("duree_min", "sum"),
        sessions=("duree_min", "size"))

    return profil.reindex(range(24), fill_value=0).rename_axis("heure")


def profil_hebdomadaire(sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Minutes lues par jour de la semaine (lundi = 0)."""
    sessions = sessions_utiles(sessions)

    noms = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
            "dimanche"]

    if sessions.empty:
        return pd.DataFrame({"minutes": [0.0] * 7, "sessions": [0] * 7,
                             "jours_observes": [0] * 7},
                            index=pd.Index(noms, name="jour_semaine"))

    profil = sessions.groupby("jour_semaine").agg(
        minutes=("duree_min", "sum"),
        sessions=("duree_min", "size"),
        jours_observes=("jour", "nunique"))
    profil = profil.reindex(range(7), fill_value=0)
    profil.index = pd.Index(noms, name="jour_semaine")

    # Normaliser par le nombre de jours REELLEMENT observes : sans ca,
    # un lundi de plus dans la fenetre fait paraitre les lundis plus
    # studieux. Piege classique des profils hebdomadaires.
    profil["minutes_par_occurrence"] = (
        profil["minutes"] / profil["jours_observes"].replace(0, pd.NA))

    return profil


def carte_heures_jours(sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Matrice jour de la semaine x heure, en minutes. Pour la heatmap."""
    sessions = sessions_utiles(sessions)

    noms = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
            "dimanche"]
    vide = pd.DataFrame(0.0, index=noms, columns=range(24))

    if sessions.empty:
        return vide

    table = sessions.pivot_table(index="jour_semaine", columns="heure",
                                 values="duree_min", aggfunc="sum",
                                 fill_value=0.0)
    table = table.reindex(index=range(7), columns=range(24), fill_value=0.0)
    table.index = noms

    return table


def vitesse_par_session(sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """Mots par minute, session par session. Vide si aucune telemetrie."""
    sessions = sessions_utiles(sessions)

    if sessions.empty or sessions["mots"].isna().all():
        return sessions.iloc[0:0] if not sessions.empty else sessions

    avec = sessions[sessions["mots"].notna()].copy()
    avec["mots_par_min"] = avec["mots"] / avec["duree_min"]

    return avec


# ------------------------------------------------------------ par livre

def classement_livres(livres: pd.DataFrame | None = None) -> pd.DataFrame:
    """Les livres, tries par temps de lecture puis par annotations.

    Un livre peut avoir des annotations sans aucune session : ses
    surlignements datent d'avant la collecte. C'est le cas de la quasi
    totalite du catalogue aujourd'hui, et ce n'est pas une anomalie.
    """
    if livres is None:
        livres = load.livres()

    if livres.empty:
        return livres

    livres = livres.copy()
    livres["annotations"] = (livres[["surlignements", "notes",
                                     "marque_pages"]].sum(axis=1))

    return livres.sort_values(["minutes", "annotations"],
                              ascending=False, na_position="last")


def densite_annotation(livres: pd.DataFrame | None = None) -> pd.DataFrame:
    """Surlignements pour 10 000 positions : quels livres font reagir.

    Rapporter au nombre de positions est indispensable. Sans cela, un
    pave annote normalement bat toujours un court roman annote
    intensement - on classerait la longueur, pas l'interet.
    """
    if livres is None:
        livres = load.livres()

    if livres.empty:
        return livres

    mesurables = livres[livres["position_max"].notna()
                        & (livres["position_max"] > 0)].copy()

    if mesurables.empty:
        return mesurables

    mesurables["densite"] = (mesurables["surlignements"] * 10000.0
                             / mesurables["position_max"])

    return mesurables.sort_values("densite", ascending=False)


def progression(historique: pd.DataFrame | None = None) -> pd.DataFrame:
    """L'avancement dans chaque livre au fil des collectes.

    Une seule ligne par livre tant qu'aucune position n'a bouge : c'est
    le dedoublonnage par empreinte de contenu qui produit ce resultat,
    pas un filtre. La courbe se remplira d'elle-meme.
    """
    if historique is None:
        historique = load.progression_livres()

    return historique


# --------------------------------------------------------- annotations

def rythme_annotations(annot: pd.DataFrame | None = None,
                       frequence: str = "ME") -> pd.DataFrame:
    """Annotations par periode et par genre. La seule serie longue.

    'ME' = fin de mois. Sur 19 mois d'historique, le mois est le grain
    qui montre une tendance sans noyer le signal dans le bruit
    quotidien - la plupart des jours n'ont aucune annotation.
    """
    if annot is None:
        annot = load.annotations_lecture()

    if annot.empty:
        return pd.DataFrame()

    table = annot.copy()
    table["periode"] = table["started_at"].dt.tz_localize(None).dt.to_period(
        frequence.rstrip("E") if frequence in ("ME",) else frequence)

    compte = (table.groupby(["periode", "genre"]).size()
              .unstack(fill_value=0))
    compte.index = compte.index.to_timestamp()

    # Les periodes sans aucune annotation manquent : les rendre visibles.
    if len(compte) > 1:
        plage = pd.date_range(compte.index.min(), compte.index.max(),
                              freq="MS" if frequence == "ME" else frequence)
        compte = compte.reindex(plage, fill_value=0)

    return compte


def mots_frequents(annot: pd.DataFrame | None = None,
                   top: int = 25) -> pd.DataFrame:
    """Les mots cherches le plus souvent au dictionnaire."""
    if annot is None:
        annot = load.annotations_lecture()

    if annot.empty:
        return pd.DataFrame()

    mots = annot[(annot["genre"] == "word_lookup") & annot["mot"].notna()]

    if mots.empty:
        return pd.DataFrame()

    compte = (mots.groupby(mots["mot"].str.lower())
              .agg(occurrences=("mot", "size"),
                   livres=("asin", "nunique"),
                   derniere=("started_at", "max"))
              .sort_values("occurrences", ascending=False))

    return compte.head(top)


def surlignements_sans_texte(annot: pd.DataFrame | None = None) -> Constat:
    """Combien de surlignements attendent encore leur texte.

    La liseuse ne stocke que des positions ; le texte doit etre extrait
    des fichiers KFX. Tant que ce n'est pas fait, aucune analyse de
    CONTENU n'est possible - seulement des analyses de comportement.
    """
    if annot is None:
        annot = load.annotations_lecture()

    if annot.empty:
        return Constat("Surlignements sans texte", None, 0)

    hauts = annot[annot["genre"] == "highlight"]
    vides = int(hauts["texte"].isna().sum())

    return Constat("Surlignements sans texte", vides, len(hauts),
                   note="extraction KFX a faire" if vides else "complet")


# ---------------------------------------------------- croisements

# Ce que la lecture peut raisonnablement eclairer, une fois l'historique
# constitue. Le decalage dit dans quel sens la question se pose : le
# sommeil de la nuit precede la lecture du jour, donc decalage = 1 sur
# les colonnes nocturnes.
PAIRES_CANDIDATES = [
    ("sommeil_score", "minutes", 1, "Le sommeil de la nuit change-t-il "
                                    "le temps de lecture du lendemain ?"),
    ("hrv", "minutes", 1, "La recuperation precede-t-elle les longues "
                          "sessions ?"),
    ("minutes", "sommeil_score", 0, "Lire davantage change-t-il la nuit "
                                    "qui suit ?"),
    ("pas", "minutes", 0, "Lecture et activite physique s'excluent-elles ?"),
    ("veille_kcal", "minutes", 0, "L'alimentation joue-t-elle ?"),
]


def table_croisee() -> pd.DataFrame:
    """Lecture + sommeil + activite + alimentation, sur un index de jours.

    C'est le seul endroit ou la centralisation en une base paie
    reellement : ces colonnes viennent de quatre sources qui ne se
    connaissent pas, et seule la DATE les rapproche.
    """
    quotidien = load.lecture()

    if quotidien.empty:
        return quotidien

    autres = []

    for chargeur, prefixe in ((load.daily, ""), (load.sleep, "")):
        try:
            table = chargeur()
        except Exception:                                   # noqa: BLE001
            continue

        if not table.empty:
            autres.append(table.add_prefix(prefixe))

    fusion = quotidien

    for table in autres:
        nouvelles = [c for c in table.columns if c not in fusion.columns]
        fusion = fusion.join(table[nouvelles], how="outer")

    return fusion


def croisements(table: pd.DataFrame | None = None) -> list:
    """Teste les paires candidates. Renvoie des Correlation de correlate.py.

    Aucune conclusion n'est tiree ici : `tester` rend r, n et p, et c'est
    l'affichage qui refuse de conclure quand n est trop petit. Meme
    contrat que run_correlate en V3.
    """
    if table is None:
        table = table_croisee()

    if table.empty:
        return []

    resultats = []

    for x, y, decalage, _question in PAIRES_CANDIDATES:
        if x not in table.columns or y not in table.columns:
            continue

        resultat = correlate.tester(table[x], table[y], x, y,
                                    decalage=decalage)

        if resultat is not None:
            resultats.append(resultat)

    return resultats


def question_de(x: str, y: str) -> str:
    """La question posee par une paire, en francais."""
    for cx, cy, _d, question in PAIRES_CANDIDATES:
        if cx == x and cy == y:
            return question

    return f"{x} et {y} sont-ils lies ?"
