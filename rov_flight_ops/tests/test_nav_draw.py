"""
Drawing a survey plan: what a press, a drag and a release actually mean.

Driven through `PlanEditor` against a stub map, so the interaction is tested
without a window. The geometry itself lives in `test_nav_plan.py`; this is
about the pointer -- that pan and draw stay separate modes, that Escape gives
you back what you had, and that a shape drawn on screen comes out with the
meters it looked like it had.
"""

from __future__ import annotations

import math

import pytest

from rov_flight_ops.nav import plan as P

navdraw = pytest.importorskip("rov_flight_ops.gui.navdraw")

#: Pier 59. Everything here is drawn around it.
LAT, LON = 47.6075661, -122.3438752

#: Meters per pixel for the stub projection. 0.2 is roughly the map at z19.
MPP = 0.2


class _Canvas:
    """Records drawing calls; answers configure() so the cursor can be set."""

    def __init__(self):
        self.texts: list[str] = []

    def create_text(self, _x, _y, text="", **_kw):
        self.texts.append(text)

    def configure(self, **_kw):
        pass

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Map:
    """A flat projection at a fixed scale, centered on Pier 59.

    Good enough for the editor, which only ever asks it to turn a pixel into
    a latitude and back. East is +x, north is -y, as on any screen.
    """

    ORIGIN = (400.0, 300.0)

    def __init__(self, mpp: float = MPP):
        self.canvas = _Canvas()
        self.center = (LAT, LON)
        self._a = P.Anchor(LAT, LON)
        self._mpp = mpp
        self.draws = 0

    def xy(self, lat, lon):
        east, north = self._a.to_local(lat, lon)
        return (self.ORIGIN[0] + east / self._mpp,
                self.ORIGIN[1] - north / self._mpp)

    def latlon_at(self, x, y):
        east = (x - self.ORIGIN[0]) * self._mpp
        north = (self.ORIGIN[1] - y) * self._mpp
        return self._a.to_geo(east, north)

    def draw(self):
        self.draws += 1


class _Event:
    def __init__(self, x, y, state=0):
        self.x, self.y, self.state = x, y, state


@pytest.fixture
def editor():
    changes: list[str] = []
    ed = navdraw.PlanEditor(_Map(), on_change=changes.append)
    ed.plan = P.Plan(name="Test", site="pier59")
    ed.changes = changes
    return ed


def _click(ed, x, y, state=0):
    ed.press(_Event(x, y, state))


def _drag(ed, x0, y0, x1, y1):
    ed.press(_Event(x0, y0))
    ed.motion(_Event(x1, y1))
    ed.release(_Event(x1, y1))


# --------------------------------------------------------------------------
#  Modes
# --------------------------------------------------------------------------


def test_pan_mode_never_creates_anything(editor):
    """The whole reason a tool is armed deliberately: on a boat, with a
    trackpad, a stray click on the map must move the map and nothing else."""
    assert editor.tool == "pan"
    for x in range(380, 460, 10):
        _click(editor, x, 300)
        editor.release(_Event(x, 300))
    assert editor.plan.features == []
    assert editor.changes == []


def test_arming_a_tool_says_what_it_does(editor):
    for tool in navdraw.TOOLS:
        editor.set_tool(tool)
        assert editor.tool == tool
        assert editor.status == navdraw.TOOL_HINT[tool]
    assert editor.drawing is False or editor.tool != "pan"


def test_an_unknown_tool_is_ignored_rather_than_armed(editor):
    editor.set_tool("line")
    editor.set_tool("lasso")
    assert editor.tool == "line"


def test_escape_gives_back_exactly_what_was_there(editor):
    """A canceled draft leaves the plan untouched -- not "mostly" untouched,
    and not with a stub feature to tidy up afterwards."""
    editor.set_tool("line")
    _click(editor, 400, 300)
    assert editor._draft, "nothing was started"
    editor.cancel_draft()
    assert editor.plan.features == []
    assert editor._draft == [] and editor._draft_anchor is None
    assert editor.changes == []


def test_switching_tool_abandons_a_half_drawn_shape(editor):
    editor.set_tool("polygon")
    for x in (400, 450, 450):
        _click(editor, x, 300)
    assert editor._draft
    editor.set_tool("rect")
    assert editor._draft == []
    assert editor.plan.features == []


# --------------------------------------------------------------------------
#  Lines
# --------------------------------------------------------------------------


