"""
Which message actually produced each column, and how well it behaved.

The EKF's aiding flags say what the *filter* was using. They say nothing about
where ``Altitude`` came from, and that matters more than it looks: altitude
drives ``Width`` and ``Area_m2``, area goes as its square, and the same column
can be fed by the DVL's own range or by a fallback beam reading depending on
what the vehicle happened to stream that day. A number that silently changed
instrument between two dives is not comparable across them.

So this walks the same precedence the extractor uses and reports, per column,
which message won, which were available behind it, and how each behaved --
sample rate, value range, and the dropouts that leave holes in the output.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from mcap.reader import make_reader

from .mcap_read import (
    DEPTH_PRECEDENCE,
    _brief,
    _iter_indexed,
    _msg_type,
    _sysid_rank,
    select_mcaps,
)


def _depth_candidates() -> tuple[tuple[str, str, str], ...]:
    """Depth order comes from the extractor, so the two cannot drift."""
    return tuple((msg, field_, detail)
                 for msg, field_, _col, detail in DEPTH_PRECEDENCE)

#: Messages worth timing, beyond the diagnostics the health report already reads.
FEED_TYPES = (
    "RANGEFINDER", "DISTANCE_SENSOR", "VISION_POSITION_DELTA",
    "LOCAL_POSITION_NED", "VFR_HUD", "GLOBAL_POSITION_INT",
    "SCALED_PRESSURE2", "ATTITUDE", "GPS_RAW_INT",
)

#: A gap longer than this leaves a visible hole once the hold limit expires.
GAP_S = 1.0

#: Columns where using the fallback means a *different instrument* is behind the
#: number, rather than the same one read at a different stage.
#:
#: Only Altitude qualifies. Its fallback swaps the autopilot's fused range for a
#: raw beam, and it drives Width and Area_m2 -- so a dive that quietly used the
#: other one is not comparable with the rest. The DVL track falling through to
#: VISION_POSITION_DELTA, by contrast, is the ordinary case on every
#: Cockpit-recorded dive: same DVL, taken one step earlier. Raising that as a
#: concern would fire on every survey the team flies and teach people to ignore
#: the list. It is still shown in the provenance listing, where it belongs.
FALLBACK_IS_NOTABLE = frozenset({"Altitude"})

#: column -> candidates in the order the extractor prefers them, as
#: (message, field, what it physically is)
PRECEDENCE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "Altitude": (
        ("RANGEFINDER", "distance", "the DVL A50's own range to the seabed"),
        ("DISTANCE_SENSOR", "current_distance", "a raw beam reading, used only "
                                                "when RANGEFINDER is absent"),
    ),
    "Depth": _depth_candidates(),
    "DVLx / DVLy": (
        ("LOCAL_POSITION_NED", "x", "the EKF's fused local position"),
        ("VISION_POSITION_DELTA", "position_delta", "the DVL's body-frame "
                                                    "deltas, integrated here"),
    ),
    "Velocity_mps": (
        ("VISION_POSITION_DELTA", "position_delta", "the DVL's own displacement "
                                                    "over its own time delta"),
        ("VFR_HUD", "groundspeed", "the HUD's speed, which carries filter spikes"),
    ),
    "Heading": (
        ("ATTITUDE", "yaw", "the EKF's fused attitude"),
    ),
    "Latitude / Longitude": (
        ("GPS_RAW_INT", "lat", "the Water Linked UGPS, injected as GPS_INPUT"),
    ),
}


@dataclass
class Feed:
    """One candidate message for one column."""
    message: str
    detail: str
    used: bool = False
    samples: int = 0
    hz: float = 0.0
    lo: float | None = None
    hi: float | None = None
    gaps: int = 0
    longest_gap: float = 0.0
    gap_seconds: float = 0.0
    note: str = ""

    def line(self) -> str:
        mark = "->" if self.used else "  "
        if not self.samples:
            return f"   {mark} {self.message:<22} not recorded"
        rng = ""
        if self.lo is not None and self.hi is not None:
            rng = f"  {self.lo:.2f} to {self.hi:.2f}"
        gaps = (f"  {self.gaps} gap(s) over {GAP_S:.0f}s, worst {self.longest_gap:.1f}s"
                if self.gaps else "  no gaps")
        return (f"   {mark} {self.message:<22} {self.samples:>7,} msgs "
                f"{self.hz:5.1f} Hz{rng}{gaps}")


@dataclass
class TransectFeeds:
    """The same sources, measured only inside one transect."""
    name: str
    window: str
    seconds: float
    columns: dict[str, Feed] = field(default_factory=dict)

    def coverage(self, column: str) -> float:
        """Fraction of the transect's seconds that have a sample."""
        f = self.columns.get(column)
        if f is None or not self.seconds:
            return 0.0
        covered = self.seconds - sum_gaps(f)
        return max(0.0, min(1.0, covered / self.seconds))


