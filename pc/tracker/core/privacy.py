"""Vie privee : ce qui sort de la machine est nettoye ICI, avant la file.

Rien de ce qui est redige n'est jamais ecrit sur le disque du tracker ni
envoye a Chronicle. Quatre outils :

    sanitize_url()     une URL -> un domaine (defaut), ou un chemin nettoye
    redact_secrets()   une commande, un message -> jetons et mots de passe
                       remplaces par [REDACTED]
    PrivacyRules       applications, domaines et chemins exclus
    normalize_title()  un titre de fenetre sans ses compteurs volatils

Principe de redaction : il vaut mieux masquer un hash de commit innocent
que laisser passer une cle d'API. Les faux positifs coutent un peu
d'information ; un faux negatif coute un secret, pour toujours.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASQUE = "[REDACTED]"

URL_MODES = ("domain", "path", "full")


# ================================================================== URL

# Parametres de requete dont la valeur est gardee en mode "full". Tous les
# autres voient leur valeur masquee : une liste blanche, parce qu'une liste
# noire des parametres sensibles ne sera jamais complete.
PARAMETRES_SURS = frozenset({"q", "query", "search", "s", "v", "list", "t",
                             "page", "p", "tab", "lang", "hl", "sort",
                             "order", "view", "mode", "type", "category"})

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEXA_LONG = re.compile(r"^[0-9a-fA-F]{16,}$")
_CHIFFRES_LONGS = re.compile(r"^\d{6,}$")
_EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
_JETON = re.compile(r"^[A-Za-z0-9_\-+=.~]{24,}$")


def _segment(segment: str) -> str:
    """Un morceau de chemin -> lui-meme, ou une etiquette s'il identifie."""
    if not segment:
        return segment

    if _EMAIL.match(segment):
        return ":email"

    if _UUID.match(segment) or _HEXA_LONG.match(segment):
        return ":id"

    if _CHIFFRES_LONGS.match(segment):
        return ":n"

    if _JETON.match(segment) and _entropie(segment) > 3.5:
        return ":token"

    return segment


def _hote(parties) -> str | None:
    hote = (parties.hostname or "").lower().rstrip(".")

    if not hote:
        return None

    if hote.startswith("www."):
        hote = hote[4:]

    port = parties.port

    if port and not ((parties.scheme == "http" and port == 80)
                     or (parties.scheme == "https" and port == 443)):
        return f"{hote}:{port}"

    return hote


def sanitize_url(url: str | None, mode: str = "domain"
                 ) -> tuple[str | None, str | None]:
    """URL brute -> (domaine, url nettoyee ou None).

    domain  seul le domaine est garde (defaut, recommande)
    path    schema + domaine + chemin ; identifiants et jetons du chemin
            remplaces par :id, :token, :email ; requete et fragment retires
    full    comme path, plus la requete, valeurs masquees sauf liste blanche

    Toujours retires : identifiants dans l'URL (user:pass@), fragment (#).
    Les pages locales (file://) ne donnent ni domaine ni chemin : un chemin
    de fichier dit plus qu'il ne faut.
    """
    if not url or not isinstance(url, str):
        return None, None

    try:
        parties = urlsplit(url.strip())
    except ValueError:
        return None, None

    schema = parties.scheme.lower()

    if schema in ("http", "https"):
        domaine = _hote(parties)
    elif schema == "file":
        return "file", None
    elif schema in ("chrome", "edge", "about", "brave", "opera", "vivaldi",
                    "moz-extension", "chrome-extension", "extension"):
        # Pages internes du navigateur (chrome://newtab) : sans risque.
        interne = (parties.netloc or parties.path.split("/")[0]).lower()
        return (f"{schema}://{interne}" if interne else f"{schema}:"), None
    else:
        return None, None

    if domaine is None:
        return None, None

    if mode not in ("path", "full"):
        return domaine, None

    chemin = "/".join(_segment(s) for s in parties.path.split("/"))
    requete = ""

    if mode == "full" and parties.query:
        paires = [(cle, valeur if cle.lower() in PARAMETRES_SURS else MASQUE)
                  for cle, valeur in parse_qsl(parties.query,
                                               keep_blank_values=True)]
        requete = urlencode(paires, safe="[]")

    return domaine, urlunsplit((schema, domaine, chemin or "/", requete, ""))


