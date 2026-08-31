"""Sea-state figure: a swell compass rose (height + from-direction + period)
next to a 24-hour strip of significant wave height for the flight day.

Kept separate from the tide figure on purpose -- tide is feet against a civil
clock; swell is a direction/period/height vector, and overlaying them muddies
both.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import numpy as np

from .. import brand
from ..conditions import Conditions
from ..sources.geo import compass_point
from . import style


def _arrow_from(ax, bearing_deg, r, color, lw, label=None, label_color=None):
    """Arrow entering the rose from `bearing_deg` (the direction the swell comes
    FROM), pointing at the centre."""
    th = math.radians(bearing_deg)
    ax.annotate("", xy=(th, 0.02), xytext=(th, r),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=lw,
                                shrinkA=0, shrinkB=0))
    if label:
        ax.text(th, min(r + 0.2, 1.16), label, ha="center", va="center",
                fontsize=7.4, color=label_color or color, fontweight="bold")


def plot_sea_state(c: Conditions, theme_name: str, out_stem: Path | str) -> Path | None:
    wv = c.waves
    if wv is None or not wv.hours:
        return None
    theme = brand.THEMES[theme_name]
    zone = ZoneInfo(c.tz)
    day0 = datetime(c.day.year, c.day.month, c.day.day, tzinfo=zone)
    day1 = day0 + timedelta(days=1)

    at = wv.at(c.flight_window.mid)
    buoy = wv.buoy
    # A buoy far outside the operating area is context, not a cross-check.
    buoy_near = bool(buoy and buoy.distance_km < 90 and buoy.wvht_ft)
    negligible = wv.negligible()

    with style.use_theme(theme):
        import matplotlib.pyplot as plt
        fig = plt.figure(figsize=(7.2, 3.4))
        fig.subplots_adjust(top=0.74, bottom=0.16, left=0.02, right=0.965)
        gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.55], wspace=0.30)
        rose = fig.add_subplot(gs[0, 0], projection="polar")
        strip = fig.add_subplot(gs[0, 1])

        # -- rose ------------------------------------------------------
        rose.set_theta_zero_location("N")
        rose.set_theta_direction(-1)
        rose.set_rlim(0, 1)
        rose.set_rticks([])
        rose.set_xticks(np.radians([0, 90, 180, 270]))
        rose.set_xticklabels(["N", "E", "S", "W"], fontsize=8, color=theme.text_muted)
        rose.grid(color=theme.grid, alpha=0.4, lw=0.5)
        rose.set_facecolor(theme.bg)

        ref_ft = 1.0
        for v in (at.swell_ft, at.wave_ft,
                  buoy.wvht_ft if buoy else None, buoy.swell_ft if buoy else None):
            if v:
                ref_ft = max(ref_ft, v)

        drawn = False
        if at.swell_ft and at.swell_dir_deg is not None and at.swell_ft > 0.15:
            r = 0.25 + 0.7 * min(at.swell_ft / ref_ft, 1.0)
            per = f" · {at.swell_period_s:.0f} s" if at.swell_period_s else ""
            _arrow_from(rose, at.swell_dir_deg, r, theme.accent, 3.4,
                        label=f"{at.swell_ft:.1f} ft{per}", label_color=theme.text)
            drawn = True
        if at.wind_wave_ft and at.wind_wave_ft > 0.2:
            wd = at.swell_dir_deg if at.swell_dir_deg is not None else 270
            r = 0.2 + 0.5 * min(at.wind_wave_ft / ref_ft, 1.0)
            _arrow_from(rose, wd, r, theme.accent2, 1.7)
        if buoy_near and buoy.swell_ft and buoy.swell_dir_deg is not None:
            r = 0.25 + 0.7 * min(buoy.swell_ft / ref_ft, 1.0)
            _arrow_from(rose, buoy.swell_dir_deg, r, theme.text_muted, 1.6,
                        label=f"buoy {buoy.swell_ft:.1f} ft", label_color=theme.text_muted)

        if negligible or not drawn:
            rose.text(0, 0, "negligible\nswell", ha="center", va="center",
                      fontsize=8.5, color=theme.text_muted, style="italic")

        if at.swell_dir_deg is not None and drawn:
            cap = (f"from {compass_point(at.swell_dir_deg)} "
                   f"({at.swell_dir_deg:.0f}°) at {c.flight_window.start:%H:%M}")
        else:
            cap = f"at {c.flight_window.start:%H:%M}"
        rose.annotate(cap, xy=(0.5, -0.13), xycoords="axes fraction",
                      ha="center", va="top", fontsize=7.6, color=theme.text_muted)

        # -- 24-h strip ---------------------------------------------
        t = [h.time for h in wv.hours]
        wave = np.array([np.nan if h.wave_ft is None else h.wave_ft for h in wv.hours])
        swell = np.array([np.nan if h.swell_ft is None else h.swell_ft for h in wv.hours])

        strip.axvspan(c.float_window.start, c.float_window.end,
                      color=theme.accent2, alpha=0.09, lw=0)
        strip.axvspan(c.flight_window.start, c.flight_window.end,
                      color=theme.accent, alpha=0.16, lw=0)
        strip.plot(t, wave, color=theme.accent, lw=1.8, label="sig. wave ht")
        strip.plot(t, swell, color=theme.accent2, lw=1.3, ls="--", label="swell ht")
        strip.fill_between(t, 0, wave, color=theme.accent, alpha=0.12, lw=0)

        if buoy_near:
            strip.axhline(buoy.wvht_ft, color=theme.text_muted, lw=1.0, ls=":")
            lbl = f"buoy {buoy.station_id.upper()}  {buoy.wvht_ft:.1f} ft"
            if buoy.dom_period_s:
                lbl += f" @ {buoy.dom_period_s:.0f} s"
            strip.annotate(lbl, xy=(day0, buoy.wvht_ft), xytext=(4, 3),
                           textcoords="offset points", fontsize=7,
                           color=theme.text_muted)

        finite = wave[np.isfinite(wave)]
        top = max(1.0, float(finite.max()) * 1.4) if finite.size else 1.0
        strip.set_ylim(0, top)
        strip.set_xlim(day0, day1)
        strip.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 25, 6), tz=zone))
        strip.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=zone))
        strip.set_ylabel("wave height · ft", fontsize=8.5)
        strip.grid(axis="y", lw=0.5, alpha=0.4)
        strip.legend(loc="upper left", fontsize=7, ncol=2, handlelength=1.6,
                     borderaxespad=0.3)
        strip.tick_params(labelsize=7.5)

        # -- header --------------------------------------------------
        extra = "" if wv.nws_swell_available else "  ·  NWS cell swell: n/a"
        if buoy_near:
            bd = f"  ·  buoy {buoy.station_id.upper()} ({buoy.distance_km:.0f} km)"
        elif buoy:
            bd = f"  ·  no wave buoy within 90 km (nearest {buoy.station_id.upper()}, {buoy.distance_km:.0f} km)"
        else:
            bd = "  ·  no wave buoy nearby"
        fig.suptitle("Sea state", x=0.02, ha="left", fontsize=12,
                     fontweight="bold", color=theme.heading, y=0.97)
        fig.text(0.02, 0.845, f"{c.day:%A %d %b %Y} · {wv.source}{extra}{bd}",
                 fontsize=8, color=theme.text_muted)

        return style.save(fig, out_stem, theme)
