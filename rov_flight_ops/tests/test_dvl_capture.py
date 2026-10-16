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


def wait_for_data(cap, *, lines: bool = False) -> None:
    """Until bytes -- or whole lines -- have arrived from the simulated DVL.

    `start` waits only for the connection. On a busy CI runner the first
    report can take longer than a short fixed sleep to arrive, and a test that
    then looks for data finds none. The capture counts a chunk only after its
    raw bytes have gone to the file, so this also means there is something to
    have written.
    """
    if lines:
        arrived = lambda: cap.live.snapshot(history=False).lines_in > 0  # noqa: E731
    else:
        arrived = lambda: cap.live.snapshot(history=False).bytes_in > 0  # noqa: E731
    assert wait_for(arrived), "nothing ever arrived from the simulated DVL"


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


def test_the_dvls_clock_is_read_over_tcp_when_the_web_api_has_none(tmp_path, monkeypatch):
    """Software 2.7.2 answers /api/v1/time with a 404, so on 15 October 2026
    every clock column was blank -- while the TCP get_time_status answer,
    in _commands.csv all along, said the DVL was 871 days behind."""
    monkeypatch.setattr(CAP, "STATUS_PERIOD_S", 0.25)
    behind_s = -871 * 86400.0
    dvl = SimulatedDvl(rate_hz=20, clock_offset_s=behind_s).start()
    try:
        cap = start(dvl, tmp_path)
        assert wait_for(lambda: cap.live.snapshot(history=False).status
                        .get("dvl_clock_utc")), "no status row ever had the DVL's clock"
        cap.stop()
    finally:
        dvl.stop()
    status = rows(files(cap)["status"])
    timed = [r for r in status if r["dvl_clock_utc"]]
    r = timed[-1]
    assert r["clock_source"] == "TCP get_time_status" and r["clock_read_utc"]
    # Whole seconds: within the round trip and the second it was cut to.
    assert float(r["dvl_clock_minus_laptop_ms"]) == pytest.approx(behind_s * 1000, abs=2000)
    assert 1000 <= float(r["clock_uncertainty_ms"]) < 2000
    assert r["ntp_synchronized"] == "0" and r["ntp_server"] == "auto"
    assert any("/api/v1/time: HTTP 404" in row["errors"] for row in status)
    record = json.loads((cap.folder / f"{cap.stem}.json").read_text(encoding="utf-8"))
    first = record["time_at_start"]
    assert first["source"] == "TCP get_time_status"
    assert first["answer"]["ntp_synced"] is False
    assert first["dvl_clock_minus_laptop_ms"] == pytest.approx(behind_s * 1000, abs=2000)
    assert "871.0 days behind this laptop" in files(cap)["events"].read_text(encoding="utf-8")


def test_the_web_apis_clock_is_used_when_there_is_one(tmp_path, monkeypatch):
    monkeypatch.setattr(CAP, "STATUS_PERIOD_S", 0.25)
    dvl = SimulatedDvl(rate_hz=20, web_time=True).start()
    try:
        cap = start(dvl, tmp_path)
        assert wait_for(lambda: cap.live.snapshot(history=False).status
                        .get("clock_source") == "web API /api/v1/time")
        cap.stop()
    finally:
        dvl.stop()
    r = [r for r in rows(files(cap)["status"]) if r["dvl_clock_utc"]][-1]
    assert r["clock_source"] == "web API /api/v1/time"
    # Nanoseconds, so the round trip is the whole uncertainty.
    assert abs(float(r["dvl_clock_minus_laptop_ms"])) < 1000
    assert float(r["clock_uncertainty_ms"]) < 1000


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
    # The status poll runs every STATUS_PERIOD_S from the moment the capture
    # starts, so its first poll can land before the DVL has registered this
    # capture's TCP connection, and a fixed sleep then decided whether a
    # second poll got in before the stop -- about one run in five did not.
    # Wait for a poll that saw the connection instead.
    assert wait_for(lambda: (cap.live.snapshot(history=False).status
                             .get("json_clients") or 0) >= 1,
                    timeout=3 * CAP.STATUS_PERIOD_S), \
        "no status poll ever saw this capture's TCP connection"
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


