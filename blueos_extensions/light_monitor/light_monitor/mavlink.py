"""
Into MAVLink, and out of it, through BlueOS's mavlink2rest.

**Out.** A summary of the lights is published as NAMED_VALUE_FLOATs, and
events as STATUSTEXT. mavlink2rest hands them to the MAVLink router, which
routes broadcast messages to every endpoint, so they are:

* in the **tlog** on BlueOS 1.4 -- mavlink-router's telemetry log accepts
  broadcast messages from any source (`Endpoint::accept_msg`);
* in the **mcap** on BlueOS 1.5 -- the recorder keeps every MAVLink message
  under `mavlink/<system>/<component>/<TYPE>`, as it already does for the DVL
  extension's `mavlink/255/0/...`;
* in **Cockpit** and **QGroundControl** live (QGC also writes them into its
  own topside tlog).

They are not in the autopilot's .BIN: ArduPilot does not log NAMED_VALUE_FLOAT
it receives.

The names, all at most 10 characters (MAVLink's limit):

=========  =================================================================
LTn_OK     light n answering: 1, or 0 (not answering or no answer for 15 s)
LTn_LVL    light n's own report of its level, %
LTn_PC     light n's power-on count (rises when it restarts)
LTn_T2/T3  light n's 2nd and 3rd STAT? temperatures, deg C (the first reads 0)
LTn_RH     light n's humidity, %, from STAT?
LTn_FLG    light n's fault flags (DeepSea's code; 0 = none)
LTn_EW     light n's *expected* draw at its level, W -- a model, not measured
LT_CMD     the level the controller is commanding, %
LT_PWM     the PWM pulse width it reads from the Navigator, us
LT_SIG     PWM signal present: 1 / 0
LT_LINK    the extension is hearing the controller: 1 / 0
LT_UP      the controller's uptime, s (drops to ~0 when it restarts)
LT_LOST    telemetry lines lost between controller and extension (total)
LT_V/A/W   the light supply's voltage, current, power -- only with a power
           monitor fitted (none on Lutris as of 13 October 2026)
LT_EW      the four lights' expected total draw, W
=========  =================================================================

A value is sent when it changes and otherwise every `KEEPALIVE_S`, and never
faster than `MAX_PER_S` messages a second in all: this shares a Pi that is
recording video, and Cockpit and QGC need nothing faster. Unknown values are
not sent at all -- a missing name is honest, a sentinel is a trap.

They are sent as system 1 (the vehicle), component 191 (MAV_COMP_ID_ONBOARD_
COMPUTER) by default. The readers in this repository that once kept only the
autopilot's (1/1) copy of a message type now keep NAMED_VALUE_FLOAT and
STATUSTEXT from every source, so these are not dropped.

**In.** Read-only GETs, twice a second: SYS_STATUS (the vehicle's battery
voltage and current -- on Lutris, the tether supply that also feeds the light
box's boost converter) and NAMED_VALUE_FLOAT, whose `Lights1` and `PilotGain`
are what the pilot has asked for. mavlink2rest keeps only the latest message
of each type, and ArduSub sends its named floats in a burst, so each read
catches whichever came last; both names are caught within a second or two.
A message whose counter has not moved is not a new reading (the lesson of
`rov_flight_ops.nav.mav2rest`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:6040"
SYSTEM_ID = 1
COMPONENT_ID = 191
KEEPALIVE_S = 10.0
MAX_PER_S = 12
#: Changes smaller than these are not worth a message of their own.
TOLERANCE = {"_T2": 0.5, "_T3": 0.5, "_RH": 1.0, "_EW": 2.0, "LT_PWM": 5.0,
             "LT_UP": 1e12, "LT_V": 0.1, "LT_A": 0.1, "LT_W": 2.0}
SEVERITY = {"warn": "MAV_SEVERITY_WARNING", "info": "MAV_SEVERITY_INFO"}
_UA = "BlueOS Light Monitor (Seattle Aquarium CCR)"


def _tolerance(name: str) -> float:
    for suffix, tol in TOLERANCE.items():
        if name.endswith(suffix) or name == suffix:
            return tol
    return 0.0


def _chars(text: str, n: int) -> list[str]:
    """mavlink2rest's char[n]: a list of single characters, NUL padded."""
    text = text[:n]
    return list(text) + ["\u0000"] * (n - len(text))


