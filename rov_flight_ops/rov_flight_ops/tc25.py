"""
TC-25: the time the transects are written down in, as the GoPro Labs page
shows it.

The crew sets each GoPro's clock from GoPro Labs' precision-time page
(https://gopro.github.io/labs/control/precisiontime/), and reads transect
times off the same page. That page does not ask the internet for the time:
it takes **this laptop's own clock** -- `new Date()`, local time, daylight
saving included -- and shows it as timecode at 25 frames a second,
``HH:MM:SS:FF`` with ``FF = trunc(milliseconds × 25 / 1000)``. Its QR code
sets the camera to that same clock. So the program computes the same figure
from the same clock, and it is identical to the page's whether or not there
is a network (`tc25`).

What the internet can add is a check on the clock itself: `ClockCheck` asks a
time server, now and then, how far this laptop's clock is from true time. The
TC-25 shown is never corrected by it -- the GoPros were set from the
uncorrected clock, and the times written down must match them -- but a
laptop several seconds out is worth knowing before the cameras are synced
from it.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from datetime import datetime

#: The time server Windows itself uses.
NTP_SERVER = "time.windows.com"
#: How often the clock is checked, and how long a check may take.
CHECK_EVERY_S = 600.0
TIMEOUT_S = 2.0
#: Seconds between 1900 (NTP's epoch) and 1970 (the Unix one).
NTP_EPOCH = 2_208_988_800


def tc25(when: datetime | None = None) -> str:
    """``HH:MM:SS:FF`` at 25 frames a second, local time, as the page has it."""
    t = when or datetime.now()
    frame = int(t.microsecond / 1000 * 25 / 1000)
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}:{frame:02d}"


def sntp_offset(server: str = NTP_SERVER, timeout: float = TIMEOUT_S) -> float:
    """Seconds to add to this laptop's clock to get the server's: one SNTP
    request (RFC 4330), the round trip halved. Raises OSError offline."""
    packet = b"\x1b" + 47 * b"\0"            # LI 0, version 3, mode 3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(packet, (server, 123))
        data, _addr = sock.recvfrom(512)
        t4 = time.time()
    if len(data) < 48:
        raise OSError("short reply from the time server")

    def stamp(offset: int) -> float:
        sec, frac = struct.unpack("!II", data[offset:offset + 8])
        return sec - NTP_EPOCH + frac / 2 ** 32

    t2, t3 = stamp(32), stamp(40)            # server receive, server transmit
    return ((t2 - t1) + (t3 - t4)) / 2


class ClockCheck:
    """The laptop clock against a time server, checked in the background."""

    def __init__(self, server: str = NTP_SERVER, every_s: float = CHECK_EVERY_S,
                 query=sntp_offset) -> None:
        self.server = server
        self.every_s = every_s
        self._query = query
        self._lock = threading.Lock()
        self.offset_s: float | None = None
        self.checked_at: float | None = None
        self.error = ""
        self._busy = False
        self._last_try = 0.0

    def poll(self) -> None:
        """Start a check if one is due. Never blocks the caller."""
        now = time.monotonic()
        with self._lock:
            if self._busy or (self._last_try and now - self._last_try < self.every_s):
                return
            self._busy = True
            self._last_try = now
        threading.Thread(target=self._check, daemon=True, name="clock-check").start()

    def _check(self) -> None:
        try:
            offset = self._query(self.server)
            with self._lock:
                self.offset_s, self.checked_at, self.error = offset, time.monotonic(), ""
        except Exception as ex:                                  # noqa: BLE001
            with self._lock:
                self.error = str(ex) or type(ex).__name__
                # Offline is the usual reason: try again sooner than a full
                # interval, but not every second.
                self._last_try = time.monotonic() - self.every_s + 60.0
        finally:
            with self._lock:
                self._busy = False

    def words(self) -> str:
        """What to say under the time."""
        with self._lock:
            offset, error = self.offset_s, self.error
        if offset is None:
            return "laptop clock · not checked (offline)" if error else "laptop clock"
        if abs(offset) < 0.05:
            return "laptop clock · on time"
        return (f"laptop clock · {abs(offset):.2f} s "
                f"{'slow' if offset > 0 else 'fast'}")

    def off_by(self) -> float | None:
        with self._lock:
            return self.offset_s
