"""
The typed reading, the geodesy and the power arithmetic.

These are the parts that decide whether a number on the screen means what it
says, so they are tested against the cases that have actually gone wrong on
this fleet rather than against a tidy happy path.
"""

from __future__ import annotations

import math
import time

import pytest

from rov_flight_ops.nav import geo, power
from rov_flight_ops.nav import model as M
from rov_flight_ops.nav.model import Quality

# --------------------------------------------------------------------------
#  unknown is not zero
# --------------------------------------------------------------------------


def test_a_fresh_snapshot_holds_no_numbers_at_all():
    """Every instrument starts unknown. None of them starts at zero."""
    s = M.NavSnapshot()
    for name in ("altitude", "depth", "speed", "voltage", "current", "watts",
                 "energy_wh", "peak_w", "mode", "heading"):
        r = getattr(s, name)
        assert r.quality is Quality.NEVER_RECEIVED, name
        assert r.number() is None, name
        assert r.text() == M.NO_VALUE, name
    assert s.rov_fix is None and s.vessel_fix is None


def test_an_invalid_reading_keeps_its_value_out_of_arithmetic():
    """A DVL with no lock reports honestly; nothing may integrate it."""
    r = M.invalid("no bottom lock", value=0.0, unit="m")
    assert r.number() is None          # not for arithmetic
    assert r.held() is None            # not even for a marked display
    assert r.text() == M.NO_VALUE
    assert "no bottom lock" in r.note


def test_a_stale_reading_shows_its_last_value_but_is_not_usable():
    """The difference between 'stopped arriving' and 'reporting nothing'."""
    r = M.good(0.83, unit="m")
    s = r.staled("no new sample")
    assert s.quality is Quality.STALE
    assert s.number() is None          # never steered by
    assert s.held() == pytest.approx(0.83)   # but still shown, marked
    assert "0.83" in s.text(2)


def test_age_comes_from_the_monotonic_clock_not_the_wall_clock():
    """A clock step must not make a stale reading look fresh."""
    now = time.monotonic()
    r = M.good(1.0, recv_mono=now - 12.0)
    assert r.age(now) == pytest.approx(12.0, abs=0.01)
    aged = M.age_out(r, 5.0, now_mono=now)
    assert aged.quality is Quality.STALE
    kept = M.age_out(r, 30.0, now_mono=now)
    assert kept.quality is Quality.OK


def test_unsupported_is_not_a_fault():
    r = M.unsupported("this endpoint does not publish fix quality")
    assert r.quality is Quality.UNSUPPORTED
    assert not r.quality.usable and not r.quality.has_value


# --------------------------------------------------------------------------
#  coordinates
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lat,lon,ok", [
    (47.62691, -122.39018, True),
    (0.0, 0.0, False),                 # null island: every uninitialised value
    (0.005, -0.004, False),            # and its neighbourhood
    (91.0, 0.0, False),
    (0.0, 181.0, False),
    (None, None, False),
    (-89.9, 179.9, True),
    # Strings are refused rather than coerced. A string arriving here means
    # something upstream did not parse its input, and quietly accepting it
    # would hide that. Operator-typed coordinates are parsed deliberately, by
    # `origin.validate`, which says what is wrong with them.
    ("47.6", "-122.4", False),
    (float("nan"), 0.0, False),
])
def test_null_island_and_out_of_range_coordinates_are_refused(lat, lon, ok):
    """The WL UGPS external extension returns 0/0 before its first sentence.

    Plotted, that is a vessel in the Gulf of Guinea. Range-checked here rather
    than trusted from upstream, because a transposed or uninitialised pair
    looks exactly like a real one downstream.
    """
    assert M.valid_latlon(lat, lon) is ok


# --------------------------------------------------------------------------
#  geodesy
# --------------------------------------------------------------------------


def test_distance_and_bearing_match_a_known_pair():
    """Checked against geographiclib to under a millimeter when written."""
    d, brg, _ = geo.inverse(47.62691, -122.39018, 47.62713957, -122.39396450)
    assert d == pytest.approx(285.5921, abs=0.001)
    assert brg == pytest.approx(275.129, abs=0.01)


