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

Thresholds are relative to the recent median interval rather than fixed,
because the A50's rate runs from 2 Hz to 15 Hz with altitude and a fixed
threshold would be wrong at one end or the other. They are written to the
capture's record with the counts, so nobody has to guess what was flagged.

**Invalid reports are counted separately and are not gaps.** The DVL keeps
reporting while it has no bottom lock, with `velocity_valid` false. The stock
BlueOS extension discards those reports before they reach the autopilot, so
from the vehicle's side an invalid stretch looks exactly like a gap. Here it
is its own thing, with its own count and duration.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

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


@dataclass
class Gap:
    """One long interval, and the best reading of where it was made."""

    kind: str                         # dvl_quiet | missing_reports | delivery_stall | clock_step
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
        return (f"DVL clock went back or stood still ({self.dvl_ms:.0f} ms) — "
                f"a clock step, or a repeated report")


@dataclass
class CadenceStats:
    """Totals over a capture, for its record and the tab."""

    reports: int = 0
    invalid: int = 0
    invalid_spans: int = 0
    invalid_ms: float = 0.0
    longest_invalid_ms: float = 0.0
    gaps: dict = field(default_factory=lambda: {
        "dvl_quiet": 0, "missing_reports": 0, "delivery_stall": 0,
        "clock_step": 0})
    gap_ms: dict = field(default_factory=lambda: {
        "dvl_quiet": 0.0, "missing_reports": 0.0, "delivery_stall": 0.0,
        "clock_step": 0.0})
    longest_gap_ms: float = 0.0
    missing_estimate: int = 0

    def as_dict(self) -> dict:
        return {
            "reports": self.reports,
            "invalid_reports": self.invalid,
            "invalid_spans": self.invalid_spans,
            "invalid_ms": round(self.invalid_ms, 1),
            "longest_invalid_ms": round(self.longest_invalid_ms, 1),
            "gaps": dict(self.gaps),
            "gap_ms": {k: round(v, 1) for k, v in self.gap_ms.items()},
            "longest_gap_ms": round(self.longest_gap_ms, 1),
            "missing_reports_estimate": self.missing_estimate,
            "thresholds": {
                "gap_factor": GAP_FACTOR, "gap_floor_ms": GAP_FLOOR_MS,
                "median_window": WINDOW, "missing_slack_ms": MISSING_SLACK_MS,
                "stall_slack_ms": STALL_SLACK_MS,
            },
        }


class Cadence:
    """Follows one stream of velocity reports. Not thread-safe; one owner."""

    def __init__(self) -> None:
        self.stats = CadenceStats()
        self._dvl_intervals: deque[float] = deque(maxlen=WINDOW)
        self._prev_tov: float | None = None
        self._prev_rx_mono_ns: int | None = None
        self._invalid_since_tov: float | None = None
        self._invalid_since_unix: float | None = None

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

    def add(self, *, tov_us: float | None, time_ms: float | None,
            rx_mono_ns: int, rx_unix: float,
            valid: bool | None) -> tuple[list[Gap], str]:
        """One report in. Returns any gaps it closed, and an event to note.

        The event is "" or a sentence about validity changing -- the start or
        end of a stretch the extension would have thrown away.
        """
        gaps: list[Gap] = []
        note = ""
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
        if dvl_ms is not None and dvl_ms <= 0:
            gaps.append(Gap("clock_step", rx_unix, dvl_ms, time_ms, rx_ms, median))
        elif dvl_ms is not None and threshold is not None and dvl_ms > threshold:
            slack = max(MISSING_SLACK_MS, 0.5 * (median or 0.0))
            if time_ms is not None and dvl_ms - time_ms > slack:
                missing = max(1, round(dvl_ms / median) - 1) if median else 1
                gaps.append(Gap("missing_reports", rx_unix, dvl_ms, time_ms,
                                rx_ms, median, missing=missing))
            else:
                gaps.append(Gap("dvl_quiet", rx_unix, dvl_ms, time_ms, rx_ms,
                                median))
        elif (dvl_ms is not None and rx_ms is not None and median is not None
              and rx_ms > dvl_ms + max(STALL_SLACK_MS, 2 * median)):
            gaps.append(Gap("delivery_stall", rx_unix, dvl_ms, time_ms, rx_ms,
                            median))

        for gap in gaps:
            st.gaps[gap.kind] += 1
            length = (gap.rx_ms if gap.kind == "delivery_stall" else gap.dvl_ms) or 0.0
            st.gap_ms[gap.kind] += max(0.0, length)
            st.longest_gap_ms = max(st.longest_gap_ms, length)
            st.missing_estimate += gap.missing

        # Only ordinary intervals feed the median, so a long gap does not
        # raise the bar for spotting the next one.
        if dvl_ms is not None and dvl_ms > 0 and not gaps:
            self._dvl_intervals.append(dvl_ms)

        if valid is False:
            st.invalid += 1
            if self._invalid_since_tov is None:
                self._invalid_since_tov = tov_us
                self._invalid_since_unix = rx_unix
                st.invalid_spans += 1
                note = "velocity became INVALID (no bottom lock) — the stock extension drops these"
        elif valid is True and self._invalid_since_tov is not None:
            if tov_us is not None:
                span = (tov_us - self._invalid_since_tov) / 1000.0
            else:
                span = (rx_unix - (self._invalid_since_unix or rx_unix)) * 1000.0
            st.invalid_ms += max(0.0, span)
            st.longest_invalid_ms = max(st.longest_invalid_ms, span)
            note = f"velocity valid again after {span / 1000.0:.1f} s"
            self._invalid_since_tov = None
            self._invalid_since_unix = None

        if tov_us is not None:
            self._prev_tov = tov_us
        self._prev_rx_mono_ns = rx_mono_ns
        return gaps, note


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
