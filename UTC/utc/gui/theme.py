"""
Seattle Aquarium theming for CustomTkinter.

CustomTkinter accepts ``(light, dark)`` tuples for any colour, and switches
between them when the appearance mode changes. So every colour here is defined
as a pair and the toggle costs nothing at runtime.

Both schemes come from the brand palette and respect the guidelines'
accessibility rules: dark is Fathom ground with white type and Algae/Seafoam
accents; light is White/Pumice with Stone body copy (which the guidelines
require on white) and Salish/Mediterranean for headings and actions.
"""

from __future__ import annotations

from .. import brand

L, D = brand.LIGHT, brand.DARK


def pair(attr: str) -> tuple[str, str]:
    """(light, dark) for a Theme attribute."""
    return (getattr(L, attr), getattr(D, attr))


BG = pair("bg")
SURFACE = pair("surface")
SURFACE_ALT = pair("surface_alt")
BORDER = pair("border")
TEXT = pair("text")
TEXT_MUTED = pair("text_muted")
HEADING = pair("heading")
ACCENT = pair("accent")
ACCENT_HOVER = pair("accent_hover")
ACCENT_TEXT = pair("accent_text")
OK = pair("ok")
WARN = pair("warn")
ERROR = pair("error")

#: Entry fields need a little more contrast against the surface they sit on.
FIELD_BG = (brand.WHITE, "#0A1E36")
FIELD_BORDER = (brand.STONE_TINTS[20], "#37567F")

# Type scale, following the hierarchy on p.24 of the guidelines: Title is Bold,
# Header 1 Medium, Header 2 SemiBold, body copy Regular.
#
# Montserrat's weights are separate Windows families, so the weight travels in
# the family name. Do not add a "bold" style alongside -- Tk already reports
# Montserrat SemiBold as bold, and asking for both synthesises a double-bold.
#
# The guidelines also specify tracking per level; Tk exposes no letter-spacing
# control, so that part is print-only and is deliberately not attempted here.
# Register any fonts we ship before resolving family names -- an unregistered
# bundled TTF cannot be named by Tk, so this has to happen first. theme is the
# module that needs them, which makes it the right place to guarantee ordering.
brand.register_bundled_fonts()

FAMILY = brand.font_family("regular")
FAMILY_MEDIUM = brand.font_family("medium")
FAMILY_SEMIBOLD = brand.font_family("semibold")

#: Consolas is used only for the monospace log pane, where column alignment
#: matters more than branding.
MONO = "Consolas"

FONT_TITLE = (FAMILY, 22, "bold")        # Title
FONT_H1 = (FAMILY_MEDIUM, 16)            # Header 1 (primary)
FONT_H2 = (FAMILY_SEMIBOLD, 13)          # Header 2 (secondary)
FONT_BODY = (FAMILY, 12)                 # Body copy
FONT_SMALL = (FAMILY, 11)
FONT_MONO = (MONO, 11)

RADIUS = 8
PAD = 12


def apply(ctk, mode: str = "dark") -> None:
    """Set the global appearance mode."""
    ctk.set_appearance_mode("dark" if mode.lower().startswith("d") else "light")


def logo_for(mode: str) -> str | None:
    """Logo variant that is legible on the current ground.

    The guidelines allow White on any dark background; Mediterranean Blue is the
    primary treatment on white or light.
    """
    return brand.logo_path("white" if mode.lower().startswith("d") else "mediterranean")
