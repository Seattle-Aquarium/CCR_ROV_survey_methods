"""
surftrak_performance.py
=======================

How well ``surftrak`` held the ROV at its target altitude on real survey
transects, measured from the vehicle's own telemetry.

For every flight folder it finds, the script:

1. **Reads the transect windows** from the per-transect CSVs that
   ``tlog_to_csv.py`` (or ``mcap_to_csv``) wrote for that flight, using the
   exact seconds each CSV contains (so a paused transect keeps its gap). Folders
   often hold two copies of a transect under different naming schemes, and the
   occasional whole-flight export; overlapping windows are merged into one
   transect, and files that are derived or whole-flight exports are ignored
   (see ``SKIP_WORDS``).
2. **Reads the flight's ``.tlog`` files** and reduces them to one row per
   second: the flight mode from the autopilot's ``HEARTBEAT`` (only heartbeats
   from the submarine itself, never from a ground station), the mean
   rangefinder altitude from ``RANGEFINDER.distance`` over that second, and the
   ``surftrak`` target from the ``RFTarget`` named value when the firmware sends
   one.
3. **Scores each transect**: how many of its seconds were flown in ``SURFTRAK``
   (ArduSub mode 21), and, for those seconds, the altitude error against the
   target -- the logged ``RFTarget`` where present, otherwise the survey
   protocol's nominal 0.8 m (``--nominal``).

A second is counted as a ``surftrak`` second only when the mode was
``SURFTRAK`` *and* a rangefinder reading arrived in that second; seconds in
``SURFTRAK`` without a reading are reported separately as dropouts rather than
filled in, because a held value would make a dead sensor look like perfect
tracking.

Usage
-----
    python surftrak_performance.py
    python surftrak_performance.py --root "D:/flights" --programs HSIL Port_of_Seattle --since 2024

Outputs (default ``results/surftrak/`` in this repository)::

    surftrak_transects.csv   one row per transect
    surftrak_flights.csv     one row per flight folder (survey day and site)
    surftrak_summary.txt     pooled statistics, as reported in the manuscript
    surftrak_error_hist.png  distribution of altitude error (if matplotlib is present)

Requires ``pymavlink``, ``numpy`` and ``pandas``.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

LOCAL_TZ = ZoneInfo("America/Los_Angeles")   # transect CSV times are local
SURFTRAK = 21                                # ArduSub custom_mode for SURFTRAK
MAV_TYPE_SUBMARINE = 12

DEFAULT_ROOT = Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience\flights")
DEFAULT_PROGRAMS = ["HSIL", "Port_of_Seattle"]
REPO = Path(__file__).resolve().parents[1]

#: Transect CSVs are named ..._T<n>.csv
TRANSECT_CSV = re.compile(r"_T(\d+)\.csv$", re.I)
#: File names that mark a derived file or a whole-flight export, not a transect.
SKIP_WORDS = ("meter_marks", "with_distances", "test", "mode", "batt",
              "depth-standardized", "unknown_date", "stills")
#: Folders that hold imagery products, not telemetry.
SKIP_DIRS = ("downward", "photo", "forward", "video")
#: A transect CSV spanning longer than this is a whole-flight export.
MAX_TRANSECT_S = 45 * 60
#: A transect needs at least this many seconds to be scored.
MIN_TRANSECT_S = 60
#: A heartbeat older than this no longer says what mode the vehicle is in.
MODE_HOLD_S = 5
#: Rangefinder readings outside this range are "no bottom lock", not altitude.
ALT_VALID = (0.05, 30.0)


# --------------------------------------------------------------------------
#  Transect windows
# --------------------------------------------------------------------------


@dataclass
class Window:
    source: str
    number: int
    seconds: set[int]
    curated: bool = False      # has a Transect_ID column (current extractor output)

    @property
    def start(self) -> int:
        return min(self.seconds)

    @property
    def end(self) -> int:
        return max(self.seconds)


def _is_transect_csv(path: Path) -> bool:
    low = str(path).lower()
    if any(f"{os.sep}{d}" in low for d in SKIP_DIRS):
        return False
    name = path.name.lower()
    return bool(TRANSECT_CSV.search(path.name)) and not any(w in name for w in SKIP_WORDS)


def read_window(path: Path) -> Window | None:
    """The epoch seconds a transect CSV covers, from its Date and Time columns."""
    try:
        header = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns
        df = pd.read_csv(path, usecols=lambda c: c.strip() in ("Date", "Time"),
                         dtype=str, encoding="utf-8-sig")
    except Exception:
        return None
    curated = "Transect_ID" in [c.strip() for c in header]
    if not {"Date", "Time"} <= set(c.strip() for c in df.columns):
        return None
    df.columns = [c.strip() for c in df.columns]
    stamps = pd.to_datetime(df["Date"].str.strip() + " " + df["Time"].str.strip(),
                            errors="coerce", format="mixed")
    stamps = stamps.dropna()
    if stamps.empty:
        return None
    local = stamps.dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="NaT").dropna()
    # Epoch seconds without assuming the datetime resolution (pandas 3 parses
    # to microseconds, older pandas to nanoseconds).
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    secs = set(((local.dt.tz_convert("UTC") - epoch) // pd.Timedelta(seconds=1)).astype(int).tolist())
    if not secs or max(secs) - min(secs) > MAX_TRANSECT_S or len(secs) < MIN_TRANSECT_S:
        return None
    m = TRANSECT_CSV.search(path.name)
    return Window(str(path), int(m.group(1)) if m else 0, secs, curated)


def transect_windows(flight_dir: Path) -> list[Window]:
    """One window per transect, and every second in at most one of them.

    A flight folder often holds the same transects exported more than once:
    under two naming schemes, with windows drawn slightly differently, or all
    together in one file. Windows claim their seconds in order of preference
    -- CSVs from the current extractor (with a ``Transect_ID`` column) first,
    then the tightest windows -- and a window left with less than half of its
    seconds unclaimed is a duplicate and is dropped. Working on the seconds
    rather than the start-to-end span keeps paused transects intact: one can
    span half an hour with another transect flown inside its gap.
    """
    wins = [w for p in sorted(flight_dir.rglob("*.csv")) if _is_transect_csv(p)
            for w in [read_window(p)] if w is not None]
    # An aggregate (several transects exported as one file) contains most of a
    # much smaller window; left in, its between-transect seconds would pass
    # for a transect of their own.
    wins = [w for w in wins
            if not any(len(w.seconds) >= 1.5 * len(o.seconds)
                       and len(o.seconds & w.seconds) >= 0.8 * len(o.seconds)
                       for o in wins if o is not w)]
    claimed: set[int] = set()
    out: list[Window] = []
    for w in sorted(wins, key=lambda w: (not w.curated, len(w.seconds), w.source)):
        free = w.seconds - claimed
        if len(free) >= 0.5 * len(w.seconds) and len(free) >= MIN_TRANSECT_S:
            out.append(Window(w.source, w.number, free, w.curated))
            claimed |= free
    return sorted(out, key=lambda w: w.start)


# --------------------------------------------------------------------------
#  Telemetry
# --------------------------------------------------------------------------


def read_tlog(path: Path) -> pd.DataFrame:
    """Per-second mode, altitude and surftrak target from one .tlog."""
    from pymavlink import mavutil

    mav = mavutil.mavlink_connection(str(path), robust_parsing=True)
    types = ["HEARTBEAT", "RANGEFINDER", "NAMED_VALUE_FLOAT"]
    rows: dict[int, list] = {}          # sec -> [mode, mode_t, alt_sum, alt_n, target]
    mode = mode_t = target = None
    while True:
        try:
            msg = mav.recv_match(type=types, blocking=False)
        except Exception:
            continue
        if msg is None:
            break
        t = getattr(msg, "_timestamp", 0.0)
        if not t or t < 1.4e9:          # before 2014: not a real wall-clock stamp
            continue
        sec = int(t)
        r = rows.get(sec)
        if r is None:
            r = rows[sec] = [None, None, 0.0, 0, None]
        kind = msg.get_type()
        if kind == "HEARTBEAT":
            if msg.type == MAV_TYPE_SUBMARINE:
                mode, mode_t = int(msg.custom_mode), t
        elif kind == "RANGEFINDER":
            d = float(msg.distance)
            if ALT_VALID[0] <= d <= ALT_VALID[1] and math.isfinite(d):
                r[2] += d
                r[3] += 1
        elif kind == "NAMED_VALUE_FLOAT":
            name = msg.name.rstrip("\x00").strip() if isinstance(msg.name, str) else ""
            if name.lower() == "rftarget" and math.isfinite(msg.value) and msg.value > 0:
                target = float(msg.value)
        if mode is not None and t - mode_t <= MODE_HOLD_S:
            r[0], r[1] = mode, mode_t
        r[4] = target
    if not rows:
        return pd.DataFrame(columns=["sec", "mode", "alt", "alt_n", "target"])
    df = pd.DataFrame(
        [(s, v[0], v[2] / v[3] if v[3] else np.nan, v[3], v[4]) for s, v in rows.items()],
        columns=["sec", "mode", "alt", "alt_n", "target"])
    return df


def flight_telemetry(flight_dir: Path) -> pd.DataFrame:
    """All of a flight's tlogs as one per-second table (duplicate seconds merged)."""
    frames = []
    for p in sorted(flight_dir.rglob("*.tlog")):
        try:
            frames.append(read_tlog(p))
        except Exception as e:           # a damaged log should not stop the run
            print(f"  ! {p.name}: {e}", file=sys.stderr)
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["sec", "mode", "alt", "alt_n", "target"])
    df = pd.concat(frames, ignore_index=True)
    # The same second can appear in two logs (e.g. two ground stations): keep
    # the one with the most rangefinder samples.
    df = df.sort_values(["sec", "alt_n"], ascending=[True, False])
    return df.drop_duplicates("sec", keep="first").set_index("sec")


