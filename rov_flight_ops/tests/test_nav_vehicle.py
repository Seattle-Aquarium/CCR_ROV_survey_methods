"""
Talking to the vehicle: freshness, profiles, the origin, and the sensor matrix.

The cases here are the ones that cost this fleet a day of coordinates on
18 September 2026, plus the ones the September 2026 stack makes easy to get
wrong. Each names the source it was derived from, so the next firmware can be
checked against the same list.
"""

from __future__ import annotations

import json
import time

import pytest

from rov_flight_ops.nav import extensions as E
from rov_flight_ops.nav import mav2rest
from rov_flight_ops.nav import model as M
from rov_flight_ops.nav import origin as O
from rov_flight_ops.nav import profiles as PR
from rov_flight_ops.nav.model import Quality

# --------------------------------------------------------------------------
#  The parameter set this fleet actually flew on 18 September 2026
# --------------------------------------------------------------------------

#: A cut-down copy of the real dump in
#: flights/Port_of_Seattle/2026/2026_09_18_EBM_E/logs/flight_*.json -- the
#: navigation-relevant parameters, verbatim. Small enough for the repository;
#: the whole 1,020-parameter file is not.
EBM_E_2026_09_18 = {
    "AHRS_EKF_TYPE": 3.0, "AHRS_OPTIONS": 0.0, "AHRS_ORIENTATION": 16.0,
    "EK2_ENABLE": 0.0, "EK3_ENABLE": 1.0,
    "EK3_SRC1_POSXY": 6.0, "EK3_SRC1_POSZ": 1.0, "EK3_SRC1_VELXY": 6.0,
    "EK3_SRC1_VELZ": 0.0, "EK3_SRC1_YAW": 1.0,
    "EK3_SRC2_POSXY": 0.0, "EK3_SRC2_POSZ": 1.0, "EK3_SRC2_VELXY": 0.0,
    "EK3_SRC2_VELZ": 0.0, "EK3_SRC2_YAW": 0.0,
    "EK3_SRC3_POSXY": 0.0, "EK3_SRC3_POSZ": 1.0, "EK3_SRC3_VELXY": 0.0,
    "EK3_SRC3_VELZ": 0.0, "EK3_SRC3_YAW": 0.0,
    "EK3_SRC_OPTIONS": 1.0,
    "GPS_TYPE": 14.0,
    "ORIGIN_ALT": 0.0, "ORIGIN_LAT": 47.62691, "ORIGIN_LON": -122.39018,
    "RNGFND1_MAX_CM": 5000.0, "RNGFND1_MIN_CM": 20.0, "RNGFND1_ORIENT": 25.0,
    "RNGFND1_TYPE": 10.0,
    "SCR_ENABLE": 1.0, "SURFTRAK_DEPTH": -100.0,
    "VISO_TYPE": 1.0,
    # EK3_GPS_TYPE is deliberately absent: it does not exist on 4.5.7.
}


# --------------------------------------------------------------------------
#  Freshness: an HTTP 200 proves nothing
# --------------------------------------------------------------------------


class FakeService:
    """Answers like mavlink2rest, with a counter we control."""

    def __init__(self):
        self.counter = 10
        self.body = {"distance": 0.85, "voltage": 0.0}
        self.calls = 0

    def reply(self):
        self.calls += 1
        return json.dumps({
            "message": {"type": "RANGEFINDER", **self.body},
            "status": {"time": {"counter": self.counter, "frequency": 4.2,
                                "first_update": "2026-09-18T18:29:16Z",
                                "last_update": "2026-09-18T18:29:19Z"}}})


@pytest.fixture
def fake_mav(monkeypatch):
    svc = FakeService()

    def fake_get(url, *, timeout=4.0):
        return mav2rest.Answer(True, 200, svc.reply())

    monkeypatch.setattr(mav2rest, "_get", fake_get)
    mav = mav2rest.Mavlink2Rest("192.0.2.1")
    return mav, svc


