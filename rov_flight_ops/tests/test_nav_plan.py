"""
Survey-plan geometry: lines, rotated rectangles, grids and their lanes.

Checked against independent calculations rather than against themselves --
the rectangle's area against the shoelace formula *and* against geodesic
distances between its real corners, the lane spacing against the rule stated
in `Grid`'s docstring.
"""

from __future__ import annotations

import json
import math

import pytest

from rov_flight_ops.nav import geo
from rov_flight_ops.nav import plan as P

LAT, LON = 47.6075661, -122.3438752          # Pier 59 / OTS
ANCHOR = P.Anchor(LAT, LON)


# --------------------------------------------------------------------------
#  Lines
# --------------------------------------------------------------------------


def test_a_line_is_the_length_and_bearing_it_was_given():
    ln = P.Line.from_bearing(LAT, LON, 125.0, 30.0)
    assert ln.length_m == pytest.approx(30.0, abs=1e-6)
    assert ln.bearing_deg == pytest.approx(125.0, abs=1e-6)


def test_a_lines_meters_survive_the_trip_to_real_coordinates():
    """The independent check: measure the exported coordinates geodesically."""
    for bearing in (0.0, 45.0, 125.0, 270.0, 359.0):
        ln = P.Line.from_bearing(LAT, LON, bearing, 30.0)
        a, b = ln.geo_points()
        d, brg, _ = geo.inverse(*a, *b)
        assert d == pytest.approx(30.0, abs=0.001), bearing
        assert geo.angle_diff(brg, bearing) == pytest.approx(0.0, abs=0.01)


def test_retyping_a_line_holds_the_end_that_was_asked_for():
    ln = P.Line.from_bearing(LAT, LON, 90.0, 10.0)
    start, end = ln.points[0], ln.points[1]

    ln.set_length_bearing(45.0, 270.0, fix="start")
    assert ln.points[0] == start
    assert ln.length_m == pytest.approx(45.0)
    assert ln.bearing_deg == pytest.approx(270.0)

    end = ln.points[1]
    ln.set_length_bearing(20.0, 10.0, fix="end")
    assert ln.points[1] == pytest.approx(end)
    assert ln.length_m == pytest.approx(20.0)


def test_a_polyline_reports_each_segment_and_the_total():
    ln = P.Line(anchor=ANCHOR, points=[(0, 0), (0, 10), (10, 10)])
    m = ln.measurements()
    assert m["length_m"] == pytest.approx(20.0)
    assert m["segment_lengths_m"] == pytest.approx([10.0, 10.0])
    assert ln.segments()[0][1] == pytest.approx(0.0)      # due north
    assert ln.segments()[1][1] == pytest.approx(90.0)     # due east


def test_reversing_a_line_reverses_its_bearing():
    ln = P.Line.from_bearing(LAT, LON, 125.0, 30.0)
    ln.reverse()
    assert ln.bearing_deg == pytest.approx(305.0)
    assert ln.length_m == pytest.approx(30.0)


def test_parallels_are_perpendicular_to_the_line_and_evenly_spaced():
    ln = P.Line.from_bearing(LAT, LON, 0.0, 50.0)     # due north
    copies = ln.parallels(3, 2.0, side="right")
    assert len(copies) == 3
    for i, c in enumerate(copies, start=1):
        # Due north, so "right" is due east: the offset is in east only.
        assert c.points[0][0] == pytest.approx(2.0 * i, abs=1e-9)
        assert c.points[0][1] == pytest.approx(0.0, abs=1e-9)
        assert c.length_m == pytest.approx(50.0)


def test_a_line_with_no_direction_has_no_parallels():
    ln = P.Line(anchor=ANCHOR, points=[(0, 0), (0, 0)])
    assert ln.bearing_deg is None
    with pytest.raises(ValueError):
        ln.parallels(2, 2.0)


# --------------------------------------------------------------------------
#  Rectangles
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rot", [0.0, 37.0, 90.0, 125.0, 300.0, 359.5])
def test_a_30_by_20_rectangle_is_600_square_meters_at_any_rotation(rot):
    """The acceptance case, three independent ways."""
    r = P.Rect(anchor=ANCHOR, center=(0, 0), length_m=30.0, width_m=20.0,
               rotation_deg=rot)
    assert r.measurements()["area_m2"] == pytest.approx(600.0)
    # Shoelace over the derived corners.
    assert P.polygon_area(r.corners()) == pytest.approx(600.0, abs=1e-6)
    # And the real corners, measured geodesically on the ground.
    g = r.geo_points()
    sides = [geo.distance_m(*g[i], *g[(i + 1) % 4]) for i in range(4)]
    assert sorted(round(s, 3) for s in sides) == pytest.approx(
        [20.0, 20.0, 30.0, 30.0], abs=0.002)


