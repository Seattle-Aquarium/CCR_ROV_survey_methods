"""Check the ROV's distance sources against 100 m transect tapes.

On the diver-ROV comparison days a 100 m tape was laid on the seafloor and the
ROV flew along it, end to end, several times; the field log records when each
pass started and finished (``tape_passes.csv``). Each pass is therefore a
distance measurement with a known answer. For every pass this script measures
the distance travelled with each source ROV Imagery Processing can use, with
that program's own code (``rov_imagery_processing.metermark``):

* ``ekf_velocity``  - the autopilot's EKF velocity (LOCAL_POSITION_NED vx, vy),
  integrated at sensor rate;
* ``dvl``           - DVL odometry (VISION_POSITION_DELTA dx, dy), summed;
* ``gps_velocity``  - GLOBAL_POSITION_INT velocity, integrated;
* ``ekf_position``  - LOCAL_POSITION_NED position, differenced.

It reports each source's uncalibrated length as a share of the tape, the
calibration factor that implies (1 / median share), the factor the program
actually uses, and how far calibrated distances land from 100 m. It also asks
what 1 Hz data would have done instead of sensor-rate data: marks are placed
every metre both ways and matched to photographs taken every 3 s, and the
share of marks that end up on a different photograph is counted.

    python distance_calibration/tape_calibration.py --root path/to/flights

Telemetry comes from each flight's ``.tlog`` recordings (read with the repo's
``mcap_to_csv`` tlog reader; needs pymavlink and numpy). For each pass the one
recording with the most navigation samples inside the pass is used, so the
same messages are never counted twice.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:0] = [str(REPO / "rov_imagery_processing"), str(REPO / "mcap_to_csv")]

from ccr_m2c.tlog_read import iter_tlog, probe_tlog  # noqa: E402
from rov_imagery_processing import metermark as mm  # noqa: E402
from rov_imagery_processing.telemetry import Series, TelemetryStore  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
WANTED = ("LOCAL_POSITION_NED", "VISION_POSITION_DELTA", "GLOBAL_POSITION_INT", "ATTITUDE")
NAV_FIELDS = ("LOCAL_POSITION_NED.vx", "VISION_POSITION_DELTA.dx", "GLOBAL_POSITION_INT.vx")
FRAME_INTERVAL_S = 3.0      # GoPro time-lapse interval (Section S6 settings)
MIN_COVERAGE = 0.90         # a source must span this share of the pass to count
SOURCE_KEYS = [s.key for s in mm.SOURCES]


# ---------------------------------------------------------------------------
#  Telemetry
# ---------------------------------------------------------------------------

def _values(mt: str, f: dict):
    if mt == "LOCAL_POSITION_NED":
        for k in ("x", "y", "vx", "vy"):
            yield f"{mt}.{k}", f.get(k)
    elif mt == "VISION_POSITION_DELTA":
        pos = f.get("position_delta") or ()
        if len(pos) >= 2:
            yield f"{mt}.dx", pos[0]
            yield f"{mt}.dy", pos[1]
        yield f"{mt}.confidence", f.get("confidence")
    elif mt == "GLOBAL_POSITION_INT":
        yield f"{mt}.vx", f.get("vx")
        yield f"{mt}.vy", f.get("vy")
    elif mt == "ATTITUDE":
        yield f"{mt}.yaw", f.get("yaw")


def load_tlog(path: Path) -> TelemetryStore:
    cols: dict[str, tuple[list, list]] = {}
    for mt, fields, t in iter_tlog(path, WANTED):
        for name, v in _values(mt, fields):
            if v is None:
                continue
            ts, vs = cols.setdefault(name, ([], []))
            ts.append(t)
            vs.append(float(v))
    store = TelemetryStore()
    for name, (ts, vs) in cols.items():
        t = np.asarray(ts, float)
        v = np.asarray(vs, float)
        order = np.argsort(t, kind="stable")
        t, v = t[order], v[order]
        keep = np.concatenate([[True], np.diff(t) > 0])   # drop exact repeats
        store.series[name] = Series(t=t[keep], v=v[keep])
        lo, hi = float(t[0]), float(t[-1])
        store.t_start = lo if store.t_start is None else min(store.t_start, lo)
        store.t_end = hi if store.t_end is None else max(store.t_end, hi)
    return store


def n_samples(store: TelemetryStore, lo: float, hi: float) -> int:
    n = 0
    for name in NAV_FIELDS:
        s = store.series.get(name)
        if s is not None:
            n += int(np.count_nonzero((s.t >= lo) & (s.t <= hi)))
    return n


def coverage(store: TelemetryStore, name: str, spans) -> float:
    """Share of the active time spanned by samples no more than MAX_FILL_S apart."""
    s = store.series.get(name)
    total = sum(b - a for a, b in spans)
    if s is None or total <= 0:
        return 0.0
    covered = 0.0
    for a, b in spans:
        t = s.t[(s.t >= a) & (s.t <= b)]
        if len(t) < 2:
            continue
        t = np.concatenate([[a], t, [b]])
        dt = np.diff(t)
        covered += float(dt[dt <= mm.MAX_FILL_S].sum())
    return covered / total


SOURCE_FIELD = {"ekf_velocity": "LOCAL_POSITION_NED.vx", "dvl": "VISION_POSITION_DELTA.dx",
                "gps_velocity": "GLOBAL_POSITION_INT.vx", "ekf_position": "LOCAL_POSITION_NED.x"}


# ---------------------------------------------------------------------------
#  Sensor rate versus 1 Hz
# ---------------------------------------------------------------------------

def one_hz_track(store: TelemetryStore, track: mm.Track, spans) -> mm.Track | None:
    """The same EKF-velocity track, as a 1 Hz table would have measured it:
    the velocity held once per second and integrated over whole seconds."""
    got = mm._pair(store, "LOCAL_POSITION_NED.vx", "LOCAL_POSITION_NED.vy",
                   *mm._bounds(spans))
    if got is None:
        return None
    t, vx, vy = got
    lo, hi = mm._bounds(spans)
    t1 = np.arange(math.ceil(lo), math.floor(hi) + 1, 1.0)
    idx = np.clip(np.searchsorted(t, t1, side="right") - 1, 0, len(t) - 1)
    speed = np.hypot(vx[idx], vy[idx])
    step = speed[:-1] * 1.0
    step = np.where(mm._active_mask(t1, spans), step, 0.0)
    cum = np.concatenate([[0.0], np.cumsum(step)]) * track.source.scale
    return mm.Track(t1, np.interp(t1, track.t, track.x), np.interp(t1, track.t, track.y),
                    cum, track.source, track.xy_source, spans=track.spans)


def frames_moved(track: mm.Track, track1: mm.Track, lo: float, hi: float) -> tuple[int, int]:
    frames = [(lo + k * FRAME_INTERVAL_S, f"f{k}")
              for k in range(int((hi - lo) / FRAME_INTERVAL_S) + 2)]
    a = mm.choose_frames(mm.place_marks(track), list(frames))
    b = mm.choose_frames(mm.place_marks(track1), list(frames))
    n = min(len(a), len(b))
    moved = sum(1 for i in range(n) if a[i].stem != b[i].stem)
    return n, moved


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def epoch(day: str, hms: str) -> float:
    return datetime.fromisoformat(f"{day}T{hms}").replace(tzinfo=TZ).timestamp()


def active_spans(row) -> list[tuple[float, float]]:
    lo, hi = epoch(row["date"], row["start"]), epoch(row["date"], row["end"])
    spans = [(lo, hi)]
    for p in filter(None, (row["pauses"] or "").split(";")):
        a, b = (epoch(row["date"], x.strip()) for x in p.split("-"))
        nxt = []
        for s0, s1 in spans:
            if b <= s0 or a >= s1:
                nxt.append((s0, s1))
            else:
                if a > s0:
                    nxt.append((s0, a))
                if b < s1:
                    nxt.append((b, s1))
        spans = nxt
    return spans


def median(xs):
    xs = sorted(xs)
    return float(np.median(xs)) if xs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=os.environ.get("CCR_FLIGHTS_ROOT"),
                    help="the flights folder (or set CCR_FLIGHTS_ROOT)")
    ap.add_argument("--passes", type=Path, default=HERE / "tape_passes.csv")
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args()
    if args.root is None:
        ap.error("give the flights folder with --root, or set CCR_FLIGHTS_ROOT")
    root = Path(args.root)

    with open(args.passes, newline="", encoding="utf-8") as f:
        passes = list(csv.DictReader(f))

    stores: dict[Path, TelemetryStore] = {}
    probes: dict[Path, tuple] = {}
    results = []
    for row in passes:
        spans = active_spans(row)
        lo, hi = mm._bounds(spans)
        out = dict(row, active_s=round(sum(b - a for a, b in spans)), tlog="")
        tlogs = sorted((root / row["flight"]).rglob("*.tlog"))
        best, best_n = None, 0
        for p in tlogs:
            if p not in probes:
                probes[p] = probe_tlog(p)
            t0, t1, _ = probes[p]
            if t0 is None or t1 < lo or t0 > hi:
                continue
            if p not in stores:
                print(f"reading {p.relative_to(root)}", flush=True)
                stores[p] = load_tlog(p)
            n = n_samples(stores[p], lo, hi)
            if n > best_n:
                best, best_n = p, n
        if best is None:
            out["status"] = "no telemetry covering the pass"
            results.append(out)
            continue
        store = stores[best]
        out["tlog"] = str(best.relative_to(root / row["flight"])).replace("\\", "/")
        raw = mm.cross_check(store, spans, calibrate=False)
        for key in SOURCE_KEYS:
            cov = coverage(store, SOURCE_FIELD[key], spans)
            out[f"{key}_coverage"] = round(cov, 3)
            out[f"{key}_m"] = round(raw[key], 2) if key in raw and cov >= MIN_COVERAGE else ""
        track = mm.build_track(store, spans)
        if track is not None:
            out.update(source_used=track.source.key, calibrated_m=round(track.length, 2),
                       straightness=round(track.straightness, 3), resets_removed=track.repairs)
            if track.source.key == "ekf_velocity":
                t1 = one_hz_track(store, track, spans)
                if t1 is not None:
                    n, moved = frames_moved(track, t1, lo, hi)
                    out.update(marks_compared=n, marks_moved_1hz=moved)
        out["status"] = "ok"
        results.append(out)

    # ---- per-pass table ----------------------------------------------------
    cols = (list(passes[0].keys()) + ["active_s", "tlog", "status"]
            + [f"{k}_m" for k in SOURCE_KEYS] + [f"{k}_coverage" for k in SOURCE_KEYS]
            + ["source_used", "calibrated_m", "straightness", "resets_removed",
               "marks_compared", "marks_moved_1hz"])
    with open(args.out / "tape_calibration_passes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({c: r.get(c, "") for c in cols})

    # ---- summary -----------------------------------------------------------
    used = [r for r in results if r["use"] == "yes" and r["status"] == "ok"]
    lines = [
        "Distance sources against 100 m transect tapes",
        f"generated {datetime.now(TZ):%Y-%m-%d %H:%M} by distance_calibration/tape_calibration.py",
        "",
        f"passes logged: {len(results)}; used: {len(used)} "
        f"(excluded in tape_passes.csv: {sum(r['use'] != 'yes' for r in results)}; "
        f"no telemetry: {sum(r['use'] == 'yes' and r['status'] != 'ok' for r in results)})",
        f"days: {len({r['date'] for r in used})}",
        "",
        "source          passes  median share of tape (IQR)   implied factor  program factor  "
        "calibrated: median, MAE",
    ]
    for src in mm.SOURCES:
        vals = [float(r[f"{src.key}_m"]) / float(r["true_m"]) for r in used if r.get(f"{src.key}_m") != ""]
        if not vals:
            lines.append(f"{src.key:<15} {0:>6}")
            continue
        q1, q3 = np.percentile(vals, [25, 75])
        cal = [v * src.scale for v in vals]
        lines.append(
            f"{src.key:<15} {len(vals):>6}  {100 * median(vals):6.1f}% "
            f"({100 * q1:.1f}-{100 * q3:.1f}%)       {1 / median(vals):8.3f}        {src.scale:8.3f}"
            f"       {100 * median(cal):6.1f}%, {100 * float(np.mean(np.abs(np.array(cal) - 1))):.1f}%")
    prim = [r for r in used if r.get("calibrated_m") not in ("", None)]
    if prim:
        err = [float(r["calibrated_m"]) / float(r["true_m"]) for r in prim]
        by = {}
        for r in prim:
            by[r["source_used"]] = by.get(r["source_used"], 0) + 1
        lines += ["",
                  f"as the program measures it (first available source, calibrated), {len(prim)} passes: "
                  f"median {100 * median(err):.1f}% of the tape, mean absolute error "
                  f"{100 * float(np.mean(np.abs(np.array(err) - 1))):.1f}%; sources used: {by}"]
    both = [r for r in used if r.get("marks_compared") not in ("", None)]
    if both:
        n = sum(int(r["marks_compared"]) for r in both)
        m = sum(int(r["marks_moved_1hz"]) for r in both)
        lines += ["",
                  f"1 Hz instead of sensor rate (EKF velocity, {len(both)} passes, {n} one-metre marks, "
                  f"photographs every {FRAME_INTERVAL_S:.0f} s): {m} marks ({100 * m / n:.1f}%) "
                  f"land on a different photograph"]
    text = "\n".join(lines) + "\n"
    (args.out / "tape_calibration_summary.txt").write_text(text, encoding="utf-8")
    print("\n" + text)


if __name__ == "__main__":
    main()
