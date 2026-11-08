"""
Running the light controller's MicroPython under CPython, for its logic only.

The firmware modules use `time.ticks_ms()` and friends, and `machine`; neither
exists on a laptop. This puts a fake clock on `time` and a stub `machine` in
`sys.modules` before anything is imported, so the poller, the bus deadline,
the telemetry encoding and the power-monitor drivers can be exercised against
scripted hardware. What it cannot tell you is whether the real UART, USB and
I2C behave as scripted -- that is the bench checklist's job.

    python -m pytest lighting/code/tests
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path[:0] = [str(SRC), str(SRC / "lib")]


class FakeClock:
    """A millisecond clock that only moves when told to (or when slept on)."""

    def __init__(self) -> None:
        self.now = 1000

    def ticks_ms(self) -> int:
        return self.now

    def ticks_us(self) -> int:
        return self.now * 1000

    @staticmethod
    def ticks_diff(a: int, b: int) -> int:
        return a - b

    def sleep_ms(self, ms: int) -> None:
        self.now += max(1, int(ms))


CLOCK = FakeClock()
for _name in ("ticks_ms", "ticks_us", "ticks_diff", "sleep_ms"):
    if not hasattr(time, _name):
        setattr(time, _name, getattr(CLOCK, _name))

_machine = types.ModuleType("machine")


class _Pin:
    IN = OUT = PULL_DOWN = PULL_UP = IRQ_RISING = IRQ_FALLING = 0

    def __init__(self, *a, **k) -> None:
        pass

    def irq(self, *a, **k) -> None:
        pass

    def value(self, *a):
        return 0


_machine.Pin = _Pin
_machine.I2C = lambda *a, **k: None
_machine.UART = lambda *a, **k: None
_machine.reset_cause = lambda: 1
sys.modules.setdefault("machine", _machine)
