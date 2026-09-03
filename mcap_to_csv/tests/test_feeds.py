"""Tracing each column back to the message that produced it."""

from __future__ import annotations

import pytest

from conftest import BASE_EPOCH
from ccr_m2c.feeds import PRECEDENCE, Feed, FeedReport, read_feeds


def _dive(b, *, seconds=20, rangefinder=True, distance_sensor=True,
          local_ned=False, vfr_alt=0.0, alt_gap_at=None, global_pos=True,
          pressure=False):
    """A descending dive.

    Depth has to actually change: the extractor refuses a source that never
    moves, so a fixture holding a constant depth would exercise the fallback
    path in every test rather than the normal one.
    """
    for i in range(seconds):
        t = BASE_EPOCH + i
        descent = 2.0 + i * 0.25                       # metres below the surface
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VFR_HUD",
              {"groundspeed": 0.3, "heading": 0,
               "alt": -descent if vfr_alt == "vary" else vfr_alt})
        if global_pos:
            b.add(t, "GLOBAL_POSITION_INT",
                  {"lat": 476176249, "lon": -1223610207,
                   "relative_alt": int(-descent * 1000)})
        if pressure:
            b.add(t, "SCALED_PRESSURE2",
                  {"press_abs": 1013.25 + descent * 100.5, "temperature": 1200})
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [0.3, 0.0, 0.0],
               "confidence": 98.0}, sysid=255, compid=0)
        if rangefinder and (alt_gap_at is None or not (alt_gap_at <= i < alt_gap_at + 6)):
            b.add(t, "RANGEFINDER", {"distance": 2.0 + i * 0.1, "voltage": 0})
        if distance_sensor:
            b.add(t, "DISTANCE_SENSOR",
                  {"current_distance": 250, "id": 0,
                   "orientation": {"type": "MAV_SENSOR_ROTATION_PITCH_270"}},
                  sysid=255, compid=0)
        if local_ned:
            b.add(t, "LOCAL_POSITION_NED",
                  {"x": float(i), "y": 0.0, "z": 5.0, "vx": 0, "vy": 0, "vz": 0})
    return b.close()


def test_altitude_is_traced_to_the_dvl_range(builder):
    rep = read_feeds([_dive(builder())])
    used = rep.used("Altitude")

    assert used is not None
    assert used.message == "RANGEFINDER.distance"
    assert "DVL A50" in used.detail
    assert used.samples == 20
    assert used.lo == pytest.approx(2.0) and used.hi == pytest.approx(3.9)
    assert "Altitude" in "\n".join(rep.lines())


def test_altitude_falls_back_and_says_so(builder):
    """The fallback is a different instrument, so it must not pass unremarked."""
    rep = read_feeds([_dive(builder(), rangefinder=False)])
    used = rep.used("Altitude")

    assert used.message == "DISTANCE_SENSOR.current_distance"
    assert used.lo == pytest.approx(2.5)
    assert any("fell back" in c and "Altitude" in c for c in rep.concerns())


def test_an_altitude_dropout_is_reported(builder):
    rep = read_feeds([_dive(builder(), alt_gap_at=8)])
    used = rep.used("Altitude")

    assert used.gaps == 1
    assert used.longest_gap == pytest.approx(7.0, abs=0.01)
    assert any("dropped out" in c and "Altitude" in c for c in rep.concerns())


def test_depth_prefers_the_autopilot_over_a_flat_vfr_alt(builder):
    """VFR_HUD.alt is present on every dive but reads zero on some vehicles."""
    rep = read_feeds([_dive(builder(), vfr_alt=0.0)])

    assert rep.used("Depth").message == "GLOBAL_POSITION_INT.relative_alt"
    vfr = next(f for f in rep.columns["Depth"] if f.message == "VFR_HUD.alt")
    assert vfr.samples == 20 and not vfr.used


def test_depth_uses_vfr_alt_when_the_autopilot_depth_is_absent(builder):
    rep = read_feeds([_dive(builder(), vfr_alt="vary", global_pos=False)])
    assert rep.used("Depth").message == "VFR_HUD.alt"


def test_the_dvl_track_prefers_the_ekf_position_when_present(builder):
    with_ned = read_feeds([_dive(builder("a.mcap"), local_ned=True)])
    without = read_feeds([_dive(builder("b.mcap"), local_ned=False)])

    assert with_ned.used("DVLx / DVLy").message == "LOCAL_POSITION_NED.x"
    assert without.used("DVLx / DVLy").message == "VISION_POSITION_DELTA.position_delta"


