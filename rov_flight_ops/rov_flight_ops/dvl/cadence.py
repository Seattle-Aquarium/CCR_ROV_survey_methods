"""
Report-to-report intervals, on two clocks, and what a long one means.

Every velocity report carries three times, and this laptop adds a fourth:

* `time_of_validity` -- the DVL's clock at the centre of the ping;
* `time_of_transmission` -- the DVL's clock just before it sent the report;
* `time` -- the DVL's own count of milliseconds since its *previous* report;
* the moment this laptop's `recv` returned it.

A gap in what arrived could have been made in three different places, and
those times are enough to tell which:

=====================  ======================================================
``dvl_quiet``          the DVL's clock moved a long way between two reports,
                       and its own `time` field agrees: it produced nothing in
                       between. Lost bottom lock, a range-mode search, the
                       periodic-cycling check, thermal trouble -- the DVL.
``missing_reports``    the DVL's clock moved a long way, but `time` says the
                       DVL's previous report was recent: reports were made
                       that never reached this laptop. The DVL's server, or
                       the network, lost them.
``delivery_stall``     the DVL's clock moved normally, but this laptop got
                       nothing for a long time and then several at once: the
                       reports were made on time and delivered late. The
                       tether, the network or this laptop.
=====================  ======================================================

The third does not lose anything on TCP -- it arrives late -- but the same
stall on the vehicle is where the BlueOS extension, which gives up and
reconnects after three seconds of silence, throws its buffer away.

**A gap while the acoustics were switched off is none of these.** With
`acoustic_enabled` false the DVL makes no velocity reports at all, so the
silence is a setting, not a fault, and it is ``acoustics_off``. Which
periods those were comes from the configuration polls (`acoustics`), and a
gap is one if any of them overlaps the time this laptop waited for it. The
A50 on software 2.7.2 makes it look like the others: its first report after
the acoustics come back carries a stale time of validity -- one interval
after the last ping before they went off -- with its own `time` saying how
long it really was; the next report's time of validity then jumps the whole
way. Seen on 15 October 2026 as a "delivery stall" of 76 s followed by "~291
reports made but not received". So the first report after an acoustics-off
silence closes it, and the interval to the one after is not judged on the
DVL's clock.

Thresholds are relative to the recent median interval rather than fixed,
because the A50's rate runs from 2 Hz to 15 Hz with altitude and a fixed
threshold would be wrong at one end or the other. They are written to the
capture's record with the counts, so nobody has to guess what was flagged.

**Invalid reports are counted separately and are not gaps.** The DVL keeps
reporting while it has no bottom lock, with `velocity_valid` false. The stock
BlueOS extension discards those reports before they reach the autopilot, so
from the vehicle's side an invalid stretch looks exactly like a gap. Here it
is its own thing, with its own count and duration.

A stretch is measured on the DVL's clock, from its first invalid report to
the first valid one, unless that disagrees with this laptop's clock by more
than a stall's slack -- which is what a stale time of validity does, and on
15 October one reached back across the hour the acoustics had been off
before the capture began. Then it is this laptop's measure, which cannot
start before the capture did. A stretch still open is counted to its latest
report, and one the acoustics being switched off interrupts ends at its last
invalid report.
"""

from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass, field

from .webapi import utc

#: An interval this many times the recent median is a gap...
GAP_FACTOR = 2.5
#: ...and so is one this much longer than the median, whichever is larger. At
#: 15 Hz, 2.5 x 67 ms is 167 ms, which a single late report can reach.
GAP_FLOOR_MS = 150.0
#: How many recent intervals the median is taken over.
WINDOW = 51
#: Fewer intervals than this and no median is trusted yet.
MIN_HISTORY = 5
#: `time` must fall this far short of the clock interval before reports are
#: called missing rather than unmade -- a fraction of the median, and never
#: less than this.
MISSING_SLACK_MS = 50.0
#: A laptop interval this much longer than the DVL's own is a stall.
STALL_SLACK_MS = 250.0
#: A report that ends a wait this long, while the acoustics were off, is the
#: first after they came back. The configuration is polled every 2 s, so no
#: shorter acoustics-off period is seen at all.
ACOUSTICS_SILENCE_MS = 1000.0

#: The kinds of gap, in the order the record lists them.
GAP_KINDS = ("dvl_quiet", "missing_reports", "delivery_stall", "clock_step",
             "acoustics_off")


