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


def test_the_recorder_path_is_the_one_the_vehicle_logs():
    """Observed in the extension's own output, not guessed."""
    assert blueos.RECORDER_DIR == "/usr/blueos/userdata/recorder"
