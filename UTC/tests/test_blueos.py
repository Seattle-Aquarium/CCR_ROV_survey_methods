"""Asking the ROV what it offers.

This runs against a small fake BlueOS rather than a real vehicle, so it can
run in CI and on a desk. What it pins down is the behaviour that matters in
the field: the probe never raises, never writes to the vehicle, and reports
honestly when it cannot reach one — because a tool that throws a traceback on
a wet deck is worse than no tool.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import blueos  # noqa: E402

SERVICES = [
    {"name": "helper", "port": 81, "path": "/helper"},
    {"name": "recorder-extractor", "port": 9997, "path": "/recorder-extractor"},
]

#: Two volumes, as a Pi reports them: a small boot partition that is always
#: nearly full, and the one the recordings are actually written to. Picking
#: the wrong one would report 0.2 GiB free on a healthy vehicle.
DISKS = [
    {"name": "/dev/mmcblk0p1", "mount_point": "/boot",
     "available_space_B": 210_000_000, "total_space_B": 268_435_456},
    {"name": "/dev/mmcblk0p2", "mount_point": "/usr/blueos/userdata",
     "available_space_B": 4_509_715_660, "total_space_B": 30 * 2 ** 30},
]

#: The shape BlueOS actually publishes, copied from this programme's own
#: recordings of 3 September 2026. `FrequencyCapping` is the Pi capping its
#: clock because it is hot; nothing here is an under-voltage event.
PLATFORM = {"Ok": {"model": "Raspberry Pi 4 B", "raspberry": {
    "model": "Raspberry Pi 4 B", "soc": "BCM2711",
    "events": {"occurring": [], "list": [
        {"time": "2026-09-03T21:00:07.729041554Z", "type": "FrequencyCapping"},
        {"time": "2026-09-03T21:06:11.101002031Z", "type": "FrequencyCapping"},
        {"time": "2026-09-03T21:13:41.870331054Z", "type": "FrequencyCapping"},
    ]}}}}

MEMORY = {"ram": {"total_kB": 8_000_000, "used_kB": 1_440_000}}

PARAMS = {"RNGFND1_TYPE": 21.0, "BARO_PRIMARY": 1.0, "SCHED_LOOP_RATE": 200.0}

#: Every request the fake vehicle was asked to serve, so a test can assert
#: that nothing but GET was ever sent.
SEEN: list[tuple[str, str]] = []


class _Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):            # keep pytest output clean
        pass

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        SEEN.append(("GET", self.path))
        if self.path.endswith("/version/current"):
            return self._send(200, json.dumps({"version": "1.5.47-beta"}).encode())
        if self.path.endswith("/web_services"):
            return self._send(200, json.dumps(SERVICES).encode())
        if self.path.endswith("/vehicle_type"):
            return self._send(200, b'"Sub"')
        if self.path.endswith("/system/disk"):
            return self._send(200, json.dumps(DISKS).encode())
        # Deliberately only the second of the two candidates: BlueOS moves
        # these paths between releases, so the fallback chain has to work.
        if self.path.endswith("/system/platform"):
            return self._send(200, json.dumps(PLATFORM).encode())
        if self.path.endswith("/system/memory"):
            return self._send(200, json.dumps(MEMORY).encode())
        if self.path.endswith("/v1.0/parameters"):
            return self._send(200, json.dumps(PARAMS).encode())
        if "recorder" in self.path:
            if self.headers.get("Range"):
                return self._send(206, b"\x89MCAP0\r\n" + b"0" * 100,
                                  "application/octet-stream")
            return self._send(200, json.dumps(
                {"items": [{"name": "recorder_20260903_190601.mcap",
                            "size": 5_303_579_794}]}).encode())
        self._send(404, b"{}")

    # Anything that could change the vehicle is refused loudly, so a test
    # would fail rather than the probe quietly mutating a real ROV.
    def do_POST(self):
        SEEN.append(("POST", self.path))
        self._send(405, b"{}")

    def do_DELETE(self):
        SEEN.append(("DELETE", self.path))
        self._send(405, b"{}")


@pytest.fixture()
def vehicle():
    SEEN.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------
#  with a vehicle
# --------------------------------------------------------------------------


def test_it_reads_the_version_and_the_service_list(vehicle):
    rep = blueos.probe(host=vehicle)
    assert rep.reachable
    assert rep.version == "1.5.47-beta"
    assert rep.vehicle == "Sub"
    assert [s["name"] for s in rep.services] == ["helper", "recorder-extractor"]


def test_it_finds_out_whether_a_header_can_be_read_without_downloading(vehicle):
    """This is the question the whole feature turns on: judging a recording's
    span on the vehicle means reading its first kilobyte, not its 5 GB."""
    rep = blueos.probe(host=vehicle)
    assert rep.range_supported is True
    assert any(a.status == 206 for a in rep.answers)


def test_the_probe_only_ever_reads(vehicle):
    """Read-only is the safety property. Freeing space on the Pi stays a
    deliberate act in BlueOS's own interface."""
    blueos.probe(host=vehicle)
    assert SEEN, "the fake vehicle saw no requests at all"
    assert {m for m, _ in SEEN} == {"GET"}, sorted(set(m for m, _ in SEEN))


