"""Shared figure styling: fonts, per-theme rcParams, the wind-speed colour
scale, and the vendored coastline."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import matplotlib
import matplotlib.font_manager as fm
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap

from .. import brand, config

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# -- Fonts --------------------------------------------------------------
_FONT_DIRS = [
    config.PKG_DIR.parent.parent / "UTC" / "assets" / "fonts",  # repo-vendored
    config.PKG_DIR / "assets" / "fonts",                        # local, if copied
]
_FONT_FAMILY = "DejaVu Sans"
for d in _FONT_DIRS:
    if d.is_dir():
        for ttf in d.glob("Montserrat-*.ttf"):
            try:
                fm.fontManager.addfont(str(ttf))
            except Exception:
                pass
if any("Montserrat" == f.name for f in fm.fontManager.ttflist):
    _FONT_FAMILY = "Montserrat"


# -- Wind speed scale (knots) -- Beaufort-ish bands, danger end tied to the
#    Small Craft Advisory / gale thresholds -----------------------------
WIND_BOUNDS = [0, 3, 7, 11, 16, 21, 27, 34, 47]
WIND_LABELS = [
    "0–3 calm", "3–7 light", "7–11 gentle", "11–16 moderate",
    "16–21 fresh", "21–27 strong (SCA)", "27–34 near gale", "34+ gale",
]
_WIND_COLORS_LIGHT = ["#EAF3F6", "#BFE6EC", "#7FD3DE", "#3CA9C7",
                      "#1963B0", "#123B73", "#B4472F", "#7A241A"]
_WIND_COLORS_DARK = ["#12314F", "#1C4E63", "#2E7C8C", "#3CCBDA",
                     "#5AA0E0", "#8FC2FF", "#F58674", "#C0402C"]


def wind_cmap_norm(theme: brand.Theme):
    # 9 boundaries -> 8 bins -> 8 colours. Speeds above the top bound clamp to
    # the last colour; the colourbar still shows a "max" triangle.
    colors = _WIND_COLORS_DARK if theme.name == "dark" else _WIND_COLORS_LIGHT
    cmap = ListedColormap(colors, name="wind_kt")
    cmap.set_over(colors[-1])
    cmap.set_bad(theme.bg)
    return cmap, BoundaryNorm(WIND_BOUNDS, ncolors=len(colors))


# -- Coastline --------------------------------------------------------
_LAND_PATH = config.FIG_ASSET_DIR / "land_westcoast.geojson"


def load_land_polygons() -> list[list[list[tuple[float, float]]]]:
    """MultiPolygon coordinates from the vendored west-coast land file."""
    if not _LAND_PATH.is_file():
        return []
    gj = json.loads(_LAND_PATH.read_text(encoding="utf-8"))
    polys = []
    for feat in gj.get("features", []):
        g = feat["geometry"]
        if g["type"] == "Polygon":
            polys.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            polys.extend(g["coordinates"])
    return polys


def draw_land(ax, theme: brand.Theme, *, zorder: float = 0) -> None:
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    land_fill = "#1B3557" if theme.name == "dark" else "#E7E4DC"
    edge = theme.grid
    for rings in load_land_polygons():
        verts, codes = [], []
        for ring in rings:
            if len(ring) < 3:
                continue
            verts.extend(ring)
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 2)
                         + [MplPath.CLOSEPOLY])
        if not verts:
            continue
        patch = PathPatch(MplPath(np.array(verts), codes), facecolor=land_fill,
                          edgecolor=edge, linewidth=0.6, zorder=zorder)
        ax.add_patch(patch)


# -- rcParams ------------------------------------------------------
@contextmanager
def use_theme(theme: brand.Theme):
    rc = {
        "font.family": _FONT_FAMILY,
        "font.size": 9,
        "figure.facecolor": theme.bg,
        "axes.facecolor": theme.bg,
        "savefig.facecolor": theme.bg,
        "text.color": theme.text,
        "axes.labelcolor": theme.text,
        "axes.edgecolor": theme.grid,
        "axes.titlecolor": theme.heading,
        "xtick.color": theme.text_muted,
        "ytick.color": theme.text_muted,
        "xtick.labelcolor": theme.text,
        "ytick.labelcolor": theme.text,
        "grid.color": theme.grid,
        "grid.alpha": 0.5,
        "axes.grid": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "figure.dpi": 130,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.06,
    }
    with plt.rc_context(rc):
        yield


def save(fig, stem: Path | str, theme: brand.Theme) -> Path:
    """Write ``<stem>.pdf`` (for LaTeX) and ``<stem>.png`` (for the GUI)."""
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    pdf = stem.with_suffix(".pdf")
    fig.savefig(pdf, facecolor=theme.bg)
    fig.savefig(stem.with_suffix(".png"), facecolor=theme.bg, dpi=200)
    plt.close(fig)
    return pdf


def heading(ax, text: str, theme: brand.Theme, *, subtitle: str = "") -> None:
    ax.set_title(text, loc="left", fontsize=12, fontweight="bold",
                 color=theme.heading, pad=14 if subtitle else 8)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8.2,
                color=theme.text_muted, va="bottom")