def test_a_rectangle_stays_a_rectangle_through_edits():
    """Stored as center/length/width/rotation, so no drag can shear it."""
    r = P.Rect(anchor=ANCHOR, center=(0, 0), length_m=30.0, width_m=20.0,
               rotation_deg=37.0)
    for length, width in ((40.0, 15.0), (5.0, 5.0), (12.5, 31.25)):
        r.set_size(length, width)
        c = r.corners()
        sides = [math.dist(c[i], c[(i + 1) % 4]) for i in range(4)]
        assert sides[0] == pytest.approx(sides[2])
        assert sides[1] == pytest.approx(sides[3])
        # Diagonals equal is what makes it a rectangle rather than a rhombus.
        assert math.dist(c[0], c[2]) == pytest.approx(math.dist(c[1], c[3]))
        assert r.measurements()["area_m2"] == pytest.approx(length * width)


def test_resizing_can_pin_a_chosen_corner():
    for corner in range(4):
        r2 = P.Rect(anchor=ANCHOR, center=(0, 0), length_m=30.0, width_m=20.0,
                    rotation_deg=37.0)
        keep = r2.corners()[corner]
        r2.set_size(40.0, 15.0, anchor_corner=corner)
        assert math.dist(keep, r2.corners()[corner]) == pytest.approx(0.0, abs=1e-9)


def test_a_rectangle_refuses_to_be_smaller_than_a_mis_drag():
    r = P.Rect(anchor=ANCHOR, center=(0, 0), length_m=30.0, width_m=20.0)
    with pytest.raises(ValueError):
        r.set_size(0.1, 20.0)


# --------------------------------------------------------------------------
#  Grids
# --------------------------------------------------------------------------


def _grid(**kw):
    base = dict(anchor=ANCHOR, center=(0, 0), length_m=30.0, width_m=20.0,
                rotation_deg=0.0, spacing_m=3.0)
    base.update(kw)
    return P.Grid(**base)


def test_lanes_fill_the_rectangle_and_are_clipped_to_it():
    g = _grid()
    lanes = g.lanes()
    assert lanes, "no lanes were generated"
    for lane in lanes:
        # Along the length axis, so each lane is 30 m long.
        assert lane.length_m == pytest.approx(30.0)
        for e, n in (lane.start, lane.end):
            assert abs(e) <= 10.0 + 1e-6      # half the 20 m width
            assert abs(n) <= 15.0 + 1e-6      # half the 30 m length


def test_the_effective_spacing_is_never_wider_than_requested():
    """The case that was wrong: Python rounds half to even, so a 5 m width at
    2 m spacing gave two lanes 3 m apart -- wider than asked, with a strip
    nobody would have looked at."""
    for across, want in ((5.0, 2.0), (20.0, 3.0), (20.0, 5.0), (7.0, 2.0),
                         (11.0, 2.5), (1.0, 0.3)):
        g = _grid(length_m=10.0, width_m=across, spacing_m=want)
        eff = g.effective_spacing_m()
        if g.lane_count() > 1:
            assert eff <= want + 1e-9, (across, want, g.lane_count(), eff)


def test_the_five_meter_case_gives_three_lanes_at_one_and_a_half():
    g = _grid(length_m=10.0, width_m=5.0, spacing_m=2.0)
    assert g.lane_count() == 3
    assert g.effective_spacing_m() == pytest.approx(1.5)
    offsets = sorted(round(x.start[0], 6) for x in g.lanes())
    assert offsets == pytest.approx([-1.5, 0.0, 1.5])


def test_no_edge_strip_is_left_beyond_half_the_requested_spacing():
    """The outermost lane is inset half a spacing, by the documented rule."""
    for across, want in ((20.0, 3.0), (5.0, 2.0), (13.0, 4.0)):
        g = _grid(length_m=10.0, width_m=across, spacing_m=want)
        offsets = [x.start[0] for x in g.lanes()]
        margin = across / 2.0 - max(abs(o) for o in offsets)
        assert margin == pytest.approx(min(want / 2.0, across / 2.0), abs=1e-6)


