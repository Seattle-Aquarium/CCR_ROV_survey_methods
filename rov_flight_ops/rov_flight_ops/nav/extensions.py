"""
The three BlueOS extensions the navigation suite is actually made of.

They are not interchangeable and they are not one system. Traced end to end,
on the stack this fleet ran on 18 September 2026:

    vessel satellite compass --GGA/HDT over UDP 6200--> WL UGPS External
        --HTTP /api/v1/external/master--> Water Linked G2 topside

    G2 acoustic solution + topside reference --> Water Linked UGPS extension
        --GPS_INPUT--> ArduSub --> EKF --> filtered ROV position

    ArduSub depth --> Water Linked UGPS extension --> G2 topside

    Water Linked DVL --VISION_POSITION_DELTA / _ESTIMATE--> ArduSub --> EKF

Four consequences the dashboard has to respect, each of them a way an earlier
version of this display would have lied:

**Vessel heading is not ROV heading.** The satellite compass tells the G2 which
way the *boat* is pointing so it can rotate the acoustic solution. It is not
the ROV's yaw and must never be fed to one.

**Seeing GGA arrive does not mean the acoustic solution works.** The external
extension's `/status` reports whether a sentence turned up in the last four
seconds. That is all it reports. Whether the G2 has a clock sync, whether it
can hear the locator, whether ArduSub accepted the resulting `GPS_INPUT` --
none of those are in that reply, and each is tracked separately here.

**`/status` is not a measurement timestamp.** Its `latitude`/`longitude`
default to **0** before the first sentence ever arrives, and its freshness
booleans are computed from receipt time inside the extension. Polling it twice
does not make it fresher; a cached identical reply must not reset an age. Both
guarded below.

**`GPS_INPUT.vdop` is not a dilution of precision.** The Water Linked UGPS
extension puts the acoustic standard deviation *in metres* into that field.
Read as VDOP it is meaningless; read as what it is, it is the single most
useful quality number the acoustic system produces.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import model as M
from .model import Quality, Reading, Source

log = logging.getLogger(__name__)

_UA = "rov_flight_ops Navigation (Seattle Aquarium CCR)"

#: The Water Linked DVL extension's Flask app. Fixed in its own source
#: (`app.run(host="0.0.0.0", port=9001)`), so it is a default rather than a
#: guess -- but discovery still wins when BlueOS reports something else.
DVL_PORT = 9001

#: The WL UGPS External extension serves FastAPI on 8080 *inside its
#: container*. Port 6200 is where the satellite compass sends UDP NMEA and is
#: emphatically not an HTTP port; asking it for /status gets nothing. The
#: host-side port is whatever BlueOS mapped, which is why this is discovered.
UGPS_EXTERNAL_CONTAINER_PORT = 8080

#: How stale the external extension's own four-second window makes a reading.
#: Matched to `TopsidePosition.TIMEOUT_S` in its source so the two agree about
#: what "fresh" means rather than drifting apart.
EXTERNAL_TIMEOUT_S = 4.0


def _get(url: str, timeout: float = 3.0) -> tuple[bool, str, str]:
    """(ok, body, error). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read(200_000).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as ex:
        return False, "", f"HTTP {ex.code}"
    except Exception as ex:
        return False, "", str(ex) or type(ex).__name__


# --------------------------------------------------------------------------
#  Finding them
# --------------------------------------------------------------------------


@dataclass
class Service:
    """One extension's HTTP endpoint, as discovered on this vehicle."""

    name: str
    port: int | None = None
    path: str = ""
    found: bool = False
    #: How it was found: "helper", "default" or "absent". Recorded so the
    #: manifest can say whether a port was observed or assumed.
    via: str = "absent"
    note: str = ""

    def url(self, host: str, suffix: str = "") -> str:
        base = f"http://{host}:{self.port}" if self.port else f"http://{host}"
        return base + (self.path or "") + suffix


