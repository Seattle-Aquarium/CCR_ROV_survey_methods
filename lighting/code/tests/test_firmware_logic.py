"""
The light controller's telemetry logic, against scripted hardware.

What these hold the firmware to:

* a light that never answers costs the bus deadline, not the UART's two
  second timeout;
* a light's answer is passed on verbatim -- STAT? with its header row;
* the first pass asks everything, later passes follow the schedule;
* every telemetry line is one JSON object with k / n / ms, numbered from 1;
* a stalled reader on the telemetry port drops lines, never waits;
* INA226 and INA238 are told apart by their ID registers, and current and
  power are left out when the shunt is unknown.
"""

from __future__ import annotations

import json

import pytest

from conftest import CLOCK

import board
import ina2xx
import telemetry
from lightbus import LightBus
from poller import Poller


# --------------------------------------------------------------------------
#  scripted hardware
# --------------------------------------------------------------------------


class ScriptedUART:
    """Answers each question after `delay_ms`, from a table of replies."""

    def __init__(self, replies: dict[tuple[int, str], bytes], delay_ms: int = 20):
        self.replies = replies
        self.delay_ms = delay_ms
        self.sent: list[bytes] = []
        self._pending = b""
        self._ready_at = None

    def write(self, data: bytes) -> int:
        self.sent.append(data)
        text = data.decode()
        addr, cmd = int(text[1:4]), text[5:9].lower()
        self._pending = self.replies.get((addr, cmd), b"")
        self._ready_at = CLOCK.now + self.delay_ms
        return len(data)

    def any(self) -> int:
        if self._pending and self._ready_at is not None and CLOCK.now >= self._ready_at:
            return len(self._pending)
        return 0

    def read(self, n: int) -> bytes:
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


STAT = (
    b"RunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
    b"92.9hr\t30\t87.0C\t0.0C\t24.0C\t21.1C\t0%\t0x0000\r\n\r\n"
)


def bus_for(replies, **kw) -> tuple[LightBus, ScriptedUART]:
    uart = ScriptedUART(replies, **kw)
    return LightBus(
        uart,
        ticks_ms=CLOCK.ticks_ms,
        ticks_diff=CLOCK.ticks_diff,
        sleep_ms=CLOCK.sleep_ms,
    ), uart


# --------------------------------------------------------------------------
#  the bus
# --------------------------------------------------------------------------


def test_an_answer_comes_back_verbatim_with_its_round_trip():
    bus, uart = bus_for({(4, "lout"): b"100\r\n"})
    ok, raw, err, rtt = bus.query(4, "lout")
    assert (ok, raw.strip(), err) == (1, "100", "")
    assert 20 <= rtt < 40
    assert uart.sent[0].startswith(b"!004:lout?*")


def test_stat_keeps_its_header_row():
    bus, _ = bus_for({(4, "stat"): STAT})
    ok, raw, err, _ = bus.query(4, "stat")
    assert ok and err == ""
    assert raw.splitlines()[0].startswith("RunTime")
    assert "92.9hr" in raw


def test_a_silent_light_costs_the_deadline_not_two_seconds():
    bus, _ = bus_for({})
    t0 = CLOCK.now
    ok, raw, err, rtt = bus.query(2, "lout")
    assert (ok, err) == (0, "timeout")
    assert CLOCK.now - t0 <= bus.timeout_ms + 5
    assert bus.failed == 1


def test_a_nak_is_a_nak():
    bus, _ = bus_for({(1, "temp"): b"?\r\n"})
    assert bus.query(1, "temp")[2] == "nak"


def test_a_bare_cr_reply_is_accepted_after_a_pause():
    bus, _ = bus_for({(3, "lout"): b"55\r"})
    ok, raw, err, _ = bus.query(3, "lout")
    assert ok and raw.strip() == "55"


def test_an_echo_of_our_own_question_is_not_the_answer():
    class Echoing(ScriptedUART):
        def write(self, data):
            n = super().write(data)
            self._pending = data + self._pending
            return n

    uart = Echoing({(1, "lout"): b"40\r\n"})
    bus = LightBus(
        uart,
        ticks_ms=CLOCK.ticks_ms,
        ticks_diff=CLOCK.ticks_diff,
        sleep_ms=CLOCK.sleep_ms,
    )
    ok, raw, _, _ = bus.query(1, "lout")
    assert ok and raw.strip() == "40"