def test_a_repeated_cached_message_is_not_a_new_sample(fake_mav):
    """The trap this whole module exists to close.

    Ask for RANGEFINDER after the DVL has been unplugged and mavlink2rest
    answers 200 with a perfect 0.85 m forever. Only the counter moving says
    anything arrived.
    """
    mav, svc = fake_mav
    first = mav.read("RANGEFINDER")
    assert first.fresh and first.num("distance") == 0.85

    again = mav.read("RANGEFINDER")           # same counter
    assert svc.calls == 2                     # the request did succeed
    assert not again.fresh                    # and it was not a new message
    assert again.num("distance") == 0.85      # the value is still available

    svc.counter += 1
    assert mav.read("RANGEFINDER").fresh


def test_freshness_age_counts_from_the_last_new_message(fake_mav):
    mav, svc = fake_mav
    mav.read("RANGEFINDER")
    health = mav.health["RANGEFINDER"]
    moved_at = health.last_change_mono
    for _ in range(5):
        mav.read("RANGEFINDER")               # successful, unchanged
    assert health.last_change_mono == moved_at
    assert health.quality(max_age=0.0) is Quality.STALE


def test_a_counter_going_backwards_is_a_restart(fake_mav):
    mav, svc = fake_mav
    mav.read("RANGEFINDER")
    svc.counter = 1                           # the service restarted
    sample = mav.read("RANGEFINDER")
    assert sample.reset and sample.fresh
    assert mav.health["RANGEFINDER"].reset


def test_a_404_means_the_vehicle_never_sends_it(monkeypatch):
    monkeypatch.setattr(mav2rest, "_get",
                        lambda url, *, timeout=4.0: mav2rest.Answer(False, 404))
    mav = mav2rest.Mavlink2Rest("192.0.2.1")
    s = mav.read("ODOMETRY")
    assert not s.fresh
    assert "never sent" in mav.health["ODOMETRY"].error


# --------------------------------------------------------------------------
#  Profiles, against the real parameter set
# --------------------------------------------------------------------------


def test_the_fleets_own_parameters_read_as_dvl_only():
    key, why = PR.detect(EBM_E_2026_09_18)
    assert key == "dvl" and "ExternalNav" in why


def test_ek3_gps_type_is_absent_on_4_5_7_and_is_not_a_fault():
    """EKF3 dropped it after 4.1 (`// 1 was GPS_TYPE` in AP_NavEKF3.cpp), and
    it is in none of the 1,020 parameters this fleet flew. The DVL extension
    still writes it in both presets, so that write lands nowhere."""
    res = PR.check(PR.DVL_ONLY, EBM_E_2026_09_18, dvl_reachable=True,
                   dvl_message_type="POSITION_ESTIMATE", origin_set=True)
    finding = next(f for f in res.findings if f.param == "EK3_GPS_TYPE")
    assert finding.absent and finding.ok
    assert finding.severity is PR.Severity.INFO
    assert finding.param not in {f.param for f in res.changes()}


def test_position_delta_is_relative_aiding_not_an_absence_of_coordinates():
    """The wording this had first was too strong, and the review was right.

    ArduPilot routes VISION_POSITION_DELTA to `writeBodyFrameOdom`, so EKF3
    reaches relative rather than absolute aiding. But `getLLH` returns the
    origin plus the relative offset whenever `horiz_pos_rel` is set, so with a
    confirmed origin that is a perfectly usable dead-reckoned position. It is
    an advisory, not a blocker: making it a blocker would refuse a
    configuration this fleet has flown.
    """
    res = PR.check(PR.DVL_ONLY, EBM_E_2026_09_18, dvl_reachable=True,
                   dvl_message_type="POSITION_DELTA", origin_set=True)
    note = " ".join(x for _s, x in res.notes)
    assert "POSITION_DELTA" in note and "relative aiding" in note
    assert "dead-reckoned and usable" in note
    assert not res.blockers
    assert not res.note_blockers
    assert res.matches