def domain_matches(domaine: str | None, motifs) -> bool:
    """`banque.be` couvre banque.be, www.banque.be et secure.banque.be."""
    if not domaine:
        return False

    hote = domaine.split(":")[0].lower()

    for motif in motifs:
        motif = motif.lower().lstrip(".")

        if hote == motif or hote.endswith("." + motif):
            return True

    return False


def is_ip(hote: str) -> bool:
    try:
        ipaddress.ip_address(hote.split(":")[0])
        return True
    except ValueError:
        return False


# ============================================================ secrets

def _entropie(texte: str) -> float:
    """Entropie de Shannon, en bits par caractere."""
    if not texte:
        return 0.0

    frequences = {c: texte.count(c) / len(texte) for c in set(texte)}
    return -sum(p * math.log2(p) for p in frequences.values())


# Noms qui annoncent un secret, dans une variable, une option, un parametre.
_NOM_SECRET = (r"(?:[A-Za-z0-9_]*?(?:pass(?:word|wd|phrase)?|pwd|secret|token|"
               r"api[_-]?key|apikey|access[_-]?key|auth(?:orization)?|"
               r"credentials?|private[_-]?key|client[_-]?secret|session|"
               r"cookie|signature|conn(?:ection)?[_-]?str(?:ing)?|dsn)"
               r"[A-Za-z0-9_]*)")