def test_a_silence_with_the_acoustics_off_is_not_a_fault(sim, tmp_path, monkeypatch):
    """15 October 2026: the acoustics were switched off for 76 s, and capture
    2026-10-15_092531 logged a 76 s delivery stall and then "~291 report(s)
    made but not received". None were made. The simulator reproduces the
    A50's stale time of validity on the first report after."""
    monkeypatch.setattr(CAP, "STATUS_PERIOD_S", 0.25)
    cap = start(sim, tmp_path)
    assert wait_for(lambda: cap.cadence.median_ms is not None)
    off_at = time.time()
    sim.acoustics_off(1.5)
    st = cap.cadence.stats
    assert wait_for(lambda: st.gaps["acoustics_off"] >= 1), \
        "the silence was never put down to the acoustics"
    after = st.reports
    # The report after the stale one, whose time of validity jumps the gap.
    assert wait_for(lambda: st.reports >= after + 3)
    # And a status poll that saw them back on: a busy machine can lag it.
    assert wait_for(lambda: st.acoustics_off and st.acoustics_off[-1]["on_utc"])
    cap.stop()
    # Nothing else in that stretch. (A busy test machine can still make an
    # unrelated gap elsewhere in the run, which is not what this tests.)
    during = [g.kind for g in cap.live.snapshot(history=False).gaps
              if off_at <= g.ended_unix <= off_at + 3.0]
    assert during == ["acoustics_off"]
    record = json.loads((cap.folder / f"{cap.stem}.json").read_text(encoding="utf-8"))
    (period,) = record["cadence"]["acoustics_off_periods"]
    assert period["off_utc"] and period["on_utc"]
    events = files(cap)["events"].read_text(encoding="utf-8")
    assert "gap: acoustics_off" in events and "acoustics were switched off" in events


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
    wait_for_data(cap, lines=True)
    time.sleep(0.3)                  # long enough to write, if it were going to
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
    wait_for_data(cap)
    cap.stop()
    assert (tmp_path / f"dvl_{stamp}_velocity.csv").read_text() == "precious\n"
    assert cap.problem.startswith(f"could not create dvl_{stamp}_velocity.csv")
    # The rest of the capture still ran and wrote its own files.
    assert (tmp_path / f"dvl_{stamp}_tcp.jsonl").stat().st_size > 0


def test_a_full_disk_stops_writing_and_says_so(sim, tmp_path, monkeypatch):
    monkeypatch.setattr(CAP, "DISK_CRITICAL_BYTES", 1 << 62)
    cap = start(sim, tmp_path)
    wait_for_data(cap)
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


class FakeMavlink2Rest:
    """mavlink2rest's message reads, each answered as told.

    ``count`` a message flowing; ``empty`` HTTP 200 with no body, which is how
    BlueOS 1.4.6 says it has never seen one; ``garbled`` a body that is not
    JSON; ``silent`` no answer at all.
    """

    def __init__(self, how: dict[str, str]):
        self.how = how
        self.asked: list[str] = []
        self.counter = 100

    def __call__(self, url, *, timeout=4.0):
        from rov_flight_ops.nav import mav2rest
        parts = url.split("/")
        key = f"{parts[-5]}/{parts[-3]}/{parts[-1]}"
        self.asked.append(key)
        how = self.how.get(key, "count")
        if how == "empty":
            return mav2rest.Answer(True, 200, "")
        if how == "garbled":
            return mav2rest.Answer(True, 200, "<html>Bad Gateway</html>")
        if how == "silent":
            return mav2rest.Answer(False, None, error="timed out")
        self.counter += 1
        return mav2rest.Answer(True, 200, json.dumps({
            "message": {"type": parts[-1]},
            "status": {"time": {"counter": self.counter, "frequency": 5.0,
                                "last_update": "2026-10-15T16:30:00Z"}}}))


