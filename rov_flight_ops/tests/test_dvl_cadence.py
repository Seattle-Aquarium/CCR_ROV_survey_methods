"""
Which side of the link a gap came from, read off the DVL's own clocks.

Each test builds a stream of reports with known times -- the DVL's time of
validity, its own `time` field, and when this laptop received each -- and
checks the gap is put where it was made.
"""

from __future__ import annotations

import pytest

from rov_flight_ops.dvl import cadence as C

PERIOD_MS = 100.0


class Stream:
    """Reports at 10 Hz on both clocks, with faults injected on request."""

    def __init__(self):
        self.cad = C.Cadence()
        self.tov_us = 1_700_000_000_000_000
        self.rx_ns = 5_000_000_000
        self.unix = 1_700_000_000.0
        self.gaps: list[C.Gap] = []
        self.notes: list[str] = []

    def report(self, *, dvl_ms=PERIOD_MS, rx_ms=None, time_ms=None, valid=True):
        self.tov_us += int(dvl_ms * 1000)
        self.rx_ns += int((dvl_ms if rx_ms is None else rx_ms) * 1e6)
        self.unix += (dvl_ms if rx_ms is None else rx_ms) / 1000
        gaps, note = self.cad.add(tov_us=self.tov_us,
                                  time_ms=dvl_ms if time_ms is None else time_ms,
                                  rx_mono_ns=self.rx_ns, rx_unix=self.unix,
                                  valid=valid)
        self.gaps += gaps
        if note:
            self.notes.append(note)
        return gaps

    def steady(self, n=20):
        for _ in range(n):
            self.report()


def test_a_steady_stream_has_no_gaps():
    s = Stream()
    s.steady(200)
    assert s.gaps == []
    assert s.cad.median_ms == pytest.approx(PERIOD_MS)


def test_the_dvl_going_quiet_is_the_dvls():
    s = Stream()
    s.steady()
    # The DVL's clock moved 1.2 s and its own interval agrees: it made nothing.
    (gap,) = s.report(dvl_ms=1200)
    assert gap.kind == "dvl_quiet"
    assert s.cad.stats.gaps["dvl_quiet"] == 1


def test_reports_made_and_not_received_are_missing():
    s = Stream()
    s.steady()
    # Its clock moved 400 ms, but its own count says the previous report was
    # 100 ms ago: three reports were made that never arrived.
    (gap,) = s.report(dvl_ms=400, time_ms=100)
    assert gap.kind == "missing_reports"
    assert gap.missing == 3
    assert s.cad.stats.missing_estimate == 3


def test_reports_made_on_time_and_delivered_late_are_a_stall():
    s = Stream()
    s.steady()
    (gap,) = s.report(dvl_ms=100, rx_ms=1500)
    assert gap.kind == "delivery_stall"
    # Then the backlog arrives at once.
    for _ in range(13):
        assert s.report(dvl_ms=100, rx_ms=0.1) == []


def test_a_clock_going_backwards_is_a_step_not_a_gap():
    s = Stream()
    s.steady()
    (gap,) = s.report(dvl_ms=-500)
    assert gap.kind == "clock_step"


def test_gaps_do_not_raise_the_bar_for_the_next_one():
    s = Stream()
    s.steady()
    s.report(dvl_ms=2000)
    s.report(dvl_ms=2000)
    assert s.cad.median_ms == pytest.approx(PERIOD_MS)
    assert s.cad.stats.gaps["dvl_quiet"] == 2


def test_a_slow_range_mode_is_not_a_gap():
    """At 2 Hz, 500 ms is ordinary -- the threshold follows the median."""
    s = Stream()
    for _ in range(30):
        s.report(dvl_ms=500)
    assert s.gaps == []
    (gap,) = s.report(dvl_ms=2000)
    assert gap.kind == "dvl_quiet"


def test_nothing_is_judged_before_there_is_a_median():
    s = Stream()
    assert s.report(dvl_ms=3000) == []           # first interval, no history yet


def test_invalid_stretches_are_counted_apart_from_gaps():
    s = Stream()
    s.steady()
    for _ in range(8):
        s.report(valid=False)
    s.report(valid=True)
    st = s.cad.stats
    assert st.invalid == 8 and st.invalid_spans == 1
    # From the first invalid report to the first valid one: eight intervals.
    assert st.invalid_ms == pytest.approx(800, abs=1)
    assert s.gaps == []
    assert "INVALID" in s.notes[0] and "valid again" in s.notes[1]


def test_derived_columns_are_plain_differences():
    prev = {"time_of_validity_us": 1_000_000, "_rx_mono_ns": 10_000_000}
    row = {"time_of_validity_us": 1_150_000, "time_of_transmission_us": 1_152_500,
           "time_ms": 100.0}
    d = C.derived_velocity(row, prev, 1.155, 12_000_000)
    assert d["d_tov_ms"] == 150.0                 # DVL clock, report to report
    assert d["dvl_unaccounted_ms"] == 50.0        # minus the DVL's own interval
    assert d["tx_minus_tov_ms"] == 2.5            # ping centre to send
    assert d["d_rx_ms"] == 2.0                    # this laptop, report to report
    assert d["rx_minus_tx_ms"] == pytest.approx(2.5)   # send to receipt (+ clock offset)
    first = C.derived_velocity(row, None, 1.155, 12_000_000)
    assert first["d_tov_ms"] is None and first["d_rx_ms"] is None
    assert first["tx_minus_tov_ms"] == 2.5
