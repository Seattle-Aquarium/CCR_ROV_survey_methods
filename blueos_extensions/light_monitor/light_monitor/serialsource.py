"""
Finding the light controller on the Pi's USB, and reading it -- read-only.

The controller (a QT Py RP2040 or Pico running MicroPython) shows up as one
USB device with up to two serial ports, which Linux names by interface:

    /dev/serial/by-id/usb-MicroPython_..._<serial>-if00   the console (REPL)
    /dev/serial/by-id/usb-MicroPython_..._<serial>-if02   telemetry (datalink.py)

The by-id names are used rather than /dev/ttyACM0 and 1, because ACM numbers
are handed out in the order devices appear and change when one is unplugged.

**Which port.** `auto` (the default) reads the telemetry port when there is
one, and the console otherwise -- the console is what today's firmware, and
Aaron's debug build, print to. Reading the console is harmless to the
controller (nothing is ever written to it), but it does compete with
`mpremote` for the bytes the controller sends; hence **release**, which closes
the port for a while so mpremote can have it, and reopens it on its own when
the time is up, so it cannot be forgotten in the released state before a dive.

**Never writes.** No byte is sent to either port, ever: a Ctrl-C (0x03) on the
console stops main.py, which turns all four lights off. The port is opened
with pyserial's defaults, which raise DTR -- that is what tells MicroPython a
reader is present (and lets the telemetry port's `is_open()` see us).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

BY_ID = Path("/dev/serial/by-id")

#: Substrings of a by-id name that mark a MicroPython RP2040/RP2350 board.
#: MicroPython's rp2 port reports manufacturer "MicroPython"; Adafruit's QT Py
#: build keeps that string and changes only the vendor/product IDs.
MATCH = ("micropython", "adafruit", "qt_py", "qtpy", "pico", "rp2040", "rp2350")

TELEMETRY_IFACE = 2
CONSOLE_IFACE = 0

MAX_LINE = 64 * 1024
RESCAN_S = 2.0
#: While on the console, look for a telemetry port this often (firmware
#: updated, or the port came up late) and move to it.
UPGRADE_CHECK_S = 10.0


def candidates(by_id: Path = BY_ID) -> list[tuple[str, int | None]]:
    """(path, interface number) for every MicroPython-looking serial port."""
    out = []
    try:
        entries = sorted(by_id.iterdir())
    except OSError:
        return out
    for p in entries:
        name = p.name.lower()
        if not any(m in name for m in MATCH):
            continue
        m = re.search(r"-if([0-9a-f]{2})", name)
        out.append((str(p), int(m.group(1), 16) if m else None))
    return out


def choose(cands: list[tuple[str, int | None]], mode: str = "auto",
           data_port: str = "", console_port: str = "") -> tuple[str, str] | None:
    """(path, role) to read, role "data" or "console"; None if nothing fits."""
    if mode in ("auto", "data") and data_port:
        return data_port, "data"
    data = [p for p, i in cands if i == TELEMETRY_IFACE]
    if mode in ("auto", "data") and data:
        return data[0], "data"
    if mode in ("auto", "console"):
        if console_port:
            return console_port, "console"
        console = [p for p, i in cands if i in (CONSOLE_IFACE, None)]
        if console:
            return console[0], "console"
    return None


class SerialSource:
    """A thread that reads lines from the controller and hands them on.

    `on_line(text, role, rx_epoch)` for every line; `on_status(**fields)`
    whenever the connection changes. `opener(path)` returns an object with
    read(n) and close() -- pyserial in production, a fake in tests.
    """

    def __init__(self, on_line: Callable, on_status: Callable, *, mode: str = "auto",
                 data_port: str = "", console_port: str = "",
                 opener: Callable | None = None, by_id: Path = BY_ID,
                 clock=time.time) -> None:
        self.on_line = on_line
        self.on_status = on_status
        self.mode = mode
        self.data_port = data_port or os.environ.get("LIGHTS_DATA_PORT", "")
        self.console_port = console_port or os.environ.get("LIGHTS_CONSOLE_PORT", "")
        self.opener = opener or _open_pyserial
        self.by_id = by_id
        self.clock = clock
        self._stop = threading.Event()
        self._released_until: float | None = None
        self._thread: threading.Thread | None = None
        self._kick = threading.Event()

    # -- control --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="serial")
        self._thread.start()

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        self._kick.set()
        if self._thread:
            self._thread.join(wait)

    def release(self, minutes: float = 15.0) -> float:
        """Close the port for `minutes` (for mpremote). Returns when it reopens."""
        self._released_until = self.clock() + max(0.5, minutes) * 60.0
        self._kick.set()
        return self._released_until

    def resume(self) -> None:
        self._released_until = None
        self._kick.set()

    @property
    def released_until(self) -> float | None:
        if self._released_until is not None and self.clock() >= self._released_until:
            self._released_until = None
        return self._released_until

    # -- the loop --------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self._kick.wait(seconds)
        self._kick.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            until = self.released_until
            if until is not None:
                self.on_status(connected=False, source="released", port="",
                               released_until=until,
                               note="released for maintenance (mpremote may use the port)")
                self._wait(min(5.0, max(0.1, until - self.clock())))
                continue
            got = choose(candidates(self.by_id), self.mode, self.data_port, self.console_port)
            if got is None:
                self.on_status(connected=False, source="none", port="", released_until=None,
                               note="no light controller found on USB")
                self._wait(RESCAN_S)
                continue
            path, role = got
            try:
                handle = self.opener(path)
            except Exception as ex:                       # noqa: BLE001
                self.on_status(connected=False, source=role, port=path, released_until=None,
                               note=f"could not open: {ex}")
                self._wait(RESCAN_S)
                continue
            self.on_status(connected=True, source=role, port=path, released_until=None,
                           note=("telemetry port" if role == "data" else
                                 "console, read-only (no telemetry port found)"))
            try:
                self._read(handle, role)
            except Exception as ex:                       # noqa: BLE001
                self.on_status(connected=False, source=role, port=path,
                               note=f"read stopped: {ex}")
            finally:
                try:
                    handle.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self._wait(0.5)

    def _read(self, handle, role: str) -> None:
        buf = b""
        next_upgrade = self.clock() + UPGRADE_CHECK_S
        while not self._stop.is_set():
            if self.released_until is not None:
                return
            if role == "console" and self.clock() >= next_upgrade:
                next_upgrade = self.clock() + UPGRADE_CHECK_S
                better = choose(candidates(self.by_id), "data", self.data_port, "")
                if better is not None and self.mode == "auto":
                    self.on_status(note="telemetry port appeared; moving to it")
                    return
            chunk = handle.read(4096)
            if chunk is None:
                raise OSError("port closed")
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._deliver(line, role)
            if len(buf) > MAX_LINE:
                self._deliver(buf, role)
                buf = b""

    def _deliver(self, raw: bytes, role: str) -> None:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        try:
            self.on_line(text, role, self.clock())
        except Exception:                                 # noqa: BLE001
            log.exception("handling a line from the controller")


def _open_pyserial(path: str):
    import serial  # pyserial; imported here so tests need not have it
    # The baud rate means nothing on a USB CDC port; DTR is raised on open.
    return serial.Serial(path, 115200, timeout=0.5, write_timeout=0, exclusive=False)
