"""Vie privee : URL, secrets, titres, chemins, regles d'exclusion."""

from __future__ import annotations

import pytest

from pc.tracker.core.privacy import (MASQUE, PrivacyRules, domain_matches,
                                     hash_identifier, is_private_window,
                                     normalize_path, normalize_title,
                                     redact_secrets, sanitize_url)

# ================================================================ URL

URL = ("https://user:pass@www.github.com/AwesomeBoat/Chronicle/pull/42"
       "?token=abc123&q=pc+tracker#section")


def test_url_mode_domaine_par_defaut():
    assert sanitize_url(URL) == ("github.com", None)


def test_url_mode_path_sans_requete_ni_fragment_ni_identifiants():
    domaine, url = sanitize_url(URL, "path")
    assert domaine == "github.com"
    assert url == "https://github.com/AwesomeBoat/Chronicle/pull/42"
    assert "pass" not in url and "#" not in url and "token" not in url


def test_url_mode_full_masque_les_valeurs_sauf_liste_blanche():
    _, url = sanitize_url(URL, "full")
    assert "q=pc+tracker" in url
    assert "abc123" not in url and "token=" in url


@pytest.mark.parametrize("segment, etiquette", [
    ("550e8400-e29b-41d4-a716-446655440000", ":id"),
    ("a3f5c9d2e8b1f4a7c6d9", ":id"),
    ("12345678", ":n"),
    ("ernest@example.com", ":email"),
    ("eyJhbGciOiJIUzI1NiJ9xyzAbC9d8e7f6", ":token")])
def test_url_segments_identifiants_masques(segment, etiquette):
    _, url = sanitize_url(f"https://site.be/u/{segment}/profil", "path")
    assert url == f"https://site.be/u/{etiquette}/profil"


def test_url_port_non_standard_garde_et_standard_retire():
    assert sanitize_url("http://localhost:3000/app")[0] == "localhost:3000"
    assert sanitize_url("https://example.com:443/x")[0] == "example.com"


@pytest.mark.parametrize("url, attendu", [
    ("file:///C:/Users/ernest/secret.pdf", ("file", None)),
    ("chrome://newtab/", ("chrome://newtab", None)),
    ("about:blank", ("about://blank", None)),
    ("javascript:alert(1)", (None, None)),
    ("", (None, None)), (None, (None, None))])
def test_url_schemas_particuliers(url, attendu):
    assert sanitize_url(url) == attendu


def test_domaine_couvre_les_sous_domaines():
    motifs = ["belfius.be"]
    assert domain_matches("belfius.be", motifs)
    assert domain_matches("secure.belfius.be", motifs)
    assert not domain_matches("notbelfius.be", motifs)
    assert not domain_matches(None, motifs)


# ============================================================= secrets

@pytest.mark.parametrize("commande, secret", [
    ("git clone https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/x",
     "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnop1234567890",
     "sk-proj-abcdefghijklmnop1234567890"),
    ('$env:ANTHROPIC_API_KEY = "sk-ant-api03-abcdefghij1234567890"',
     "sk-ant-api03-abcdefghij1234567890"),
    ("mysql -u root -pS3cr3tP4ss mydb", "S3cr3tP4ss"),
    ("docker login --password hunter2hunter2 registry.io", "hunter2hunter2"),
    ("curl -u admin:motdepasse https://api.local", "motdepasse"),
    ('curl -H "Authorization: Bearer abcdef1234567890ghijkl" https://x',
     "abcdef1234567890ghijkl"),
    ("psql postgresql://twin:twin_local_dev@localhost:5432/digitaltwin",
     "twin_local_dev"),
    ("set DB_PASSWORD=azerty123", "azerty123"),
    ("aws configure set aws_secret_access_key wJalrXUtnFEMIK7MDENGbPxRfiCY",
     "wJalrXUtnFEMIK7MDENGbPxRfiCY"),
    ("$p = ConvertTo-SecureString \"MonMotDePasse!\" -AsPlainText -Force",
     "MonMotDePasse!"),
    ("ssh-keygen -t ed25519 -N 'ma phrase secrete'", "ma phrase secrete"),
    ("python app.py --api-key=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz",
     "eyJzdWIiOiIxIn0"),
])
def test_secrets_rediges(commande, secret):
    redige, masque = redact_secrets(commande)
    assert masque is True
    assert secret not in redige
    assert MASQUE in redige


