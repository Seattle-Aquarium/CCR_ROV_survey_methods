"""
The collector, end to end, against a fake vehicle that speaks the real shapes.

A small HTTP server standing in for mavlink2rest, the BlueOS helper and the
two extensions, answering with the schemas the September 2026 stack actually
uses -- including the ones that are easy to get wrong: mavlink2rest's
`status.time.counter`, the Water Linked DVL's `should_send`, and the WL UGPS
External extension's `/status` with its three booleans.

What this pins down is the part unit tests cannot: that the collector
discovers, reads, assembles and stops without a vehicle in the room, and that
it sends nothing while doing it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rov_flight_ops.nav import extensions as E
from rov_flight_ops.nav.collector import NavCollector
from rov_flight_ops.nav.model import Quality

#: Every request the fake saw, as (method, path). The read-only guarantee is
#: checked against this rather than against intent.
SEEN: list[tuple[str, str]] = []

#: One counter per message, so "has anything new arrived" can be driven.
COUNTERS: dict[str, int] = {}

STATE = {
    "ATTITUDE": {"roll": 0.02, "pitch": -0.01, "yaw": -1.2},
    "VFR_HUD": {"heading": 291, "groundspeed": 0.31, "alt": 13.4, "climb": 0.0},
    "GLOBAL_POSITION_INT": {"lat": 476269100, "lon": -1223901800,
                            "relative_alt": -13486, "hdg": 29100},
    "LOCAL_POSITION_NED": {"x": -10.5, "y": -31.6, "z": 13.4,
                           "vx": 0.21, "vy": 0.22, "vz": 0.0},
    "RANGEFINDER": {"distance": 0.86, "voltage": 0.0},
    "NAMED_VALUE_FLOAT": {"name": "RFTarget", "value": 0.75,
                          "time_boot_ms": 341271},
    "BATTERY_STATUS": {"voltages": [16040, 65535], "current_battery": 130,
                       "battery_remaining": -1},
    "HEARTBEAT": {"custom_mode": 21, "base_mode": 209,
                  "autopilot": {"type": "MAV_AUTOPILOT_ARDUPILOTMEGA"}},
    "EKF_STATUS_REPORT": {
        "flags": "EKF_ATTITUDE | EKF_VELOCITY_HORIZ | EKF_POS_HORIZ_REL "
                 "| EKF_POS_VERT_ABS",
        "velocity_variance": 0.03, "pos_horiz_variance": 0.0002,
        "pos_vert_variance": 0.06, "compass_variance": 0.09},
    "GPS_RAW_INT": {"fix_type": 0, "satellites_visible": 0, "lat": 0, "lon": 0,
                    "vdop": 65535.0, "hdop": 65535.0},
    "SYS_STATUS": {"load": 350},
    "SCALED_PRESSURE2": {"temperature": 1120, "press_abs": 2320.0},
    "VIBRATION": {"vibration_x": 0.2, "vibration_y": 0.2, "vibration_z": 0.3},
    "SYSTEM_TIME": {"time_unix_usec": 1789756156000000},
    "DISTANCE_SENSOR": {"current_distance": 86, "id": 0},
    "AUTOPILOT_VERSION": {"flight_sw_version": 67437311},
}

DVL_STATUS = {"status": "running", "enabled": True, "orientation": 1,
              "hostname": "192.168.2.95", "rangefinder": True,
              "should_send": "POSITION_DELTA",
              "origin": [47.62691, -122.39018]}

VESSEL_STATUS = {"gga_status": False, "hdt_status": False,
                 "inject_status": False, "latitude": 0, "longitude": 0,
                 "heading": 0, "ugps_host": "http://192.168.2.94",
                 "send_rate": 2.0}


class _Fake(BaseHTTPRequestHandler):
    """Answers the navigation endpoints; refuses to be written to."""

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):            # noqa: N802
        SEEN.append(("POST", self.path))
        self._send({"refused": "this fake never accepts a write"}, 405)

    def do_GET(self):             # noqa: N802
        path = self.path
        SEEN.append(("GET", path))

        if path.endswith("/web_services"):
            return self._send([
                {"name": "Water Linked DVL", "port": 9001},
                {"name": "WL UGPS External", "port": 8080},
                {"name": "Water Linked UGPS", "port": 8081},
            ])
        if path == "/get_status":
            return self._send(DVL_STATUS)
        if path == "/status":
            return self._send(VESSEL_STATUS)
        if path.startswith("/v1/mavlink/vehicles") and "/messages/" in path:
            name = path.rsplit("/", 1)[-1]
            if name not in STATE:
                return self._send({"error": "never received"}, 404)
            COUNTERS[name] = COUNTERS.get(name, 0) + 1
            return self._send({
                "message": {"type": name, **STATE[name]},
                "status": {"time": {
                    "counter": COUNTERS[name], "frequency": 4.0,
                    "first_update": "2026-09-18T18:29:16.0Z",
                    "last_update": "2026-09-18T18:29:19.0Z"}}})
        if path.rstrip("/") == "/v1/mavlink/vehicles":
            return self._send({"1": {"components": {"1": {}}}})
        return self._send({"ok": True})


@pytest.fixture()
def vehicle():
    SEEN.clear()
    COUNTERS.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_port}", srv
    srv.shutdown()


def _wired(host: str, port: int) -> NavCollector:
    """A collector pointed entirely at the fake, on one port."""
    c = NavCollector(host)
    c.mav.port = port
    c.services = {
        "dvl": E.Service("Water Linked DVL", port, found=True, via="test"),
        "ugps_external": E.Service("WL UGPS External", port, found=True,
                                   via="test"),
        "ugps": E.Service("Water Linked UGPS", port, found=True, via="test"),
    }
    return c


def _pump(c: NavCollector, cycles: int = 4) -> None:
    for i in range(cycles):
        c._next = {k: 0.0 for k in c._next}
        c._cycle(time.monotonic() + i)


def test_the_collector_assembles_a_whole_snapshot(vehicle):
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c)
    s = c.snapshot()

    assert s.mode.value == "Surftrak"
    assert s.altitude.number() == pytest.approx(0.86)
    assert s.depth.number() == pytest.approx(-13.486, abs=1e-3)
    assert s.speed.number() == pytest.approx(0.304, abs=0.01)
    assert s.heading.number() == pytest.approx(291.0)
    assert s.surftrak_target.number() == pytest.approx(0.75)
    assert s.voltage.number() == pytest.approx(16.04)
    assert s.current.number() == pytest.approx(1.30)
    assert s.watts.number() == pytest.approx(16.04 * 1.30, rel=1e-6)
    assert s.rov_fix is not None
    assert s.rov_fix.lat == pytest.approx(47.62691)
    assert s.armed.number() == 1.0          # base_mode 209 has the armed bit


def test_the_collector_reads_the_extensions_and_reports_the_message_type(vehicle):
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c)
    s = c.snapshot()

    assert s.dvl["message_type"].value == "POSITION_DELTA"
    # POSITION_DELTA cannot give an absolute position, so it is marked invalid
    # with the reason, not shown as a healthy setting.
    assert s.dvl["message_type"].quality is Quality.INVALID
    assert "relative aiding" in s.dvl["message_type"].note


def test_a_vessel_that_has_never_had_a_fix_is_not_plotted_at_null_island(vehicle):
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c)
    s = c.snapshot()
    assert s.vessel_fix is None
    assert s.vessel["position"].quality is Quality.NEVER_RECEIVED


def test_the_estimator_row_reports_relative_aiding_honestly(vehicle):
    """The fake reports EKF_POS_HORIZ_REL and not _ABS -- the 18 September
    state. The matrix must say dead-reckoned, not healthy."""
    from rov_flight_ops.gui.navstatus import matrix_rows

    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c)
    s = c.snapshot()
    assert s.ekf["horiz_pos_rel"].number() == 1.0
    assert s.ekf["horiz_pos_abs"].number() == 0.0

    rows = matrix_rows(s, time.monotonic())
    assert "relative" in rows["ekf"].detail


def test_a_frozen_vehicle_goes_stale_rather_than_holding_a_number(vehicle):
    """The counters stop moving while the HTTP keeps succeeding -- exactly
    what a disconnected sensor behind a live mavlink2rest looks like.

    This is the whole reason freshness is taken from the counter. Every read
    below returns HTTP 200 and a perfect 0.86 m altitude; none of them is a
    new measurement, and the page has to know that.
    """
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c)
    assert c.snapshot().altitude.quality is Quality.OK

    original = _Fake.do_GET
    try:
        def do_GET(self):                    # noqa: N802
            """Answers perfectly, and never advances a counter."""
            path = self.path
            SEEN.append(("GET", path))
            if path.startswith("/v1/mavlink/vehicles") and "/messages/" in path:
                name = path.rsplit("/", 1)[-1]
                if name not in STATE:
                    return self._send({"error": "never received"}, 404)
                return self._send({
                    "message": {"type": name, **STATE[name]},
                    "status": {"time": {"counter": COUNTERS.get(name, 1),
                                        "frequency": 0.0,
                                        "last_update": "2026-09-18T18:29:19.0Z"}}})
            return original(self)

        _Fake.do_GET = do_GET
        frozen_reads = [c.mav.read("RANGEFINDER") for _ in range(3)]
    finally:
        _Fake.do_GET = original

    assert all(s.error == "" for s in frozen_reads), "the requests succeeded"
    assert all(s.num("distance") == 0.86 for s in frozen_reads), "with a value"
    assert not any(s.fresh for s in frozen_reads), "and none was a new message"

    health = c.mav.health["RANGEFINDER"]
    assert health.seen
    # And the page's own aging turns that into a stale reading.
    from rov_flight_ops.nav import model as MM
    aged = MM.age_out(c.snapshot().altitude, 0.0)
    assert aged.quality is Quality.STALE
    assert aged.held() == pytest.approx(0.86), "the last value is still shown"


def test_the_collector_sends_the_vehicle_nothing(vehicle):
    """Every navigation read is a GET. The one thing that can POST needs the
    operator's unlock and is never reached from the polling loop."""
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    c._discover()
    _pump(c, cycles=6)
    assert SEEN, "the fake saw no traffic at all"
    assert {m for m, _p in SEEN} == {"GET"}


