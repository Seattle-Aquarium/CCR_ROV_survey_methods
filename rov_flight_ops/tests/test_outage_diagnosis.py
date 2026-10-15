"""
The 15 October 2026 outages, made to diagnose themselves next time.

That day the vehicle disarmed seventeen times on ArduSub's ground-station
failsafe, each after three to five seconds in which only the Pi went silent.
Saying "only the Pi" took a manual review of a DVL capture that happened to be
running. These tests pin down the three changes that make it automatic, and
the one that stops the Navigation page pulling the whole dataflash log over
the tether every two minutes:

  * the fast network trace echoes the DVL beside the Pi, writes both to one
    ping file without disturbing its readers, and places every outage as
    "Pi only" or "whole vehicle" in its events file;
  * the post-flight report places outages the same way from the ping file, and
    from the Pi's own count of carrier losses on eth0;
  * that count is read from BlueOS's cable-guy service into the laptop row;
  * the parameter refresh reads each log once and then only its new tail.
"""

from __future__ import annotations

import csv
import json
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from rov_flight_ops import blueos, flightlog, laptop, netdiag, nettrace
from rov_flight_ops import flightreport as R
from rov_flight_ops import flightscan as S
from rov_flight_ops import wincounters as W

T0 = 1_792_000_000.0          # a fixed "now", so nothing depends on the clock


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat(
        timespec="milliseconds")


# --------------------------------------------------------------------------
#  placing an outage, live
# --------------------------------------------------------------------------


def _drive(tracer, script, start=T0, period=0.2):
    """Feed the tracer one echo per target per tick, the DVL's 0.1 s later.

    `script` is a list of (pi answered, dvl answered); None skips that echo.
    """
    for i, (pi, dvl) in enumerate(script):
        now = start + i * period
        if pi is not None:
            tracer._echo_result(tracer._vehicle, pi, now)
        if dvl is not None and tracer._dvl is not None:
            tracer._echo_result(tracer._dvl, dvl, now + 0.1)


def _lines(tracer, prefix):
    return [e for e in tracer.events if e.what.startswith(prefix)]


def test_the_pi_going_quiet_while_the_dvl_answers_is_placed_on_the_pi():
    """The 15 October pattern: the Pi silent for 4 s, the DVL streaming on."""
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, True)] * 10 + [(False, True)] * 20
           + [(True, True)] * 10)
    placed = _lines(tracer, "OUTAGE")
    assert [e.what for e in placed] == ["OUTAGE: Pi only"]
    assert "4.0 s" in placed[0].detail
    assert "the DVL answered 20 of 20" in placed[0].detail
    record = tracer.outages[0]
    assert record["target"] == "vehicle" and record["place"] == S.ALONE
    assert record["witness_answered"] == 20 and record["witness_lost"] == 0
    # The vehicle's own transitions are written exactly as before.
    assert [e.what for e in tracer.events] == [
        "ECHO LOST", "echo returned", "OUTAGE: Pi only"]


def test_both_going_quiet_is_the_whole_vehicle_and_not_the_dvl_alone():
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, True)] * 10 + [(False, False)] * 20
           + [(True, True)] * 10)
    whats = [e.what for e in tracer.events]
    assert "OUTAGE: whole vehicle" in whats
    assert "DVL only" not in whats, "the DVL's half of a whole-vehicle outage"
    assert {"DVL echo lost", "DVL echo returned"} <= set(whats)
    vehicle = [o for o in tracer.outages if o["target"] == "vehicle"]
    assert vehicle[0]["place"] == S.BOTH


def test_a_dvl_that_never_answered_cannot_condemn_the_whole_vehicle():
    """A DVL that is off, or never answers ICMP, is no witness at all."""
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, False)] * 10 + [(False, False)] * 20
           + [(True, False)] * 5)
    placed = _lines(tracer, "OUTAGE")
    assert [e.what for e in placed] == ["OUTAGE: unplaced"]
    assert "had not answered" in placed[0].detail
    assert "whole vehicle" not in " ".join(e.what for e in tracer.events)


