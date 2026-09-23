"""
Distance marks along a transect, and the frame that belongs to each one.

A transect is a run along a depth contour. The question this answers is "which
photograph was taken at meter 14?", which needs two things the rest of the
program does not already provide: a trustworthy running distance, and a rule
for handing out frames so that no two marks claim the same one.

Everything here works at **sensor rate**, off the full-rate series in
``TelemetryStore``. That matters more than it sounds. Measuring distance from
positions already resampled to one row per second throws away the motion inside
each second before it is ever counted; measured against the 100 m tape
transects that moved a third of the marks far enough to select a different
photograph. Accumulating at sensor rate and only rounding when marks are
written out puts that figure near zero.

Which channel the distance comes from is not a fixed choice, because the
channels are not equally available -- a quarter of the recordings in the
2024-26 archive have no ``LOCAL_POSITION_NED`` at all. Sources are tried in
order and the first one present wins, each carrying its own calibration
constant measured against the tape (see ``SOURCES``).

A paused transect arrives here as several active spans under one name. The
geometry is built across the whole transect so the track stays continuous, but
only steps inside an active span add distance: hovering through a pause is not
survey progress, and counting it would push every later mark downstream.
"""

from __future__ import annotations

import bisect
import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import layout
from .telemetry import TelemetryStore

#: Default spacing between marks, in meters.
DEFAULT_INTERVAL_M = 1.0

#: A frame further than this from a mark is reported rather than used. Capture
#: cadence is about 3 s, so this is several frames' worth of slack.
DEFAULT_MAX_OFFSET_S = 15.0

#: Longest telemetry gap the repair pass will synthesize across. Beyond this
#: the travel is unrecoverable and inventing it would mislead.
MAX_FILL_S = 10.0

#: A step larger than this multiple of what the vehicle's own speed allows is
#: a filter reset, not travel. Resets are GPS corrections snapping the EKF
#: position; they arrive at a perfectly normal sample interval, so only a
#: speed-based test can see them.
JUMP_FACTOR = 4.0
MIN_JUMP_M = 0.25

#: Below this the sources are treated as agreeing. Above it at least one is
#: wrong and the transect's length should not be trusted.
SPREAD_WARN = 0.10

#: Net displacement over path length. A contour-following transect sits near
#: 1; well below means the window holds maneuvering rather than the run.
STRAIGHTNESS_WARN = 0.50


# --------------------------------------------------------------------------
#  Where distance comes from
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DistanceSource:
    """One way of measuring how far the vehicle traveled.

    ``scale`` brings the source onto the same footing as the others. Each was
    measured against the 19 usable 100 m tape transects in the 2024-26
    archive; the median ratio of measured to true is inverted to give the
    factor here.
    """

    key: str
    label: str
    scale: float
    note: str


#: Preference order. The first source whose inputs are present is used.
SOURCES: tuple[DistanceSource, ...] = (
    DistanceSource(
        "ekf_velocity", "EKF velocity", 1.010,
        "LOCAL_POSITION_NED vx/vy integrated; measured 99.0% of the tape"),
    DistanceSource(
        "dvl", "DVL odometer", 1.037,
        "VISION_POSITION_DELTA magnitudes summed; measured 96.5% of the tape"),
    DistanceSource(
        "gps_velocity", "GLOBAL_POSITION_INT velocity", 1.058,
        "fallback when LOCAL_POSITION_NED is absent; reads ~4.5% under it"),
    DistanceSource(
        "ekf_position", "EKF position", 0.979,
        "last resort; inflated by position noise and filter resets"),
)

SOURCES_BY_KEY = {s.key: s for s in SOURCES}

F_NED_X, F_NED_Y = "LOCAL_POSITION_NED.x", "LOCAL_POSITION_NED.y"
F_NED_VX, F_NED_VY = "LOCAL_POSITION_NED.vx", "LOCAL_POSITION_NED.vy"
F_DVL_DX, F_DVL_DY = "VISION_POSITION_DELTA.dx", "VISION_POSITION_DELTA.dy"
F_DVL_CONF = "VISION_POSITION_DELTA.confidence"
F_YAW = "ATTITUDE.yaw"
F_GPI_VX, F_GPI_VY = "GLOBAL_POSITION_INT.vx", "GLOBAL_POSITION_INT.vy"

