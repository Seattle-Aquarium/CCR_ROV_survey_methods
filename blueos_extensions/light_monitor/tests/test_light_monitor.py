"""
The Light Monitor extension, without a vehicle.

Held to:

* Aaron Marburg's debug output (CCR_development#50, 13 Oct 2026) and the
  ccr.lights/1 lines both become the same records;
* STAT? is read by its header when there is one, by position when not, and
  Lutris's eight columns land in the right fields;
* the four events that matter -- not answering, answering again, restarted
  (power-on count up), level mismatch -- fire once each, at the threshold;
* a controller restart and lost lines are seen from the sequence numbers;
* every line is recorded verbatim before anything else, and the topside
  mirror (/api/v1/records) returns exactly what was written;
* MAVLink names are at most 10 characters, unknown values are not sent, and
  the publisher sends changes first and never more than its cap a second;
* the controller's serial port is never written to.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from light_monitor import mavlink, power, protocol                     # noqa: E402
from light_monitor.model import (MISMATCH_AFTER, NOT_ANSWERING_AFTER,   # noqa: E402
                                 Model, csv_columns)
from light_monitor.recorder import Recorder                             # noqa: E402
from light_monitor.serialsource import SerialSource, choose             # noqa: E402

AARON = """\
PWM: 1109 us | Level: 0%
1609790283245552000   1: Stat:     93.7hr       112     87.0C   0.0C    24.0C   20.6C   0%      0x0000
1609790283268117000   1: Level:    0
1609790283293502000   1: Temp:     0.0
1609790283315646000   1: Humidity: 0.0
1609790283843871000   4: Stat:     92.9hr       30      87.0C   0.0C    24.0C   21.1C   0%      0x0000
"""  # noqa: E501 -- verbatim from CCR_development#50

STAT = ("RunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
        "92.9hr\t30\t87.0C\t0.0C\t24.0C\t21.1C\t0%\t0x0000\r\n\r\n")


def line(kind, n, **f):
    return json.dumps({"k": kind, "n": n, "ms": n * 100, **f})


# --------------------------------------------------------------------------
#  protocol
# --------------------------------------------------------------------------


def test_aarons_debug_output_is_read():
    recs = [protocol.parse_line(x) for x in AARON.splitlines()]
    assert recs[0] == {"k": "ctl", "pwm": 1109, "sig": 1, "cmd": 0, "legacy": 1}
    assert recs[1]["c"] == "stat" and recs[1]["id"] == 1
    st = protocol.parse_stat(recs[1]["raw"])
    assert st["by"] == "position"
    assert (st["runtime_h"], st["power_cycles"], st["max_temp_c"]) == (93.7, 112, 87.0)
    assert (st["temp1_c"], st["temp2_c"], st["temp3_c"]) == (0.0, 24.0, 20.6)
    assert st["humidity_pct"] == 0.0 and st["flags"] == 0
    assert recs[2] == {"k": "q", "id": 1, "c": "lout", "ok": 1, "raw": "0", "legacy": 1}
    assert recs[3]["c"] == "temp" and recs[4]["c"] == "rhum"


def test_stat_is_read_by_its_header():
    st = protocol.parse_stat(STAT)
    assert st["by"] == "header"
    assert st["power_cycles"] == 30 and st["temp3_c"] == 21.1 and st["flags_hex"] == "0x0000"
    assert st["columns"][0] == ["RunTime", "92.9hr"]


def test_the_specs_five_column_stat_is_read_too():
    st = protocol.parse_stat("RunTime\tPrCycle\tMaxTemp\tHumid\tFLAGS\r\n"
                             "0.5hr\t24\t93.7C\t44%\t0x0001\r\n\r\n")
    assert (st["power_cycles"], st["humidity_pct"], st["flags"]) == (24, 44.0, 1)


def test_telemetry_lines_with_or_without_the_console_prefix():
    a = protocol.parse_line('{"k":"ctl","n":3,"ms":10,"pwm":1820}')
    b = protocol.parse_line('@L {"k":"ctl","n":3,"ms":10,"pwm":1820}')
    assert a == b and a["pwm"] == 1820
    assert protocol.parse_line("Lutris Lighting System Starting...") is None
    assert protocol.parse_line('{"not": "ours"}') is None


def test_info_is_split_into_its_fields():
    info = protocol.parse_info("004,LSL-2075,12-049-7628,0A,1.1.0,DW00,MED,SER")
    assert (info["model"], info["serial"], info["firmware"]) == ("LSL-2075", "12-049-7628", "1.1.0")


# --------------------------------------------------------------------------
#  the model
# --------------------------------------------------------------------------


def feed(m: Model, kind, n, t, **f):
    return m.ingest(json.loads(line(kind, n, **f)), rx=t)


def test_not_answering_then_back_then_restarted():
    m = Model(clock=lambda: 100.0)
    n, t = 1, 100.0
    feed(m, "q", n, t, id=4, c="stat", ok=1, raw=STAT)
    events = []
    for _ in range(NOT_ANSWERING_AFTER):
        n += 1
        events += feed(m, "q", n, t, id=4, c="lout", ok=0, err="timeout")
    assert [e.short for e in events] == ["light 4 FR not answering"]
    n += 1
    events = feed(m, "q", n, t + 3, id=4, c="lout", ok=1, raw="100\r\n")
    assert [e.short for e in events] == ["light 4 FR answering again"]
    n += 1
    events = feed(m, "q", n, t + 4, id=4, c="stat", ok=1, raw=STAT.replace("\t30\t", "\t31\t"))
    assert [e.short for e in events] == ["light 4 FR restarted (power-on 31)"]
    lt = m.snapshot(t + 4)["lights"][3]
    assert lt["restarts_this_session"] == 1 and lt["power_cycles"] == 31


def test_a_recovery_is_announced_only_if_its_dropout_was():
    """Two dropouts inside the cool-down: one 'stopped', one 'again' -- not
    one 'stopped' and two 'again'."""
    m = Model(clock=lambda: 0.0)
    shorts, n, t = [], 0, 0.0
    for _round in range(2):
        for _ in range(NOT_ANSWERING_AFTER):
            n += 1
            shorts += [e.short for e in feed(m, "q", n, t, id=4, c="lout", ok=0, err="timeout")]
        n += 1
        t += 2.0
        shorts += [e.short for e in feed(m, "q", n, t, id=4, c="lout", ok=1, raw="100")]
    assert shorts.count("light 4 FR not answering") == 1
    assert shorts.count("light 4 FR answering again") == 1


def test_a_level_mismatch_is_reported_at_the_threshold():
    m = Model(clock=lambda: 0.0)
    feed(m, "set", 1, 0.0, id=2, lvl=90, why="pwm")
    events = []
    for i in range(MISMATCH_AFTER):
        events += feed(m, "q", 2 + i, float(i), id=2, c="lout", ok=1, raw="0\r\n")
    assert [e.short for e in events] == ["light 2 at 0%, told 90%"]
    snap = m.snapshot(5.0)
    assert snap["lights"][1]["state"] == "mismatch"


def test_controller_restart_and_lost_lines_come_from_the_sequence():
    m = Model(clock=lambda: 0.0)
    feed(m, "ctl", 10, 0.0, cmd=50)
    feed(m, "ctl", 14, 1.0, cmd=50)                   # 11..13 lost
    events = m.ingest({"k": "hello", "n": 1, "ms": 5}, rx=2.0)
    assert m.lost_lines == 3
    assert [e.short for e in events] == ["controller restarted"]


def test_expected_power_follows_the_dimming_curve():
    assert power.expected_w(100) == pytest.approx(98.0)
    assert power.expected_w(0) == 0.0
    assert 10 < power.expected_w(50) < 15          # LOUT 50 is ~12 % of full output
    assert power.expected_a(100) == pytest.approx(98.0 / 24.0)


def test_low_vehicle_voltage_warns_once_with_hysteresis():
    m = Model(clock=lambda: 0.0)
    ev = m.set_vehicle(10.0, 40.0, at=1.0) + m.set_vehicle(9.9, 40.0, at=2.0)
    assert [e.lvl for e in ev] == ["warn"]
    ev = m.set_vehicle(10.6, 30.0, at=3.0) + m.set_vehicle(11.0, 30.0, at=4.0)
    assert [e.lvl for e in ev] == ["info"]


def test_nvf_names_fit_mavlink_and_unknowns_are_not_sent():
    m = Model(clock=lambda: 0.0)
    feed(m, "ctl", 1, 0.0, pwm=1500, sig=1, cmd=50)
    feed(m, "q", 2, 0.0, id=1, c="lout", ok=1, raw="50\r\n")
    vals = m.nvf_values(0.5)
    assert all(len(k) <= 10 for k in vals)
    assert vals["LT1_LVL"] == 50.0 and vals["LT_CMD"] == 50.0
    assert "LT2_LVL" not in vals and "LT_V" not in vals      # not known, not sent


def test_the_csv_row_has_every_column():
    m = Model(clock=lambda: 0.0)
    row = m.csv_row(0.0)
    assert list(row) == csv_columns()


# --------------------------------------------------------------------------
#  recording
# --------------------------------------------------------------------------


def test_every_line_is_recorded_verbatim_and_mirrored(tmp_path):
    rec = Recorder(tmp_path, csv_columns(), session={"version": "test"}, min_free_bytes=0)
    rec.line('{"k":"ctl","n":1}', "data", 1000.0)
    rec.line("garbage \x01 line", "console", 1001.0)
    rec.record("event", rx=1002.0, lvl="warn", text="x")
    rec.close()
    rows = [json.loads(x) for x in rec.path.read_text().splitlines()]
    assert [r.get("x") for r in rows] == ["session", None, None, "event"]
    assert rows[1]["line"] == '{"k":"ctl","n":1}' and rows[2]["line"] == "garbage \x01 line"
    items, newest = rec.after(1)
    assert [r["id"] for r in items] == [2, 3, 4] and newest == 4
    assert rec.path.name.startswith("lights_19700101_")


def test_recording_pauses_rather_than_fill_the_disk(tmp_path):
    rec = Recorder(tmp_path, csv_columns(), min_free_bytes=10**18)
    rec.line("x", "data", 0.0)
    assert rec.paused and rec.path is None


# --------------------------------------------------------------------------
#  MAVLink
# --------------------------------------------------------------------------


class FakeLink:
    def __init__(self):
        self.sent = []

    def named_float(self, name, value, t=0):
        self.sent.append((name, value))
        return True


def test_the_publisher_sends_changes_then_keepalives_within_its_cap():
    clock = [0.0]
    link = FakeLink()
    pub = mavlink.Publisher(link, clock=lambda: clock[0])
    values = {f"LT{i}_LVL": 50.0 for i in range(1, 5)} | {f"LT{i}_OK": 1.0 for i in range(1, 5)}
    values |= {f"X{i}": float(i) for i in range(10)}
    assert pub.publish(values) == mavlink.MAX_PER_S
    clock[0] = 1.0
    link.sent.clear()
    pub.publish(values)                         # the rest, never sent before
    assert len(link.sent) == len(values) - mavlink.MAX_PER_S
    clock[0] = 2.0
    link.sent.clear()
    values["LT4_LVL"] = 0.0
    pub.publish(values)
    assert link.sent == [("LT4_LVL", 0.0)]      # only the change
    clock[0] = 2.0 + mavlink.KEEPALIVE_S
    link.sent.clear()
    pub.publish(values)
    assert 0 < len(link.sent) <= mavlink.MAX_PER_S


def test_char_arrays_are_padded_for_mavlink2rest():
    assert mavlink._chars("LT4_LVL", 10) == list("LT4_LVL") + ["\u0000"] * 3
    assert len(mavlink._chars("x" * 80, 50)) == 50


# --------------------------------------------------------------------------
#  the serial port
# --------------------------------------------------------------------------


def test_the_telemetry_interface_is_preferred_and_the_console_is_the_fallback():
    cands = [("/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_e66-if00", 0),
             ("/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_e66-if02", 2)]
    assert choose(cands, "auto") == (cands[1][0], "data")
    assert choose(cands[:1], "auto") == (cands[0][0], "console")
    assert choose(cands[:1], "data") is None


class FakePort:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.written = []

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.01)
        return b""

    def write(self, data):                       # must never be called
        self.written.append(data)

    def close(self):
        pass


def test_lines_are_split_across_reads_and_nothing_is_ever_written(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-MicroPython_Board_in_FS_mode_e66-if02").write_text("")
    port = FakePort([b'{"k":"ctl","n":1', b'}\n{"k":"ctl",', b'"n":2}\r\n'])
    got, status = [], []
    src = SerialSource(lambda t, r, rx: got.append((t, r)), lambda **f: status.append(f),
                       opener=lambda path: port, by_id=by_id)
    src.start()
    deadline = time.time() + 3
    while len(got) < 2 and time.time() < deadline:
        time.sleep(0.02)
    src.stop()
    assert got == [('{"k":"ctl","n":1}', "data"), ('{"k":"ctl","n":2}', "data")]
    assert port.written == []
    assert any(s.get("connected") for s in status)


# --------------------------------------------------------------------------
#  the web API, end to end against the simulator
# --------------------------------------------------------------------------


def test_the_api_serves_state_records_and_the_page(tmp_path):
    from light_monitor.app import App
    from light_monitor.web import serve
    app = App(data=tmp_path, mode="simulate", mavlink_url=None, sim_monitor=True)
    app.start()
    server = serve(app, "127.0.0.1", 0)
    port = server.server_address[1]
    try:
        time.sleep(1.5)
        def get(path):
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).read()
        state = json.loads(get("/api/v1/state"))
        assert state["schema"] == "light_monitor.state/1"
        assert len(state["lights"]) == 4 and state["power"]["rail"]["chip"] == "INA238"
        recs = json.loads(get("/api/v1/records?after=0&limit=50"))
        assert recs["records"][0]["x"] == "session" and recs["newest"] >= 10
        assert b"Light monitor" in get("/")
        assert json.loads(get("/register_service"))["works_in_relative_paths"] is True
        files = json.loads(get("/api/v1/files"))["files"]
        assert any(f["name"].endswith(".jsonl") for f in files)
        name = next(f["name"] for f in files if f["name"].endswith(".csv"))
        assert get(f"/files/{name}").startswith(b"utc,pi_epoch")
        with pytest.raises(urllib.error.HTTPError):
            get("/files/..%2F..%2Fetc%2Fpasswd")
    finally:
        server.shutdown()
        app.stop()