def test_the_dvl_going_quiet_alone_is_said_once():
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, True)] * 10 + [(True, False)] * 10
           + [(True, True)] * 5)
    whats = [e.what for e in tracer.events]
    assert whats.count("DVL only") == 1
    assert not _lines(tracer, "OUTAGE")
    assert "ECHO LOST" not in whats


def test_one_lost_echo_is_not_an_outage():
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, True)] * 5 + [(False, True)] + [(True, True)] * 5)
    assert not tracer.outages
    assert not _lines(tracer, "OUTAGE")


def test_an_outage_still_open_when_the_trace_stops_is_placed_as_far_as_it_goes():
    tracer = nettrace.Tracer(host="192.168.2.2")
    _drive(tracer, [(True, True)] * 10 + [(False, True)] * 10)
    tracer._close_open_outages()
    placed = _lines(tracer, "OUTAGE")
    assert [e.what for e in placed] == ["OUTAGE: Pi only"]
    assert "still silent" in placed[0].detail
    assert tracer.outages[0]["still_silent_at_stop"] is True


def test_without_a_dvl_nothing_is_placed_and_nothing_else_changes():
    tracer = nettrace.Tracer(host="192.168.2.2", dvl_host=None)
    _drive(tracer, [(True, None)] * 10 + [(False, None)] * 20
           + [(True, None)] * 10)
    assert [e.what for e in tracer.events] == ["ECHO LOST", "echo returned"]
    assert tracer.echoes == 40 and tracer.lost == 20


def test_the_placement_rule_allows_an_echo_either_side_but_no_more():
    assert S.place_outage(19, 1, True) == S.ALONE
    assert S.place_outage(1, 19, True) == S.BOTH
    assert S.place_outage(10, 10, True) == S.MIXED
    assert S.place_outage(20, 0, False) == ""
    assert S.place_outage(0, 0, True) == ""


def test_the_dvl_address_follows_the_capture_and_falls_back_to_the_default():
    asked = iter(["", "10.1.2.3", RuntimeError("boom")])

    def source():
        value = next(asked)
        if isinstance(value, Exception):
            raise value
        return value

    tracer = nettrace.Tracer(host="192.168.2.2", dvl_host=source)
    assert tracer._address(tracer._dvl) == nettrace.DVL_DEFAULT_HOST
    assert tracer._address(tracer._dvl) == "10.1.2.3"
    assert tracer._address(tracer._dvl) == nettrace.DVL_DEFAULT_HOST
    assert tracer._address(tracer._vehicle) == "192.168.2.2"


def test_the_events_and_json_sidecars_carry_the_placement(tmp_path, monkeypatch):
    monkeypatch.setattr(netdiag, "snapshot", lambda *a, **k: {})
    tracer = nettrace.Tracer(host="192.168.2.2", folder=tmp_path,
                             flight_id="t")
    tracer._dvl.host = "192.168.2.95"
    _drive(tracer, [(True, True)] * 10 + [(False, True)] * 20
           + [(True, True)] * 10)
    tracer._write_sidecars()
    events = (tmp_path / "network_events_t.txt").read_text(encoding="utf-8")
    assert "OUTAGE: Pi only" in events
    assert "DVL 192.168.2.95" in events
    payload = json.loads((tmp_path / "network_trace_t.json")
                         .read_text(encoding="utf-8"))
    assert payload["outages"][0]["place"] == S.ALONE
    assert {t["target"] for t in payload["echo_targets"]} == {"vehicle", "dvl"}


# --------------------------------------------------------------------------
#  one ping file, two targets, and the readers that already used it
# --------------------------------------------------------------------------


class _ScriptedEcho:
    """`W.Echo` with the network replaced by a rule about which hosts answer."""

    answers = staticmethod(lambda host: True)

    def __init__(self, host):
        self.host = host

    def open(self):
        return True

    def ping(self, timeout_ms=None):
        return (1.0, 0) if _ScriptedEcho.answers(self.host) else (None, 11010)

    def close(self):
        pass