_MOTIFS: list[tuple[re.Pattern, str]] = [
    # Bloc de cle privee complet.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
                r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), MASQUE),
    # Jetons a prefixe connu (GitHub, GitLab, Slack, OpenAI, Anthropic,
    # AWS, Google, Hugging Face, npm, PyPI, Stripe, SendGrid...).
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|glpat-[A-Za-z0-9_\-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                r"|sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{16,}"
                r"|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}"
                r"|ya29\.[0-9A-Za-z_\-]+|hf_[A-Za-z0-9]{30,}"
                r"|npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_\-]{40,}"
                r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
                r"|whsec_[A-Za-z0-9]{16,}|SG\.[\w\-]{16,}\.[\w\-]{16,}"
                r"|dop_v1_[a-f0-9]{40,}|shpat_[a-fA-F0-9]{32})"), MASQUE),
    # JWT.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"
                r"\.[A-Za-z0-9_\-]{5,}"), MASQUE),
    # En-tete d'autorisation : garder le mot, masquer la valeur.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9_\-.=:+/]{8,}"),
     r"\1 " + MASQUE),
    # PowerShell : ConvertTo-SecureString "motdepasse" -AsPlainText.
    (re.compile(r"(?i)(ConvertTo-SecureString\s+(?:-String\s+)?)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1" + MASQUE),
    # ssh-keygen -N "phrase de passe".
    (re.compile(r"(?i)(\bssh-keygen\b[^|;&]*?\s-N\s+)(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1" + MASQUE),
    # Identifiants dans une URL : scheme://user:pass@hote ou token@hote.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+(?::[^\s/@]*)?@"),
     r"\1" + MASQUE + "@"),
    # --password valeur, --api-key=valeur, -Password valeur (PowerShell).
    (re.compile(r"(?i)(--?" + _NOM_SECRET + r")(\s*[=\s]\s*)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2" + MASQUE),
    # NOM_SECRET=valeur, $env:NOM_SECRET = "valeur", set NOM_SECRET=valeur,
    # {"password": "valeur"}.
    (re.compile(r"(?i)(\$env:|\bset\s+|\bexport\s+|[\"']?)"
                r"(" + _NOM_SECRET + r")([\"']?\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\s,;&|)}\"']+)"),
     r"\1\2\3" + MASQUE),
    # Identifiant COMPOSE suivi d'une valeur, sans = ni -- :
    #   aws configure set aws_secret_access_key VALEUR
    #   gh secret set API_TOKEN VALEUR
    # Seulement les noms composes (un _ ou un - autour du mot-cle) : "fix
    # token expiration" dans un message de commit reste intact.
    (re.compile(r"(?i)\b((?:[A-Za-z0-9]+[_-])+(?:pass(?:word|wd)?|pwd|secret|"
                r"token|key|credentials?)|(?:pass(?:word|wd)?|secret|token|"
                r"api[_-]?key)(?:[_-][A-Za-z0-9]+)+)(\s+)(?![-\[])"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2" + MASQUE),
    # curl -u user:pass, mysql -pSECRET.
    (re.compile(r"(\s-u\s+)[^\s:]+:\S+"), r"\1" + MASQUE),
    (re.compile(r"(?i)(\bmysql(?:dump|admin)?\b[^|;&]*?\s-p)(?!\s)\S+"),
     r"\1" + MASQUE),
    (re.compile(r"(?i)(\bsshpass\s+-p\s*)\S+"), r"\1" + MASQUE),
]

# Longue chaine a haute entropie : un jeton sans prefixe connu. Les hash de
# commit (40 hexa) tombent aussi dedans : faux positif assume.
_CANDIDAT = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


def redact_secrets(texte: str | None) -> tuple[str | None, bool]:
    """Texte -> (texte redige, True si quelque chose a ete masque)."""
    if not texte:
        return texte, False

    redige = texte

    for motif, remplacement in _MOTIFS:
        redige = motif.sub(remplacement, redige)

    def _haute_entropie(trouve: re.Match) -> str:
        valeur = trouve.group(0)

        if "REDACTED" in valeur or valeur.isdigit():
            return valeur

        classes = sum(bool(re.search(p, valeur))
                      for p in (r"[a-z]", r"[A-Z]", r"\d"))

        if _entropie(valeur) >= 3.5 and (classes >= 2 or
                                          re.fullmatch(r"[0-9a-fA-F]+", valeur)):
            return MASQUE

        return valeur

    redige = _CANDIDAT.sub(_haute_entropie, redige)

    return redige, redige != texte


# ============================================================= titres

_COMPTEUR = re.compile(r"^\(\d{1,4}\+?\)\s+")          # "(3) YouTube"
_MARQUE_NON_SAUVE = re.compile(r"^[\u25cf\u2022*]\s+")  # "● fichier.py"
_ETOILE_FINALE = re.compile(r"\s*\*$")
_INVISIBLES = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_ESPACES = re.compile(r"\s+")

TITRE_MAX = 300

# Fenetres de navigation privee : ni titre ni URL, quel que soit le reglage.
_PRIVE = re.compile("(?i)inprivate|incognito|navigation priv[ée]e|"
                    "private browsing|fen[eê]tre priv[ée]e")


def normalize_title(titre: str | None) -> str | None:
    """Titre brut -> titre stable.

    Sans ca, "(3) Discord" puis "(4) Discord", ou "● main.py" puis "main.py"
    a chaque sauvegarde, couperaient une meme activite en dizaines de
    fragments.
    """
    if not titre:
        return None

    propre = _INVISIBLES.sub("", titre)
    propre = _ESPACES.sub(" ", propre).strip()
    propre = _COMPTEUR.sub("", propre)
    propre = _MARQUE_NON_SAUVE.sub("", propre)
    propre = _ETOILE_FINALE.sub("", propre)

    return propre[:TITRE_MAX] or None


def is_private_window(titre: str | None) -> bool:
    return bool(titre and _PRIVE.search(titre))


# ============================================================= chemins

def home_dir() -> str:
    return os.path.expanduser("~")


def normalize_path(chemin: str | None, maison: str | None = None) -> str | None:
    """Chemin -> forme commune a Windows et Linux : '/' et '~'.

    "C:\\Users\\everv\\Desktop\\x.py" -> "~/Desktop/x.py"
    "/home/ernest/projets/x.py"       -> "~/projets/x.py"

    Le nom d'utilisateur disparait au passage, et un meme projet a le meme
    chemin sur les deux machines s'il est range au meme endroit.
    """
    if not chemin:
        return chemin

    maison = (maison or home_dir()).replace("\\", "/").rstrip("/")
    unifie = chemin.replace("\\", "/")

    if maison and (unifie.lower() == maison.lower()
                   or unifie.lower().startswith(maison.lower() + "/")):
        return "~" + unifie[len(maison):]

    return unifie


def extension(chemin: str) -> str | None:
    suffixe = PurePath(chemin.replace("\\", "/")).suffix.lower()
    return suffixe[1:] if suffixe else None


def hash_identifier(valeur: str, sel: str) -> str:
    """Identifiant lie a un lieu ou a un tiers (SSID) -> empreinte.

    HMAC avec un sel propre a la machine, qui ne la quitte jamais : sans le
    sel, l'empreinte d'un nom de reseau Wi-Fi courant ne se retrouve pas
    par dictionnaire.
    """
    return hmac.new(sel.encode(), valeur.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


# ============================================================== regles

# Exclus par defaut. Modifiables dans [privacy] de la configuration.
APPLICATIONS_EXCLUES = ["keepass", "keepassxc", "1password", "bitwarden",
                        "dashlane", "lastpass", "enpass", "proton-pass",
                        "credentialuibroker", "consent"]

# Principales banques belges et services de paiement. "Jamais de donnees
# bancaires" vaut aussi pour le titre d'une page de banque en ligne.
DOMAINES_EXCLUS = [
    "belfius.be", "belfius.com", "ing.be", "kbc.be", "kbc.com", "cbc.be",
    "bnpparibasfortis.be", "fortis.be", "argenta.be", "crelan.be",
    "beobank.be", "keytradebank.be", "keytradebank.com", "hellobank.be",
    "axabank.be", "vdk.be", "nagelmackers.be",
    "revolut.com", "wise.com", "paypal.com", "n26.com", "bunq.com",
    "boursorama.com", "boursobank.com", "degiro.be", "degiro.com",
    "binance.com", "coinbase.com", "kraken.com", "bitvavo.com",
    "myminfin.be", "taxonweb.be", "irisbox.irisnet.be", "itsme-id.com",
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
]

CHEMINS_EXCLUS = ["~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.kube",
                  "~/AppData", "~/.password-store", "~/.local/share/keyrings",
                  "*.kdbx", "*.kdb", "*.pem", "*.key", "*.pfx", "*.p12"]


@dataclass
class PrivacyRules:
    """Ce qui est exclu, masque ou garde."""

    exclude_apps: list[str] = field(default_factory=lambda: list(
        APPLICATIONS_EXCLUES))
    redact_title_apps: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=lambda: list(
        DOMAINES_EXCLUS))
    exclude_paths: list[str] = field(default_factory=lambda: list(
        CHEMINS_EXCLUS))
    url_mode: str = "domain"
    # mask : l'intervalle existe, sans rien qui identifie ("prive").
    # drop : rien n'est emis. Le masque est le defaut : un trou dans la
    # chronologie serait indiscernable d'un PC eteint.
    mask_mode: str = "mask"

    def __post_init__(self) -> None:
        if self.url_mode not in URL_MODES:
            raise ValueError(f"url_mode doit valoir {URL_MODES}")

        if self.mask_mode not in ("mask", "drop"):
            raise ValueError("mask_mode doit valoir mask ou drop")

        self._apps = {a.lower().removesuffix(".exe")
                      for a in self.exclude_apps}
        self._titres = {a.lower().removesuffix(".exe")
                        for a in self.redact_title_apps}

    def app_excluded(self, app_id: str | None, process: str | None) -> bool:
        noms = {n.lower().removesuffix(".exe") for n in (app_id, process) if n}
        return bool(noms & self._apps)

    def title_redacted(self, app_id: str | None, process: str | None) -> bool:
        noms = {n.lower().removesuffix(".exe") for n in (app_id, process) if n}
        return bool(noms & self._titres)

    def domain_excluded(self, domaine: str | None) -> bool:
        return domain_matches(domaine, self.exclude_domains)

    def path_excluded(self, chemin_normalise: str | None) -> bool:
        """Chemin deja normalise (~, /). Motifs : prefixes ou globs."""
        if not chemin_normalise:
            return False

        chemin = chemin_normalise.lower()
        nom = chemin.rsplit("/", 1)[-1]

        for motif in self.exclude_paths:
            motif_n = motif.replace("\\", "/").lower().rstrip("/")

            if any(c in motif_n for c in "*?["):
                if fnmatch.fnmatch(nom, motif_n) or \
                        fnmatch.fnmatch(chemin, motif_n):
                    return True
            elif chemin == motif_n or chemin.startswith(motif_n + "/"):
                return True

        return False
