"""
The simulated BlueOS Light Monitor: the four DeepSea lights, as the extension
would report them.

**Answered at the extension's API, not above it.** `SimLights.fetch` stands in
for the HTTP GET the program's own `LightsClient` makes, and returns the
extension's own documents -- `light_monitor.state/1` from ``/api/v1/state``,
an empty page from ``/api/v1/records`` -- so the Lights tab, the Navigation row
and the Monitoring board all read the simulated lights through the real
reader, exactly as they would read Lutris.

**Healthy is the 13 October 2026 bench.** Each light's run time, power-on
count, highest recorded temperature and temperatures are from the STAT?
answers Aaron Marburg posted that day (CCR_development#50): temperature 1 and
humidity really do read 0 on these lights. The level follows ArduSub's Lights1
on the simulated vehicle: 1700 µs, 75 %. Expected draw is 98 W at full output
on DeepSea's dimming curve, as the extension works it out.

**The faults are the ones the lights have actually had** (CCR_development#34,
#42, #50): a supply brown-out under thruster load flickering all four, light 4
cutting out at full output, a garbled RS-485 bus, two lights that went dark
and would not come back, the PWM signal lost or on the wrong channel, a
controller gone quiet, and a light reporting fault flags. And like everything
else on the Pi, the extension stops answering during the Pi outage.
"""

from __future__ import annotations

import math
from urllib.parse import parse_qs, urlsplit

from ..lights import DEFAULT_PORT, LightsClient
from .nav import SIM_HOST
from .schedule import FAULT_S, LIGHTS
from .world import SimWorld

#: Address -> (name, short name, run time h, power-ons, highest °C, t2, t3,
#: serial). From the 13 October 2026 STAT? answers (CCR_development#50).
LIGHTS_AS_FOUND = {
    1: ("Front left", "FL", 93.7, 112, 87.0, 24.0, 20.6, "12-049-000101"),
    2: ("Rear left", "RL", 100.4, 147, 91.0, 23.1, 20.3, "12-049-000102"),
    3: ("Rear right", "RR", 94.0, 109, 87.0, 24.0, 21.1, "12-049-000103"),
    4: ("Front right", "FR", 92.9, 30, 87.0, 24.0, 21.1, "12-049-000097"),
}
LIGHT_FIRMWARE = "1.1.1686"
#: One light at LOUT 100, watts, and the light supply for amps.
FULL_W = 98.0
RAIL_V = 24.0
#: DeepSea's dimming curve at a few levels (per-cent of full output), from
#: the extension's data/lsl_seasense_dimming.csv; linear between them.
DIMMING = ((0, 0.0), (25, 6.636), (50, 12.632), (75, 31.612), (90, 65.295),
           (100, 100.0))
#: ArduSub's Lights1 on the simulated vehicle, and the level it makes.
PWM_US = 1700
LEVEL = 75.0
#: The vehicle-side voltage the light converter drops out at (#50).
DROPOUT_V = 8.6
WARN_V = 10.5


def output_pct(level: float) -> float:
    for (a, pa), (b, pb) in zip(DIMMING, DIMMING[1:]):
        if a <= level <= b:
            return pa + (pb - pa) * (level - a) / (b - a)
    return 0.0 if level <= 0 else 100.0


def expected_w(level: float | None) -> float | None:
    return None if level is None else round(FULL_W * output_pct(level) / 100.0, 1)


def _fault(world: SimWorld, t: float):
    f, p = world.schedule.fault(LIGHTS, t)
    return (f.key if f else None), p