def discover(host: str, timeout: float = 4.0) -> dict[str, Service]:
    """Ask BlueOS what it is running and where, rather than assuming ports.

    `/helper/latest/web_services` is BlueOS's own register of every service
    and the port it answers on. Extensions put themselves in it, so this
    survives a BlueOS release renumbering things underneath us -- which is
    exactly what `blueos.probe` already does for the file endpoints, and for
    the same reason.

    Falls back to the documented default port for each extension, and says so
    in `via`, so a vehicle whose helper is unreachable still gets a dashboard
    rather than a blank page.
    """
    out = {
        "dvl": Service("Water Linked DVL", DVL_PORT, via="default"),
        "ugps_external": Service("WL UGPS External", None, via="absent"),
        "ugps": Service("Water Linked UGPS", None, via="absent"),
    }
    ok, body, err = _get(f"http://{host}/helper/latest/web_services", timeout)
    if not ok:
        ok, body, err = _get(f"http://{host}/helper/v1.0/web_services", timeout)
    if not ok:
        for s in out.values():
            s.note = f"BlueOS service list unavailable ({err})"
            s.found = s.port is not None
        return out

    try:
        data = json.loads(body)
    except Exception as ex:
        for s in out.values():
            s.note = f"BlueOS service list did not parse ({ex})"
            s.found = s.port is not None
        return out

    services = data if isinstance(data, list) else data.get("services", [])
    for entry in services if isinstance(services, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("title") or "")
        port = entry.get("port")
        try:
            port = int(port) if port is not None else None
        except (TypeError, ValueError):
            port = None
        if port is None:
            continue
        low = name.lower()
        if "dvl" in low and "water" in low:
            out["dvl"] = Service(name, port, found=True, via="helper")
        elif "external" in low and "ugps" in low.replace("-", " "):
            out["ugps_external"] = Service(name, port, found=True, via="helper")
        elif "ugps" in low or "underwater gps" in low:
            # The plain UGPS extension. Checked after the external one so the
            # more specific name wins; both contain "ugps".
            if not out["ugps"].found:
                out["ugps"] = Service(name, port, found=True, via="helper")

    for svc in out.values():
        if not svc.found and svc.port is not None:
            svc.note = "port not listed by BlueOS; using the documented default"
            svc.found = True
        elif not svc.found:
            svc.note = "not found in the BlueOS service list"
    return out


# --------------------------------------------------------------------------
#  Water Linked DVL
# --------------------------------------------------------------------------


@dataclass
class DvlStatus:
    """What the DVL extension says about itself.

    `/get_status` returns `{"status": ..., **settings}` where the settings are
    the ones it persists: enabled, orientation, hostname, origin, rangefinder
    and should_send. `should_send` is the one that matters most and is the
    least obvious -- see `message_type`.
    """

    reachable: bool = False
    enabled: bool | None = None
    status_text: str = ""
    hostname: str = ""
    orientation: int | None = None
    rangefinder: bool | None = None
    #: "POSITION_DELTA", "POSITION_ESTIMATE" or "SPEED_ESTIMATE".
    #:
    #: This decides whether the vehicle can have a geographic position at all.
    #: POSITION_DELTA sends `VISION_POSITION_DELTA`, which ArduPilot routes to
    #: `writeBodyFrameOdom` -- *body-frame odometry*, which puts EKF3 into
    #: relative aiding. POSITION_ESTIMATE sends `VISION_POSITION_ESTIMATE`,
    #: which routes to `writeExtNavData` and satisfies `readyToUseExtNav()`,
    #: the only path to absolute aiding on a vehicle with no GPS.
    message_type: str = ""
    #: The origin the extension has cached. A *cache*, not the EKF's origin:
    #: it is whatever was last passed to `/setcurrentposition`, saved to the
    #: extension's own settings file, and it says nothing about whether the
    #: autopilot accepted it.
    cached_origin: tuple[float, float] | None = None
    error: str = ""
    raw: dict = field(default_factory=dict)
    mono: float = 0.0


def read_dvl(host: str, svc: Service, timeout: float = 3.0) -> DvlStatus:
    ok, body, err = _get(svc.url(host, "/get_status"), timeout)
    out = DvlStatus(mono=time.monotonic())
    if not ok:
        out.error = err
        return out
    try:
        data = json.loads(body)
    except Exception as ex:
        out.error = f"unreadable reply: {ex}"
        return out
    if not isinstance(data, dict):
        out.error = "unexpected reply shape"
        return out
    out.reachable = True
    out.raw = data
    out.status_text = str(data.get("status") or "")
    if isinstance(data.get("enabled"), bool):
        out.enabled = data["enabled"]
    out.hostname = str(data.get("hostname") or "")
    if isinstance(data.get("orientation"), (int, float)):
        out.orientation = int(data["orientation"])
    if isinstance(data.get("rangefinder"), bool):
        out.rangefinder = data["rangefinder"]
    out.message_type = str(data.get("should_send") or "")
    origin = data.get("origin")
    if (isinstance(origin, (list, tuple)) and len(origin) == 2
            and M.valid_latlon(origin[0], origin[1])):
        out.cached_origin = (float(origin[0]), float(origin[1]))
    return out


# --------------------------------------------------------------------------
#  WL UGPS External -- the vessel's position and heading
# --------------------------------------------------------------------------