def test_the_missing_origin_is_the_blocker_whatever_the_message_type():
    """What actually left the 18 September dive with no coordinates."""
    for mt in ("POSITION_DELTA", "POSITION_ESTIMATE"):
        res = PR.check(PR.DVL_ONLY, EBM_E_2026_09_18, dvl_reachable=True,
                       dvl_message_type=mt, origin_set=False)
        assert not res.matches, mt
        assert any("no origin" in x for x in res.note_blockers), mt


def test_speed_estimate_blocks_only_where_position_is_wanted_from_the_dvl():
    """In the acoustic profile the DVL supplies velocity and nothing else, so
    a velocity-only message is a legitimate choice there."""
    dvl_only = PR.check(PR.DVL_ONLY, EBM_E_2026_09_18, dvl_reachable=True,
                        dvl_message_type="SPEED_ESTIMATE", origin_set=True)
    assert dvl_only.note_blockers
    acoustic_params = dict(EBM_E_2026_09_18, EK3_SRC1_POSXY=3.0)
    acoustic = PR.check(PR.ACOUSTIC_DVL, acoustic_params, dvl_reachable=True,
                        dvl_message_type="SPEED_ESTIMATE", origin_set=True,
                        vessel_feeding=True)
    assert not acoustic.note_blockers


def test_a_missing_origin_blocks_even_when_everything_else_is_right():
    res = PR.check(PR.DVL_ONLY, EBM_E_2026_09_18, dvl_reachable=True,
                   dvl_message_type="POSITION_ESTIMATE", origin_set=False)
    assert not res.matches
    assert any("no origin" in t for t in res.note_blockers)


def test_the_fleet_is_one_parameter_away_from_the_acoustic_profile():
    res = PR.check(PR.ACOUSTIC_DVL, EBM_E_2026_09_18, dvl_reachable=True,
                   dvl_message_type="POSITION_ESTIMATE", origin_set=True,
                   vessel_feeding=True)
    changes = {f.param: f.requirement.want for f in res.changes()}
    assert changes == {"EK3_SRC1_POSXY": 3.0}


def test_an_unread_parameter_set_is_unknown_rather_than_healthy():
    res = PR.check(PR.DVL_ONLY, None)
    assert res.unknown and not res.matches
    assert "not been read" in res.summary()


def test_source_set_switching_is_refused_while_set_2_is_empty():
    """4.5.7 handles MAV_CMD_SET_EKF_SOURCE_SET for sets 1-3, but this
    fleet's SRC2/SRC3 are all zero: selecting set 2 would choose no position,
    no velocity and no yaw source at all."""
    opt = PR.source_set_available(PR.ACOUSTIC_DVL, EBM_E_2026_09_18)
    assert not opt.available
    opt = PR.source_set_available(PR.DVL_ONLY, EBM_E_2026_09_18)
    assert opt.available and opt.set_number == 1
    assert "acknowledged" in opt.reason and "active use" not in opt.reason.lower()


def test_a_configured_second_source_set_becomes_available():
    params = dict(EBM_E_2026_09_18)
    params.update({"EK3_SRC2_POSXY": 6.0, "EK3_SRC2_VELXY": 6.0,
                   "EK3_SRC2_POSZ": 1.0, "EK3_SRC2_YAW": 1.0})
    dvl2 = PR.Profile(**{**PR.DVL_ONLY.__dict__, "source_set": 2})
    assert PR.source_set_available(dvl2, params).available


def test_source_numbers_are_shown_with_their_meaning():
    assert PR.source_name("EK3_SRC1_POSXY", 6.0) == "6 (ExternalNav)"
    assert PR.source_name("EK3_SRC1_POSXY", 3.0) == "3 (GPS)"
    assert PR.source_name("EK3_SRC1_POSZ", 1.0) == "1 (Baro)"
    assert PR.source_name("EK3_SRC1_YAW", 1.0) == "1 (Compass)"


