"""Asking the ROV what it offers.

This runs against a small fake BlueOS rather than a real vehicle, so it can
run in CI and on a desk. What it pins down is the behavior that matters in
the field: the probe never raises, never writes to the vehicle, and reports
honestly when it cannot reach one — because a tool that throws a traceback on
a wet deck is worse than no tool.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
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

#: The shape BlueOS actually publishes, copied from this program's own
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

#: A File Browser session token. The real vehicle hands one out to anybody who
#: GETs /api/login -- no credentials -- and it carries create, modify and
#: delete permissions. This program uses none of them.
FB_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake.token"

#: A recorder folder as File Browser reports it: two recordings and the
#: extracted-MP4 folder that sits beside one of them.
FB_ITEMS = [
    {"name": "recorder_20260903_190601.mcap", "size": 5_303_579_794,
     "isDir": False, "modified": "2026-09-06T21:36:28Z"},
    {"name": "recorder_20260906_213623.mcap", "size": 7_046_112,
     "isDir": False, "modified": "2026-09-06T21:36:28Z"},
    {"name": "recorder_20260906_213623", "size": 0, "isDir": True,
     "modified": "2026-09-06T21:36:30Z"},
    {"name": "notes.txt", "size": 12, "isDir": False,
     "modified": "2026-09-06T21:36:30Z"},
]

#: A minimal but real mcap opening: the magic, a HEADER record, then a CHUNK
#: whose payload starts with message_start_time. 1788730583.0 in nanoseconds.
START_NS = 1_788_730_583_000_000_000


def _mcap_head() -> bytes:
    """The magic, a HEADER record, then a CHUNK carrying the start time.

    Built from `blueos.MCAP_MAGIC` and `bytes(n)` rather than written out
    as escapes: an mcap opens with bytes that do not survive being retyped,
    and taking the magic from the module also means this fixture cannot
    drift away from what the module looks for.
    """
    out = bytearray(blueos.MCAP_MAGIC)
    out += bytes([0x01]) + struct.pack("<Q", 4) + b"prof"      # HEADER
    out += bytes([blueos.OP_CHUNK]) + struct.pack("<Q", 40)    # CHUNK
    out += struct.pack("<Q", START_NS)
    out += bytes(32)
    return bytes(out)


MCAP_BYTES = _mcap_head() + bytes(4096)

#: ArduPilot's own flight logs. The parameter set for a flight is written into
#: the head of each, which is why this program never has to ask the
#: autopilot for it.
DATAFLASH_ITEMS = [
    {"name": "00000080.BIN", "size": 6_815_744, "isDir": False,
     "modified": "2026-08-31T20:10:39Z"},
    {"name": "00000079.BIN", "size": 3_040_870, "isDir": False,
     "modified": "2026-08-31T19:41:36Z"},
    {"name": "LASTLOG.TXT", "size": 4, "isDir": False,
     "modified": "2026-08-31T19:53:09Z"},
]

EXTENSIONS = [
    {"name": "Cockpit Lite", "tag": "v1.19.0-beta.2", "enabled": True},
    {"name": "Water Linked DVL", "tag": "v1.0.10", "enabled": True},
    {"name": "dvl_beam_split", "tag": "latest", "enabled": False},
]

CONTAINERS = [
    {"name": "/blueos-core", "image": "bluerobotics/blueos-core:1.5.0-beta.39",
     "status": "Up 8 days"},
]

#: What mavlink2rest serves, one message type per URL.
LIVE = {
    "SYS_STATUS": {"voltage_battery": 23205, "current_battery": -22,
                   "battery_remaining": -1},
    "SCALED_PRESSURE": {"press_abs": 1024.86, "temperature": 3866},
    "HEARTBEAT": {"system_status": {"type": "MAV_STATE_CRITICAL"}},
}

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
        # --- File Browser, as the vehicle really behaves ---------------
        # A plain GET to /api/login returns a token with no credentials asked
        # for. That is the vehicle's configuration, reproduced here so the
        # read-only property can be asserted against it.
        if self.path.startswith("/api/login"):
            return self._send(200, FB_TOKEN.encode(), "text/plain")
        if self.path.startswith("/api/resources"):
            if not self.headers.get("X-Auth"):
                return self._send(401, b"401 Unauthorized",
                                  "text/plain")
            items = (DATAFLASH_ITEMS if "ardupilot_logs" in self.path
                     else FB_ITEMS)
            return self._send(200, json.dumps({"items": items}).encode())
        if self.path.startswith("/v2.0/installed_extensions"):
            return self._send(200, json.dumps(EXTENSIONS).encode())
        if self.path.startswith("/v2.0/container"):
            return self._send(200, json.dumps(CONTAINERS).encode())
        if self.path.endswith("/firmware_info"):
            return self._send(200, json.dumps(
                {"version": "4.5.0", "type": "STABLE"}).encode())
        if self.path.endswith("/v1.0/board"):
            return self._send(200, json.dumps({"name": "Navigator"}).encode())
        if self.path.endswith("/vehicle_name"):
            return self._send(200, b'"Nereo"')
        if self.path.startswith("/api/raw"):
            rng = self.headers.get("Range")
            if not rng:
                return self._send(200, MCAP_BYTES, "application/octet-stream")
            lo, _, hi = rng.split("=")[1].partition("-")
            lo, hi = int(lo), min(int(hi or 0), len(MCAP_BYTES) - 1)
            chunk = MCAP_BYTES[lo:hi + 1]
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range",
                             f"bytes {lo}-{hi}/{len(MCAP_BYTES)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            return self.wfile.write(chunk)
        if self.path.endswith("/system/temperature"):
            return self._send(200, json.dumps(
                [{"name": "cpu_thermal temp1", "temperature": 55.0,
                  "maximum_temperature": 59.9}]).encode())
        if self.path.endswith("/unix_time_seconds"):
            return self._send(200, str(time.time() + 4.0).encode(),
                              "text/plain")
        if "PARAM_VALUE" in self.path:
            return self._send(200, json.dumps({"message": {
                "param_id": list("STAT_RUNTIME"), "param_value": 13383334.0,
                "param_count": 983, "param_index": 65535}}).encode())
        if "/messages/" in self.path:
            name = self.path.rsplit("/", 1)[-1]
            if name in LIVE:
                return self._send(200,
                                  json.dumps({"message": LIVE[name]}).encode())
            return self._send(404, b"{}")

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
#  the transport, as confirmed against a live vehicle on 2026-09-08
# --------------------------------------------------------------------------


def test_a_session_opens_without_credentials(vehicle):
    """Reproducing the vehicle's own configuration, not endorsing it: BlueOS
    ships File Browser with authentication off, so a plain GET returns a token
    carrying create, modify and delete rights."""
    assert blueos.file_token(vehicle) == FB_TOKEN


def test_only_recordings_are_listed_not_the_folder_beside_them(vehicle):
    """The recorder folder also holds the extracted-MP4 directories and the
    odd stray file. Neither is a recording."""
    names = [r["name"] for r in blueos.list_recordings(vehicle)]
    assert names == ["recorder_20260903_190601.mcap",
                     "recorder_20260906_213623.mcap"]


def test_listing_without_a_token_yields_nothing_rather_than_raising(vehicle,
                                                                    monkeypatch):
    monkeypatch.setattr(blueos, "file_token", lambda *a, **k: "")
    assert blueos.list_recordings(vehicle) == []


def test_a_span_is_read_from_the_head_of_the_file(vehicle):
    """The capability the whole feature rests on. Reading a recording's true
    start means fetching its first kilobytes, not its five gigabytes."""
    start, end = blueos.read_span(vehicle, "recorder_20260906_213623.mcap",
                                  FB_TOKEN)
    assert start == 1_788_730_583.0
    assert end is None, "the end lives in a summary a truncated file lacks"


def test_the_span_read_asks_for_a_range_and_gets_one(vehicle):
    """If the vehicle ignored Range this would still work but would download
    the whole file, which is the difference between 96 KiB and 5 GB."""
    blueos.read_span(vehicle, "recorder_20260906_213623.mcap", FB_TOKEN)
    raws = [p for m, p in SEEN if m == "GET" and p.startswith("/api/raw")]
    assert raws, "no download was attempted at all"
    a = blueos._get(blueos.recording_url(vehicle,
                                         "recorder_20260906_213623.mcap",
                                         FB_TOKEN),
                    headers={"Range": "bytes=0-1023"}, binary=True, limit=1024)
    assert a.status == 206, "the vehicle must honor a range request"


def test_something_that_is_not_an_mcap_gives_no_span(vehicle, monkeypatch):
    monkeypatch.setattr(blueos, "MCAP_MAGIC", b"NOTMCAP!")
    assert blueos.read_span(vehicle, "x.mcap", FB_TOKEN) == (None, None)


def test_a_head_with_no_chunk_yields_no_start():
    """A recording whose first chunk is past what was fetched must report that
    it does not know, rather than guessing."""
    assert blueos._first_chunk_start(blueos.MCAP_MAGIC + bytes(64)) is None


def test_a_nonsense_record_length_stops_the_walk_rather_than_hanging():
    head = bytearray(blueos.MCAP_MAGIC)
    head += bytes([0x01]) + struct.pack("<Q", 1 << 40)      # absurd length
    assert blueos._first_chunk_start(bytes(head)) is None


def test_clock_skew_is_measured_and_signed(vehicle):
    """Recordings are stamped with the Pi's clock and transects with the
    laptop's. A skew is a silent constant offset between the two; the vehicle
    checked on 2026-09-08 was about four seconds ahead."""
    skew = blueos.clock_skew(vehicle)
    assert skew is not None and 3.0 < skew < 5.0


def test_temperature_reports_now_and_the_high_water_mark(vehicle):
    now, highest = blueos.read_temperature(vehicle)
    assert now == 55.0 and highest == 59.9


def test_live_telemetry_returns_only_what_the_vehicle_offered(vehicle):
    live = blueos.read_telemetry(vehicle)
    assert live["SYS_STATUS"]["voltage_battery"] == 23205
    assert live["SCALED_PRESSURE"]["temperature"] == 3866
    # ATTITUDE is in the default set but this vehicle does not serve it.
    assert "ATTITUDE" not in live


def test_the_parameter_count_is_free_but_the_set_is_not(vehicle):
    """mavlink2rest keeps only the most recent PARAM_VALUE. Getting all 983
    means sending the vehicle a PARAM_REQUEST_LIST, which is a write to the
    vehicle bus -- so this reads the count and stops there."""
    assert blueos.parameter_count(vehicle) == 983


def test_a_host_carrying_a_port_is_used_as_given():
    """What lets one fake vehicle stand in for every service, and what would
    let someone point this at an SSH tunnel."""
    assert blueos._base("10.0.0.4:8080", 7777) == "http://10.0.0.4:8080"
    assert blueos._base("blueos", 7777) == "http://blueos:7777"


# --------------------------------------------------------------------------
#  parameters, from the flight logs rather than from the autopilot
# --------------------------------------------------------------------------


def test_only_flight_logs_are_listed(vehicle):
    """LASTLOG.TXT sits in the same folder and is not a flight."""
    names = [g["name"] for g in blueos.list_dataflash(vehicle)]
    assert names == ["00000080.BIN", "00000079.BIN"], "newest first, .BIN only"


def test_parameters_come_from_a_log_and_never_from_asking_the_vehicle(vehicle):
    """The design decision this rests on.

    Collecting all 1,014 parameters by asking the autopilot means sending it a
    PARAM_REQUEST_LIST, which is traffic on the vehicle bus. Reading them from
    the log it already wrote is a plain GET, is the set as *flown* rather than
    as currently set, and works for flights that happened years ago.
    """
    SEEN.clear()
    blueos.read_parameters_from_log(vehicle, "00000080.BIN")
    assert SEEN and {m for m, _ in SEEN} == {"GET"}
    assert not any("PARAM_REQUEST" in p for _m, p in SEEN)


def test_only_the_head_of_a_log_is_fetched(vehicle):
    """A whole log runs to 78 MB and the parameters are in the first 512 KiB."""
    SEEN.clear()
    blueos.read_parameters_from_log(vehicle, "00000080.BIN", kib=512)
    assert blueos.DATAFLASH_HEAD_KIB <= 512


def test_an_unreadable_log_yields_nothing_rather_than_raising(vehicle):
    """The fake vehicle serves bytes that are not a dataflash log at all."""
    assert blueos.read_parameters_from_log(vehicle, "00000080.BIN") == {}
    assert blueos.parse_parameters(b"not a dataflash log") == {}


def test_a_diff_tells_a_changed_value_from_an_added_one():
    """A firmware update moves parameters in and out; a person turns a knob.
    Reading the second as the first would be misleading, so absence is None.
    """
    before = {"SURFACE_DEPTH": -10.0, "ARMING_SKIPCHK": 16776767.0}
    after = {"SURFACE_DEPTH": -15.0, "ARMING_CHECK": 448.0}
    assert blueos.diff_parameters(before, after) == {
        "ARMING_CHECK": (None, 448.0),          # added by new firmware
        "ARMING_SKIPCHK": (16776767.0, None),   # gone with the old
        "SURFACE_DEPTH": (-10.0, -15.0),        # actually changed
    }
    assert blueos.diff_parameters(before, before) == {}


def test_versions_cover_everything_that_could_explain_a_change(vehicle):
    v = blueos.read_versions(vehicle)
    assert v["ardusub"] == "4.5.0" and v["ardusub_type"] == "STABLE"
    assert v["board"] == "Navigator"
    names = [e["name"] for e in v["extensions"]]
    assert "Cockpit Lite" in names
    assert any(e["enabled"] is False for e in v["extensions"]), (
        "a disabled extension must be recorded as installed but off")
    # The containers say what is *running*, which is not always what is
    # installed: an extension can be updated and not restarted.
    assert v["containers"][0]["image"].endswith("1.5.0-beta.39")


def test_a_snapshot_records_the_versions_and_the_clock(tmp_path, vehicle):
    out = blueos.save_snapshot(tmp_path, vehicle, planned_seconds=45 * 60)
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["vehicle_name"] == "Nereo"
    assert snap["versions"]["ardusub"] == "4.5.0"
    assert snap["parameters_from"] == "00000080.BIN"
    assert snap["previous_flight_log"] == "00000079.BIN"
    assert 3.0 < snap["clock_skew_s"] < 5.0
    assert snap["platform"]["soc_c"] == 55.0


def test_the_whole_transport_only_ever_reads(vehicle):
    """The safety property, over every call that touches the vehicle.

    The token this uses carries delete rights. Nothing here may exercise them:
    a bug that destroys the only copy of a dive is the one failure this
    program must not have.
    """
    SEEN.clear()
    token = blueos.file_token(vehicle)
    blueos.list_recordings(vehicle, token)
    blueos.read_span(vehicle, "recorder_20260906_213623.mcap", token)
    blueos.read_telemetry(vehicle)
    blueos.read_temperature(vehicle)
    blueos.clock_skew(vehicle)
    blueos.parameter_count(vehicle)
    blueos.probe(host=vehicle)
    assert SEEN, "the fake vehicle saw no requests at all"
    assert {m for m, _ in SEEN} == {"GET"}, sorted({m for m, _ in SEEN})


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
    # Parameters come from the flight log the vehicle already wrote. This fake
    # serves bytes that are not a dataflash log, so the snapshot records that
    # it found one and could not read it -- rather than failing to write.
    assert snap["parameters_from"] == "00000080.BIN"
    assert snap["parameter_count"] == 0
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
