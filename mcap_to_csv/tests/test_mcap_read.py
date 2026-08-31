"""Reading a recording into the per-second table."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from conftest import BASE_EPOCH, straight_north_dive
from ccr_m2c.mcap_read import (
    _add_depth, _msg_type, _sysid_rank, calculate_area, calculate_width,
    probe_mcaps, read_mcaps, select_mcaps,
)


def test_reads_every_expected_column(builder):
    path = straight_north_dive(builder(), seconds=20).close()
    res = read_mcaps([path])
    df = res.df

    assert len(df) == 20
    assert df["Date"].iloc[0] == "2026-08-26"
    assert df["Time"].iloc[0] == "10:00:00"
    assert df["Datetime_UTC"].iloc[0] == "2026-08-26T17:00:00Z"

    for col in ("Altitude", "Heading", "Depth", "Latitude", "DVLx", "DVLy",
                "Battery_V", "Mode", "Water_temp_C", "Lights_pct"):
        assert df[col].notna().all(), f"{col} has gaps"

    assert df["Mode"].iloc[0] == "MANUAL"          # custom_mode 19
    assert df["Battery_V"].iloc[0] == pytest.approx(14.0)
    assert df["Battery_A"].iloc[0] == pytest.approx(2.0)
    assert df["Battery_W"].iloc[0] == pytest.approx(28.0)
    assert df["Lights_pct"].iloc[0] == pytest.approx(50.0)
    assert df["GPS_fix_type"].iloc[0] == "GPS_FIX_TYPE_RTK_FIXED"


def test_dvl_integrates_north_when_heading_north(builder):
    """Heading 0 and a +x body delta must accumulate as pure North."""
    path = straight_north_dive(builder(), seconds=20).close()
    df = read_mcaps([path]).df

    # 5 deltas per second, 0.1 m each -> 0.5 m/s north, nothing east.
    assert df["DVLy"].abs().max() == pytest.approx(0.0, abs=1e-9)
    travelled = df["DVLx"].iloc[-1] - df["DVLx"].iloc[0]
    assert travelled == pytest.approx(0.5 * 19, rel=0.02)
    assert df["Velocity_mps"].iloc[5] == pytest.approx(0.5, rel=1e-6)


def test_dvl_rotates_body_deltas_by_yaw(builder):
    """A +x body delta while facing east must accumulate as pure East."""
    b = builder("east.mcap")
    for i in range(10):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": math.pi / 2})
        b.add(t + 0.5, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [1.0, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        b.add(t, "GLOBAL_POSITION_INT",
              {"lat": 476176249, "lon": -1223610207, "relative_alt": -1000})
    df = read_mcaps([b.close()]).df

    assert df["DVLx"].abs().max() == pytest.approx(0.0, abs=1e-9)
    travelled = df["DVLy"].iloc[-1] - df["DVLy"].iloc[0]
    assert travelled == pytest.approx(9.0, rel=1e-6)      # 1 m/s for 9 seconds
    assert df["Heading"].iloc[0] == pytest.approx(90.0, abs=1e-6)


def test_local_position_ned_wins_over_the_dvl_integration(builder):
    """A recording that does carry LOCAL_POSITION_NED must use it verbatim."""
    b = builder("lpn.mcap")
    for i in range(10):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [99.0, 99.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        b.add(t, "LOCAL_POSITION_NED",
              {"x": float(i), "y": -float(i), "z": 3.0,
               "vx": 0, "vy": 0, "vz": 0})
    res = read_mcaps([b.close()])

    assert res.dvl_source == "LOCAL_POSITION_NED"
    assert res.df["DVLx"].iloc[-1] == pytest.approx(9.0)
    assert res.df["DVLy"].iloc[-1] == pytest.approx(-9.0)
    assert res.df["NEDz"].iloc[-1] == pytest.approx(3.0)
    assert res.df["Depth"].iloc[-1] == pytest.approx(-3.0)   # -NEDz


def test_the_dvl_column_never_switches_source_mid_dive(builder):
    """The DVL extension publishes deltas before the EKF publishes position.

    If the choice were made per message, those first seconds would hold
    integrated deltas and the rest of the dive the EKF's own position, in one
    column, labelled as if it were all the latter.
    """
    b = builder("late_lpn.mcap")
    for i in range(20):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        # deltas from the start, wildly different from the EKF's numbers
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [50.0, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        b.add(t, "GLOBAL_POSITION_INT", {"lat": 0, "lon": 0, "relative_alt": -1000})
        if i >= 10:                      # the EKF only starts halfway through
            b.add(t, "LOCAL_POSITION_NED",
                  {"x": float(i), "y": 0.0, "z": 1.0, "vx": 0, "vy": 0, "vz": 0})

    res = read_mcaps([b.close()])
    assert res.dvl_source == "LOCAL_POSITION_NED"

    dvlx = res.df["DVLx"]
    # blank before the chosen source starts, never the integrated deltas
    assert dvlx.iloc[:10].isna().all()
    assert dvlx.iloc[10:].notna().all()
    assert dvlx.max() == pytest.approx(19.0)     # not the 950 m of deltas


def test_heading_averages_around_the_wrap(builder):
    """Samples either side of north must average to north, not to south."""
    b = builder("wrap.mcap")
    t = BASE_EPOCH
    for yaw_deg in (359.0, 1.0, 359.0, 1.0):
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0,
                              "yaw": math.radians(yaw_deg)})
    b.add(t, "GLOBAL_POSITION_INT", {"lat": 0, "lon": 0, "relative_alt": -1000})
    df = read_mcaps([b.close()]).df

    heading = df["Heading"].iloc[0]
    assert min(heading, 360.0 - heading) == pytest.approx(0.0, abs=1e-6)


def test_gaps_are_holes_not_flat_lines(builder):
    """A stream that stops must go blank, not hold its last value forever."""
    b = builder("gap.mcap")
    b.add(BASE_EPOCH, "RANGEFINDER", {"distance": 2.0, "voltage": 0})
    b.add(BASE_EPOCH, "GLOBAL_POSITION_INT",
          {"lat": 0, "lon": 0, "relative_alt": -1000})
    # ... then nothing from the rangefinder for a minute
    b.add(BASE_EPOCH + 60, "GLOBAL_POSITION_INT",
          {"lat": 0, "lon": 0, "relative_alt": -1000})
    df = read_mcaps([b.close()]).df

    assert len(df) == 61                      # dense one-second grid
    assert df["Altitude"].iloc[0] == pytest.approx(2.0)
    assert df["Altitude"].iloc[5] == pytest.approx(2.0)     # inside the hold
    assert pd.isna(df["Altitude"].iloc[30])                 # past it


def test_two_recordings_merge_as_one_dive(builder):
    """A dive split across files keeps one continuous DVL track."""
    a = straight_north_dive(builder("a.mcap"), seconds=10).close()
    b = straight_north_dive(builder("b.mcap"), seconds=10,
                            start=BASE_EPOCH + 10).close()
    df = read_mcaps([b, a]).df                # deliberately out of order

    assert len(df) == 20
    assert df["Time"].iloc[0] == "10:00:00"
    assert df["Time"].iloc[-1] == "10:00:19"
    assert df["DVLx"].is_monotonic_increasing


def test_speed_source_is_decided_once_for_the_whole_dive(builder):
    """One file missing the DVL must not switch Velocity_mps to groundspeed.

    Otherwise the column would silently hold two different quantities, and the
    seam would fall wherever the recorder happened to roll a file.
    """
    a = builder("with_dvl.mcap")
    for i in range(5):
        t = BASE_EPOCH + i
        a.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        a.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [0.5, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        a.add(t, "VFR_HUD", {"groundspeed": 9.9, "alt": 0, "heading": 0})

    b = builder("no_dvl.mcap")
    for i in range(5):
        t = BASE_EPOCH + 5 + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VFR_HUD", {"groundspeed": 9.9, "alt": 0, "heading": 0})

    df = read_mcaps([a.close(), b.close()]).df

    assert df["Velocity_mps"].iloc[0] == pytest.approx(0.5)
    # the second file's groundspeed is ignored, and the held DVL value expires
    assert 9.9 not in set(df["Velocity_mps"].dropna())


def test_stray_recording_is_left_out(builder):
    """A file from another day must not stretch the shared timeline."""
    near = straight_north_dive(builder("near.mcap"), seconds=5).close()
    far = straight_north_dive(builder("far.mcap"), seconds=5,
                              start=BASE_EPOCH + 5 * 86400).close()
    kept, warnings = select_mcaps([near, far])

    assert [p.name for p in kept] == ["near.mcap"]
    assert any("different dive" in w for w in warnings)


def _damage_index(path, keep: float = 0.6):
    """Chop off the summary section and put the trailing magic back.

    This is what a recording cut short in the field looks like: the data records
    are there, the index that points at them is not.
    """
    raw = path.read_bytes()
    path.write_bytes(raw[:int(len(raw) * keep)] + b"\x89MCAP0\r\n")
    return path


def test_a_recording_with_a_damaged_index_is_still_read(builder, tmp_path):
    """A dive cut short cannot be re-flown, so it must not be written off."""
    path = _damage_index(straight_north_dive(builder(), seconds=120).close())

    # the indexed reader cannot open it at all
    from mcap.reader import make_reader
    with pytest.raises(Exception):
        with open(path, "rb") as fh:
            make_reader(fh).get_summary()

    res = read_mcaps([path])
    assert len(res.df) > 10, "no telemetry recovered"
    assert res.df["Altitude"].notna().any()
    assert res.df["DVLx"].notna().any()
    assert any("index damaged" in w for w in res.warnings)
    assert any("recovered" in w for w in res.warnings)


def test_a_damaged_recording_still_probes_and_sorts(builder):
    """Its start time comes from the front of the file, so it still orders."""
    path = _damage_index(straight_north_dive(builder(), seconds=120).close())
    info = probe_mcaps([path])[0]

    assert info.usable and info.index_damaged
    assert info.end is None
    assert "no index" in info.local_span()
    assert "2026-05" not in info.local_span()

    kept, _warnings = select_mcaps([path])
    assert kept == [path]


def test_a_damaged_recording_merges_with_a_clean_one(builder):
    """The two halves of a dive must still line up on one timeline."""
    good = straight_north_dive(builder("good.mcap"), seconds=30).close()
    bad = _damage_index(straight_north_dive(
        builder("bad.mcap"), seconds=120, start=BASE_EPOCH + 30).close())

    res = read_mcaps([good, bad])
    assert res.df["Time"].iloc[0] == "10:00:00"
    assert len(res.df) > 30, "the damaged half contributed nothing"
    assert res.df["DVLx"].is_monotonic_increasing


def test_unreadable_file_is_reported_not_fatal(builder, tmp_path):
    good = straight_north_dive(builder(), seconds=5).close()
    junk = tmp_path / "truncated.mcap"
    junk.write_bytes(b"\x89MCAP0\r\n not really an mcap")

    kept, warnings = select_mcaps([good, junk])
    assert [p.name for p in kept] == [good.name]
    assert any("truncated.mcap" in w for w in warnings)

    infos = {i.path.name: i for i in probe_mcaps([good, junk])}
    assert infos[good.name].usable
    assert not infos["truncated.mcap"].usable


def test_probe_reports_the_local_span(builder):
    path = straight_north_dive(builder(), seconds=30).close()
    info = probe_mcaps([path])[0]
    assert info.usable
    assert "2026-08-26" in info.local_span()
    assert "10:00:00" in info.local_span()


# ---- unit-level helpers ---------------------------------------------------

def test_topic_parsing():
    assert _msg_type("mavlink/1/1/VFR_HUD") == "VFR_HUD"
    assert _msg_type("mavlink/1/1/GPS_RAW_INT/fix_type") is None
    assert _msg_type("mavlink/out") is None
    assert _msg_type("services/beacon/log") is None
    # the autopilot outranks anything else offering the same message
    assert _sysid_rank("mavlink/1/1/DISTANCE_SENSOR") < _sysid_rank("mavlink/255/0/DISTANCE_SENSOR")


def test_footprint_scales_with_altitude():
    assert calculate_width(0.82) == pytest.approx(1.10)
    assert calculate_area(0.82) == pytest.approx(0.99)
    assert calculate_width(1.64) == pytest.approx(2.20)      # linear
    assert calculate_area(1.64) == pytest.approx(3.96)       # quadratic
    assert calculate_width(0.0) == 0.0
    assert calculate_area(-1.0) == 0.0


def test_depth_prefers_vfr_then_relative_alt_then_pressure():
    df = pd.DataFrame({
        "VFR_alt": [-3.0, 0.0, 0.0, np.nan],
        "Relative_alt_m": [-9.0, -4.0, np.nan, np.nan],
        "NEDz": [np.nan, np.nan, np.nan, np.nan],
        "Pressure_abs_hPa": [1013.25, 1013.25, 1013.25, 1013.25 + 100.53],
    })
    out = _add_depth(df)

    assert out["Depth"].iloc[0] == pytest.approx(-3.0)
    assert out["Depth_Source"].iloc[0] == "VFR_alt"
    # a VFR_alt of 0.0 is not a real reading, so row 1 falls through to the
    # autopilot's own depth even though VFR_alt is present
    assert out["Depth"].iloc[1] == pytest.approx(-4.0)
    assert out["Depth_Source"].iloc[1] == "GLOBAL_POSITION_INT"
    # row 2 has neither, so the external pressure sensor answers
    assert out["Depth"].iloc[2] == pytest.approx(0.0, abs=1e-9)
    assert out["Depth_Source"].iloc[2] == "SCALED_PRESSURE2"
    assert out["Depth"].iloc[3] == pytest.approx(-1.0, abs=0.02)
    assert out["Depth_Source"].iloc[3] == "SCALED_PRESSURE2"


def test_depth_is_blank_when_no_source_reports():
    df = pd.DataFrame({
        "VFR_alt": [0.0, 0.0],
        "Relative_alt_m": [np.nan, -2.0],
        "NEDz": [np.nan, np.nan],
        "Pressure_abs_hPa": [np.nan, np.nan],
    })
    out = _add_depth(df)

    assert pd.isna(out["Depth"].iloc[0])
    assert pd.isna(out["Depth_Source"].iloc[0])
    assert out["Depth"].iloc[1] == pytest.approx(-2.0)
