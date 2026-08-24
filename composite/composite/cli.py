"""
Headless runner.

The GUI is the intended way in, but a command line is useful for batch work, for
re-running a flight after correcting times, and for testing without a display::

    python -m composite.cli "D:/flights/2026_08_24_Centennial" \
        --site Centennial --project HSIL --date 2026-08-24 \
        --transect T1 13:12:00 13:27:30 \
        --transect T2 13:35:00 13:50:00 \
        --res 1080p --res 720p

Or reuse the plan the GUI saved::

    python -m composite.cli "D:/flights/2026_08_24_Centennial" --plan
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import discovery
from .config import AppConfig, RENDITIONS
from .pipeline import RunRequest, run
from .survey import Site, SurveyPlan, Transect

PLAN_FILENAME = "composite_plan.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="composite",
        description="Build ROV telemetry composites for a flight folder.",
    )
    p.add_argument("flight_dir", type=Path, help="the folder for one dive")
    p.add_argument("--site", help="site name")
    p.add_argument("--project", help="project name")
    p.add_argument("--date", help="survey date, YYYY-MM-DD")
    p.add_argument(
        "--transect", nargs=3, action="append", metavar=("NAME", "START", "END"),
        help="TC-25 start and end, hh:mm:ss; repeatable",
    )
    p.add_argument("--plan", action="store_true",
                   help=f"load {PLAN_FILENAME} from the flight folder")
    p.add_argument("--res", action="append", choices=sorted(RENDITIONS),
                   help="output resolution; repeatable (default 1080p)")
    p.add_argument("--no-csv", action="store_true", help="skip the 1 Hz CSV")
    p.add_argument("--force-extract", action="store_true",
                   help="ignore the cache and re-read the mcap")
    p.add_argument("--scan-only", action="store_true",
                   help="report what was found and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    flight = args.flight_dir.expanduser().resolve()

    disc = discovery.discover(flight)
    print(disc.summary())
    if args.scan_only:
        return 0 if disc.ok else 1
    print()

    if args.plan:
        plan_path = flight / PLAN_FILENAME
        if not plan_path.is_file():
            print(f"error: no {PLAN_FILENAME} in {flight}", file=sys.stderr)
            return 2
        plan = SurveyPlan.load(plan_path)
    else:
        missing = [n for n in ("site", "project", "date")
                   if not getattr(args, n)]
        if missing or not args.transect:
            print("error: need --site --project --date and at least one "
                  "--transect (or --plan)", file=sys.stderr)
            return 2
        plan = SurveyPlan([Site(
            name=args.site, project=args.project, date=args.date,
            transects=[Transect(n, s, e) for n, s, e in args.transect],
        )])

    errs = plan.validate()
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 2

    rends = tuple(args.res or ("1080p",))
    last = [-1.0]

    def progress(frac: float, msg: str) -> None:
        if frac - last[0] >= 0.005 or frac >= 1.0:
            last[0] = frac
            print(f"\r[{frac*100:5.1f}%] {msg[:88]:<88}", end="", flush=True)

    res = run(
        RunRequest(flight_dir=flight, plan=plan, renditions=rends,
                   app=AppConfig(), write_csv=not args.no_csv,
                   force_extract=args.force_extract),
        progress=progress,
    )
    print("\n")
    print(res.summary())
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
