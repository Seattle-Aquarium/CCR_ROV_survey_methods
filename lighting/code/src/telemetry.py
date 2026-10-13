"""
Telemetry out of the light controller: one JSON object per line.

The format is `ccr.lights/1`, written down in ../TELEMETRY.md. Every line has

    "k"   what kind of line it is (hello, ctl, set, q, pwr, log, stats)
    "n"   a sequence number, 1, 2, 3 ... from boot. A gap means lines were
          lost between here and the reader; a drop back to 1 means the
          controller restarted.
    "ms"  milliseconds since the controller started. Not wall-clock time --
          the controller has no clock of its own. The reader stamps each line
          with the Pi's clock as it arrives, which is the clock the tlog and
          the mcap are recorded on.

Lines go out on the telemetry port when there is one (datalink.py), written
without waiting: a line that does not fit is dropped and counted, never
waited for. Without that port they go to the console, prefixed "@L ", so a
reader can tell them from anything else printed there.

The firmware does as little interpretation as it can. A light's answer to a
question is sent verbatim, and parsing it is the reader's job: a parser on the
Pi can be fixed and re-run over a recording; a parser in here that threw a
value away has thrown it away for good.
"""

import json
import time

PROTOCOL = "ccr.lights/1"
CONSOLE_PREFIX = "@L "


class Clock:
    """Milliseconds since start, without the 12-day wrap of ticks_ms()."""

    def __init__(self, ticks_ms=None, ticks_diff=None):
        self._ticks_ms = ticks_ms or time.ticks_ms
        self._ticks_diff = ticks_diff or time.ticks_diff
        self._last = self._ticks_ms()
        self._up = 0

    def ms(self):
        now = self._ticks_ms()
        self._up += self._ticks_diff(now, self._last)
        self._last = now
        return self._up


def _tidy(value):
    """Round floats so a line stays short; leave everything else alone."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _tidy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_tidy(v) for v in value]
    return value


class Sink:
    """Where telemetry lines go.

    `port` is a usb.device.cdc.CDCInterface (or anything with write() and
    is_open()); None means the console. `console` decides whether the console
    gets telemetry at all when there is no port -- on by default, because a
    controller with old firmware or no usb-device-cdc package should still be
    readable.
    """

    def __init__(self, port=None, clock=None, console=True, printer=print):
        self.port = port
        self.clock = clock or Clock()
        self.console = console
        self._print = printer
        self.n = 0
        self.sent = 0
        self.dropped = 0          # lines that did not go out, for any reason
        self.unread = 0           # of those, because nobody had the port open
        self._broken = False      # the last write stopped part way through a line

    @property
    def mode(self):
        return "data" if self.port is not None else ("console" if self.console else "off")

    def emit(self, kind, **fields):
        self.n += 1
        fields["k"] = kind
        fields["n"] = self.n
        fields["ms"] = self.clock.ms()
        try:
            line = json.dumps(_tidy(fields))
        except Exception as ex:   # an unserialisable value is a bug; say so
            line = json.dumps({"k": "log", "n": self.n, "ms": fields["ms"],
                               "lvl": "error", "msg": "unserialisable " + kind + ": " + repr(ex)})
        if self.port is not None:
            self._to_port(line)
        elif self.console:
            self._print(CONSOLE_PREFIX + line)
            self.sent += 1
        else:
            self.dropped += 1
        return line

    def _to_port(self, line):
        try:
            if not self.port.is_open():
                self.dropped += 1
                self.unread += 1
                return
            # After a partial write, start with a newline so the fragment ends
            # where it broke instead of swallowing this line too.
            data = (("\n" if self._broken else "") + line + "\n").encode()
            wrote = self.port.write(data)
            if wrote is None or wrote < len(data):
                # The far end sees one unparseable fragment; the sequence gap
                # says how many lines went.
                self.dropped += 1
                self._broken = bool(wrote)
            else:
                self.sent += 1
                self._broken = False
        except Exception:
            self.dropped += 1