def test_bearings_cross_north_and_the_antimeridian_correctly():
    d, brg, _ = geo.inverse(0.0, 179.999, 0.0, -179.999)
    assert d == pytest.approx(222.639, abs=0.01)     # the short way, not 3/4
    assert brg == pytest.approx(90.0, abs=0.01)


def test_the_shortest_turn_is_used_for_circular_differences():
    assert geo.angle_diff(1.0, 359.0) == pytest.approx(2.0)
    assert geo.angle_diff(359.0, 1.0) == pytest.approx(-2.0)
    assert abs(geo.angle_diff(180.0, 0.0)) == pytest.approx(180.0)


def test_coincident_points_have_no_bearing():
    """A needle that spins is worse than one that says 'at target'."""
    assert geo.initial_bearing_deg(47.6, -122.4, 47.6, -122.4) is None
    near = geo.destination(47.6, -122.4, 33.0, 0.2)
    assert geo.initial_bearing_deg(47.6, -122.4, *near) is None


def test_a_bearing_never_formats_as_360():
    assert geo.format_bearing(359.9999999) == "0°T"
    assert geo.format_bearing(275.129) == "275°T"


def test_local_ned_projects_with_the_right_axes():
    """North is +x and east is +y. Getting this wrong rotates a whole dive."""
    lat, lon = 47.62691, -122.39018
    north = geo.offset_ned(lat, lon, 100.0, 0.0)
    assert north[0] > lat and north[1] == pytest.approx(lon, abs=1e-9)
    east = geo.offset_ned(lat, lon, 0.0, 100.0)
    assert east[1] > lon and east[0] == pytest.approx(lat, abs=1e-6)
    _d, brg, _ = geo.inverse(lat, lon, *east)
    assert brg == pytest.approx(90.0, abs=0.01)
    assert geo.offset_ned(lat, lon, 0.0, 0.0) == (lat, lon)


@pytest.mark.parametrize("n,e", [(100, 0), (0, 100), (-63.1, -9.17), (7, -250)])
def test_a_ned_offset_round_trips_to_its_own_distance(n, e):
    lat, lon = 47.62691, -122.39018
    d = geo.distance_m(lat, lon, *geo.offset_ned(lat, lon, n, e))
    assert d == pytest.approx(math.hypot(n, e), rel=1e-6, abs=1e-3)


# --------------------------------------------------------------------------
#  power and energy
# --------------------------------------------------------------------------


def test_watts_refuse_the_no_current_sensor_sentinel():
    """ArduPilot reports -1 for 'not measured', not a small negative current."""
    assert power.EnergyMeter.watts(16.0, 2.0) == pytest.approx(32.0)
    assert power.EnergyMeter.watts(16.0, -1.0) is None
    assert power.EnergyMeter.watts(None, 2.0) is None
    assert power.EnergyMeter.watts(0.0, 2.0) is None       # sense line adrift
    assert power.EnergyMeter.watts(16.0, 5000.0) is None


def test_energy_integrates_a_known_signal_over_real_intervals():
    """100 W for one hour is 100 Wh, whatever the sample spacing."""
    m = power.EnergyMeter()
    m.begin("known")
    t = 0.0
    n = 0
    while t < 3600.0:
        m.update(10.0, 10.0, mono=t, fresh=True, counter=n)
        t += 0.25
        n += 1
    assert m.wh == pytest.approx(100.0, rel=0.001)
    assert m.coverage() == pytest.approx(1.0, abs=0.001)


def test_irregular_intervals_still_integrate_correctly():
    m = power.EnergyMeter()
    m.begin("irregular")
    # Deliberately uneven spacing: a busy Pi does not answer on a metronome.
    times = [0.0, 0.3, 0.55, 1.4, 2.0, 2.9, 3.0]
    for i, t in enumerate(times):
        m.update(10.0, 10.0, mono=t, fresh=True, counter=i)
    assert m.wh == pytest.approx(100.0 * 3.0 / 3600.0, rel=1e-6)