Span = tuple[float, float]


def _bounds(spans: Sequence[Span]) -> Span:
    return min(s[0] for s in spans), max(s[1] for s in spans)


def _series(store: TelemetryStore, name: str):
    s = store.series.get(name)
    if s is None or s.v is None or len(s.t) == 0:
        return None
    t = np.asarray(s.t, dtype=float)
    v = np.asarray(s.v, dtype=float)
    ok = np.isfinite(v)
    if ok.sum() < 2:
        return None
    return t[ok], v[ok]


def _pair(store: TelemetryStore, nx: str, ny: str, lo: float, hi: float):
    """Two fields on one timeline, clipped to [lo, hi]."""
    a, b = _series(store, nx), _series(store, ny)
    if a is None or b is None:
        return None
    t = a[0]
    keep = (t >= lo) & (t <= hi)
    if keep.sum() < 3:
        return None
    t = t[keep]
    return t, a[1][keep], np.interp(t, b[0], b[1])


def _active_mask(t: np.ndarray, spans: Sequence[Span]) -> np.ndarray:
    """True for each step whose midpoint falls inside a surveying stretch."""
    if len(t) < 2:
        return np.zeros(max(len(t) - 1, 0), dtype=bool)
    mid = 0.5 * (t[:-1] + t[1:])
    keep = np.zeros(len(mid), dtype=bool)
    for lo, hi in spans:
        keep |= (mid >= lo) & (mid <= hi)
    return keep


def available_sources(store: TelemetryStore, spans: Sequence[Span]) -> list[str]:
    """Keys of the sources that can actually be built for this transect."""
    return [s.key for s in SOURCES
            if _raw_source(store, s.key, spans) is not None]


def _raw_source(store: TelemetryStore, key: str, spans: Sequence[Span]):
    """(t, step) — per-interval distance, before calibration. None if absent."""
    lo, hi = _bounds(spans)
    if key == "ekf_velocity":
        got = _pair(store, F_NED_VX, F_NED_VY, lo, hi)
        if got is None:
            return None
        t, vx, vy = got
        return t, _integrate_speed(t, np.hypot(vx, vy))
    if key == "gps_velocity":
        got = _pair(store, F_GPI_VX, F_GPI_VY, lo, hi)
        if got is None:
            return None
        t, vx, vy = got
        # GLOBAL_POSITION_INT reports centimeters per second.
        return t, _integrate_speed(t, np.hypot(vx, vy) / 100.0)
    if key == "dvl":
        got = _pair(store, F_DVL_DX, F_DVL_DY, lo, hi)
        if got is None:
            return None
        t, dx, dy = got
        # The delta is travel since the previous message, so it already is a
        # step: no differencing, and no dependence on heading.
        return t, np.hypot(dx, dy)[1:]
    if key == "ekf_position":
        got = _pair(store, F_NED_X, F_NED_Y, lo, hi)
        if got is None:
            return None
        t, x, y = got
        return t, np.hypot(np.diff(x), np.diff(y))
    raise ValueError(f"unknown distance source {key!r}")


def _integrate_speed(t: np.ndarray, speed: np.ndarray) -> np.ndarray:
    """Trapezoidal travel between consecutive samples."""
    dt = np.diff(t)
    step = 0.5 * (speed[:-1] + speed[1:]) * dt
    # A gap is not travel we measured; the repair pass decides what to do.
    return np.where(dt < MAX_FILL_S, step, 0.0)


# --------------------------------------------------------------------------
#  The track
# --------------------------------------------------------------------------


@dataclass
class Track:
    """Where the vehicle was, and how far it had gone, at sensor rate."""

    t: np.ndarray                 # epoch seconds
    x: np.ndarray                 # meters north of the transect's first sample
    y: np.ndarray                 # meters east
    s: np.ndarray                 # cumulative surveyed distance, calibrated
    source: DistanceSource
    xy_source: str
    spans: tuple[Span, ...] = ()
    repairs: int = 0
    phantom_m: float = 0.0
    gaps_s: float = 0.0
    paused_s: float = 0.0

    @property
    def length(self) -> float:
        return float(self.s[-1]) if len(self.s) else 0.0

    @property
    def duration(self) -> float:
        """Surveying time only; a pause is not part of the transect."""
        return float(sum(hi - lo for lo, hi in self.spans)) or (
            float(self.t[-1] - self.t[0]) if len(self.t) > 1 else 0.0)

    @property
    def net(self) -> float:
        if len(self.x) < 2:
            return 0.0
        return float(math.hypot(self.x[-1] - self.x[0], self.y[-1] - self.y[0]))

    @property
    def straightness(self) -> float:
        return self.net / self.length if self.length > 0 else 0.0

    @property
    def mean_speed(self) -> float:
        return self.length / self.duration if self.duration > 0 else 0.0