def test_a_width_narrower_than_the_spacing_gets_one_centered_lane():
    g = _grid(length_m=2.0, width_m=1.0, spacing_m=3.0)
    assert g.lane_count() == 1
    assert g.effective_spacing_m() is None
    assert g.lanes()[0].start[0] == pytest.approx(0.0)


def test_lane_and_turn_lengths_are_reported_separately():
    g = _grid()
    m = g.measurements()
    assert m["survey_line_m"] == pytest.approx(30.0 * m["lane_count"])
    assert m["transit_m"] > 0
    assert m["total_path_m"] == pytest.approx(m["survey_line_m"] + m["transit_m"])
    assert len(m["lane_lengths_m"]) == m["lane_count"]


def test_ordinary_back_and_forth_turns_are_not_called_excursions():
    """They run along the end edge; a point-on-boundary test called every one
    of them an excursion, which made the number useless."""
    assert _grid().measurements()["transits_outside_boundary"] == 0


def test_alternate_lanes_reverse_when_flying_back_and_forth():
    g = _grid(boustrophedon=True)
    lanes = g.lanes()
    for i in range(len(lanes) - 1):
        assert geo.angle_diff(lanes[i].bearing_deg,
                              lanes[i + 1].bearing_deg) == pytest.approx(
            180.0, abs=1e-6) or geo.angle_diff(
            lanes[i].bearing_deg, lanes[i + 1].bearing_deg) == pytest.approx(
            -180.0, abs=1e-6)
    straight = _grid(boustrophedon=False).lanes()
    assert all(x.bearing_deg == pytest.approx(straight[0].bearing_deg)
               for x in straight)


@pytest.mark.parametrize("corner", [0, 1, 2, 3])
def test_every_start_corner_begins_somewhere_different(corner):
    g = _grid(spacing_m=5.0, start_corner=corner)
    first = g.lanes()[0]
    assert abs(first.start[0]) == pytest.approx(7.5)
    assert abs(first.start[1]) == pytest.approx(15.0)


def test_starting_corners_give_four_distinct_starts():
    starts = {tuple(round(v, 4) for v in _grid(spacing_m=5.0,
                                               start_corner=c).lanes()[0].start)
              for c in range(4)}
    assert len(starts) == 4


def test_lanes_run_the_other_way_when_the_axis_is_swapped():
    across = _grid(lane_axis="width")
    assert across.lanes()[0].length_m == pytest.approx(20.0)
    assert across.lane_count() == math.ceil(30.0 / 3.0)


def test_a_second_pass_runs_at_right_angles():
    g = _grid(second_pass=True)
    first = g.lanes()[0]
    second = g.second_pass_lanes()[0]
    turn = abs(geo.angle_diff(first.bearing_deg, second.bearing_deg))
    assert turn == pytest.approx(90.0, abs=1e-6) or turn == pytest.approx(
        270.0, abs=1e-6)
    assert g.second_pass_lanes()[0].index > len(g.lanes())


def test_rotating_the_grid_rotates_every_lane_by_the_same_amount():
    a = _grid(rotation_deg=0.0).lanes()
    b = _grid(rotation_deg=37.0).lanes()
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert geo.angle_diff(y.bearing_deg, x.bearing_deg) == pytest.approx(
            37.0, abs=1e-6)
        assert y.length_m == pytest.approx(x.length_m)


# --------------------------------------------------------------------------
#  Coverage is a claim, not a consequence
# --------------------------------------------------------------------------


def test_coverage_is_not_established_without_a_swath_width():
    """Flying a lane's center line does not survey the strip either side."""
    cov = _grid().coverage()
    assert cov["established"] is False
    assert "not established" in cov["note"]
    assert "swath" in cov["note"]


def test_a_stated_swath_gives_overlap_and_is_labeled_as_planned():
    g = _grid(width_m=20.0, spacing_m=3.0)
    cov = g.coverage(swath_m=3.5)
    assert cov["established"]
    assert cov["overlap_m"] == pytest.approx(3.5 - g.effective_spacing_m())
    assert cov["gap_between_lanes_m"] == 0.0
    assert "not observed coverage" in cov["note"]


