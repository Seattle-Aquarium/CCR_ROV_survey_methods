"""
The vehicle's parameters against its reference file, for the Parameters tab.

Two lists come out of `compare`:

* **the key parameters** -- navigation, positioning, the DVL, GPS, the EKF,
  the origin, failsafes, lights and pilot gain -- in groups, each with the
  vehicle's value, the reference's, and whether they agree; and
* **every other difference**, which is empty when the rest of the vehicle
  matches the file.

Some parameters are left out of both because the autopilot or the ground
station rewrites them by itself (`DYNAMIC`): across the 24 flights recorded
between 14 September and 15 October 2026 those were the only ones that moved
without anybody touching them. The origin is shown but never offered for
"set to reference": it belongs to the site being surveyed, not to the
vehicle.

Values are compared as the autopilot holds them, as float32s, so 0.3 in the
file and 0.30000001192092896 from the vehicle are the same value.

Nothing here touches Tk or the network.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from . import paramfile

#: Rewritten by the autopilot or the ground station, flight after flight.
DYNAMIC_EXACT = frozenset(paramfile.OMIT) | {"COMPASS_DEC"}
DYNAMIC_PATTERNS = (
    re.compile(r"^STAT_"),             # run time, flight time, boot count, reset
    re.compile(r"^BARO\d_GND_PRESS$"),  # ground pressure, set at every boot
    re.compile(r"^SR0_"),              # stream rates, set by the GCS on connect
)

#: Belong to the site, not the vehicle: shown, never set from the file.
PER_SITE = frozenset({"ORIGIN_LAT", "ORIGIN_LON", "ORIGIN_ALT",
                      "AHRS_ORIG_LAT", "AHRS_ORIG_LON", "AHRS_ORIG_ALT"})

#: ArduSub's servo functions for the two light channels.
LIGHTS1, LIGHTS2 = 181, 182

MATCH, DIFFERS, MISSING, SITE, DERIVED = "match", "differs", "missing", "site", "derived"


def is_dynamic(name: str) -> bool:
    return name in DYNAMIC_EXACT or any(p.match(name) for p in DYNAMIC_PATTERNS)


def float32(v: float) -> float:
    return struct.unpack("f", struct.pack("f", float(v)))[0]


def same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    fa, fb = float32(a), float32(b)
    return abs(fa - fb) <= max(1e-6, abs(fb) * 1e-6)


def text(v: float | None, name: str = "") -> str:
    if v is None:
        return "—"
    if name in PER_SITE and name.endswith(("_LAT", "_LON")):
        return f"{v:.6f}"
    if float(v) == int(v) and abs(v) < 1e9:
        return str(int(v))
    return f"{float32(v):.7g}"


# --------------------------------------------------------------------------
#  the key parameters
# --------------------------------------------------------------------------

#: What a few enumerated values mean, so the operator reads "EKF3" not "3".
MEANINGS: dict[str, dict[float, str]] = {
    "AHRS_EKF_TYPE": {2: "EKF2", 3: "EKF3", 10: "SITL"},
    "GPS_TYPE": {0: "none", 1: "auto", 14: "MAVLink"},
    "VISO_TYPE": {0: "none", 1: "MAVLink", 2: "IntelT265", 3: "VOXL"},
    "RNGFND1_TYPE": {0: "none", 10: "MAVLink"},
    "RNGFND1_ORIENT": {25: "down", 24: "up", 0: "forward"},
    "BATT_MONITOR": {0: "off", 4: "analog V + I"},
    "FS_GCS_ENABLE": {0: "off", 1: "warn", 2: "disarm", 3: "depth hold",
                      4: "surface"},
    "FS_PILOT_INPUT": {0: "off", 1: "warn", 2: "disarm"},
    "FS_LEAK_ENABLE": {0: "off", 1: "warn", 2: "surface"},
    "FS_EKF_ACTION": {0: "off", 1: "warn", 2: "disarm"},
    "FS_PRESS_ENABLE": {0: "off", 1: "warn"},
    "FS_TEMP_ENABLE": {0: "off", 1: "warn"},
    "SCR_ENABLE": {0: "off", 1: "Lua on"},
    "LOG_DISARMED": {0: "armed only", 1: "always"},
    "EK3_SRC_OPTIONS": {0: "—", 1: "fuse all velocities"},
}


def meaning(name: str, value: float | None) -> str:
    if value is None:
        return ""
    if re.match(r"^EK3_SRC\d_", name):
        from .nav.profiles import source_name
        named = source_name(name, value)            # "6 (ExternalNav)"
        return named[named.find("(") + 1:-1] if "(" in named else ""
    if name.endswith("_FUNCTION") and name.startswith("SERVO"):
        return {LIGHTS1: "Lights 1", LIGHTS2: "Lights 2", 0: "disabled",
                7: "mount tilt", 59: "RCIN9"}.get(int(value), "")
    return MEANINGS.get(name, {}).get(value, "")


#: (group, [(parameter, what it is)]). Order is the order on the page.
KEY_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Estimator (EKF3)", [
        ("AHRS_EKF_TYPE", "which estimator flies"),
        ("EK3_ENABLE", ""),
        ("EK3_SRC1_POSXY", "horizontal position"),
        ("EK3_SRC1_VELXY", "horizontal velocity"),
        ("EK3_SRC1_POSZ", "vertical position"),
        ("EK3_SRC1_VELZ", "vertical velocity"),
        ("EK3_SRC1_YAW", "heading"),
        ("EK3_SRC_OPTIONS", ""),
        ("EK3_GPS_CHECK", "GPS pre-arm checks"),
        ("EK3_POSNE_M_NSE", "position noise, m"),
        ("EK3_VELNE_M_NSE", "velocity noise, m/s"),
        ("EK3_POS_I_GATE", "position innovation gate"),
    ]),
    ("DVL", [
        ("VISO_TYPE", "visual odometry input"),
        ("VISO_POS_X", "mount, forward m"),
        ("VISO_POS_Y", "mount, right m"),
        ("VISO_POS_Z", "mount, down m"),
        ("VISO_ORIENT", ""),
        ("VISO_DELAY_MS", "latency"),
        ("VISO_VEL_M_NSE", "velocity noise"),
        ("RNGFND1_TYPE", "altitude input"),
        ("RNGFND1_ORIENT", ""),
        ("RNGFND1_MIN_CM", ""),
        ("RNGFND1_MAX_CM", ""),
    ]),
    ("GPS (UGPS)", [
        ("GPS_TYPE", "position input"),
        ("GPS_DELAY_MS", "latency"),
        ("GPS_POS1_X", "locator, forward m"),
        ("GPS_POS1_Y", "locator, right m"),
        ("GPS_POS1_Z", "locator, down m"),
        ("AHRS_GPS_USE", ""),
        ("AHRS_GPS_MINSATS", ""),
    ]),
    ("Origin", [
        ("ORIGIN_LAT", "Lua origin, set per site"),
        ("ORIGIN_LON", "Lua origin, set per site"),
        ("AHRS_ORIG_LAT", "EKF origin, set per site"),
        ("AHRS_ORIG_LON", "EKF origin, set per site"),
        ("SCR_ENABLE", "Lua scripting"),
    ]),
    ("Depth & Surftrak", [
        ("BARO_SPEC_GRAV", "water density for depth"),
        ("SURFTRAK_DEPTH", "shallowest Surftrak, cm"),
        ("PILOT_SPEED_UP", "cm/s"),
        ("PILOT_SPEED_DN", "cm/s"),
    ]),
    ("Failsafes", [
        ("FS_LEAK_ENABLE", ""),
        ("FS_GCS_ENABLE", "loss of Cockpit"),
        ("FS_PILOT_INPUT", "loss of joystick"),
        ("FS_PILOT_TIMEOUT", "seconds"),
        ("FS_EKF_ACTION", ""),
        ("FS_EKF_THRESH", ""),
        ("ARMING_CHECK", "pre-arm checks"),
    ]),
    ("Lights", [
        ("JS_LIGHTS_STEPS", "steps, dark to full"),
    ]),
    ("Pilot gain & thrusters", [
        ("JS_GAIN_DEFAULT", "gain at arming"),
        ("JS_GAIN_MIN", ""),
        ("JS_GAIN_MAX", ""),
        ("JS_GAIN_STEPS", ""),
        ("JS_THR_GAIN", "throttle gain"),
        ("MOT_PWM_MIN", "µs"),
        ("MOT_PWM_MAX", "µs"),
    ]),
    ("Logging & power", [
        ("LOG_BITMASK", ""),
        ("LOG_DISARMED", ""),
        ("BATT_MONITOR", "power sensing"),
    ]),
]

#: Advice about a value whatever the reference says: (test, why).
ADVISORIES = {
    "BARO_SPEC_GRAV": (lambda v: v is not None and v < 1.01,
                       "fresh-water value: in Puget Sound (≈1.024) depths "
                       "read about 2.4 % deep — 0.3 m at 13 m"),
    "FS_LEAK_ENABLE": (lambda v: v is not None and v == 0,
                       "the leak failsafe is off"),
    "LOG_DISARMED": (lambda v: v is not None and v == 1,
                     "logging while disarmed fills the Pi's card on deck"),
}


@dataclass
class Row:
    name: str
    what: str
    vehicle: float | None
    reference: float | None
    state: str
    #: Settable from the reference: on both sides, differs, not per-site.
    settable: bool = False
    advice: str = ""
    #: A value worked out from others (a light's step) that comes out
    #: differently: shown, but not counted -- the others are.
    derived_differs: bool = False

    @property
    def vehicle_text(self) -> str:
        if self.state == MISSING and self.vehicle is None:
            return "not on vehicle"
        m = meaning(self.name, self.vehicle)
        return text(self.vehicle, self.name) + (f"  {m}" if m else "")

    @property
    def reference_text(self) -> str:
        if self.reference is None:
            return "not in file"
        m = meaning(self.name, self.reference)
        return text(self.reference, self.name) + (f"  {m}" if m else "")


@dataclass
class Group:
    name: str
    rows: list[Row]


@dataclass
class Comparison:
    groups: list[Group] = field(default_factory=list)
    others: list[Row] = field(default_factory=list)
    #: Present on the vehicle, not in the file; and the other way round.
    only_vehicle: list[str] = field(default_factory=list)
    only_reference: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    read: int = 0

    @property
    def key_rows(self) -> list[Row]:
        return [r for g in self.groups for r in g.rows]

    @property
    def key_differences(self) -> list[Row]:
        return [r for r in self.key_rows if r.state in (DIFFERS, MISSING)]

    def headline(self) -> tuple[str, str]:
        """(word, rest) for the top of the tab."""
        if not self.read:
            return "—", "no parameters read from the vehicle yet"
        k, o = len(self.key_differences), len(self.others)
        if not k and not o:
            return "MATCHES", "every parameter agrees with the reference"
        bits = []
        if k:
            bits.append(f"{k} key parameter{'s' * (k != 1)}")
        if o:
            bits.append(f"{o} other{'s' * (o != 1)}")
        verb = "differs" if k + o == 1 else "differ"
        return "DIFFERS", " and ".join(bits) + f" {verb} from the reference"


def _row(name: str, what: str, vehicle: dict, reference: dict) -> Row:
    v, r = vehicle.get(name), reference.get(name)
    if name in PER_SITE:
        state = SITE
    elif v is None or r is None:
        state = MISSING
    else:
        state = MATCH if same(v, r) else DIFFERS
    advice = ""
    adv = ADVISORIES.get(name)
    if adv is not None and adv[0](v):
        advice = adv[1]
    return Row(name, what, v, r, state,
               settable=state == DIFFERS and name not in PER_SITE,
               advice=advice)


def _light_rows(vehicle: dict, reference: dict) -> list[Row]:
    """Each light channel, wherever it is wired, and its step size."""
    out = []
    either = {**reference, **vehicle}
    channels = sorted(
        (int(k[5:-9]), int(v)) for k, v in either.items()
        if re.match(r"^SERVO\d+_FUNCTION$", k) and int(v) in (LIGHTS1, LIGHTS2))
    for ch, fn in channels:
        label = "Lights 1" if fn == LIGHTS1 else "Lights 2"
        out.append(_row(f"SERVO{ch}_FUNCTION", label, vehicle, reference))
        for part in ("MIN", "MAX"):
            out.append(_row(f"SERVO{ch}_{part}", "µs", vehicle, reference))
        step = [_step(src, ch) for src in (vehicle, reference)]
        state = DERIVED if step[0] is None or step[1] is None or same(*step) \
            else DIFFERS
        out.append(Row(f"↳ {label} step", "µs per press", step[0], step[1],
                       DERIVED, derived_differs=state == DIFFERS))
    return out


def _step(params: dict, ch: int) -> float | None:
    lo, hi = params.get(f"SERVO{ch}_MIN"), params.get(f"SERVO{ch}_MAX")
    n = params.get("JS_LIGHTS_STEPS")
    if lo is None or hi is None or not n:
        return None
    return (hi - lo) / n


def _gain_step(params: dict) -> float | None:
    lo, hi, n = (params.get(k) for k in ("JS_GAIN_MIN", "JS_GAIN_MAX",
                                         "JS_GAIN_STEPS"))
    if lo is None or hi is None or not n or n < 2:
        return None
    return (hi - lo) / (n - 1) * 100.0


def compare(vehicle: dict[str, float] | None,
            reference: dict[str, float] | None) -> Comparison:
    vehicle = dict(vehicle or {})
    reference = dict(reference or {})
    out = Comparison(read=len(vehicle))
    shown: set[str] = set()
    for gname, items in KEY_GROUPS:
        rows = [_row(n, w, vehicle, reference) for n, w in items]
        if gname == "Lights":
            rows += _light_rows(vehicle, reference)
        if gname == "Pilot gain & thrusters":
            gv, gr = _gain_step(vehicle), _gain_step(reference)
            rows.insert(4, Row("↳ gain step", "% per press", gv, gr, DERIVED,
                               derived_differs=not (gv is None or gr is None
                                                    or same(gv, gr))))
        shown.update(r.name for r in rows)
        out.groups.append(Group(gname, rows))
    if not vehicle:
        return out
    for name in sorted(set(vehicle) | set(reference)):
        if name in shown:
            continue
        if is_dynamic(name):
            out.ignored.append(name)
            continue
        if name in PER_SITE:
            continue
        v, r = vehicle.get(name), reference.get(name)
        if v is None:
            out.only_reference.append(name)
        elif r is None:
            out.only_vehicle.append(name)
        elif not same(v, r):
            out.others.append(Row(name, "", v, r, DIFFERS, settable=True))
    return out
