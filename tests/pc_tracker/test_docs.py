"""La documentation du schema suit le code : chaque type, chaque champ.

Un document de reference qui oublie un champ ajoute trois mois plus tard
ment en silence. Ce test le rend bruyant.
"""

from __future__ import annotations

from pathlib import Path

from pc import schema

DOC = Path(__file__).resolve().parents[2] / "docs" / "PC_EVENTS.md"


def test_chaque_type_et_chaque_champ_sont_documentes():
    texte = DOC.read_text(encoding="utf-8")
    manquants = []

    for type_, spec in schema.EVENT_TYPES.items():
        section = texte.split(f"### `{type_}`", 1)

        if len(section) < 2:
            manquants.append(type_)
            continue

        corps = section[1].split("\n### ", 1)[0]

        for champ in spec.champs:
            if f"`{champ}`" not in corps:
                manquants.append(f"{type_}.{champ}")

    assert manquants == [], f"absents de docs/PC_EVENTS.md : {manquants}"
