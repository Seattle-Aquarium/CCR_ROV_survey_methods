"""
TC-25 as the GoPro Labs precision-time page shows it, and the clock check.

No network: the time server is replaced by a function that answers, or that
fails as an offline laptop does.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import tc25 as TC  # noqa: E402


def test_frames_are_milliseconds_times_25_truncated_as_the_page_does():
    # The page: padTime(Math.trunc(ms * 25 / 1000)).
    assert TC.tc25(datetime(2026, 10, 18, 9, 5, 7, 0)) == "09:05:07:00"
    assert TC.tc25(datetime(2026, 10, 18, 9, 5, 7, 39_000)) == "09:05:07:00"
    assert TC.tc25(datetime(2026, 10, 18, 9, 5, 7, 40_000)) == "09:05:07:01"
    assert TC.tc25(datetime(2026, 10, 18, 23, 59, 59, 999_000)) == "23:59:59:24"


def _settled(check: TC.ClockCheck, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while check._busy and time.monotonic() < end:
        time.sleep(0.01)


def test_the_clock_check_says_how_far_off_the_laptop_is():
    check = TC.ClockCheck(query=lambda server: 17.52)
    assert check.words() == "laptop clock"
    check.poll()
    _settled(check)
    assert check.off_by() == 17.52
    assert check.words() == "laptop clock · 17.52 s slow"
    fast = TC.ClockCheck(query=lambda server: -0.4)
    fast.poll()
    _settled(fast)
    assert fast.words().endswith("0.40 s fast")
    ontime = TC.ClockCheck(query=lambda server: 0.01)
    ontime.poll()
    _settled(ontime)
    assert ontime.words().endswith("on time")


def test_offline_says_so_and_never_blocks():
    def offline(server):
        raise OSError("getaddrinfo failed")

    check = TC.ClockCheck(query=offline)
    started = time.monotonic()
    check.poll()
    assert time.monotonic() - started < 0.5, "the check runs off the caller's thread"
    _settled(check)
    assert check.off_by() is None
    assert "offline" in check.words()


def test_a_check_is_not_repeated_until_it_is_due():
    calls = []
    check = TC.ClockCheck(query=lambda server: calls.append(server) or 0.0,
                          every_s=600)
    for _ in range(5):
        check.poll()
        _settled(check)
    assert len(calls) == 1