class SimLights:
    """The extension's API on the simulated vehicle."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world
        self.started = world.t()

    def client(self) -> LightsClient:
        return LightsClient(SIM_HOST, DEFAULT_PORT, fetch=self.fetch)

    def fetch(self, url: str):
        t = self.world.t()
        if self.world.pi_silent(t):
            # The extension lives on the Pi, which has dropped off the tether.
            raise OSError("timed out")
        parts = urlsplit(url)
        if parts.path == "/api/v1/state":
            return self.state(t)
        if parts.path == "/api/v1/records":
            after = int((parse_qs(parts.query).get("after") or ["0"])[0])
            return {"records": [], "newest": after}
        raise OSError("HTTP 404")

    # ------------------------------------------------------------------

    def state(self, t: float) -> dict:
        key, p = _fault(self.world, t)
        v = self.world.vehicle(t)
        now = self.world.wall(t)
        into = p * FAULT_S                          # seconds into this fault
        signal, pwm = True, PWM_US
        commanded = LEVEL
        volts, amps = v.volts, v.amps
        events: list[tuple[float, str, str]] = []
        line_age = 0.1

        if key == "pwm_lost":
            signal, pwm, commanded = False, None, 0.0
            events.append((now - into + 1, "warn", "PWM signal from the Navigator "
                           "lost: the controller has turned the lights off"))
        elif key == "wrong_channel":
            pwm, commanded = 1100, 0.0
        elif key == "light4_intermittent":
            commanded = 100.0
        elif key == "brownout":
            commanded = 100.0
            # Thruster load at full gain: the supply sags in pulses.
            dip = max(0.0, math.sin(into * 1.3)) ** 6
            volts = v.volts - (v.volts - 8.2) * dip
            amps = v.amps + 45.0 * dip
            if into > 2:
                events.append((now - into + 2, "warn", f"Vehicle supply {8.4:.1f} V: "
                               f"the light converter drops out near {DROPOUT_V} V"))
        elif key == "controller_silent":
            line_age = into + 0.1
            if into > 5:
                events.append((now - into + 5, "warn",
                               "The light controller has sent nothing for 5 s"))

        lights = []
        exp_total = 0.0
        for lid, (name, short, run_h, pc0, tmax, t2, t3, serial) in LIGHTS_AS_FOUND.items():
            lt = self._healthy(lid, name, short, run_h, pc0, tmax, t2, t3, serial,
                               commanded, t)
            self._apply(key, into, lt, events, now)
            if lt["answering"] is not False and lt["level_pct"] is not None:
                lt["expected_w"] = expected_w(lt["level_pct"])
                lt["expected_a"] = round(lt["expected_w"] / RAIL_V, 2)
                exp_total += lt["expected_w"]
            lt["state"], lt["state_text"] = _state(lt)
            lights.append(lt)

        if key == "controller_silent" and line_age > 15:
            for lt in lights:
                lt["state"], lt["state_text"] = "stale", f"No answer for {line_age:.0f} s"

        return {
            "schema": "light_monitor.state/1",
            "now": now,
            "extension": {"version": "0.3.0 (simulated)", "mode": "serial",
                          "started": self.world.wall(self.started)},
            "link": {"source": "serial", "port": "/dev/ttyACM1 (telemetry)",
                     "connected": True, "note": "", "released_until": None,
                     "lines": int(12 * (t - self.started)),
                     "records": int(12 * (t - self.started)), "bad_lines": 0,
                     "lost_lines": 3 if key == "bus_garbled" else 0,
                     "last_line_age_s": round(line_age, 1),
                     "lines_per_s": 0.0 if line_age > 2 else 12.1,
                     "legacy_firmware": False},
            "controller": {
                "fw": "ccr.lights/1 0.3.0", "board": "qtpy-rp2040",
                "machine": "Adafruit QT Py RP2040", "micropython": "1.24.1",
                "port_mode": "cdc", "datalink_note": "", "reset_cause": "power-on",
                "poll": 1.0, "resync": 30, "power_notes": "no monitor fitted",
                "uptime_s": round(t - self.started + 1800.0, 1), "restarts": 0,
                "pwm_us": pwm, "signal": signal, "commanded_pct": commanded,
                "target_pct": commanded, "pwm_rejected": 0,
                "ctl_age_s": round(line_age, 1),
                "stats": {"sent": int(12 * (t - self.started)), "drop": 0,
                          "asked": sum(lt["asked"] for lt in lights),
                          "answered": sum(lt["answered"] for lt in lights),
                          "failed": sum(lt["asked"] - lt["answered"] for lt in lights)},
            },
            "lights": lights,
            "power": {
                "monitors": [], "rail": None,
                "vehicle": {"v": round(volts, 2), "a": round(amps, 2),
                            "w": round(volts * amps, 1), "age_s": 0.4,
                            "source": "SYS_STATUS"},
                "ardusub": {"lights_pct": (PWM_US - 1100) / 8.0,
                            "pilot_gain_pct": 50.0 if key != "brownout" else 100.0,
                            "age_s": 0.6},
                "lights_expected_w": round(exp_total, 1),
                "lights_expected_a": round(exp_total / RAIL_V, 2),
                "vehicle_warn_v": WARN_V, "converter_dropout_v": DROPOUT_V,
                "full_w_per_light": FULL_W,
            },
            "mavlink": {"sending": True, "component": 191},
            "recording": {},
            "events": [{"t": a, "lvl": b, "text": c} for a, b, c in sorted(events)][-60:],
        }

    def _healthy(self, lid, name, short, run_h, pc0, tmax, t2, t3, serial,
                 commanded, t) -> dict:
        warm = 0.4 * math.sin(t / 40.0 + lid)
        level = commanded
        asked = int(5 * (t - self.started)) + 40
        return {
            "id": lid, "name": name, "short": short,
            "answering": True, "last_answer_age_s": 0.4, "fails_in_row": 0,
            "last_error": "", "asked": asked, "answered": asked,
            "level_pct": level, "commanded_pct": commanded, "level_age_s": 0.6,
            "power_cycles": pc0, "power_cycles_session_start": pc0,
            "restarts_this_session": 0,
            "runtime_h": round(run_h + (t - self.started) / 3600.0, 1),
            "max_temp_c": tmax, "temp1_c": 0.0,
            "temp2_c": round(t2 + (6.0 if level else 0.0) + warm, 1),
            "temp3_c": round(t3 + (4.0 if level else 0.0) + warm, 1),
            "humidity_pct": 0.0, "flags": 0, "flags_hex": "0x0000",
            "stat_columns": [], "stat_by": "telemetry", "stat_age_s": 2.1,
            "standby": False, "temp_query_c": 0.0, "rhum_query_pct": 0.0,
            "info": {"address": f"{lid:03d}", "model": "LSL-2075", "serial": serial,
                     "firmware": LIGHT_FIRMWARE},
            "expected_w": None, "expected_a": None,
        }

    def _apply(self, key, into, lt, events, now) -> None:
        """One fault, on one light."""
        lid = lt["id"]
        if key == "brownout":
            # All four drop together for a second or two in each sag, come
            # back on their own, and count a power-on each time.
            sags = int(into / (2 * math.pi / 1.3))
            if max(0.0, math.sin(into * 1.3)) ** 6 > 0.6:
                _silent(lt, "timeout")
            lt["power_cycles"] += sags
            lt["restarts_this_session"] = sags
            if sags and lid == 1:
                events.append((now - 1, "warn", "Light 1 (front left) restarted: its "
                               "power-on count went up. It lost power, however briefly."))
        elif key == "light4_intermittent" and lid == 4:
            # Light 4, the one sent back to DeepSea: cuts out at full output.
            drops = int(into / 7)
            lt["power_cycles"] += drops
            lt["restarts_this_session"] = drops
            if (into % 7) < 2.5:
                _silent(lt, "timeout")
            if drops:
                events.append((now - (into % 7), "warn",
                               f"Light 4 (front right) restarted: its power-on count "
                               f"went {lt['power_cycles'] - 1} -> {lt['power_cycles']}. "
                               f"It lost power, however briefly."))
        elif key == "bus_garbled":
            # Contention and reflections on the RS-485 star: answers go missing
            # and a level command can be lost.
            lost = int(lt["asked"] * 0.28)
            lt["answered"] = lt["asked"] - lost
            lt["fails_in_row"] = int(into * 3 + lid) % 3
            lt["last_error"] = "checksum" if lt["fails_in_row"] else ""
            if lid == 2:
                lt["level_pct"] = 50.0
                lt["_mismatch"] = True
                events.append((now - into + 4, "warn",
                               f"Light 2 (rear left) reports 50% but was told "
                               f"{lt['commanded_pct']:.0f}%"))
        elif key == "lights_3_4_dark" and lid in (3, 4):
            lt["level_pct"] = 0.0
            lt["_mismatch"] = True
            if into > 3:
                events.append((now - into + 3, "warn",
                               f"Light {lid} ({lt['name'].lower()}) reports 0% but "
                               f"was told {lt['commanded_pct']:.0f}%"))
        elif key == "pwm_lost" or key == "wrong_channel":
            lt["level_pct"] = 0.0
        elif key == "fault_flags" and lid == 2:
            lt["flags"], lt["flags_hex"] = 4, "0x0004"
            lt["temp2_c"], lt["temp3_c"] = 71.5, 64.0
            events.append((now - into + 1, "warn", "Light 2 (rear left) reports fault "
                           "flags 0x0004 (DeepSea's code)"))
        elif key == "controller_silent":
            lt["last_answer_age_s"] = round(into + 0.4, 1)
            lt["level_age_s"] = round(into + 0.6, 1)


def _silent(lt: dict, why: str) -> None:
    lt["answering"] = False
    lt["fails_in_row"] = 3
    lt["last_error"] = why
    lt["level_pct"] = None
    lt["last_answer_age_s"] = 2.4


def _state(lt: dict) -> tuple[str, str]:
    """The extension's own ranking (`model.Model._light_state`)."""
    if lt["answering"] is False:
        return "not_answering", f"Not answering ({lt['last_error']})"
    if lt["flags"]:
        return "fault", f"Fault flags {lt['flags_hex']}"
    if lt["standby"]:
        return "standby", "Standby"
    if lt.pop("_mismatch", False):
        return "mismatch", f"At {lt['level_pct']:.0f}%, told {lt['commanded_pct']:.0f}%"
    if lt["level_pct"]:
        return "on", "Answering"
    return "off", "Answering · off"