def _wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_both_echo_threads_share_one_file_and_the_reader_keeps_them_apart(
        tmp_path, monkeypatch):
    monkeypatch.setattr(W, "Echo", _ScriptedEcho)
    monkeypatch.setattr(_ScriptedEcho, "answers",
                        staticmethod(lambda host: host == "10.0.0.95"))
    tracer = nettrace.Tracer(host="10.0.0.2", folder=tmp_path, flight_id="t",
                             echo_period=0.01, dvl_host=lambda: "10.0.0.95")
    assert tracer._open_files()
    tracer._started = time.time()
    tracer._echo_loops = 2
    loops = [threading.Thread(target=tracer._echo_loop, args=(t,))
             for t in (tracer._vehicle, tracer._dvl)]
    for loop in loops:
        loop.start()
    assert _wait_for(lambda: tracer._vehicle.sent >= 20
                     and tracer._dvl.sent >= 20)
    tracer._stop.set()
    for loop in loops:
        loop.join(5)
    assert tracer._ping_fh is None, "the last echo loop out did not close it"

    with tracer.ping_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fh.seek(0)
        header = next(csv.reader(fh))
    assert header == list(nettrace.PING_COLUMNS)
    assert header[:6] == ["timestamp_utc", "elapsed_s", "sequence", "rtt_ms",
                          "status_code", "status"], "original columns moved"
    vehicle = [r for r in rows if r["target"] == "vehicle"]
    dvl = [r for r in rows if r["target"] == "dvl"]
    assert vehicle and dvl and len(vehicle) + len(dvl) == len(rows)
    assert {r["host"] for r in dvl} == {"10.0.0.95"}

    log = S.read_pings(tracer.ping_path)
    # The vehicle's figures are the vehicle's alone: every one of its echoes
    # was lost, and none of the DVL's answered ones leaked into them.
    assert log.sent == len(vehicle) and log.lost == len(vehicle)
    assert len(log.dvl_times) == len(dvl) and all(log.dvl_ok)
    assert log.dvl_host == "10.0.0.95"
    assert log.dvl_times == sorted(log.dvl_times)


def test_a_ping_file_from_before_the_dvl_still_reads_as_it_did(tmp_path):
    path = tmp_path / "network_pings_2026-10-15_103918.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(["timestamp_utc", "elapsed_s", "sequence", "rtt_ms",
                      "status_code", "status"])
        for i in range(50):
            lost = 20 <= i < 40
            out.writerow([_iso(T0 + i * 0.2), i * 0.2, i + 1,
                          "" if lost else 1, 11010 if lost else 0,
                          "request timed out" if lost else "success"])
    log = S.read_pings(path)
    assert log.sent == 50 and log.lost == 20
    assert len(log.outages) == 1
    assert not log.dvl_times and not log.dvl_outages


# --------------------------------------------------------------------------
#  placing an outage, afterwards
# --------------------------------------------------------------------------


def _ping_log(tmp_path, dvl_answers):
    """A ping file with a 4 s Pi outage from T0+20 s, and the DVL as told."""
    path = tmp_path / "network_pings_t.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(nettrace.PING_COLUMNS)
        for i in range(200):
            when = T0 + i * 0.2
            pi_lost = 100 <= i < 120
            out.writerow([_iso(when), i * 0.2, i + 1, "" if pi_lost else 1,
                          11010 if pi_lost else 0, "", "vehicle",
                          "192.168.2.2"])
            ok = dvl_answers(i)
            out.writerow([_iso(when + 0.1), i * 0.2 + 0.1, i + 1,
                          1 if ok else "", 0 if ok else 11010, "", "dvl",
                          "192.168.2.95"])
    return S.read_pings(path)


def test_the_report_places_a_pi_only_outage_from_the_ping_file(tmp_path):
    log = _ping_log(tmp_path, lambda i: True)
    day = S.FlightDay(folder=tmp_path, pings=[log])
    report = R.analyze(day)
    assert len(report.outages) == 1
    located = report.outages[0].located
    assert "the Pi stopped answering while the DVL" in located
    assert "not the tether" in located


def test_the_report_places_a_whole_vehicle_outage_from_the_ping_file(tmp_path):
    log = _ping_log(tmp_path, lambda i: not 100 <= i < 120)
    report = R.analyze(S.FlightDay(folder=tmp_path, pings=[log]))
    assert "the DVL went silent with the Pi" in report.outages[0].located


