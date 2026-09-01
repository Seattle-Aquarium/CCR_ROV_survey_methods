"""Reading the autopilot's own dataflash log.

These tests cover the parts that were wrong on the first attempt and would be
silently wrong again: the two barometer instances, angles in the wrong unit, a
mode that only appears when it changes, and -- the one that matters most --
pooling timestamps across boot sessions, which produced a clock 25 minutes off
while looking perfectly plausible.

The field conversions are tested against synthetic messages rather than a real
BIN, because pymavlink cannot write one. The clock logic is tested directly.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import binlog  # noqa: E402


class Msg:
    """Stands in for a pymavlink DFMessage."""

    def __init__(self, _type, **fields):
        self._type = _type
        self.__dict__.update(fields)

    def get_type(self):
        return self._type


def rows(msg):
    return {f: v for f, v, _s in binlog._rows(msg, 0.0)}


# --------------------------------------------------------------------------
#  field conversions
# --------------------------------------------------------------------------


def test_depth_comes_from_ctun_in_millimetres():
    """CTUN.Alt is metres, negative below the surface; the store wants the
    same quantity in millimetres, as GLOBAL_POSITION_INT reports it."""
    r = rows(Msg("CTUN", Alt=-10.5, CRt=-29))
    assert r["GLOBAL_POSITION_INT.relative_alt"] == pytest.approx(-10500.0)
    assert r["VFR_HUD.climb"] == pytest.approx(-0.29)


def test_only_the_water_barometer_is_read():
    """BARO[0] is the pressure inside the electronics tube -- around 89 kPa,
    which reads as +15 m of altitude. Mixing it with the depth sensor gives a
    trace that correlates with nothing; this cost an hour the first time."""
    assert rows(Msg("BARO", I=0, Temp=40.0, Press=88960.0)) == {}
    r = rows(Msg("BARO", I=1, Temp=13.1, Press=204730.0))
    assert r["SCALED_PRESSURE2.temperature"] == pytest.approx(1310.0)


def test_angles_are_converted_from_degrees_to_radians():
    """Dataflash logs degrees, MAVLink logs radians, and the overlay converts
    back to degrees on the way out. Skip this and every angle is 57x too big."""
    r = rows(Msg("ATT", Roll=0.0, Pitch=-3.0, Yaw=168.0))
    assert r["ATTITUDE.yaw"] == pytest.approx(math.radians(168.0))
    assert r["ATTITUDE.pitch"] == pytest.approx(math.radians(-3.0))
    assert r["VFR_HUD.heading"] == pytest.approx(168.0)


def test_heading_wraps_into_zero_to_360():
    assert rows(Msg("ATT", Roll=0, Pitch=0, Yaw=-170.0))["VFR_HUD.heading"] == \
        pytest.approx(190.0)


def test_battery_is_scaled_the_way_mavlink_reports_it():
    r = rows(Msg("BAT", Inst=0, Volt=13.55, Curr=18.2))
    assert r["BATTERY_STATUS.voltage_mv"] == pytest.approx(13550.0)
    assert r["BATTERY_STATUS.current_battery"] == pytest.approx(1820.0)
    assert rows(Msg("BAT", Inst=1, Volt=5.0, Curr=1.0)) == {}


def test_only_the_first_rangefinder_is_used():
    good = rows(Msg("RFND", Instance=0, Stat=4, Dist=0.76))
    assert good["RANGEFINDER.distance"] == pytest.approx(0.76)
    assert rows(Msg("RFND", Instance=1, Stat=4, Dist=99.0)) == {}


def test_a_lost_bottom_lock_is_not_an_altitude_of_zero():
    """The DVL loses bottom lock for about one sample in eight while flying a
    transect. ArduPilot logs that as Stat=NoData with Dist=0.00, and writing it
    through stamped "ALT 0.00 m" onto roughly one photo in eight -- a false
    measurement rather than a missing one."""
    assert rows(Msg("RFND", Instance=0, Stat=1, Dist=0.0)) == {}     # NoData
    assert rows(Msg("RFND", Instance=0, Stat=2, Dist=0.19)) == {}    # too close
    assert rows(Msg("RFND", Instance=0, Stat=0, Dist=0.0)) == {}     # no sensor
    assert rows(Msg("RFND", Instance=0, Stat=4, Dist=0.87)) != {}    # Good


@pytest.mark.parametrize("field", ["I", "Instance", "Inst", "C"])
def test_the_instance_field_is_found_whatever_it_is_called(field):
    """Dataflash names it differently per message -- BARO "I", RFND
    "Instance", BAT "Inst", the EKF "C". Looking for only one of them silently
    accepts every instance, merging two sensors into a single series."""
    assert binlog._instance(Msg("X", **{field: 1})) == 1
    assert binlog._instance(Msg("X")) == 0


def test_only_the_primary_ekf_core_is_used():
    r = rows(Msg("XKF1", C=0, VN=0.10, VE=-0.05))
    assert r["LOCAL_POSITION_NED.vx"] == pytest.approx(0.10)
    assert rows(Msg("XKF1", C=1, VN=9.0, VE=9.0)) == {}


# --------------------------------------------------------------------------
#  servo channels
# --------------------------------------------------------------------------


def test_lights_and_tilt_come_off_their_servo_channels():
    """Confirmed against a flight with both an mcap and a BIN: Lights1 tracks
    RCIN.C9 (r=+0.97) and CamTilt tracks RCOU.C10 (r=+0.94)."""
    assert rows(Msg("RCIN", C9=1740))["NVF.Lights1"] == pytest.approx(0.8)
    assert rows(Msg("RCOU", C10=1500))["NVF.CamTilt"] == pytest.approx(0.5)


@pytest.mark.parametrize("pwm,want", [
    (1100, 0.0), (1500, 0.5), (1900, 1.0),
    (2200, 1.0),        # clamped, never above full
    (900, 0.0),         # clamped, never negative
])
def test_pwm_maps_onto_a_fraction(pwm, want):
    assert binlog._pwm_fraction(pwm) == pytest.approx(want)


def test_an_unwritten_channel_is_absent_not_zero():
    """A channel the board never drives logs as 0. Reporting that as "lights
    off" would be a claim the log does not make."""
    assert binlog._pwm_fraction(0) is None
    assert rows(Msg("RCIN", C9=0)) == {}


