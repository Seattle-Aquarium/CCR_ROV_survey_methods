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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from mcap.reader import make_reader

from .mcap_read import _brief, _iter_indexed, _msg_type, _sysid_rank, select_mcaps

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
    "Depth": (
        ("VFR_HUD", "alt", "the HUD's altitude field, used only when it is "
                           "genuinely reporting"),
        ("GLOBAL_POSITION_INT", "relative_alt", "the autopilot's own baro depth"),
        ("LOCAL_POSITION_NED", "z", "the EKF's local-frame z"),
        ("SCALED_PRESSURE2", "press_abs", "the external pressure sensor"),
    ),
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
class FeedReport:
    columns: dict[str, list[Feed]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def used(self, column: str) -> Feed | None:
        return next((f for f in self.columns.get(column, ()) if f.used), None)

    def concerns(self) -> list[str]:
        out: list[str] = []
        for column, feeds in self.columns.items():
            win = next((f for f in feeds if f.used), None)
            if win is None:
                out.append(f"{column} has no source in this recording — the "
                           f"column will be empty.")
                continue
            if win.gaps:
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
        return out

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


def _stats(times: list[float], values: list[float]) -> tuple[int, float, float | None,
                                                             float | None, int, float]:
    n = len(times)
    if n == 0:
        return 0, 0.0, None, None, 0, 0.0
    t = np.asarray(times, dtype=float)
    span = float(t[-1] - t[0]) if n > 1 else 0.0
    hz = (n - 1) / span if span > 0 else 0.0
    d = np.diff(t) if n > 1 else np.array([0.0])
    gaps = int((d > GAP_S).sum())
    longest = float(d.max()) if len(d) else 0.0
    lo = hi = None
    if values:
        v = np.asarray([x for x in values if math.isfinite(x)], dtype=float)
        if v.size:
            lo, hi = float(v.min()), float(v.max())
    return n, hz, lo, hi, gaps, longest


def read_feeds(paths: Sequence[Path | str]) -> FeedReport:
    """Time every candidate message and decide which fed each column."""
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
            n, hz, lo, hi, gaps, longest = _stats(times.get(message, []),
                                                  vals.get((message, field_), []))
            feeds.append(Feed(message=f"{message}.{field_}", detail=detail,
                              samples=n, hz=hz, lo=lo, hi=hi,
                              gaps=gaps, longest_gap=longest))
        # First candidate with data wins, mirroring the extractor -- except
        # VFR_HUD.alt, which only counts for depth when it genuinely reported.
        for feed, (message, _f, _d) in zip(feeds, candidates):
            if feed.samples == 0:
                continue
            if column == "Depth" and message == "VFR_HUD" and not vfr_alt_real:
                feed.note = "present but flat zero, so it is skipped"
                continue
            feed.used = True
            break
        rep.columns[column] = feeds
    return rep