def _monitor(drops):
    """A monitor session whose Pi carrier count reads `drops(t)` each second."""
    times = [T0 + i for i in range(120)]
    return S.MonitorSession(
        start=times[0], end=times[-1], rows=len(times), times=times,
        columns={"pi_eth_carrier_drops": [drops(t - T0) for t in times]})


def test_a_step_in_the_pis_carrier_count_says_the_link_itself_dropped(tmp_path):
    day = S.FlightDay(folder=tmp_path,
                      monitors=[_monitor(lambda s: 3 if s < 30 else 4)])
    found = R._locate(day, S.Span(T0 + 20, T0 + 24))
    assert "lost carrier" in found


def test_a_first_ever_drop_counts_although_the_csv_reader_sees_booleans(
        tmp_path):
    """The monitor reader turns "0" and "1" into False and True."""
    path = tmp_path / "laptop_monitor_t.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(["timestamp_utc", "pi_eth_carrier_drops"])
        for i in range(120):
            out.writerow([_iso(T0 + i), 0 if i < 30 else 1])
    session = S.read_monitor(path)
    assert session.columns["pi_eth_carrier_drops"][0] is False
    day = S.FlightDay(folder=tmp_path, monitors=[session])
    assert "lost carrier" in R._locate(day, S.Span(T0 + 20, T0 + 24))


def test_an_unchanged_count_read_well_after_says_the_link_stayed_up(tmp_path):
    day = S.FlightDay(folder=tmp_path, monitors=[_monitor(lambda s: 3)])
    found = R._locate(day, S.Span(T0 + 20, T0 + 24))
    assert "kept carrier" in found


def test_an_unchanged_count_read_too_soon_after_says_nothing(tmp_path):
    """cable-guy caches for ten seconds: a read just after may predate a flap."""
    day = S.FlightDay(folder=tmp_path, monitors=[_monitor(
        lambda s: 3 if s <= 28 else None)])
    assert R._locate(day, S.Span(T0 + 20, T0 + 24)) == ""


# --------------------------------------------------------------------------
#  the Pi's carrier, from cable-guy, into the laptop row
# --------------------------------------------------------------------------

#: The shape cable-guy's GET /v1.0/ethernet returns (BlueOS 1.0 onward):
#: `info.connected` is the kernel's carrier, `number_of_disconnections` its
#: carrier_down_count.
CABLE_GUY = [
    {"name": "eth0", "addresses": [{"ip": "192.168.2.2", "mode": "server"}],
     "info": {"connected": True, "number_of_disconnections": 3,
              "priority": 100}},
    {"name": "usb0", "addresses": [], "info": None},
]


class _Answer:
    def __init__(self, body="", ok=True, status=200):
        self.ok, self.body, self.status, self.raw = ok, body, status, b""


def test_cable_guy_is_read_for_carrier_and_its_drop_count(monkeypatch):
    asked = []

    def fake_get(url, **kw):
        asked.append(url)
        if ":9090/v1.0/ethernet" in url:
            return _Answer(json.dumps(CABLE_GUY))
        return _Answer(ok=False, status=404)

    monkeypatch.setattr(blueos, "_get", fake_get)
    blueos._LINK_STATE_FOUND.clear()
    found = blueos.read_link_state("192.168.2.2")
    assert found == {"eth0": {"carrier": True, "carrier_drops": 3}}
    assert all(u.startswith("http://192.168.2.2:9090/") for u in asked)


def test_cable_guy_through_the_web_server_when_its_port_is_closed(monkeypatch):
    asked = []

    def fake_get(url, **kw):
        asked.append(url)
        if url.endswith(":80/cable-guy/v1.0/ethernet"):
            return _Answer(json.dumps(CABLE_GUY))
        return _Answer(ok=False)

    monkeypatch.setattr(blueos, "_get", fake_get)
    blueos._LINK_STATE_FOUND.clear()
    assert blueos.read_link_state("192.168.2.2")["eth0"]["carrier_drops"] == 3
    asked.clear()
    blueos.read_link_state("192.168.2.2")
    assert len(asked) == 1, "the route that worked was not remembered"


