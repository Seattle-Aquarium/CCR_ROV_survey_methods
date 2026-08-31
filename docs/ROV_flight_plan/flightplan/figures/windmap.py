"""Wind map: 10-m wind speed as filled colour (kt, with a legend) and an
overlay of direction arrows, over a coastline around the survey site."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import brand
from ..conditions import Conditions
from ..sources.geo import nearest_wave_buoy
from . import style


def _halo(theme: brand.Theme):
    import matplotlib.patheffects as pe
    return [pe.withStroke(linewidth=2.6, foreground=theme.bg)]


def plot_wind_map(c: Conditions, theme_name: str, out_stem: Path | str) -> Path | None:
    wf = c.wind_field
    if wf is None:
        return None
    theme = brand.THEMES[theme_name]
    w, s, e, n = wf.bbox

    lon = np.array(wf.lons)
    lat = np.array(wf.lats)
    LON, LAT = np.meshgrid(lon, lat)
    SPD = np.array([[np.nan if v is None else v for v in row] for row in wf.speed_kt])
    DIR = np.array([[np.nan if v is None else v for v in row] for row in wf.dir_deg])

    with style.use_theme(theme):
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7.2, 3.7))
        fig.subplots_adjust(top=0.78, bottom=0.11, left=0.055, right=0.995)
        ax.set_xlim(w, e)
        ax.set_ylim(s, n)
        # Fill the page width; the domain is chosen ~landscape so the horizontal
        # stretch versus true geographic scale is small (< ~15%).
        ax.set_aspect("auto")

        cmap, norm = style.wind_cmap_norm(theme)
        pcm = ax.pcolormesh(LON, LAT, SPD, cmap=cmap, norm=norm,
                            shading="gouraud", zorder=1, alpha=0.92)
        style.draw_land(ax, theme, zorder=2)

        if np.nanmax(SPD) >= 21:
            ax.contour(LON, LAT, SPD, levels=[21], colors=[theme.warn],
                       linewidths=1.3, linestyles="--", zorder=3)

        # Direction arrows, uniform length (speed is the colour). Meteorological
        # convention: the arrow points the way the wind is blowing *to*.
        st = 2 if SPD.shape[0] >= 10 else 1
        th = np.radians(DIR[::st, ::st])
        U, V = -np.sin(th), -np.cos(th)
        ax.quiver(LON[::st, ::st], LAT[::st, ::st], U, V, color=theme.text,
                  alpha=0.8, pivot="mid", scale=20, width=0.005,
                  headwidth=3.2, headlength=3.6, zorder=4)

        ax.plot([c.site.lon], [c.site.lat], marker="*", ms=17,
                mfc=theme.heading, mec=theme.bg, mew=1.3, zorder=6)
        ax.annotate(c.site.name, xy=(c.site.lon, c.site.lat), xytext=(7, 6),
                    textcoords="offset points", fontsize=8.5, fontweight="bold",
                    color=theme.text, zorder=6, path_effects=_halo(theme))

        b = nearest_wave_buoy(c.site.lat, c.site.lon)
        if b and b.distance_km < 120 and w <= b.lon <= e and s <= b.lat <= n:
            ax.plot([b.lon], [b.lat], marker="^", ms=7, mfc=theme.accent2,
                    mec=theme.bg, mew=0.8, zorder=6)
            ax.annotate(f"NDBC {b.id.upper()}", xy=(b.lon, b.lat), xytext=(6, -11),
                        textcoords="offset points", fontsize=7, color=theme.text,
                        zorder=6, path_effects=_halo(theme))

        if c.serious_alerts:
            ev = c.serious_alerts[0].event.upper()
            ax.text(0.5, 0.97, f"⚑  {ev} IN EFFECT", transform=ax.transAxes,
                    ha="center", va="top", fontsize=8.5, fontweight="bold",
                    color="#FFFFFF", zorder=8,
                    bbox=dict(boxstyle="round,pad=0.35", fc=theme.warn, ec="none"))

        g = c.weather.max_gust_kt() if c.weather else None
        if g:
            ax.text(0.02, 0.055, f"max gust {g:.0f} kt", transform=ax.transAxes,
                    ha="left", va="bottom", fontsize=7.6, color=theme.text,
                    path_effects=_halo(theme))

        cb = fig.colorbar(pcm, ax=ax, boundaries=style.WIND_BOUNDS,
                          ticks=style.WIND_BOUNDS, extend="max", pad=0.015,
                          fraction=0.043, aspect=24)
        cb.set_label("10 m wind speed (kt)", fontsize=8)
        cb.ax.tick_params(labelsize=7)
        cb.ax.axhline(21, color=theme.warn, lw=1.4)

        vt = wf.valid_time
        fig.suptitle("Wind field", x=0.02, ha="left", fontsize=12,
                     fontweight="bold", color=theme.heading, y=0.975)
        fig.text(0.02, 0.86,
                 f"10 m wind · valid {vt:%a %d %b %H:%M %Z} · {wf.source}",
                 fontsize=8, color=theme.text_muted)
        fig.text(0.02, 0.805,
                 "arrows point downwind · dashed line and colour-bar mark = "
                 "21 kt small-craft threshold",
                 fontsize=7, color=theme.text_muted)
        ax.tick_params(labelsize=7)

        return style.save(fig, out_stem, theme)
