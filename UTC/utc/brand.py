"""
Seattle Aquarium visual identity.

Values transcribed from SAQ-001_Visual-ID-Guidelines_FINAL_V1-0823 (v1, Aug 2023).
Kept in one place so the GUI and the video overlays stay on-brand together.

Guideline notes that shaped the choices here:
  * Type on a background must exceed 4.5:1 contrast (AA). Large text (>=18pt, or
    >=14pt bold) and images need 3:1.
  * Body copy on White must always be Stone Gray, even though other colors pass.
  * Do not use tint/shade values of any brand color except Stone Gray.
  * Primary Logo is Mediterranean Blue on light grounds; White on dark grounds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
#  Palette
# --------------------------------------------------------------------------

# Primary
SALISH = "#004346"          # proprietary; the color of the building
FATHOM = "#0C2340"
MEDITERRANEAN = "#1963B0"   # water, and the Primary Logo

# Secondary
ALGAE = "#00C389"
SEAFOAM = "#3CCBDA"

# Accent
PURPLE_STAR = "#7A5EA8"
CORAL = "#F58674"

# Neutral
WHITE = "#FFFFFF"
PUMICE = "#EEEEEE"
STONE = "#575757"

# Stone is the one color the guidelines permit tinting, so these are legal and
# useful for GUI chrome (borders, disabled states, hover fills).
STONE_TINTS = {
    10: "#E6E6E6",
    20: "#CDCDCD",
    40: "#9B9B9B",
    60: "#6A6A6A",
    80: "#3A3A3A",
}


# --------------------------------------------------------------------------
#  Gradients  (guidelines p.19, "Gradient palette and hierarchy")
# --------------------------------------------------------------------------
#
# The One Ocean Gradient is described there as "a core part of our visual
# expression ... emblematic of how we see the ocean: as one continuous,
# dynamic environment". The page is also specific about where each family may
# be used, and those rules are encoded in the names below rather than left to
# memory:
#
#   DEEP and MEDIUM   -> backgrounds.
#   BRIGHT 2-colour   -> UI elements only, layered over darker backgrounds,
#                        and best kept to smaller elements.
#   BRIGHT 3-colour   -> larger elements, where there is room to breathe.
#   ALT               -> only over a bright-gradient background, or the hover
#                        state of a button. Never with photographic content.
#
# The full One Ocean gradient must never be used in its entirety (p.16).

#: Backgrounds. Dark enough to carry White type at AA across their whole run.
DEEP_GRADIENTS = {
    "salish_fathom": (SALISH, FATHOM),
    "fathom_mediterranean": (FATHOM, MEDITERRANEAN),
    "salish_mediterranean": (SALISH, MEDITERRANEAN),
}

#: Backgrounds, brighter. These reach values that White type cannot sit on, so
#: type over them belongs at the darker end.
MEDIUM_GRADIENTS = {
    "salish_algae": (SALISH, ALGAE),
    "mediterranean_seafoam": (MEDITERRANEAN, SEAFOAM),
}

#: UI elements over a darker ground -- a rule, a stripe, a small indicator.
BRIGHT_GRADIENTS = {
    "algae_seafoam": (ALGAE, SEAFOAM),
    "seafoam_purple": (SEAFOAM, PURPLE_STAR),
}

BRIGHT_GRADIENTS_3 = {
    "mediterranean_seafoam_algae": (MEDITERRANEAN, SEAFOAM, ALGAE),
    "algae_seafoam_purple": (ALGAE, SEAFOAM, PURPLE_STAR),
    "algae_seafoam_coral": (ALGAE, SEAFOAM, CORAL),
}

#: Reserved. p.19 names the hover state of a button as a sanctioned use.
ALT_GRADIENTS = {
    "mediterranean_purple": (MEDITERRANEAN, PURPLE_STAR),
    "mediterranean_purple_coral": (MEDITERRANEAN, PURPLE_STAR, CORAL),
}

#: The two-colour geometry from the spec diagram on p.19: the first colour at
#: 0, the second reached at 95, and the 50/50 blend landing at 70 rather than
#: halfway. The bias is what stops a two-colour gradient reading as a flat
#: wash -- it holds the first colour and then moves.
TWO_COLOR_END = 0.95
TWO_COLOR_MIDPOINT = 0.70

#: Both angles are sanctioned; this is the one the guidelines' own background
#: panels read as, top-left to bottom-right.
GRADIENT_ANGLE = 45.0


@dataclass(frozen=True)
class Theme:
    """One resolved colour scheme. `name` matches customtkinter's appearance mode."""

    name: str
    bg: str            # window ground
    surface: str       # cards / panels sitting on the ground
    surface_alt: str   # secondary surface, e.g. list rows
    border: str
    text: str          # body copy
    text_muted: str
    heading: str
    accent: str        # primary action
    accent_hover: str
    accent_text: str   # text drawn ON accent
    ok: str
    warn: str
    error: str
    logo: str          # which logo asset to use: "white" or "mediterranean"


