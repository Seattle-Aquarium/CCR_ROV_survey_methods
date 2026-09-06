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
# The banner and the rail are drawn in brand gradients and stay dark in both
# appearance modes, while the content area between them flips. Two reasons.
#
# The first is the guidelines: deep gradients are for backgrounds, and every
# colour in them is dark enough to carry White type at AA. There is no light
# gradient in the palette, and inventing one would mean tinting a brand colour,
# which p.18 forbids.
#
# The second is that it says something true about the application. The rail is
# the order of a survey day, and it runs across every mode and every page; a
# constant dark chrome around a changing content area is that idea drawn.

#: Deep gradients, per mode. Both pairs carry White type at AA throughout.
HEADER_GRADIENT = {
    "dark": brand.DEEP_GRADIENTS["salish_fathom"],
    "light": brand.DEEP_GRADIENTS["salish_mediterranean"],
}

#: The rail is flat. A gradient behind a list of four rows gives each of them
#: a slightly different ground, which reads as four states rather than one
#: control -- so the rail takes its distinctness from being a step off the
#: window ground, the way it always did, and the banner keeps the gradient.
RAIL_BG = SURFACE

#: A three-colour bright gradient, three pixels tall, dividing the banner from
#: the work below it. p.19 sanctions exactly this: a bright gradient as a UI
#: element layered over a darker background.
RULE_GRADIENT = brand.BRIGHT_GRADIENTS_3["algae_seafoam_purple"]
RULE_HEIGHT = 3

#: The open tool's underline on the section strip -- a small UI element, which
#: is what the two-colour bright gradients are for. The rail's own marker is
#: the flat accent: its ground is Pumice in light mode, and p.19 puts bright
#: gradients over *darker* backgrounds.
STRIPE_GRADIENT = brand.BRIGHT_GRADIENTS["algae_seafoam"]

#: Chrome type. Flat values rather than (light, dark) pairs: the ground under
#: them is a dark gradient whichever mode the rest of the window is in.
#:
#: Measured against the darkest and lightest ends of both gradients rather than
#: chosen by eye. The foot of the light-mode rail is Mediterranean, which is
#: far lighter than anything the dark mode reaches -- the previous muted value
#: sat at 1.6:1 there, invisible. These clear 3:1 on every ground they touch,
#: which is the bar for the large type the rail is set in.
CHROME_TEXT = brand.WHITE               # 6.1:1 worst case (on Mediterranean)
CHROME_TEXT_MUTED = "#CBD9E8"           # 4.2:1 worst case
CHROME_TEXT_DIM = "#9FB6D0"             # 2.9:1; only used at the Salish end

#: The rail's chapter names. Larger than body copy because the rail is the
#: progression through a survey day rather than a list of settings -- and at
#: 15px semibold it also clears the 3:1 large-text bar rather than 4.5:1.
FONT_RAIL = (FAMILY_SEMIBOLD, 15)
FONT_RAIL_SMALL = (FAMILY, 15)

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

    White in both modes, because the banner it sits on is a deep gradient in
    both. Mediterranean Blue is the treatment for a white or light ground, and
    the banner stopped being one -- a blue logo there would read as a smudge
    against Mediterranean's own end of the light-mode gradient.
    """
    del mode                      # kept: callers pass the current mode
    return brand.logo_path("white")
