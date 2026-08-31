"""Seattle Aquarium visual identity -- palette only.

Hex values transcribed from SAQ-001_Visual-ID-Guidelines_FINAL_V1-0823 (v1,
Aug 2023), p.18, and kept in step with UTC/utc/brand.py and CCR_ROV_field_log.tex.

Guideline rules that shape how these are used in the figures and PDF:
  * Text on a background must exceed 4.5:1 contrast (AA); large text / meaningful
    graphics may go to 3:1.
  * Body copy on White is always Stone Gray.
  * No tint/shade of any brand colour except Stone Gray.
  * Lead with the primary colours; secondary/accent colours have a limited role.
  * Bright colours (Algae, Seafoam, Coral) are fill/graphic colours only on
    light grounds -- never text.
"""
from __future__ import annotations

from dataclasses import dataclass

# -- Primary --------------------------------------------------------------
SALISH = "#004346"
FATHOM = "#0C2340"
MEDITERRANEAN = "#1963B0"

# -- Secondary ----------------------------------------------------------
ALGAE = "#00C389"
SEAFOAM = "#3CCBDA"

# -- Accent -----------------------------------------------------------
PURPLE_STAR = "#7A5EA8"
CORAL = "#F58674"

# -- Neutral --------------------------------------------------------
WHITE = "#FFFFFF"
PUMICE = "#EEEEEE"
STONE = "#575757"

# Stone is the one colour the guidelines permit tinting.
STONE_TINTS = {10: "#E6E6E6", 20: "#CDCDCD", 40: "#9B9B9B", 60: "#6A6A6A", 80: "#3A3A3A"}


@dataclass(frozen=True)
class Theme:
    """One resolved colour scheme for the figures."""

    name: str
    bg: str            # figure / axes ground
    surface: str       # panel fills, shaded spans
    grid: str
    text: str
    text_muted: str
    heading: str
    accent: str        # primary series / markers
    accent2: str       # secondary series
    ok: str
    warn: str
    ebb: str           # falling tide
    flood: str         # rising tide
    night: str         # night-shade fill


# Light: White ground, Stone body text, Salish headings, Mediterranean action.
LIGHT = Theme(
    name="light",
    bg=WHITE,
    surface=PUMICE,
    grid=STONE_TINTS[20],
    text=STONE,
    text_muted=STONE_TINTS[60],
    heading=SALISH,
    accent=MEDITERRANEAN,
    accent2=SALISH,
    ok="#00795A",       # Algae is too light for type / thin marks on white
    warn="#B4472F",     # Coral likewise
    ebb="#1963B0",
    flood="#C56A3A",
    night=STONE_TINTS[20],
)

# Dark: Fathom ground, white text, Seafoam headings, Algae accent.
DARK = Theme(
    name="dark",
    bg=FATHOM,
    surface="#132C4C",
    grid="#2A4A73",
    text=WHITE,
    text_muted="#A8BBD4",
    heading=SEAFOAM,
    accent=ALGAE,
    accent2=SEAFOAM,
    ok=ALGAE,
    warn=CORAL,
    ebb="#3CCBDA",
    flood="#F58674",
    night="#0A1B30",
)

THEMES = {"light": LIGHT, "dark": DARK}


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