# Dark: Fathom ground with white type and Algae/Seafoam accents. This pairing is
# shown as accessible in the guidelines' colour-and-accessibility grid (p.20).
DARK = Theme(
    name="dark",
    bg=FATHOM,
    surface="#132C4C",       # Fathom lifted slightly for card separation
    surface_alt="#1B3557",
    border="#2A4A73",
    text=WHITE,
    text_muted="#A8BBD4",
    heading=SEAFOAM,
    accent=ALGAE,
    accent_hover="#00A876",
    accent_text=FATHOM,      # dark type on the bright Algae fill
    ok=ALGAE,
    warn=CORAL,
    error=CORAL,
    logo="white",
)

# Light: White ground, Stone body copy (per the guidelines), Salish headings,
# Mediterranean as the action colour.
LIGHT = Theme(
    name="light",
    bg=WHITE,
    surface=PUMICE,
    surface_alt="#F7F7F7",
    border=STONE_TINTS[20],
    text=STONE,
    text_muted=STONE_TINTS[60],
    heading=SALISH,
    accent=MEDITERRANEAN,
    accent_hover="#154F8C",
    accent_text=WHITE,
    ok="#00795A",            # Algae is too light for small type on white
    warn="#B4472F",          # Coral likewise
    error="#B4472F",
    logo="mediterranean",
)

THEMES = {"dark": DARK, "light": LIGHT}


# --------------------------------------------------------------------------
#  Typography
# --------------------------------------------------------------------------

#: Primary typeface per the guidelines.
FONT_FAMILY = "Montserrat"

#: Where the operating system keeps fonts it has registered. A family here can
#: be named by a GUI toolkit.
_OS_FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
]

#: Fonts we ship ourselves. Pillow can load these straight off disk for the
#: video overlays, but a GUI toolkit cannot see them until they are registered
#: -- see register_bundled_fonts().
_BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_FONT_DIRS = [*_OS_FONT_DIRS, _BUNDLED_FONT_DIR]

_WEIGHTS = {
    "thin": "Montserrat-Thin.ttf",
    "extralight": "Montserrat-ExtraLight.ttf",
    "light": "Montserrat-Light.ttf",
    "regular": "Montserrat-Regular.ttf",
    "medium": "Montserrat-Medium.ttf",
    "semibold": "Montserrat-SemiBold.ttf",
    "bold": "Montserrat-Bold.ttf",
    "extrabold": "Montserrat-ExtraBold.ttf",
    "black": "Montserrat-Black.ttf",
}

# Used only if Montserrat is missing entirely.
_FALLBACKS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]


def font_path(weight: str = "regular") -> str | None:
    """Absolute path to a Montserrat TTF, or a sane fallback, or None.

    Montserrat is not monospace, so the telemetry panel right-aligns its values
    by measuring them rather than relying on fixed advance widths -- see
    overlay.py. That keeps us on-brand without sacrificing column alignment.
    """
    name = _WEIGHTS.get(weight.lower(), _WEIGHTS["regular"])
    for d in _FONT_DIRS:
        p = d / name
        if p.is_file():
            return str(p)
    for d in _FONT_DIRS:
        for fb in _FALLBACKS:
            p = d / fb
            if p.is_file():
                return str(p)
    return None


#: TTF filenames successfully handed to the OS by register_bundled_fonts().
_registered: set[str] = set()


