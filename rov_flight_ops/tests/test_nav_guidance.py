"""
Following a line: cross-track sign, along-track progress, and what stale
position does to both.

The sign convention is the one that matters most here. "3 m right" has to
mean three metres to the right of the intended track looking along it, for
every line direction -- including the ones that cross north.
"""

from __future__ import annotations

import time

import pytest

from rov_flight_ops.nav import geo
from rov_flight_ops.nav import guidance as G
from rov_flight_ops.nav.model import Fix, Quality
from rov_flight_ops.nav.plan import Anchor

LAT, LON = 47.6075661, -122.3438752
A = Anchor(LAT, LON)


def _fix_at(east: float, north: float, quality=Quality.OK) -> Fix:
    lat, lon = A.to_geo(east, north)
    return Fix(lat=lat, lon=lon, kind="ekf", quality=quality,
               recv_mono=time.monotonic())


# --------------------------------------------------------------------------
#  Cross-track sign
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bearing", [0.0, 45.0, 90.0, 135.0, 180.0, 225.0,
                                     270.0, 315.0, 359.0])
def test_right_of_the_line_is_right_whichever_way_it_runs(bearing):
    """The sign convention, for every direction including across north."""
    import math

    ln = 30.0
    start = (0.0, 0.0)
    end = (ln * math.sin(math.radians(bearing)),
           ln * math.cos(math.radians(bearing)))
    # A point 4 m to the right of the midpoint, by construction.
    mid = (end[0] / 2, end[1] / 2)
    right = math.radians(bearing + 90.0)
    off = (mid[0] + 4.0 * math.sin(right), mid[1] + 4.0 * math.cos(right))

    g = G.follow(A, start, end, _fix_at(*off))
    assert g.valid
    assert g.cross_track_m == pytest.approx(4.0, abs=0.01), bearing
    assert g.along_m == pytest.approx(ln / 2, abs=0.01)

    left = (mid[0] - 4.0 * math.sin(right), mid[1] - 4.0 * math.cos(right))
    g2 = G.follow(A, start, end, _fix_at(*left))
    assert g2.cross_track_m == pytest.approx(-4.0, abs=0.01), bearing


def test_reversing_the_line_flips_the_side():
    """Right of a line is left of the same line run the other way."""
    start, end = (0.0, 0.0), (0.0, 30.0)     # due north
    off = _fix_at(5.0, 15.0)                 # 5 m east, which is right
    assert G.follow(A, start, end, off).cross_track_m == pytest.approx(5.0, abs=0.01)
    assert G.follow(A, end, start, off).cross_track_m == pytest.approx(-5.0, abs=0.01)


# --------------------------------------------------------------------------
#  Along-track
# --------------------------------------------------------------------------


def test_progress_is_the_projection_not_the_distance_flown():
    start, end = (0.0, 0.0), (0.0, 30.0)
    g = G.follow(A, start, end, _fix_at(10.0, 10.0))
    # 10 m along, though the vehicle is 14 m from the start.
    assert g.along_m == pytest.approx(10.0, abs=0.01)
    assert g.progress == pytest.approx(10.0 / 30.0, abs=0.001)
    assert g.remaining_m == pytest.approx(20.0, abs=0.01)


def test_before_the_start_is_negative_and_says_so():
    start, end = (0.0, 0.0), (0.0, 30.0)
    g = G.follow(A, start, end, _fix_at(0.0, -6.0))
    assert g.along_m == pytest.approx(-6.0, abs=0.01)
    assert g.state == "before"
    assert g.progress == 0.0


def test_past_the_end_is_an_overrun_not_an_arrival():
    start, end = (0.0, 0.0), (0.0, 30.0)
    assert G.follow(A, start, end, _fix_at(0.0, 31.0)).state == "arrived"
    assert G.follow(A, start, end, _fix_at(0.0, 40.0)).state == "past"


def test_the_corridor_is_a_stated_width_not_an_accuracy_claim():
    start, end = (0.0, 0.0), (0.0, 30.0)
    inside = G.follow(A, start, end, _fix_at(0.5, 15.0), corridor_m=1.0)
    outside = G.follow(A, start, end, _fix_at(2.0, 15.0), corridor_m=1.0)
    assert inside.within_corridor and not outside.within_corridor
    # A corridor cannot be narrowed below what is defensible.
    assert G.follow(A, start, end, _fix_at(0.0, 1.0),
                    corridor_m=0.01).corridor_m >= G.MIN_CORRIDOR_M


