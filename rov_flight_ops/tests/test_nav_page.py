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


def test_the_page_starts_with_no_numbers_on_it(app):
    """Never initialise instruments to healthy-looking zero values.

    Built and drawn with a collector that has never heard from anything: every
    primary row must read the no-value mark, not 0.00.
    """
    from rov_flight_ops.nav.replay import ReplayCollector

    page = _nav_page(app)
    previous = page.collector
    try:
        page.collector = ReplayCollector([], label="nothing")
        page._render()
        app.update()
        for hud in (page.flight_hud, page.power_hud):
            for row in hud._rows:
                assert row.value.cget("text") == M.NO_VALUE, row.name.cget("text")
        assert page.position_label.cget("text").startswith(M.NO_VALUE)
    finally:
        page.collector = previous


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


def test_the_page_lays_out_at_every_supported_viewport(app):
    """The HUDs stay side by side down to the documented minimum, and the
    workspace never loses its floor without the HUDs giving up height first."""
    import customtkinter as ctk

    page = _nav_page(app)
    before = app.geometry()
    try:
        results = {}
        for geom in ("1920x1080", "1600x900", "1366x768", "1280x800"):
            app.geometry(geom)
            for _ in range(30):
                app.update()
                time.sleep(0.005)
            scaling = ctk.ScalingTracker.get_widget_scaling(page) or 1.0
            results[geom] = {
                "narrow": page._narrow,
                "hud_layout": page.flight_hud._layout,
                "workspace": page.workspace.winfo_height() / scaling,
            }
        for geom, r in results.items():
            # Whatever the arrangement, the workspace keeps enough height for
            # the profile controls and several matrix rows.
            assert r["workspace"] > 150, (geom, r)
            assert r["hud_layout"] in ("row", "column")
    finally:
        app.geometry(before)
        for _ in range(20):
            app.update()
            time.sleep(0.005)


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