def register_bundled_fonts() -> int:
    """Make fonts shipped in ``assets/fonts`` usable by a GUI toolkit.

    Returns how many were registered.

    A TTF sitting in a folder is invisible to Tk: Windows only offers families
    it has registered, so a packaged .exe on a laptop without Montserrat
    installed would quietly draw itself in a default face -- and that is exactly
    the machine we never get to look at. ``FR_PRIVATE`` registers for this
    process only, so it needs no admin rights and installs nothing permanently.

    Safe to call more than once, and a no-op off Windows.
    """
    if os.name != "nt" or not _BUNDLED_FONT_DIR.is_dir():
        return 0
    try:
        import ctypes

        gdi32 = ctypes.WinDLL("gdi32")
    except Exception:
        return 0

    FR_PRIVATE = 0x10
    for ttf in sorted(_BUNDLED_FONT_DIR.glob("*.ttf")):
        if ttf.name in _registered:
            continue
        try:
            if gdi32.AddFontResourceExW(ctypes.c_wchar_p(str(ttf)), FR_PRIVATE, 0):
                _registered.add(ttf.name)
        except Exception:
            pass
    return len(_registered)


def font_available(weight: str = "regular") -> bool:
    """True when a GUI toolkit can actually name that Montserrat weight.

    Deliberately stricter than `font_path`: a bundled file counts only once it
    has been registered, because naming a family the toolkit cannot resolve
    falls back to its default face rather than to our chosen fallback.
    """
    name = _WEIGHTS.get(weight.lower(), _WEIGHTS["regular"])
    if name in _registered:
        return True
    return any((d / name).is_file() for d in _OS_FONT_DIRS)


#: Windows registers most Montserrat weights as their own font family, so a
#: weight is selected by family *name*. Regular and Bold are the two styles of
#: the base family; everything else stands alone.
_FAMILY_NAMES = {
    "regular": "Montserrat",
    "bold": "Montserrat",
    "medium": "Montserrat Medium",
    "semibold": "Montserrat SemiBold",
    "extrabold": "Montserrat ExtraBold",
    "light": "Montserrat Light",
}


def font_family(weight: str = "regular", fallback: str = "Segoe UI") -> str:
    """Family name to hand a GUI toolkit for a given Montserrat weight.

    Tk has only normal/bold, so a Medium or SemiBold face cannot be requested as
    a style -- it has to be named. Falls back weight by weight, so a laptop with
    only the base family installed still gets Montserrat rather than dropping
    the whole app to Segoe UI.
    """
    key = weight.lower()
    if font_available(key):
        return _FAMILY_NAMES.get(key, FONT_FAMILY)
    if font_available("regular"):
        return FONT_FAMILY
    return fallback


# --------------------------------------------------------------------------
#  Logo assets
# --------------------------------------------------------------------------

_LOGO_SOURCE = (
    Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience")
    / "visual_media" / "communications" / "presentation_graphics" / "logos"
    / "Seattle_Aquarium_logos"
)

_LOGO_FILES = {
    "white": ["Seattle Aquarium Logo-White-LG.png", "SEAQ_white_logo.PNG"],
    "black": ["Seattle Aquarium Logo-Black-LG.png"],
    "mediterranean": [
        "png/Seattle Aquarium Logo-Mediterranean-SM.png",
        "Seattle Aquarium Logo-Mediterranean-LG.jpg",
    ],
}


def logo_path(variant: str = "white") -> str | None:
    """Locate a logo bitmap.

    Prefers a copy vendored into ``assets/`` (so a packaged .exe is
    self-contained), and falls back to the shared Dropbox originals.
    """
    local = Path(__file__).resolve().parent.parent / "assets" / f"logo_{variant}.png"
    if local.is_file():
        return str(local)
    for rel in _LOGO_FILES.get(variant, []):
        p = _LOGO_SOURCE / rel
        if p.is_file():
            return str(p)
    return None


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(h: str) -> float:
    """WCAG relative luminance of a colour."""
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours, 1.0 to 21.0.

    The guidelines set the bar on p.20: 4.5:1 for type, 3:1 for large type
    (>=18pt, or >=14pt bold) and for meaningful images. Worth having in code
    rather than in a person's head -- the chrome sits on a gradient, so a
    colour that reads on one end of it can vanish at the other, and that is
    exactly what happened to the rail's muted ink at 1.6:1 on Mediterranean.
    """
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def rgba(h: str, alpha: float) -> tuple[int, int, int, int]:
    """Brand colour with an alpha channel, for the semi-transparent overlays."""
    r, g, b = hex_to_rgb(h)
    return (r, g, b, max(0, min(255, int(round(alpha * 255)))))