def sum_gaps(f: Feed) -> float:
    """Seconds lost to dropouts. Approximated from the worst gap and the count,
    which is enough to tell a clean transect from a broken one."""
    return f.gap_seconds


@dataclass
class FeedReport:
    columns: dict[str, list[Feed]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Per transect, when transect windows were supplied. A dive is mostly not
    #: transect: on 2026-09-02, 85 minutes of recording held about 40 minutes of
    #: transect, so whole-dive dropout counts are dominated by the surface
    #: intervals between them and say nothing about the data being analysed.
    per_transect: list[TransectFeeds] = field(default_factory=list)

    def used(self, column: str) -> Feed | None:
        return next((f for f in self.columns.get(column, ()) if f.used), None)

    def transect_lines(self) -> list[str]:
        if not self.per_transect:
            return []
        cols = [c for c in self.columns if self.used(c)]
        L = ["", "Inside the transects only",
             "   (coverage of each transect's seconds, and its worst single gap)"]
        for t in self.per_transect:
            L.append("")
            L.append(f"   {t.name}   {t.window}   {t.seconds / 60:.1f} min")
            for column in cols:
                f = t.columns.get(column)
                if f is None or f.samples == 0:
                    L.append(f"      {column:<22} no data in this window")
                    continue
                worst = (f"worst gap {f.longest_gap:5.1f}s" if f.gaps
                         else "no gaps")
                L.append(f"      {column:<22} {t.coverage(column) * 100:5.1f}% "
                         f"covered   {worst}")
        return L

    def concerns(self) -> list[str]:
        out: list[str] = []
        for column, feeds in self.columns.items():
            win = next((f for f in feeds if f.used), None)
            if win is None:
                out.append(f"{column} has no source in this recording — the "
                           f"column will be empty.")
                continue
            if win.gaps and not self.per_transect:
                out.append(
                    f"{column} came from {win.message} and dropped out "
                    f"{win.gaps} time(s), the worst for {win.longest_gap:.1f} s. "
                    f"Rows past the hold limit are blank rather than stale."
                )
            if (column in FALLBACK_IS_NOTABLE
                    and feeds and feeds[0] is not win and feeds[0].samples == 0):
                out.append(
                    f"{column} fell back to {win.message} because "
                    f"{feeds[0].message} was not recorded — {win.detail}. "
                    f"Width and Area_m2 are derived from it, so this dive is "
                    f"not strictly comparable with ones that used the range."
                )
        # With transects known, dropouts are summarised once rather than once
        # per column: every column drops out together when the vehicle loses
        # bottom lock, and six near-identical lines bury the rest of the list.
        out.extend(self._transect_dropout_concerns())
        return out

    def _transect_dropout_concerns(self) -> list[str]:
        if not self.per_transect:
            return []
        worst_cov = None
        worst_gap = None
        for t in self.per_transect:
            for column, f in t.columns.items():
                if not f.samples:
                    continue
                cov = t.coverage(column)
                if worst_cov is None or cov < worst_cov[0]:
                    worst_cov = (cov, column, t.name)
                if f.gaps and (worst_gap is None or f.longest_gap > worst_gap[0]):
                    worst_gap = (f.longest_gap, column, t.name)
        if worst_cov is None or worst_cov[0] > 0.995:
            return []
        line = (f"Inside the transects the data is not continuous: lowest "
                f"coverage is {worst_cov[1]} at {worst_cov[0] * 100:.0f}% in "
                f"{worst_cov[2]}")
        if worst_gap:
            line += (f", and the longest single gap is {worst_gap[0]:.1f} s "
                     f"({worst_gap[1]} in {worst_gap[2]})")
        return [line + ". Rows past the hold limit are blank rather than stale."]

    def lines(self) -> list[str]:
        L = ["Where each column came from",
             "   (-> is the one used; the others were available behind it)"]
        for column, feeds in self.columns.items():
            L.append("")
            L.append(f"   {column}")
            for f in feeds:
                L.append(f.line())
            win = next((x for x in feeds if x.used), None)
            if win:
                L.append(f"      {win.detail}")
        return L


def _value(message: str, field_: str, m: dict) -> float | None:
    """The named field, in the units the CSV reports it in.

    Converting here rather than at display time means the range printed beside a
    source is directly comparable with the column it feeds -- metres against
    metres, not centimetres or millidegrees.
    """
    v = m.get(field_)
    if field_ == "position_delta":
        # the useful scalar is how far the vehicle moved, not one axis
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                return math.hypot(float(v[0]), float(v[1]))
            except (TypeError, ValueError):
                return None
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        return None
    scale = {
        ("DISTANCE_SENSOR", "current_distance"): 0.01,     # cm -> m
        ("GLOBAL_POSITION_INT", "relative_alt"): 1e-3,     # mm -> m
        ("GPS_RAW_INT", "lat"): 1e-7,                      # degE7 -> deg
    }.get((message, field_), 1.0)
    return float(v) * scale


def _varies(values: list[float]) -> bool:
    """The same test the extractor applies before believing a depth source."""
    from .mcap_read import MIN_DEPTH_VARIATION_M
    if len(values) < 2:
        return False
    return float(np.std(np.asarray(values, dtype=float))) >= MIN_DEPTH_VARIATION_M


def _stats(times: list[float], values: list[float]):
    """(count, Hz, min, max, gaps, longest gap, seconds lost to gaps)."""
    n = len(times)
    if n == 0:
        return 0, 0.0, None, None, 0, 0.0, 0.0
    t = np.asarray(times, dtype=float)
    span = float(t[-1] - t[0]) if n > 1 else 0.0
    hz = (n - 1) / span if span > 0 else 0.0
    d = np.diff(t) if n > 1 else np.array([0.0])
    over = d[d > GAP_S]
    gaps = int(over.size)
    longest = float(d.max()) if len(d) else 0.0
    # only the excess counts as lost: a stream at 1 Hz is not 'down' between
    # its own samples
    lost = float((over - GAP_S).sum()) if over.size else 0.0
    lo = hi = None
    if values:
        v = np.asarray([x for x in values if math.isfinite(x)], dtype=float)
        if v.size:
            lo, hi = float(v.min()), float(v.max())
    return n, hz, lo, hi, gaps, longest, lost


def read_feeds(paths: Sequence[Path | str],
               transects: Sequence = ()) -> FeedReport:
    """Time every candidate message and decide which fed each column.

    ``transects`` are TransectSpec-likes (``.transect_id``, ``.windows`` of
    local HH:MM:SS pairs). Given them, the same statistics are also computed
    inside each transect, which is usually the only part anyone is analysing.
    """
    ordered, warnings = select_mcaps(paths)
    rep = FeedReport(warnings=list(warnings))
    if not ordered:
        return rep

    times: dict[str, list[float]] = {t: [] for t in FEED_TYPES}
    # Keyed by (message, field): one message can feed two columns from two
    # different fields, and reporting one field's range under the other's
    # name is worse than reporting no range at all.
    vals: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    fields_of: dict[str, set[str]] = collections.defaultdict(set)
    for _col, cands in PRECEDENCE.items():
        for message, field_, _d in cands:
            fields_of[message].add(field_)
    vfr_alt_real = False          # did VFR_HUD.alt ever report a real depth?

    for path in ordered:
        try:
            with open(path, "rb") as fh:
                reader = make_reader(fh)
                summary = reader.get_summary()
                if not summary:
                    continue
                # Same system/component preference the extractor applies, so the
                # rates reported here are the ones it actually consumed.
                best: dict[str, tuple[tuple[int, int], str]] = {}
                for ch in summary.channels.values():
                    mt = _msg_type(ch.topic)
                    if mt not in FEED_TYPES:
                        continue
                    rank = _sysid_rank(ch.topic)
                    if mt not in best or rank < best[mt][0]:
                        best[mt] = (rank, ch.topic)
                chosen = {mt: topic for mt, (_r, topic) in best.items()}
                if not chosen:
                    continue

                for mt, m, t in _iter_indexed(reader, chosen):
                    times[mt].append(t)
                    if mt == "VFR_HUD":
                        a = m.get("alt")
                        if isinstance(a, (int, float)) and a < -0.5:
                            vfr_alt_real = True
                    for field_ in fields_of.get(mt, ()):
                        v = _value(mt, field_, m)
                        if v is not None:
                            vals[(mt, field_)].append(v)
        except Exception as ex:
            rep.warnings.append(f"{path.name}: {_brief(ex)}")

    for column, candidates in PRECEDENCE.items():
        feeds: list[Feed] = []
        for message, field_, detail in candidates:
            n, hz, lo, hi, gaps, longest, lost = _stats(
                times.get(message, []), vals.get((message, field_), []))
            feeds.append(Feed(message=f"{message}.{field_}", detail=detail,
                              samples=n, hz=hz, lo=lo, hi=hi, gaps=gaps,
                              longest_gap=longest, gap_seconds=lost))
        # First candidate with data wins, mirroring the extractor. Depth carries
        # the extra rules _add_depth applies, so the source named here is the
        # one that will actually appear in Depth_Source.
        # One Feed was appended per candidate just above, so these are
        # the same length by construction.
        for feed, (message, field_, _d) in zip(feeds, candidates, strict=True):
            if feed.samples == 0:
                continue
            if column == "Depth":
                series = vals.get((message, field_), [])
                if not _varies(series):
                    feed.note = ("constant across the recording, so it is a "
                                 "fixed offset rather than a depth")
                    continue
                if message == "VFR_HUD" and not vfr_alt_real:
                    feed.note = "never below -0.5 m, so it is not reporting depth"
                    continue
            feed.used = True
            break
        else:
            # Nothing passed the guards; the extractor falls back to whichever
            # source has data at all rather than leaving the column empty.
            for feed in feeds:
                if feed.samples:
                    feed.used = True
                    feed.note = (feed.note + "; used anyway, nothing better"
                                 ).lstrip("; ")
                    break
        rep.columns[column] = feeds

    if transects:
        rep.per_transect = _scope_to_transects(rep, transects, times, vals)
    return rep


def _epoch_windows(sample_times: list[float], windows) -> list[tuple[float, float]]:
    """Local HH:MM:SS pairs -> absolute epoch ranges.

    The windows are written in local clock time, the way they are read off a
    field sheet; the recording is stamped in epoch seconds. The dive's own first
    sample supplies the date, so a window never lands on the wrong day.
    """
    from datetime import datetime, timedelta, timezone

    from .mcap_read import PACIFIC_TZ

    if not sample_times:
        return []
    first = datetime.fromtimestamp(min(sample_times), timezone.utc).astimezone(PACIFIC_TZ)
    midnight = first.replace(hour=0, minute=0, second=0, microsecond=0)

    out: list[tuple[float, float]] = []
    for start, end in windows:
        try:
            h1, m1, s1 = (int(x) for x in str(start).split(":"))
            h2, m2, s2 = (int(x) for x in str(end).split(":"))
        except ValueError:
            continue
        lo = midnight + timedelta(hours=h1, minutes=m1, seconds=s1)
        hi = midnight + timedelta(hours=h2, minutes=m2, seconds=s2)
        if hi <= lo:                      # a window that runs past local midnight
            hi += timedelta(days=1)
        out.append((lo.timestamp(), hi.timestamp()))
    return out


def _scope_to_transects(rep: FeedReport, transects, times, vals) -> list[TransectFeeds]:
    """Re-measure the winning source of each column inside each transect."""
    all_times = [t for series in times.values() for t in series]
    out: list[TransectFeeds] = []

    for spec in transects:
        name = getattr(spec, "transect_id", None) or getattr(spec, "name", "?")
        windows = list(getattr(spec, "windows", ()) or ())
        ranges = _epoch_windows(all_times, windows)
        if not ranges:
            continue
        seconds = sum(hi - lo for lo, hi in ranges)
        tf = TransectFeeds(name=name, seconds=seconds,
                           window=", ".join(f"{a}-{b}" for a, b in windows))

        for column, feeds in rep.columns.items():
            win = next((f for f in feeds if f.used), None)
            if win is None:
                continue
            message, _, field_ = win.message.partition(".")
            t_all = np.asarray(times.get(message, []), dtype=float)
            v_all = vals.get((message, field_), [])
            if t_all.size == 0:
                continue
            keep = np.zeros(t_all.size, dtype=bool)
            for lo, hi in ranges:
                keep |= (t_all >= lo) & (t_all <= hi)
            # values are appended in step with times only when finite, so they
            # are re-derived rather than indexed
            n, hz, lo_v, hi_v, gaps, longest, lost = _stats(
                list(t_all[keep]), v_all if len(v_all) == t_all.size else [])
            tf.columns[column] = Feed(
                message=win.message, detail=win.detail, used=True, samples=n,
                hz=hz, lo=lo_v, hi=hi_v, gaps=gaps, longest_gap=longest,
                gap_seconds=lost)
        out.append(tf)
    return out