# --------------------------------------------------------------------------
#  the poller
# --------------------------------------------------------------------------


def test_the_first_pass_asks_everything_then_the_schedule_thins_out():
    asked = []

    class Bus:
        def query(self, addr, cmd):
            asked.append((addr, cmd))
            return 1, "0\r\n", "", 5

    p = Poller(
        Bus(),
        [1, 2],
        lambda *a, **k: None,
        gap_ms=0,
        ticks_ms=CLOCK.ticks_ms,
        ticks_diff=CLOCK.ticks_diff,
    )
    while p.passes < 2 or p.queue:
        p.step()
    first = asked[:12]
    assert {c for _a, c in first} == {"lout", "stat", "stby", "temp", "rhum", "info"}
    second = asked[12:]
    assert {c for _a, c in second} == {"lout"}  # pass 1: lout only


def test_ask_now_jumps_the_queue():
    asked = []

    class Bus:
        def query(self, addr, cmd):
            asked.append((addr, cmd))
            return 1, "", "", 1

    p = Poller(
        Bus(),
        [1, 2, 3, 4],
        lambda *a, **k: None,
        gap_ms=0,
        ticks_ms=CLOCK.ticks_ms,
        ticks_diff=CLOCK.ticks_diff,
    )
    p.step()
    p.ask_now(4, "lout")
    p.step()
    assert asked[-1] == (4, "lout")


def test_the_poller_waits_its_gap_between_questions():
    class Bus:
        def query(self, addr, cmd):
            return 1, "", "", 1

    p = Poller(
        Bus(),
        [1],
        lambda *a, **k: None,
        gap_ms=25,
        ticks_ms=CLOCK.ticks_ms,
        ticks_diff=CLOCK.ticks_diff,
    )
    assert p.step() is True
    assert p.step() is False
    CLOCK.now += 25
    assert p.step() is True


# --------------------------------------------------------------------------
#  telemetry lines
# --------------------------------------------------------------------------


def test_lines_are_numbered_json_with_kind_and_uptime():
    out = []
    sink = telemetry.Sink(
        printer=out.append, clock=telemetry.Clock(CLOCK.ticks_ms, CLOCK.ticks_diff)
    )
    sink.emit("ctl", pwm=1820, cmd=90)
    CLOCK.now += 500
    sink.emit("q", id=4, c="lout", ok=1, raw="90\r\n")
    assert all(line.startswith(telemetry.CONSOLE_PREFIX) for line in out)
    a, b = (json.loads(line[len(telemetry.CONSOLE_PREFIX) :]) for line in out)
    assert (a["k"], a["n"], b["n"]) == ("ctl", 1, 2)
    assert b["ms"] - a["ms"] == 500
    assert b["raw"] == "90\r\n"


class StalledPort:
    def __init__(self, room: int):
        self.room = room
        self.got = b""

    def is_open(self):
        return True

    def write(self, data):
        n = min(self.room, len(data))
        self.got += data[:n]
        self.room -= n
        return n


def test_a_stalled_reader_costs_lines_not_time():
    port = StalledPort(room=60)
    sink = telemetry.Sink(
        port=port, clock=telemetry.Clock(CLOCK.ticks_ms, CLOCK.ticks_diff)
    )
    for _ in range(5):
        sink.emit("ctl", pwm=1500, cmd=50)
    assert sink.dropped >= 4 and sink.sent <= 1


def test_after_a_partial_line_the_next_starts_on_a_new_line():
    port = StalledPort(room=10)
    sink = telemetry.Sink(
        port=port, clock=telemetry.Clock(CLOCK.ticks_ms, CLOCK.ticks_diff)
    )
    sink.emit("ctl", pwm=1500)
    port.room = 10_000
    sink.emit("ctl", pwm=1600)
    lines = port.got.decode().split("\n")
    assert json.loads(lines[1])["pwm"] == 1600


def test_nobody_listening_is_counted_separately():
    class Closed(StalledPort):
        def is_open(self):
            return False

    sink = telemetry.Sink(
        port=Closed(100), clock=telemetry.Clock(CLOCK.ticks_ms, CLOCK.ticks_diff)
    )
    sink.emit("ctl")
    assert sink.unread == 1 and sink.mode == "data"