def test_discovery_finds_the_extensions_from_the_blueos_service_list(vehicle):
    addr, _srv = vehicle
    host, port = addr.split(":")
    found = E.discover(f"{host}:{port}")
    assert found["dvl"].port == 9001 and found["dvl"].via == "helper"
    assert found["ugps_external"].port == 8080
    assert found["ugps_external"].via == "helper"


def test_a_vehicle_that_is_not_there_leaves_a_usable_page():
    """Nothing raises, and the page gets an honest empty snapshot."""
    c = NavCollector("127.0.0.1")
    c.mav.port = 9              # discard: refuses connections
    c.mav.timeout = 0.2
    c.services = {}
    c._cycle(time.monotonic())
    s = c.snapshot()
    assert s.altitude.number() is None
    assert s.rov_fix is None


def test_stopping_is_immediate_and_reports_whether_it_finished():
    c = NavCollector("127.0.0.1")
    c.mav.port = 9
    c.mav.timeout = 0.2
    c.start("test")
    began = time.monotonic()
    c.stop()                    # the default does not join
    assert time.monotonic() - began < 0.2
    assert c._stop.is_set()
    # And a caller that genuinely wants to wait can.
    c.stop(timeout=5.0)


def test_a_slow_only_cycle_does_not_age_what_it_did_not_read(vehicle):
    """The fast and slow groups are read on different cadences.

    A cycle where only the slow group is due must not conclude that the
    position, the altitude or anything else in the fast group has gone stale
    -- it did not look at them. Timing jitter makes slow-only cycles rare and
    not impossible, and the symptom would be the map blinking to "last known"
    every couple of seconds on a perfectly healthy vehicle.
    """
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))
    _pump(c, cycles=2)
    good = c.snapshot()
    assert good.rov_fix is not None and good.rov_fix.quality is Quality.OK
    assert good.altitude.quality is Quality.OK

    # Only the slow group is due.
    now = time.monotonic() + 100
    c._next["fast"] = now + 10
    c._next["ext"] = now + 10
    c._next["param"] = now + 10
    c._next["slow"] = 0.0
    c._cycle(now)

    after = c.snapshot()
    assert after.rov_fix is not None
    assert after.rov_fix.quality is Quality.OK, "the fix was aged unread"
    assert after.altitude.quality is Quality.OK, "the altitude was aged unread"
    assert after.rov_fix.lat == pytest.approx(good.rov_fix.lat)