def test_two_clicks_make_a_line_of_the_meters_it_looked_like(editor):
    """150 pixels at 0.2 m per pixel is 30 m, due east: 30 m on 090 true."""
    editor.set_tool("line")
    _click(editor, 400, 300)
    _click(editor, 550, 300)

    assert len(editor.plan.features) == 1
    line = editor.plan.features[0]
    assert line.kind == "line"
    length, bearing = line.segments()[0]
    assert length == pytest.approx(30.0, abs=0.05)
    assert bearing == pytest.approx(90.0, abs=0.1)
    assert editor.changes, "the edit was not recorded for undo"


@pytest.mark.parametrize("dx, dy, bearing", [
    (0, -150, 0.0),      # up the screen is north
    (150, 0, 90.0),
    (0, 150, 180.0),
    (-150, 0, 270.0),
])
def test_screen_direction_becomes_the_right_true_bearing(editor, dx, dy,
                                                         bearing):
    editor.set_tool("line")
    _click(editor, 400, 300)
    _click(editor, 400 + dx, 300 + dy)
    got = editor.plan.features[0].segments()[0][1]
    assert got == pytest.approx(bearing, abs=0.1)


def test_a_polyline_keeps_every_vertex_and_stays_a_line(editor):
    """Adding a vertex to a line must not turn it into a different kind of
    thing -- the inspector, the export and the guidance all key off `kind`."""
    editor.set_tool("polyline")
    for at in ((400, 300), (475, 300), (475, 225)):
        _click(editor, *at)
    assert editor.finish() is True

    line = editor.plan.features[0]
    assert line.kind == "line"
    assert len(line.points) == 3
    assert len(line.segments()) == 2
    for length, _b in line.segments():
        assert length == pytest.approx(15.0, abs=0.05)


def test_a_polyline_of_one_point_is_not_a_line(editor):
    editor.set_tool("polyline")
    _click(editor, 400, 300)
    assert editor.finish() is False
    assert editor.plan.features == []


# --------------------------------------------------------------------------
#  Boxes and grids
# --------------------------------------------------------------------------


def test_a_dragged_box_comes_out_the_size_it_was_dragged(editor):
    """100 px across by 150 px down, at 0.2 m/px: 20 m wide, 30 m long."""
    editor.set_tool("rect")
    _drag(editor, 400, 300, 500, 450)

    rect = editor.plan.features[0]
    assert rect.kind == "rect"
    assert rect.width_m == pytest.approx(20.0, abs=0.05)
    assert rect.length_m == pytest.approx(30.0, abs=0.05)
    assert rect.measurements()["area_m2"] == pytest.approx(600.0, rel=0.005)


def test_a_box_too_small_to_survey_is_refused_with_a_reason(editor):
    """Better than a 0.4 m rectangle nobody meant to draw."""
    editor.set_tool("rect")
    _drag(editor, 400, 300, 402, 302)
    assert editor.plan.features == []
    assert "at least" in editor.status


def test_a_grid_is_born_with_the_editor_s_spacing(editor):
    editor.set_tool("grid")
    editor.grid_spacing_m = 2.0
    _drag(editor, 400, 300, 500, 450)

    grid = editor.plan.features[0]
    assert grid.kind == "grid"
    assert grid.spacing_m == 2.0
    # 20 m across at 2 m: ten lanes, and they really are inside the box.
    assert grid.lane_count() == 10
    assert len(grid.lanes()) == 10


def test_a_release_that_never_moved_draws_nothing(editor):
    """A click is not a zero-sized box."""
    editor.set_tool("rect")
    editor.press(_Event(400, 300))
    editor.release(_Event(400, 300))
    assert editor.plan.features == []


# --------------------------------------------------------------------------
#  Circles and polygons
# --------------------------------------------------------------------------


def test_a_circle_takes_its_radius_from_the_second_click(editor):
    editor.set_tool("circle")
    _click(editor, 400, 300)
    _click(editor, 460, 300)          # 60 px = 12 m
    circle = editor.plan.features[0]
    assert circle.radius_m == pytest.approx(12.0, abs=0.05)
    assert circle.measurements()["area_m2"] == pytest.approx(
        math.pi * 144, rel=0.01)


