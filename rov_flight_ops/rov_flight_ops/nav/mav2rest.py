"""
Reading live MAVLink off BlueOS, and the one rule that makes it honest.

mavlink2rest keeps the most recent of each message type and serves it at its
own URL. That is a fine way to read a vehicle from a laptop -- no MAVLink
connection of our own, no competing for Cockpit's port, no risk of the GCS
heartbeat this program must never emit -- and it has exactly one trap, which
this module exists to close:

    **A successful GET proves the service answered. It does not prove the
    vehicle has sent anything.**

Ask for `RANGEFINDER` after the DVL has been unplugged for ten minutes and you
get HTTP 200 and a beautifully formatted 0.85 m. Poll it once a second and it
never changes, and nothing in the response body says so. A dashboard built on
"did the request succeed" shows a live altitude for a sensor that is gone --
which is the failure this whole chapter is meant to prevent.

The way out is in the response. Every message carries a `status.time` block::

    {"message": {...},
     "status": {"time": {"counter": 22750, "frequency": 5.495,
                         "first_update": "...", "last_update": "..."}}}

`counter` is how many of that message the service has received. Its *change*
between two polls is the only evidence that anything new arrived. So every
read here compares the counter with the one before it, and a message whose
counter has not moved is not a new sample, however good the HTTP status was.
`frequency` is mavlink2rest's own measurement of the vehicle-side rate, which
is what the message-health panel shows. A counter that goes *backwards* means
the service or the vehicle restarted, and everything derived from that message
is from before the restart.

**Only `read` and `parameters` are used during ordinary operation, and both
are GETs.** The two functions that send anything -- `request_message` and
`send` -- are called only from an operator's explicit action, are refused
outright in replay, and are named so that a grep for writes finds them.

The version this was written against is BlueOS 1.5.0-beta.39, whose
mavlink2rest is t0.11.25. `probe_api` records what the connected vehicle
actually serves rather than trusting that.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .model import MessageHealth

log = logging.getLogger(__name__)

#: Taken from `/helper/v1.0/web_services` on a live vehicle, and the default
#: the Water Linked UGPS extension itself ships with (`--mavlink_host
#: http://blueos.local:6040`). Discovery can override it.
MAVLINK2REST_PORT = 6040

#: The vehicle and component the autopilot answers on. Not assumed -- see
#: `choose_vehicle` -- but it is where the search starts.
AUTOPILOT_SYSTEM = 1
AUTOPILOT_COMPONENT = 1

_UA = "rov_flight_ops Navigation (Seattle Aquarium CCR)"

#: A single message body is small. The cap is here so a service that answers
#: with something enormous cannot be read into the window's memory.
_LIMIT = 200_000


class VehicleWriteRefused(RuntimeError):
    """Raised when something tries to send to a vehicle that must not be sent to.

    Replay sets `allow_writes=False`, and that has to be a hard error rather
    than a quiet no-op: a code path that silently does nothing in replay and
    something in the field is a path nobody can test.
    """


@dataclass
class Answer:
    """One HTTP result. Never raises; failures are recorded."""

    ok: bool
    status: int | None = None
    body: str = ""
    error: str = ""
    seconds: float = 0.0


def _get(url: str, *, timeout: float = 4.0) -> Answer:
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(_LIMIT).decode("utf-8", "replace")
            return Answer(True, r.status, body, seconds=time.monotonic() - t0)
    except urllib.error.HTTPError as ex:
        return Answer(False, ex.code, error=f"HTTP {ex.code}",
                      seconds=time.monotonic() - t0)
    except Exception as ex:
        return Answer(False, None, error=str(ex) or type(ex).__name__,
                      seconds=time.monotonic() - t0)


def _post(url: str, payload: dict, *, timeout: float = 6.0) -> Answer:
    t0 = time.monotonic()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": _UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(_LIMIT).decode("utf-8", "replace")
            return Answer(True, r.status, body, seconds=time.monotonic() - t0)
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = ex.read(4000).decode("utf-8", "replace")
        except Exception:
            pass
        return Answer(False, ex.code, error=f"HTTP {ex.code} {detail}".strip(),
                      seconds=time.monotonic() - t0)
    except Exception as ex:
        return Answer(False, None, error=str(ex) or type(ex).__name__,
                      seconds=time.monotonic() - t0)


def _parse_iso(s: str) -> float | None:
    """mavlink2rest's RFC3339 timestamps, as a POSIX float.

    These are the *vehicle's* clock, which on a Pi with no fix and no internet
    can be wrong by years. They are recorded for provenance and never used to
    compute an age -- that is what the counter and this laptop's monotonic
    clock are for.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