# --------------------------------------------------------------------------
#  Origin
# --------------------------------------------------------------------------


def test_leftover_origin_parameters_with_no_origin_are_called_out():
    """Exactly the 18 September state: ORIGIN_LAT/LON hold a real coordinate,
    nothing is reading them, and GLOBAL_POSITION_INT reports 0, 0."""
    st = O.detect(EBM_E_2026_09_18, firmware="4.5.7", active=False)
    assert st.authority == "ORIGIN_"
    assert st.configured_families and st.scripting_enabled is True
    assert any("not evidence that anything is reading it" in w
               for w in st.warnings)


def test_two_origin_families_are_a_warning():
    params = dict(EBM_E_2026_09_18)
    params.update({"AHRS_ORIG_LAT": 0.0, "AHRS_ORIG_LON": 0.0,
                   "AHRS_ORIG_ALT": 0.0})
    st = O.detect(params, active=True)
    assert any("More than one origin parameter family" in w
               for w in st.warnings)


def test_no_family_at_all_falls_back_to_the_laptop():
    st = O.detect({"SCR_ENABLE": 0.0}, active=False)
    assert st.authority == "gcs"
    assert "only be set from this laptop" in st.authority_reason


@pytest.mark.parametrize("lat,lon", [(0, 0), (91, 0), (0, 181), ("x", "y")])
def test_an_unusable_origin_is_refused_with_a_reason(lat, lon):
    with pytest.raises(O.OriginError):
        O.validate(lat, lon, 0)


def test_saving_the_origin_is_refused_until_it_has_been_set():
    """The safety property, and the reason for the order.

    While the EKF has no origin, an ahrs-set-origin applet may be in its
    five-second retry loop, and its guard passes as soon as *any one* of the
    three parameters is non-zero -- so writing them one at a time can hand it
    a latitude with no longitude. Once the origin is set the applet returns at
    `if ahrs:get_origin()` before reading them at all.
    """
    wrote = []
    res = O.save_for_next_boot(lambda n, v: (wrote.append(n), (True, ""))[1],
                               "ORIGIN_", 47.62691, -122.39018, 0.0,
                               origin_is_set=False)
    assert res.outcome == "failed"
    assert not wrote                       # nothing was written at all
    assert "Set the origin on the vehicle first" in res.message


def test_saving_writes_latitude_last_once_the_origin_is_set():
    wrote = []

    def setter(name, value):
        wrote.append(name)
        return True, f"read back {value:g}"

    res = O.save_for_next_boot(setter, "ORIGIN_", 47.62691, -122.39018, 0.0,
                               origin_is_set=True)
    assert res.outcome == "succeeded"
    assert wrote == ["ORIGIN_ALT", "ORIGIN_LON", "ORIGIN_LAT"]


def test_a_partial_write_is_reported_as_partial_not_as_failure():
    def setter(name, value):
        return (False, "no PARAM_VALUE came back") if name == "ORIGIN_LAT" \
            else (True, "ok")

    res = O.save_for_next_boot(setter, "ORIGIN_", 47.62691, -122.39018, 0.0,
                               origin_is_set=True)
    assert res.outcome == "partial"
    assert "inconsistent" in res.message


def test_origin_parameters_round_trip_through_float32():
    """A read-back that differs in the seventh decimal is storage, not a
    mismatch, and chasing it would waste an operator's morning."""
    assert O.float32(47.62691) == pytest.approx(47.62691, abs=2e-5)
    assert O.float32(47.62691) != 47.62691


def test_a_zero_origin_message_is_not_a_real_origin():
    """GPS_GLOBAL_ORIGIN carrying 0, 0 is the uninitialised value."""

    class Mav:
        system = component = 1

        def read(self, name):
            return mav2rest.Sample(
                name=name, fresh=True,
                message={"latitude": 0, "longitude": 0, "altitude": 0})

    is_set, detail = O.read_active(Mav(), request=False)
    assert is_set is False and "0, 0" in detail["note"]