def _xy_track(store: TelemetryStore, lo: float, hi: float):
    """(t, x, y, label) for the geometry, independent of the distance source.

    Prefers the EKF's own local frame; falls back to integrating the DVL
    deltas and rotating them by heading, which is how ``DVLx``/``DVLy`` are
    built elsewhere. Spans the whole transect, pause included, so the drawn
    track does not tear where the survey stopped.
    """
    got = _pair(store, F_NED_X, F_NED_Y, lo, hi)
    if got is not None:
        t, x, y = got
        return t, x - x[0], y - y[0], "LOCAL_POSITION_NED"

    got = _pair(store, F_DVL_DX, F_DVL_DY, lo, hi)
    if got is None:
        return None
    t, dx, dy = got
    yaw_s = _series(store, F_YAW)
    yaw = (np.interp(t, yaw_s[0], np.unwrap(yaw_s[1])) if yaw_s is not None
           else np.zeros_like(t))
    c, sn = np.cos(yaw), np.sin(yaw)
    return (t, np.cumsum(dx * c - dy * sn), np.cumsum(dx * sn + dy * c),
            "VISION_POSITION_DELTA")


def _repair(t: np.ndarray, step: np.ndarray, speed: np.ndarray | None):
    """Drop filter resets and fill short dropouts. Returns (step, stats)."""
    if len(step) == 0:
        return step, (0, 0.0, 0.0)
    dt = np.diff(t)
    if speed is not None and len(speed) == len(t):
        expected = 0.5 * (speed[:-1] + speed[1:]) * dt
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = step / np.where(dt > 0, dt, np.nan)
        med = float(np.nanmedian(rate)) if len(rate) else 0.0
        med = 0.0 if not math.isfinite(med) else med
        expected = med * dt

    out = step.astype(float).copy()
    jump = out > np.maximum(expected * JUMP_FACTOR, MIN_JUMP_M)
    phantom = float((out[jump] - expected[jump]).sum())
    out[jump] = expected[jump]

    hole = dt > MAX_FILL_S
    gaps = float(dt[hole].sum())
    fill = (~hole) & (dt > 1.0) & (out < expected * 0.5)
    out[fill] = expected[fill]
    return out, (int(jump.sum() + fill.sum()), phantom, gaps)


def build_track(
    store: TelemetryStore,
    spans: Sequence[Span],
    *,
    source: str | None = None,
    calibrate: bool = True,
) -> Track | None:
    """Reconstruct one transect. None when nothing usable is recorded."""
    spans = tuple(sorted(spans))
    lo, hi = _bounds(spans)
    geo = _xy_track(store, lo, hi)
    if geo is None:
        return None
    gt, gx, gy, xy_label = geo

    paused = max(0.0, (hi - lo) - sum(b - a for a, b in spans))

    for key in ([source] if source else [s.key for s in SOURCES]):
        raw = _raw_source(store, key, spans)
        if raw is None:
            continue
        st, step = raw
        speed = None
        if key in ("ekf_velocity", "gps_velocity"):
            fields = ((F_NED_VX, F_NED_VY) if key == "ekf_velocity"
                      else (F_GPI_VX, F_GPI_VY))
            got = _pair(store, *fields, lo, hi)
            if got is not None:
                div = 1.0 if key == "ekf_velocity" else 100.0
                speed = np.hypot(got[1], got[2]) / div
        step, (n_rep, phantom, gaps) = _repair(st, step, speed)

        # Only surveying stretches add distance. The pause still appears in
        # the geometry, so the track is drawn whole.
        step = np.where(_active_mask(st, spans), step, 0.0)

        src = SOURCES_BY_KEY[key]
        cum = np.concatenate([[0.0], np.cumsum(step)])
        if calibrate:
            cum = cum * src.scale
        # Put distance on the geometry's timeline so marks carry coordinates.
        return Track(gt, gx, gy, np.interp(gt, st, cum), src, xy_label,
                     spans=spans, repairs=n_rep, phantom_m=phantom,
                     gaps_s=gaps, paused_s=paused)
    return None


