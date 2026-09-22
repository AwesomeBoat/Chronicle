"""Noms canoniques des applications, communs a toutes les machines.

Le meme logiciel porte des noms differents selon le systeme :

    Windows            Linux (Hyprland)        canonique
    Code.exe           code / Code             vscode
    msedge.exe         microsoft-edge          edge
    chrome.exe         google-chrome           chrome

Sans table commune, "temps passe dans VS Code" demanderait une requete par
systeme. Le champ `app` des evenements est donc canonique ; le nom brut
reste dans `process` (et `window_class` sous Linux) pour pouvoir refaire la
correspondance si cette table change.

C'est une NORMALISATION, pas une analyse : on dit "c'est VS Code", jamais
"c'est du travail". La categorie (code, navigation, loisir) est une
decision d'analyse, elle vit dans Chronicle.

Ajouter une application : une ligne dans CONNUES. La cle est le nom de
l'executable sans extension, ou la classe de fenetre, en minuscules.
"""

from __future__ import annotations

import re

# cle (minuscules, sans .exe) -> (identifiant canonique, nom lisible)
CONNUES: dict[str, tuple[str, str]] = {
    # Editeurs et IDE
    "code": ("vscode", "Visual Studio Code"),
    "code-oss": ("vscode", "Visual Studio Code"),
    "codium": ("vscodium", "VSCodium"),
    "code - insiders": ("vscode-insiders", "VS Code Insiders"),
    "code-insiders": ("vscode-insiders", "VS Code Insiders"),
    "cursor": ("cursor", "Cursor"),
    "windsurf": ("windsurf", "Windsurf"),
    "zed": ("zed", "Zed"),
    "sublime_text": ("sublime-text", "Sublime Text"),
    "notepad++": ("notepad-plus-plus", "Notepad++"),
    "notepad": ("notepad", "Bloc-notes"),
    "idea64": ("intellij", "IntelliJ IDEA"),
    "idea": ("intellij", "IntelliJ IDEA"),
    "pycharm64": ("pycharm", "PyCharm"),
    "pycharm": ("pycharm", "PyCharm"),
    "studio64": ("android-studio", "Android Studio"),
    "android-studio": ("android-studio", "Android Studio"),
    "devenv": ("visual-studio", "Visual Studio"),
    "nvim": ("neovim", "Neovim"),
    "vim": ("vim", "Vim"),
    "arduino ide": ("arduino-ide", "Arduino IDE"),
    "obsidian": ("obsidian", "Obsidian"),
    "jupyter-lab": ("jupyter", "Jupyter"),

    # Navigateurs
    "chrome": ("chrome", "Google Chrome"),
    "google-chrome": ("chrome", "Google Chrome"),
    "google-chrome-stable": ("chrome", "Google Chrome"),
    "chromium": ("chromium", "Chromium"),
    "chromium-browser": ("chromium", "Chromium"),
    "msedge": ("edge", "Microsoft Edge"),
    "microsoft-edge": ("edge", "Microsoft Edge"),
    "microsoft-edge-stable": ("edge", "Microsoft Edge"),
    "firefox": ("firefox", "Firefox"),
    "firefox-esr": ("firefox", "Firefox"),
    "brave": ("brave", "Brave"),
    "brave-browser": ("brave", "Brave"),
    "opera": ("opera", "Opera"),
    "vivaldi": ("vivaldi", "Vivaldi"),
    "vivaldi-stable": ("vivaldi", "Vivaldi"),
    "librewolf": ("librewolf", "LibreWolf"),
    "zen": ("zen", "Zen Browser"),
    "zen-browser": ("zen", "Zen Browser"),
    "floorp": ("floorp", "Floorp"),

    # Terminaux et shells
    "windowsterminal": ("windows-terminal", "Windows Terminal"),
    "wt": ("windows-terminal", "Windows Terminal"),
    "openconsole": ("windows-terminal", "Windows Terminal"),
    "conhost": ("console", "Console Windows"),
    "powershell": ("powershell", "Windows PowerShell"),
    "pwsh": ("pwsh", "PowerShell"),
    "cmd": ("cmd", "Invite de commandes"),
    "mintty": ("mintty", "Git Bash (mintty)"),
    "alacritty": ("alacritty", "Alacritty"),
    "kitty": ("kitty", "kitty"),
    "ghostty": ("ghostty", "Ghostty"),
    "com.mitchellh.ghostty": ("ghostty", "Ghostty"),
    "foot": ("foot", "foot"),
    "wezterm": ("wezterm", "WezTerm"),
    "wezterm-gui": ("wezterm", "WezTerm"),
    "org.wezfurlong.wezterm": ("wezterm", "WezTerm"),
    "gnome-terminal-server": ("gnome-terminal", "GNOME Terminal"),
    "konsole": ("konsole", "Konsole"),

    # Communication
    "discord": ("discord", "Discord"),
    "slack": ("slack", "Slack"),
    "ms-teams": ("teams", "Microsoft Teams"),
    "teams": ("teams", "Microsoft Teams"),
    "msteams": ("teams", "Microsoft Teams"),
    "telegram": ("telegram", "Telegram"),
    "org.telegram.desktop": ("telegram", "Telegram"),
    "whatsapp": ("whatsapp", "WhatsApp"),
    "whatsapp.root": ("whatsapp", "WhatsApp"),
    "signal": ("signal", "Signal"),
    "zoom": ("zoom", "Zoom"),
    "outlook": ("outlook", "Outlook"),
    "olk": ("outlook", "Outlook"),
    "thunderbird": ("thunderbird", "Thunderbird"),

    # Bureautique
    "winword": ("word", "Microsoft Word"),
    "excel": ("excel", "Microsoft Excel"),
    "powerpnt": ("powerpoint", "Microsoft PowerPoint"),
    "onenote": ("onenote", "OneNote"),
    "acrord32": ("acrobat-reader", "Adobe Acrobat Reader"),
    "acrobat": ("acrobat-reader", "Adobe Acrobat Reader"),
    "sumatrapdf": ("sumatra-pdf", "SumatraPDF"),
    "libreoffice": ("libreoffice", "LibreOffice"),
    "soffice": ("libreoffice", "LibreOffice"),
    "calibre": ("calibre", "calibre"),

    # Medias et creation
    "spotify": ("spotify", "Spotify"),
    "vlc": ("vlc", "VLC"),
    "mpv": ("mpv", "mpv"),
    "obs64": ("obs", "OBS Studio"),
    "obs": ("obs", "OBS Studio"),
    "com.obsproject.studio": ("obs", "OBS Studio"),
    "blender": ("blender", "Blender"),
    "gimp": ("gimp", "GIMP"),
    "gimp-2.10": ("gimp", "GIMP"),
    "figma": ("figma", "Figma"),
    "audacity": ("audacity", "Audacity"),
    "musescore4": ("musescore", "MuseScore"),

    # Systeme et fichiers
    "explorer": ("file-explorer", "Explorateur de fichiers"),
    "nautilus": ("file-manager", "Fichiers"),
    "org.gnome.nautilus": ("file-manager", "Fichiers"),
    "thunar": ("file-manager", "Thunar"),
    "dolphin": ("file-manager", "Dolphin"),
    "taskmgr": ("task-manager", "Gestionnaire des taches"),
    "systemsettings": ("settings", "Parametres"),
    "docker desktop": ("docker-desktop", "Docker Desktop"),
    "steam": ("steam", "Steam"),
    "steamwebhelper": ("steam", "Steam"),
    "claude": ("claude", "Claude"),
    "chatgpt": ("chatgpt", "ChatGPT"),
    "python": ("python", "Python"),
    "pythonw": ("python", "Python"),
    # Hote des applications du Store, quand l'application hebergee n'a
    # pas pu etre resolue (fenetre minimisee, application suspendue).
    "applicationframehost": ("store-app", "Application du Store"),
    "startmenuexperiencehost": ("start-menu", "Menu Demarrer"),
    "searchhost": ("search", "Recherche Windows"),
    "shellexperiencehost": ("shell-ui", "Interface Windows"),
    "lockapp": ("lock-screen", "Ecran de verrouillage"),
}

