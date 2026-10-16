"""
A pretend light controller, speaking ccr.lights/1, for working without Lutris.

`python -m light_monitor --simulate` runs the extension against this instead
of a USB port: the page, the recording, the MAVLink publishing (pointed at a
real vehicle or not) and `rov_flight_ops`'s Lights tab can all be developed
and shown on a laptop.

It plays a loop of what has actually been seen on Lutris:

* the pilot steps the lights 0 -> 50 -> 100 -> 80 -> 0 over about two minutes;
* light 4 at 90 % and above drops out every so often for about three seconds
  -- stops answering -- and comes back with its power-on count up by one,
  the brown-out signature (CCR_development#42, #50);
* STAT? answers in the eight-column form Lutris's LSL-2075s use, with the
  power-on counts Aaron read off them on 13 October 2026 (112, 147, 109, 30)
  and TEMP?/RHUM? reading 0.0, as they do.

`--sim-monitor` adds a power monitor on the 24 V rail, which Lutris does not
have: its current drops by a light's worth while light 4 is dark.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable

from . import power

STEPS = ((0, 15.0), (50, 25.0), (100, 45.0), (80, 25.0), (0, 10.0))
POWER_ON_COUNTS = {1: 112, 2: 147, 3: 109, 4: 30}
RUNTIME_H = {1: 93.7, 2: 100.4, 3: 94.0, 4: 92.9}
SERIALS = {1: "12-049-7626", 2: "12-049-7627", 3: "12-049-7629", 4: "12-049-7628"}


class SimulatedController:

    def __init__(self, on_line: Callable, *, monitor: bool = False, seed: int = 7,
                 clock=time.time, speed: float = 1.0) -> None:
        self.on_line = on_line
        self.monitor = monitor
        self.rng = random.Random(seed)
        self.clock = clock
        self.speed = speed
        self.n = 0
        self.t0 = clock()
        self.level = 0
        self.dark_until: dict[int, float] = {}
        self.power_on = dict(POWER_ON_COUNTS)
        self._queue: list[tuple[int, str]] = []
        self._pass = 0
        self._stop = threading.Event()

    # -- one line --------------------------------------------------------

    def _emit(self, kind: str, **fields) -> None:
        self.n += 1
        ms = int((self.clock() - self.t0) * 1000 * self.speed)
        self.on_line(json.dumps({"k": kind, "n": self.n, "ms": ms, **fields}),
                     "simulator", self.clock())

    def hello(self) -> None:
        self._emit("hello", proto="ccr.lights/1", fw="simulator", board="simulated",
                   machine="Simulated QT Py", mpy="1.27.0", reset=1, port="data",
                   datalink="", lights=[1, 2, 3, 4],
                   power=([{"addr": 64, "chip": "INA238", "shunt_ohms": 0.002}]
                          if self.monitor else []),
                   power_notes=[] if self.monitor else ["no power monitor on I2C"],
                   poll=True, resync=False, deadband=5)

    # -- the script ------------------------------------------------------

    def target(self, t: float) -> int:
        period = sum(d for _l, d in STEPS)
        x = t % period
        for level, d in STEPS:
            if x < d:
                return level
            x -= d
        return 0

    def _answer(self, addr: int, cmd: str, now: float) -> tuple[int, str, str, int]:
        if now < self.dark_until.get(addr, 0.0):
            return 0, "", "timeout", 150
        rtt = self.rng.randint(18, 40)
        if cmd == "lout":
            return 1, f"{self.level}\r\n", "", rtt
        if cmd == "stat":
            t2 = 22.0 + self.level / 25 + self.rng.random() * 0.4
            t3 = 20.0 + self.level / 40 + self.rng.random() * 0.4
            raw = ("RunTime\tPrCycle\tMaxTemp\tTemp1\tTemp2\tTemp3\tHumid\tFLAGS\r\n"
                   f"{RUNTIME_H[addr]:.1f}hr\t{self.power_on[addr]}\t87.0C\t0.0C\t"
                   f"{t2:.1f}C\t{t3:.1f}C\t0%\t0x0000\r\n\r\n")
            return 1, raw, "", rtt + 70
        if cmd in ("temp", "rhum"):
            return 1, "0.0\r\n", "", rtt
        if cmd == "stby":
            return 1, "0\r\n", "", rtt
        if cmd == "info":
            return 1, f"{addr:03d},LSL-2075,{SERIALS[addr]},0A,1.1.0,DW00,MED,SER\r\n", "", rtt
        return 0, "?\r\n", "nak", rtt

    def _refill(self) -> None:
        sched = (("lout", 1), ("stat", 3), ("stby", 10), ("temp", 60), ("rhum", 60),
                 ("info", 600))
        for addr in (1, 2, 3, 4):
            for cmd, every in sched:
                if self._pass == 0 or self._pass % every == 0:
                    self._queue.append((addr, cmd))
        self._pass += 1

    def step(self, now: float) -> None:
        t = (now - self.t0) * self.speed
        tgt = self.target(t)
        if abs(tgt - self.level) >= 5:
            self.level = tgt
            for addr in (1, 2, 3, 4):
                self._emit("set", id=addr, lvl=tgt, why="pwm")
        # light 4 browns out now and then when driven hard
        if self.level >= 90 and now >= self.dark_until.get(4, 0.0) and self.rng.random() < 0.004:
            self.dark_until[4] = now + 3.0
            self.power_on[4] += 1
        if not self._queue:
            self._refill()
        addr, cmd = self._queue.pop(0)
        ok, raw, err, rtt = self._answer(addr, cmd, now)
        fields = {"id": addr, "c": cmd, "ok": ok, "rtt": rtt}
        if raw:
            fields["raw"] = raw
        if err:
            fields["err"] = err
        self._emit("q", **fields)

    def run(self) -> None:
        self.hello()
        last_ctl = last_pwr = last_hello = last_stats = 0.0
        while not self._stop.is_set():
            now = self.clock()
            self.step(now)
            if now - last_ctl >= 0.5:
                pwm = 1100 + self.level * 8
                self._emit("ctl", pwm=pwm, age=12, sig=1, cmd=self.level, tgt=self.level, rej=0)
                last_ctl = now
            if self.monitor and now - last_pwr >= 0.25:
                lit = [a for a in (1, 2, 3, 4) if now >= self.dark_until.get(a, 0.0)]
                w = sum(power.expected_w(self.level) or 0 for _a in lit)
                v = 24.2 - w / 400.0 + self.rng.random() * 0.05
                self._emit("pwr", ch="bus", chip="INA238", addr=64, v=round(v, 3),
                           a=round(w / v, 3), w=round(w, 1), t=31.0, uv=0, shunt_mv=0.0)
                last_pwr = now
            if now - last_hello >= 60.0:
                if last_hello:
                    self.hello()
                last_hello = now
            if now - last_stats >= 10.0:
                self._emit("stats", sent=self.n, drop=0, unread=0, asked=self.n, answered=self.n,
                           failed=0, loop_max=180, mem=120000, passes=self._pass)
                last_stats = now
            self._stop.wait(0.08)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, daemon=True, name="simulator")
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