@pytest.mark.parametrize("commande", [
    "git status", "git commit -m \"fix token expiration\"",
    "npm install express", "python main.py collect", "cd ~/Desktop",
    "cat .env", "ls -la", "docker compose up -d"])
def test_commandes_ordinaires_intactes(commande):
    assert redact_secrets(commande) == (commande, False)


def test_cle_privee_complete():
    texte = ("echo '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n"
             "-----END OPENSSH PRIVATE KEY-----' > k")
    redige, masque = redact_secrets(texte)
    assert masque and "b3BlbnNzaC1rZXk" not in redige


def test_redaction_vide():
    assert redact_secrets(None) == (None, False)
    assert redact_secrets("") == ("", False)


# ============================================================= titres

@pytest.mark.parametrize("brut, propre", [
    ("(3) Discord", "Discord"),
    ("\u25cf main.py - Chronicle - Visual Studio Code",
     "main.py - Chronicle - Visual Studio Code"),
    ("notes.txt *", "notes.txt"),
    ("  Deux   espaces\u200b ", "Deux espaces"),
    ("", None), (None, None)])
def test_normalisation_des_titres(brut, propre):
    assert normalize_title(brut) == propre


def test_titre_tronque():
    assert len(normalize_title("x" * 1000)) == 300


@pytest.mark.parametrize("titre", ["Nouvel onglet - Navigation InPrivate",
                                   "Google - Incognito",
                                   "Accueil - Navigation priv\u00e9e",
                                   "Mozilla Firefox Private Browsing"])
def test_fenetres_privees_detectees(titre):
    assert is_private_window(titre)


def test_fenetre_ordinaire_non_privee():
    assert not is_private_window("GitHub - Google Chrome")


# ============================================================ chemins

def test_chemins_normalises_windows_et_linux():
    assert normalize_path(r"C:\Users\everv\Desktop\x.py",
                          r"C:\Users\everv") == "~/Desktop/x.py"
    assert normalize_path("/home/ernest/projets/x.py",
                          "/home/ernest") == "~/projets/x.py"
    assert normalize_path(r"D:\Data\y.csv", r"C:\Users\everv") == "D:/Data/y.csv"
    assert normalize_path(r"c:\users\EVERV\a", r"C:\Users\everv") == "~/a"


def test_empreinte_de_ssid_salee_et_stable():
    a = hash_identifier("MaBox-5G", "sel1")
    assert a == hash_identifier("MaBox-5G", "sel1")
    assert a != hash_identifier("MaBox-5G", "sel2")
    assert "MaBox" not in a and len(a) == 16


# ============================================================= regles

def test_regles_par_defaut_excluent_gestionnaires_et_banques():
    regles = PrivacyRules()
    assert regles.app_excluded("keepassxc", "KeePassXC.exe")
    assert regles.app_excluded(None, "Bitwarden.exe")
    assert not regles.app_excluded("vscode", "Code.exe")
    assert regles.domain_excluded("www.belfius.be")
    assert not regles.domain_excluded("github.com")


@pytest.mark.parametrize("chemin, exclu", [
    ("~/.ssh/id_ed25519", True), ("~/.ssh", True),
    ("~/Desktop/coffre.kdbx", True), ("~/AppData/Local/x", True),
    ("~/Desktop/main.py", False), ("~/.sshx/config", False)])
def test_chemins_exclus(chemin, exclu):
    assert PrivacyRules().path_excluded(chemin) is exclu


def test_mode_url_invalide_refuse():
    with pytest.raises(ValueError):
        PrivacyRules(url_mode="tout")