def cross_check(store: TelemetryStore, spans: Sequence[Span],
                *, calibrate: bool = True) -> dict[str, float]:
    """Calibrated length from every source this transect supports."""
    out: dict[str, float] = {}
    for src in SOURCES:
        raw = _raw_source(store, src.key, spans)
        if raw is None:
            continue
        st, step = raw
        step, _stats = _repair(st, step, None)
        step = np.where(_active_mask(st, spans), step, 0.0)
        out[src.key] = float(np.sum(step)) * (src.scale if calibrate else 1.0)
    return out


def spread(lengths: dict[str, float]) -> float:
    """Relative disagreement between sources; 0 when fewer than two."""
    vals = [v for v in lengths.values() if v > 0]
    if len(vals) < 2:
        return 0.0
    return (max(vals) - min(vals)) / (sum(vals) / len(vals))


# --------------------------------------------------------------------------
#  Marks
# --------------------------------------------------------------------------


@dataclass
class Mark:
    number: int
    distance_m: float
    epoch: float
    x: float
    y: float
    quality: str = "good"
    note: str = ""


def place_marks(track: Track, interval: float = DEFAULT_INTERVAL_M) -> list[Mark]:
    """One mark every `interval` meters along the track."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if track.length < interval:
        return []

    targets = np.arange(interval, track.length + 1e-9, interval)
    s, t = track.s, track.t
    epochs = np.interp(targets, s, t)
    xs = np.interp(targets, s, track.x)
    ys = np.interp(targets, s, track.y)

    # A mark landing inside a telemetry gap was interpolated across data that
    # was never recorded, so say so rather than presenting it as measured.
    gap_at = np.zeros(len(t), dtype=bool)
    if len(t) > 1:
        gap_at[:-1] = np.diff(t) > MAX_FILL_S
    suspect = np.interp(epochs, t, gap_at.astype(float)) > 0

    return [
        Mark(i + 1, float(d), float(e), float(x), float(y),
             "poor" if bad else "good",
             "interpolated across a telemetry gap" if bad else "")
        for i, (d, e, x, y, bad) in enumerate(
            zip(targets, epochs, xs, ys, suspect, strict=True))
    ]


# --------------------------------------------------------------------------
#  Handing out frames
# --------------------------------------------------------------------------


@dataclass
class MarkMatch:
    mark: Mark
    stem: str | None = None
    offset_s: float = float("nan")

    @property
    def matched(self) -> bool:
        return self.stem is not None


def choose_frames(
    marks: Sequence[Mark],
    frames: Sequence[tuple[float, str]],
    *,
    max_offset: float | None = DEFAULT_MAX_OFFSET_S,
) -> list[MarkMatch]:
    """Give each mark its nearest frame in time, at most one mark per frame.

    Frames are consumed as they are matched. Without that, a run of marks
    closer together than the capture cadence all resolve to the same frame and
    every one after the first is silently left with nothing.
    """
    pool = sorted(frames)
    times = [t for t, _ in pool]
    out: list[MarkMatch] = []

    for mark in marks:
        if not times:
            out.append(MarkMatch(mark))
            continue
        i = bisect.bisect_left(times, mark.epoch)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(times):
                d = abs(times[j] - mark.epoch)
                if best is None or d < best[0]:
                    best = (d, j)
        d, j = best
        if max_offset is not None and d > max_offset:
            out.append(MarkMatch(mark))
            continue
        times.pop(j)
        out.append(MarkMatch(mark, pool.pop(j)[1], d))
    return out


# --------------------------------------------------------------------------
#  One transect, start to finish
# --------------------------------------------------------------------------


@dataclass
class Reconstruction:
    name: str
    track: Track | None
    marks: list[Mark] = field(default_factory=list)
    matches: list[MarkMatch] = field(default_factory=list)
    lengths: dict[str, float] = field(default_factory=dict)
    coverage: float = 0.0
    requested_s: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.track is not None and bool(self.marks)

    @property
    def matched(self) -> int:
        return sum(1 for m in self.matches if m.matched)

    def summary(self) -> str:
        if self.track is None:
            return f"{self.name}: no usable telemetry in this window"
        t = self.track
        bits = [
            f"{self.name}: {t.length:.1f} m in {t.duration/60:.1f} min "
            f"({t.mean_speed:.2f} m/s)",
            f"  source {t.source.label} (x{t.source.scale:.3f}), "
            f"geometry from {t.xy_source}",
            f"  {len(self.marks)} mark(s), {self.matched} matched to frames",
            f"  straightness {t.straightness:.2f}, "
            f"coverage {self.coverage*100:.0f}%",
        ]
        if t.paused_s > 1:
            bits.append(f"  {t.paused_s:.0f}s of pause excluded from distance")
        if len(self.lengths) > 1:
            other = ", ".join(f"{SOURCES_BY_KEY[k].label} {v:.1f}"
                              for k, v in self.lengths.items()
                              if k != t.source.key)
            bits.append(f"  cross-check: {other}  "
                        f"(spread {spread(self.lengths)*100:.1f}%)")
        return "\n".join(bits)


def reconstruct(
    store: TelemetryStore,
    name: str,
    spans: Sequence[Span],
    *,
    interval: float = DEFAULT_INTERVAL_M,
    source: str | None = None,
    frames: Sequence[tuple[float, str]] = (),
    max_offset: float | None = DEFAULT_MAX_OFFSET_S,
) -> Reconstruction:
    """Measure one transect over its surveying spans and give marks frames."""
    spans = tuple(sorted(spans))
    surveyed = sum(hi - lo for lo, hi in spans)
    rec = Reconstruction(name=name, track=None, requested_s=max(surveyed, 0.0))

    track = build_track(store, spans, source=source)
    if track is None:
        rec.warnings.append("no position or velocity telemetry inside this window")
        return rec

    rec.track = track
    covered = 0.0
    if len(track.t) > 1:
        dt = np.diff(track.t)
        covered = float(dt[_active_mask(track.t, spans) & (dt < MAX_FILL_S)].sum())
    rec.coverage = covered / surveyed if surveyed else 0.0
    rec.lengths = cross_check(store, spans)
    rec.marks = place_marks(track, interval)
    rec.matches = choose_frames(rec.marks, frames, max_offset=max_offset)

    if rec.coverage < 0.95:
        rec.warnings.append(
            f"telemetry covers only {rec.coverage*100:.0f}% of the surveyed "
            f"time; marks after the gap will read short")
    if track.straightness < STRAIGHTNESS_WARN and track.length > 10:
        rec.warnings.append(
            f"straightness {track.straightness:.2f} is low for a contour "
            f"transect - the window may include the transit to the start, or "
            f"two passes")
    sp = spread(rec.lengths)
    if sp > SPREAD_WARN:
        rec.warnings.append(
            f"distance sources disagree by {sp*100:.0f}% - treat this "
            f"transect's length as uncertain")
    if track.phantom_m > 0.5:
        rec.warnings.append(
            f"{track.repairs} filter reset(s) removed, "
            f"{track.phantom_m:.1f} m of phantom travel")
    if track.gaps_s > MAX_FILL_S:
        rec.warnings.append(
            f"{track.gaps_s:.0f}s of telemetry missing inside the window")
    unmatched = [m.mark.number for m in rec.matches if not m.matched]
    if unmatched:
        shown = ", ".join(str(n) for n in unmatched[:10])
        rec.warnings.append(
            f"{len(unmatched)} mark(s) had no frame within "
            f"{max_offset:.0f}s: {shown}{'...' if len(unmatched) > 10 else ''}")
    return rec


# --------------------------------------------------------------------------
#  Writing it out
# --------------------------------------------------------------------------

CSV_COLUMNS = (
    "Transect_ID", "Mark_number", "Distance_m", "Time", "Datetime_UTC",
    "Epoch", "Image", "Image_offset_s", "DVLx", "DVLy",
    "Distance_source", "Source_scale", "Quality", "Note",
)

#: Filenames written beside each transect's imagery.
CSV_SUFFIX = "_meter_marks.csv"
PNG_SUFFIX = "_track.png"


def write_marks_csv(rec: Reconstruction, path: Path, *, tz=None) -> Path:
    """One row per mark. Column naming follows the transect CSVs."""
    from datetime import datetime, timezone  # noqa: PLC0415

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    src = rec.track.source if rec.track else None

    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for m in rec.matches:
            dt_utc = datetime.fromtimestamp(m.mark.epoch, timezone.utc)
            local = dt_utc.astimezone(tz) if tz else dt_utc
            w.writerow([
                rec.name, m.mark.number, f"{m.mark.distance_m:.3f}",
                local.strftime("%H:%M:%S"),
                dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
                f"{m.mark.epoch:.3f}",
                m.stem or "", "" if not m.matched else f"{m.offset_s:.2f}",
                f"{m.mark.x:.3f}", f"{m.mark.y:.3f}",
                src.key if src else "", f"{src.scale:.4f}" if src else "",
                m.mark.quality, m.mark.note,
            ])
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------
#  Running it over a whole flight
# --------------------------------------------------------------------------


@dataclass
class MarkOptions:
    """What the import page's checkboxes turn into."""

    enabled: bool = False
    interval_m: float = DEFAULT_INTERVAL_M
    write_csv: bool = False
    write_png: bool = False
    #: File the frames that landed on a mark into <folder>/meters/.
    move_gpr: bool = True
    #: The same for JPG_edited. Off by default: those frames feed downstream
    #: ML, and moving them changes where it has to look.
    move_jpg: bool = False
    #: None means "use the preference order in SOURCES".
    source: str | None = None
    max_offset_s: float = DEFAULT_MAX_OFFSET_S