@dataclass
class Gap:
    """One long interval, and the best reading of where it was made."""

    kind: str                         # one of GAP_KINDS
    #: When it ended, on this laptop's wall clock.
    ended_unix: float
    #: The interval on the DVL's clock, and the DVL's own `time` field.
    dvl_ms: float | None
    dvl_time_ms: float | None
    #: The interval on this laptop's clock.
    rx_ms: float | None
    #: The median interval it was judged against.
    median_ms: float | None
    #: For missing reports, about how many.
    missing: int = 0

    def text(self) -> str:
        med = f"{self.median_ms:.0f}" if self.median_ms else "?"
        if self.kind == "dvl_quiet":
            return (f"DVL produced nothing for {self.dvl_ms:.0f} ms "
                    f"(its own interval {self.dvl_time_ms or 0:.0f} ms; "
                    f"usual {med} ms)")
        if self.kind == "missing_reports":
            return (f"~{self.missing} report(s) made but not received: DVL clock "
                    f"moved {self.dvl_ms:.0f} ms, its own interval says "
                    f"{self.dvl_time_ms or 0:.0f} ms (usual {med} ms)")
        if self.kind == "delivery_stall":
            return (f"nothing received for {self.rx_ms:.0f} ms while the DVL "
                    f"reported every {self.dvl_ms:.0f} ms — delivered late")
        if self.kind == "acoustics_off":
            return (f"no velocity reports for {self.rx_ms or 0:.0f} ms: the DVL's "
                    f"acoustics were switched off")
        return (f"DVL clock went back or stood still ({self.dvl_ms:.0f} ms) — "
                f"a clock step, or a repeated report")


@dataclass
class CadenceStats:
    """Totals over a capture, for its record and the tab."""

    reports: int = 0
    invalid: int = 0
    invalid_spans: int = 0
    #: Closed stretches only; `as_dict` adds the open one.
    invalid_ms: float = 0.0
    longest_invalid_ms: float = 0.0
    #: The stretch still open, to its latest report; 0 when none is.
    open_invalid_ms: float = 0.0
    gaps: dict = field(default_factory=lambda: dict.fromkeys(GAP_KINDS, 0))
    gap_ms: dict = field(default_factory=lambda: dict.fromkeys(GAP_KINDS, 0.0))
    #: The longest gap that was a fault: acoustics_off is not counted.
    longest_gap_ms: float = 0.0
    missing_estimate: int = 0
    #: Each period the configuration said the acoustics were off:
    #: {"off_utc", "on_utc"}, `on_utc` None while it lasts.
    acoustics_off: list = field(default_factory=list)

    def as_dict(self) -> dict:
        open_ms = self.open_invalid_ms
        return {
            "reports": self.reports,
            "invalid_reports": self.invalid,
            "invalid_spans": self.invalid_spans,
            "invalid_ms": round(self.invalid_ms + open_ms, 1),
            "longest_invalid_ms": round(max(self.longest_invalid_ms, open_ms), 1),
            "invalid_open_ms": round(open_ms, 1),
            "gaps": dict(self.gaps),
            "gap_ms": {k: round(v, 1) for k, v in self.gap_ms.items()},
            "longest_gap_ms": round(self.longest_gap_ms, 1),
            "missing_reports_estimate": self.missing_estimate,
            "acoustics_off_periods": [dict(p) for p in self.acoustics_off],
            "thresholds": {
                "gap_factor": GAP_FACTOR, "gap_floor_ms": GAP_FLOOR_MS,
                "median_window": WINDOW, "missing_slack_ms": MISSING_SLACK_MS,
                "stall_slack_ms": STALL_SLACK_MS,
                "acoustics_silence_ms": ACOUSTICS_SILENCE_MS,
            },
        }


