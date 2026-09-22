"""
Following a planned line by hand: how far along, how far off, which way back.

This is guidance for a human pilot flying with a joystick, not autonomy. It
computes numbers and draws a corridor; it never sends the vehicle anything and
never uploads a mission.

Four distinctions the arithmetic has to keep straight, because collapsing any
of them produces a readout that is confidently wrong:

**Left and right are relative to the line, not the bow.** A vehicle crabbing
sideways down a transect is still on the line. Cross-track offset is signed
about the line's own direction of travel, so "3 m right" means three metres to
the right *of the intended track looking along it*, whichever way the ROV
happens to be pointing.

**Along-track progress is not distance flown.** Wandering up and down a lane
must not fill the progress bar. Progress is the projection of the vehicle onto
the line, and it can go backwards.

**Heading, course and the line's bearing are three different angles.** The
compass says where the bow points; course over ground says where the vehicle
is going; the line bearing says where it should be going. They are reported
separately, and nothing here infers sideways motion from yaw.

**Arrival is a claim about now.** A stale or invalid position cannot complete
a lane, however close its last known value was to the end.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import geo
from .model import Fix, Quality
from .plan import Anchor

#: Past this much of the line beyond its end, the vehicle has overrun rather
#: than arrived. Kept generous: an ROV drifts past a mark all the time.
OVERRUN_M = 5.0

#: Within this of an endpoint, the approach cue turns on.
APPROACH_M = 3.0

#: A corridor narrower than this is not a claim this program can support at
#: dead-reckoned accuracy, and is refused with a reason.
MIN_CORRIDOR_M = 0.25


@dataclass
class Guidance:
    """Where the vehicle is relative to one planned line, right now.

    `valid` is the only field worth reading first: everything else is
    meaningless without it, and it is False whenever the position is stale,
    invalid or unreferenced.
    """

    valid: bool = False
    reason: str = ""

    #: What is being followed.
    plan_name: str = ""
    feature_id: str = ""
    lane_index: int | None = None

    #: The line itself.
    line_bearing_deg: float | None = None
    line_length_m: float = 0.0

    #: Along the line from its start, metres. Can exceed the length (overrun)
    #: or go negative (not started).
    along_m: float = 0.0
    remaining_m: float = 0.0
    progress: float = 0.0            # 0..1, clamped

    #: Signed metres from the line: positive is to the right of the direction
    #: of travel.
    cross_track_m: float = 0.0

    #: Bearing from the vehicle to the line's end point.
    bearing_to_end_deg: float | None = None
    #: The turn needed from the vehicle's own heading, when that heading has a
    #: known north reference. None when it has not.
    relative_turn_deg: float | None = None

    #: State: "before", "on", "past", "arrived".
    state: str = "before"
    within_corridor: bool = True
    corridor_m: float = 1.0

    def line(self) -> str:
        if not self.valid:
            return self.reason or "no guidance"
        side = "right" if self.cross_track_m >= 0 else "left"
        return (f"{self.along_m:.1f} / {self.line_length_m:.1f} m · "
                f"{abs(self.cross_track_m):.1f} m {side} of the line")


def cross_track(anchor: Anchor, start: tuple, end: tuple,
                lat: float, lon: float) -> tuple[float, float, float]:
    """(along, cross, line length) in metres, in the line's local frame.

    `cross` is positive to the right of the direction of travel. Computed by
    projecting onto the line in the local east/north frame, which is exact
    Cartesian arithmetic over the distances involved -- not a spherical
    approximation and not screen pixels.
    """
    px, py = anchor.to_local(lat, lon)
    ex, ey = end[0] - start[0], end[1] - start[1]
    length = math.hypot(ex, ey)
    if length < 1e-9:
        return 0.0, math.dist((px, py), start), 0.0
    ux, uy = ex / length, ey / length
    dx, dy = px - start[0], py - start[1]
    along = dx * ux + dy * uy
    # The right-hand normal of a local (east, north) vector pointing along the
    # line is (north, -east) -- rotating the direction clockwise by 90 deg,
    # the way a bearing turns.
    cross = dx * uy - dy * ux
    return along, cross, length


def follow(anchor: Anchor, start: tuple, end: tuple, fix: Fix | None, *,
           plan_name: str = "", feature_id: str = "",
           lane_index: int | None = None, corridor_m: float = 1.0,
           heading_deg: float | None = None,
           heading_referenced: bool = False,
           now_mono: float | None = None) -> Guidance:
    """Guidance for one line, from one position.

    `heading_referenced` says whether the vehicle's heading has a verified
    north reference. Without it the relative turn is left None rather than
    computed from a compass whose datum nobody has checked -- a magnetic
    heading compared against a true bearing is wrong by the local variation,
    which at Seattle is about 15 degrees and would send a pilot the wrong way
    round a lane.
    """
    g = Guidance(plan_name=plan_name, feature_id=feature_id,
                 lane_index=lane_index,
                 corridor_m=max(MIN_CORRIDOR_M, corridor_m))

    ex, ey = end[0] - start[0], end[1] - start[1]
    g.line_length_m = math.hypot(ex, ey)
    if g.line_length_m >= 1e-9:
        g.line_bearing_deg = geo.wrap360(math.degrees(math.atan2(ex, ey)))

    if fix is None:
        g.reason = "no ROV position"
        return g
    if fix.quality is not Quality.OK:
        age = fix.age(now_mono)
        g.reason = (f"position is {fix.quality.value}"
                    + (f", {age:.0f} s old" if age is not None else "")
                    + " — guidance needs a current fix")
        return g
    if g.line_length_m < 1e-9:
        g.reason = "the line has no length"
        return g

    along, cross, length = cross_track(anchor, start, end, fix.lat, fix.lon)
    g.valid = True
    g.along_m = along
    g.cross_track_m = cross
    g.remaining_m = length - along
    g.progress = max(0.0, min(1.0, along / length))
    g.within_corridor = abs(cross) <= g.corridor_m

    end_lat, end_lon = anchor.to_geo(*end)
    d, brg, _ = geo.inverse(fix.lat, fix.lon, end_lat, end_lon)
    g.bearing_to_end_deg = brg if d >= geo.MIN_BEARING_M else None

    if heading_deg is not None and heading_referenced \
            and g.bearing_to_end_deg is not None:
        g.relative_turn_deg = geo.angle_diff(g.bearing_to_end_deg, heading_deg)

    if along < 0:
        g.state = "before"
    elif along > length + OVERRUN_M:
        g.state = "past"
    elif length - along <= APPROACH_M:
        # "arrived" is a claim about *now*, and only a fresh fix can make it.
        g.state = "arrived"
    else:
        g.state = "on"
    return g


@dataclass
class Progress:
    """How much of a line has actually been covered, across a whole run.

    Separate from `Guidance` because it accumulates: it remembers the furthest
    the vehicle has got, so drifting back down a lane does not un-complete it,
    while *forward* progress still has to be earned.
    """

    furthest_m: float = 0.0
    length_m: float = 0.0
    #: Real path length travelled while following, for comparison. Wandering
    #: shows up as this being much larger than `furthest_m`.
    path_m: float = 0.0
    samples: int = 0
    gaps: int = 0
    _last: tuple | None = None

    def update(self, g: Guidance, fix: Fix | None) -> None:
        if not g.valid or fix is None:
            self.gaps += 1
            self._last = None
            return
        self.length_m = g.line_length_m
        self.furthest_m = max(self.furthest_m, min(g.along_m, g.line_length_m))
        here = (fix.lat, fix.lon)
        if self._last is not None:
            step = geo.distance_m(*self._last, *here)
            # A jump is an estimator reset, not swimming; it is not path.
            if step < 25.0:
                self.path_m += step
            else:
                self.gaps += 1
        self._last = here
        self.samples += 1

    @property
    def fraction(self) -> float:
        return 0.0 if not self.length_m else min(
            1.0, self.furthest_m / self.length_m)

    def wander(self) -> float | None:
        """How much further the vehicle swam than the line is long.

        None until there is enough to say. A value near 1.0 means it flew the
        line; 2.0 means it covered twice the distance getting there.
        """
        if self.samples < 5 or self.furthest_m < 1.0:
            return None
        return self.path_m / max(1e-6, self.furthest_m)

    def line(self) -> str:
        if not self.length_m:
            return "not started"
        bits = [f"{self.fraction * 100:.0f}% of {self.length_m:.0f} m"]
        w = self.wander()
        if w is not None and w > 1.25:
            bits.append(f"path {w:.1f}× the line")
        if self.gaps:
            bits.append(f"{self.gaps} gap(s)")
        return " · ".join(bits)