def test_the_report_names_the_host_and_lists_what_answered(vehicle):
    text = blueos.probe(host=vehicle).report()
    assert vehicle in text
    assert "1.5.47-beta" in text
    assert "recorder-extractor" in text
    assert "range reads" in text


# --------------------------------------------------------------------------
#  without one
# --------------------------------------------------------------------------


def test_no_vehicle_is_reported_not_raised():
    """A field laptop that has not been plugged in yet is the normal case."""
    rep = blueos.probe(host=None)
    if rep.reachable:                       # something really is listening
        pytest.skip("a vehicle answered on this network")
    assert not rep.reachable
    text = rep.report()
    assert "No vehicle answered" in text
    assert "192.168.2.2" in text, "should say where it looked"


def test_find_host_returns_none_when_nothing_listens():
    assert blueos.find_host(["203.0.113.1"], timeout=0.25) is None


def test_a_request_that_fails_is_recorded_rather_than_thrown():
    a = blueos._get("http://127.0.0.1:9/nothing", timeout=0.25)
    assert not a.ok and a.error
    assert "  [ -- ]" in a.line()


def test_a_dead_endpoint_does_not_stop_the_rest(vehicle):
    """404s are expected -- the probe is trying candidates on purpose."""
    rep = blueos.probe(host=vehicle)
    assert any(a.status == 404 for a in rep.answers), "expected some misses"
    assert rep.version, "a miss must not abort the run"


def test_a_reading_falls_through_to_the_next_candidate_endpoint(vehicle):
    """The first platform path 404s on this vehicle and the second answers.
    Endpoints move between BlueOS releases, so one that has moved must cost a
    request rather than the reading."""
    assert blueos.PLATFORM_PROBES[0] == "/system-information/platform"
    assert blueos.read_platform(vehicle).found


def test_the_recorder_path_is_the_one_the_vehicle_logs():
    """Observed in the extension's own output, not guessed."""
    assert blueos.RECORDER_DIR == "/usr/blueos/userdata/recorder"


# --------------------------------------------------------------------------
#  before the dive: room on the vehicle
# --------------------------------------------------------------------------


def test_free_space_is_read_from_the_volume_the_recordings_live_on(vehicle):
    """A Pi's boot partition is small and always nearly full. Reading that one
    would report a healthy vehicle as having no room at all."""
    space = blueos.read_space(vehicle)
    assert space.found
    assert space.path == "/usr/blueos/userdata"
    assert space.free_bytes == 4_509_715_660


def test_room_is_answered_in_minutes_of_recording_not_gigabytes(vehicle):
    """Gigabytes are not the question being asked on a deck."""
    space = blueos.read_space(vehicle)
    assert 50 < space.minutes_left < 56          # 4.2 GiB at ~1.4 MB/s
    _, text = space.verdict()
    assert "minutes of recording" in text


def test_a_dive_that_will_not_fit_is_refused_before_anyone_gets_wet():
    space = blueos.Space(found=True, free_bytes=2 * 2 ** 30,
                         total_bytes=30 * 2 ** 30)
    ok, why = space.verdict(planned_seconds=60 * 60)      # an hour
    assert not ok
    assert "stop part way" in why
    assert "60 minutes" in why, "must say what it compared against"


def test_a_dive_that_only_just_fits_is_allowed_but_warned_about():
    space = blueos.Space(found=True, free_bytes=int(2.5 * 2 ** 30))
    ok, why = space.verdict(planned_seconds=25 * 60)
    assert ok and "not much more" in why


def test_unreadable_free_space_does_not_block_a_dive():
    """A missing endpoint must never be the reason a survey does not happen."""
    ok, why = blueos.Space().verdict(planned_seconds=3600)
    assert ok and "could not be read" in why


# --------------------------------------------------------------------------
#  before the dive: the Pi itself
# --------------------------------------------------------------------------


def test_the_pi_s_throttle_log_is_read_and_counted(vehicle):
    plat = blueos.read_platform(vehicle)
    assert plat.found
    assert plat.model == "Raspberry Pi 4 B"
    assert plat.throttle == {"FrequencyCapping": 3}
    assert plat.first_event.startswith("2026-09-03T21:00:07")
    assert plat.last_event.startswith("2026-09-03T21:13:41")
    assert round(plat.ram_used, 2) == 0.18


