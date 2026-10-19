"""
The simulated ROV, flying back and forth off Pier 59, and the clock it runs on.

Everything else in the simulator reads from here: the laptop monitor, the
MAVLink the Navigation tab polls, the DVL, the UGPS topside. One vehicle, one
time base, so a turn at the end of a lane is the same turn on every tab.

**Where it flies.** Four survey lanes in the slip north of Pier 59, between
it and Pier 60, each 40 m long and 3 m apart, flown back and forth at 0.35 m/s
about a metre off the bottom -- the envelope the 15 October 2026 dive flew in,
widened into a survey. After the fourth lane it runs back down the east end
and starts again. The EKF origin is the one that dive's parameters carried
(47.607567, -122.34387), on the north face of Pier 59; the UGPS topside sits
where it did, on the pier's north-west corner.

**How deep.** The bottom here is about 14.5 m down: the DVL read 12.7-15.2 m
of altitude from the surface that morning. It deepens gently to the west.

**Time.** Simulation time `t` is seconds since the simulated dive began. The
simulator is switched on ten minutes into the dive (`BACKFILL_S`), so the
charts open on a flight already under way, and the first fault starts then.
The vehicle's own progress is `mission_time`: simulation time less any time it
spent disarmed, so a failsafe disarm stops it where it is.

Every function of time here is deterministic. Two readers asking about the
same instant get the same vehicle, which is what keeps the tabs consistent.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from ..nav import geo
from .schedule import NAV, Schedule

#: The simulator opens this far into the dive.
BACKFILL_S = 600.0

#: The EKF origin from the 15 October 2026 parameters (AHRS_ORIG_LAT/LON).
ORIGIN = (47.607567, -122.34387)
#: Where the UGPS topside antenna and GNSS compass were on Pier 59.
TOPSIDE = (47.60754, -122.34397)
TOPSIDE_HEADING = 271.6

#: The lanes, as (north offset from the origin, m), flown between these east
#: offsets. West of -28 m is open water; Pier 60's walkway is at about +15.7.
LANE_NORTH = (5.0, 8.0, 11.0, 14.0)
LANE_WEST, LANE_EAST = -28.0, 12.0
SURVEY_SPEED = 0.35
TURN_SPEED = 0.25
TRANSIT_SPEED = 0.30
SURVEY_ALTITUDE = 1.0

#: Where it goes in, and how long the descent takes.
LAUNCH = (2.0, -7.0)            # (north, east), just off the pier corner
DESCENT_S = 45.0


def seabed_depth(north: float, east: float) -> float:
    """Metres from the surface to the bottom at a point in the local frame."""
    return (14.5 - 0.042 * east + 0.03 * north
            + 0.22 * math.sin(east / 6.5) * math.cos(north / 4.1)
            + 0.08 * math.sin(east / 1.9 + north / 2.7))


@dataclass
class Leg:
    kind: str                  # "descent", "lane", "turn", "transit"
    duration: float
    start: tuple[float, float]
    end: tuple[float, float]
    lane: int = -1
    #: For a turn: its centre, radius, and which way it bulges (-1 west).
    centre: tuple[float, float] | None = None
    radius: float = 0.0
    sweep: float = 0.0


def _build_legs() -> tuple[list[Leg], list[Leg]]:
    """(the run-in, the survey loop). Points are (north, east)."""
    first = (LANE_NORTH[0], LANE_EAST)
    d = math.dist(LAUNCH, first)
    run_in = [Leg("descent", DESCENT_S, LAUNCH, LAUNCH),
              Leg("transit", d / TRANSIT_SPEED, LAUNCH, first)]
    loop: list[Leg] = []
    east_to_west = True
    for i, n in enumerate(LANE_NORTH):
        a, b = (LANE_EAST, LANE_WEST) if east_to_west else (LANE_WEST, LANE_EAST)
        loop.append(Leg("lane", abs(b - a) / SURVEY_SPEED, (n, a), (n, b), lane=i))
        if i + 1 < len(LANE_NORTH):
            n2 = LANE_NORTH[i + 1]
            r = (n2 - n) / 2
            # A semicircle outward from the lane's end: west of a lane flown
            # westward, east of one flown eastward.
            loop.append(Leg("turn", math.pi * r / TURN_SPEED, (n, b), (n2, b),
                            centre=(n + r, b), radius=r,
                            sweep=-1.0 if east_to_west else 1.0))
        east_to_west = not east_to_west
    last = loop[-1].end
    back = (LANE_NORTH[0], LANE_EAST)
    loop.append(Leg("transit", math.dist(last, back) / TRANSIT_SPEED, last, back))
    return run_in, loop


RUN_IN, LOOP = _build_legs()
RUN_IN_S = sum(leg.duration for leg in RUN_IN)
LOOP_S = sum(leg.duration for leg in LOOP)


@dataclass
class Vehicle:
    """The simulated ROV at one instant."""

    t: float
    north: float
    east: float
    lat: float
    lon: float
    depth: float               # m below the surface, positive
    altitude: float            # m above the bottom
    seabed: float
    heading: float             # degrees, 0-360
    roll: float
    pitch: float
    speed: float               # m/s over the ground
    v_north: float
    v_east: float
    v_down: float
    #: Body frame: forward, right, down.
    v_fwd: float
    v_right: float
    v_body_down: float
    armed: bool
    mode: str
    leg: str
    lane: int
    surftrak_target: float | None
    volts: float
    amps: float


def _leg_at(tau: float) -> tuple[Leg, float]:
    """The leg the mission is on at mission time `tau`, and how far through it."""
    if tau < RUN_IN_S:
        for leg in RUN_IN:
            if tau < leg.duration:
                return leg, tau / leg.duration
            tau -= leg.duration
    tau = (tau - RUN_IN_S) % LOOP_S
    for leg in LOOP:
        if tau < leg.duration:
            return leg, tau / leg.duration
        tau -= leg.duration
    return LOOP[-1], 1.0


def _point(leg: Leg, f: float) -> tuple[float, float, float]:
    """(north, east, path heading in degrees) a fraction `f` along a leg."""
    if leg.kind == "turn" and leg.centre is not None:
        th, r, side = math.pi * f, leg.radius, leg.sweep
        n = leg.centre[0] - r * math.cos(th)
        e = leg.centre[1] + side * r * math.sin(th)
        # The tangent, so the heading follows the curve.
        return n, e, math.degrees(math.atan2(side * r * math.cos(th),
                                             r * math.sin(th))) % 360.0
    (n0, e0), (n1, e1) = leg.start, leg.end
    n, e = n0 + (n1 - n0) * f, e0 + (e1 - e0) * f
    if leg.kind == "descent":
        return n, e, 90.0
    return n, e, math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360.0


class SimWorld:
    """The time base, the fault clock and the vehicle, shared by every part.

    `t()` is simulation time now. Engaging the simulator sets it to
    `BACKFILL_S`; the first fault begins at that moment.
    """

    def __init__(self, *, backfill_s: float = BACKFILL_S,
                 mono: float | None = None) -> None:
        now = time.monotonic() if mono is None else mono
        self.backfill_s = backfill_s
        self.t0_mono = now - backfill_s
        self.t0_wall = time.time() - backfill_s
        self.schedule = Schedule(start=backfill_s)
        self._lock = threading.Lock()
        #: (system, component, message) -> [counter, last t].
        self._counters: dict[tuple, list] = {}
        #: Counts the simulated DVL keeps, for what the extension forwards.
        self.dvl_valid_reports = 0
        self.dvl_reports = 0
        self.stopped = threading.Event()

    # -- time -----------------------------------------------------------------

    def t(self, mono: float | None = None) -> float:
        return (time.monotonic() if mono is None else mono) - self.t0_mono

    def wall(self, t: float) -> float:
        return self.t0_wall + t

    def fault(self, channel: str, t: float | None = None):
        return self.schedule.fault(channel, self.t() if t is None else t)

    def nav_fault_key(self, t: float) -> tuple[str, float]:
        f, p = self.schedule.fault(NAV, t)
        return (f.key if f else ""), p

    # -- the Pi outage, which reaches everything that talks to the Pi --------

    #: Within a pi_outage fault: the Pi is silent from OUTAGE[0] to OUTAGE[1]
    #: seconds in, the GCS failsafe disarms three seconds after it goes, and
    #: the pilot re-arms at REARM_S.
    OUTAGE = (4.0, 12.0)
    DISARM_AFTER_S = 3.0
    REARM_S = 20.0

    def _outage_into(self, t: float) -> float | None:
        """Seconds into the current pi_outage fault, or None."""
        key, p = self.nav_fault_key(t)
        if key != "pi_outage":
            return None
        return p * 30.0

    def pi_silent(self, t: float) -> bool:
        into = self._outage_into(t)
        return into is not None and self.OUTAGE[0] <= into < self.OUTAGE[1]

    def disarmed(self, t: float) -> bool:
        into = self._outage_into(t)
        return (into is not None
                and self.OUTAGE[0] + self.DISARM_AFTER_S <= into < self.REARM_S)

    def _pauses(self, t: float) -> list[tuple[float, float]]:
        """Every disarmed interval before `t`, as (start, end)."""
        span = (self.OUTAGE[0] + self.DISARM_AFTER_S, self.REARM_S)
        return [(s + span[0], s + span[1])
                for s in self.schedule.windows(NAV, "pi_outage", t)]

    def mission_time(self, t: float) -> tuple[float, float | None]:
        """(the vehicle's own progress, seconds since it was disarmed or None)."""
        lost = 0.0
        since = None
        for a, b in self._pauses(t):
            if t >= b:
                lost += b - a
            elif t >= a:
                lost += t - a
                since = t - a
        return t - lost, since

    def _rise_after(self, t: float) -> float:
        """Extra height from drifting up while disarmed, decaying once re-armed."""
        best = 0.0
        for a, b in self._pauses(t):
            if a <= t < b:
                best = max(best, 0.08 * (t - a))
            elif t >= b:
                peak = 0.08 * (b - a)
                best = max(best, peak * math.exp(-(t - b) / 4.0))
        return best

    # -- the vehicle ------------------------------------------------------------

    def vehicle(self, t: float | None = None) -> Vehicle:
        t = self.t() if t is None else t
        tau, disarmed_for = self.mission_time(t)
        leg, f = _leg_at(tau)
        n, e, path_hdg = _point(leg, f)
        armed = disarmed_for is None

        # A pilot does not fly a ruler line: a slow weave across the lane.
        weave = 0.22 * math.sin(tau / 23.0) + 0.12 * math.sin(tau / 7.3 + 1.1)
        dweave = (0.22 / 23.0) * math.cos(tau / 23.0) + (0.12 / 7.3) * math.cos(tau / 7.3 + 1.1)
        if leg.kind == "lane":
            n += weave
        if leg.kind == "descent":
            speed = 0.02
        elif leg.kind == "turn":
            speed = TURN_SPEED
        elif leg.kind == "transit":
            speed = TRANSIT_SPEED
        else:
            speed = SURVEY_SPEED + 0.025 * math.sin(tau / 13.0)
        if not armed:
            speed = 0.0

        crab = math.degrees(math.atan2(dweave, speed)) if leg.kind == "lane" and speed > 0.05 else 0.0
        heading = (path_hdg + crab + 1.6 * math.sin(tau / 5.7) + 0.7 * math.sin(tau / 1.9)) % 360.0

        seabed = seabed_depth(n, e)
        if leg.kind == "descent":
            frac = min(1.0, f * 1.15)
            altitude = seabed - (seabed - SURVEY_ALTITUDE) * (frac * frac * (3 - 2 * frac))
            altitude = max(altitude, SURVEY_ALTITUDE)
        else:
            altitude = (SURVEY_ALTITUDE + 0.06 * math.sin(tau / 5.3)
                        + 0.03 * math.sin(tau / 1.7))
        altitude += self._rise_after(t)
        depth = seabed - altitude

        rad = math.radians(path_hdg)
        v_n, v_e = speed * math.cos(rad), speed * math.sin(rad)
        if leg.kind == "lane" and armed:
            v_n += dweave
        if leg.kind == "descent":
            v_down = (seabed - SURVEY_ALTITUDE) / DESCENT_S * 1.5 * (1 - abs(2 * f - 1))
        else:
            # Surftrak chasing the bottom: the altitude's own wobble, reversed.
            v_down = -((0.06 / 5.3) * math.cos(tau / 5.3)
                       + (0.03 / 1.7) * math.cos(tau / 1.7))
        if not armed:
            v_down = -0.08
        h = math.radians(heading)
        v_fwd = v_n * math.cos(h) + v_e * math.sin(h)
        v_right = -v_n * math.sin(h) + v_e * math.cos(h)

        lat, lon = geo.offset_ned(ORIGIN[0], ORIGIN[1], n, e)
        roll = 0.6 * math.sin(tau / 4.1) + 0.25 * math.sin(tau / 1.3)
        pitch = (-1.4 if speed > 0.1 else -0.3) - 0.7 * math.sin(tau / 3.3)
        surveying = leg.kind != "descent" and armed
        # The busbar: thrusters pull harder in turns and in the descent.
        work = {"descent": 260.0, "turn": 240.0, "transit": 210.0}.get(leg.kind, 190.0)
        watts = work + 35.0 * math.sin(tau / 9.0) + 15.0 * math.sin(tau / 2.3)
        if not armed:
            watts = 45.0
        volts = 15.85 - 0.45 * watts / 1000.0
        return Vehicle(
            t=t, north=n, east=e, lat=lat, lon=lon, depth=depth,
            altitude=altitude, seabed=seabed, heading=heading, roll=roll,
            pitch=pitch, speed=speed, v_north=v_n, v_east=v_e, v_down=v_down,
            v_fwd=v_fwd, v_right=v_right, v_body_down=v_down, armed=armed,
            mode="Surftrak" if surveying else "Depth Hold", leg=leg.kind,
            lane=leg.lane, surftrak_target=SURVEY_ALTITUDE if surveying else None,
            volts=volts, amps=watts / volts)

    # -- MAVLink counters, shared by everything that reads mavlink2rest ------

    def count(self, key: tuple, rate_hz: float, t: float,
              flowing: bool = True) -> int:
        """mavlink2rest's counter for one message, advanced to `t`.

        One counter per message however many readers ask -- the Navigation
        tab and the DVL capture read the same mavlink2rest on a real vehicle.
        A message that is not flowing holds its count.
        """
        with self._lock:
            c = self._counters.get(key)
            if c is None:
                # As though mavlink2rest had been counting since the dive began.
                c = self._counters[key] = [rate_hz * max(0.0, t) + 1.0, t]
            dt = max(0.0, t - c[1])
            if flowing:
                c[0] += rate_hz * dt
            c[1] = max(c[1], t)
            return int(c[0])

    # -- where each lane was flown, for the Transects tab -----------------------

    def lane_windows(self, t_end: float) -> list[tuple[int, float, float]]:
        """(lane, start t, end t) of every lane finished before `t_end`."""
        out = []
        tau_end, _ = self.mission_time(t_end)
        start = RUN_IN_S
        k = 0
        while start < tau_end:
            for leg in LOOP:
                end = start + leg.duration
                if leg.kind == "lane" and end <= tau_end:
                    out.append((leg.lane, self._t_of_tau(start), self._t_of_tau(end)))
                start = end
                if start >= tau_end:
                    break
            k += 1
            if k > 1000:
                break
        return out

    def _t_of_tau(self, tau: float) -> float:
        """Simulation time at which the mission reached `tau` (no pauses before
        the simulator was switched on, so this is exact for the history)."""
        t = tau
        for _ in range(4):
            got, _since = self.mission_time(t)
            t += tau - got
        return t