def test_reading_the_origin_needs_a_request_and_that_is_a_write():
    """ArduPilot does not stream GPS_GLOBAL_ORIGIN, so confirming it sends
    the vehicle a command -- which a read-only connection refuses."""
    mav = mav2rest.Mavlink2Rest("192.0.2.1", allow_writes=False)
    with pytest.raises(mav2rest.VehicleWriteRefused):
        O.read_active(mav, request=True)


# --------------------------------------------------------------------------
#  The extensions
# --------------------------------------------------------------------------


def _vessel(**kw):
    st = E.VesselStatus(reachable=True, mono=time.monotonic(), **kw)
    st.changed_mono = st.mono
    return st


def test_a_vessel_with_no_gga_never_plots_null_island():
    """`/status` returns latitude 0 and longitude 0 before its first sentence."""
    st = E.VesselStatus(reachable=True, gga_ok=False, hdt_ok=False,
                        mono=time.monotonic())
    rows = E.vessel_readings(st)
    assert rows["position"].quality is Quality.NEVER_RECEIVED
    assert rows["position"].number() is None


def test_gga_and_hdt_go_stale_independently():
    """A compass can lose heading while still reporting a position, and a bow
    pointing a way nobody has confirmed is worse than no bow."""
    st = _vessel(gga_ok=True, hdt_ok=False, lat=47.6, lon=-122.4,
                 heading_deg=None, inject_ok=True)
    rows = E.vessel_readings(st)
    assert rows["position"].quality is Quality.OK
    assert rows["heading"].quality is Quality.NEVER_RECEIVED


def test_the_extensions_missing_fields_are_marked_unsupported_not_zero():
    """Its /status publishes three booleans and three numbers. Fix quality,
    HDOP and satellite count are not among them, and its course and speed
    over ground are fixed at 0 and never written from NMEA."""
    rows = E.vessel_readings(_vessel(gga_ok=True, lat=47.6, lon=-122.4))
    assert rows["fix_quality"].quality is Quality.UNSUPPORTED
    assert rows["speed_over_ground"].quality is Quality.UNSUPPORTED
    assert "not a measurement" in rows["speed_over_ground"].note


def test_an_identical_cached_status_does_not_stay_fresh(monkeypatch):
    """Polling twice does not make a frozen extension fresh."""
    body = json.dumps({"gga_status": True, "hdt_status": True,
                       "inject_status": True, "latitude": 47.6,
                       "longitude": -122.4, "heading": 88.0,
                       "ugps_host": "http://192.168.2.94", "send_rate": 2.0})
    monkeypatch.setattr(E, "_get", lambda url, timeout=3.0: (True, body, ""))
    reader = E.VesselReader()
    svc = E.Service("WL UGPS External", 8080, found=True)

    first = reader.read("192.0.2.1", svc)
    assert first.changed_mono is not None
    # Wind the change time back past the extension's own four-second window.
    reader._last_change_mono = time.monotonic() - 10.0
    again = reader.read("192.0.2.1", svc)
    rows = E.vessel_readings(again)
    assert rows["position"].quality is Quality.STALE
    assert "same values" in rows["position"].note


def test_gps_input_vdop_is_read_as_an_acoustic_standard_deviation():
    """The Water Linked extension repurposes the field; read as a vertical
    dilution of precision it is meaningless."""
    sample = mav2rest.Sample(
        name="GPS_RAW_INT", fresh=True,
        message={"fix_type": 3, "vdop": 0.42, "hdop": 1.0,
                 "satellites_visible": 6, "lat": 476269100,
                 "lon": -1223901800})
    st = E.acoustic_from_gps_input(sample)
    assert st.std_m == pytest.approx(0.42)
    assert st.lat == pytest.approx(47.62691)