# --------------------------------------------------------------------------
#  Position that cannot be trusted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("quality", [Quality.STALE, Quality.INVALID,
                                     Quality.NEVER_RECEIVED])
def test_a_position_that_is_not_current_gives_no_guidance(quality):
    start, end = (0.0, 0.0), (0.0, 30.0)
    g = G.follow(A, start, end, _fix_at(0.0, 29.5, quality=quality))
    assert not g.valid
    assert g.state != "arrived", "a stale fix must not complete a lane"
    assert quality.value in g.reason


def test_no_position_at_all_gives_no_guidance():
    g = G.follow(A, (0.0, 0.0), (0.0, 30.0), None)
    assert not g.valid and "no ROV position" in g.reason


def test_a_zero_length_line_is_refused():
    g = G.follow(A, (0.0, 0.0), (0.0, 0.0), _fix_at(1.0, 1.0))
    assert not g.valid and "no length" in g.reason


# --------------------------------------------------------------------------
#  Heading references
# --------------------------------------------------------------------------


def test_the_relative_turn_needs_a_verified_north_reference():
    """A magnetic heading against a true bearing is wrong by the local
    variation -- about 15 degrees at Seattle, enough to send a pilot the
    wrong way round a lane."""
    start, end = (0.0, 0.0), (0.0, 30.0)
    fix = _fix_at(0.0, 10.0)
    unref = G.follow(A, start, end, fix, heading_deg=90.0,
                     heading_referenced=False)
    assert unref.relative_turn_deg is None
    ref = G.follow(A, start, end, fix, heading_deg=90.0,
                   heading_referenced=True)
    assert ref.relative_turn_deg is not None
    # Bow east, line running north: turn about 90 degrees to port.
    assert ref.relative_turn_deg == pytest.approx(-90.0, abs=1.0)


def test_the_line_bearing_is_reported_separately_from_any_heading():
    g = G.follow(A, (0.0, 0.0), (30.0, 0.0), _fix_at(10.0, 0.0),
                 heading_deg=200.0, heading_referenced=True)
    assert g.line_bearing_deg == pytest.approx(90.0, abs=0.01)
    assert geo.angle_diff(g.line_bearing_deg, 200.0) != 0


# --------------------------------------------------------------------------
#  Accumulated progress
# --------------------------------------------------------------------------


def test_wandering_does_not_inflate_completion():
    """The vehicle swims up and down the first third of a lane. Progress must
    reflect how far along it got, not how far it swam."""
    start, end = (0.0, 0.0), (0.0, 30.0)
    prog = G.Progress()
    track = [0, 4, 8, 10, 6, 9, 10, 5, 10]
    for n in track:
        fix = _fix_at(0.0, float(n))
        prog.update(G.follow(A, start, end, fix), fix)
    assert prog.furthest_m == pytest.approx(10.0, abs=0.05)
    assert prog.fraction == pytest.approx(1 / 3, abs=0.01)
    assert prog.path_m > 25.0, "it really did swim that far"
    assert prog.wander() > 2.0


def test_progress_does_not_go_backwards_but_the_readout_does():
    start, end = (0.0, 0.0), (0.0, 30.0)
    prog = G.Progress()
    for n in (0, 20, 5):
        fix = _fix_at(0.0, float(n))
        g = G.follow(A, start, end, fix)
        prog.update(g, fix)
    assert prog.furthest_m == pytest.approx(20.0, abs=0.05)
    # The live readout still says where it is now, which is 5 m.
    assert g.along_m == pytest.approx(5.0, abs=0.05)


def test_a_position_gap_is_counted_and_does_not_add_path():
    start, end = (0.0, 0.0), (0.0, 30.0)
    prog = G.Progress()
    fix = _fix_at(0.0, 2.0)
    prog.update(G.follow(A, start, end, fix), fix)
    prog.update(G.follow(A, start, end, None), None)
    before = prog.path_m
    fix2 = _fix_at(0.0, 4.0)
    prog.update(G.follow(A, start, end, fix2), fix2)
    assert prog.gaps >= 1
    assert prog.path_m == before, "a gap must not be counted as swimming"


def test_an_estimator_jump_is_not_counted_as_distance_flown():
    start, end = (0.0, 0.0), (0.0, 300.0)
    prog = G.Progress()
    for n in (0.0, 2.0, 200.0, 202.0):
        fix = _fix_at(0.0, n)
        prog.update(G.follow(A, start, end, fix), fix)
    # The 198 m jump is a reset, not swimming.
    assert prog.path_m < 10.0
    assert prog.gaps >= 1