def test_a_polygon_needs_three_corners_and_keeps_the_work_meanwhile(editor):
    """Pressing Enter one corner early used to clear the draft, add nothing
    and report success -- two clicks gone with no message. Now it says what it
    wants and keeps what you have."""
    editor.set_tool("polygon")
    _click(editor, 400, 300)
    _click(editor, 500, 300)

    assert editor.finish() is False
    assert len(editor._draft) == 2, "the work was thrown away"
    assert "at least 3 points" in editor.status
    assert editor.plan.features == []

    _click(editor, 500, 400)
    assert editor.finish() is True
    poly = editor.plan.features[0]
    assert poly.kind == "polygon"
    assert poly.measurements()["area_m2"] == pytest.approx(200.0, rel=0.01)


def test_a_refused_box_says_why_rather_than_just_canceled(editor):
    """The explanation used to be set and then overwritten by `cancel_draft`,
    so a box refused for being too small looked like one the operator had
    abandoned on purpose."""
    editor.set_tool("rect")
    _drag(editor, 400, 300, 402, 302)
    assert editor.plan.features == []
    assert "Too small" in editor.status
    assert editor.status != "Canceled."

    # Escape still says "canceled", because that is what it is.
    editor.set_tool("line")
    _click(editor, 400, 300)
    editor.cancel_draft()
    assert editor.status == "Canceled."


# --------------------------------------------------------------------------
#  Snapping and selection
# --------------------------------------------------------------------------


def test_a_new_vertex_snaps_to_one_already_there(editor):
    """Plans that join up cleanly export as plans that join up cleanly."""
    editor.set_tool("line")
    _click(editor, 400, 300)
    _click(editor, 550, 300)
    first = editor.plan.features[0]
    end_lat, end_lon = first.geo_points()[1]

    editor.set_tool("line")
    _click(editor, 550 + 4, 300 + 3)      # within SNAP_PX of the end
    _click(editor, 550, 450)
    second = editor.plan.features[1]

    start = second.geo_points()[0]
    assert start[0] == pytest.approx(end_lat, abs=1e-9)
    assert start[1] == pytest.approx(end_lon, abs=1e-9)


def test_a_vertex_further_off_than_the_snap_radius_is_left_alone(editor):
    editor.set_tool("line")
    _click(editor, 400, 300)
    _click(editor, 550, 300)
    end = editor.plan.features[0].geo_points()[1]

    editor.set_tool("line")
    away = navdraw.SNAP_PX * 3
    _click(editor, 550 + away, 300)
    _click(editor, 550 + away, 450)
    start = editor.plan.features[1].geo_points()[0]
    assert start[0] != pytest.approx(end[0], abs=1e-9) or \
        start[1] != pytest.approx(end[1], abs=1e-9)


def test_snapping_does_not_reach_a_hidden_feature(editor):
    """A hidden feature is one the operator has put out of the way; snapping
    to something invisible is how a plan acquires a vertex nobody placed."""
    editor.set_tool("line")
    _click(editor, 400, 300)
    _click(editor, 550, 300)
    editor.plan.features[0].hidden = True

    editor.set_tool("line")
    _click(editor, 552, 302)
    _click(editor, 552, 450)
    start = editor.map.xy(*editor.plan.features[1].geo_points()[0])
    assert start[0] == pytest.approx(552, abs=0.5)
    assert start[1] == pytest.approx(302, abs=0.5)


def test_clicking_a_feature_selects_it_and_empty_water_clears_it(editor):
    editor.set_tool("rect")
    _drag(editor, 400, 300, 500, 450)
    rect = editor.plan.features[0]
    editor.set_tool("pan")

    editor.press(_Event(450, 375))            # inside the box
    assert editor.selected_id == rect.id

    editor.press(_Event(50, 50))              # well outside it
    assert editor.selected_id is None


def test_a_locked_feature_does_not_offer_its_handles(editor):
    editor.set_tool("rect")
    _drag(editor, 400, 300, 500, 450)
    rect = editor.plan.features[0]
    rect.locked = True
    editor.set_tool("pan")
    editor.select(rect.id)

    corner = editor.map.xy(*rect.geo_points()[0])
    editor.press(_Event(int(corner[0]), int(corner[1])))
    assert editor._grab is None, "a locked feature was grabbed"


def test_nothing_here_can_reach_a_vehicle(editor):
    """`navdraw` is a drawing surface. It has no vehicle to write to, and the
    import graph is where that is guaranteed rather than by convention."""
    import inspect

    source = inspect.getsource(navdraw)
    for forbidden in ("mav2rest", "collector", "requests", "urllib"):
        assert forbidden not in source, forbidden
