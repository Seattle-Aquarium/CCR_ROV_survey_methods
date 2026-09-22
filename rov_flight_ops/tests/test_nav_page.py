"""
The instruments, the map, waypoints, the session log and replay.

Mostly the explicit acceptance cases: the altitude sequence 10 → 2 → 1.5 →
0.8 → 0.3 m, the watt sequences across 900 and 1,000 W, waypoint capture at
button-press time, and the guarantee that replay cannot touch a vehicle.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rov_flight_ops.gui import navgauges as G
from rov_flight_ops.nav import model as M
from rov_flight_ops.nav import replay, session, waypoints
from rov_flight_ops.nav.model import Fix, Quality

#: A real transect from this year's surveys, if this machine has the flight
#: archive. Searched for rather than hard-coded: the archive lives outside the
#: repository and is not on a CI runner, so these tests skip cleanly there and
#: run against real data on an operator's laptop.
REFERENCE_TRANSECT = ("2026_09_17_EBM_W", "T1.csv")


def _reference_transect() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        hit = (parent / "flights" / "Port_of_Seattle" / "2026"
               / REFERENCE_TRANSECT[0] / "transects" / REFERENCE_TRANSECT[1])
        if hit.is_file():
            return hit
    return None


# --------------------------------------------------------------------------
#  The altitude gauge
# --------------------------------------------------------------------------


def test_the_survey_scale_puts_0_8_at_the_exact_midpoint():
    """A linear 0-1.5 m axis has its midpoint at 0.75. The scale is split at
    0.8 so the altitude these surveys are flown at sits exactly halfway up."""
    assert G.survey_fraction(0.8) == 0.5
    assert G.survey_fraction(0.0) == 0.0
    assert G.survey_fraction(1.5) == 1.0
    assert G.survey_fraction(0.4) == pytest.approx(0.25)
    assert G.survey_fraction(1.15) == pytest.approx(0.75)


def test_the_split_scale_is_monotonic_and_clamped():
    xs = [i / 200.0 for i in range(0, 400)]
    us = [G.survey_fraction(x) for x in xs]
    assert all(b >= a for a, b in zip(us, us[1:], strict=False))
    assert G.survey_fraction(-1.0) == 0.0 and G.survey_fraction(99.0) == 1.0


def test_the_two_halves_of_the_split_have_different_scales():
    """Documented rather than hidden: equal screen distances are not equal
    altitude increments across the midpoint.

    The upper half is the stretched one -- it spreads 0.7 m of altitude over
    the same height the lower half gives 0.8 m -- so a tenth of a metre above
    the survey reference moves the icon further than a tenth below it.
    """
    below = G.survey_fraction(0.8) - G.survey_fraction(0.7)
    above = G.survey_fraction(0.9) - G.survey_fraction(0.8)
    assert above > below
    assert below == pytest.approx(0.5 * 0.1 / 0.8)
    assert above == pytest.approx(0.5 * 0.1 / 0.7)


def test_an_approach_reading_sits_in_the_upper_part_of_its_axis():
    for value in (2.0, 4.0, 10.0, 25.0):
        top = G.approach_top(value)
        assert top > value
        assert 0.55 <= value / top <= 0.90, value
    assert G.approach_top(10.0) == 12.0


@pytest.mark.parametrize("gui", [True])
def test_the_gauge_walks_the_acceptance_altitude_sequence(app, gui):
    """10 → 2 → 1.5 → 0.8 → 0.3 m, then back above range."""
    import customtkinter as ctk

    holder = ctk.CTkFrame(app)
    gauge = G.AltitudeGauge(holder)
    t = 0.0

    def feed(metres, dt=0.5):
        nonlocal t
        t += dt
        gauge.update_reading(M.good(metres, unit="m"), None, now=t)

    feed(10.0)
    assert gauge.mode == "approach" and gauge._top == 12.0
    feed(2.0)
    assert gauge.mode == "approach"
    feed(1.5)
    assert gauge.mode == "survey"                # at the threshold, not above
    feed(0.8)
    assert gauge.mode == "survey"
    assert gauge.fraction(0.8) == 0.5
    feed(0.3)
    assert gauge.mode == "survey"
    assert 0 < gauge.fraction(0.3) < 0.5

    # The hysteresis band: above 1.5 but not yet above 1.7 and held.
    feed(1.6)
    assert gauge.mode == "survey", "1.6 m is inside the band"
    feed(1.8, dt=0.2)
    assert gauge.mode == "survey", "one sample above 1.7 is not enough"
    feed(1.8, dt=2.0)
    assert gauge.mode == "approach", "sustained above 1.7 leaves survey"
    holder.destroy()


def test_an_invalid_altitude_cannot_rescale_the_axis(app):
    """A reading that does not know where the vehicle is has no business
    making a statement about where the vehicle is."""
    import customtkinter as ctk

    holder = ctk.CTkFrame(app)
    gauge = G.AltitudeGauge(holder)
    gauge.update_reading(M.good(10.0, unit="m"), now=1.0)
    top, mode = gauge._top, gauge.mode

    gauge.update_reading(M.invalid("no bottom lock", unit="m"), now=2.0)
    assert gauge._top == top and gauge.mode == mode
    gauge.update_reading(M.good(0.8, unit="m").staled("stopped"), now=3.0)
    assert gauge._top == top and gauge.mode == mode
    holder.destroy()


def test_the_axis_grows_at_once_and_shrinks_only_after_a_hold(app):
    import customtkinter as ctk

    holder = ctk.CTkFrame(app)
    gauge = G.AltitudeGauge(holder)
    gauge.update_reading(M.good(10.0, unit="m"), now=1.0)
    assert gauge._top == 12.0
    gauge.update_reading(M.good(18.0, unit="m"), now=1.5)
    # 18 m wants 21.6 m of axis with headroom, so the next stop up is 30.
    assert gauge._top == 30.0, "a climbing vehicle is never clipped"
    gauge.update_reading(M.good(3.0, unit="m"), now=2.0)
    assert gauge._top == 30.0, "shrinking waits"
    gauge.update_reading(M.good(3.0, unit="m"), now=9.0)
    assert gauge._top == 5.0
    holder.destroy()


# --------------------------------------------------------------------------
#  Surftrak target
# --------------------------------------------------------------------------


def test_a_negative_rftarget_is_the_absence_of_a_target(app):
    """ModeSurftrak uses -1 cm for INVALID_TARGET, which arrives as -0.01 m.
    The fixed 0.8 m survey preference must never stand in for it."""
    from rov_flight_ops.nav.collector import NavCollector
    from rov_flight_ops.nav.mav2rest import Sample

    c = NavCollector("192.0.2.1")
    prev = M.NavSnapshot()
    s = Sample(name="NAMED_VALUE_FLOAT", fresh=True, mono=time.monotonic(),
               message={"name": "RFTarget", "value": -0.01})
    got = c._surftrak_target({"NAMED_VALUE_FLOAT": s}, prev)
    assert got.quality is Quality.INVALID
    assert got.number() is None
    assert "no target" in got.note


def test_a_named_float_for_another_variable_is_not_a_target(app):
    """ArduSub sends nine named floats in one burst; mavlink2rest keeps the
    most recent, whichever that is."""
    from rov_flight_ops.nav.collector import NavCollector
    from rov_flight_ops.nav.mav2rest import Sample

    c = NavCollector("192.0.2.1")
    prev = M.NavSnapshot()
    prev.surftrak_target = M.good(0.75, unit="m")
    s = Sample(name="NAMED_VALUE_FLOAT", fresh=True, mono=time.monotonic(),
               message={"name": "Lights1", "value": 0.5})
    got = c._surftrak_target({"NAMED_VALUE_FLOAT": s}, prev)
    assert got is prev.surftrak_target, "an unrelated variable changes nothing"


def test_a_valid_target_is_taken_in_metres(app):
    from rov_flight_ops.nav.collector import NavCollector
    from rov_flight_ops.nav.mav2rest import Sample

    c = NavCollector("192.0.2.1")
    s = Sample(name="NAMED_VALUE_FLOAT", fresh=True, mono=time.monotonic(),
               message={"name": "RFTarget", "value": 0.75})
    got = c._surftrak_target({"NAMED_VALUE_FLOAT": s}, M.NavSnapshot())
    assert got.quality is Quality.OK and got.number() == pytest.approx(0.75)


# --------------------------------------------------------------------------
#  Altitude source selection
# --------------------------------------------------------------------------


def test_a_zero_range_is_no_bottom_lock_not_an_altitude_of_zero(app):
    from rov_flight_ops.nav.collector import NavCollector
    from rov_flight_ops.nav.mav2rest import Sample

    c = NavCollector("192.0.2.1")
    s = Sample(name="RANGEFINDER", fresh=True, mono=time.monotonic(),
               message={"distance": 0.0})
    got = c._altitude({"RANGEFINDER": s}, {}, M.NavSnapshot())
    assert got.quality is Quality.INVALID and got.number() is None
    assert "no bottom lock" in got.note


def test_distance_sensor_centimetres_are_not_read_as_metres(app):
    """DISTANCE_SENSOR.current_distance is cm; RANGEFINDER.distance is m.
    Confusing them is a hundredfold error in the altitude an ROV flies at."""
    from rov_flight_ops.nav.collector import NavCollector
    from rov_flight_ops.nav.mav2rest import Sample

    c = NavCollector("192.0.2.1")
    ds = Sample(name="DISTANCE_SENSOR", fresh=True, mono=time.monotonic(),
                message={"current_distance": 85, "id": 0})
    got = c._altitude({}, {"DISTANCE_SENSOR": ds}, M.NavSnapshot())
    assert got.number() == pytest.approx(0.85)


# --------------------------------------------------------------------------
#  Waypoints
# --------------------------------------------------------------------------


def _fix(lat=47.62691, lon=-122.39018, **kw):
    kw.setdefault("quality", Quality.OK)
    kw.setdefault("recv_mono", time.monotonic())
    return Fix(lat=lat, lon=lon, **kw)


def test_a_waypoint_records_the_position_at_capture_not_at_naming(tmp_path):
    """The whole point: an operator naming a wolf eel den is looking at the
    eel, and the ROV has moved by the time the typing finishes."""
    store = waypoints.WaypointStore(tmp_path)
    at_press = _fix(47.62691, -122.39018)
    wp = store.capture(at_press)
    assert wp.lat == pytest.approx(47.62691)

    # The vehicle moves, and the rename must not follow it.
    store.rename(wp.id, "wolf eel den")
    again = waypoints.WaypointStore(tmp_path)
    saved = again.points[0]
    assert saved.name == "wolf eel den"
    assert saved.lat == pytest.approx(47.62691)


def test_cancelling_the_rename_keeps_the_point(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    wp = store.capture(_fix())
    # No rename call at all -- which is what cancelling does.
    assert waypoints.WaypointStore(tmp_path).points[0].name == wp.name
    assert wp.name.startswith("WP ")


def test_a_waypoint_is_on_disk_before_success_is_reported(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    store.capture(_fix())
    assert store.path.is_file()
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["waypoints"][0]["lat"] == pytest.approx(47.62691)


def test_capture_is_refused_without_a_usable_position(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    with pytest.raises(waypoints.CaptureRefused, match="no ROV position"):
        store.capture(None)
    with pytest.raises(waypoints.CaptureRefused):
        store.capture(_fix(0.0, 0.0))


def test_a_stale_position_may_be_captured_only_deliberately(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    stale = _fix(quality=Quality.STALE, note="no current position")
    with pytest.raises(waypoints.CaptureRefused, match="stale"):
        store.capture(stale)
    wp = store.capture(stale, allow_stale=True)
    assert wp.stale and wp.fix_quality == "stale"


def test_a_dead_reckoned_waypoint_says_so(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    wp = store.capture(_fix(kind="dead"), origin=(47.6, -122.4),
                       profile="dvl")
    assert wp.dead_reckoned and wp.origin == [47.6, -122.4]
    assert wp.profile == "dvl"


def test_waypoints_survive_a_restart_and_export(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    store.capture(_fix())
    store.capture(_fix(47.628, -122.391))
    reopened = waypoints.WaypointStore(tmp_path)
    assert len(reopened.points) == 2

    gj = reopened.to_geojson()
    assert gj["type"] == "FeatureCollection"
    # GeoJSON is [lon, lat] -- the opposite of everywhere else in this program.
    lon, lat = gj["features"][0]["geometry"]["coordinates"]
    assert lon == pytest.approx(-122.39018) and lat == pytest.approx(47.62691)
    csv_path = reopened.export_csv(tmp_path / "wp.csv")
    assert "dead_reckoned" in csv_path.read_text(encoding="utf-8")


def test_a_map_point_is_a_different_claim_from_the_rovs_position(tmp_path):
    store = waypoints.WaypointStore(tmp_path)
    wp = store.capture_map_point(47.63, -122.40)
    assert wp.origin_of == "map" and wp.fix_kind == "manual"


# --------------------------------------------------------------------------
#  Session log
# --------------------------------------------------------------------------


def test_the_session_log_appends_and_survives_a_torn_line(tmp_path):
    s = session.NavSession(tmp_path, "2026-09-18_112919")
    assert s.open()
    for i in range(5):
        s.event("rov_fix", {"lat": 47.6 + i * 1e-5, "lon": -122.4, "seg": 0})
    s.close("test")

    # A laptop losing power mid-write leaves a truncated final line.
    with s.events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "rov_fix", "lat": 47.6')
    rows = session.read_events(s.events_path)
    assert sum(1 for r in rows if r["kind"] == "rov_fix") == 5


def test_positions_are_logged_at_full_rate_while_display_is_decimated(tmp_path):
    """The samples on disk are the record; what the screen could fit is not."""
    s = session.NavSession(tmp_path, "rate")
    s.open()
    for _ in range(200):
        s.event("rov_fix", {"lat": 47.6, "lon": -122.4, "seg": 0})
    s.close()
    rows = session.read_events(s.events_path, kinds=("rov_fix",))
    assert len(rows) == 200


def test_repeated_diagnostics_are_thinned_but_measurements_are_not(tmp_path):
    s = session.NavSession(tmp_path, "thin")
    s.open()
    for _ in range(50):
        s.event("collector_error", {"error": "timed out"})
    s.close()
    assert len(session.read_events(s.events_path,
                                   kinds=("collector_error",))) < 50


def test_a_track_keeps_its_segments_apart(tmp_path):
    """A break exists because something discontinuous happened; drawing
    across it would invent a movement."""
    s = session.NavSession(tmp_path, "segs")
    s.open()
    for seg in (0, 0, 1, 1, 1):
        s.event("rov_fix", {"lat": 47.6, "lon": -122.4, "seg": seg})
    s.close()
    segments = session.track_from_events(
        session.read_events(s.events_path))
    assert [len(x) for x in segments] == [2, 3]


def test_a_log_that_cannot_be_written_is_reported_not_hidden(tmp_path):
    s = session.NavSession(tmp_path / "nope" / "deeper", "fail")
    s.open()
    s._fh = None                    # as if the disk went away
    s.event("rov_fix", {"lat": 1, "lon": 2})
    assert not s.healthy
    assert "not open" in s.status_line()


# --------------------------------------------------------------------------
#  Replay
# --------------------------------------------------------------------------


def test_replay_has_no_way_to_reach_a_vehicle():
    rc = replay.ReplayCollector([], label="test")
    assert rc.mav is None and rc.allow_writes is False


def test_replay_speed_does_not_change_the_watt_hours():
    """The property that makes replay usable for checking the arithmetic."""
    frames = replay.synthetic_dive(seconds=300, hz=4)
    totals = []
    for speed in (1.0, 8.0, 0.25):
        rc = replay.ReplayCollector(frames, speed=speed, synthetic=True)
        for f in frames:
            rc._publish(f)
        totals.append(round(rc.energy.wh, 6))
    assert len(set(totals)) == 1, totals
    assert totals[0] > 0


def test_a_replayed_reading_is_as_fresh_as_the_moment_it_was_drawn():
    """Two clocks: the frame's own time drives the energy, this laptop's
    drives freshness. Conflating them made every reading read hours old."""
    frames = replay.synthetic_dive(seconds=60, hz=4)
    rc = replay.ReplayCollector(frames, synthetic=True)
    for f in frames:
        rc._publish(f)
    s = rc.snapshot()
    assert s.altitude.quality is Quality.OK
    assert s.altitude.age() < 1.0


def test_a_replayed_dvl_dropout_shows_unknown_not_zero():
    frames = replay.synthetic_dive(seconds=600, hz=4)
    lost = [f for f in frames if "dvl_lock_lost" in f.faults]
    assert lost, "the synthetic dive should contain a lock loss"
    rc = replay.ReplayCollector(frames, synthetic=True)
    rc._publish(lost[len(lost) // 2])
    s = rc.snapshot()
    assert s.speed.number() is None and s.altitude.number() is None
    assert "not zero" in s.speed.note


def test_synthetic_data_is_labelled_everywhere_it_appears():
    frames = replay.synthetic_dive(seconds=30, hz=4)
    rc = replay.ReplayCollector(frames, synthetic=True)
    rc._publish(frames[10])
    s = rc.snapshot()
    assert "SYNTHETIC" in s.depth.note
    assert "synthetic" in s.link.host


def test_the_synthetic_dive_crosses_both_power_thresholds():
    frames = replay.synthetic_dive(seconds=600, hz=4)
    watts = [f.volts * f.amps for f in frames if f.volts and f.amps]
    assert max(watts) > 1000.0
    assert any(900.0 <= w <= 1000.0 for w in watts)


def test_a_real_transect_csv_replays_and_its_energy_agrees_with_the_extractor():
    """Cross-checked against a completely separate implementation.

    The transect extractor accumulates its own Battery_Wh_used column from the
    same recording; this integrates V*I over the real intervals. Agreeing to
    under a percent on a real five-minute transect is the strongest evidence
    available that neither is wrong.
    """
    import csv as _csv

    p = _reference_transect()
    if p is None:
        pytest.skip("the flight archive is not on this machine")

    frames = replay.from_transect_csv(p)
    assert len(frames) > 100
    rc = replay.ReplayCollector(frames, label="T1")
    for f in frames:
        rc._publish(f)

    with p.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(_csv.DictReader(fh))
    theirs = float(rows[-1]["Battery_Wh_used"])
    assert rc.energy.wh == pytest.approx(theirs, rel=0.02)


def test_a_dvl_only_transect_replays_from_its_dead_reckoned_track():
    """`Latitude`/`EKFlat` are empty for the whole of a DVL-only dive -- which
    is the case worth being able to replay."""
    p = _reference_transect()
    if p is None:
        pytest.skip("the flight archive is not on this machine")
    frames = replay.from_transect_csv(p)
    assert all(f.lat is not None for f in frames[:50])
    assert frames[0].altitude_m == pytest.approx(0.813, abs=0.01)


# --------------------------------------------------------------------------
#  The page itself
# --------------------------------------------------------------------------


def _nav_page(app):
    page = app.pages.get("navigation")
    assert page is not None, "the Navigation chapter is not mounted"
    return page


def _demo_plan(page):
    """A plan with one of every kind of feature, adopted by the page.

    Returns a callable that puts the page's own plan back.
    """
    from rov_flight_ops.nav import plan as P

    before = (page.plan, page.history, page.plan_path)
    a = P.Anchor(47.6075661, -122.3438752)
    fresh = P.Plan(name="Panel test", site=page.site["key"])
    fresh.add(P.Line(anchor=a, name="Line 1", points=[(0.0, 0.0), (30.0, 0.0)]))
    fresh.add(P.Line(anchor=a, name="Polyline 1",
                     points=[(0.0, 0.0), (10.0, 5.0), (20.0, 0.0)]))
    fresh.add(P.Rect(anchor=a, name="Rect 1", centre=(0.0, 0.0),
                     length_m=30.0, width_m=20.0, rotation_deg=37.0))
    fresh.add(P.Grid(anchor=a, name="Grid 1", centre=(0.0, 0.0),
                     length_m=30.0, width_m=20.0, rotation_deg=37.0,
                     spacing_m=2.0))
    fresh.add(P.Circle(anchor=a, name="Circle 1", centre=(0.0, 0.0),
                       radius_m=12.0))
    fresh.add(P.Polygon(anchor=a, name="Polygon 1",
                        points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]))
    page._adopt(fresh)

    def restore():
        page.plan, page.history, page.plan_path = before
        if page.editor is not None:
            page.editor.plan, page.editor.selected_id = before[0], None
        page.plan_panel.refresh()
    return restore


class _FakeCanvas:
    """Records what would have been drawn. No window, no pixels of its own."""

    def __init__(self):
        self.texts: list[str] = []

    def create_text(self, _x, _y, text="", **_kw):
        self.texts.append(text)

    def __getattr__(self, _name):          # create_line, create_oval, ...
        return lambda *a, **k: None


class _FakeMap:
    """Just enough map for the editor: a projection at a chosen scale."""

    def __init__(self, anchor, metres_per_pixel: float):
        self.canvas = _FakeCanvas()
        self.centre = (anchor.lat, anchor.lon)
        self._a, self._mpp = anchor, metres_per_pixel

    def xy(self, lat, lon):
        east, north = self._a.to_local(lat, lon)
        return (400 + east / self._mpp, 300 - north / self._mpp)

    def draw(self):
        pass


@pytest.mark.parametrize(
    "metres_per_pixel, wants_labels",
    [(2.0, False),      # a 30 m edge is 15 px: nothing legible fits
     (0.4, False),      # 75 px: the edges fit, the centre block does not
     (0.1, True)])      # 300 px: everything fits
def test_dimensions_are_left_off_when_they_cannot_fit(metres_per_pixel,
                                                      wants_labels):
    """A 30 by 20 m box seen from far enough away has a few dozen pixels of
    edge, and five labels drawn into that space are a smudge rather than a
    diagram -- one that hides the shape it is describing.

    So the labels are gated on screen length. This is the one place in the
    plan where pixels decide anything: what the numbers *are* still comes
    from the geometry, and the inspector shows them at any zoom.
    """
    from rov_flight_ops.gui import navdraw
    from rov_flight_ops.nav import plan as P

    a = P.Anchor(47.6075661, -122.3438752)
    grid = P.Grid(anchor=a, name="EBM box", centre=(0.0, 0.0), length_m=30.0,
                  width_m=20.0, rotation_deg=37.0, spacing_m=2.0)
    fake = _FakeMap(a, metres_per_pixel)
    ed = navdraw.PlanEditor(fake)
    pts = [fake.xy(lat, lon) for lat, lon in grid.geo_points()]
    ed._draw_dimensions(grid, pts)
    drawn = fake.canvas.texts

    if wants_labels:
        # Two long edges and two short ones, each with its own dimension.
        assert drawn.count("30.0 m") == 2, drawn
        assert drawn.count("20.0 m") == 2, drawn
        assert any("600 m²" in x and "10 lanes @ 2 m" in x
                   for x in drawn), drawn
    elif metres_per_pixel == 2.0:
        assert drawn == [], drawn
    else:
        # Only the 30 m edges are long enough to carry a label -- and they
        # must carry *their own* 30, which is what caught the two dimensions
        # being drawn on the wrong pair of edges. The block in the middle
        # would land on top of the shape, so it is left out.
        assert drawn.count("30.0 m") == 2, drawn
        assert "20.0 m" not in drawn, drawn
        assert not any("m²" in x for x in drawn), drawn


def test_a_grid_needs_more_room_for_its_block_than_a_rectangle():
    """A rectangle's centre block is two lines and a grid's is three, so one
    threshold for both is wrong for one of them. At a scale where the
    rectangle can say what it is, the grid cannot yet."""
    from rov_flight_ops.gui import navdraw
    from rov_flight_ops.nav import plan as P

    a = P.Anchor(47.6075661, -122.3438752)
    shape = dict(anchor=a, centre=(0.0, 0.0), length_m=30.0, width_m=20.0,
                 rotation_deg=0.0)
    # 0.22 m per pixel: the 20 m edge is 91 px -- over two lines, under three.
    drawn = {}
    for name, feature in (("rect", P.Rect(**shape)),
                          ("grid", P.Grid(spacing_m=2.0, **shape))):
        fake = _FakeMap(a, 0.22)
        ed = navdraw.PlanEditor(fake)
        pts = [fake.xy(lat, lon) for lat, lon in feature.geo_points()]
        ed._draw_dimensions(feature, pts)
        drawn[name] = fake.canvas.texts

    assert any("m²" in x for x in drawn["rect"]), drawn["rect"]
    assert not any("m²" in x for x in drawn["grid"]), drawn["grid"]
    # Both still carry their edge dimensions; only the block is held back.
    for name in ("rect", "grid"):
        assert drawn[name].count("30.0 m") == 2, (name, drawn[name])


def _matrix_detail(page, key: str):
    return page.matrix._cells[key][4]


def test_a_detail_too_long_for_its_column_ends_in_an_ellipsis(app):
    """A sentence cut off mid-word reads as a rendering fault, which on a
    panel built to be believed is expensive.

    This also guards the way it first went wrong: `cget("font")` on a CTkLabel
    returns an unscaled spec tuple with no `measure`, the AttributeError went
    into a bare except, and nothing was ever elided while every test passed.
    """
    from rov_flight_ops.gui.navpage import _elide, _measurer

    page = _nav_page(app)
    app.update()
    label = _matrix_detail(page, "dvl_position")
    long_text = ("relative aiding, and no confirmed origin to reference it "
                 "against, so there is no latitude and longitude")

    # The measurement has to come from the font actually being drawn with.
    measure = _measurer(label).measure
    assert measure(long_text) > 200

    _elide(label, long_text, 200)
    shown = label.cget("text")
    assert shown != long_text, "nothing was elided"
    assert shown.endswith("…"), shown
    assert long_text.startswith(shown[:-1].rstrip()), shown
    assert measure(shown) <= 200

    # It keeps the whole sentence, for the drawer and for a later re-fit.
    assert label._full == long_text

    # Room enough, and it is left exactly alone.
    _elide(label, long_text, measure(long_text) + 40)
    assert label.cget("text") == long_text


def test_the_detail_column_is_given_a_width_of_its_own(app):
    """Without one the column cannot be narrower than its longest sentence,
    the row runs off the side of the panel, and the frame cuts it."""
    page = _nav_page(app)
    app.update()
    page.matrix._refit()
    app.update()
    assert page.matrix._detail_px >= 120
    for key in ("dvl_position", "ekf", "acoustic"):
        assert _matrix_detail(page, key).cget("width") == page.matrix._detail_px


def test_the_plan_panel_lists_every_kind_of_feature(app):
    """A panel refresh with features actually on the plan.

    Every earlier test refreshed this panel with an empty plan, so the whole
    per-feature branch of `_refresh_list` -- including the glyph lookup --
    never ran, and a wrong module name in it passed six hundred tests. The
    point of this test is to make the list draw real rows.
    """
    page = _nav_page(app)
    restore = _demo_plan(page)
    try:
        page.plan_panel.refresh()
        app.update()
        assert len(page.plan_panel._rows) == 6
        for f in page.plan.features:
            row = page.plan_panel._rows[f.id]
            text = row._name.cget("text")
            assert text.endswith(f.name), text
            # A glyph, not the fallback bullet: every kind we draw has one.
            assert not text.startswith("•"), f"{f.kind} has no glyph"
    finally:
        restore()


def test_every_feature_kind_the_editor_can_draw_has_a_glyph(app):
    """The palette and the list are keyed differently on purpose -- the
    polyline tool makes a line -- so each map is checked against what it is
    for rather than against the other."""
    from rov_flight_ops.gui import navdraw, navplanpanel

    assert set(navdraw.KIND_GLYPH) == {"line", "rect", "grid", "circle",
                                       "polygon"}
    assert set(navplanpanel.GLYPH) == set(navdraw.TOOLS)
    del app


def test_selecting_a_feature_fills_the_inspector(app):
    """The other half of the panel an empty plan never exercised: the
    inspector builds a different set of fields for each kind."""
    page = _nav_page(app)
    restore = _demo_plan(page)
    try:
        for f in page.plan.features:
            page.select(f.id)
            app.update()
            assert page.plan_panel.title.cget("text").endswith(f.name)
            summary = page.plan_panel.summary.cget("text")
            assert summary, f"{f.kind} shows no measurements"
            if f.kind in ("rect", "grid", "circle", "polygon"):
                assert "m²" in summary, summary
    finally:
        restore()


def test_the_page_claims_no_position_before_it_has_one(app):
    """Nothing is initialised to a healthy-looking value.

    The flight HUDs are gone from this chapter, so what matters here is the
    position readout and the strip: with a collector that has never heard from
    anything, neither may claim a fix.
    """
    from rov_flight_ops.nav.replay import ReplayCollector

    page = _nav_page(app)
    previous = page.collector
    try:
        page.collector = ReplayCollector([], label="nothing")
        page._render()
        app.update()
        assert page.position_label.cget("text").startswith(M.NO_VALUE)
        assert page.strip_home._value.cget("text") == M.NO_VALUE
    finally:
        page.collector = previous


def test_the_flight_huds_are_gone_but_their_telemetry_is_not(app):
    """Altitude, velocity, depth and power lost their gauges in this chapter;
    the collector still gathers them, because navigation, the logs and replay
    all still need them."""
    page = _nav_page(app)
    assert not hasattr(page, "flight_hud")
    assert not hasattr(page, "power_hud")
    s = M.NavSnapshot()
    for field in ("altitude", "depth", "speed", "voltage", "current",
                  "watts", "energy_wh"):
        assert hasattr(s, field), field


def test_the_page_survives_a_snapshot_with_nothing_in_it(app):
    """A redraw must not raise on an empty snapshot -- that is the state it
    is in for the first second of every launch."""
    from rov_flight_ops.nav.replay import ReplayCollector

    page = _nav_page(app)
    previous = page.collector
    try:
        page.collector = ReplayCollector([], label="empty")
        for _ in range(3):
            page._render()
            app.update()
    finally:
        page.collector = previous


class _Posted(Exception):
    """Raised by the stub in place of any HTTP the program tries to do."""


def _no_http(monkeypatch):
    """Make every outbound POST and GET fail loudly instead of happening.

    Stronger than watching a vehicle mock: it asserts nothing *left the
    program*. A test that only checks "the ROV did not change" cannot tell a
    write that was never attempted from one that was attempted and refused.
    """
    from rov_flight_ops.nav import mav2rest

    calls = []

    def boom(*a, **k):
        calls.append(a[0] if a else "?")
        raise _Posted(f"the program tried to reach the vehicle: {a[:1]}")

    for name in ("_post", "_get"):
        if hasattr(mav2rest, name):
            monkeypatch.setattr(mav2rest, name, boom)
    return calls


def test_choosing_a_start_site_writes_nothing_to_the_vehicle(app, monkeypatch):
    """The claim the Start dialog is built around, and the one that lets an
    operator set a dive up on the train.

    Selecting OTS, typing a custom start and switching profile are all local:
    the map moves, the coordinates are staged, and the vehicle is not told
    anything. Initialising it is a separate step behind the write interlock.
    """
    from rov_flight_ops.gui import navdialogs
    from rov_flight_ops.nav import bundled

    page = _nav_page(app)
    before_site, before_profile = page.site, page.profile_key
    calls = _no_http(monkeypatch)
    dlg = None
    try:
        dlg = navdialogs.StartDialog(page)
        app.update()

        dlg._pick_ots()
        app.update()
        assert page.site["lat"] == pytest.approx(bundled.PIER59["lat"])
        assert "nothing has been sent to the vehicle" in \
            dlg.site_label.cget("text").lower()

        monkeypatch.setattr(
            navdialogs.ctk, "CTkInputDialog",
            lambda *a, **k: type("D", (), {"get_input": lambda _s:
                                           "47.60, -122.34"})())
        dlg._pick_custom()
        app.update()
        assert page.site["key"] == "custom"

        for key in ("acoustic", "dvl"):
            dlg.mode_var.set(key)
            dlg._mode_changed()
            app.update()

        assert calls == [], f"the program reached the vehicle: {calls}"
        assert page.writes_unlocked is False
    finally:
        if dlg is not None:
            dlg.destroy()
        page.site, page.profile_key = before_site, before_profile
        page.map.site = before_site
        app.update()


def test_the_start_dialog_says_which_step_touches_the_vehicle(app,
                                                              monkeypatch):
    """Three cards, and exactly one of them is a write. An operator who cannot
    tell which is which will either avoid the dialog or trust it too far."""
    from rov_flight_ops.gui import navdialogs

    page = _nav_page(app)
    _no_http(monkeypatch)
    dlg = None
    try:
        dlg = navdialogs.StartDialog(page)
        app.update()
        texts = []

        def walk(w):
            try:
                texts.append(str(w.cget("text")))
            except Exception:
                pass
            for kid in w.winfo_children():
                walk(kid)

        walk(dlg)
        blob = " ".join(texts).lower()
        assert "nothing is sent to the vehicle by choosing a site" in blob
        assert "the only step here that touches the rov" in blob
    finally:
        if dlg is not None:
            dlg.destroy()
        app.update()


def test_writes_are_locked_at_startup_and_in_replay(app):
    """A program that remembers permission to write to a vehicle is a program
    that writes to a vehicle somebody did not expect."""
    from rov_flight_ops.nav.replay import ReplayCollector

    page = _nav_page(app)
    assert page.writes_unlocked is False
    previous = page.collector
    try:
        page.collector = ReplayCollector([], label="replay")
        page.writes_unlocked = True          # even if it were somehow set
        assert page._can_write() is False, "replay has no connection to write to"
    finally:
        page.writes_unlocked = False
        page.collector = previous


def test_the_map_keeps_the_larger_share_of_the_page(app):
    """The layout contract, asserted from the grid rather than from pixels.

    The session window is withdrawn, so measuring a child's width there
    returns 1 and proves nothing. The contract that matters is the one in the
    grid: the map column carries more weight than the side column, and both
    are real columns rather than one squeezing the other out.
    """
    from rov_flight_ops.gui import navpage as NP

    page = _nav_page(app)
    assert NP.MAP_WEIGHT > NP.SIDE_WEIGHT
    assert NP.MAP_WEIGHT / (NP.MAP_WEIGHT + NP.SIDE_WEIGHT) >= 0.6, (
        "the map should keep about two thirds of the page")
    info = page.grid_columnconfigure(0)
    assert int(info["weight"]) == NP.MAP_WEIGHT


def test_a_narrow_page_puts_the_side_column_under_the_map(app):
    """The compact fallback: below the threshold the matrix and the plan
    inspector go under the map rather than being squeezed beside it."""
    page = _nav_page(app)
    was_narrow = page._narrow
    try:
        page._narrow = False
        page._on_resize_to(900.0)
        assert page._narrow is True
        assert int(page.side_col.grid_info()["row"]) == 1

        page._on_resize_to(1600.0)
        assert page._narrow is False
        assert int(page.side_col.grid_info()["column"]) == 1
    finally:
        page._narrow = was_narrow


def test_stopping_the_page_does_not_block_the_window(app):
    """`before_close` must never join a worker: the collector can be inside a
    request to a vehicle that has stopped answering, and the window would
    freeze for as long as that timeout has left."""
    from rov_flight_ops.nav.replay import ReplayCollector

    page = _nav_page(app)
    previous, previous_session = page.collector, page.session
    try:
        page.collector = ReplayCollector(
            replay.synthetic_dive(seconds=60, hz=4), label="stopping")
        page.collector.start("t")
        page.session = None
        began = time.monotonic()
        page.shutdown()
        assert time.monotonic() - began < 0.3
    finally:
        page.collector = previous
        page.session = previous_session


# --------------------------------------------------------------------------
#  Planned features
# --------------------------------------------------------------------------


def _plan_file(tmp_path, features):
    p = tmp_path / "planned.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection",
                             "features": features}), encoding="utf-8")
    return p


def test_a_survey_plan_imports_from_geojson(tmp_path):
    """`survey.Site` carries names, dates and transect times and no
    coordinates, so there is no existing format to reuse for a map overlay."""
    p = _plan_file(tmp_path, [
        {"type": "Feature", "properties": {"name": "EBM East"},
         "geometry": {"type": "Point",
                      "coordinates": [-122.39018, 47.62691]}},
        {"type": "Feature", "properties": {"name": "T1"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-122.390, 47.6269],
                                      [-122.394, 47.6271]]}},
    ])
    features, problems = waypoints.read_planned(p)
    assert not problems
    assert [f.shape for f in features] == ["point", "line"]
    # GeoJSON is [lon, lat]; the import puts them back the right way round.
    assert features[0].points == [(47.62691, -122.39018)]


def test_coordinates_the_wrong_way_round_are_caught_and_named(tmp_path):
    """The single most common way an imported plan ends up in the wrong
    hemisphere."""
    p = _plan_file(tmp_path, [
        {"type": "Feature", "properties": {"name": "swapped"},
         "geometry": {"type": "Point", "coordinates": [47.6, -122.4]}},
    ])
    features, problems = waypoints.read_planned(p)
    assert not features
    assert any("lon/lat order" in x for x in problems)


def test_a_half_usable_plan_gives_the_usable_half(tmp_path):
    """A map that refuses a whole file because one feature is odd helps
    nobody on a boat."""
    p = _plan_file(tmp_path, [
        {"type": "Feature", "properties": {"name": "good"},
         "geometry": {"type": "Point", "coordinates": [-122.39, 47.62]}},
        {"type": "Feature", "properties": {"name": "odd"},
         "geometry": {"type": "GeometryCollection"}},
    ])
    features, problems = waypoints.read_planned(p)
    assert len(features) == 1 and features[0].name == "good"
    assert any("odd" in x for x in problems)


def test_a_missing_plan_is_not_an_error(tmp_path):
    features, problems = waypoints.read_planned(tmp_path / "nothing.geojson")
    assert features == [] and len(problems) == 1


def test_the_map_draws_a_plan_without_a_vehicle(app):
    """The plan is drawn under everything live, and drawing it must not
    depend on there being any telemetry at all.

    The canvas is given an explicit size and laid out before anything is
    asserted. A Tk canvas inside a frame that has not been mapped reports
    1x1, `draw` returns early on it, and the test passes or fails for reasons
    that have nothing to do with the drawing -- which is the same trap that
    makes a widget tree look perfect while showing nothing.
    """
    import customtkinter as ctk

    from rov_flight_ops.gui.navmap import MapCanvas
    from rov_flight_ops.nav.tiles import TileCache

    holder = ctk.CTkFrame(app, width=640, height=420)
    holder.grid(row=0, column=0)
    holder.grid_propagate(False)
    holder.grid_rowconfigure(0, weight=1)
    holder.grid_columnconfigure(0, weight=1)
    cache = TileCache(online=False)
    m = MapCanvas(holder, cache=cache)
    m.grid(row=0, column=0, sticky="nsew")
    for _ in range(20):
        app.update()
        time.sleep(0.005)
    try:
        assert m.canvas.winfo_width() > 20, "the canvas was never laid out"
        m.planned = [
            waypoints.Planned("EBM East", "point", [(47.62691, -122.39018)]),
            waypoints.Planned("T1", "line",
                              [(47.6269, -122.390), (47.6271, -122.394)])]
        m.centre = (47.6270, -122.392)
        m.draw()
        items = m.canvas.find_all()
        assert items, "the map drew nothing at all"
        texts = {m.canvas.itemcget(i, "text") for i in items
                 if m.canvas.type(i) == "text"}
        assert "EBM East" in texts and "T1" in texts
        # Fit frames the plan even with no track at all.
        m.fit_track()
        assert m.centre is not None
    finally:
        cache.stop()
        holder.destroy()


def test_the_map_falls_back_to_a_grid_with_no_tiles(app):
    """No basemap is a supported state, not a broken one: the grid, the scale
    bar and the tracks are most of what the map is for."""
    import customtkinter as ctk

    from rov_flight_ops.gui.navmap import MapCanvas
    from rov_flight_ops.nav.tiles import TileCache

    holder = ctk.CTkFrame(app, width=640, height=420)
    holder.grid(row=0, column=0)
    holder.grid_propagate(False)
    holder.grid_rowconfigure(0, weight=1)
    holder.grid_columnconfigure(0, weight=1)
    cache = TileCache(online=False)
    m = MapCanvas(holder, cache=cache)
    m.grid(row=0, column=0, sticky="nsew")
    for _ in range(20):
        app.update()
        time.sleep(0.005)
    try:
        m.centre = (47.6270, -122.392)
        m.draw()
        texts = " ".join(m.canvas.itemcget(i, "text")
                         for i in m.canvas.find_all()
                         if m.canvas.type(i) == "text")
        assert "grid" in texts, "no grid was drawn"
        assert "no basemap" in texts, "the missing basemap was not declared"
        assert "N" in texts                       # the north arrow
    finally:
        cache.stop()
        holder.destroy()


def test_the_map_says_so_when_there_is_no_geographic_position(app):
    """A DVL-only dive before the origin is set: the shape is real and its
    place on the Earth is not known, and the map must not imply otherwise."""
    import customtkinter as ctk

    from rov_flight_ops.gui.navmap import MapCanvas
    from rov_flight_ops.nav.tiles import TileCache

    holder = ctk.CTkFrame(app, width=640, height=420)
    holder.grid(row=0, column=0)
    holder.grid_propagate(False)
    holder.grid_rowconfigure(0, weight=1)
    holder.grid_columnconfigure(0, weight=1)
    cache = TileCache(online=False)
    m = MapCanvas(holder, cache=cache)
    m.grid(row=0, column=0, sticky="nsew")
    for _ in range(20):
        app.update()
        time.sleep(0.005)
    try:
        m.local_only = True
        m.local_track = [(0.0, 0.0), (5.0, 2.0), (11.0, 7.0)]
        m.draw()
        texts = " ".join(m.canvas.itemcget(i, "text")
                         for i in m.canvas.find_all()
                         if m.canvas.type(i) == "text")
        assert "LOCAL VIEW" in texts
        assert "not a geographic position" in texts
        assert "start" in texts
    finally:
        cache.stop()
        holder.destroy()


def test_the_canvases_pick_the_right_half_of_a_theme_colour(app):
    """`theme` stores every colour as a (light, dark) pair for CustomTkinter,
    which resolves them itself. A raw Tk canvas does not, so the gauges and
    the map have to pick the current mode's half -- and a canvas handed a
    tuple raises rather than drawing."""
    import customtkinter as ctk

    from rov_flight_ops.gui import navgauges, navmap
    from rov_flight_ops.gui import theme as T

    before = ctk.get_appearance_mode()
    try:
        for mode in ("Light", "Dark"):
            ctk.set_appearance_mode(mode)
            for fn in (navgauges._hex, navmap._hex):
                got = fn(T.SURFACE)
                assert isinstance(got, str) and got.startswith("#"), (mode, got)
                assert got == (T.SURFACE[0] if mode == "Light"
                               else T.SURFACE[1])
            assert navgauges._hex("#123456") == "#123456"
    finally:
        ctk.set_appearance_mode(before)


def test_the_gauges_draw_in_both_appearance_modes(app):
    """A gauge that raises on a theme change takes the whole redraw with it."""
    import customtkinter as ctk

    from rov_flight_ops.gui.navgauges import AltitudeGauge, PowerGauge

    before = ctk.get_appearance_mode()
    holder = ctk.CTkFrame(app, width=260, height=340)
    holder.grid(row=0, column=0)
    holder.grid_propagate(False)
    holder.grid_rowconfigure((0, 1), weight=1)
    holder.grid_columnconfigure(0, weight=1)
    alt = AltitudeGauge(holder)
    alt.grid(row=0, column=0, sticky="nsew")
    pwr = PowerGauge(holder)
    pwr.grid(row=1, column=0, sticky="nsew")
    for _ in range(20):
        app.update()
        time.sleep(0.005)
    try:
        for mode in ("Light", "Dark"):
            ctk.set_appearance_mode(mode)
            alt.update_reading(M.good(0.84, unit="m"),
                               M.good(0.75, unit="m"), now=1.0)
            pwr.update_reading(M.good(940.0, unit="W"),
                               M.good(1180.0, unit="W"))
            app.update()
            for gauge in (alt, pwr):
                assert gauge.canvas.find_all(), f"{mode}: nothing drawn"
            # The high-load and over-range states must still be announced.
            texts = " ".join(pwr.canvas.itemcget(i, "text")
                             for i in pwr.canvas.find_all()
                             if pwr.canvas.type(i) == "text")
            assert "HIGH LOAD" in texts and "900" in texts
            assert "1,180" in texts, "the observed peak is not shown"
    finally:
        ctk.set_appearance_mode(before)
        holder.destroy()


def test_an_over_range_reading_keeps_its_true_value(app):
    """Pinned marker, true number, and an over-range mark -- never a marker
    at the top with 1,000 W beside it."""
    import customtkinter as ctk

    from rov_flight_ops.gui.navgauges import PowerGauge

    holder = ctk.CTkFrame(app, width=260, height=300)
    holder.grid(row=0, column=0)
    holder.grid_propagate(False)
    holder.grid_rowconfigure(0, weight=1)
    holder.grid_columnconfigure(0, weight=1)
    pwr = PowerGauge(holder)
    pwr.grid(row=0, column=0, sticky="nsew")
    for _ in range(20):
        app.update()
        time.sleep(0.005)
    try:
        pwr.update_reading(M.good(1180.0, unit="W"))
        app.update()
        texts = " ".join(pwr.canvas.itemcget(i, "text")
                         for i in pwr.canvas.find_all()
                         if pwr.canvas.type(i) == "text")
        assert "1,180" in texts
        assert "OVER" in texts
    finally:
        holder.destroy()


# --------------------------------------------------------------------------
#  The return bearing
# --------------------------------------------------------------------------


def _home(page, snapshot):
    page._render_home(snapshot, time.monotonic())
    return (page.strip_home._title.cget("text"),
            page.strip_home._value.cget("text"),
            page.strip_home._note.cget("text"))


def test_the_bearing_is_to_the_vessel_in_the_acoustic_profile(app):
    page = _nav_page(app)
    before = page.profile_key
    try:
        page.profile_key = "acoustic"
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018)
        # 285 m roughly west-north-west, which is where the vessel sits.
        s.vessel_fix = _fix(47.62714, -122.39396, kind="vessel")
        title, value, _note = _home(page, s)
        assert title == "TO VESSEL"
        assert "°T" in value and "m" in value
        assert "275°T" in value          # checked against the geodesic
        assert "286 m" in value or "285 m" in value
    finally:
        page.profile_key = before


def test_a_stale_vessel_gives_no_bearing_at_all(app):
    """No fresh-target claim from stale data."""
    page = _nav_page(app)
    before = page.profile_key
    try:
        page.profile_key = "acoustic"
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018)
        s.vessel_fix = _fix(47.62714, -122.39396, kind="vessel",
                            quality=Quality.STALE)
        title, value, note = _home(page, s)
        assert title == "TO VESSEL"
        assert value == M.NO_VALUE
        assert "stale" in note
    finally:
        page.profile_key = before


def test_dvl_only_measures_back_to_the_launch_site(app):
    """Same arithmetic as the vessel bearing, a completely different claim,
    so it never keeps the vessel's label."""
    page = _nav_page(app)
    before_profile, before_site = page.profile_key, page.site
    try:
        page.profile_key = "dvl"
        page.site = {"key": "t", "name": "Test", "short": "Test site",
                     "lat": 47.62714, "lon": -122.39396, "zoom": 18}
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018, kind="dead")
        title, value, note = _home(page, s)
        assert title == "TO SITE"
        assert "275°T" in value
        assert "dead-reckoned" in note, "the drift caveat is not shown"
    finally:
        page.profile_key, page.site = before_profile, before_site