class Cadence:
    """Follows one stream of velocity reports.

    One owner calls `add`. `acoustics` may be called from any thread -- the
    configuration is read by the status poll and by the TCP thread -- and
    what it shares with `add` is under a lock.
    """

    def __init__(self) -> None:
        self.stats = CadenceStats()
        self._dvl_intervals: deque[float] = deque(maxlen=WINDOW)
        self._prev_tov: float | None = None
        self._prev_rx_mono_ns: int | None = None
        #: The open invalid stretch: its first report, and its latest, each
        #: as (time of validity µs or None, receipt monotonic ns).
        self._invalid_first: tuple[float | None, int] | None = None
        self._invalid_last: tuple[float | None, int] | None = None
        #: Periods with the acoustics off, on this laptop's monotonic clock:
        #: [off_ns, on_ns | None, resumed_ns | None]. `on` is when a poll saw
        #: them back on; `resumed`, when the first report after them came.
        self._off: list[list] = []
        self._off_lock = threading.Lock()
        #: The previous report ended an acoustics-off silence, so its time of
        #: validity is stale and the next interval is not judged on it.
        self._stale_tov = False

    @property
    def median_ms(self) -> float | None:
        if len(self._dvl_intervals) < MIN_HISTORY:
            return None
        return statistics.median(self._dvl_intervals)

    def threshold_ms(self) -> float | None:
        med = self.median_ms
        if med is None:
            return None
        return max(GAP_FACTOR * med, med + GAP_FLOOR_MS)

    def acoustics(self, enabled, *, mono_ns: int, unix: float) -> None:
        """The DVL's `acoustic_enabled`, as a configuration read just found it.

        Only a change matters. Anything but True or False -- a read that did
        not say -- changes nothing.
        """
        if not isinstance(enabled, bool):
            return
        with self._off_lock:
            is_off = bool(self._off) and self._off[-1][1] is None
            if not enabled and not is_off:
                self._off.append([mono_ns, None, None])
                self.stats.acoustics_off.append({"off_utc": utc(unix), "on_utc": None})
            elif enabled and is_off:
                self._off[-1][1] = mono_ns
                self.stats.acoustics_off[-1]["on_utc"] = utc(unix)

    def _off_during(self, start_ns: int | None, end_ns: int) -> bool:
        """Were the acoustics off at any time in (start_ns, end_ns]?

        A period ends when a poll saw them back on, or when the first report
        after them came, whichever was first: the polls are seconds apart,
        and reports flowing again is the better evidence.
        """
        with self._off_lock:
            for off, on, resumed in self._off:
                ends = [t for t in (on, resumed) if t is not None]
                end = min(ends) if ends else None
                if off <= end_ns and (end is None or start_ns is None or end > start_ns):
                    return True
        return False

    def _resumed(self, rx_mono_ns: int) -> None:
        with self._off_lock:
            for period in self._off:
                if period[2] is None and period[0] < rx_mono_ns:
                    period[2] = rx_mono_ns

    def _span_ms(self, start: tuple, end: tuple) -> float:
        """An invalid stretch from `start` to `end`, each (tov µs | None, rx ns).

        On the DVL's clock, unless that disagrees with this laptop's by more
        than a stall's slack -- a stale time of validity -- and then on this
        laptop's, which cannot reach back before the capture began.
        """
        (tov0, rx0), (tov1, rx1) = start, end
        rx_span = max(0.0, (rx1 - rx0) / 1e6)
        if tov0 is not None and tov1 is not None:
            dvl_span = (tov1 - tov0) / 1000.0
            slack = max(STALL_SLACK_MS, 2 * (self.median_ms or 0.0))
            if 0.0 <= dvl_span <= rx_span + slack:
                return dvl_span
        return rx_span

    def _close_invalid(self, end: tuple) -> float:
        st = self.stats
        span = self._span_ms(self._invalid_first, end)
        # Open first, closed second: a reader on another thread in between
        # sees the stretch missing for a moment, never counted twice.
        st.open_invalid_ms = 0.0
        st.invalid_ms += span
        st.longest_invalid_ms = max(st.longest_invalid_ms, span)
        self._invalid_first = self._invalid_last = None
        return span

    def add(self, *, tov_us: float | None, time_ms: float | None,
            rx_mono_ns: int, rx_unix: float,
            valid: bool | None) -> tuple[list[Gap], str]:
        """One report in. Returns any gaps it closed, and an event to note.

        The event is "" or a sentence about validity changing -- the start or
        end of a stretch the extension would have thrown away.
        """
        gaps: list[Gap] = []
        notes: list[str] = []
        st = self.stats
        st.reports += 1

        dvl_ms = None
        if tov_us is not None and self._prev_tov is not None:
            dvl_ms = (tov_us - self._prev_tov) / 1000.0
        rx_ms = None
        if self._prev_rx_mono_ns is not None:
            rx_ms = (rx_mono_ns - self._prev_rx_mono_ns) / 1e6

        median = self.median_ms
        threshold = self.threshold_ms()
        # The acoustics were off while this laptop waited for this report:
        # after a long wait, it is the first since they came back.
        off = self._off_during(self._prev_rx_mono_ns, rx_mono_ns)
        resumed = off and (rx_ms is None
                           or rx_ms > max(ACOUSTICS_SILENCE_MS, threshold or 0.0))
        # If the previous report was that first one, its time of validity is
        # stale, and this interval on the DVL's clock is measured from it.
        judged = None if self._stale_tov else dvl_ms

        if judged is not None and judged <= 0:
            gaps.append(Gap("clock_step", rx_unix, dvl_ms, time_ms, rx_ms, median))
        elif judged is not None and threshold is not None and judged > threshold:
            slack = max(MISSING_SLACK_MS, 0.5 * (median or 0.0))
            if time_ms is not None and judged - time_ms > slack:
                missing = max(1, round(judged / median) - 1) if median else 1
                gaps.append(Gap("missing_reports", rx_unix, dvl_ms, time_ms,
                                rx_ms, median, missing=missing))
            else:
                gaps.append(Gap("dvl_quiet", rx_unix, dvl_ms, time_ms, rx_ms,
                                median))
        elif (judged is not None and rx_ms is not None and median is not None
              and rx_ms > judged + max(STALL_SLACK_MS, 2 * median)):
            gaps.append(Gap("delivery_stall", rx_unix, dvl_ms, time_ms, rx_ms,
                            median))
        if off and gaps and gaps[0].kind != "clock_step":
            gaps = [Gap("acoustics_off", rx_unix, dvl_ms, time_ms, rx_ms, median)]

        for gap in gaps:
            st.gaps[gap.kind] += 1
            length = (gap.rx_ms if gap.kind in ("delivery_stall", "acoustics_off")
                      else gap.dvl_ms) or 0.0
            st.gap_ms[gap.kind] += max(0.0, length)
            if gap.kind != "acoustics_off":
                st.longest_gap_ms = max(st.longest_gap_ms, length)
            st.missing_estimate += gap.missing

        # Only ordinary intervals feed the median, so a long gap does not
        # raise the bar for spotting the next one.
        if judged is not None and judged > 0 and not gaps and not off:
            self._dvl_intervals.append(judged)

        here = (tov_us, rx_mono_ns)
        if resumed and self._invalid_first is not None:
            # The acoustics went off part-way through an invalid stretch: it
            # ended with the last invalid report before they did.
            span = self._close_invalid(self._invalid_last)
            notes.append(f"invalid stretch ended after {span / 1000.0:.1f} s, "
                         f"when the acoustics were switched off")
        if valid is False:
            st.invalid += 1
            if self._invalid_first is None:
                self._invalid_first = here
                st.invalid_spans += 1
                notes.append("velocity became INVALID (no bottom lock) — the "
                             "stock extension drops these")
            self._invalid_last = here
            st.open_invalid_ms = self._span_ms(self._invalid_first, here)
        elif valid is True and self._invalid_first is not None:
            span = self._close_invalid(here)
            notes.append(f"velocity valid again after {span / 1000.0:.1f} s")

        if resumed:
            self._resumed(rx_mono_ns)
        self._stale_tov = resumed
        if tov_us is not None:
            self._prev_tov = tov_us
        self._prev_rx_mono_ns = rx_mono_ns
        return gaps, "; ".join(notes)