def test_speed_prefers_the_dvl_over_the_hud(builder):
    rep = read_feeds([_dive(builder())])
    assert rep.used("Velocity_mps").message == "VISION_POSITION_DELTA.position_delta"


def test_a_column_with_no_source_is_called_out(builder):
    b = builder("bare.mcap")
    for i in range(5):
        b.add(BASE_EPOCH + i, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
    rep = read_feeds([b.close()])

    assert rep.used("Altitude") is None
    assert any("Altitude has no source" in c for c in rep.concerns())
    assert rep.used("Heading").message == "ATTITUDE.yaw"


@pytest.mark.parametrize("vfr_alt, local_ned", [
    (0.0, False),        # VFR_HUD.alt flat zero
    (-0.61, False),      # the 2026-08-26 trap: constant, but below -0.5
    (-4.0, False),       # genuinely reporting
    (-4.0, True),        # everything available at once
])
def test_the_report_names_the_source_the_csv_actually_uses(builder, vfr_alt, local_ned):
    """The health report and Depth_Source must never disagree.

    They did once: the extractor's precedence changed and the report kept its
    own copy of the old order, so it spent a release confidently naming a source
    the CSV had not used. Binding them in a test is the only thing that stops
    that happening again.
    """
    from ccr_m2c.mcap_read import read_mcaps

    path = _dive(builder(), vfr_alt=vfr_alt, local_ned=local_ned)
    reported = read_feeds([path]).used("Depth")
    actual = read_mcaps([path]).df["Depth_Source"].dropna()

    assert reported is not None
    assert not actual.empty, "the extractor produced no depth at all"
    # Depth_Source is the column name; the report names message.field
    assert reported.message.startswith(actual.mode()[0].split(".")[0])


def test_a_constant_depth_source_is_skipped_in_the_report_too(builder):
    """The 2026-08-26 trap, seen from the report side: a constant -0.61 m must
    be passed over even though it is the leading candidate with data."""
    b = builder("flat.mcap")
    for i in range(30):
        t = BASE_EPOCH + i
        descent = 2.0 + i * 0.3
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VFR_HUD", {"groundspeed": 0.3, "alt": -0.61, "heading": 0})
        b.add(t, "LOCAL_POSITION_NED",
              {"x": 0.0, "y": 0.0, "z": descent, "vx": 0, "vy": 0, "vz": 0})
    rep = read_feeds([b.close()])

    used = rep.used("Depth")
    assert used.message == "LOCAL_POSITION_NED.z"
    vfr = next(f for f in rep.columns["Depth"] if f.message == "VFR_HUD.alt")
    assert not vfr.used and "constant" in vfr.note


def test_rates_are_reported(builder):
    rep = read_feeds([_dive(builder(), seconds=30)])
    assert rep.used("Altitude").hz == pytest.approx(1.0, abs=0.05)


# --- how the provenance block reads -----------------------------------------

def test_every_candidate_carries_its_own_description():
    for column, cands in PRECEDENCE.items():
        seen: set[str] = set()
        for message, _field, detail in cands:
            assert detail, f"{column}/{message} has no description"
            assert detail not in seen, (
                f"{column}: {message} reuses another candidate's description")
            seen.add(detail)


def _two_source_report() -> FeedReport:
    return FeedReport(columns={"Depth": [
        Feed(message="GLOBAL_POSITION_INT.relative_alt",
             detail="the autopilot's own baro depth",
             used=True, samples=100, hz=2.7, lo=-9.7, hi=0.3),
        Feed(message="SCALED_PRESSURE2.press_abs",
             detail="the external pressure sensor",
             samples=50, hz=1.4, lo=1012.8, hi=1981.8),
    ]})


def test_each_source_is_labelled_with_its_own_description():
    """The description used to be printed once per column, after the last
    source listed -- so "the autopilot's own baro depth" appeared beneath
    SCALED_PRESSURE2 and read as a label for it."""
    text = "\n".join(_two_source_report().lines())

    for line in text.splitlines():
        if "GLOBAL_POSITION_INT" in line:
            assert "baro depth" in line
        if "SCALED_PRESSURE2" in line:
            assert "external pressure" in line


def test_no_description_is_orphaned_at_the_end_of_a_block():
    text = "\n".join(_two_source_report().lines()).rstrip()
    assert not text.endswith("the autopilot's own baro depth")


def test_the_console_report_stays_ascii():
    """It is printed to a Windows console, where cp1252 turns an em-dash into a
    replacement character."""
    rep = _two_source_report()
    text = "\n".join(rep.lines() + rep.concerns() + rep.transect_lines())
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, f"non-ascii in the console report: {bad}"