def test_a_swath_narrower_than_the_spacing_reports_a_gap():
    g = _grid(width_m=20.0, spacing_m=4.0)
    cov = g.coverage(swath_m=1.0)
    assert cov["gap_between_lanes_m"] > 0
    assert cov["uncovered_edge_m"] > 0


# --------------------------------------------------------------------------
#  Persistence and export
# --------------------------------------------------------------------------


def test_a_grid_round_trips_as_a_grid_not_as_a_bundle_of_lines():
    pl = P.Plan(name="Pier 59", site="pier59")
    pl.add(P.Line.from_bearing(LAT, LON, 125.0, 30.0))
    pl.add(_grid(name="G1"))
    back = P.Plan.from_json(json.loads(json.dumps(pl.to_json())))
    g = [f for f in back.features if f.kind == "grid"][0]
    assert isinstance(g, P.Grid)
    assert g.spacing_m == pytest.approx(3.0)
    assert len(g.lanes()) == len(_grid().lanes())
    # And the editable semantics survive: change the spacing, get new lanes.
    g.spacing_m = 5.0
    assert len(g.lanes()) != len(_grid().lanes())


def test_a_plan_saves_and_reloads(tmp_path):
    pl = P.Plan(name="Pier 59")
    pl.add(_grid(name="G1"))
    p = tmp_path / P.FILENAME
    assert pl.save(p)
    back = P.Plan.load(p)
    assert back.name == "Pier 59"
    assert back.features[0].measurements()["area_m2"] == pytest.approx(600.0)


def test_geojson_export_is_lon_lat_and_keeps_the_plan_block():
    pl = P.Plan(name="Pier 59")
    pl.add(_grid(name="G1"))
    gj = pl.to_geojson()
    feat = gj["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    lon, lat = feat["geometry"]["coordinates"][0][0]
    assert -123 < lon < -122 and 47 < lat < 48, "lon/lat came out swapped"
    assert feat["properties"]["plan"]["kind"] == "grid"
    assert len(feat["properties"]["lanes"]) == len(_grid().lanes())


def test_a_session_local_plan_refuses_to_be_exported_as_geography():
    """Meters from an unreferenced vehicle frame are not latitude and
    longitude, and writing them out as such invents a position."""
    pl = P.Plan(name="local", crs="local")
    pl.add(P.Line(anchor=P.Anchor(0, 0), points=[(0, 0), (10, 0)]))
    with pytest.raises(ValueError, match="confirmed transform"):
        pl.to_geojson()


def test_lane_state_records_done_and_skipped():
    g = _grid()
    g.set_lane_state(1, "done")
    g.set_lane_state(2, "skipped")
    m = g.measurements()
    assert m["done"] == 1 and m["skipped"] == 1
    assert g.lanes()[0].state == "done"
    g.set_lane_state(1, "")
    assert g.measurements()["done"] == 0


# --------------------------------------------------------------------------
#  Undo
# --------------------------------------------------------------------------


def test_undo_and_redo_walk_the_plan_back_and_forward():
    pl = P.Plan(name="p")
    hist = P.History(pl)
    pl.add(P.Line.from_bearing(LAT, LON, 0.0, 10.0))
    hist.record(pl, "add line")
    pl.add(_grid(name="G1"))
    hist.record(pl, "add grid")
    assert len(pl.features) == 2

    back = hist.undo()
    assert len(back.features) == 1
    back = hist.undo()
    assert len(back.features) == 0
    assert not hist.can_undo

    fwd = hist.redo()
    assert len(fwd.features) == 1
    assert hist.can_redo


def test_a_new_edit_clears_the_redo_branch():
    pl = P.Plan(name="p")
    hist = P.History(pl)
    pl.add(P.Line.from_bearing(LAT, LON, 0.0, 10.0))
    hist.record(pl)
    hist.undo()
    assert hist.can_redo
    pl.add(P.Line.from_bearing(LAT, LON, 90.0, 5.0))
    hist.record(pl)
    assert not hist.can_redo


def test_history_is_bounded():
    pl = P.Plan(name="p")
    hist = P.History(pl)
    for i in range(P.History.LIMIT * 2):
        pl.add(P.Line.from_bearing(LAT, LON, float(i % 360), 5.0))
        hist.record(pl)
    assert len(hist._undo) <= P.History.LIMIT