@dataclass
class Sample:
    """One message read, and whether it was actually new."""

    name: str
    message: dict
    #: True only when the counter moved since the previous read.
    fresh: bool
    counter: int | None = None
    frequency: float | None = None
    #: The vehicle's own last-update time, for the log. Not for ages.
    vehicle_time: float | None = None
    #: This laptop's monotonic clock at the moment of the read.
    mono: float = 0.0
    wall: float = 0.0
    reset: bool = False
    error: str = ""

    def get(self, *path, default=None):
        """A field out of the message body, tolerating the enum wrapper.

        mavlink2rest renders enums as `{"type": "MAV_SEVERITY_INFO"}` rather
        than a bare value, so `sample.get("severity")` on a raw dict gives a
        dict where the caller wanted a name. This unwraps one level of that.
        """
        cur: object = self.message
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        if isinstance(cur, dict) and set(cur) == {"type"}:
            return cur["type"]
        return cur

    def num(self, *path, default=None) -> float | None:
        v = self.get(*path)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        return float(v)


class Mavlink2Rest:
    """A connection's worth of state: the host, and what each message has done.

    One instance per collector. It is not thread-safe and is not meant to be:
    the collector owns it on its own thread, and the window never touches it.
    """

    def __init__(self, host: str, *, port: int = MAVLINK2REST_PORT,
                 system: int = AUTOPILOT_SYSTEM,
                 component: int = AUTOPILOT_COMPONENT,
                 allow_writes: bool = False, timeout: float = 4.0) -> None:
        self.host = host
        self.port = port
        self.system = system
        self.component = component
        #: False in replay, and False until the operator unlocks writes. Every
        #: sending function checks it.
        self.allow_writes = allow_writes
        self.timeout = timeout
        #: name -> how that message has been behaving.
        self.health: dict[str, MessageHealth] = {}
        #: The last body of each message, so a poll that brings nothing new
        #: can still answer "what was it".
        self._last: dict[str, dict] = {}

    # -- addresses -------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def message_url(self, name: str) -> str:
        return (f"{self.base}/v1/mavlink/vehicles/{self.system}"
                f"/components/{self.component}/messages/{name}")

    # -- reading ---------------------------------------------------------

    def read(self, name: str) -> Sample:
        """Read one message, and say honestly whether it is new.

        The returned `Sample` always carries the most recent body the service
        has, so a caller that wants "the last thing it said" can have it; what
        it must check is `fresh`.
        """
        now_mono, now_wall = time.monotonic(), time.time()
        h = self.health.setdefault(name, MessageHealth(name=name))
        a = _get(self.message_url(name), timeout=self.timeout)
        h.last_read_mono = now_mono

        if not a.ok:
            # A 404 from mavlink2rest means this vehicle has never sent this
            # message. That is a fact about the vehicle, not a network error,
            # and the difference matters to the health panel.
            h.error = ("never sent by this vehicle" if a.status == 404
                       else a.error)
            return Sample(name=name, message=self._last.get(name, {}),
                          fresh=False, mono=now_mono, wall=now_wall,
                          counter=h.counter, frequency=h.frequency,
                          error=h.error)
        h.error = ""

        try:
            data = json.loads(a.body)
        except Exception as ex:
            h.error = f"unreadable reply: {ex}"
            return Sample(name=name, message=self._last.get(name, {}),
                          fresh=False, mono=now_mono, wall=now_wall,
                          error=h.error)

        msg = data.get("message", data) if isinstance(data, dict) else {}
        if not isinstance(msg, dict):
            msg = {}
        status = (data.get("status") or {}) if isinstance(data, dict) else {}
        tinfo = (status.get("time") or {}) if isinstance(status, dict) else {}
        counter = tinfo.get("counter")
        counter = int(counter) if isinstance(counter, (int, float)) else None
        freq = tinfo.get("frequency")
        freq = float(freq) if isinstance(freq, (int, float)) else None
        vtime = _parse_iso(tinfo.get("last_update") or "")

        # The whole point of this module.
        reset = False
        if counter is None:
            # A service that does not publish a counter leaves us no way to
            # tell a new message from a cached one. Treat every read as fresh
            # -- it is the old, weaker behaviour -- but record that, so the
            # health panel can say the freshness is unverified rather than
            # implying it was checked.
            fresh = True
            h.error = h.error or ""
            h.seen = True
            h.last_change_mono = now_mono
        else:
            previous = h.counter
            if previous is None:
                fresh = True
            elif counter > previous:
                fresh = True
            elif counter < previous:
                # Backwards: the service restarted, or the vehicle did.
                fresh, reset = True, True
            else:
                fresh = False
            if fresh:
                h.seen = True
                h.last_change_mono = now_mono
            h.counter = counter
        h.frequency = freq
        h.reset = h.reset or reset
        self._last[name] = msg

        return Sample(name=name, message=msg, fresh=fresh, counter=counter,
                      frequency=freq, vehicle_time=vtime, mono=now_mono,
                      wall=now_wall, reset=reset)

    def read_many(self, names) -> dict[str, Sample]:
        return {n: self.read(n) for n in names}

    def counter_moved(self, name: str) -> bool:
        h = self.health.get(name)
        return bool(h and h.seen)

    # -- discovery -------------------------------------------------------

    def probe_api(self) -> dict:
        """What this vehicle actually serves, recorded rather than assumed.

        Goes in the session manifest so that a question months later about why
        a reading was missing is answered by what was there on the day.
        """
        out: dict = {"host": self.host, "port": self.port,
                     "system": self.system, "component": self.component}
        a = _get(f"{self.base}/v1/mavlink", timeout=self.timeout)
        out["root_ok"] = a.ok
        if not a.ok:
            out["error"] = a.error
            return out
        try:
            data = json.loads(a.body)
        except Exception:
            return out
        vehicles = data.get("vehicles", data)
        if isinstance(vehicles, dict):
            out["vehicles"] = sorted(vehicles)
        b = _get(f"{self.base}/info", timeout=self.timeout)
        if b.ok:
            try:
                out["service"] = json.loads(b.body)
            except Exception:
                out["service_raw"] = b.body[:400]
        return out

    def choose_vehicle(self) -> tuple[int, int] | None:
        """Find the autopilot rather than trusting the first heartbeat.

        This fleet's recordings carry heartbeats from five different component
        ids on system 1 -- the autopilot, plus BlueOS services at 100, 191 and
        194 -- and a ground station at 255/240. Picking whichever answered
        first would sometimes pick a service that publishes no telemetry at
        all. The autopilot is the component whose HEARTBEAT declares an
        autopilot type that is not INVALID; 1/1 is preferred when several do.
        """
        a = _get(f"{self.base}/v1/mavlink/vehicles", timeout=self.timeout)
        if not a.ok:
            return None
        try:
            data = json.loads(a.body)
        except Exception:
            return None
        candidates: list[tuple[int, int]] = []
        vehicles = data if isinstance(data, dict) else {}
        for sys_key, comps in vehicles.items():
            try:
                sysid = int(str(sys_key).lstrip("/"))
            except ValueError:
                continue
            comp_map = comps.get("components", comps) if isinstance(comps, dict) else {}
            if not isinstance(comp_map, dict):
                continue
            for comp_key in comp_map:
                try:
                    compid = int(str(comp_key).lstrip("/"))
                except ValueError:
                    continue
                candidates.append((sysid, compid))
        if not candidates:
            return None
        if (AUTOPILOT_SYSTEM, AUTOPILOT_COMPONENT) in candidates:
            return AUTOPILOT_SYSTEM, AUTOPILOT_COMPONENT
        for sysid, compid in sorted(candidates):
            probe = Mavlink2Rest(self.host, port=self.port, system=sysid,
                                 component=compid, timeout=self.timeout)
            hb = probe.read("HEARTBEAT")
            ap = hb.get("autopilot")
            if ap and ap != "MAV_AUTOPILOT_INVALID":
                return sysid, compid
        return sorted(candidates)[0]

    # -- parameters ------------------------------------------------------

    def parameter(self, name: str) -> float | None:
        """One parameter, from whatever the service already holds.

        mavlink2rest keeps only the most recent `PARAM_VALUE`, so this answers
        only for a parameter something has recently asked about. The
        authoritative read is `blueos.read_parameters_now`, off the dataflash
        log, which is what the profile check actually uses; this is for
        confirming a single value straight after writing it.
        """
        s = self.read("PARAM_VALUE")
        if s.get("param_id") != name:
            return None
        v = s.num("param_value")
        return v

    # -- the two that send ------------------------------------------------

    def _require_writes(self, what: str) -> None:
        if not self.allow_writes:
            raise VehicleWriteRefused(
                f"{what} was refused: this connection is read-only "
                f"({'replay' if self.host in ('', 'replay') else 'writes not unlocked'})")

    def helper_template(self, name: str) -> dict | None:
        """mavlink2rest's own blank message, so field names are never guessed.

        `/helper/mavlink?name=COMMAND_LONG` returns a fully populated skeleton
        for whatever the connected service's dialect actually is. Filling that
        in cannot drift from the dialect the way a hand-written JSON literal
        can -- which is how the DVL extension does it, and it is the right way.
        """
        a = _get(f"{self.base}/helper/mavlink?name={urllib.parse.quote(name)}",
                 timeout=self.timeout)
        if not a.ok:
            return None
        try:
            out = json.loads(a.body)
            return out if isinstance(out, dict) else None
        except Exception:
            return None

    def request_message(self, message_id: int, *, reason: str = "") -> Answer:
        """Ask the vehicle to send one message now.

        This *is* traffic to the vehicle -- a `MAV_CMD_REQUEST_MESSAGE` command
        -- which is why it is not in the polling loop. It exists for one job:
        reading `GPS_GLOBAL_ORIGIN`, which ArduPilot does not stream and which
        is the only way to find out whether the EKF origin has actually been
        set. The operator presses a button for it.
        """
        self._require_writes(f"REQUEST_MESSAGE({message_id})")
        tpl = self.helper_template("COMMAND_LONG")
        if tpl is None:
            return Answer(False, error="mavlink2rest would not supply a "
                                       "COMMAND_LONG template")
        msg = tpl.setdefault("message", {})
        msg["command"] = {"type": "MAV_CMD_REQUEST_MESSAGE"}
        msg["param1"] = float(message_id)
        for p in ("param2", "param3", "param4", "param5", "param6", "param7"):
            msg[p] = 0.0
        msg["target_system"] = self.system
        msg["target_component"] = self.component
        msg["confirmation"] = 0
        log.info("requesting message %s from %s/%s (%s)", message_id,
                 self.system, self.component, reason or "no reason given")
        return _post(f"{self.base}/mavlink", tpl, timeout=self.timeout)

    def send(self, payload: dict, *, what: str) -> Answer:
        """POST a prepared MAVLink message. The only general write path.

        `what` is required and is logged: every byte this program ever sends a
        vehicle should be answerable from the diagnostics log alone.
        """
        self._require_writes(what)
        log.info("sending to %s: %s", self.host, what)
        return _post(f"{self.base}/mavlink", payload, timeout=self.timeout)