@dataclass
class MarkReport:
    reconstructions: list[Reconstruction] = field(default_factory=list)
    csv_paths: list[Path] = field(default_factory=list)
    png_paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    filed: int = 0

    @property
    def marks(self) -> int:
        return sum(len(r.marks) for r in self.reconstructions)

    @property
    def matched(self) -> int:
        return sum(r.matched for r in self.reconstructions)

    def summary(self) -> str:
        if not self.reconstructions:
            return "Meter marks: nothing to do"
        lines = [f"Meter marks: {self.marks} across "
                 f"{len(self.reconstructions)} transect(s), "
                 f"{self.matched} matched to frames"]
        for r in self.reconstructions:
            lines.append("  " + r.summary().replace("\n", "\n  "))
        if self.csv_paths:
            lines.append(f"  {len(self.csv_paths)} CSV file(s) written")
        if self.png_paths:
            lines.append(f"  {len(self.png_paths)} PNG file(s) written")
        if self.filed:
            lines.append(f"  {self.filed} frame(s) filed into "
                         f"{layout.METERS}/")
        return "\n".join(lines)


def group_windows(windows: Sequence[tuple[str, float, float]],
                  ) -> list[tuple[str, list[Span]]]:
    """Collect a plan's windows into one entry per transect, order kept.

    ``plan_windows(exclude_pauses=True)`` emits one window per surveying
    stretch, so a paused transect arrives as several rows sharing a name.
    They are one transect and must be measured as one.
    """
    order: list[str] = []
    spans: dict[str, list[Span]] = {}
    for name, lo, hi in windows:
        if name not in spans:
            spans[name] = []
            order.append(name)
        spans[name].append((float(lo), float(hi)))
    return [(n, sorted(spans[n])) for n in order]


