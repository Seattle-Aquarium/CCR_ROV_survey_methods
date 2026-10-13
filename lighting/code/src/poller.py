"""
What to ask each light, and when -- one question per turn of the control loop.

Steering the lights comes first. The control loop runs every 50 ms; on a turn
where the pilot's brightness changed it sends the new level and asks nothing.
On any other turn it may ask one question, which costs at most the bus's
deadline (150 ms, 450 ms for STAT?). So a brightness change waits, at worst,
for one answer already in flight.

The questions, and why each is worth the bus time:

=======  ============  ======================================================
LOUT?    every pass    the level the light *thinks* it is at -- the check
                       that a command arrived, and that a light which
                       restarted came back at the level it was told
STAT?    every 3rd     power-on count (the one number that says a light lost
                       power and restarted), run time, temperatures, humidity
                       and DeepSea's fault flags
STBY?    every 10th    standby: a light can answer and still be dark
TEMP?    every 60th    reads 0.0 on all four of Lutris's lights (Aaron
RHUM?    every 60th    Marburg, CCR_development#50) -- kept, rarely, in case
                       a firmware update or a different light makes them real
INFO?    every 600th   serial number and firmware, which also confirms which
                       physical light answers to which address
=======  ============  ======================================================

On the first pass everything is asked, so a reader that starts with the
controller learns everything within a few seconds. With four lights a pass
of LOUT? alone takes about a third of a second, and one with STAT? about a
second, so each light is heard from roughly every half second to a second.
"""

import time

#: (command, ask on every n-th pass)
SCHEDULE = (
    ("lout", 1),
    ("stat", 3),
    ("stby", 10),
    ("temp", 60),
    ("rhum", 60),
    ("info", 600),
)

#: Quiet time on the bus after an answer before the next question.
GAP_MS = 25


class Poller:

    def __init__(self, bus, addresses, emit, schedule=SCHEDULE, gap_ms=GAP_MS,
                 ticks_ms=None, ticks_diff=None):
        self.bus = bus
        self.addresses = list(addresses)
        self.emit = emit
        self.schedule = tuple(schedule)
        self.gap_ms = gap_ms
        self._ticks_ms = ticks_ms or time.ticks_ms
        self._ticks_diff = ticks_diff or time.ticks_diff
        self.queue = []
        self.passes = 0
        self._last = None
        self.enabled = True

    def _refill(self):
        first = self.passes == 0
        for addr in self.addresses:
            for command, every in self.schedule:
                if first or self.passes % every == 0:
                    self.queue.append((addr, command))
        self.passes += 1

    def due(self):
        if not self.enabled:
            return False
        if self._last is None:
            return True
        return self._ticks_diff(self._ticks_ms(), self._last) >= self.gap_ms

    def step(self):
        """Ask the next question if one is due. Returns True if one was asked."""
        if not self.due():
            return False
        if not self.queue:
            self._refill()
        if not self.queue:
            return False
        addr, command = self.queue.pop(0)
        try:
            ok, raw, err, rtt = self.bus.query(addr, command)
        except Exception as ex:                  # noqa: BLE001
            ok, raw, err, rtt = 0, "", "error: " + repr(ex), 0
        fields = {"id": addr, "c": command, "ok": 1 if ok else 0, "rtt": rtt}
        if raw:
            fields["raw"] = raw
        if err:
            fields["err"] = err
        self.emit("q", **fields)
        self._last = self._ticks_ms()
        return True

    def ask_now(self, addr, command):
        """Put a question at the front of the queue (e.g. LOUT? after a set)."""
        item = (addr, command)
        if item in self.queue:
            self.queue.remove(item)
        self.queue.insert(0, item)