def one_vehicle_cycle(tmp_path, monkeypatch, how: dict[str, str]):
    """A capture's first round of vehicle reads, against a fake mavlink2rest."""
    from rov_flight_ops.nav import mav2rest
    fake = FakeMavlink2Rest(how)
    monkeypatch.setattr(mav2rest, "_get", fake)
    monkeypatch.setattr(CAP.DvlCapture, "_read_extension",
                        lambda self, session, host: (session, None,
                                                     {"ext_reachable": False}))
    cap = CAP.DvlCapture(folder=tmp_path, vehicle_host="192.0.2.1",
                         vehicle=True, snapshot_hz=0)
    cap.start()
    try:
        assert wait_for(lambda: "_down" in cap.live.snapshot(history=False).mavlink), \
            "the vehicle thread never finished a round of reads"
    finally:
        cap.stop()
    return cap, fake


def test_a_message_never_sent_does_not_stop_the_rest_being_read(tmp_path, monkeypatch):
    """15 October 2026: GLOBAL_VISION_POSITION_ESTIMATE, second in the list,
    was never sent; mavlink2rest answered 200 and an empty body; the capture
    called that a vehicle not answering, and nothing after it in the list
    was read all day."""
    from rov_flight_ops.nav import mav2rest
    cap, fake = one_vehicle_cycle(tmp_path, monkeypatch, {
        "255/0/VISION_POSITION_DELTA": "empty",
        "255/0/GLOBAL_VISION_POSITION_ESTIMATE": "empty",
        "255/0/VISION_SPEED_ESTIMATE": "garbled",
    })
    every = {f"255/0/{m}" for m in CAP.EXTENSION_MESSAGES} | {
        f"1/1/{m}" for m in CAP.AUTOPILOT_MESSAGES}
    assert set(fake.asked) == every
    mav = cap.live.snapshot(history=False).mavlink
    assert mav["_down"] == ""
    assert mav["255/0/GLOBAL_VISION_POSITION_ESTIMATE"]["error"] == mav2rest.NEVER_SENT
    assert mav["255/0/VISION_SPEED_ESTIMATE"]["error"].startswith("unreadable reply")
    assert mav["1/1/RANGEFINDER"]["counter"] is not None
    logged = {(r["system"], r["component"], r["message"]): r
              for r in rows(files(cap)["mavlink"])}
    assert logged[("255", "0", "VISION_POSITION_ESTIMATE")]["counter"]
    assert logged[("1", "1", "DISTANCE_SENSOR")]["counter"]


def test_a_vehicle_that_does_not_answer_costs_one_timeout(tmp_path, monkeypatch):
    cap, fake = one_vehicle_cycle(tmp_path, monkeypatch, {
        f"{s}/{c}/{m}": "silent"
        for (s, c), names in (((255, 0), CAP.EXTENSION_MESSAGES),
                              ((1, 1), CAP.AUTOPILOT_MESSAGES))
        for m in names})
    assert fake.asked[0] == "255/0/VISION_POSITION_DELTA"
    assert set(fake.asked) == {"255/0/VISION_POSITION_DELTA"}
    assert cap.live.snapshot(history=False).mavlink["_down"] == "timed out"


def test_one_unanswered_read_after_answered_ones_is_not_a_vehicle_down(
        tmp_path, monkeypatch):
    cap, fake = one_vehicle_cycle(tmp_path, monkeypatch, {
        "255/0/VISION_POSITION_ESTIMATE": "silent"})
    # The round stops at the timeout, so a Pi that has gone costs one...
    assert fake.asked[:3] == ["255/0/VISION_POSITION_DELTA",
                              "255/0/GLOBAL_VISION_POSITION_ESTIMATE",
                              "255/0/VISION_POSITION_ESTIMATE"]
    # ...but the reads before it were answered, so it is not called down.
    assert cap.live.snapshot(history=False).mavlink["_down"] == ""


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
