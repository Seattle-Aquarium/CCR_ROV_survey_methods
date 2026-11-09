"""
The extension against the firmware that is actually on the vehicle.

`lighting/code/src` -- `main.LightingController` and everything it imports,
unchanged -- is run here under CPython with a fake millisecond clock, a stub
`machine`, a pretend RS-485 bus of four SeaLites behind the QT Py's
transceiver (which hears its own transmissions), and a pretend telemetry port
that keeps every byte the firmware writes. Those bytes then go through the
extension the way they arrive from the controller's second USB port: split
into lines, parsed, fed to the model.

So if the firmware's telemetry changes in a way the extension cannot read --
as it did between the format's first draft ("k", "q", "c") and the firmware
on Lutris ("type", "query", "cmd") -- this fails, and the image is not built.

The bus answers as DeepSea's SeaSense specification says a light does: data
then CR LF for a query, a bare CR LF for a write (section 5), and STAT? as a
tab-separated header row, a value row and a blank row (Appendix G), with the
eight columns Lutris's LSL-2075s send.
"""

from __future__ import annotations

import gc
import re
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from light_monitor import protocol                                       # noqa: E402
from light_monitor.model import Model                                    # noqa: E402

FIRMWARE = Path(__file__).resolve().parents[3] / "lighting" / "code" / "src"
MODULES = ("main", "board", "telemetry", "datalink", "lightbus", "poller", "seasense",
           "uart_wrapper", "pwm_reader", "ina2xx")

pytestmark = pytest.mark.skipif(not (FIRMWARE / "main.py").is_file(),
                                reason="the light controller's firmware is not in this checkout")


