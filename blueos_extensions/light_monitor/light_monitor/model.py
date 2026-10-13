"""
The state of the four lights and their controller, built from the records
the controller sends, and the events worth telling a pilot about.

Everything that reads the lights reads this: the extension's own page, the
recording's once-a-second CSV, the MAVLink names the extension publishes, and
`rov_flight_ops` (through the extension's API). One interpretation, in one
place, so a light cannot be "answering" on one screen and "not answering" on
another.

**What the four states of a light distinguish** -- the reason any of this
exists (CCR_development#34, #42, #50):

=================  =========================================================
not answering      the light's own electronics are not replying: it has lost
                   power, or the bus to it is broken
answering, dark    replies, reports the level it was told, but gives no
                   light -- a fault inside the light (driver, thermal). Only
                   visible with a power monitor (measured << expected) or on
                   camera; the fault flags may say why
restarted          its power-on count went up: it lost power and came back,
                   however briefly -- the brown-out signature
mismatch           replies with a level other than the one it was told: a
                   command lost on the bus, or a restart into a stale level
=================  =========================================================

Thresholds are deliberate: one missed answer on a shared bus is noise, three
in a row (about three seconds) is a light that has stopped answering.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import power
from .protocol import first_number, parse_info, parse_stat

#: Address -> (name, short name). Light 1 front left, 2 rear left, 3 rear
#: right, 4 front right, as numbered on Lutris (CCR_development#42: light 4
#: was moved to the forward starboard position on 24 August 2026).
LIGHT_NAMES = {
    1: ("Front left", "FL"),
    2: ("Rear left", "RL"),
    3: ("Rear right", "RR"),
    4: ("Front right", "FR"),
}

NOT_ANSWERING_AFTER = 3          # consecutive failed questions
MISMATCH_AFTER = 3               # consecutive LOUT answers off the command
STALE_AFTER_S = 15.0             # no answer at all for this long
CONTROLLER_QUIET_S = 5.0         # no line from the controller
VEHICLE_WARN_V = 10.5            # below this, warn (converter dropout ~8.6 V)
VEHICLE_HYSTERESIS_V = 0.3
#: The vehicle-side voltage at which Aaron Marburg saw the light converter's
#: output collapse on the bench (CCR_development#50, 9 October 2026).
CONVERTER_DROPOUT_V = 8.6
EVENT_COOLDOWN_S = 10.0
EVENTS_KEPT = 300


@dataclass
class Event:
    t: float
    lvl: str            # "info" | "warn"
    text: str           # for people
    short: str          # for STATUSTEXT, <= 42 characters after "Lights: "
    key: str = ""

    def as_dict(self) -> dict:
        return {"t": self.t, "lvl": self.lvl, "text": self.text}


@dataclass
class Light:
    id: int
    name: str
    short: str
    asked: int = 0
    answered: int = 0
    fails_in_row: int = 0
    answering: bool | None = None
    last_ok: float | None = None
    last_err: str = ""
    level: float | None = None
    level_at: float | None = None
    commanded: float | None = None
    mismatch_in_row: int = 0
    mismatch: bool = False
    stat: dict = field(default_factory=dict)
    stat_at: float | None = None
    power_cycles_first: int | None = None
    power_cycles: int | None = None
    restarts: int = 0
    flags: int | None = None
    standby: bool | None = None
    temp_query_c: float | None = None
    rhum_query_pct: float | None = None
    info: dict = field(default_factory=dict)
    #: Whether the "stopped answering" / "level differs" events were actually
    #: announced. A recovery is announced only if its fault was: two dropouts
    #: inside the cool-down would otherwise show "answering again" twice and
    #: "stopped answering" once.
    down_said: bool = False
    mismatch_said: bool = False


def _age(now: float, t: float | None) -> float | None:
    return None if t is None else max(0.0, now - t)


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


class Model:
    """Thread-safe: records arrive on the serial thread, the vehicle's power
    on the MAVLink thread, and snapshots are taken by the web server."""

    def __init__(self, names: dict[int, tuple[str, str]] | None = None,
                 vehicle_warn_v: float = VEHICLE_WARN_V, clock=time.time) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.started = clock()
        self.vehicle_warn_v = vehicle_warn_v
        self.lights = {i: Light(i, n, s) for i, (n, s) in (names or LIGHT_NAMES).items()}
        self.events: deque[Event] = deque(maxlen=EVENTS_KEPT)
        self._last_event: dict[str, float] = {}
        # the link
        self.link = {"source": "none", "port": "", "connected": False, "note": "",
                     "released_until": None}
        self.lines = 0
        self.records = 0
        self.bad_lines = 0
        self.last_line_at: float | None = None
        self._rate: deque[float] = deque(maxlen=400)
        # the controller
        self.hello: dict = {}
        self.hello_at: float | None = None
        self.ctl: dict = {}
        self.ctl_at: float | None = None
        self.stats: dict = {}
        self.last_seq: int | None = None
        self.last_ms: int | None = None
        self.lost_lines = 0
        self.controller_restarts = 0
        self._quiet = False
        self.legacy = False
        # power
        self.monitors: dict[str, dict] = {}
        self.vehicle: dict | None = None
        self._vehicle_low = False
        self.ardusub: dict = {}

    # ------------------------------------------------------------------
    #  events
    # ------------------------------------------------------------------

    def _event(self, now: float, lvl: str, text: str, short: str, key: str = "",
               cooldown: float = EVENT_COOLDOWN_S) -> Event | None:
        key = key or text
        last = self._last_event.get(key)
        if last is not None and now - last < cooldown:
            return None
        self._last_event[key] = now
        ev = Event(now, lvl, text, short[:42], key)
        self.events.append(ev)
        return ev

    def _label(self, lt: Light) -> str:
        return f"light {lt.id} ({lt.name.lower()})"

    # ------------------------------------------------------------------
    #  input
    # ------------------------------------------------------------------

    def set_link(self, **fields) -> list[Event]:
        out = []
        with self._lock:
            now = self._clock()
            was = self.link.get("connected")
            self.link.update(fields)
            if "connected" in fields and fields["connected"] != was:
                if fields["connected"]:
                    e = self._event(now, "info", f"Reading the light controller on "
                                    f"{self.link.get('port') or self.link.get('source')}",
                                    "controller connected", "link")
                else:
                    e = self._event(now, "warn", "The light controller's port went away "
                                    f"({fields.get('note') or 'unplugged or reset'})",
                                    "controller port lost", "link")
                if e:
                    out.append(e)
        return out

    def note_line(self, rx: float, parsed: bool) -> None:
        with self._lock:
            self.lines += 1
            if parsed:
                self.records += 1
            self.last_line_at = rx
            self._rate.append(rx)

    def note_bad_line(self) -> None:
        with self._lock:
            self.bad_lines += 1

    def ingest(self, rec: dict, rx: float | None = None) -> list[Event]:
        """One record from the controller. Returns the events it caused."""
        with self._lock:
            now = rx if rx is not None else self._clock()
            out: list[Event] = []
            self._sequence(rec, now, out)
            kind = rec.get("k")
            if rec.get("legacy"):
                self.legacy = True
            if kind == "q":
                self._answer(rec, now, out)
            elif kind == "ctl":
                self._control(rec, now, out)
            elif kind == "set":
                lt = self.lights.get(rec.get("id"))
                if lt is not None and rec.get("lvl") is not None:
                    lt.commanded = float(rec["lvl"])
            elif kind == "pwr":
                self._power(rec, now, out)
            elif kind == "hello":
                self.hello = dict(rec)
                self.hello_at = now
            elif kind == "stats":
                self.stats = dict(rec)
            elif kind == "log":
                msg = str(rec.get("msg", ""))
                e = self._event(now, "warn" if rec.get("lvl") in ("warn", "error") else "info",
                                f"Controller: {msg}", msg, "log:" + msg)
                if e:
                    out.append(e)
            return out

    def _sequence(self, rec: dict, now: float, out: list) -> None:
        n, ms = rec.get("n"), rec.get("ms")
        if not isinstance(n, int):
            return
        if self.last_seq is not None:
            restarted = n <= self.last_seq and (not isinstance(ms, int) or self.last_ms is None
                                                or ms < self.last_ms)
            if restarted:
                self.controller_restarts += 1
                e = self._event(now, "warn", "The light controller restarted "
                                f"(its line count went back to {n})",
                                "controller restarted", "restart", cooldown=2.0)
                if e:
                    out.append(e)
            elif n > self.last_seq + 1:
                self.lost_lines += n - self.last_seq - 1
        self.last_seq = n
        if isinstance(ms, int):
            self.last_ms = ms

    def _control(self, rec: dict, now: float, out: list) -> None:
        was = self.ctl.get("sig")
        self.ctl = dict(rec)
        self.ctl_at = now
        sig = rec.get("sig")
        if was == 1 and sig == 0:
            e = self._event(now, "warn", "PWM signal from the Navigator lost: the "
                            "controller has turned the lights off", "PWM signal lost", "sig")
            if e:
                out.append(e)
        elif was == 0 and sig == 1:
            e = self._event(now, "info", "PWM signal from the Navigator is back",
                            "PWM signal back", "sig")
            if e:
                out.append(e)
        if rec.get("legacy") and rec.get("cmd") is not None:
            for lt in self.lights.values():
                lt.commanded = float(rec["cmd"])
        elif rec.get("cmd") is not None:
            for lt in self.lights.values():
                if lt.commanded is None:
                    lt.commanded = float(rec["cmd"])

    def _answer(self, rec: dict, now: float, out: list) -> None:
        lt = self.lights.get(rec.get("id"))
        if lt is None:
            return
        lt.asked += 1
        cmd = rec.get("c")
        raw = rec.get("raw")
        if not rec.get("ok"):
            lt.fails_in_row += 1
            lt.last_err = str(rec.get("err") or "no answer")
            if lt.fails_in_row >= NOT_ANSWERING_AFTER and lt.answering is not False:
                lt.answering = False
                e = self._event(now, "warn", f"{self._label(lt).capitalize()} stopped "
                                f"answering ({lt.last_err})",
                                f"light {lt.id} {lt.short} not answering", f"down{lt.id}")
                lt.down_said = e is not None
                if e:
                    out.append(e)
            return
        lt.answered += 1
        lt.fails_in_row = 0
        lt.last_err = ""
        if lt.answering is False and lt.down_said:
            e = self._event(now, "info", f"{self._label(lt).capitalize()} is answering again",
                            f"light {lt.id} {lt.short} answering again", f"up{lt.id}",
                            cooldown=0.0)
            if e:
                out.append(e)
        lt.down_said = False
        lt.answering = True
        lt.last_ok = now
        if cmd == "lout":
            self._level(lt, first_number(raw), now, out)
        elif cmd == "stat":
            self._stat(lt, raw, now, out)
        elif cmd == "stby":
            v = first_number(raw)
            standby = None if v is None else v >= 1
            if standby and not lt.standby:
                e = self._event(now, "warn", f"{self._label(lt).capitalize()} is in "
                                "standby: it answers but gives no light",
                                f"light {lt.id} {lt.short} in standby", f"stby{lt.id}")
                if e:
                    out.append(e)
            lt.standby = standby
        elif cmd == "temp":
            lt.temp_query_c = first_number(raw)
        elif cmd == "rhum":
            lt.rhum_query_pct = first_number(raw)
        elif cmd == "info":
            lt.info = parse_info(raw)

    def _level(self, lt: Light, level: float | None, now: float, out: list) -> None:
        if level is None:
            return
        lt.level, lt.level_at = level, now
        if lt.commanded is None or abs(level - lt.commanded) < 0.5:
            lt.mismatch_in_row = 0
            if lt.mismatch:
                lt.mismatch = False
                if lt.mismatch_said:
                    e = self._event(now, "info", f"{self._label(lt).capitalize()} is at its "
                                    f"commanded {lt.commanded:.0f}% again",
                                    f"light {lt.id} {lt.short} level ok", f"misok{lt.id}",
                                    cooldown=0.0)
                    if e:
                        out.append(e)
                lt.mismatch_said = False
            return
        lt.mismatch_in_row += 1
        if lt.mismatch_in_row >= MISMATCH_AFTER and not lt.mismatch:
            lt.mismatch = True
            e = self._event(now, "warn", f"{self._label(lt).capitalize()} reports "
                            f"{level:.0f}% but was told {lt.commanded:.0f}%",
                            f"light {lt.id} at {level:.0f}%, told {lt.commanded:.0f}%",
                            f"mis{lt.id}")
            lt.mismatch_said = e is not None
            if e:
                out.append(e)

    def _stat(self, lt: Light, raw, now: float, out: list) -> None:
        st = parse_stat(raw)
        if not st:
            return
        lt.stat, lt.stat_at = st, now
        pc = st.get("power_cycles")
        if isinstance(pc, int):
            if lt.power_cycles_first is None:
                lt.power_cycles_first = pc
            elif lt.power_cycles is not None and pc > lt.power_cycles:
                lt.restarts += pc - lt.power_cycles
                e = self._event(now, "warn", f"{self._label(lt).capitalize()} restarted: "
                                f"its power-on count went {lt.power_cycles} -> {pc}. "
                                "It lost power, however briefly.",
                                f"light {lt.id} {lt.short} restarted (power-on {pc})",
                                f"pc{lt.id}", cooldown=0.0)
                if e:
                    out.append(e)
            lt.power_cycles = pc
        flags = st.get("flags")
        if isinstance(flags, int):
            if flags and flags != lt.flags:
                e = self._event(now, "warn", f"{self._label(lt).capitalize()} reports "
                                f"fault flags {st.get('flags_hex')} (DeepSea's code)",
                                f"light {lt.id} flags {st.get('flags_hex')}", f"flg{lt.id}")
                if e:
                    out.append(e)
            lt.flags = flags

    def _power(self, rec: dict, now: float, out: list) -> None:
        ch = str(rec.get("ch") or "bus")
        entry = {k: rec.get(k) for k in ("chip", "addr", "v", "a", "w", "t", "uv",
                                         "shunt_mv", "err")}
        entry["at"] = now
        self.monitors[ch] = entry
        if rec.get("uv"):
            e = self._event(now, "warn", f"The light supply ({ch}) dipped below its "
                            "under-voltage limit since the last reading",
                            "light supply dipped", f"uv:{ch}")
            if e:
                out.append(e)

    def set_vehicle(self, v: float | None, a: float | None, at: float | None = None,
                    source: str = "") -> list[Event]:
        out = []
        with self._lock:
            now = at if at is not None else self._clock()
            self.vehicle = {"v": v, "a": a,
                            "w": (v * a) if (v is not None and a is not None) else None,
                            "at": now, "source": source}
            if v is not None and v > 3.0:
                if v < self.vehicle_warn_v and not self._vehicle_low:
                    self._vehicle_low = True
                    e = self._event(now, "warn", f"Vehicle supply {v:.1f} V: the light "
                                    f"converter drops out near {CONVERTER_DROPOUT_V} V",
                                    f"vehicle {v:.1f} V, lights at risk", "vlow")
                    if e:
                        out.append(e)
                elif v > self.vehicle_warn_v + VEHICLE_HYSTERESIS_V and self._vehicle_low:
                    self._vehicle_low = False
                    e = self._event(now, "info", f"Vehicle supply back to {v:.1f} V",
                                    f"vehicle {v:.1f} V", "vlow", cooldown=0.0)
                    if e:
                        out.append(e)
        return out

    def set_ardusub(self, at: float | None = None, **values) -> None:
        with self._lock:
            self.ardusub.update({k: v for k, v in values.items() if v is not None})
            self.ardusub["at"] = at if at is not None else self._clock()

    def tick(self, now: float | None = None) -> list[Event]:
        """Time-driven checks: a controller gone quiet, a light gone stale."""
        out = []
        with self._lock:
            now = now if now is not None else self._clock()
            if self.link.get("connected"):
                quiet = (self.last_line_at is not None
                         and now - self.last_line_at > CONTROLLER_QUIET_S)
                if quiet and not self._quiet:
                    self._quiet = True
                    e = self._event(now, "warn", "The light controller has sent nothing "
                                    f"for {CONTROLLER_QUIET_S:.0f} s", "controller silent",
                                    "quiet")
                    if e:
                        out.append(e)
                elif not quiet and self._quiet:
                    self._quiet = False
                    e = self._event(now, "info", "The light controller is sending again",
                                    "controller sending again", "quiet", cooldown=0.0)
                    if e:
                        out.append(e)
        return out

    # ------------------------------------------------------------------
    #  output
    # ------------------------------------------------------------------

    def _rail(self) -> dict | None:
        if not self.monitors:
            return None
        label = "bus" if "bus" in self.monitors else sorted(self.monitors)[0]
        m = self.monitors[label]
        return {"label": label, **m}

    def _light_state(self, lt: Light, now: float) -> tuple[str, str]:
        age = _age(now, lt.last_ok)
        if lt.answering is False:
            return "not_answering", f"Not answering ({lt.last_err})"
        if lt.answering is None:
            return "unknown", "No answer yet"
        if age is not None and age > STALE_AFTER_S:
            return "stale", f"No answer for {age:.0f} s"
        if lt.flags:
            return "fault", f"Fault flags {lt.stat.get('flags_hex')}"
        if lt.standby:
            return "standby", "Standby"
        if lt.mismatch:
            return "mismatch", (f"At {lt.level:.0f}%, told {lt.commanded:.0f}%"
                                if lt.level is not None and lt.commanded is not None
                                else "Level differs")
        if lt.level is not None and lt.level > 0:
            return "on", "Answering"
        return "off", "Answering · off"

    def snapshot(self, now: float | None = None, recording: dict | None = None,
                 mavlink: dict | None = None, extension: dict | None = None) -> dict:
        with self._lock:
            now = now if now is not None else self._clock()
            rail = self._rail()
            rail_v = rail.get("v") if rail else None
            lights = []
            exp_total = 0.0
            for lt in self.lights.values():
                state, text = self._light_state(lt, now)
                level = lt.level if lt.answering is not False else None
                ew = power.expected_w(level) if level is not None else None
                if ew is not None:
                    exp_total += ew
                st = lt.stat
                lights.append({
                    "id": lt.id, "name": lt.name, "short": lt.short,
                    "state": state, "state_text": text,
                    "answering": lt.answering, "last_answer_age_s": _r(_age(now, lt.last_ok), 1),
                    "fails_in_row": lt.fails_in_row, "last_error": lt.last_err,
                    "asked": lt.asked, "answered": lt.answered,
                    "level_pct": lt.level, "commanded_pct": lt.commanded,
                    "level_age_s": _r(_age(now, lt.level_at), 1),
                    "power_cycles": lt.power_cycles,
                    "power_cycles_session_start": lt.power_cycles_first,
                    "restarts_this_session": lt.restarts,
                    "runtime_h": st.get("runtime_h"), "max_temp_c": st.get("max_temp_c"),
                    "temp1_c": st.get("temp1_c"), "temp2_c": st.get("temp2_c"),
                    "temp3_c": st.get("temp3_c"), "humidity_pct": st.get("humidity_pct"),
                    "flags": lt.flags, "flags_hex": st.get("flags_hex"),
                    "stat_columns": st.get("columns", []), "stat_by": st.get("by"),
                    "stat_age_s": _r(_age(now, lt.stat_at), 1),
                    "standby": lt.standby, "temp_query_c": lt.temp_query_c,
                    "rhum_query_pct": lt.rhum_query_pct, "info": lt.info,
                    "expected_w": _r(ew, 1),
                    "expected_a": (_r(power.expected_a(level, rail_v), 2)
                                   if level is not None else None),
                })
            veh = dict(self.vehicle) if self.vehicle else None
            if veh:
                veh["age_s"] = _r(_age(now, veh.pop("at")), 1)
            ard = dict(self.ardusub)
            if ard:
                ard["age_s"] = _r(_age(now, ard.pop("at", None)), 1)
            monitors = []
            for label, m in sorted(self.monitors.items()):
                d = {"label": label, **{k: v for k, v in m.items() if k != "at"}}
                d["age_s"] = _r(_age(now, m.get("at")), 1)
                monitors.append(d)
            recent = [t for t in self._rate if now - t <= 10.0]
            hello = self.hello
            ctl = self.ctl
            return {
                "schema": "light_monitor.state/1",
                "now": now,
                "extension": extension or {},
                "link": {**self.link, "lines": self.lines, "records": self.records,
                         "bad_lines": self.bad_lines, "lost_lines": self.lost_lines,
                         "last_line_age_s": _r(_age(now, self.last_line_at), 1),
                         "lines_per_s": round(len(recent) / 10.0, 1),
                         "legacy_firmware": self.legacy},
                "controller": {
                    "fw": hello.get("fw"), "board": hello.get("board"),
                    "machine": hello.get("machine"), "micropython": hello.get("mpy"),
                    "port_mode": hello.get("port"), "datalink_note": hello.get("datalink"),
                    "reset_cause": hello.get("reset"), "poll": hello.get("poll"),
                    "resync": hello.get("resync"), "power_notes": hello.get("power_notes"),
                    "uptime_s": _r(self.last_ms / 1000.0, 1) if self.last_ms is not None else None,
                    "restarts": self.controller_restarts,
                    "pwm_us": ctl.get("pwm"), "signal": (None if ctl.get("sig") is None
                                                         else bool(ctl.get("sig"))),
                    "commanded_pct": ctl.get("cmd"), "target_pct": ctl.get("tgt"),
                    "pwm_rejected": ctl.get("rej"),
                    "ctl_age_s": _r(_age(now, self.ctl_at), 1),
                    "stats": {k: self.stats.get(k) for k in
                              ("sent", "drop", "unread", "asked", "answered", "failed",
                               "loop_max", "mem", "passes") if k in self.stats},
                },
                "lights": lights,
                "power": {
                    "monitors": monitors,
                    "rail": ({k: rail.get(k) for k in ("label", "chip", "v", "a", "w", "uv")}
                             if rail else None),
                    "vehicle": veh,
                    "ardusub": ard,
                    "lights_expected_w": round(exp_total, 1),
                    "lights_expected_a": round(exp_total / (rail_v if rail_v and rail_v > 5
                                                             else power.NOMINAL_RAIL_V), 2),
                    "vehicle_warn_v": self.vehicle_warn_v,
                    "converter_dropout_v": CONVERTER_DROPOUT_V,
                    "full_w_per_light": power.FULL_W,
                },
                "mavlink": mavlink or {},
                "recording": recording or {},
                "events": [e.as_dict() for e in list(self.events)[-60:]],
            }

    # -- MAVLink ---------------------------------------------------------

    def nvf_values(self, now: float | None = None) -> dict[str, float]:
        """The NAMED_VALUE_FLOATs to publish: only what is known."""
        snap = self.snapshot(now)
        out: dict[str, float] = {}

        def put(name, value):
            if value is not None:
                out[name] = float(value)

        for lt in snap["lights"]:
            n = lt["id"]
            if lt["answering"] is not None:
                put(f"LT{n}_OK", 1.0 if lt["answering"] and lt["state"] != "stale" else 0.0)
            put(f"LT{n}_LVL", lt["level_pct"])
            put(f"LT{n}_PC", lt["power_cycles"])
            put(f"LT{n}_T2", lt["temp2_c"])
            put(f"LT{n}_T3", lt["temp3_c"])
            put(f"LT{n}_RH", lt["humidity_pct"])
            put(f"LT{n}_FLG", lt["flags"])
            put(f"LT{n}_EW", lt["expected_w"])
        c = snap["controller"]
        put("LT_CMD", c["commanded_pct"])
        put("LT_PWM", c["pwm_us"])
        if c["signal"] is not None:
            put("LT_SIG", 1.0 if c["signal"] else 0.0)
        age = snap["link"]["last_line_age_s"]
        put("LT_LINK", 1.0 if (age is not None and age < CONTROLLER_QUIET_S) else 0.0)
        put("LT_UP", c["uptime_s"])
        put("LT_LOST", snap["link"]["lost_lines"])
        rail = snap["power"]["rail"]
        if rail:
            put("LT_V", rail.get("v"))
            put("LT_A", rail.get("a"))
            put("LT_W", rail.get("w"))
        put("LT_EW", snap["power"]["lights_expected_w"])
        return out

    # -- the once-a-second CSV -------------------------------------------

    def csv_row(self, now: float | None = None) -> dict:
        snap = self.snapshot(now)
        t = snap["now"]
        c = snap["controller"]
        row = {"utc": datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds"),
               "pi_epoch": round(t, 3),
               "link": 1 if snap["link"]["connected"] else 0,
               "source": snap["link"]["source"],
               "controller_uptime_s": c["uptime_s"], "pwm_us": c["pwm_us"],
               "pwm_signal": None if c["signal"] is None else int(c["signal"]),
               "commanded_pct": c["commanded_pct"]}
        for lt in snap["lights"]:
            p = f"L{lt['id']}_"
            row.update({
                p + "state": lt["state"],
                p + "answering": None if lt["answering"] is None else int(lt["answering"]),
                p + "level_pct": lt["level_pct"], p + "power_cycles": lt["power_cycles"],
                p + "restarts": lt["restarts_this_session"],
                p + "temp1_c": lt["temp1_c"], p + "temp2_c": lt["temp2_c"],
                p + "temp3_c": lt["temp3_c"], p + "max_temp_c": lt["max_temp_c"],
                p + "humidity_pct": lt["humidity_pct"], p + "flags": lt["flags"],
                p + "standby": None if lt["standby"] is None else int(lt["standby"]),
                p + "expected_w": lt["expected_w"],
                p + "last_answer_age_s": lt["last_answer_age_s"],
            })
        pw = snap["power"]
        rail, veh, ard = pw["rail"] or {}, pw["vehicle"] or {}, pw["ardusub"] or {}
        row.update({"rail_v": rail.get("v"), "rail_a": rail.get("a"), "rail_w": rail.get("w"),
                    "rail_uv": rail.get("uv"), "vehicle_v": veh.get("v"),
                    "vehicle_a": veh.get("a"), "vehicle_w": veh.get("w"),
                    "ardusub_lights_pct": ard.get("lights_pct"),
                    "ardusub_pilot_gain_pct": ard.get("pilot_gain_pct"),
                    "lights_expected_w": pw["lights_expected_w"],
                    "lost_lines": snap["link"]["lost_lines"]})
        return row


def csv_columns(names: dict[int, tuple[str, str]] | None = None) -> list[str]:
    """The CSV header, in order, for the lights in `names`."""
    cols = ["utc", "pi_epoch", "link", "source", "controller_uptime_s", "pwm_us",
            "pwm_signal", "commanded_pct"]
    per = ["state", "answering", "level_pct", "power_cycles", "restarts", "temp1_c",
           "temp2_c", "temp3_c", "max_temp_c", "humidity_pct", "flags", "standby",
           "expected_w", "last_answer_age_s"]
    for i in (names or LIGHT_NAMES):
        cols += [f"L{i}_{p}" for p in per]
    cols += ["rail_v", "rail_a", "rail_w", "rail_uv", "vehicle_v", "vehicle_a",
             "vehicle_w", "ardusub_lights_pct", "ardusub_pilot_gain_pct",
             "lights_expected_w", "lost_lines"]
    return cols