# --------------------------------------------------------------------------
#  placing the log on the clock
# --------------------------------------------------------------------------


def test_a_gps_fix_without_a_week_number_is_not_a_clock(tmp_path, monkeypatch):
    """ArduSub fed position by an external UGPS logs Status=1 and GWk=0. That
    is a position, not a time, and must not be mistaken for one."""
    msgs = [Msg("GPS", Status=1, GWk=0, GMS=0, TimeUS=1_000_000),
            Msg("GPS", Status=1, GWk=0, GMS=0, TimeUS=2_000_000)]
    _fake_connection(monkeypatch, msgs)
    assert binlog.align_from_gps(tmp_path / "x.BIN") is None


def test_a_real_gps_fix_gives_the_offset(tmp_path, monkeypatch):
    week, ms = 2400, 123_456_000
    wall = binlog._GPS_EPOCH + week * 604800 + ms / 1000 - binlog._GPS_LEAP
    _fake_connection(monkeypatch, [
        Msg("GPS", Status=3, GWk=week, GMS=ms, TimeUS=60_000_000)])
    al = binlog.align_from_gps(tmp_path / "x.BIN")
    assert al is not None and al.method == "gps"
    assert al.offset == pytest.approx(wall - 60.0)
    assert al.trustworthy, "a GPS fix needs no further corroboration"


def _fake_connection(monkeypatch, msgs):
    class Conn:
        def __init__(self):
            self._it = iter(list(msgs))
            self.offset = 0

        def recv_match(self, type=None):
            for m in self._it:
                if type is None or m.get_type() in type:
                    return m
            return None

    import pymavlink.mavutil as mu
    monkeypatch.setattr(mu, "mavlink_connection", lambda *_a, **_k: Conn())


# --------------------------------------------------------------------------
#  trust
# --------------------------------------------------------------------------


