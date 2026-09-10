"""
Reading .tlog recordings.

The point of these is that a tlog and an mcap of the same dive go down the same
path from the moment the frames are parsed. So the tests build one dive twice --
once as each format -- and assert the extracted table matches, rather than
checking the tlog reader against its own idea of what it should produce.
"""

from __future__ import annotations

import struct

import pytest

from conftest import BASE_EPOCH, straight_north_dive
from ccr_m2c.mcap_read import WANTED_TYPES, probe_mcaps, read_mcaps
from ccr_m2c.tlog_read import is_tlog, probe_tlog, scan_types

pymavlink = pytest.importorskip("pymavlink")
from pymavlink.dialects.v20 import ardupilotmega as mav  # noqa: E402


def write_tlog(path, seconds=20, start=BASE_EPOCH):
    """The same dive straight_north_dive builds, as a .tlog.

    A tlog is an 8-byte big-endian microsecond timestamp followed by the frame,
    repeated -- so it can be written without a library.
    """
    link = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    with open(path, "wb") as f:
        def put(t, msg):
            f.write(struct.pack(">Q", int(t * 1e6)))
            f.write(msg.pack(link))

        for i in range(seconds):
            t = start + i
            # time_boot_ms is uint32: milliseconds since the autopilot booted,
            # not since the epoch.
            boot_ms = int((t - start) * 1000)
            for k in range(10):
                put(t + k / 10, mav.MAVLink_attitude_message(
                    boot_ms, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            for k in range(5):
                put(t + k / 5, mav.MAVLink_vision_position_delta_message(
                    0, 200000, [0.0, 0.0, 0.0], [0.1, 0.0, 0.0], 99.0))
            put(t, mav.MAVLink_gps_raw_int_message(
                0, 3, 476176249, -1223610207, 0, 100, 100, 0, 0, 12))
            put(t, mav.MAVLink_global_position_int_message(
                boot_ms, 476176249, -1223610207, 0, -5000, 0, 0, 0, 0))
            put(t, mav.MAVLink_rangefinder_message(2.0, 0.0))
            put(t, mav.MAVLink_vfr_hud_message(0.0, 0.0, 0, 50, 0.0, 0.0))
            put(t, mav.MAVLink_scaled_pressure2_message(
                boot_ms, 1013.25 + 5 * 100.53, 0.0, 1200))
            put(t, mav.MAVLink_heartbeat_message(18, 8, 128, 19, 3, 3))
            put(t, mav.MAVLink_battery_status_message(
                0, 0, 0, 1200, [14000] + [65535] * 9, 200, 10 * i, 5 * i, -1))
            put(t, mav.MAVLink_named_value_float_message(
                boot_ms, b"Lights1", 0.5))
    return path


@pytest.fixture
def tlog(tmp_path):
    return write_tlog(tmp_path / "dive.tlog")


def test_it_is_recognised_by_its_suffix(tmp_path):
    assert is_tlog(tmp_path / "a.tlog")
    assert is_tlog(tmp_path / "A.TLOG")
    assert not is_tlog(tmp_path / "a.mcap")


def test_the_span_is_read_without_parsing_the_frames(tlog):
    start, end, err = probe_tlog(tlog)
    assert err is None
    assert start == pytest.approx(BASE_EPOCH, abs=1)
    assert end == pytest.approx(BASE_EPOCH + 19, abs=2)


def test_a_file_that_is_not_a_tlog_is_reported_not_guessed(tmp_path):
    junk = tmp_path / "notreally.tlog"
    junk.write_bytes(b"\x89MCAP0\r\n" + b"\x00" * 400)
    _s, _e, err = probe_tlog(junk)
    assert err and "timestamp" in err


def test_the_probe_survives_an_empty_file(tmp_path):
    empty = tmp_path / "empty.tlog"
    empty.write_bytes(b"")
    _s, _e, err = probe_tlog(empty)
    assert err


def test_the_type_scan_finds_what_the_file_carries(tlog):
    found = scan_types(tlog, WANTED_TYPES)
    for expected in ("ATTITUDE", "RANGEFINDER", "GLOBAL_POSITION_INT",
                     "VISION_POSITION_DELTA", "HEARTBEAT", "BATTERY_STATUS"):
        assert expected in found, expected
    assert "LOCAL_POSITION_NED" not in found        # this dive has none


def test_a_tlog_reads_into_the_same_table_as_an_mcap(tlog, builder):
    """The whole reason the reader yields the mcap's own three-tuple."""
    mcap = straight_north_dive(builder(), seconds=20).close()
    a = read_mcaps([mcap]).df
    b = read_mcaps([tlog]).df

    assert len(a) == len(b)
    assert list(a["Time"]) == list(b["Time"])
    for col, tol in (("Altitude", 1e-6), ("Depth", 0.02), ("Heading", 1e-6),
                     ("DVLx", 1e-6), ("DVLy", 1e-6), ("Battery_V", 1e-6),
                     ("Width", 1e-6), ("Area_m2", 1e-6)):
        assert a[col].notna().sum() == b[col].notna().sum(), f"{col} coverage"
        assert a[col].dropna().iloc[-1] == pytest.approx(
            b[col].dropna().iloc[-1], abs=tol), col


def test_enums_read_the_same_from_both(tlog, builder):
    """pymavlink gives an int where the mcap's JSON gives the name, so a fix
    type would otherwise read '3' from one file and GPS_FIX_TYPE_3D_FIX from
    the other."""
    df = read_mcaps([tlog]).df
    assert df["GPS_fix_type"].dropna().iloc[0] == "GPS_FIX_TYPE_3D_FIX"


def test_named_values_survive_their_padding(tlog):
    """A char[10] field arrives with its nulls still attached."""
    df = read_mcaps([tlog]).df
    assert df["Lights_pct"].dropna().iloc[0] == pytest.approx(50.0)


def test_the_dvl_source_is_chosen_the_same_way(tmp_path):
    """A tlog has no channel list, so what it carries has to be scanned for --
    and getting that wrong lets two sources feed one column at once."""
    path = write_tlog(tmp_path / "vpd.tlog")
    res = read_mcaps([path])
    assert res.dvl_source == "VISION_POSITION_DELTA"


def test_a_tlog_and_an_mcap_can_be_read_together(tlog, builder):
    """A dive split across a format change still merges on one timeline."""
    mcap = straight_north_dive(builder(), seconds=20,
                               start=BASE_EPOCH + 20).close()
    df = read_mcaps([tlog, mcap]).df
    assert len(df) == 40
    assert df["Time"].iloc[0] == "10:00:00"
    assert df["Time"].iloc[-1] == "10:00:39"


def test_probe_mcaps_handles_tlogs(tlog):
    info = probe_mcaps([tlog])[0]
    assert info.usable
    assert "2026-08-26" in info.local_span()
