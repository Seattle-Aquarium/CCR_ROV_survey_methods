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

# --------------------------------------------------------------------------
#  Chrome: the banner and the rail
# --------------------------------------------------------------------------
#
# Both are flat surfaces that flip with the appearance mode. Gradients were
# tried on each and dropped: on the rail a gradient gives each of four rows a
# slightly different ground, which reads as four states rather than one
# control, and on the banner it fought the calm the rest of the window has.
#
# One gradient survives, and it is the piece that earns it -- the bright rule
# across the foot of the banner. It is a single object, it carries no type, and
# p.19 sanctions exactly that: a bright gradient as a UI element over a darker
# ground.

HEADER_BG = SURFACE
RAIL_BG = SURFACE

#: The three-colour bright gradient dividing the banner from the work below.
#: Deliberately the full bright range -- Algae through Seafoam into Purple
#: Star -- because it is the one place the whole palette gets to show at once.
RULE_GRADIENT = brand.BRIGHT_GRADIENTS_3["algae_seafoam_purple"]
RULE_HEIGHT = 6

#: The open tool's underline on the section strip: a small UI element, which
#: is what the two-colour bright gradients are for.
STRIPE_GRADIENT = brand.BRIGHT_GRADIENTS["algae_seafoam"]

#: The rail's chapter names. Larger than body copy because the rail is the
#: roadmap through a survey day rather than a list of settings -- and at 15pt
#: it also clears the 3:1 large-text bar rather than 4.5:1.
FONT_RAIL = (FAMILY_SEMIBOLD, 15)
FONT_RAIL_SMALL = (FAMILY_MEDIUM, 15)
FONT_RAIL_NUM = (FAMILY, 12)

#: The tools within a chapter. A step above body copy: the strip is a control,
#: and at 12pt it was reading as a caption.
FONT_SECTION = (FAMILY_MEDIUM, 13)
FONT_SECTION_ON = (FAMILY_SEMIBOLD, 14)

# --------------------------------------------------------------------------
#  Chapter buttons
# --------------------------------------------------------------------------
#
# Each chapter carries its own brand colour. Fathom is out -- it is the
# dark-mode window ground, and a button in it would be a hole. Everything else
# is available, Algae included: it is the dark mode's action colour but the
# light mode's is Mediterranean, so it was never reserved in the way it first
# appeared to be.
#
# The type on each is chosen by `ink_for`, not from a table, so a set can be
# swapped here and the labels follow.

#: name -> the four colours, in rail order.
CHAPTER_PALETTES = {
    # No Algae -- the set these started from, kept for comparison.
    "ocean": (brand.SALISH, brand.MEDITERRANEAN, brand.SEAFOAM,
              brand.PURPLE_STAR),
    # Green through blue: the Salish Sea seen from the surface downward.
    "kelp": (brand.SALISH, brand.ALGAE, brand.SEAFOAM, brand.MEDITERRANEAN),
    # Dark to bright, so the rail lightens as the day's work moves on.
    "tideline": (brand.SALISH, brand.MEDITERRANEAN, brand.ALGAE,
                 brand.SEAFOAM),
    # One primary anchoring three brighter ones.
    "estuary": (brand.SALISH, brand.ALGAE, brand.MEDITERRANEAN,
                brand.PURPLE_STAR),
    # The bright half of the palette, end to end.
    "spectrum": (brand.ALGAE, brand.SEAFOAM, brand.PURPLE_STAR, brand.CORAL),
    # Cool to warm, finishing on the accent.
    "shore": (brand.SALISH, brand.ALGAE, brand.SEAFOAM, brand.CORAL),
}

CHAPTER_COLOURS = CHAPTER_PALETTES["ocean"]

#: Bigger than a standard button (which is 28px tall at radius 6), because
#: these four are the roadmap rather than an action on a card.
CHAPTER_BTN_H = 70
CHAPTER_BTN_GAP = 18
CHAPTER_BTN_INSET = 14
CHAPTER_BTN_RADIUS = 10
CHAPTER_BTN_BORDER = 2
CHAPTER_BTN_BORDER_ON = 3
#: Room above the first button, and how far down the four sit as a group.
CHAPTER_BTN_TOP = 18

FONT_BANNER = (FAMILY, 23, "bold")
FONT_BANNER_SUB = (FAMILY, 11)


# --------------------------------------------------------------------------
#  Display scaling
# --------------------------------------------------------------------------
#
# CustomTkinter scales its own widgets and fonts for the display -- 2.5x on the
# field laptop -- but a raw tkinter Canvas knows nothing about that, and Tk
# itself reports 96 DPI regardless. So anything drawn on a canvas (the banner
# and the rail) has to be scaled by hand, or it renders at two-fifths the size
# of everything around it. This was not a subtle bug: the banner came out
# shorter than a single card heading.


def scale_of(widget) -> float:
    """CustomTkinter's widget scaling for this display, or 1.0."""
    try:
        import customtkinter as ctk
        return float(ctk.ScalingTracker.get_widget_scaling(widget))
    except Exception:
        return 1.0


def scale_font(font: tuple, factor: float) -> tuple:
    """A font tuple at display scale, for drawing on a canvas."""
    size = max(1, int(round(font[1] * factor)))
    return (font[0], size, *font[2:])


def apply(ctk, mode: str = "dark") -> None:
    """Set the global appearance mode."""
    ctk.set_appearance_mode("dark" if mode.lower().startswith("d") else "light")


def logo_for(mode: str) -> str | None:
    """Logo variant that is legible on the current ground.

    The guidelines allow White on any dark background; Mediterranean Blue is the
    primary treatment on white or light.
    """
    return brand.logo_path("white" if mode.lower().startswith("d")
                           else "mediterranean")


def ink_for(ground: str) -> str:
    """The brand colour that reads best as type on `ground`.

    Used where a background is a brand colour rather than a theme surface --
    the chapter buttons, which each carry their own. Seafoam sits in the middle
    of the luminance range and takes dark type; Salish and Purple Star take
    White. Picking it by measurement rather than by a lookup means a palette
    can be changed without anyone remembering to change the type with it.
    """
    return max((brand.WHITE, brand.FATHOM),
               key=lambda ink: brand.contrast(ink, ground))