def frames_in_transect(flight: Path, name: str,
                       tz_name: str | None = None) -> list[tuple[float, str]]:
    """(epoch, stem) for the frames already sorted into one transect.

    Reads what is on disk rather than what the import intended, so a mark can
    only ever be given a frame that is really there. Previews are preferred
    over raws because they are what gets looked at, but either will do -- the
    two share a stem by design.

    ``meters/`` is searched as well, so re-running after the frames have been
    filed still sees them and lands on the same answer.
    """
    from . import photos as ph  # noqa: PLC0415

    tdir = layout.transect_dir(Path(flight), name)
    seen: dict[str, float] = {}
    for sub in (layout.JPG_PREVIEW, layout.JPG, layout.GPR, layout.JPG_EDITED):
        for folder in (tdir / sub, tdir / sub / layout.METERS):
            if not folder.is_dir():
                continue
            for p in sorted(folder.iterdir()):
                if not p.is_file() or p.name.startswith("."):
                    continue
                if p.stem in seen:
                    continue
                when = ph.time_from_name(p.name, tz_name=tz_name)
                if when is not None:
                    seen[p.stem] = when.timestamp()
    return sorted((e, s) for s, e in seen.items())


def file_marked_frames(
    flight: Path,
    name: str,
    matches: Sequence[MarkMatch],
    *,
    move_gpr: bool = True,
    move_jpg: bool = False,
) -> tuple[int, list[str]]:
    """Move the frames that landed on a mark into ``<folder>/meters/``.

    Separating them is the point of the pass: a transect folder holds a frame
    every few seconds, and only some of those sit on a distance mark. Files
    are *moved*, never re-encoded, so nothing loses a JPEG generation -- which
    is why this is safe to do inside ``JPG_edited`` even though that folder is
    otherwise never written to. It does relocate frames that downstream ML
    reads, so the JPG side is opt-in.

    Returns (files moved, warnings). Re-running is harmless: a frame already
    in ``meters/`` is left alone.
    """
    tdir = layout.transect_dir(Path(flight), name)
    stems = {m.stem for m in matches if m.matched and m.stem}
    moved, warnings = 0, []
    if not stems:
        return 0, warnings

    wanted = [(layout.GPR, move_gpr), (layout.JPG_EDITED, move_jpg)]
    for sub, on in wanted:
        folder = tdir / sub
        if not on or not folder.is_dir():
            continue
        dest = folder / layout.METERS
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.stem not in stems:
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / p.name
                if target.exists():
                    continue          # already filed by an earlier run
                p.replace(target)
                moved += 1
            except OSError as ex:
                warnings.append(f"could not file {p.name}: {ex}")
    return moved, warnings