class Clock:
    """MicroPython's ticks, moving only when the firmware sleeps or waits."""

    def __init__(self) -> None:
        self.now = 10_000
        self.on_advance = None

    def ticks_ms(self) -> int:
        return self.now

    def ticks_us(self) -> int:
        return self.now * 1000

    @staticmethod
    def ticks_diff(a: int, b: int) -> int:
        return a - b

    def advance(self, ms: int) -> None:
        self.now += max(1, int(ms))
        if self.on_advance is not None:
            self.on_advance()

    def sleep_ms(self, ms: int) -> None:
        self.advance(ms)

    def sleep_us(self, us: int) -> None:
        self.advance(us // 1000)


class Bus:
    """Four SeaLites at addresses 1-4, and the QT Py hearing itself transmit."""

    PACKET = re.compile(r"^!(\d{3}):(\w{4})([=?+-])([^*\r\n]*)")
    UART_TIMEOUT_MS = 2000      # main.py gives the UART two seconds

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rx = bytearray()
        self.level = {1: 0, 2: 0, 3: 0, 4: 0}
        self.power_on = {1: 112, 2: 147, 3: 109, 4: 30}
        self.silent: set[int] = set()      # hear nothing, answer nothing
        self.deaf: set[int] = set()        # miss LOUT= writes, answer questions

    # -- what the firmware calls -----------------------------------------
    def write(self, data) -> int:
        data = data.encode("ascii") if isinstance(data, str) else bytes(data)
        self.rx += data                                # the local echo
        reply = self._answer(data.decode("ascii"))
        if reply is not None:
            self.rx += reply.encode("ascii")
        return len(data)

    def flush(self) -> None:
        pass

    def any(self) -> int:
        return len(self.rx)

    def read(self, n: int = 1):
        if not self.rx:
            return None
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def read_until(self, terminator: bytes = b"\n", size=None) -> bytes:
        i = self.rx.find(terminator)
        if i < 0:
            out = bytes(self.rx)
            self.rx.clear()
            self.clock.advance(self.UART_TIMEOUT_MS)   # waits out the timeout
            return out
        out = bytes(self.rx[:i + 1])
        del self.rx[:i + 1]
        self.clock.advance(3)
        return out

    # -- the lights --------------------------------------------------------
    def _answer(self, packet: str) -> str | None:
        m = self.PACKET.match(packet)
        if not m:
            return None
        addr, cmd, access, data = int(m.group(1)), m.group(2).lower(), m.group(3), m.group(4)
        if addr not in self.level or addr in self.silent:
            return None
        if access == "=" and cmd == "lout":
            if addr in self.deaf:
                return None                            # the command was lost
            self.level[addr] = int(data)
            return "\r\n"                              # ACK: a bare CR LF
        if access != "?":
            return "?\r\n"
        if cmd == "lout":
            return f"{self.level[addr]}\r\n"
        if cmd == "stat":
            return ("RunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
                    f"93.7hr\t{self.power_on[addr]}\t87.0C\t0.0C\t24.0C\t20.6C\t0%\t0x0000\r\n"
                    "\r\n")
        if cmd in ("temp", "rhum"):
            return "0.0\r\n"
        if cmd == "stby":
            return "0\r\n"
        if cmd == "info":
            return f"{addr:03d},LSL-2075,12-049-762{addr},0A,1.1.0,DW00,MED,SER\r\n"
        return "?\r\n"


class TelemetryPort:
    """The second USB serial port, with the Light Monitor holding it open."""

    def __init__(self) -> None:
        self.data = bytearray()

    def is_open(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        self.data += data
        return len(data)


@pytest.fixture
def firmware(monkeypatch):
    """The firmware's modules, imported fresh against fakes, removed afterwards."""
    clock = Clock()
    machine = types.ModuleType("machine")

    class Pin:
        IN = OUT = PULL_DOWN = PULL_UP = IRQ_RISING = IRQ_FALLING = 0

        def __init__(self, *a, **k) -> None:
            pass

        def irq(self, *a, **k) -> None:
            pass

        def value(self, *a):
            return 0

    machine.Pin = Pin
    machine.I2C = lambda *a, **k: None       # no power monitor answers, as on Lutris
    machine.UART = lambda *a, **k: None      # the bus below stands in for it
    machine.reset_cause = lambda: 1
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "machine", machine)
    for name in ("ticks_ms", "ticks_us", "ticks_diff", "sleep_ms", "sleep_us"):
        monkeypatch.setattr(time, name, getattr(clock, name), raising=False)
    monkeypatch.setattr(gc, "mem_free", lambda: 150_000, raising=False)
    monkeypatch.syspath_prepend(str(FIRMWARE / "lib"))
    monkeypatch.syspath_prepend(str(FIRMWARE))      # sys.path is restored afterwards
    import board
    import datalink
    monkeypatch.setattr(board, "machine_name", lambda: "Adafruit QT Py RP2040 with RP2040")
    port = TelemetryPort()
    datalink.port, datalink.error = port, ""
    import main
    yield types.SimpleNamespace(main=main, clock=clock, port=port)
    for name in MODULES:
        sys.modules.pop(name, None)


def test_the_extension_reads_everything_the_lutris_firmware_sends(firmware):
    clock, port = firmware.clock, firmware.port
    ctl = firmware.main.LightingController()
    assert ctl.board["name"] == "qtpy" and ctl.tel.mode == "data"
    bus = Bus(clock)
    ctl._bus.uart = bus

    # The pilot's servo output over a 26 s run, as the controller's PWM pin
    # sees it. Light 4 goes silent for 10 s while the level holds (each
    # question to it then costs the UART's 2 s timeout); light 2 loses power
    # once; light 3 misses the command to go to 100 %.
    script = ((0, 1100), (3_000, 1580), (18_000, 1900), (21_000, 1340))
    t0 = clock.now
    stopped = []

    def pwm_now(t: int) -> int:
        return [us for start, us in script if t >= start][-1]

    def world() -> None:
        t = clock.now - t0
        reader = ctl._pwm_reader
        reader._last_edge_us = clock.ticks_us()      # edges keep coming
        reader._pulse_width_us = pwm_now(t)
        bus.silent = {4} if 5_000 <= t < 15_000 else set()
        bus.deaf = {3} if 18_000 <= t < 19_000 else set()
        if t >= 10_000 and bus.power_on[2] == 147:
            bus.power_on[2] += 1
        if t >= 26_000 and not stopped:
            stopped.append(t)
            raise KeyboardInterrupt                  # how main.py is stopped
        if t >= 90_000:
            raise RuntimeError("the control loop did not stop")

    ctl._pwm_reader._last_edge_us = clock.ticks_us()
    ctl._pwm_reader._pulse_width_us = 1100
    clock.on_advance = world
    ctl.run()                                        # returns after the KeyboardInterrupt
    clock.on_advance = None
    assert stopped

    # -- through the extension, as from /dev/ttyACM1 ----------------------
    m = Model(clock=lambda: 0.0)
    kinds, unread, events = set(), [], []
    texts = [t.rstrip("\r") for t in port.data.decode("utf-8").split("\n")]
    for i, text in enumerate(t for t in texts if t):
        rec = protocol.parse_line(text)
        if rec is None:
            unread.append(text)
            continue
        kinds.add(rec["k"])
        events += m.ingest(rec, rx=1000.0 + i * 0.05)
    assert unread == []
    assert {"hello", "ctl", "set", "q", "stats", "log"} <= kinds
    assert m.hello["fw"].startswith("lutris-lights") and m.hello["board"] == "qtpy"
    assert m.lost_lines == 0 and m.controller_restarts == 0

    final = ctl.pwm_to_level(1340)                   # the firmware's own mapping
    lights = {lt.id: lt for lt in m.lights.values()}
    for i in (1, 2, 3, 4):
        assert lights[i].answering is True, i
        assert lights[i].power_cycles is not None
        assert lights[i].level == final, (i, lights[i].level)
        assert not lights[i].mismatch, i
    assert lights[2].restarts == 1
    shorts = [e.short for e in events]
    assert "light 4 FR missed an answer" in shorts
    assert "light 4 FR not answering" in shorts
    assert "light 4 FR answering again" in shorts
    assert any(s.startswith("light 2 RL restarted") for s in shorts)
    # Light 3 missed the command to go to 100 %, and this firmware does not
    # resend (RESYNC_LEVELS = False): it stayed where it was until the next
    # change reached it -- the one level mismatch in the run, and its end.
    told = [s for s in shorts if "told" in s]
    assert told == [f"light 3 at {ctl.pwm_to_level(1580)}%, told {ctl.pwm_to_level(1900)}%"], told
    assert "light 3 RR level ok" in shorts
    snap = m.snapshot()
    assert snap["controller"]["fw"].startswith("lutris-lights")
    assert snap["controller"]["pwm_us"] is not None
    assert snap["controller"]["stats"]["asked"] > 0
    assert snap["lights"][0]["temp2_c"] == 24.0 and snap["lights"][0]["stat_by"] == "header"