class Mavlink2Rest:

    def __init__(self, url: str = DEFAULT_URL, system: int = SYSTEM_ID,
                 component: int = COMPONENT_ID, timeout: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.system = system
        self.component = component
        self.timeout = timeout
        self.sent = 0
        self.errors = 0
        self.last_error = ""
        self._templates: dict[str, dict] = {}
        self._post_path: str | None = None
        self._get_prefix: str | None = None
        self._counters: dict[str, int] = {}

    # -- plumbing ----------------------------------------------------------

    def _request(self, path: str, payload: dict | None = None) -> tuple[bool, str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url + path, data=data, method="POST" if data else "GET",
            headers={"User-Agent": _UA, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return True, r.read(200_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            try:
                detail = ex.read(2000).decode("utf-8", "replace")
            except Exception:
                detail = ""
            return False, f"HTTP {ex.code} {detail}".strip()
        except Exception as ex:                                  # noqa: BLE001
            return False, str(ex) or type(ex).__name__

    def template(self, name: str) -> dict | None:
        """mavlink2rest's own blank message, so field names are never guessed."""
        if name in self._templates:
            return json.loads(json.dumps(self._templates[name]))
        for path in (f"/helper/mavlink?name={name}", f"/v1/helper/mavlink?name={name}"):
            ok, body = self._request(path)
            if ok:
                try:
                    tpl = json.loads(body)
                except ValueError:
                    continue
                if isinstance(tpl, dict) and "message" in tpl:
                    self._templates[name] = tpl
                    return json.loads(json.dumps(tpl))
        return None

    def post(self, payload: dict) -> bool:
        paths = [self._post_path] if self._post_path else ["/mavlink", "/v1/mavlink"]
        err = ""
        for path in paths:
            ok, body = self._request(path, payload)
            if ok:
                self._post_path = path
                self.sent += 1
                return True
            err = body
        self.errors += 1
        self.last_error = err[:200]
        return False

    def _message(self, name: str, fields: dict) -> dict:
        tpl = self.template(name) or {"header": {}, "message": {"type": name}}
        tpl["header"] = {"system_id": self.system, "component_id": self.component,
                         "sequence": 0}
        msg = tpl.setdefault("message", {})
        msg["type"] = name
        msg.update(fields)
        return tpl

    # -- sending -------------------------------------------------------------

    def named_float(self, name: str, value: float, time_boot_ms: int = 0) -> bool:
        return self.post(self._message("NAMED_VALUE_FLOAT", {
            "time_boot_ms": int(time_boot_ms) & 0xFFFFFFFF,
            "name": _chars(name, 10), "value": float(value)}))

    def statustext(self, text: str, lvl: str = "info") -> bool:
        return self.post(self._message("STATUSTEXT", {
            "severity": {"type": SEVERITY.get(lvl, "MAV_SEVERITY_INFO")},
            "text": _chars(text, 50), "id": 0, "chunk_seq": 0}))

    # -- reading -------------------------------------------------------------

    def read(self, name: str, system: int = 1, component: int = 1) -> dict | None:
        """The latest message of a type, or None if absent or not new."""
        prefixes = ([self._get_prefix] if self._get_prefix else ["/v1/mavlink", "/mavlink"])
        for prefix in prefixes:
            ok, body = self._request(
                f"{prefix}/vehicles/{system}/components/{component}/messages/{name}")
            if not ok:
                continue
            self._get_prefix = prefix
            try:
                doc = json.loads(body)
            except ValueError:
                return None
            counter = (((doc.get("status") or {}).get("time") or {}).get("counter"))
            key = f"{system}/{component}/{name}"
            if isinstance(counter, int):
                if self._counters.get(key) == counter:
                    return None                         # nothing new arrived
                self._counters[key] = counter
            return doc.get("message")
        return None


class Publisher:
    """Decides which named values go out this second."""

    def __init__(self, link: Mavlink2Rest, clock=time.time) -> None:
        self.link = link
        self.clock = clock
        self._last: dict[str, tuple[float, float]] = {}      # name -> (value, sent at)

    def due(self, values: dict[str, float]) -> list[tuple[str, float]]:
        now = self.clock()
        changed, stale = [], []
        for name, value in sorted(values.items()):
            prev = self._last.get(name)
            if prev is None or abs(value - prev[0]) > _tolerance(name):
                changed.append((name, value))
            elif now - prev[1] >= KEEPALIVE_S:
                stale.append((prev[1], name, value))
        stale.sort()
        picked = changed + [(n, v) for _t, n, v in stale]
        return picked[:MAX_PER_S]

    def publish(self, values: dict[str, float], time_boot_ms: int = 0) -> int:
        n = 0
        for name, value in self.due(values):
            if self.link.named_float(name, value, time_boot_ms):
                self._last[name] = (value, self.clock())
                n += 1
            else:
                break                    # mavlink2rest is not answering; next second
        return n


class VehicleReader:
    """Polls the vehicle's power and the pilot's settings."""

    def __init__(self, link: Mavlink2Rest, on_vehicle: Callable, on_ardusub: Callable,
                 period_s: float = 0.5) -> None:
        self.link = link
        self.on_vehicle = on_vehicle
        self.on_ardusub = on_ardusub
        self.period_s = period_s
        self.ok = False
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="vehicle")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        sysst = self.link.read("SYS_STATUS")
        if sysst is not None:
            mv, ca = sysst.get("voltage_battery"), sysst.get("current_battery")
            v = mv / 1000.0 if isinstance(mv, (int, float)) and mv not in (0, 65535) else None
            a = ca / 100.0 if isinstance(ca, (int, float)) and ca >= 0 else None
            self.on_vehicle(v, a, source="ArduSub SYS_STATUS")
            self.ok = True
        nvf = self.link.read("NAMED_VALUE_FLOAT")
        if nvf is not None:
            name = nvf.get("name")
            if isinstance(name, list):
                name = "".join(c for c in name if c and c != "\u0000")
            name = str(name or "").strip("\x00 ")
            value = nvf.get("value")
            if isinstance(value, (int, float)):
                if name == "Lights1":
                    self.on_ardusub(lights_pct=round(float(value) * 100.0, 1))
                elif name == "PilotGain":
                    self.on_ardusub(pilot_gain_pct=round(float(value) * 100.0, 1))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.error = ""
            except Exception as ex:                          # noqa: BLE001
                self.error = str(ex)
                self.ok = False
            self._stop.wait(self.period_s)