def test_no_cable_guy_is_an_empty_answer_not_an_error(monkeypatch):
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _Answer(ok=False))
    blueos._LINK_STATE_FOUND.clear()
    assert blueos.read_link_state("192.168.2.2") == {}


def test_the_tether_reader_carries_the_carrier_count_into_the_row(monkeypatch):
    rec = flightlog.FlightRecorder(host="192.168.2.2")
    vehicle = laptop.VehicleState()
    monkeypatch.setattr(rec, "_vehicle", lambda: vehicle)
    monkeypatch.setattr(blueos, "read_interfaces", lambda *a, **k: {
        "eth0": {"rx_bytes": 1000, "rx_errors": 0}})
    monkeypatch.setattr(blueos, "read_link_state", lambda *a, **k: {
        "eth0": {"carrier": True, "carrier_drops": 7}})
    monkeypatch.setattr(blueos, "read_tether", lambda *a, **k: {})
    monkeypatch.setattr(blueos, "read_tether_devices", lambda *a, **k: {})
    rec._read_tether()
    assert vehicle.eth_carrier is True and vehicle.eth_carrier_drops == 7

    # The service stops answering: held forward for a few polls, then unknown.
    monkeypatch.setattr(blueos, "read_link_state", lambda *a, **k: {})
    rec._read_tether()
    assert vehicle.eth_carrier_drops == 7
    rec._link_read_at -= flightlog.TETHER_POLL_S * flightlog.STALE_AFTER_POLLS + 1
    rec._read_tether()
    assert vehicle.eth_carrier is None and vehicle.eth_carrier_drops is None


def test_the_carrier_columns_sit_beside_the_pis_byte_count():
    cols = list(laptop.COLUMNS)
    at = cols.index("pi_eth_rx_bytes")
    assert cols[at:at + 4] == ["pi_eth_rx_bytes", "pi_eth_rx_errors",
                               "pi_eth_carrier", "pi_eth_carrier_drops"]
    assert "pi_eth_carrier_drops" in laptop.GROUPS["Tether"]


# --------------------------------------------------------------------------
#  the parameter refresh reads a log once, then only what it gained
# --------------------------------------------------------------------------

PARM_TYPE = 64


def _fmt(typ: int, length: int, name: str, fmt: str, cols: str) -> bytes:
    return b"\xa3\x95\x80" + struct.pack(
        "<BB4s16s64s", typ, length, name.encode(), fmt.encode(), cols.encode())


def _parm(name: str, value: float, t: int = 0) -> bytes:
    return b"\xa3\x95" + bytes([PARM_TYPE]) + struct.pack(
        "<Q16sff", t, name.encode(), value, 0.0)


def _noise(n: int) -> bytes:
    """Other records, some carrying a PARM-looking header in their payload."""
    record = (b"\xa3\x95\x46" + struct.pack("<Q", 1)
              + b"\xa3\x95" + bytes([PARM_TYPE]) + b"\x01\x02")
    return record * n