@dataclass
class VesselStatus:
    """The surface vessel, as the external extension reports it.

    Everything here is deliberately conservative. The extension's `/status`
    reports three booleans and three numbers and nothing else; this type does
    not invent a fourth.
    """

    reachable: bool = False
    #: True when a GGA arrived inside the extension's four-second window.
    #: **Not** a validated fix: `TopsidePosition.location_valid()` checks the
    #: receipt clock and does not look at `fix_quality` at all, so a GGA
    #: reporting no fix still reads valid here.
    gga_ok: bool = False
    #: The same, for the HDT heading sentence, tracked separately because they
    #: fail separately: a compass can lose heading while still reporting a
    #: position, and a bow that points the wrong way is worse than no bow.
    hdt_ok: bool = False
    #: Whether the extension believes its injections into the G2 are landing.
    inject_ok: bool = False
    lat: float | None = None
    lon: float | None = None
    heading_deg: float | None = None
    ugps_host: str = ""
    send_rate: float | None = None
    error: str = ""
    #: Monotonic clock when this laptop last saw *changed* content. A repeat of
    #: an identical reply does not move it -- see `age_source` below.
    changed_mono: float | None = None
    mono: float = 0.0
    #: A digest of the reply, to spot the identical-cache case.
    fingerprint: str = ""


class VesselReader:
    """Polls the external extension and refuses to be fooled by a cached reply.

    Two independent freshness tests, because either alone is wrong:

    * The extension's own `gga_status`/`hdt_status` booleans -- its four-second
      receipt window. These are the authority on whether NMEA is arriving.
    * Whether the reply has *changed*. A satellite compass is on a moving boat;
      identical latitude, longitude and heading across many seconds means the
      extension is serving a frozen value, and the booleans can keep saying
      true if its own clock comparison is the thing that has stuck.

    Neither is derived from the HTTP poll succeeding.
    """

    def __init__(self) -> None:
        self._last_fingerprint = ""
        self._last_change_mono: float | None = None

    def read(self, host: str, svc: Service, timeout: float = 3.0) -> VesselStatus:
        out = VesselStatus(mono=time.monotonic())
        if not svc.found or svc.port is None:
            out.error = svc.note or "extension not found"
            return out
        ok, body, err = _get(svc.url(host, "/status"), timeout)
        if not ok:
            out.error = err
            return out
        try:
            data = json.loads(body)
        except Exception as ex:
            out.error = f"unreadable reply: {ex}"
            return out
        if not isinstance(data, dict):
            out.error = "unexpected reply shape"
            return out

        out.reachable = True
        out.gga_ok = bool(data.get("gga_status"))
        out.hdt_ok = bool(data.get("hdt_status"))
        out.inject_ok = bool(data.get("inject_status"))
        out.ugps_host = str(data.get("ugps_host") or "")
        rate = data.get("send_rate")
        if isinstance(rate, (int, float)):
            out.send_rate = float(rate)

        lat, lon = data.get("latitude"), data.get("longitude")
        # The extension initialises these to 0 and only ever overwrites them
        # from a GGA. A reply with gga_status false is carrying whatever was
        # there before -- on the first poll of the day, literally 0/0.
        if out.gga_ok and M.valid_latlon(lat, lon):
            out.lat, out.lon = float(lat), float(lon)
        hdg = data.get("heading")
        if out.hdt_ok and isinstance(hdg, (int, float)):
            out.heading_deg = float(hdg) % 360.0

        out.fingerprint = f"{lat!r}|{lon!r}|{hdg!r}|{out.gga_ok}|{out.hdt_ok}"
        if out.fingerprint != self._last_fingerprint:
            self._last_fingerprint = out.fingerprint
            self._last_change_mono = out.mono
        out.changed_mono = self._last_change_mono
        return out