def test_a_gap_is_not_integrated_and_is_reported():
    """A two-minute dropout must not invent the watt-hours it did not measure."""
    m = power.EnergyMeter()
    m.begin("gap")
    m.update(10.0, 10.0, mono=0.0, fresh=True, counter=1)
    m.update(10.0, 10.0, mono=1.0, fresh=True, counter=2)
    m.update(10.0, 10.0, mono=121.0, fresh=True, counter=3)   # 2-minute hole
    m.update(10.0, 10.0, mono=122.0, fresh=True, counter=4)
    assert m.wh == pytest.approx(100.0 * 2.0 / 3600.0, rel=1e-6)
    assert m.state.gap_s == pytest.approx(120.0)
    assert m.coverage() < 0.05
    assert "coverage" in m.note()


def test_a_stale_or_duplicate_sample_is_never_integrated():
    m = power.EnergyMeter()
    m.begin("dupes")
    m.update(10.0, 10.0, mono=0.0, fresh=True, counter=1)
    m.update(10.0, 10.0, mono=1.0, fresh=True, counter=2)
    before = m.wh
    m.update(10.0, 10.0, mono=2.0, fresh=False, counter=2)    # no new message
    m.update(10.0, 10.0, mono=3.0, fresh=True, counter=2)     # same counter
    assert m.wh == before


def test_time_going_backwards_is_refused():
    m = power.EnergyMeter()
    m.begin("backwards")
    m.update(10.0, 10.0, mono=10.0, fresh=True, counter=1)
    m.update(10.0, 10.0, mono=11.0, fresh=True, counter=2)
    before = m.wh
    m.update(10.0, 10.0, mono=5.0, fresh=True, counter=3)
    assert m.wh == before


def test_the_peak_survives_and_is_taken_from_raw_samples():
    m = power.EnergyMeter()
    m.begin("peak")
    for i, w in enumerate((100.0, 250.0, 1180.0, 260.0, 90.0)):
        m.update(16.0, w / 16.0, mono=i * 0.25, fresh=True, counter=i)
    assert m.peak_w == pytest.approx(1180.0, rel=1e-6)
    # A stale sample cannot set a peak: it is not an observation.
    m.update(16.0, 4000.0 / 16.0, mono=2.0, fresh=False, counter=99)
    assert m.peak_w == pytest.approx(1180.0, rel=1e-6)


def test_a_reset_is_recorded_rather_than_silent():
    m = power.EnergyMeter()
    m.begin("reset")
    for i in range(20):
        m.update(16.0, 5.0, mono=i, fresh=True, counter=i)
    assert m.wh > 0
    m.reset("new flight")
    assert m.wh == 0 and m.peak_w == 0
    assert m.state.resets and m.state.resets[0]["why"] == "new flight"
    assert m.state.resets[0]["wh_before"] > 0


def test_energy_state_resumes_only_its_own_session(tmp_path):
    """Yesterday's total is not this flight's, and adopting it silently
    would be worse than starting from zero."""
    m = power.EnergyMeter()
    m.begin("flight-A")
    for i in range(40):
        m.update(16.0, 5.0, mono=i, fresh=True, counter=i)
    p = tmp_path / "energy.json"
    assert m.save(p)

    same, resumed = power.EnergyMeter.resume(p, "flight-A")
    assert resumed and same.wh == pytest.approx(m.wh)
    other, resumed = power.EnergyMeter.resume(p, "flight-B")
    assert not resumed and other.wh == 0.0


def test_the_gauge_never_rescales_and_over_range_keeps_its_value():
    assert power.gauge_fraction(0.0) == 0.0
    assert power.gauge_fraction(500.0) == pytest.approx(0.5)
    assert power.gauge_fraction(1000.0) == 1.0
    # Pinned for position, true for the number.
    assert power.gauge_fraction(1180.0) == 1.0
    assert power.over_range(1180.0) and not power.over_range(999.0)
    assert power.high_load(900.0) and not power.high_load(899.0)