# --------------------------------------------------------------------------
#  Scoring
# --------------------------------------------------------------------------


@dataclass
class FlightResult:
    program: str
    flight: str
    transects: list[dict] = field(default_factory=list)
    errors: list[float] = field(default_factory=list)       # pooled per-second errors
    altitudes: list[float] = field(default_factory=list)
    all_errors: list[float] = field(default_factory=list)   # every SURFTRAK second, on or off transect
    note: str = ""


def score_flight(program: str, flight_dir: str, nominal: float) -> FlightResult:
    flight_dir = Path(flight_dir)
    res = FlightResult(program, flight_dir.name)
    tel = flight_telemetry(flight_dir)
    if tel.empty:
        res.note = "no tlog telemetry"
        return res
    every = tel[(tel["mode"] == SURFTRAK) & tel["alt"].notna()]
    res.all_errors = (every["alt"] - every["target"].fillna(nominal)).tolist()
    wins = transect_windows(flight_dir)
    if not wins:
        res.note = "no transect CSVs"
        return res
    for w in wins:
        secs = sorted(s for s in w.seconds if s in tel.index)
        if len(secs) < MIN_TRANSECT_S:
            continue                       # window not covered by this flight's logs
        t = tel.loc[secs]
        known = t["mode"].notna()
        st = t[t["mode"] == SURFTRAK]
        with_alt = st[st["alt"].notna()]
        logged = with_alt["target"].notna()
        target = with_alt["target"].where(logged, nominal)
        err = (with_alt["alt"] - target).to_numpy(dtype=float)
        row = dict(
            program=program, flight=flight_dir.name,
            date=datetime.fromtimestamp(secs[0], LOCAL_TZ).strftime("%Y-%m-%d"),
            transect=f"T{w.number}", source=os.path.relpath(w.source, flight_dir),
            start=secs[0], end=secs[-1],
            transect_s=int(known.sum()), surftrak_s=int(len(st)),
            surftrak_pct=round(100 * len(st) / max(int(known.sum()), 1), 1),
            scored_s=int(len(with_alt)), dropout_s=int(len(st) - len(with_alt)),
            target_logged_pct=round(100 * logged.mean(), 1) if len(with_alt) else np.nan,
        )
        if len(err):
            a = np.abs(err)
            alt = with_alt["alt"].to_numpy(dtype=float)
            row.update(
                median_alt=round(float(np.median(alt)), 3),
                median_target=round(float(np.median(target)), 3),
                mae=round(float(a.mean()), 3),
                within_010=round(100 * float((a <= 0.10).mean()), 1),
                within_020=round(100 * float((a <= 0.20).mean()), 1),
            )
            res.errors.extend(err.tolist())
            res.altitudes.extend(alt.tolist())
        res.transects.append(row)
    if not res.transects:
        res.note = "transect windows not covered by tlogs"
    return res


