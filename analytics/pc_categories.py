"""Categories d'applications : une decision d'ANALYSE, donc cote Chronicle.

Le tracker dit "c'etait VS Code" (pc/apps.py, une normalisation). Dire
"c'etait du code" est une interpretation : elle vit ici, pas sur la
machine, et elle peut changer sans toucher a une seule donnee brute - la
base garde l'application, jamais la categorie.

L'ORDRE EST UNE DECISION DE COULEUR
-----------------------------------
Chaque categorie a un rang fixe = une couleur fixe de la palette validee
(dashboard). Une categorie garde sa couleur quels que soient la periode et
la machine choisies : on apprend une fois que "Code" est bleu.

Ajouter une application : une ligne dans APPS (identifiant canonique de
pc/apps.py -> categorie). Une application absente tombe dans "Autre".
"""

from __future__ import annotations

# (identifiant, libelle) dans l'ordre des couleurs du dashboard.
CATEGORIES: list[tuple[str, str]] = [
    ("code", "Code & IA"),
    ("web", "Navigation"),
    ("communication", "Communication"),
    ("documents", "Notes & documents"),
    ("medias", "Medias & loisirs"),
    ("systeme", "Systeme"),
]

AUTRE = ("autre", "Autre")

APPS: dict[str, str] = {
    # Code & IA : editeurs, terminaux, outils de dev, assistants
    **dict.fromkeys([
        "vscode", "vscode-insiders", "vscodium", "cursor", "windsurf", "zed",
        "sublime-text", "notepad-plus-plus", "intellij", "pycharm",
        "android-studio", "visual-studio", "neovim", "vim", "arduino-ide",
        "jupyter", "windows-terminal", "console", "powershell", "pwsh", "cmd",
        "mintty", "alacritty", "kitty", "ghostty", "foot", "wezterm",
        "gnome-terminal", "konsole", "docker-desktop", "python", "claude",
        "chatgpt"], "code"),
    # Navigation
    **dict.fromkeys([
        "chrome", "chromium", "edge", "firefox", "brave", "opera", "vivaldi",
        "librewolf", "zen", "floorp"], "web"),
    # Communication
    **dict.fromkeys([
        "discord", "slack", "teams", "telegram", "whatsapp", "signal", "zoom",
        "outlook", "thunderbird"], "communication"),
    # Notes & documents
    **dict.fromkeys([
        "obsidian", "word", "excel", "powerpoint", "onenote", "acrobat-reader",
        "sumatra-pdf", "libreoffice", "calibre", "notepad"], "documents"),
    # Medias & loisirs
    **dict.fromkeys([
        "spotify", "vlc", "mpv", "obs", "blender", "gimp", "figma", "audacity",
        "musescore", "steam"], "medias"),
    # Systeme : bureau, fichiers, reglages, interface Windows
    **dict.fromkeys([
        "file-explorer", "file-manager", "task-manager", "settings", "taskbar",
        "desktop", "task-switcher", "shell-ui", "start-menu", "search",
        "store-app", "lock-screen"], "systeme"),
}

LIBELLES = dict(CATEGORIES + [AUTRE])


def category_of(app: str | None) -> str:
    """Identifiant canonique -> categorie ("autre" si inconnue ou privee)."""
    return APPS.get(app or "", AUTRE[0])


def as_payload() -> list[dict]:
    """Pour le dashboard : l'ordre donne la couleur (slot 1, 2, ...)."""
    categories = [{"id": ident, "label": libelle, "slot": rang}
                  for rang, (ident, libelle) in enumerate(CATEGORIES, 1)]
    categories.append({"id": AUTRE[0], "label": AUTRE[1], "slot": None})
    return categories
