"""The EKF and sensor-health report."""

from __future__ import annotations

import pytest

from conftest import BASE_EPOCH
from ccr_m2c.health import HealthReport, read_health

MAG = "MAV_SYS_STATUS_SENSOR_3D_MAG"
AHRS = "MAV_SYS_STATUS_AHRS"
VISION = "MAV_SYS_STATUS_SENSOR_VISION_POSITION"
ALL = f"{MAG} | {AHRS} | {VISION}"

AIDED = ("EKF_ATTITUDE | EKF_VELOCITY_HORIZ | EKF_POS_HORIZ_REL "
         "| EKF_POS_HORIZ_ABS | EKF_POS_VERT_ABS")
DEAD_RECKONING = "EKF_ATTITUDE | EKF_VELOCITY_HORIZ | EKF_POS_HORIZ_REL | EKF_POS_VERT_ABS"


def build(b, *, seconds=20, flags=DEAD_RECKONING, health=ALL,
          compass_var=0.1, vibe=1.0, clipping=0, texts=()):
    for i in range(seconds):
        t = BASE_EPOCH + i
        b.add(t, "EKF_STATUS_REPORT",
              {"flags": flags, "compass_variance": compass_var,
               "velocity_variance": 0.1, "pos_horiz_variance": 0.02,
               "pos_vert_variance": 0.01, "terrain_alt_variance": 0.0})
        b.add(t, "SYS_STATUS",
              {"onboard_control_sensors_present": ALL,
               "onboard_control_sensors_enabled": ALL,
               "onboard_control_sensors_health": health})
        b.add(t, "VIBRATION", {"vibration_x": vibe, "vibration_y": vibe,
                               "vibration_z": vibe, "clipping_0": clipping,
                               "clipping_1": 0, "clipping_2": 0})
        b.add(t, "AHRS", {"error_rp": 0.002, "error_yaw": 0.03,
                          "omegaIx": 0.0, "omegaIy": 0.0, "omegaIz": 0.0})
        b.add(t, "GPS_RAW_INT",
              {"lat": 0, "lon": 0, "fix_type": {"type": "GPS_FIX_TYPE_NO_GPS"},
               "satellites_visible": 0})
        # The columns the report traces back to a message. Without these a
        # "clean" dive would still be reported as having no altitude source,
        # which would be true of the fixture but not of a real flight.
        b.add(t, "RANGEFINDER", {"distance": 2.0, "voltage": 0})
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VFR_HUD", {"groundspeed": 0.4, "alt": -5.0, "heading": 0})
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [0.4, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
    for sev, text in texts:
        b.add(BASE_EPOCH + 1, "STATUSTEXT",
              {"severity": {"type": sev}, "text": text, "id": 0, "chunk_seq": 0})
    return b.close()


def test_reports_dead_reckoning_when_there_is_no_absolute_fix(builder):
    rep = read_health([build(builder())])

    assert not rep.had_absolute_position
    assert rep.ekf_flags["EKF_POS_HORIZ_REL"] == pytest.approx(100.0)
    assert "EKF_POS_HORIZ_ABS" not in rep.ekf_flags
    assert any("dead reckoning" in c for c in rep.concerns())
    assert "NO -- dead reckoning only" in "\n".join(rep.lines())


def test_reports_an_absolute_fix_when_the_ekf_had_one(builder):
    rep = read_health([build(builder(), flags=AIDED)])

    assert rep.had_absolute_position
    assert not any("dead reckoning" in c for c in rep.concerns())


def test_the_ahrs_bit_is_not_cried_wolf_over_without_gps(builder):
    """ArduSub reports AHRS unhealthy on every no-GPS dive; that is not a fault."""
    rep = read_health([build(builder(), health=f"{MAG} | {VISION}")])   # AHRS missing

    assert rep.unhealthy[AHRS] == pytest.approx(100.0)
    assert not any("AHRS" in c for c in rep.concerns())     # explained, not alarmed
    assert "expected with no GPS/USBL" in "\n".join(rep.lines())


def test_the_ahrs_bit_is_a_real_concern_when_there_was_a_fix(builder):
    rep = read_health([build(builder(), flags=AIDED, health=f"{MAG} | {VISION}")])
    assert any("AHRS" in c for c in rep.concerns())


def test_an_unhealthy_dvl_is_named_in_plain_language(builder):
    rep = read_health([build(builder(), health=f"{MAG} | {AHRS}")])     # VISION missing
    concerns = "\n".join(rep.concerns())
    assert "VISION_POSITION" in concerns and "DVL" in concerns


def test_a_high_variance_is_flagged(builder):
    rep = read_health([build(builder(), compass_var=1.4)])
    assert rep.variances["compass_variance"][2] == pytest.approx(1.4)
    assert any("compass variance" in c and "1.40" in c for c in rep.concerns())


def test_a_middling_compass_variance_earns_a_calibration_note(builder):
    rep = read_health([build(builder(), compass_var=0.7)])
    assert any("calibration" in c for c in rep.concerns())


def test_a_clean_dive_reports_nothing_of_concern(builder):
    rep = read_health([build(builder(), flags=AIDED, compass_var=0.05)])
    assert rep.concerns() == []
    assert "Nothing of concern found." in "\n".join(rep.lines())


def test_vibration_and_clipping_are_flagged(builder):
    rep = read_health([build(builder(), flags=AIDED, vibe=45.0, clipping=7)])
    concerns = "\n".join(rep.concerns())
    assert "Vibration" in concerns
    assert "clipping" in concerns.lower()


def test_only_warnings_and_worse_are_surfaced(builder):
    rep = read_health([build(builder(), texts=[
        ("MAV_SEVERITY_INFO", "rangefinder target is 0.80 meters"),
        ("MAV_SEVERITY_ERROR", "EKF3 waiting for GPS config data"),
        ("MAV_SEVERITY_WARNING", "EKF3 IMU0 stopped aiding"),
    ])])
    shown = [t for _r, _s, t, _c in rep.messages]
    assert "EKF3 waiting for GPS config data" in shown
    assert "EKF3 IMU0 stopped aiding" in shown
    assert not any("rangefinder target" in t for t in shown)
    # most severe first
    assert rep.messages[0][1] == "ERROR"


def test_a_recording_with_no_diagnostics_still_reports(builder):
    b = builder("bare.mcap")
    for i in range(5):
        b.add(BASE_EPOCH + i, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
    rep = read_health([b.close()])
    assert rep.ekf_flags == {}
    assert "no EKF_STATUS_REPORT" in "\n".join(rep.lines())