# Classes de fenetres Windows qui ne sont pas des applications : sans cette
# table, cliquer sur la barre des taches compterait comme "explorer".
CLASSES_SPECIALES: dict[str, tuple[str, str]] = {
    "shell_traywnd": ("taskbar", "Barre des taches"),
    "shell_secondarytraywnd": ("taskbar", "Barre des taches"),
    "progman": ("desktop", "Bureau"),
    "workerw": ("desktop", "Bureau"),
    "multitaskingviewframe": ("task-switcher", "Alt+Tab"),
    "xamlexplorerhostislandwindow": ("task-switcher", "Alt+Tab"),
}

# Identifiants canoniques des navigateurs (collecteur browser_page).
NAVIGATEURS = frozenset({"chrome", "chromium", "edge", "firefox", "brave",
                         "opera", "vivaldi", "librewolf", "zen", "floorp"})

_SLUG = re.compile(r"[^a-z0-9._+-]+")


def _cle(nom: str) -> str:
    """"C:\\...\\Code.exe" -> "code" ; "Code" -> "code"."""
    base = nom.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()

    if base.endswith(".exe"):
        base = base[:-4]

    return base


def slug(nom: str) -> str:
    """Identifiant sur quand l'application est inconnue de la table."""
    propre = _SLUG.sub("-", _cle(nom)).strip("-")
    return propre[:48] or "unknown"


def canonical_app(process: str | None, window_class: str | None = None,
                  description: str | None = None) -> tuple[str, str | None]:
    """(identifiant canonique, nom lisible) d'une application.

    Ordre de recherche : classe de fenetre speciale (bureau, barre des
    taches), nom du processus, classe de fenetre (Linux), puis repli sur
    le nom du processus tel quel.

    `description` est le nom declare par l'executable lui-meme (le
    FileDescription de Windows). Il sert de nom lisible pour une
    application absente de la table, jamais d'identifiant : il est
    traduit selon la langue du systeme ("Explorateur Windows").
    """
    if window_class:
        speciale = CLASSES_SPECIALES.get(window_class.strip().lower())

        if speciale:
            return speciale

    for candidat in (process, window_class):
        if candidat:
            connue = CONNUES.get(_cle(candidat))

            if connue:
                return connue

    if process:
        return slug(process), (description or None)

    if window_class:
        return slug(window_class), (description or None)

    return "unknown", description or None


def is_browser(app_id: str | None) -> bool:
    return app_id in NAVIGATEURS