def test_the_site_bearing_needs_a_usable_rov_position(app):
    page = _nav_page(app)
    before = page.profile_key
    try:
        page.profile_key = "dvl"
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018, quality=Quality.STALE)
        title, value, note = _home(page, s)
        assert title == "TO SITE" and value == M.NO_VALUE
        assert "no usable ROV position" in note
    finally:
        page.profile_key = before


def test_coincident_positions_say_at_target_rather_than_spinning(app):
    page = _nav_page(app)
    before_profile = page.profile_key
    try:
        page.profile_key = "acoustic"
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018)
        s.vessel_fix = _fix(47.62691, -122.39018, kind="vessel")
        _title, value, note = _home(page, s)
        assert "at/near target" in value
        assert "too close" in note
    finally:
        page.profile_key = before_profile


def test_no_usable_rov_position_gives_no_bearing(app):
    page = _nav_page(app)
    before_profile = page.profile_key
    try:
        page.profile_key = "acoustic"
        s = M.NavSnapshot()
        s.rov_fix = _fix(47.62691, -122.39018, quality=Quality.STALE)
        s.vessel_fix = _fix(47.62714, -122.39396, kind="vessel")
        _title, value, note = _home(page, s)
        assert value == M.NO_VALUE and "no usable ROV position" in note
    finally:
        page.profile_key = before_profile


# --------------------------------------------------------------------------
#  Every module actually imports
# --------------------------------------------------------------------------


def test_every_navigation_module_imports():
    """Including the ones only imported lazily, inside a button's callback.

    `navdialogs` is imported when a drawer is opened, so nothing in the suite
    loaded it -- and it sat in the repository for a session with a real NUL
    byte in it where the source should have had an escape. It imported
    nowhere, and the first person to press Details would have found out.
    """
    import importlib

    for name in ("bundled", "collector", "extensions", "geo", "guidance",
                 "mav2rest", "model", "offline", "origin", "plan", "power",
                 "profiles", "replay", "session", "tiles", "waypoints"):
        importlib.import_module(f"rov_flight_ops.nav.{name}")
    for name in ("navdialogs", "navdraw", "navgauges", "navmap", "navpage",
                 "navplanpanel", "navstatus"):
        importlib.import_module(f"rov_flight_ops.gui.{name}")


def test_no_source_file_contains_a_null_byte():
    """A NUL in a .py file is a SyntaxError at import, and greps right past."""
    root = Path(__file__).resolve().parents[1] / "rov_flight_ops"
    bad = [p for p in root.rglob("*.py") if bytes([0]) in p.read_bytes()]
    assert not bad, [str(p) for p in bad]
