"""
A DVL capture, end to end, against the simulated DVL.

What these hold, in order of how much they matter:

* the raw file is **every byte the DVL sent**, in order, across reconnections
  and however the bytes were split -- the guarantee everything else rests on;
* every line is accounted for in the index, and every report has its row;
* gaps are put on the side of the link they were made on;
* nothing is written without a flight folder, and nothing on disk is ever
  replaced;
* a failure is shown and counted, never raised.

Each test runs a real capture for a second or two, so these are the slowest
tests in the DVL suite. The simulator reports at 20 Hz to keep them short.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from rov_flight_ops.dvl import capture as CAP
from rov_flight_ops.dvl import protocol as P
from rov_flight_ops.dvl.recorder import DvlRecorder
from rov_flight_ops.dvl.simulator import SimulatedDvl


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    """The disk guard is tested on its own; elsewhere it must not interfere
    (the laptop these were written on had half a gigabyte free)."""
    monkeypatch.setattr(CAP, "DISK_LOW_BYTES", 0)
    monkeypatch.setattr(CAP, "DISK_CRITICAL_BYTES", 0)


@pytest.fixture
def sim():
    s = SimulatedDvl(rate_hz=20, position_hz=10, ws_hz=20).start()
    yield s
    s.stop()


def wait_for(cond, timeout=8.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def start(sim, folder, **kw) -> CAP.DvlCapture:
    cap = CAP.DvlCapture(folder=folder, dvl_override=sim.address,
                         tcp_port=sim.tcp_port, vehicle=kw.pop("vehicle", False),
                         snapshot_hz=kw.pop("snapshot_hz", 2.0), **kw)
    cap.start()
    assert wait_for(lambda: cap.live.snapshot(history=False).tcp_connected), \
        "the capture never connected to the simulated DVL"
    return cap


def assert_every_byte(raw: bytes, sim) -> None:
    """The raw file is exactly what arrived: what the DVL sent, in order, up to
    whatever was still in flight when the capture stopped -- a line or two."""
    sent = b"".join(bytes(v) for v in sim.sent.values())
    assert sent.startswith(raw), "the raw file must be what was sent, in order"
    assert len(sent) - len(raw) < 4000, "only the last line or two may be missing"
    assert len(raw) > 5000


def files(cap) -> dict[str, Path]:
    return {k: cap.folder / f"{cap.stem}{suffix}" for k, (suffix, _) in CAP.FILES.items()}


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------
#  every byte
# --------------------------------------------------------------------------


def test_the_raw_file_is_every_byte_the_dvl_sent(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(1.0)
    assert cap.stop()
    assert_every_byte(files(cap)["tcp_raw"].read_bytes(), sim)


def test_lines_split_across_writes_are_kept_whole(sim, tmp_path):
    sim.split_writes = True
    cap = start(sim, tmp_path)
    time.sleep(1.0)
    cap.stop()
    assert_every_byte(files(cap)["tcp_raw"].read_bytes(), sim)
    index = rows(files(cap)["tcp_index"])
    straddled = [r for r in index if r["chunk"] != r["first_chunk"]]
    assert straddled, "the simulator should have split some lines across reads"
    assert not [r for r in index if r["kind"] == "unparsed"]
    # A line the capture stopped in the middle of is kept, and named so.
    partial = [r for r in index if r["kind"] == "partial"]
    assert len(partial) <= 1 and (not partial or partial[0] is index[-1])


def test_every_byte_survives_a_reconnection(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(0.6)
    sim.disconnect_clients()
    assert wait_for(lambda: len(cap.connections) >= 2
                    and cap.live.snapshot(history=False).tcp_connected)
    time.sleep(0.6)
    cap.stop()
    raw = files(cap)["tcp_raw"].read_bytes()
    index = rows(files(cap)["tcp_index"])
    # The raw file is each connection's bytes, one after the other; the index
    # says where each connection's begin. Each is what that connection sent.
    sent = list(sim.sent.values())
    conns = sorted({int(r["conn"]) for r in index})
    assert len(conns) >= 2 and len(sent) >= 2
    for k in conns:
        mine = [r for r in index if int(r["conn"]) == k]
        start_, end = int(mine[0]["offset"]), int(mine[-1]["offset"]) + int(mine[-1]["length"])
        assert bytes(sent[k - 1]).startswith(raw[start_:end])
    assert sum(int(r["length"]) for r in index) == len(raw)
    events = files(cap)["events"].read_text(encoding="utf-8")
    assert "TCP disconnected" in events and "connection 2" in events


def test_the_index_finds_every_line_in_the_raw_file(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(1.0)
    cap.stop()
    raw = files(cap)["tcp_raw"].read_bytes()
    index = rows(files(cap)["tcp_index"])
    assert [int(r["line_no"]) for r in index] == list(range(1, len(index) + 1))
    pos = 0
    for r in index:
        off, n = int(r["offset"]), int(r["length"])
        assert off == pos, "lines must tile the raw file with no gap or overlap"
        piece = raw[off:off + n]
        pos += n
        if r["kind"] == "partial":
            assert r is index[-1] and r["terminator"] == ""
            continue
        assert piece.endswith(b"\r\n") and r["terminator"] == "CRLF"
        assert P.kind_of(json.loads(piece)) == r["kind"]
    assert pos == len(raw)


# --------------------------------------------------------------------------
#  the tables
# --------------------------------------------------------------------------


def test_every_report_has_exactly_one_row(sim, tmp_path):
    cap = start(sim, tmp_path, flight_id=lambda: "2026-10-01_120000")
    time.sleep(1.0)
    cap.stop()
    index = rows(files(cap)["tcp_index"])
    vel = rows(files(cap)["velocity"])
    pos = rows(files(cap)["deadreckoning"])
    assert len(vel) == sum(r["kind"] == "velocity" for r in index) > 10
    assert len(pos) == sum(r["kind"] == "position_local" for r in index) > 5
    with files(cap)["velocity"].open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == list(CAP.VELOCITY_COLUMNS)
    assert {r["flight_id"] for r in vel} == {"2026-10-01_120000"}
    # Joined back to the index by line number.
    kinds = {r["line_no"]: r["kind"] for r in index}
    assert all(kinds[r["line_no"]] == "velocity" for r in vel)
    # The report's own values, untouched.
    first = json.loads(files(cap)["tcp_raw"].read_bytes().split(b"\r\n")[
        int(vel[0]["line_no"]) - 1])
    assert float(vel[0]["altitude"]) == first["altitude"]
    assert int(vel[0]["time_of_validity_us"]) == first["time_of_validity"]


def test_only_read_only_commands_are_sent_one_at_a_time(sim, tmp_path):
    cap = start(sim, tmp_path)
    assert wait_for(lambda: len(cap.command_outcomes) >= 4)
    cap.stop()
    sent = [r[1] for r in sim.requests if r[0] == "tcp"]
    assert sent, "the capture should have asked its read-only questions"
    for data in sent:
        assert data.count(b"\n") == 1, "one command per write"
        assert json.loads(data)["command"] in P.READ_ONLY_COMMANDS
    answered = rows(files(cap)["commands"])
    assert {r["command"] for r in answered} >= {"get_config", "get_version_info",
                                                "get_time_ntp", "get_time_status"}
    assert all(r["outcome"] == "answered" for r in answered)
    assert cap.config_first.get("speed_of_sound") == 1475.0


def test_an_older_dvl_that_does_not_know_a_command_is_logged_not_fatal(tmp_path):
    old = SimulatedDvl(rate_hz=20, version="2.6.4", json_format="json_v3.1").start()
    try:
        cap = start(old, tmp_path)
        assert wait_for(lambda: "get_version_info" in cap.command_outcomes)
        time.sleep(0.3)
        cap.stop()
    finally:
        old.stop()
    outcome = cap.command_outcomes["get_version_info"]
    assert outcome["outcome"] == "answered" and outcome["success"] is False
    assert not cap.problem
    assert rows(files(cap)["velocity"])


def test_the_web_stream_status_and_snapshots_are_captured(sim, tmp_path):
    cap = start(sim, tmp_path, snapshot_hz=5.0)
    assert wait_for(lambda: cap.live.snapshot(history=False).rates.get("echo", 0) > 0)
    time.sleep(1.2)
    cap.stop()
    f = files(cap)
    ws = [json.loads(line) for line in f["ws_raw"].read_text(encoding="utf-8").splitlines()]
    assert ws and all("text" in m for m in ws)
    assert {json.loads(m["text"])["channel"] for m in ws} >= {"velocity", "roll_pitch_yaw"}
    assert rows(f["ws_velocity"]) and rows(f["ws_motion"])
    status = rows(f["status"])
    # Any row, not the last: the last poll may land after this capture's own
    # connection has closed, and then rightly counts no clients.
    assert {r["temperature_c"] for r in status} == {"31.5"}
    assert max(int(r["json_clients"]) for r in status) >= 1
    assert {r["cfg_periodic_cycling_enabled"] for r in status} == {"0"}
    http = [json.loads(line) for line in f["http_raw"].read_text(encoding="utf-8").splitlines()]
    about = next(r for r in http if r["path"] == "/api/v1/about")
    assert json.loads(about["body"])["product_name"] == "DVL A50"
    echo = [json.loads(line) for line in f["echo"].read_text(encoding="utf-8").splitlines()]
    body = json.loads(echo[0]["body"])
    assert len(body["data"]) == 1024 and len(body["data"][0]) == 4
    spec = json.loads(f["spectrum"].read_text(encoding="utf-8").splitlines()[0])
    assert len(json.loads(spec["body"])["data"]) == 128
    # The DVL was only ever read.
    assert {m for kind, m, *_ in sim.requests if kind == "http"} == {"GET"}


def test_snapshots_can_be_turned_off(sim, tmp_path):
    cap = start(sim, tmp_path, snapshot_hz=10.0)
    assert wait_for(lambda: cap.live.snapshot(history=False).rates.get("echo", 0) > 0)
    cap.set_snapshot_hz(0)
    time.sleep(0.4)
    before = files(cap)["echo"].stat().st_size
    time.sleep(0.8)
    cap.stop()
    assert files(cap)["echo"].stat().st_size == before
    assert [r["hz"] for r in cap.snapshot_rates] == [10.0, 0.0]


# --------------------------------------------------------------------------
#  gaps, and where they came from
# --------------------------------------------------------------------------


def test_each_kind_of_gap_is_put_on_its_own_side(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(1.0)
    sim.pause(0.8)          # the DVL makes nothing
    time.sleep(1.5)
    sim.drop(4)             # made, never sent
    time.sleep(1.0)
    sim.stall(0.8)          # made on time, delivered late
    time.sleep(1.5)
    cap.stop()
    gaps = cap.cadence.stats.gaps
    assert gaps["dvl_quiet"] >= 1
    assert gaps["missing_reports"] >= 1
    assert gaps["delivery_stall"] >= 1
    assert cap.cadence.stats.missing_estimate >= 3
    events = files(cap)["events"].read_text(encoding="utf-8")
    for kind in ("dvl_quiet", "missing_reports", "delivery_stall"):
        assert f"gap: {kind}" in events
    record = json.loads((cap.folder / f"{cap.stem}.json").read_text(encoding="utf-8"))
    assert record["cadence"]["gaps"]["missing_reports"] >= 1


def test_an_invalid_stretch_is_counted_and_is_not_a_gap(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(0.8)
    sim.invalid(0.6)
    time.sleep(1.2)
    cap.stop()
    st = cap.cadence.stats
    assert st.invalid > 0 and st.invalid_spans == 1
    vel = rows(files(cap)["velocity"])
    assert {r["velocity_valid"] for r in vel} == {"0", "1"}
    # No gap during the stretch. (A busy test machine can still make an
    # unrelated one elsewhere in the run, which is not what this tests.)
    bad = [float(r["rx_unix"]) for r in vel if r["velocity_valid"] == "0"]
    lo, hi = min(bad) - 0.05, max(bad) + 0.15
    during = [g for g in cap.live.snapshot(history=False).gaps if lo <= g.ended_unix <= hi]
    assert during == []
    assert "the stock extension drops these" in files(cap)["events"].read_text(encoding="utf-8")


def test_periodic_cycling_being_on_is_called_out(tmp_path):
    cycling = SimulatedDvl(rate_hz=20, periodic_cycling=True).start()
    try:
        cap = start(cycling, tmp_path)
        assert wait_for(lambda: cap.config_first)
        time.sleep(0.5)
        cap.stop()
    finally:
        cycling.stop()
    assert "periodic cycling is ON" in files(cap)["events"].read_text(encoding="utf-8")


# --------------------------------------------------------------------------
#  where things are written, and when they are not
# --------------------------------------------------------------------------


def test_a_live_view_writes_nothing(sim, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cap = start(sim, None)
    time.sleep(0.6)
    cap.stop()
    assert list(tmp_path.iterdir()) == []
    snap = cap.live.snapshot(history=False)
    assert snap.lines_in > 0 and not snap.capturing


def test_the_record_says_what_was_captured(sim, tmp_path):
    cap = start(sim, tmp_path)
    time.sleep(0.8)
    cap.stop()
    record = json.loads((cap.folder / f"{cap.stem}.json").read_text(encoding="utf-8"))
    assert record["schema"] == CAP.CAPTURE_SCHEMA
    assert record["state"] == "closed" and record["ended_utc"]
    # A typed web port makes the address a URL, so it can be pasted into a browser.
    assert record["dvl_address"] == f"http://{sim.address}"
    raw_size = files(cap)["tcp_raw"].stat().st_size
    assert record["tcp_bytes"] == raw_size
    assert record["files"]["tcp_raw"]["bytes_written"] == raw_size
    # vehicle=False: the extension and mavlink2rest files are never opened.
    assert set(record["files"]) == set(CAP.FILES) - {"vehicle_raw", "mavlink"}
    for meta in record["files"].values():
        assert meta["file"].startswith(cap.stem)
        assert (cap.folder / meta["file"]).is_file()
    assert set(record["read_only_commands"]) == set(P.READ_ONLY_COMMANDS)
    assert not record["stuck_threads"]


def test_a_second_capture_in_the_same_second_gets_its_own_files(tmp_path):
    when = time.time()
    first = CAP.unique_capture_id(tmp_path, when)
    (tmp_path / f"dvl_{first}_tcp.jsonl").write_bytes(b"")
    second = CAP.unique_capture_id(tmp_path, when)
    assert second != first and second.startswith(first)


def test_nothing_is_ever_replaced(sim, tmp_path, monkeypatch):
    stamp = "2026-10-01_000000"
    monkeypatch.setattr(CAP, "unique_capture_id", lambda folder, when=None: stamp)
    (tmp_path / f"dvl_{stamp}_velocity.csv").write_text("precious\n")
    cap = start(sim, tmp_path)
    time.sleep(0.3)
    cap.stop()
    assert (tmp_path / f"dvl_{stamp}_velocity.csv").read_text() == "precious\n"
    assert cap.problem.startswith(f"could not create dvl_{stamp}_velocity.csv")
    # The rest of the capture still ran and wrote its own files.
    assert (tmp_path / f"dvl_{stamp}_tcp.jsonl").stat().st_size > 0


def test_a_full_disk_stops_writing_and_says_so(sim, tmp_path, monkeypatch):
    monkeypatch.setattr(CAP, "DISK_CRITICAL_BYTES", 1 << 62)
    cap = start(sim, tmp_path)
    time.sleep(0.5)
    cap.stop()
    assert cap.problem.startswith("DVL CAPTURE STOPPED WRITING")
    assert files(cap)["tcp_raw"].stat().st_size == 0
    record = json.loads((cap.folder / f"{cap.stem}.json").read_text(encoding="utf-8"))
    assert record["files"]["tcp_raw"]["dropped_disk_full"] > 0


def test_a_failed_write_is_shown_counted_and_cleared(tmp_path):
    cap = CAP.DvlCapture(folder=tmp_path, vehicle=False)
    cap.stem = "dvl_test"

    class Flaky:
        def __init__(self):
            self.fail = True

        def write(self, data):
            if self.fail:
                raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def fileno(self):
            raise OSError("no fileno")

        def close(self):
            pass

    sink = CAP._Sink(cap, "tcp_raw", binary=True)
    sink.fh.close()
    sink.fh = Flaky()
    assert sink.write(b"abc") is False
    assert cap.problem.startswith("RECORDING FAILED") and sink.failed == 1
    sink.fh.fail = False
    assert sink.write(b"abc") is True
    assert cap.problem == ""
    assert any("write(s) were lost" in n for n in cap.notes)


def test_long_paths_get_the_extended_prefix_on_windows():
    deep = "C:\\" + "\\".join(["folder_with_a_long_name"] * 12) + "\\dvl.csv"
    out = CAP.os_path(deep)
    if os.name == "nt":
        assert out.startswith("\\\\?\\") and out.endswith("dvl.csv")
    else:
        assert out == deep
    assert CAP.os_path("C:\\short.csv") == "C:\\short.csv"


# --------------------------------------------------------------------------
#  the vehicle side
# --------------------------------------------------------------------------


@pytest.fixture
def extension():
    """A BlueOS DVL extension's /get_status, saying where the DVL is."""
    state = {"hostname": "127.0.0.1"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"status": "Running", "enabled": True,
                               "orientation": 1, "hostname": state["hostname"],
                               "origin": [47.6, -122.3], "rangefinder": True,
                               "should_send": "POSITION_ESTIMATE"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], state
    srv.shutdown()
    srv.server_close()