def test_an_mcap_alignment_is_only_trusted_once_depth_agrees():
    """The failure this guards against: pooling timestamps across two boot
    sessions produced an offset 25 minutes wrong that looked entirely
    reasonable. Only the dive-profile match caught it, so an alignment without
    that check must not be called good."""
    a = binlog.BinAlignment(offset=0.0, method="mcap", samples=9999)
    assert not a.trustworthy, "no corroboration is not the same as agreement"
    a.depth_agreement = 0.79
    assert not a.trustworthy, "0.79 was the wrong-session score"
    a.depth_agreement = 0.9999
    assert a.trustworthy


def test_agreement_needs_a_real_overlap():
    import numpy as np
    bt = np.arange(0.0, 100.0, 0.1)
    bd = np.sin(bt / 5.0) * 5 + 10
    same = [(t, float(np.interp(t, bt, bd))) for t in np.arange(10, 90, 0.5)]
    assert binlog._agreement(bt, bd, same) == pytest.approx(1.0, abs=1e-6)
    # a window that does not overlap the log at all cannot be judged
    elsewhere = [(t, 5.0 + 0.001 * t) for t in np.arange(500, 600, 0.5)]
    assert binlog._agreement(bt, bd, elsewhere) is None
    assert binlog._agreement(bt, bd, [(1.0, 2.0)]) is None


def test_a_flat_trace_is_not_scored():
    """A vehicle sitting still gives a flat profile that would correlate with
    anything, so it proves nothing about which session a log came from."""
    import numpy as np
    bt = np.arange(0.0, 100.0, 0.1)
    flat = np.full_like(bt, 3.0)
    assert binlog._agreement(bt, flat,
                             [(t, 3.0) for t in np.arange(10, 90, 0.5)]) is None


# --------------------------------------------------------------------------
#  discovery and the override
# --------------------------------------------------------------------------


def test_list_bins_finds_both_cases_without_duplicating(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "00000167.BIN").write_bytes(b"x")
    (tmp_path / "logs" / "other.bin").write_bytes(b"x")
    (tmp_path / "logs" / "notes.txt").write_text("hi")
    found = binlog.list_bins(tmp_path)
    assert len(found) == 2 and {p.name for p in found} == \
        {"00000167.BIN", "other.bin"}
    assert binlog.list_bins(tmp_path / "nope") == []


def test_the_override_is_explicit_and_reversible(tmp_path):
    """UTC must never change telemetry source on its own -- a flight that read
    the mcap yesterday has to read it today unless someone said otherwise."""
    assert binlog.override_active(tmp_path) is None
    (tmp_path / binlog.OVERRIDE_CSV).write_text("t,field,value,sval\n")
    (tmp_path / binlog.OVERRIDE_META).write_text(
        json.dumps({"source": "00000167.BIN", "rows": 5, "depth_agreement": 1.0}))
    meta = binlog.override_active(tmp_path)
    assert meta and meta["rows"] == 5 and meta["csv"].endswith(binlog.OVERRIDE_CSV)
    binlog.clear_override(tmp_path)
    assert binlog.override_active(tmp_path) is None


def test_an_override_without_its_metadata_still_works(tmp_path):
    (tmp_path / binlog.OVERRIDE_CSV).write_text("t,field,value,sval\n")
    assert binlog.override_active(tmp_path) is not None


def test_covers_reports_only_wholly_contained_transects():
    # boot 0..3600 with a +1000 offset means wall clock 1000..4600
    info = binlog.BinInfo(Path("x.BIN"), boot_first=0.0, boot_last=3600.0)
    al = binlog.BinAlignment(offset=1000.0, method="gps")
    wins = [("T1", 1100.0, 1200.0),      # inside
            ("T2", 4000.0, 4200.0),      # also inside, near the end
            ("T3", 900.0, 1500.0),       # starts before the log does
            ("T4", 4500.0, 5000.0)]      # runs past the end
    assert binlog.covers(info, al, wins) == ["T1", "T2"]


def test_writing_an_override_refuses_more_than_one_log(tmp_path):
    """Each BIN has its own boot clock, so two cannot share one offset."""
    al = binlog.BinAlignment(offset=0.0, method="gps")
    with pytest.raises(ValueError):
        binlog.write_override(tmp_path, [Path("a.BIN"), Path("b.BIN")], al)