def derived_velocity(row: dict, prev: dict | None, rx_unix: float,
                     rx_mono_ns: int) -> dict:
    """The arithmetic columns of a velocity row, from it and the one before.

    Each is a difference of two recorded numbers and nothing else; none of
    them uses a threshold. Documented in the log schema.
    """
    tov = row.get("time_of_validity_us")
    tot = row.get("time_of_transmission_us")
    out = {"d_tov_ms": None, "d_rx_ms": None, "dvl_unaccounted_ms": None,
           "tx_minus_tov_ms": None, "rx_minus_tx_ms": None}
    if prev is not None:
        ptov = prev.get("time_of_validity_us")
        if isinstance(tov, (int, float)) and isinstance(ptov, (int, float)):
            out["d_tov_ms"] = (tov - ptov) / 1000.0
            if isinstance(row.get("time_ms"), (int, float)):
                out["dvl_unaccounted_ms"] = out["d_tov_ms"] - row["time_ms"]
        pmono = prev.get("_rx_mono_ns")
        if isinstance(pmono, int):
            out["d_rx_ms"] = (rx_mono_ns - pmono) / 1e6
    if isinstance(tov, (int, float)) and isinstance(tot, (int, float)):
        out["tx_minus_tov_ms"] = (tot - tov) / 1000.0
    if isinstance(tot, (int, float)):
        out["rx_minus_tx_ms"] = (rx_unix * 1e6 - tot) / 1000.0
    return out


#: The derived velocity columns, in order.
DERIVED_VELOCITY_COLUMNS = ("d_tov_ms", "d_rx_ms", "dvl_unaccounted_ms",
                            "tx_minus_tov_ms", "rx_minus_tx_ms")


def derived_position(row: dict, prev: dict | None, rx_mono_ns: int) -> dict:
    out = {"d_ts_ms": None, "d_rx_ms": None}
    if prev is not None:
        ts, pts = row.get("ts"), prev.get("ts")
        if isinstance(ts, (int, float)) and isinstance(pts, (int, float)):
            out["d_ts_ms"] = (ts - pts) * 1000.0
        pmono = prev.get("_rx_mono_ns")
        if isinstance(pmono, int):
            out["d_rx_ms"] = (rx_mono_ns - pmono) / 1e6
    return out


DERIVED_POSITION_COLUMNS = ("d_ts_ms", "d_rx_ms")
