"""Command-line flight-plan generator.

    python -m flightplan --place "Bodega Head, CA" --date 2026-08-29 \
        --float 08:00-13:00 --flight 10:00-11:00 --out out/plan.pdf

Runs the whole pipeline (scrape -> figures -> LaTeX) with no GUI, so every
piece is testable from a shell and in CI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from . import config
from .conditions import Conditions, build_conditions
from .render import Meta, render_flight_plan


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "site"


def _geocode(place: str) -> tuple[float, float, str]:
    from geopy.geocoders import Nominatim
    geo = Nominatim(user_agent=config.USER_AGENT)
    loc = geo.geocode(place, timeout=15)
    if loc is None:
        sys.exit(f"could not geocode: {place!r}")
    return loc.latitude, loc.longitude, place


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="flightplan", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    loc = p.add_mutually_exclusive_group(required=True)
    loc.add_argument("--place", help='geocoded text, e.g. "Newport, Oregon"')
    loc.add_argument("--lat", type=float, help="decimal degrees N")
    p.add_argument("--lon", type=float, help="decimal degrees E (negative W)")
    p.add_argument("--site", default="", help="display name for the location")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat(),
                   help="YYYY-MM-DD (default: tomorrow)")
    p.add_argument("--float", dest="float_w", default="-".join(config.DEFAULT_FLOAT),
                   metavar="HH:MM-HH:MM", help="on-water window")
    p.add_argument("--flight", dest="flight_w", default="-".join(config.DEFAULT_FLIGHT),
                   metavar="HH:MM-HH:MM", help="ROV flight window")
    p.add_argument("--theme", choices=("light", "dark"), default="light")
    p.add_argument("--out", type=Path, help="output PDF path")
    p.add_argument("--pilot", default="")
    p.add_argument("--tender", default="")
    p.add_argument("--rov", default="")
    p.add_argument("--vessel", default="")
    p.add_argument("--objective", default="")
    p.add_argument("--force", action="store_true", help="ignore cached responses")
    p.add_argument("--keep-tex", action="store_true", help="keep .tex and build dir")
    p.add_argument("--figures-only", action="store_true",
                   help="write the three figure files, skip LaTeX")
    args = p.parse_args(argv)
    if args.lat is not None and args.lon is None:
        p.error("--lon is required with --lat")
    return args


def _print_summary(c: Conditions) -> None:
    def line(k, v):
        print(f"  {k:<20} {v}")

    print(f"\nSITE   {c.site.name}   ({c.site.lat:.4f}, {c.site.lon:.4f})")
    print(f"DATE   {c.day:%A %d %b %Y}   on water {c.float_window.label()}   "
          f"flight {c.flight_window.label()}   {c.tz}")
    if c.tide:
        lo, hi = c.tide.range_ft()
        line("tide", f"{lo:.1f}-{hi:.1f} ft {c.tide.datum} @ {c.tide.station_name} "
                     f"({c.tide.station_id}, {c.tide.station_distance_km:.0f} km)")
        line("  highs/lows", "  ".join(f"{e.kind}{e.height_ft:.1f}@{e.time:%H:%M}"
                                       for e in c.tide.extremes
                                       if e.time.date() == c.day))
    if c.sun and c.sun.sunrise:
        line("sun", f"up {c.sun.sunrise:%H:%M}  down {c.sun.sunset:%H:%M}")
    w = c.wind_at_flight()
    if w:
        line("wind @ flight", f"{w.wind_kt:.0f} kt @ {w.wind_dir:.0f} deg  "
                              f"gust {w.gust_kt or 0:.0f} kt   [{c.weather.source}]")
    if c.weather:
        tr = c.weather.temp_range_f()
        line("air / sky", f"{tr[0]:.0f}-{tr[1]:.0f} F, {c.weather.sky_summary()}, "
                          f"PoP {c.weather.max_pop() or 0:.0f}%")
    wv = c.waves_at_flight()
    if wv:
        line("sea @ flight", f"Hs {wv.wave_ft or 0:.1f} ft, swell {wv.swell_ft or 0:.1f} ft "
                             f"@ {wv.swell_period_s or 0:.0f} s   [{c.waves.source}]")
    if c.waves and c.waves.buoy and c.waves.buoy.wvht_ft:
        b = c.waves.buoy
        line("buoy", f"{b.station_id.upper()} {b.wvht_ft:.1f} ft @ {b.dom_period_s or 0:.0f} s "
                     f"({b.distance_km:.0f} km)")
    if c.wind_field:
        line("wind field", f"{len(c.wind_field.lats)}x{len(c.wind_field.lons)} grid  "
                           f"[{c.wind_field.source}]")
    if c.alerts:
        for a in c.alerts:
            line("ALERT", f"{a.event} ({a.severity}) {a.headline}")
    else:
        line("alerts", "none active")
    if c.warnings:
        for wmsg in c.warnings:
            line("warning", wmsg)
    print()


def main(argv=None) -> int:
    args = _parse_args(argv)
    day = dt.date.fromisoformat(args.date)

    if args.place:
        lat, lon, name = _geocode(args.place)
        site = config.Site(lat, lon, args.site or name)
    else:
        site = config.Site(args.lat, args.lon, args.site)

    print(f"[flightplan] gathering conditions for {site.name} on {day} ...")
    c = build_conditions(site, day, float_window=args.float_w,
                         flight_window=args.flight_w, force=args.force)
    _print_summary(c)

    out = args.out or Path("out") / f"flight_plan_{day}_{_slug(site.name)}.pdf"

    if args.figures_only:
        from .figures.seastate import plot_sea_state
        from .figures.tide import plot_tide
        from .figures.windmap import plot_wind_map
        stem = out.with_suffix("")
        for fn in (plot_tide, plot_wind_map, plot_sea_state):
            p = fn(c, args.theme, f"{stem}_{fn.__name__.replace('plot_', '')}")
            print(f"  wrote {p}")
        return 0

    meta = Meta(pilot=args.pilot, tender=args.tender, rov=args.rov,
                vessel=args.vessel, objective=args.objective)
    pdf = render_flight_plan(c, out_pdf=out, theme=args.theme, meta=meta,
                             keep_tex=args.keep_tex)
    print(f"[flightplan] wrote {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