HEAD = (_fmt(0x80, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
        + _fmt(PARM_TYPE, 35, "PARM", "QNff", "TimeUS,Name,Value,Default")
        + _fmt(0x46, 16, "NOIS", "QBBBBB", "TimeUS,A,B,C,D,E")
        + _parm("EK3_SRC1_POSXY", 6.0) + _parm("AHRS_EKF_TYPE", 3.0)
        + _parm("STAT_RUNTIME", 100.0))

#: What the fake File Browser serves: its logs, whether it honours Range, and
#: every range it was asked for with how many bytes it sent back.
FB = {"logs": {}, "honour_range": True, "sent": []}


class _FileBrowser(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code, body, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/login"):
            return self._send(200, b"token")
        if self.path.startswith("/api/resources"):
            items = [{"name": n, "size": len(b), "isDir": False}
                     for n, b in FB["logs"].items()]
            return self._send(200, json.dumps({"items": items}).encode())
        if self.path.startswith("/api/raw"):
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            data = bytes(FB["logs"][name])
            rng = self.headers.get("Range")
            if not rng or not FB["honour_range"]:
                FB["sent"].append((None, len(data)))
                return self._send(200, data)
            lo, _, hi = rng.split("=")[1].partition("-")
            lo, hi = int(lo), min(int(hi), len(data) - 1)
            FB["sent"].append((lo, hi + 1 - lo))
            return self._send(206, data[lo:hi + 1], [
                ("Content-Range", f"bytes {lo}-{hi}/{len(data)}")])
        self._send(404, b"{}")


@pytest.fixture()
def file_browser():
    FB.update(logs={"00000025.BIN": bytearray(HEAD + _noise(2000))},
              honour_range=True, sent=[])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FileBrowser)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_the_first_read_matches_reading_the_log_whole(file_browser):
    follower = blueos.ParameterFollower(file_browser)
    got = follower.read()
    whole, _names = blueos.read_parameters_now(file_browser)
    assert got == whole
    assert got["EK3_SRC1_POSXY"] == 6.0 and len(got) == 3


def test_a_log_that_has_not_grown_is_not_downloaded_again(file_browser):
    follower = blueos.ParameterFollower(file_browser)
    first = follower.read()
    FB["sent"].clear()
    assert follower.read() == first
    assert follower.read() == first
    assert FB["sent"] == [], "an unchanged log was fetched again"


def test_a_grown_log_moves_only_its_tail_and_a_change_in_it_is_seen(
        file_browser):
    log = FB["logs"]["00000025.BIN"]
    follower = blueos.ParameterFollower(file_browser)
    follower.read()
    before = len(log)
    added = _noise(500) + _parm("EK3_SRC1_POSXY", 3.0, t=9) + _noise(500)
    log.extend(added)
    FB["sent"].clear()
    got = follower.read()
    assert got["EK3_SRC1_POSXY"] == 3.0
    [(start, length)] = FB["sent"]
    assert start == before - blueos.TAIL_OVERLAP
    assert length == len(added) + blueos.TAIL_OVERLAP
    assert got == blueos.read_parameters_now(file_browser)[0]


def test_a_record_cut_by_the_end_of_one_read_is_read_whole_by_the_next(
        file_browser):
    log = FB["logs"]["00000025.BIN"]
    follower = blueos.ParameterFollower(file_browser)
    follower.read()
    record = _parm("AHRS_EKF_TYPE", 2.0, t=5)
    log.extend(record[:20])                 # the autopilot is mid-write
    assert follower.read()["AHRS_EKF_TYPE"] == 3.0
    log.extend(record[20:] + _noise(10))
    assert follower.read()["AHRS_EKF_TYPE"] == 2.0


def test_a_new_log_is_read_from_its_start(file_browser):
    follower = blueos.ParameterFollower(file_browser)
    follower.read()
    FB["logs"]["00000026.BIN"] = bytearray(
        HEAD.replace(_parm("STAT_RUNTIME", 100.0), _parm("STAT_RUNTIME", 200.0))
        + _noise(10))
    FB["sent"].clear()
    got = follower.read()
    assert follower.log == "00000026.BIN"
    assert got["STAT_RUNTIME"] == 200.0
    assert FB["sent"][0][0] == 0


def test_a_server_that_ignores_range_is_not_asked_for_the_whole_log_again(
        file_browser):
    follower = blueos.ParameterFollower(file_browser)
    assert follower.read()
    FB["honour_range"] = False
    FB["logs"]["00000025.BIN"].extend(_noise(100))
    assert follower.read() == {}
    assert "Range" in follower.problem
    FB["logs"]["00000025.BIN"].extend(_noise(100))
    FB["sent"].clear()
    assert follower.read() == {}
    assert FB["sent"] == [], "the whole log was pulled again"


def test_the_app_hands_the_trace_its_dvl_captures_address():
    from rov_flight_ops.gui.app import App

    capture = SimpleNamespace(address=SimpleNamespace(host="192.168.2.96"))
    app = SimpleNamespace(dvl=SimpleNamespace(capture=capture), settings={})
    assert App.dvl_address(app) == "192.168.2.96"
    app = SimpleNamespace(dvl=None, settings={"dvl_host": "192.168.2.97:8080"})
    assert App.dvl_address(app) == "192.168.2.97"
    app = SimpleNamespace(dvl=None, settings={})
    assert App.dvl_address(app) == ""