def vessel_readings(st: VesselStatus, source_key: str = "ext:ugps_external"
                    ) -> dict[str, Reading]:
    """The vessel row of the sensor matrix, one reading per thing that can fail.

    Written out at length because each of these has a distinct failure and an
    operator needs to see which one it is. "Vessel: bad" helps nobody at 0800
    on a moving deck.
    """
    src = Source(key=source_key, message="/status", device="WL UGPS External")
    now = st.mono
    out: dict[str, Reading] = {}

    if not st.reachable:
        why = st.error or "no answer"
        for key in ("position", "heading", "inject", "fix_quality"):
            out[key] = Reading(quality=Quality.NEVER_RECEIVED, source=src,
                               note=why)
        return out

    stale_note = ""
    if st.changed_mono is not None and (now - st.changed_mono) > EXTERNAL_TIMEOUT_S:
        stale_note = (f"the extension has served the same values for "
                      f"{now - st.changed_mono:.0f} s")

    if st.lat is not None and st.lon is not None:
        r = M.good((st.lat, st.lon), frame="wgs84", source=src,
                   recv_mono=now, note=stale_note)
        out["position"] = r.staled(stale_note) if stale_note else r
    else:
        out["position"] = Reading(
            quality=Quality.INVALID if st.gga_ok else Quality.NEVER_RECEIVED,
            source=src,
            note=("GGA arriving but its position is not usable"
                  if st.gga_ok else
                  "no GGA within the extension's four-second window"))

    if st.heading_deg is not None:
        r = M.good(st.heading_deg, unit="°T", source=src, recv_mono=now,
                   note="HDT true heading")
        out["heading"] = r.staled(stale_note) if stale_note else r
    else:
        out["heading"] = Reading(
            quality=Quality.NEVER_RECEIVED, source=src, unit="°T",
            note="no HDT within the extension's four-second window")

    out["inject"] = M.good(
        st.inject_ok, source=src, recv_mono=now,
        note=f"injecting to {st.ugps_host or 'the G2'}"
             + (f" at {st.send_rate:g} Hz" if st.send_rate else "")
    ) if st.inject_ok else Reading(
        quality=Quality.INVALID, value=False, source=src,
        note="the extension is not injecting into the acoustic topside")

    # Not a limitation to hide: the endpoint genuinely does not publish it.
    out["fix_quality"] = M.unsupported(
        "the extension's /status does not publish GGA fix quality, HDOP or "
        "satellite count — its status booleans are a four-second receipt "
        "window only",
        source=src)
    # COG/SOG are declared in the extension's own model and never written from
    # GGA or HDT, so they are always exactly 0. Displaying that as a speed
    # would say the boat is stopped.
    out["speed_over_ground"] = M.unsupported(
        "the extension fixes course and speed over ground at 0 and never "
        "updates them from NMEA — 0 here is not a measurement",
        source=src)
    return out


# --------------------------------------------------------------------------
#  Water Linked UGPS -- the acoustic ROV position
# --------------------------------------------------------------------------


@dataclass
class AcousticStatus:
    """The acoustic solution, read through ArduSub rather than the G2.

    Deliberately sourced from `GPS_INPUT` as the autopilot received it, not
    from the G2's own API. Two reasons: it is the number that actually reaches
    the EKF, so it is the one worth judging; and it costs nothing extra,
    because the collector is already reading the autopilot.
    """

    #: The acoustic standard deviation in metres, out of `GPS_INPUT.vdop`.
    std_m: float | None = None
    fix_type: int | None = None
    satellites: int | None = None
    hdop: float | None = None
    lat: float | None = None
    lon: float | None = None
    note: str = ""


#: `GPS_INPUT.hdop` is set to this when the extension has nothing usable.
HDOP_INVALID = 65535.0


def acoustic_from_gps_input(sample) -> AcousticStatus:
    """Interpret `GPS_INPUT`/`GPS_RAW_INT` the way the extension actually fills it.

    The field meanings below are not the MAVLink standard ones -- they are what
    `waterlinked/blueos-ugps-extension` v1.0.7 puts there:

    * `vdop` carries the **acoustic standard deviation in metres**, not a
      vertical dilution of precision.
    * `hdop` carries the topside GNSS HDOP, or 1.0 when the extension was
      started with `--ignore_gps`, or 65535 when there is nothing.
    * `satellites_visible` counts the *topside* receiver's satellites, and is
      forced to at least 6 under `--ignore_gps` so ArduSub will accept the
      fix. It is not a measure of underwater position quality.
    * `fix_type` is set to 0 whenever the acoustic position is not valid,
      which makes it the one field here that genuinely means what it says.
    """
    out = AcousticStatus()
    if sample is None:
        return out
    ft = sample.num("fix_type")
    out.fix_type = int(ft) if ft is not None else None
    std = sample.num("vdop")
    if std is not None and std < HDOP_INVALID:
        out.std_m = std
    hd = sample.num("hdop")
    if hd is not None and hd < HDOP_INVALID:
        out.hdop = hd
    sats = sample.num("satellites_visible")
    out.satellites = int(sats) if sats is not None else None
    lat, lon = sample.num("lat"), sample.num("lon")
    if lat is not None and lon is not None:
        lat, lon = lat * 1e-7, lon * 1e-7
        if M.valid_latlon(lat, lon):
            out.lat, out.lon = lat, lon
    return out
