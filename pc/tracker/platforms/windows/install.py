"""Installation Windows : tache planifiee et hook PowerShell.

LA TACHE PLANIFIEE
------------------
Declencheur : ouverture de session de l'utilisateur, 30 s apres (le
bureau doit etre pret). Repetee toutes les 15 min avec la regle "ne pas
lancer une nouvelle instance" : si le tracker tourne, rien ne se passe ;
s'il est tombe, il repart. C'est le chien de garde, sans service.

Executable : pythonw.exe, la variante sans console de Python. Rien ne
s'affiche, rien ne clignote.

Droits : ceux de l'utilisateur (LeastPrivilege), session interactive
(InteractiveToken). Aucun droit administrateur n'est demande.

LE HOOK POWERSHELL
------------------
Une ligne ajoutee au profil PowerShell de l'utilisateur, entre deux
marqueurs, pour pouvoir la retirer proprement sans toucher au reste.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Chronicle PC Tracker"
CREATE_NO_WINDOW = 0x08000000
DEBUT = "# >>> chronicle-pc >>>"
FIN = "# <<< chronicle-pc <<<"

HOOK_PS1 = Path(__file__).resolve().parents[2] / "shell" / \
    "chronicle_pc_hook.ps1"
HOOK_BASH = Path(__file__).resolve().parents[2] / "shell" / \
    "chronicle_pc_hook.bash"
RACINE_DEPOT = Path(__file__).resolve().parents[4]


def pythonw() -> str:
    """pythonw.exe a cote de l'interpreteur courant (meme venv)."""
    courant = Path(sys.executable)
    candidat = courant.with_name("pythonw.exe")
    return str(candidat if candidat.exists() else courant)


def _utilisateur() -> str:
    domaine = os.environ.get("USERDOMAIN")
    nom = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domaine}\\{nom}" if domaine else nom


def task_xml(commande: str, arguments: str, dossier: str) -> str:
    utilisateur = escape(_utilisateur())

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Chronicle : collecte de l'activite PC (pc/tracker). Relance toutes les 15 min si arrete.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{utilisateur}</UserId>
      <Delay>PT30S</Delay>
      <Repetition>
        <Interval>PT15M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{utilisateur}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>10</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(commande)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(dossier)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *arguments], capture_output=True,
                          creationflags=CREATE_NO_WINDOW)


def _texte(resultat: subprocess.CompletedProcess) -> str:
    brut = (resultat.stdout or b"") + (resultat.stderr or b"")
    return brut.decode("cp850", "replace").strip()


def install_task(config_path: Path | None = None) -> tuple[bool, str]:
    """Enregistre (ou remplace) la tache. Renvoie (ok, message)."""
    arguments = "-m pc.tracker run"

    if config_path is not None:
        arguments += f' --config "{config_path}"'

    contenu = task_xml(pythonw(), arguments, str(RACINE_DEPOT))

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-16") as fichier:
        fichier.write(contenu)
        chemin = fichier.name

    try:
        resultat = _schtasks("/Create", "/TN", TASK_NAME, "/XML", chemin, "/F")
    finally:
        os.unlink(chemin)

    return resultat.returncode == 0, _texte(resultat)


def uninstall_task() -> tuple[bool, str]:
    resultat = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    return resultat.returncode == 0, _texte(resultat)


def task_installed() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def run_task() -> tuple[bool, str]:
    resultat = _schtasks("/Run", "/TN", TASK_NAME)
    return resultat.returncode == 0, _texte(resultat)


# --------------------------------------------------------- hook shell

def _powershell(commande: str, programme: str = "powershell") -> str | None:
    try:
        resultat = subprocess.run(
            [programme, "-NoProfile", "-NonInteractive", "-Command", commande],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None

    return resultat.stdout.decode("utf-8", "replace").strip() or None


def powershell_profiles() -> list[Path]:
    """Profils "tous les hotes" de Windows PowerShell et de PowerShell 7."""
    profils = []

    for programme in ("powershell", "pwsh"):
        chemin = _powershell("$PROFILE.CurrentUserAllHosts", programme)

        if chemin:
            profils.append(Path(chemin))

    return profils


def execution_policy() -> str | None:
    return _powershell("Get-ExecutionPolicy")


def _retirer_bloc(texte: str) -> str:
    lignes, dedans = [], False

    for ligne in texte.splitlines():
        if ligne.strip() == DEBUT:
            dedans = True
            continue

        if ligne.strip() == FIN:
            dedans = False
            continue

        if not dedans:
            lignes.append(ligne)

    return "\n".join(lignes).rstrip() + ("\n" if lignes else "")


def install_shell_hook(bash: bool = False) -> list[str]:
    """Ajoute le hook aux profils. Renvoie ce qui a ete fait, ligne a ligne."""
    rapport = []
    politique = execution_policy()

    if politique in ("Restricted", "AllSigned"):
        return [f"Politique d'execution PowerShell : {politique}. Le profil ne "
                "serait pas charge. Pour l'autoriser (decision a prendre) :",
                "  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"]

    for profil in powershell_profiles():
        profil.parent.mkdir(parents=True, exist_ok=True)
        existant = profil.read_text(encoding="utf-8-sig") \
            if profil.exists() else ""
        propre = _retirer_bloc(existant)
        bloc = f"{DEBUT}\n. '{HOOK_PS1}'\n{FIN}\n"
        profil.write_text((propre + "\n" if propre.strip() else "") + bloc,
                          encoding="utf-8")
        rapport.append(f"PowerShell : {profil}")

    if bash:
        bashrc = Path.home() / ".bashrc"
        existant = bashrc.read_text(encoding="utf-8") if bashrc.exists() \
            else ""
        propre = _retirer_bloc(existant)
        chemin = str(HOOK_BASH).replace("\\", "/")
        bloc = f'{DEBUT}\nsource "{chemin}"\n{FIN}\n'
        bashrc.write_text((propre + "\n" if propre.strip() else "") + bloc,
                          encoding="utf-8", newline="\n")
        rapport.append(f"bash : {bashrc}")

    return rapport


def uninstall_shell_hook() -> list[str]:
    rapport = []
    cibles = powershell_profiles() + [Path.home() / ".bashrc"]

    for fichier in cibles:
        if not fichier.exists():
            continue

        texte = fichier.read_text(encoding="utf-8-sig")

        if DEBUT in texte:
            fichier.write_text(_retirer_bloc(texte), encoding="utf-8")
            rapport.append(f"retire de {fichier}")

    return rapport