def test_the_dvls_address_comes_from_the_extension_when_none_is_typed(
        sim, tmp_path, extension):
    port, _state = extension
    cap = CAP.DvlCapture(folder=tmp_path, vehicle_host="127.0.0.1",
                         tcp_port=sim.tcp_port, vehicle=True, snapshot_hz=0)
    cap.extension = {"port": port}
    cap.start()
    try:
        assert wait_for(lambda: cap.address is not None, 10)
        assert cap.address.host == "127.0.0.1"
        assert cap.address_source == "the BlueOS DVL extension"
        assert wait_for(lambda: cap.live.snapshot(history=False).tcp_connected, 10)
    finally:
        cap.stop()
    vehicle = files(cap)["vehicle_raw"].read_text(encoding="utf-8").splitlines()
    assert json.loads(json.loads(vehicle[0])["body"])["should_send"] == "POSITION_ESTIMATE"
    assert cap.extension["should_send"] == "POSITION_ESTIMATE"


# --------------------------------------------------------------------------
#  the application's handle on captures
# --------------------------------------------------------------------------


def test_choosing_another_folder_closes_one_capture_and_opens_the_next(sim, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    rec = DvlRecorder(dvl_host=sim.address, vehicle=False, snapshot_hz=0)
    try:
        assert rec.use_folder(a).wait(10)
        first = rec.capture
        first.tcp_port = sim.tcp_port
        assert wait_for(lambda: rec.live.snapshot(history=False).tcp_connected, 10)
        assert rec.use_folder(b).wait(15)
        second = rec.capture
        assert second is not first and second.folder == b
        assert not first.running
        record = json.loads((a / f"{first.stem}.json").read_text(encoding="utf-8"))
        assert record["state"] == "closed"
        assert rec.ensure_running(b) is None, "a running capture is left alone"
    finally:
        assert rec.shutdown()
    assert not rec.running
