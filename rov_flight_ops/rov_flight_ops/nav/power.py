"""
Watts, watt-hours, and the peak — from a busbar that is not a battery.

On this fleet `BATT_MONITOR` is 4 (analog voltage and current) reading the
Navigator's own sense pins off the main busbar, with `BATT_VOLT_MULT` 11 and
`BATT_AMP_PERVLT` 37.88. Power comes down the tether from an Outland OTPS whose
stated limit is 1,000 W. So:

* `P = V * I` from `BATTERY_STATUS` is a real instantaneous busbar draw, and
  the two come out of the *same message*, which is why multiplying them is
  legitimate -- they are simultaneous by construction rather than by luck.
* It is **not** the OTPS's own protection measurement. The topside supply, the
  tether's own loss and anything powered before this sense point are not in
  it. The gauge says 1,000 W because that is the operating reference the
  operator flies to, not because this sensor measures the trip point.

That distinction is on the face of the gauge, not buried here.

**Energy is integrated over the intervals that actually happened.** Not
samples times a nominal rate: the collector's cadence varies with the Pi's
load, reconnects leave gaps, and replay runs at whatever speed the operator
chose. Each accepted sample contributes `W * dt` where `dt` is the real
elapsed time since the previous accepted sample, and four things are refused:

1. A sample whose `dt` is zero or negative -- a duplicate, or clock going
   backwards.
2. A sample separated from the last by more than `MAX_GAP_S`. The vehicle was
   out of contact; we do not know what it drew, and pretending the last value
   held across a two-minute dropout would silently invent watt-hours. The gap
   is recorded and the session is marked as having incomplete coverage.
3. A stale reading. Integrating a held value is exactly how a disconnected ROV
   accumulates energy it never used.
4. A sample already counted -- matched on the message counter, so a poll that
   returns the same `BATTERY_STATUS` twice adds nothing.

`coverage` reports the fraction of wall time that was actually integrated, so
"14.2 Wh" can be read alongside "over 96% of the flight" rather than being
quoted as though it were complete.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: The gauge's fixed top. Never auto-ranged: an operator learns where 700 W
#: sits on the dial, and a gauge that rescales destroys that the moment it
#: matters most.
GAUGE_MAX_W = 1000.0
#: Where the red band starts.
HIGH_LOAD_W = 900.0

#: Longer than this between two samples and the span is a gap, not an
#: interval. Three seconds is a few times the BATTERY_STATUS period on this
#: vehicle and well inside a normal reconnect.
MAX_GAP_S = 3.0

#: A busbar reading outside this is not a measurement. ArduPilot reports
#: `current_battery = -1` for "not measured", which is caught separately; this
#: catches a sense line that has come adrift.
MIN_VOLTS, MAX_VOLTS = 5.0, 60.0
MIN_AMPS, MAX_AMPS = -5.0, 200.0


@dataclass
class EnergyState:
    """What survives a restart of the program mid-flight.

    Written next to the flight's other logs, so a crash and relaunch during a
    dive resumes the same session's total rather than starting again at zero
    -- and so that resuming is visible rather than silent.
    """

    session_id: str = ""
    started_wall: float = 0.0
    wh: float = 0.0
    peak_w: float = 0.0
    peak_wall: float = 0.0
    #: Seconds of wall time actually covered by integration.
    integrated_s: float = 0.0
    #: Seconds skipped because the gap was too long or data was unusable.
    gap_s: float = 0.0
    #: How many times integration was skipped, by reason.
    skipped: dict = field(default_factory=dict)
    #: Deliberate resets, with when and why. Never silently discarded.
    resets: list = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


class EnergyMeter:
    """Integrates busbar power, tracks the observed peak, survives reconnects.

    One meter per flight session. `begin` starts a new one; `resume` picks up a
    saved one. The difference is deliberate and is logged both ways, because
    "is this 14 Wh the whole dive?" has to be answerable.
    """

    def __init__(self, state: EnergyState | None = None) -> None:
        self.state = state or EnergyState()
        #: Monotonic clock of the last integrated sample.
        self._last_mono: float | None = None
        #: The counter of the last BATTERY_STATUS actually counted, so a
        #: repeated cache cannot be integrated twice.
        self._last_counter: int | None = None
        self._first_mono: float | None = None
        self._last_seen_mono: float | None = None

    # -- session boundaries -----------------------------------------------

    def begin(self, session_id: str) -> None:
        """Start a fresh flight. Everything accumulated is discarded."""
        self.state = EnergyState(session_id=session_id,
                                 started_wall=time.time())
        self._last_mono = self._first_mono = self._last_seen_mono = None
        self._last_counter = None
        log.info("energy: new session %s", session_id)

    def reset(self, why: str) -> None:
        """An operator's deliberate reset, recorded rather than silent."""
        self.state.resets.append({
            "at": time.time(), "why": why,
            "wh_before": round(self.state.wh, 4),
            "peak_before": round(self.state.peak_w, 1)})
        self.state.wh = 0.0
        self.state.peak_w = 0.0
        self.state.peak_wall = 0.0
        self.state.integrated_s = 0.0
        self.state.gap_s = 0.0
        self.state.skipped = {}
        self._last_mono = self._first_mono = None
        self._last_counter = None
        log.info("energy: reset (%s)", why)

    # -- the integration --------------------------------------------------

    def update(self, volts: float | None, amps: float | None, *,
               mono: float, fresh: bool = True,
               counter: int | None = None) -> float | None:
        """Take one busbar sample. Returns the instantaneous watts, or None.

        `fresh` is the caller's verdict from the message counter, not from an
        HTTP status. A stale sample still returns its watts for a *marked*
        display -- the operator may want to see the last known draw -- but it
        is never integrated.
        """
        watts = self.watts(volts, amps)
        if watts is None:
            self._skip("unusable reading")
            # An unusable reading breaks the interval: the next good one must
            # not integrate across the hole.
            self._last_mono = None
            return None

        self._last_seen_mono = mono
        if self._first_mono is None:
            self._first_mono = mono

        if not fresh:
            self._skip("not a new sample")
            return watts
        if counter is not None and counter == self._last_counter:
            self._skip("duplicate counter")
            return watts
        self._last_counter = counter

        # The peak is taken from the raw sample, before any display smoothing
        # ever touches it, and from fresh samples only -- a peak that can be
        # set by a cached value is not an observation.
        if watts > self.state.peak_w:
            self.state.peak_w = watts
            self.state.peak_wall = time.time()

        if self._last_mono is None:
            self._last_mono = mono
            return watts

        dt = mono - self._last_mono
        self._last_mono = mono
        if dt <= 0.0:
            self._skip("time did not advance")
            return watts
        if dt > MAX_GAP_S:
            self.state.gap_s += dt
            self._skip("gap too long to integrate")
            return watts

        self.state.wh += watts * dt / 3600.0
        self.state.integrated_s += dt
        return watts

    def _skip(self, why: str) -> None:
        self.state.skipped[why] = self.state.skipped.get(why, 0) + 1

    @staticmethod
    def watts(volts: float | None, amps: float | None) -> float | None:
        """`V * I`, or None when either half cannot be believed.

        ArduPilot's `current_battery` is -1 for "no current sensor", which is
        a sentinel and not a small negative current; it has to be caught here
        rather than producing a plausible negative wattage.
        """
        if volts is None or amps is None:
            return None
        if not (MIN_VOLTS <= volts <= MAX_VOLTS):
            return None
        if amps <= -1.0 or not (MIN_AMPS <= amps <= MAX_AMPS):
            return None
        return volts * amps

    # -- what it has to say -------------------------------------------------

    @property
    def wh(self) -> float:
        return self.state.wh

    @property
    def peak_w(self) -> float:
        return self.state.peak_w

    def coverage(self) -> float | None:
        """Fraction of elapsed time that was actually integrated, 0..1.

        None until there is enough to say. Displayed beside the watt-hours
        whenever it is below one, because an energy total quoted without its
        coverage is the kind of number that ends up in a report.
        """
        if self._first_mono is None or self._last_seen_mono is None:
            return None
        span = self._last_seen_mono - self._first_mono
        if span <= 0:
            return None
        return max(0.0, min(1.0, self.state.integrated_s / span))

    def complete(self) -> bool:
        cov = self.coverage()
        return cov is not None and cov >= 0.99

    def note(self) -> str:
        """One line for the operator about how trustworthy the total is."""
        cov = self.coverage()
        if cov is None:
            return "not enough samples yet"
        if cov >= 0.99:
            return "complete coverage"
        return (f"{cov * 100:.0f}% coverage — "
                f"{self.state.gap_s:.0f} s not integrated")

    # -- durability ---------------------------------------------------------

    def save(self, path: Path) -> bool:
        """Write the session state. Best effort; returns whether it landed.

        Failures are returned rather than raised: a laptop that cannot write
        this must not lose the flight, but the page has to be able to show
        that the total will not survive a restart.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.state.to_json(), indent=1),
                           encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception as ex:
            log.warning("energy state could not be saved to %s: %s", path, ex)
            return False

    @classmethod
    def resume(cls, path: Path, session_id: str) -> tuple[EnergyMeter, bool]:
        """(meter, whether a saved session was picked up).

        Only resumes a state whose session id matches. A file left over from
        yesterday's flight is not this flight's energy, and quietly adopting it
        is worse than starting from zero.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("session_id") == session_id:
                st = EnergyState(**{k: v for k, v in data.items()
                                    if k in EnergyState.__dataclass_fields__})
                log.info("energy: resumed session %s at %.3f Wh",
                         session_id, st.wh)
                return cls(st), True
        except FileNotFoundError:
            pass
        except Exception as ex:
            log.warning("energy state at %s could not be read: %s", path, ex)
        m = cls()
        m.begin(session_id)
        return m, False


def gauge_fraction(watts: float) -> float:
    """Where the marker sits on the fixed 0–1,000 W gauge, 0..1.

    Clamped for the *position* only. The number beside it keeps its true value
    and the gauge shows an over-range mark, because a marker pinned at the top
    with "1,180 W" next to it is the honest picture and a marker pinned at the
    top with "1,000 W" next to it is a lie about a condition that matters.
    """
    return max(0.0, min(1.0, watts / GAUGE_MAX_W))


def over_range(watts: float) -> bool:
    return watts > GAUGE_MAX_W


def high_load(watts: float) -> bool:
    return watts >= HIGH_LOAD_W