def pooled(errors: np.ndarray, altitudes: np.ndarray) -> dict:
    a = np.abs(errors)
    q1, q3 = np.percentile(altitudes, [25, 75])
    return dict(seconds=len(errors), minutes=len(errors) / 60,
                median_alt=float(np.median(altitudes)), q1_alt=float(q1), q3_alt=float(q3),
                median_err=float(np.median(errors)), mae=float(a.mean()),
                within_010=100 * float((a <= 0.10).mean()),
                within_020=100 * float((a <= 0.20).mean()))


# --------------------------------------------------------------------------
#  Main
# --------------------------------------------------------------------------


def find_flights(root: Path, programs: list[str], since: int) -> list[tuple[str, Path]]:
    out = []
    for prog in programs:
        pdir = root / prog
        if not pdir.is_dir():
            continue
        for ydir in sorted(p for p in pdir.iterdir() if p.is_dir() and p.name.isdigit()):
            if int(ydir.name) < since:
                continue
            out += [(prog, f) for f in sorted(ydir.iterdir()) if f.is_dir()]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="the flights folder")
    ap.add_argument("--programs", nargs="+", default=DEFAULT_PROGRAMS)
    ap.add_argument("--since", type=int, default=2024, help="first year to include")
    ap.add_argument("--nominal", type=float, default=0.8,
                    help="target altitude (m) when no RFTarget was logged")
    ap.add_argument("--min-surftrak-pct", type=float, default=50.0,
                    help="a transect counts as 'flown in surftrak' above this share")
    ap.add_argument("--out", type=Path, default=REPO / "results" / "surftrak")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args(argv)

    flights = find_flights(args.root, args.programs, args.since)
    print(f"{len(flights)} flight folders under {args.root}")
    results: list[FlightResult] = []
    with ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(score_flight, p, str(f), args.nominal): f for p, f in flights}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            n = sum(1 for t in r.transects if t["surftrak_pct"] >= args.min_surftrak_pct)
            print(f"  {r.program:16s} {r.flight:40s} transects={len(r.transects):2d} "
                  f"surftrak={n:2d} {r.note}")

    results.sort(key=lambda r: (r.program, r.flight))
    args.out.mkdir(parents=True, exist_ok=True)
    tr = pd.DataFrame([t for r in results for t in r.transects])
    if tr.empty:
        print("No transects found.")
        return 1
    # A transect copied into a second flight folder (with its logs) must count once.
    dup = tr.duplicated(subset=["start", "end"], keep="first")
    if dup.any():
        print(f"{int(dup.sum())} transect(s) found in more than one folder; counted once")
    tr = tr[~dup].reset_index(drop=True)
    tr.to_csv(args.out / "surftrak_transects.csv", index=False)

    flown = tr[tr["surftrak_pct"] >= args.min_surftrak_pct]
    fl = (flown.groupby(["program", "flight", "date"])
          .agg(transects=("transect", "count"), surftrak_min=("scored_s", lambda s: s.sum() / 60),
               median_alt=("median_alt", "median"), mae=("mae", "mean"),
               within_010=("within_010", "mean"), within_020=("within_020", "mean"))
          .round(3).reset_index())
    fl.to_csv(args.out / "surftrak_flights.csv", index=False)

    # Pool the per-second errors of the transects that count as flown in surftrak.
    keep = {(r, t) for r, t in zip(flown["flight"], flown["transect"])}
    errs, alts = [], []
    for r in results:
        # Rebuild per-transect errors in order; score_flight appends them in
        # transect order, so walk the same order here.
        i = 0
        for t in r.transects:
            n = t["scored_s"]
            if (r.flight, t["transect"]) in keep:
                errs += r.errors[i:i + n]
                alts += r.altitudes[i:i + n]
            i += n
    errs, alts = np.array(errs), np.array(alts)
    s = pooled(errs, alts)
    logged = flown["target_logged_pct"].fillna(0)
    lines = [
        f"surftrak performance, {', '.join(args.programs)}, {args.since} onward",
        f"generated {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M} by code/surftrak_performance.py",
        "",
        f"transects scored (any telemetry):          {len(tr)}",
        f"transects with any SURFTRAK time:           {int((tr['surftrak_s'] > 0).sum())}",
        f"transects flown in SURFTRAK (>= {args.min_surftrak_pct:.0f}% of time): {len(flown)}",
        f"survey days (flight folders) represented:   {flown['flight'].nunique()}",
        f"  by program: " + ", ".join(f"{p} {n}" for p, n in flown.groupby('program')['flight'].nunique().items()),
        f"median share of transect time in SURFTRAK:  {flown['surftrak_pct'].median():.1f}%",
        f"SURFTRAK seconds with altitude (pooled):    {s['seconds']} ({s['minutes']:.1f} min)",
        f"SURFTRAK seconds without altitude (dropout): {int(flown['dropout_s'].sum())}",
        f"target: logged RFTarget for {100 * (flown['scored_s'] * logged / 100).sum() / max(flown['scored_s'].sum(), 1):.1f}% "
        f"of seconds, nominal {args.nominal} m otherwise",
        "",
        f"altitude, median (IQR):   {s['median_alt']:.2f} m ({s['q1_alt']:.2f}-{s['q3_alt']:.2f})",
        f"error vs target, median:  {s['median_err']:+.3f} m",
        f"mean absolute error:      {s['mae']:.3f} m",
        f"within +/-0.10 m:         {s['within_010']:.1f}% of seconds",
        f"within +/-0.20 m:         {s['within_020']:.1f}% of seconds",
        "",
        "per-transect share within +/-0.20 m: "
        f"median {flown['within_020'].median():.1f}%, "
        f"range {flown['within_020'].min():.1f}-{flown['within_020'].max():.1f}%",
    ]
    every = np.array([e for r in results for e in r.all_errors])
    if len(every):
        a = np.abs(every)
        n_fl = sum(1 for r in results if r.all_errors)
        lines += [
            "",
            "all SURFTRAK time in the logs, on or off transect (sanity check):",
            f"  {len(every)} s ({len(every) / 3600:.1f} h) across {n_fl} flight folders; "
            f"within +/-0.10 m {100 * (a <= 0.10).mean():.1f}%, "
            f"within +/-0.20 m {100 * (a <= 0.20).mean():.1f}%, MAE {a.mean():.3f} m",
        ]
    quiet = [f"{r.program}/{r.flight} ({r.note})" for r in results if r.note]
    if quiet:
        lines += ["", "flight folders not scored:"] + [f"  {q}" for q in quiet]
    text = "\n".join(lines)
    (args.out / "surftrak_summary.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(5, 3.2), dpi=200)
        ax.hist(np.clip(errs, -0.6, 0.6), bins=np.arange(-0.6, 0.62, 0.02), color="#1f6f8b")
        for x in (-0.2, -0.1, 0.1, 0.2):
            ax.axvline(x, color="0.4", lw=0.8, ls=":" if abs(x) > 0.15 else "--")
        ax.set_xlabel("altitude minus surftrak target (m)")
        ax.set_ylabel("seconds")
        ax.set_title(f"{len(flown)} transects, {s['minutes']:.0f} min", fontsize=9)
        fig.tight_layout()
        fig.savefig(args.out / "surftrak_error_hist.png")
    except ImportError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