def run_for_flight(
    flight: Path,
    windows: Sequence[tuple[str, float, float]],
    store: TelemetryStore,
    options: MarkOptions | None = None,
    *,
    tz_name: str | None = None,
    tz=None,
    progress=None,
    cancel=None,
) -> MarkReport:
    """Reconstruct every transect in a flight and write what was asked for.

    Safe to re-run: it only ever writes its own CSV and PNG beside the
    imagery, and never touches a frame.
    """
    opts = options or MarkOptions()
    rep = MarkReport()
    if not opts.enabled or not windows:
        return rep

    flight = Path(flight)
    grouped = group_windows(windows)
    total = max(1, len(grouped))
    for i, (name, spans) in enumerate(grouped):
        if cancel is not None and cancel.is_set():
            from .ffmpeg_tools import CancelledError  # noqa: PLC0415
            raise CancelledError("canceled")
        if progress:
            progress(i / total, f"meter marks: {name}")

        if not layout.transect_dir(flight, name).is_dir():
            continue          # never imported; nothing to mark against

        try:
            frames = frames_in_transect(flight, name, tz_name)
            rec = reconstruct(
                store, name, spans, interval=opts.interval_m,
                source=opts.source, frames=frames,
                max_offset=opts.max_offset_s)
        except Exception as ex:                       # one bad transect only
            rep.errors.append(f"{name}: {ex}")
            continue

        rep.reconstructions.append(rec)
        rep.warnings.extend(f"{name}: {w}" for w in rec.warnings)
        if not rec.ok:
            continue

        if opts.move_gpr or opts.move_jpg:
            try:
                n, warns = file_marked_frames(
                    flight, name, rec.matches,
                    move_gpr=opts.move_gpr, move_jpg=opts.move_jpg)
                rep.filed += n
                rep.warnings.extend(f"{name}: {w}" for w in warns)
            except Exception as ex:
                rep.errors.append(f"{name}: could not file frames: {ex}")

        tdir = layout.transect_dir(flight, name)
        try:
            if opts.write_csv:
                rep.csv_paths.append(
                    write_marks_csv(rec, tdir / f"{name}{CSV_SUFFIX}", tz=tz))
            if opts.write_png:
                from .trackplot import save_track_png  # noqa: PLC0415
                rep.png_paths.append(
                    save_track_png(rec, tdir / f"{name}{PNG_SUFFIX}"))
        except Exception as ex:
            rep.errors.append(f"{name}: could not write output: {ex}")

    if progress:
        progress(1.0, f"meter marks: {rep.marks} across "
                      f"{len(rep.reconstructions)} transect(s)")
    return rep