def test_the_invalid_sentinels_are_recognized():
    sample = mav2rest.Sample(
        name="GPS_RAW_INT", fresh=True,
        message={"fix_type": 0, "vdop": 65535.0, "hdop": 65535.0,
                 "satellites_visible": 0, "lat": 0, "lon": 0})
    st = E.acoustic_from_gps_input(sample)
    assert st.std_m is None and st.hdop is None and st.lat is None


def test_the_dvl_message_type_is_read_from_its_status():
    status = {"status": "running", "enabled": True, "orientation": 1,
              "hostname": "192.168.2.95", "rangefinder": True,
              "should_send": "POSITION_DELTA", "origin": [47.6, -122.4]}
    import rov_flight_ops.nav.extensions as ext
    real = ext._get
    try:
        ext._get = lambda url, timeout=3.0: (True, json.dumps(status), "")
        st = ext.read_dvl("192.0.2.1", ext.Service("dvl", 9001, found=True))
    finally:
        ext._get = real
    assert st.message_type == "POSITION_DELTA"
    assert st.cached_origin == (47.6, -122.4)


# --------------------------------------------------------------------------
#  The sensor matrix
# --------------------------------------------------------------------------


def _snapshot(**kw):
    s = M.NavSnapshot()
    s.params = dict(EBM_E_2026_09_18)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_the_matrix_separates_receiving_valid_and_fused():
    """The 18 September state, as the panel would have shown it: the DVL
    delivering perfectly and the estimator not using it for position."""
    from rov_flight_ops.gui.navstatus import matrix_rows

    s = _snapshot()
    s.dvl = {"message_type": M.good("POSITION_DELTA"),
             "bottom_lock": M.good(True)}
    s.ekf = {"horiz_pos_abs": M.good(False), "horiz_pos_rel": M.good(True),
             "const_pos_mode": M.good(False), "flags": M.good("EKF_ATTITUDE")}
    rows = matrix_rows(s, time.monotonic())

    # Position and velocity are separate rows now: in the acoustic profile the
    # DVL supplies velocity while position comes from the acoustics, and one
    # DVL row could not say that.
    pos = rows["dvl_position"]
    assert pos.marks[1][0] == "✓"            # receiving
    assert pos.marks[2][0] == "✓"            # the measurement is valid
    assert pos.marks[3][0] == "◐"            # relative aiding, not absolute
    assert "relative aiding" in pos.detail
    assert "body-frame odometry" in pos.basis

    vel = rows["dvl_velocity"]
    assert vel.marks[3][0] == "✓"            # velocity really is fused

    ekf = rows["ekf"]
    assert "relative" in ekf.detail and "dead-reckoned" in ekf.detail


def test_the_vessel_is_never_marked_as_fused_into_the_vehicle_position():
    from rov_flight_ops.gui.navstatus import matrix_rows

    s = _snapshot()
    s.vessel = E.vessel_readings(_vessel(gga_ok=True, hdt_ok=True, lat=47.6,
                                         lon=-122.4, heading_deg=88.0,
                                         inject_ok=True))
    rows = matrix_rows(s, time.monotonic())
    assert "never fused as ROV position" in rows["vessel_gga"].detail
    assert "not the ROV" in rows["vessel_hdt"].detail


def test_the_surftrak_fixit_orientation_damage_is_recognized():
    """Surftrak Fixit v1.0.0-beta.2's `prb_bad_max` branch logs 'setting
    RNGFND1_MAX_CM to 5000' and calls set_param('RNGFND1_ORIENT', 5000).
    This program never calls it; it checks for the damage and names it."""
    from rov_flight_ops.gui.navstatus import matrix_rows

    s = _snapshot()
    s.params["RNGFND1_ORIENT"] = 5000.0
    detail = matrix_rows(s, time.monotonic())["range"].detail
    assert "5000" in detail and "Surftrak Fixit" in detail

    healthy = matrix_rows(_snapshot(), time.monotonic())["range"].detail
    assert "Surftrak Fixit" not in healthy
