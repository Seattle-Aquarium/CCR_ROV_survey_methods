"""
Command line, for scripted runs and for looking inside a recording.

    python -m ccr_m2c --inspect logs/*.mcap
    python -m ccr_m2c logs/*.mcap --site Centennial_Park --date 20260826 \
        --out ./out --transect "EBM_S24_T1=10:07:41-10:13:50,10:35:52-10:40:07"
    python -m ccr_m2c --map out/transects/*.csv --out out

``--inspect`` is the one to reach for first on an unfamiliar recording: it
prints the local clock span (which is what the transect windows have to be
written in) and which telemetry the file actually contains, without doing any
of the work.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

from .health import read_health
from .vehicle import read_vehicle
from .mcap_read import probe_mcaps, read_mcaps
from .mapping import write_map_from_csvs
from .pipeline import TransectSpec, run
from .survey import load_plan
from .tide import STATIONS

log = logging.getLogger(__name__)

#: Progress is drawn on one rewritten line of stderr, so it never breaks up the
#: report on stdout and `> run.log` captures the report without the spinner.
_CR = "\r"
_CLEAR = " " * 78
NL = "\n"


def _spin(fraction: float, message: str) -> None:
    print(f"  {fraction * 100:5.1f}%  {message:<60}", end=_CR, file=sys.stderr)


def _expand(patterns: list[str]) -> list[Path]:
    """Expand shell globs ourselves -- cmd.exe does not."""
    out: list[Path] = []
    for pat in patterns:
        hits = [Path(p) for p in glob.glob(pat)] or ([Path(pat)] if Path(pat).exists() else [])
        if not hits:
            log.warning("no files matched %s", pat)
        out.extend(hits)
    seen, unique = set(), []
    for p in out:
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(p)
    return unique


def _parse_transect(spec: str) -> TransectSpec:
    """``ID=HH:MM:SS-HH:MM:SS[,HH:MM:SS-HH:MM:SS]`` -> a TransectSpec."""
    if "=" not in spec:
        raise argparse.ArgumentTypeError(
            f"--transect needs ID=start-end, got {spec!r}")
    name, _, windows = spec.partition("=")
    pairs: list[tuple[str, str]] = []
    for chunk in windows.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, sep, end = chunk.partition("-")
        if not sep:
            raise argparse.ArgumentTypeError(
                f"time window needs start-end, got {chunk!r}")
        pairs.append((start.strip(), end.strip()))
    if not pairs:
        raise argparse.ArgumentTypeError(f"no time windows in {spec!r}")
    return TransectSpec(name.strip(), pairs)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccr_m2c",
        description="Turn BlueOS .mcap recordings into per-transect CSVs and a map.")
    p.add_argument("inputs", nargs="*",
                   help=".mcap files or globs; --params also accepts .BIN logs")
    p.add_argument("--inspect", action="store_true",
                   help="print what the recordings contain, then exit")
    p.add_argument("--params", action="store_true",
                   help="report the vehicle's firmware and parameters, then exit; "
                        "give it the .BIN logs as well as the .mcap files, since "
                        "the .BIN carries the complete parameter set")
    p.add_argument("--grep", metavar="TEXT",
                   help="with --params, show only parameters whose name contains "
                        "this")
    p.add_argument("--all", action="store_true",
                   help="with --params, show every parameter rather than the "
                        "notable ones")
    p.add_argument("--health", action="store_true",
                   help="report what the EKF was using and how the sensors behaved, "
                        "then exit")
    p.add_argument("--map", dest="map_csvs", nargs="+", metavar="CSV",
                   help="build a map from transect CSVs that already exist, then exit")
    p.add_argument("--site", default="", help="site name written into every row")
    p.add_argument("--date", default="", help="survey date, YYYYMMDD (for the tide lookup)")
    p.add_argument("--station", default=STATIONS[0][1],
                   help="NOAA tide station id (default %(default)s = Elliott Bay)")
    p.add_argument("--out", default=".", help="save location; CSVs land in <out>/transects")
    p.add_argument("--transect", action="append", type=_parse_transect, default=[],
                   metavar="ID=START-END", help="repeatable; omit to process the whole log")
    p.add_argument("--plan", metavar="JSON",
                   help="survey plan (the same file UTC uses); supplies the site, "
                        "date and transects, so --site/--date/--transect are not needed")
    p.add_argument("--prefix-site", action="store_true",
                   help="with --plan, name the CSVs <site>_<transect> rather than "
                        "<transect>; use when one folder holds several sites")
    p.add_argument("--no-map", action="store_true", help="skip the Leaflet map")
    p.add_argument("--no-tide", action="store_true",
                   help="skip the NOAA lookup; Depth_std is left blank")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _inspect(paths: list[Path]) -> int:
    infos = probe_mcaps(paths)
    for i in infos:
        print(f"\n{i.path.name}")
        print(f"  {i.local_span()}")
        if i.error:
            print(f"  ERROR: {i.error}")
        else:
            print(f"  {i.messages:,} messages, {i.path.stat().st_size / 1e6:,.0f} MB")

    good = [i.path for i in infos if i.usable]
    if not good:
        return 1

    print("\nreading telemetry...")
    res = read_mcaps(good, progress=lambda f, m: print(
        f"  {f * 100:5.1f}%  {m:<60}", end="\r", file=sys.stderr))
    df = res.df
    print(" " * 78, end="\r", file=sys.stderr)
    print(f"\n{len(df):,} seconds  "
          f"{df['Date'].iloc[0]} {df['Time'].iloc[0]} - {df['Time'].iloc[-1]} local")
    print(f"DVL track source: {res.dvl_source}")
    print("depth sources: " + (", ".join(f"{k} x{v}" for k, v in res.depth_sources.items())
                               or "none"))
    print("\nmessage types used:")
    for k, v in sorted(res.types_seen.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>9,}  {k}")

    print("\ncolumn coverage (non-empty rows):")
    for col in df.columns:
        n = int(df[col].notna().sum())
        bar = "#" * int(30 * n / max(1, len(df)))
        print(f"  {col:<20}{n:>7,}  {bar}")

    for w in res.warnings:
        print(f"  ! {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if args.map_csvs:
        csvs = _expand(args.map_csvs)
        if not csvs:
            print("no CSVs matched", file=sys.stderr)
            return 1
        out = Path(args.out) / "transect_map.html"
        path, warns = write_map_from_csvs(csvs, out, site_name=args.site,
                                          survey_date=args.date)
        for w in warns:
            print(f"  ! {w}")
        print(f"Map written: {path}")
        return 0

    paths = _expand(args.inputs)
    if not paths:
        print("no .mcap files given", file=sys.stderr)
        return 2

    if args.params:
        rep = read_vehicle(paths, progress=_spin)
        print(_CLEAR, end=_CR, file=sys.stderr)
        print(NL.join(rep.lines(grep=args.grep or "", full=args.all)))
        return 0

    if args.health:
        # With a plan or --transect, the report also scopes itself to the
        # transects: most of a dive is transit, and whole-dive dropout
        # counts say nothing about the part being analysed.
        specs = list(args.transect)
        if args.plan:
            try:
                for site in load_plan(args.plan).sites:
                    specs.extend(site.transects)
            except (ValueError, AttributeError) as ex:
                print(f"  ! could not read the plan: {ex}", file=sys.stderr)
        rep = read_health(paths, transects=specs, progress=_spin)
        print(_CLEAR, end=_CR, file=sys.stderr)
        print("\n".join(rep.lines()))
        return 0

    if args.inspect:
        return _inspect(paths)

    # Progress goes to stderr and the log to stdout, so the carriage returns of
    # the one do not chew through the lines of the other -- and `> run.log`
    # captures the report without the spinner.
    spin = lambda f, m: print(f"  {f * 100:5.1f}%  {m:<60}", end="\r", file=sys.stderr)

    if args.plan:
        try:
            plan = load_plan(args.plan)
        except ValueError as ex:
            print(ex, file=sys.stderr)
            return 2
        for w in plan.warnings:
            print(f"  ! {w}", file=sys.stderr)

        failed = False
        for site in plan.sites:
            print(f"\n=== {site} ===")
            # Each site gets its own folder: the map is drawn per site, and one
            # map spanning two locations is mostly empty ocean.
            out = Path(args.out) / site.name if len(plan.sites) > 1 else Path(args.out)
            transects = site.transects
            if args.prefix_site:
                transects = [TransectSpec(f"{site.name}_{t.transect_id}", t.windows)
                             for t in transects]
            result = run(
                paths,
                site_name=site.name,
                survey_date=site.survey_date,
                station_id=None if args.no_tide else args.station,
                save_location=out,
                transects=transects,
                make_map=not args.no_map,
                progress=_spin,
            )
            print(file=sys.stderr)
            print("\n".join(result.summary_lines()))
            failed = failed or not result.saved
        return 1 if failed else 0

    if not args.site:
        print("--site is required (or use --plan, or --inspect)", file=sys.stderr)
        return 2
    if not args.date and not args.no_tide:
        print("--date is required for the tide lookup (or pass --no-tide)", file=sys.stderr)
        return 2

    result = run(
        paths,
        site_name=args.site,
        survey_date=args.date,
        station_id=None if args.no_tide else args.station,
        save_location=args.out,
        transects=args.transect,
        make_map=not args.no_map,
        progress=_spin,
    )
    print(file=sys.stderr)
    print("\n".join(result.summary_lines()))
    return 0 if result.saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