# --------------------------------------------------------------------------
#  Message sets
# --------------------------------------------------------------------------
#
# What is polled, and how often. These are *measured* choices, not "ask for
# everything at 10 Hz": each entry costs one HTTP GET against a Pi that is
# also running Cockpit, Madrona and a recorder, and nothing on this page
# changes faster than the eye.
#
# Nothing here alters a stream rate on the vehicle. The rates below are how
# often this laptop *reads* what BlueOS already holds; asking ArduSub to send
# faster would be a write, would compete with Cockpit, and is deliberately not
# done.

#: Read at the dashboard rate -- the instruments an operator is flying on.
FAST = (
    "ATTITUDE",
    "VFR_HUD",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "RANGEFINDER",
    "NAMED_VALUE_FLOAT",
    "BATTERY_STATUS",
    "HEARTBEAT",
)

#: Read a few times a minute -- state that moves slowly, or costs more.
SLOW = (
    "EKF_STATUS_REPORT",
    "GPS_RAW_INT",
    "SYS_STATUS",
    "SCALED_PRESSURE2",
    "VIBRATION",
    "SYSTEM_TIME",
    "DISTANCE_SENSOR",
    "GPS_GLOBAL_ORIGIN",
    "AUTOPILOT_VERSION",
)

#: How long each message stays believable once it has stopped arriving. A
#: number per message rather than one global timeout, because the honest
#: answer differs by an order of magnitude: attitude at 10 Hz is suspect after
#: two seconds, a flight mode in a heartbeat is fine for half a minute.
MAX_AGE = {
    "ATTITUDE": 2.0,
    "VFR_HUD": 3.0,
    "GLOBAL_POSITION_INT": 4.0,
    "LOCAL_POSITION_NED": 4.0,
    "RANGEFINDER": 3.0,
    "NAMED_VALUE_FLOAT": 6.0,
    "BATTERY_STATUS": 5.0,
    "HEARTBEAT": 6.0,
    "EKF_STATUS_REPORT": 15.0,
    "GPS_RAW_INT": 15.0,
    "SYS_STATUS": 10.0,
    "SCALED_PRESSURE2": 10.0,
    "VIBRATION": 20.0,
    "SYSTEM_TIME": 90.0,
    "DISTANCE_SENSOR": 5.0,
    # Never streamed; it only ever appears in answer to a request, so it is
    # not aged out on the clock at all -- it is shown with its age instead.
    "GPS_GLOBAL_ORIGIN": 1e9,
    "AUTOPILOT_VERSION": 1e9,
}


def max_age(name: str) -> float:
    return MAX_AGE.get(name, 5.0)