def test_frequency_capping_is_reported_as_heat_and_not_as_a_power_fault():
    """The distinction matters: one is a Pi in a sealed tube, the other is a
    failing tether, and they look identical in a CPU graph."""
    plat = blueos.Platform(found=True, throttle={"FrequencyCapping": 88})
    assert not plat.undervoltage
    assert "thermal" in plat.advice()
    assert "88 past throttle events" in plat.note()


def test_undervoltage_is_called_out_as_a_power_problem():
    plat = blueos.Platform(found=True, throttle={"UnderVoltage": 2})
    assert plat.undervoltage
    assert "power problem" in plat.advice()
    assert "tether" in plat.advice()


def test_throttling_happening_right_now_outranks_the_history():
    plat = blueos.Platform(found=True, throttle={"FrequencyCapping": 4},
                           occurring=["FrequencyCapping"])
    assert "THROTTLING NOW" in plat.note()
    assert "right now" in plat.advice()


def test_a_quiet_pi_says_so_and_offers_no_advice():
    plat = blueos.Platform(found=True, model="Raspberry Pi 4 B")
    assert "no throttling logged" in plat.note()
    assert plat.advice() == ""


# --------------------------------------------------------------------------
#  the parameter snapshot
# --------------------------------------------------------------------------


def test_the_parameter_set_is_read_and_says_where_it_came_from(vehicle):
    params, source = blueos.read_parameters(vehicle)
    assert params["RNGFND1_TYPE"] == 21.0
    assert "parameters" in source, "the endpoint that answered must be recorded"


def test_parameters_arriving_as_a_list_are_folded_into_names(vehicle,
                                                             monkeypatch):
    """mavlink2rest returns PARAM_VALUE messages, whose names are padded with
    NULs. Stored raw they would not be searchable."""
    body = json.dumps([{"param_id": "SURFACE_DEPTH\x00\x00", "param_value": -10.0}])
    monkeypatch.setattr(blueos, "_first_ok",
                        lambda *a, **k: blueos.Answer(
                            url="http://x/mavlink2rest", ok=True, body=body))
    params, _ = blueos.read_parameters("x")
    assert params == {"SURFACE_DEPTH": -10.0}


def test_the_snapshot_lands_in_the_flights_own_logs_folder(tmp_path, vehicle):
    """It has to travel with the data. A configuration file in a temp folder
    answers no question six months later."""
    out = blueos.save_snapshot(tmp_path, vehicle, planned_seconds=45 * 60)
    assert out == tmp_path / "logs" / "vehicle_snapshot.json"

    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["blueos_version"] == "1.5.47-beta"
    assert snap["vehicle_type"] == "Sub"
    assert snap["parameter_count"] == 3
    assert snap["parameters"]["BARO_PRIMARY"] == 1.0
    assert snap["disk"]["enough_room"] is True
    assert snap["platform"]["throttle_events"] == {"FrequencyCapping": 3}
    assert snap["taken"], "an undated snapshot is not evidence of anything"


def test_a_snapshot_taken_with_no_vehicle_still_writes_a_file(tmp_path,
                                                              monkeypatch):
    """Better a record saying the vehicle was unreachable than no record."""
    monkeypatch.setattr(blueos, "find_host", lambda *a, **k: None)
    out = blueos.save_snapshot(tmp_path)
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["reachable"] is False and snap["taken"]


def test_taking_a_snapshot_never_writes_to_the_vehicle(tmp_path, vehicle):
    """The safety property, restated for the path that gathers the most."""
    blueos.save_snapshot(tmp_path, vehicle, planned_seconds=600)
    assert SEEN and {m for m, _ in SEEN} == {"GET"}


# --------------------------------------------------------------------------
#  the two together
# --------------------------------------------------------------------------


def test_readiness_puts_space_and_the_pi_in_one_answer(vehicle):
    r = blueos.check_readiness(host=vehicle, planned_seconds=10 * 60)
    assert r.reachable and r.ok
    text = "\n".join(r.lines())
    assert "minutes of recording" in text
    assert "Raspberry Pi 4 B" in text


def test_readiness_with_no_vehicle_reports_rather_than_raising(monkeypatch):
    monkeypatch.setattr(blueos, "find_host", lambda *a, **k: None)
    r = blueos.check_readiness(planned_seconds=600)
    assert not r.reachable and not r.ok
    assert "tether" in r.lines()[0]


def test_the_probe_report_carries_the_pre_dive_readings(vehicle):
    """One run beside the vehicle should settle every open question at once."""
    text = blueos.probe(host=vehicle).report()
    assert "before a dive:" in text
    assert "minutes of recording" in text
    assert "FrequencyCapping" in text
    assert "3 read from" in text, "should name the parameter endpoint"