def test_a_slow_parameter_read_does_not_hold_the_poll_cadence(vehicle):
    """Parameters come from the head of a dataflash log over File Browser --
    a third of a second against a healthy vehicle and seconds against a busy
    one. On the collector's own thread that would freeze the 4 Hz instruments
    for its whole duration, every two minutes."""
    addr, _srv = vehicle
    host, port = addr.split(":")
    c = _wired(host, int(port))

    started = threading.Event()
    release = threading.Event()

    def slow_reader():
        started.set()
        release.wait(5.0)
        return {"EK3_SRC1_POSXY": 6.0}

    c.set_parameter_reader(slow_reader)
    try:
        began = time.monotonic()
        c._next = {k: 0.0 for k in c._next}
        c._cycle(time.monotonic())
        took = time.monotonic() - began
        assert started.wait(2.0), "the parameter read never started"
        assert took < 1.0, f"the cycle waited {took:.1f} s for the parameters"
        # And a second cycle does not start a competing reader.
        c._next["param"] = 0.0
        c._cycle(time.monotonic())
        release.set()
        for _ in range(50):
            if c.params:
                break
            time.sleep(0.05)
        assert c.params.get("EK3_SRC1_POSXY") == 6.0
    finally:
        release.set()
        if c._param_thread is not None:
            c._param_thread.join(3.0)
