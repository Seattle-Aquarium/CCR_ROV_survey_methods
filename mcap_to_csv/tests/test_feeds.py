"""Tracing each column back to the message that produced it."""

from __future__ import annotations

import pytest

from conftest import BASE_EPOCH
from ccr_m2c.feeds import read_feeds


def _dive(b, *, seconds=20, rangefinder=True, distance_sensor=True,
          local_ned=False, vfr_alt=0.0, alt_gap_at=None):
    for i in range(seconds):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VFR_HUD", {"groundspeed": 0.3, "alt": vfr_alt, "heading": 0})
        b.add(t, "GLOBAL_POSITION_INT",
              {"lat": 476176249, "lon": -1223610207, "relative_alt": -5000})
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


def test_depth_skips_a_flat_vfr_alt(builder):
    """VFR_HUD.alt is present on every dive but reads zero on some vehicles."""
    rep = read_feeds([_dive(builder(), vfr_alt=0.0)])
    used = rep.used("Depth")

    assert used.message == "GLOBAL_POSITION_INT.relative_alt"
    vfr = rep.columns["Depth"][0]
    assert vfr.samples == 20 and not vfr.used
    assert "flat zero" in vfr.note


def test_depth_uses_vfr_alt_when_it_really_reports(builder):
    rep = read_feeds([_dive(builder(), vfr_alt=-4.0)])
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


def test_rates_are_reported(builder):
    rep = read_feeds([_dive(builder(), seconds=30)])
    assert rep.used("Altitude").hz == pytest.approx(1.0, abs=0.05)