# --------------------------------------------------------------------------
#  boards
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Adafruit QT Py RP2040 with RP2040", "qtpy"),
        ("Raspberry Pi Pico 2 with RP2350", "pico"),
        ("Raspberry Pi Pico with RP2040", "pico"),
        ("", "pico"),
    ],
)
def test_the_board_is_recognised_from_its_name(name, expected):
    assert board.detect(name)["name"] == expected


def test_the_qtpy_uses_its_stemma_qt_i2c_and_a0_for_pwm():
    q = board.detect("Adafruit QT Py RP2040 with RP2040")
    assert (q["pwm_pin"], q["i2c_sda"], q["i2c_scl"]) == (29, 22, 23)
    p = board.detect("Raspberry Pi Pico 2 with RP2350")
    assert (p["pwm_pin"], p["i2c_sda"], p["i2c_scl"]) == (16, 14, 15)


# --------------------------------------------------------------------------
#  power monitors
# --------------------------------------------------------------------------


class FakeI2C:
    def __init__(self, chips: dict[int, dict[int, int]]):
        self.chips = chips
        self.writes: list[tuple[int, int, bytes]] = []

    def scan(self):
        return sorted(self.chips)

    def readfrom_mem(self, addr, reg, n):
        v = self.chips[addr].get(reg)
        if v is None:
            raise OSError(5)
        return v.to_bytes(n, "big")

    def writeto_mem(self, addr, reg, data):
        self.writes.append((addr, reg, bytes(data)))


def ina238(vbus_v=24.0, temp_c=30.0):
    return {
        0x3E: 0x5449,
        0x3F: 0x2381,
        0x05: int(vbus_v / 3.125e-3),
        0x04: 100,
        0x06: int(temp_c / 0.125) << 4,
        0x07: 1000,
        0x08: 5000,
        0x0B: 1 << 3,
    }


def ina226(vbus_v=13.2):
    return {
        0xFE: 0x5449,
        0xFF: 0x2260,
        0x02: int(vbus_v / 1.25e-3),
        0x01: 400,
        0x03: 200,
        0x04: 1500,
        0x06: 1 << 4,
    }


def test_chips_are_identified_by_their_id_registers():
    i2c = FakeI2C({0x40: ina238(), 0x41: ina226(), 0x48: {0x00: 0}})
    assert ina2xx.identify(i2c, 0x40) == "INA238"
    assert ina2xx.identify(i2c, 0x41) == "INA226"
    assert ina2xx.identify(i2c, 0x48) is None


def test_without_a_shunt_value_only_voltage_is_reported():
    i2c = FakeI2C({0x40: ina238(vbus_v=24.1)})
    mons, notes = ina2xx.find(i2c)
    ((label, mon),) = mons
    r = mon.read()
    assert label == "bus" and mon.chip == "INA238"
    assert r["v"] == pytest.approx(24.1, abs=0.01)
    assert "a" not in r and "w" not in r
    assert r["t"] == pytest.approx(30.0)


def test_with_a_shunt_value_current_and_power_follow():
    i2c = FakeI2C({0x41: ina226()})
    ((_l, mon),) = ina2xx.find(i2c, shunt_ohms=0.002, max_current=32.768)[0]
    r = mon.read()
    assert r["a"] == pytest.approx(1.5, abs=1e-6)  # 1500 x 1 mA
    assert r["w"] == pytest.approx(5.0, abs=1e-6)  # 200 x 25 mW


def test_the_under_voltage_latch_is_reported_when_armed():
    i2c = FakeI2C({0x40: ina238()})
    ((_l, mon),) = ina2xx.find(i2c, uv_volts=20.0)[0]
    assert mon.read()["uv"] == 1
    # BUVL written in 3.125 mV steps, and the latch enabled.
    assert (0x40, 0x0F, int(20.0 / 3.125e-3).to_bytes(2, "big")) in i2c.writes
    assert (0x40, 0x0B, (1 << 15).to_bytes(2, "big")) in i2c.writes


def test_no_monitor_is_a_note_not_an_error():
    mons, notes = ina2xx.find(FakeI2C({}))
    assert mons == [] and notes == ["no power monitor on I2C"]
