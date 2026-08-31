"""The continuous 24-hour tide figure.

Shows the full predicted curve for the flight day with day/night shading, the
on-water (float) window, the ROV flight window, and the predicted highs/lows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from .. import brand
from ..conditions import Conditions
from . import style


def plot_tide(c: Conditions, theme_name: str, out_stem: Path | str) -> Path | None:
    if not c.tide or not c.tide.curve:
        return None
    theme = brand.THEMES[theme_name]
    zone = ZoneInfo(c.tz)
    day0 = datetime(c.day.year, c.day.month, c.day.day, tzinfo=zone)
    day1 = day0 + timedelta(days=1)

    t = [p.time for p in c.tide.curve]
    h = np.array([p.height_ft for p in c.tide.curve])

    with style.use_theme(theme):
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7.2, 3.5))
        fig.subplots_adjust(top=0.80, bottom=0.22, left=0.085, right=0.985)

        lo, hi = float(h.min()), float(h.max())
        y0, y1 = lo - 1.4, hi + 3.0
        ax.set_ylim(y0, y1)
        ax.set_xlim(day0, day1)

        # -- day / night ------------------------------------------------
        s = c.sun
        if s and s.sunrise and s.sunset:
            for a, b in ((day0, s.sunrise), (s.sunset, day1)):
                ax.axvspan(a, b, color=theme.night, alpha=0.5, lw=0, zorder=0)
            for when, lab in ((s.sunrise, f"sunrise {s.sunrise:%H:%M}"),
                              (s.sunset, f"sunset {s.sunset:%H:%M}")):
                ax.axvline(when, color=theme.text_muted, lw=0.7, ls=(0, (2, 2)), zorder=1)
                ax.annotate(lab, xy=(when, hi + 2.5), xytext=(0, 0),
                            textcoords="offset points", ha="center", va="center",
                            fontsize=7.2, color=theme.text_muted)

        # -- ebb / flood fill under the curve -------------------------
        rising = np.gradient(h) >= 0
        ax.fill_between(t, y0, h, where=rising, interpolate=True,
                        color=theme.flood, alpha=0.15, lw=0, zorder=2)
        ax.fill_between(t, y0, h, where=~rising, interpolate=True,
                        color=theme.ebb, alpha=0.15, lw=0, zorder=2)
        ax.plot(t, h, color=theme.accent, lw=1.8, zorder=5)

        # -- float (on-water) window ---------------------------------
        fw = c.float_window
        ax.axvspan(fw.start, fw.end, color=theme.accent2, alpha=0.09, lw=0, zorder=3)
        for x in (fw.start, fw.end):
            ax.axvline(x, color=theme.accent2, lw=1.0, zorder=4)
        ax.annotate(f"on water  {fw.start:%H:%M}–{fw.end:%H:%M}",
                    xy=(fw.start, y0), xytext=(4, 5), textcoords="offset points",
                    ha="left", va="bottom", fontsize=7.6, color=theme.accent2,
                    fontweight="bold")

        # -- flight window: highlight + bracket + on-curve markers ----
        gw = c.flight_window
        ax.axvspan(gw.start, gw.end, color=theme.accent, alpha=0.16, lw=0, zorder=3)
        ybr = hi + 1.35
        ax.plot([gw.start, gw.end], [ybr, ybr], color=theme.heading, lw=1.4, zorder=6)
        for x in (gw.start, gw.end):
            ax.plot([x, x], [ybr - 0.18, ybr + 0.18], color=theme.heading, lw=1.4, zorder=6)
        hs = c.tide.height_at(gw.start)
        he = c.tide.height_at(gw.end)
        st = c.tide.state_at(gw.mid)
        sub = ""
        if hs is not None and he is not None:
            sub = f"{st + ' · ' if st else ''}{hs:.1f} → {he:.1f} ft"
        ax.annotate(f"ROV flight  {gw.start:%H:%M}–{gw.end:%H:%M}",
                    xy=(gw.mid, ybr), xytext=(0, 12), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, fontweight="bold",
                    color=theme.heading)
        if sub:
            ax.annotate(sub, xy=(gw.mid, ybr), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=7.4, color=theme.text_muted)
        for x, hv in ((gw.start, hs), (gw.end, he)):
            if hv is None:
                continue
            ax.plot([x], [hv], "o", ms=6, mfc=theme.bg, mec=theme.heading,
                    mew=1.6, zorder=7)

        # -- predicted highs / lows -----------------------------------
        for e in c.tide.extremes:
            if not (day0 <= e.time <= day1):
                continue
            up = e.kind == "H"
            ax.plot([e.time], [e.height_ft], "v" if up else "^", ms=5,
                    color=theme.text_muted, zorder=6)
            ax.annotate(f"{e.kind} {e.height_ft:.1f} ft · {e.time:%H:%M}",
                        xy=(e.time, e.height_ft), xytext=(0, 8 if up else -8),
                        textcoords="offset points", ha="center",
                        va="bottom" if up else "top", fontsize=7,
                        color=theme.text_muted)

        # -- axes cosmetics ----------------------------------------
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 25, 3), tz=zone))
        ax.xaxis.set_minor_locator(mdates.HourLocator(tz=zone))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=zone))
        ax.set_ylabel(f"tide height · ft above {c.tide.datum}", fontsize=8.5)
        ax.grid(axis="y", lw=0.5, alpha=0.4)
        ax.tick_params(labelsize=8)
        ax.margins(x=0)

        dist = (f" · {c.tide.station_distance_km:.0f} km from site"
                if c.tide.station_distance_km else "")
        fig.suptitle(f"Predicted tide — {c.site.name}", x=0.085, ha="left",
                     fontsize=12, fontweight="bold", color=theme.heading, y=0.975)
        fig.text(0.085, 0.86,
                 f"{c.day:%A %d %b %Y} · NOAA {c.tide.station_name} "
                 f"({c.tide.station_id}){dist}",
                 fontsize=8, color=theme.text_muted)

        handles = [
            Line2D([], [], color=theme.accent, lw=1.8, label="predicted tide"),
            Patch(facecolor=theme.flood, alpha=0.3, label="rising"),
            Patch(facecolor=theme.ebb, alpha=0.3, label="falling"),
            Patch(facecolor=theme.accent, alpha=0.16, label="ROV flight"),
            Patch(facecolor=theme.night, alpha=0.5, label="night"),
        ]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14),
                  fontsize=7.4, ncol=5, handlelength=1.4, columnspacing=1.4,
                  borderaxespad=0.0, frameon=False)

        return style.save(fig, out_stem, theme)
