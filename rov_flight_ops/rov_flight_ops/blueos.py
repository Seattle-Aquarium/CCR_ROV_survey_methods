"""
Talking to BlueOS on the ROV's Raspberry Pi.

The point is to stop choosing recordings by hand. Three separate field
failures came from that: a flight whose covering recording was never
downloaded, a 6.7 GB file from a previous day pulled in because BlueOS had
rewritten its modification time, and a stray recording from six weeks earlier
sitting in a folder. UTC already knows the transect times and can read an
mcap's true span in under a second, so it can pick the right files itself.

**Everything here is read-only.** GET requests only, no deletes, no writes to
the vehicle. Freeing space on the Pi stays a deliberate act in BlueOS's own
interface: a bug here must never be able to destroy the only copy of a dive.

The API is *discovered*, not assumed. BlueOS moves between releases and
extensions register themselves at runtime, so `probe()` walks what the vehicle
actually offers and reports it. Building against a guessed endpoint is how you
get a tool that works on one vehicle and not the next.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

ProgressCB = Callable[[float, str], None]

#: Where the Pi answers. The tether address first: it is what the vehicle uses
#: when plugged in, and mDNS often does not resolve on a field laptop that has
#: just joined a MiFi hotspot.
DEFAULT_HOSTS = ("192.168.2.2", "blueos.local", "blueos", "127.0.0.1")

#: BlueOS's own services, reached through the reverse proxy on port 80.
#: `helper` is the one that matters: it enumerates everything else, so the
#: rest of this module does not have to hard-code ports.
CORE_PROBES = (
    ("version", "/version-chooser/v1.0/version/current"),
    ("services", "/helper/latest/web_services"),
    ("vehicle", "/ardupilot-manager/v1.0/vehicle_type"),
    ("disk", "/system-information/system/disk"),
)

#: Where the recorder extension keeps its files on the Pi. Seen in its own
#: log output, so this is observed rather than assumed -- but it is a path on
#: the vehicle, not a URL, and still needs a service willing to serve it.
RECORDER_DIR = "/usr/blueos/userdata/recorder"

#: Ports the services answer on directly. Taken from `/helper/v1.0/web_services`
#: on a live vehicle rather than assumed -- the reverse proxy on port 80 also
#: serves most of these by name, but the ports are unambiguous.
FILE_BROWSER_PORT = 7777        # filebrowser.org: lists and serves any file
DISK_USAGE_PORT = 9151          # `du` as a tree, so a folder can be sized
EXTRACTOR_PORT = 9150           # the recorder extension's own API
LINUX2REST_PORT = 6030          # CPU, memory, temperature, throttle events
MAVLINK2REST_PORT = 6040        # live telemetry, one message type per URL
KRAKEN_PORT = 9134              # extensions and their versions

#: Where the recordings live, as File Browser addresses them. Its root is not
#: the filesystem root: `system_root` is a mount point it publishes, and the
#: recorder folder hangs off that.
RECORDER_FB_PATH = "/system_root/usr/blueos/userdata/recorder"

#: Endpoints the probe tries for a directory listing. The first is the one that
#: works; the rest are kept because a different BlueOS may not have File
#: Browser installed, and a probe that reports "none of these" is more useful
#: than one that only knows about the vehicle it was written against.
FILE_PROBES = (
    "/file-browser/api/resources/",
    "/recorder-extractor/v1.0/recorder/files",
    "/recorder-extractor/v1.0/recorder/status",
    "/filebrowser/api/resources/userdata/recorder",
)

MCAP_MAGIC = b"\x89MCAP0\r\n"
#: mcap record opcode for CHUNK, whose payload opens with the span.
OP_CHUNK = 0x06

_UA = "UTC-probe (Seattle Aquarium CCR)"


@dataclass
class Answer:
    """What one request returned. Never raises -- the probe records failures."""

    url: str
    ok: bool
    status: int | None = None
    seconds: float = 0.0
    kind: str = ""
    body: str = ""
    #: Bytes, when the caller asked for them -- a range read of a recording is
    #: not text and must not be put through a decoder.
    raw: bytes = b""
    error: str = ""

    def line(self) -> str:
        if self.ok:
            return (f"  [{self.status}] {self.seconds * 1000:5.0f}ms  {self.url}"
                    + (f"   {self.kind}" if self.kind else ""))
        return f"  [ -- ] {self.url}   {self.error}"


@dataclass
class Probe:
    host: str | None = None
    reachable: bool = False
    version: str = ""
    vehicle: str = ""
    services: list[dict] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    range_supported: bool | None = None
    #: What the vehicle calls itself. The address is not an identity: two
    #: vehicles on this programme's network both answer to `blueos`.
    name: str = ""
    skew: float | None = None
    notes: list[str] = field(default_factory=list)
    #: Filled in by `probe`; declared here so a bare Probe() is still usable.
    space: Space = field(default_factory=lambda: Space())
    platform: Platform = field(default_factory=lambda: Platform())
    parameter_count: int = 0
    parameters_from: str = ""

    def report(self) -> str:
        out = [f"BlueOS probe  --  {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        if not self.reachable:
            out += ["No vehicle answered on any of:",
                    *(f"    {h}" for h in DEFAULT_HOSTS), "",
                    "Check the tether is connected and that this laptop has an",
                    "address on 192.168.2.x, then run it again."]
            return "\n".join(out)

        out += [f"vehicle      : {self.name or 'unnamed'}",
                f"address      : {self.host}",
                f"BlueOS       : {self.version or 'unknown'}",
                f"vehicle type : {self.vehicle or 'unknown'}",
                f"range reads  : {_range_word(self.range_supported)}", ""]
        out += ["before a dive:",
                f"    space    : {self.space.verdict()[1]}",
                f"    the Pi   : {self.platform.note()}"]
        if self.platform.first_event:
            out.append(f"    throttled: {self.platform.first_event} .. "
                       f"{self.platform.last_event}")
        out.append("    clock    : "
                   + (f"{self.skew:+.1f} s against this laptop"
                      if self.skew is not None else "unreadable")
                   + ("   <-- WRONG DAY, set it before flying"
                      if self.skew is not None and abs(self.skew) > 120 else ""))
        out.append(f"    params   : {self.parameter_count} read"
                   + (f" from {self.parameters_from}"
                      if self.parameters_from else " -- no endpoint answered"))
        out.append("")
        if self.services:
            out += [f"registered services ({len(self.services)}):"]
            for s in self.services:
                name = s.get("name") or s.get("title") or "?"
                port = s.get("port", "?")
                path = s.get("path") or s.get("webpage") or ""
                out.append(f"    {str(port):>6}  {name}  {path}")
            out.append("")
        out += ["endpoints tried:"]
        out += [a.line() for a in self.answers]
        if self.notes:
            out += ["", "notes:", *(f"  - {n}" for n in self.notes)]
        return "\n".join(out)


def _range_word(v: bool | None) -> str:
    if v is None:
        return "not established"
    return ("yes -- a recording's span can be read without downloading it"
            if v else "NO -- headers cannot be read without a full download")


# --------------------------------------------------------------------------
#  the smallest possible HTTP client
# --------------------------------------------------------------------------


def _get(url: str, *, timeout: float = 6.0, headers: dict | None = None,
         limit: int = 64_000, binary: bool = False) -> Answer:
    """One GET. Everything is caught: a probe reports, it does not raise."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(limit)
            return Answer(url=url, ok=True, status=r.status,
                          seconds=time.time() - t0,
                          kind=r.headers.get("Content-Type", ""),
                          body="" if binary else raw.decode("utf-8", "replace"),
                          raw=raw if binary else b"")
    except urllib.error.HTTPError as ex:
        return Answer(url=url, ok=False, status=ex.code,
                      seconds=time.time() - t0, error=f"HTTP {ex.code}")
    except Exception as ex:
        return Answer(url=url, ok=False, seconds=time.time() - t0,
                      error=f"{type(ex).__name__}: {str(ex)[:80]}")


def find_vehicles(hosts: Iterable[str] = DEFAULT_HOSTS,
                  timeout: float = 3.0) -> list[tuple[str, str]]:
    """Every candidate address that answers, as (address, vehicle name).

    All of them, not the first, because more than one vehicle can answer at
    once. On this programme's own network the tethered ROV and a fixed camera
    both call themselves `blueos`, so picking the first responder chose the
    camera -- and the recordings it offered looked perfectly plausible.

    Asked over HTTP rather than by opening a socket. A bare connect was what
    this used to do, and it disagreed with the request that followed it: on a
    tether still negotiating its address, the socket timed out while an HTTP
    GET to the same address succeeded in 116 ms. Probing with the mechanism
    actually used removes the disagreement.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for h in hosts:
        try:                                # collapse aliases for one machine
            ip = socket.gethostbyname(h.split(":")[0])
        except OSError:
            continue
        if ip in seen:
            continue
        a = _get(_base(h, BEACON_PORT) + "/v1.0/vehicle_name", timeout=timeout)
        if not a.ok:
            a = _get(f"http://{h}/version-chooser/v1.0/version/current",
                     timeout=timeout)
            if not a.ok:
                continue
            found.append((h, ""))
        else:
            found.append((h, a.body.strip().strip('"')[:40]))
        seen.add(ip)
    return found


def find_host(hosts: Iterable[str] = DEFAULT_HOSTS,
              timeout: float = 3.0) -> str | None:
    """The first candidate that answers, tether address first."""
    got = find_vehicles(hosts, timeout)
    return got[0][0] if got else None


def probe(host: str | None = None,
          progress: ProgressCB | None = None) -> Probe:
    """Ask the vehicle what it offers. Read-only, and safe to run any time."""
    out = Probe()
    if progress:
        progress(0.0, "looking for the vehicle…")
    out.host = host or find_host()
    if out.host is None:
        return out
    out.reachable = True
    out.name = vehicle_name(out.host)
    out.skew = clock_skew(out.host)
    base = f"http://{out.host}"

    steps = len(CORE_PROBES) + len(FILE_PROBES) + 2
    done = 0

    def tick(msg: str) -> None:
        nonlocal done
        done += 1
        if progress:
            progress(min(0.99, done / steps), msg)

    for name, path in CORE_PROBES:
        a = _get(base + path)
        out.answers.append(a)
        tick(f"{name}…")
        if not a.ok:
            continue
        if name == "version":
            out.version = _first_string(a.body, ("version", "tag", "name")) or a.body[:60]
        elif name == "vehicle":
            out.vehicle = a.body.strip().strip('"')[:40]
        elif name == "services":
            try:
                data = json.loads(a.body)
                out.services = data if isinstance(data, list) else data.get("services", [])
            except Exception:
                out.notes.append("the service list did not parse as JSON")

    for path in FILE_PROBES:
        a = _get(base + path)
        out.answers.append(a)
        tick("file endpoints…")

    # The pre-dive readings. Recorded here too so that one run beside the
    # vehicle settles which of the candidate endpoints this BlueOS actually
    # serves, rather than each feature discovering it separately.
    out.space = read_space(out.host, out.answers)
    out.platform = read_platform(out.host, out.answers)
    params, out.parameters_from = read_parameters(out.host, out.answers)
    out.parameter_count = len(params)
    tick("disk, platform, parameters…")
    if not out.space.found:
        out.notes.append(
            "Free space could not be read. Tried: "
            + ", ".join(DISK_PROBES))
    if not out.parameters_from:
        out.notes.append(
            "No parameter endpoint answered. Tried: " + ", ".join(PARAM_PROBES))

    # Can a recording's header be read without pulling the whole file? This
    # decides whether UTC can judge a recording's span on the vehicle, which
    # is the whole point -- so it is tested against the transport that will
    # actually do it, on a real recording, rather than against whichever
    # endpoint happened to answer.
    token = file_token(out.host)
    recs = list_recordings(out.host, token) if token else []
    if recs:
        smallest = min(recs, key=lambda r: r["size"])
        r = _get(recording_url(out.host, smallest["name"], token),
                 headers={"Range": "bytes=0-1023"}, limit=2048, binary=True)
        out.range_supported = (r.status == 206)
        out.answers.append(Answer(
            url=f"[range] {smallest['name']}", ok=r.ok, status=r.status,
            seconds=r.seconds, kind=f"{len(r.raw)} bytes of "
                                    f"{smallest['size'] / 2 ** 20:,.0f} MiB",
            error=r.error))
        out.notes.append(
            f"{len(recs)} recordings on the vehicle, "
            f"{sum(x['size'] for x in recs) / 2 ** 30:,.2f} GiB.")
    elif token:
        out.notes.append(
            f"File Browser opened a session but found no .mcap in "
            f"{RECORDER_FB_PATH}.")
    else:
        out.notes.append(
            "File Browser would not open a session, so recordings cannot be "
            "listed or fetched. It is the only service that serves them: the "
            "recorder extension refuses anything that is not an .mp4.")
    tick("range support…")

    others = [(h, n) for h, n in find_vehicles() if h != out.host]
    if others:
        out.notes.append(
            "More than one vehicle answered: "
            + "; ".join(f"{n or 'unnamed'} at {h}" for h, n in
                        [(out.host, out.name), *others])
            + f". This report is {out.name or 'the one'} at {out.host}.")
    if not out.host.startswith(("192.168.2.", "127.")):
        out.notes.append(
            f"This is {out.host}, not the tether address 192.168.2.2. Two "
            f"vehicles on this network answer to the hostname 'blueos' -- "
            f"check the name above is the one you meant to reach.")
    if not any(a.ok for a in out.answers if "recorder" in a.url or "resources" in a.url):
        out.notes.append(
            "No file-listing endpoint answered. The service list above is the "
            "place to look: find the entry serving the recorder folder and "
            "send this report back.")
    if progress:
        progress(1.0, "done")
    return out


def _first_string(body: str, keys: tuple[str, ...]) -> str:
    try:
        data = json.loads(body)
    except Exception:
        return ""
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, str):
                return v
    return ""


# --------------------------------------------------------------------------
#  before the dive: is the vehicle fit to fly?
# --------------------------------------------------------------------------

#: Roughly what a dive writes per second, measured from this programme's own
#: recordings: 4.73 GB over 56m49s and 5.30 GB over 67m32s, both about
#: 1.4 MB/s. Used to turn free space into the only number that matters on a
#: deck -- how many more minutes can be recorded.
BYTES_PER_SECOND = 1_400_000

#: Candidates for each reading. None is promised. The first that answers wins
#: and the probe reports which, so Wednesday's run against the real vehicle
#: settles these rather than a guess doing it.
DISK_PROBES = (
    "/system-information/system/disk",
    "/system-information/v1.0/system/disk",
    "/disk-usage/v1.0/disk",
)
PLATFORM_PROBES = (
    "/system-information/platform",
    "/system-information/system/platform",
)
MEMORY_PROBES = (
    "/system-information/system/memory",
    "/system-information/v1.0/system/memory",
)
#: The full parameter set. ardupilot-manager is the likeliest; mavlink2rest
#: exposes PARAM_VALUE, and the bag of holding stores what BlueOS itself has
#: saved. All three are tried.
PARAM_PROBES = (
    "/ardupilot-manager/v1.0/parameters",
    "/mavlink2rest/v1/mavlink/vehicles/1/components/1/messages/PARAM_VALUE",
    "/bag-of-holding/v1.0/bag/ardupilot",
)


def _first_ok(base: str, paths: Iterable[str], sink: list | None = None,
              retries: int = 1) -> Answer | None:
    """The first candidate endpoint that answers, or None.

    Every attempt is appended to `sink` when one is given, misses included.
    Which candidates were tried and what they returned is the whole point of
    running the probe beside a real vehicle -- a reading that quietly fell
    through to the third candidate is something to know.

    A connection that failed outright is retried once before moving on. Only
    that case: an HTTP status means the vehicle answered and said no, and
    asking again would not change its mind.
    """
    for path in paths:
        for attempt in range(retries + 1):
            a = _get(base + path)
            if sink is not None:
                sink.append(a)
            if a.ok:
                return a
            # A Pi that is busy -- reading a dozen recording headers will do
            # it -- drops the odd request. Reporting "could not be read" on a
            # single miss sends someone looking for a fault that is not there.
            if attempt < retries and a.status is None:
                time.sleep(0.4)
            else:
                break
    return None


@dataclass
class Space:
    """Room left where the recorder writes."""

    path: str = ""
    free_bytes: int = 0
    total_bytes: int = 0
    found: bool = False
    source: str = ""

    @property
    def minutes_left(self) -> float:
        return self.free_bytes / BYTES_PER_SECOND / 60

    def verdict(self, planned_seconds: float = 0.0) -> tuple[bool, str]:
        """Is there room for the dive that is planned?

        Answered in minutes of recording rather than gigabytes, because that
        is the question actually being asked on the deck. A recorder that
        fills mid-transect does not warn anyone -- it just stops, and the
        transect is gone.
        """
        if not self.found:
            return True, "free space on the vehicle could not be read"
        free_gib = self.free_bytes / 2 ** 30
        room = (f"{free_gib:,.1f} GiB free -- about {self.minutes_left:,.0f} "
                f"minutes of recording")
        if planned_seconds <= 0:
            return True, room
        need = planned_seconds * BYTES_PER_SECOND
        planned_min = planned_seconds / 60
        if self.free_bytes < need:
            return False, (
                f"Only {free_gib:,.1f} GiB free on the vehicle, but the plan "
                f"is {planned_min:,.0f} minutes -- about {need / 2 ** 30:,.1f} "
                f"GiB. The recorder will stop part way through. Move older "
                f"recordings off the ROV before diving.")
        if self.free_bytes < need * 2:
            return True, (
                f"{free_gib:,.1f} GiB free: enough for the {planned_min:,.0f} "
                f"minutes planned, but not much more. Worth clearing space "
                f"before a long day.")
        return True, room


def read_space(host: str, sink: list | None = None) -> Space:
    """Free space where the recorder writes."""
    out = Space()
    a = _first_ok(f"http://{host}", DISK_PROBES, sink)
    if a is None:
        return out
    out.source = a.url
    try:
        data = json.loads(a.body)
    except Exception:
        return out

    best: tuple[int, str, int, int] | None = None
    for entry in (data if isinstance(data, list) else [data]):
        if not isinstance(entry, dict):
            continue
        free = _num(entry, ("available_space_B", "free_bytes", "free",
                            "available"))
        if free is None:
            continue
        total = _num(entry, ("total_space_B", "total_bytes", "total", "size"))
        mount = str(entry.get("mount_point") or entry.get("path")
                    or entry.get("name") or "")
        # Prefer the volume the recordings are written to over, say, a boot
        # partition that is small and always nearly full.
        score = 2 if ("userdata" in mount or mount == "/") else 1
        if best is None or score > best[0]:
            best = (score, mount, int(free), int(total or 0))

    if best is not None:
        out.found = True
        _, out.path, out.free_bytes, out.total_bytes = best
    return out


def _num(d: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


@dataclass
class Platform:
    """What the Pi says about its own condition."""

    model: str = ""
    ram_used: float = 0.0                    # fraction of total
    throttle: dict = field(default_factory=dict)     # kind -> count
    occurring: list = field(default_factory=list)    # happening right now
    first_event: str = ""
    last_event: str = ""
    found: bool = False

    @property
    def undervoltage(self) -> bool:
        """A failing tether or supply, as distinct from a hot Pi."""
        return any("olt" in k for k in
                   list(self.throttle) + [str(o) for o in self.occurring])

    def note(self) -> str:
        """One line, honest about what it does and does not mean."""
        if not self.found:
            return "the Pi's own state could not be read"
        bits = [self.model or "Raspberry Pi"]
        if self.ram_used:
            bits.append(f"RAM {self.ram_used * 100:.0f}%")
        if self.occurring:
            kinds = ", ".join(sorted({str(o) for o in self.occurring}))
            bits.append(f"THROTTLING NOW: {kinds}")
        elif self.throttle:
            n = sum(self.throttle.values())
            kinds = ", ".join(sorted(self.throttle))
            bits.append(f"{n} past throttle events ({kinds})")
        else:
            bits.append("no throttling logged")
        return "   ".join(bits)

    def advice(self) -> str:
        """What, if anything, to do about it."""
        if self.undervoltage:
            return ("Under-voltage is logged. That is a power problem, not a "
                    "heat one -- check the tether and the supply before "
                    "diving; it corrupts recordings.")
        if self.occurring:
            return ("The Pi is capping its own clock right now, which is heat. "
                    "It sits in a sealed tube with no airflow, so this builds "
                    "over a dive. Expect dropped frames rather than an error.")
        if self.throttle:
            return ("Clock capping has been logged this boot. It is thermal, "
                    "and it is normal for a Pi in a sealed tube -- worth "
                    "watching rather than acting on.")
        return ""


def read_platform(host: str, sink: list | None = None) -> Platform:
    """Model, memory, and the Pi's own throttle log.

    ``FrequencyCapping`` is the Pi capping its clock because it is hot;
    ``UnderVoltage`` is the supply sagging. They look alike in a CPU graph and
    mean entirely different things, so they are reported apart. This programme
    has seen the first and not the second.
    """
    out = Platform()
    a = _first_ok(f"http://{host}", PLATFORM_PROBES, sink)
    if a is not None:
        try:
            data = json.loads(a.body)
        except Exception:
            data = None
        if isinstance(data, dict):
            body = data.get("Ok") if isinstance(data.get("Ok"), dict) else data
            pi = body.get("raspberry") if isinstance(body, dict) else None
            if isinstance(pi, dict):
                out.found = True
                out.model = str(pi.get("model") or "")
                events = pi.get("events") or {}
                stamps = []
                for e in (events.get("list") or []):
                    kind = e.get("type") if isinstance(e, dict) else str(e)
                    out.throttle[str(kind)] = out.throttle.get(str(kind), 0) + 1
                    if isinstance(e, dict) and e.get("time"):
                        stamps.append(str(e["time"]))
                out.occurring = [
                    (o.get("type") if isinstance(o, dict) else o)
                    for o in (events.get("occurring") or [])]
                if stamps:
                    out.first_event, out.last_event = min(stamps), max(stamps)

    m = _first_ok(f"http://{host}", MEMORY_PROBES, sink)
    if m is not None:
        try:
            ram = json.loads(m.body).get("ram") or {}
            total = _num(ram, ("total_kB", "total_B", "total"))
            used = _num(ram, ("used_kB", "used_B", "used"))
            if total:
                out.ram_used = (used or 0) / total
                out.found = True
        except Exception:
            pass
    return out


def read_parameters(host: str, sink: list | None = None) -> tuple[dict, str]:
    """The vehicle's parameter set, and the endpoint it came from.

    Worth keeping per flight because it is the configuration that produced
    the data. "Was the rangefinder's quality filter on in August?" is a
    lookup if this was captured and guesswork if it was not.
    """
    a = _first_ok(f"http://{host}", PARAM_PROBES, sink)
    if a is None:
        return {}, ""
    try:
        data = json.loads(a.body)
    except Exception:
        return {}, a.url

    if isinstance(data, list):
        out = {}
        for e in data:
            if isinstance(e, dict):
                name = e.get("param_id") or e.get("name") or e.get("id")
                if name:
                    out[str(name).rstrip("\x00").strip()] = (
                        e.get("param_value", e.get("value")))
        return out, a.url
    if isinstance(data, dict):
        # mavlink2rest wraps each message; unwrap one level if it did.
        inner = data.get("message") if isinstance(data.get("message"), dict) else None
        if inner and "param_id" in inner:
            return ({str(inner["param_id"]).rstrip("\x00").strip():
                     inner.get("param_value")}, a.url)
        return data, a.url
    return {}, a.url


#: Beacon, which knows what the vehicle calls itself.
BEACON_PORT = 9111

#: Beyond this many seconds apart, the Pi's clock and the laptop's disagree
#: enough to matter at a transect boundary.
SKEW_NOTE_S = 2.0
#: Beyond this, the recordings will be filed under the wrong time entirely.
SKEW_ALARM_S = 120.0


def vehicle_name(host: str) -> str:
    """What the vehicle calls itself.

    Worth showing before anything else. Two vehicles on this programme's own
    network both answer to the hostname `blueos` -- the ROV on the tether and
    a fixed camera on the wifi -- so the address is not an identity and the
    name is the only thing that distinguishes them.
    """
    a = _get(_base(host, BEACON_PORT) + "/v1.0/vehicle_name", timeout=6)
    return a.body.strip().strip('"')[:40] if a.ok else ""


@dataclass
class Readiness:
    """Everything checked before a dive, in one place."""

    host: str = ""
    name: str = ""
    reachable: bool = False
    version: str = ""
    space: Space = field(default_factory=Space)
    platform: Platform = field(default_factory=Platform)
    planned_seconds: float = 0.0
    #: Pi clock minus this laptop's, in seconds. None if it could not be read.
    skew: float | None = None
    soc_c: float | None = None
    soc_peak_c: float | None = None

    @property
    def clock_ok(self) -> bool:
        return self.skew is None or abs(self.skew) < SKEW_ALARM_S

    @property
    def ok(self) -> bool:
        return (self.reachable
                and self.space.verdict(self.planned_seconds)[0]
                and self.clock_ok)

    def clock_note(self) -> str:
        """What the clock difference means, in the terms that matter.

        The Pi has no battery-backed clock. With no internet it restores the
        last time it knew at boot and stays there -- so a vehicle that has
        been off since the last dive comes up believing it is still that day,
        and stamps everything it records accordingly. Nereo was found 8.09
        days behind on 2026-09-08, sitting exactly on its previous flight.
        """
        if self.skew is None:
            return "the vehicle's clock could not be read"
        d = abs(self.skew)
        if d < SKEW_NOTE_S:
            return f"clock agrees with this laptop ({self.skew:+.1f} s)"
        if d < SKEW_ALARM_S:
            return (f"clock is {self.skew:+.1f} s off this laptop -- enough to "
                    f"shift a transect boundary, not enough to lose a dive")
        days = d / 86400
        when = "behind" if self.skew < 0 else "ahead"
        size = f"{days:,.1f} days" if days >= 1 else f"{d / 60:,.0f} minutes"
        return (f"THE VEHICLE'S CLOCK IS {size.upper()} {when.upper()}. "
                f"Every recording will be stamped with that time, and the file "
                f"names come from it too, so today's dive would be filed under "
                f"the wrong date and could overwrite an earlier one. Set the "
                f"time in BlueOS before flying.")

    def lines(self) -> list[str]:
        if not self.reachable:
            return ["No vehicle answered. Check the tether."]
        out = [f"{self.name or 'unnamed vehicle'} at {self.host}",
               self.space.verdict(self.planned_seconds)[1],
               self.clock_note(),
               self.platform.note()]
        if self.soc_c is not None:
            out[-1] += f"   SoC {self.soc_c:.0f}C (peak {self.soc_peak_c:.0f}C)"
        tip = self.platform.advice()
        if tip:
            out.append(tip)
        return out


def check_readiness(host: str | None = None,
                    planned_seconds: float = 0.0) -> Readiness:
    """Space and Pi health, together, before anyone gets wet."""
    found = host or find_host()
    out = Readiness(planned_seconds=planned_seconds)
    if found is None:
        return out
    out.host, out.reachable = found, True
    out.name = vehicle_name(found)
    a = _get(f"http://{found}/version-chooser/v1.0/version/current")
    if a.ok:
        out.version = _first_string(a.body, ("version", "tag", "name"))
    out.space = read_space(found)
    out.platform = read_platform(found)
    out.skew = clock_skew(found)
    out.soc_c, out.soc_peak_c = read_temperature(found)
    return out


def save_snapshot(flight_dir: Path, host: str | None = None, *,
                  planned_seconds: float = 0.0) -> Path:
    """Record what the vehicle *was*, beside the flight it flew.

    Versions, parameters and the Pi's state at dive time. Behaviour has
    already changed underneath this programme twice -- the recorder's repair
    sweep rewriting old files, and a BlueOS beta -- and tying a data anomaly
    to a version change is straightforward with this and close to impossible
    without it. Written into the flight's own ``logs`` folder, so it travels
    with the data.
    """
    import datetime as dt

    found = host or find_host()
    snap: dict = {
        "taken": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": found or "",
        "reachable": found is not None,
    }
    if found is not None:
        token = file_token(found)
        rep = probe(host=found)
        space = read_space(found)
        plat = read_platform(found)
        versions = read_versions(found)
        soc, soc_peak = read_temperature(found)
        ok, verdict = space.verdict(planned_seconds)

        # Parameters come from the autopilot's own flight log rather than from
        # asking it. Nothing is sent to the vehicle, and what is captured is
        # the set as *flown* rather than as currently set.
        logs = list_dataflash(found, token)
        params: dict = {}
        changed: dict = {}
        from_log = ""
        if logs:
            from_log = logs[0]["name"]
            params = read_parameters_from_log(found, from_log, token)
            if len(logs) > 1 and params:
                before = read_parameters_from_log(found, logs[1]["name"], token)
                if before:
                    changed = {k: list(v) for k, v in
                               diff_parameters(before, params).items()}

        snap.update({
            "vehicle_name": vehicle_name(found),
            "vehicle_type": rep.vehicle,
            "versions": versions,
            "blueos_version": versions.get("blueos") or rep.version,
            "clock_skew_s": clock_skew(found),
            "services": rep.services,
            "disk": {"path": space.path, "free_bytes": space.free_bytes,
                     "total_bytes": space.total_bytes, "source": space.source,
                     "enough_room": ok, "verdict": verdict},
            "platform": {"model": plat.model, "ram_used": plat.ram_used,
                         "soc_c": soc, "soc_peak_c": soc_peak,
                         "throttle_events": plat.throttle,
                         "throttling_now": plat.occurring,
                         "first_event": plat.first_event,
                         "last_event": plat.last_event},
            "parameters": params,
            "parameters_from": from_log,
            "parameter_count": len(params),
            "parameters_changed_since_previous_flight": changed,
            "previous_flight_log": logs[1]["name"] if len(logs) > 1 else "",
        })

    out = Path(flight_dir) / "logs" / "vehicle_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    return out


def run(argv: list[str] | None = None) -> int:
    """`--probe-rov [report.txt]`, mirroring --selftest."""
    import sys
    import tempfile

    argv = list(argv or sys.argv)
    dest = None
    if "--probe-rov" in argv:
        i = argv.index("--probe-rov")
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            dest = Path(argv[i + 1])
    if dest is None:
        dest = Path(tempfile.gettempdir()) / "utc_rov_probe.txt"

    rep = probe(progress=lambda f, m="": print(f"  [{f * 100:3.0f}%] {m}",
                                               file=sys.stderr))
    text = rep.report()
    print(text)
    try:
        dest.write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {dest}")
    except Exception:
        pass
    return 0 if rep.reachable else 1


# --------------------------------------------------------------------------
#  the recordings: listing, spans, and fetching
# --------------------------------------------------------------------------
#
# Confirmed against a live vehicle on 2026-09-08 (BlueOS 1.5.0-beta.34,
# ArduSub 4.5.7, Raspberry Pi 4 B). Three things were established there and
# each of them shapes what follows.
#
# **The recorder extension will not serve an mcap.** Its /recorder/files lists
# only the MP4s it has extracted, and asking it for an .mcap is refused
# outright: "Only .mp4 recordings are supported." So the recordings themselves
# have to come from somewhere else.
#
# **File Browser serves them, and hands out a token to anyone who asks.** A
# plain GET to /api/login -- no credentials -- returns a JWT with create,
# modify and delete permissions. Nothing here ever uses those: this module
# issues GETs and nothing else, and `verify_read_only` exists so that is a
# checked property rather than a promise.
#
# **Range requests work**, which is the one that matters. A recording's true
# recorded span can be read from its first kilobytes -- about 96 KiB and 75
# milliseconds per file -- instead of downloading 350 MB to find out. Judging
# recordings on their span rather than their name or their modification time
# is the whole point of this feature, and it is only affordable because of
# this.


def _base(host: str, port: int) -> str:
    """Base URL for one service.

    A host given with an explicit port is used as it stands. That is how the
    tests reach a single fake vehicle serving every service, and it is also
    what lets someone point this at an SSH tunnel or a port-forward rather
    than at the vehicle's own network.
    """
    return f"http://{host}" if ":" in host else f"http://{host}:{port}"


def file_token(host: str, timeout: float = 6.0) -> str:
    """A File Browser session token, or "" if it will not give one.

    The vehicle's File Browser is configured without authentication, so a GET
    to /api/login returns a token to anybody who can reach the port. That is
    BlueOS's decision, not this programme's; what this programme controls is
    that it only ever reads.
    """
    a = _get(_base(host, FILE_BROWSER_PORT) + "/api/login", timeout=timeout)
    return a.body.strip() if a.ok and a.body.strip() else ""


def list_recordings(host: str, token: str = "",
                    folder: str = RECORDER_FB_PATH) -> list[dict]:
    """Every .mcap on the vehicle, with its size and modification time.

    The modification time is reported but deliberately not trusted: BlueOS
    rewrites it when its repair sweep touches an old recording, which is how a
    file from a previous day came to look like it belonged to the dive. Use
    `read_span` to settle which day a recording is actually from.
    """
    token = token or file_token(host)
    if not token:
        return []
    url = (_base(host, FILE_BROWSER_PORT) + "/api/resources"
           + urllib.parse.quote(folder))
    a = _get(url, headers={"X-Auth": token}, limit=4_000_000)
    if not a.ok:
        return []
    try:
        items = json.loads(a.body).get("items", []) or []
    except Exception:
        return []
    return [
        {"name": i.get("name", ""),
         "size": int(i.get("size") or 0),
         "modified": i.get("modified", "")}
        for i in items
        if str(i.get("name", "")).endswith(".mcap") and not i.get("isDir")
    ]


def recording_url(host: str, name: str, token: str,
                  folder: str = RECORDER_FB_PATH) -> str:
    """The raw-download URL for one recording."""
    return (_base(host, FILE_BROWSER_PORT) + "/api/raw"
            + urllib.parse.quote(folder) + "/" + urllib.parse.quote(name)
            + f"?auth={urllib.parse.quote(token)}")


def read_span(host: str, name: str, token: str, *,
              kib: int = 96) -> tuple[float | None, float | None]:
    """The recorded span, read from the file's first bytes.

    Returns ``(start, end)`` as epoch seconds; `end` is None because it lives
    in the summary at the *end* of the file, and a truncated recording has no
    summary at all. The start is enough to place a recording on a day, which
    is what the matching needs.
    """
    a = _get(recording_url(host, name, token),
             headers={"Range": f"bytes=0-{kib * 1024 - 1}"},
             limit=kib * 1024, binary=True)
    if not a.ok or not a.raw or not a.raw.startswith(MCAP_MAGIC):
        return None, None
    return _first_chunk_start(a.raw), None


def _first_chunk_start(head: bytes) -> float | None:
    """Walk the record stream for the first CHUNK's message_start_time.

    An mcap record is opcode(1) + length(uint64 LE) + payload, and a CHUNK's
    payload opens with message_start_time as nanoseconds. Reading it directly
    avoids handing a deliberately truncated file to a parser that expects a
    whole one.
    """
    pos = len(MCAP_MAGIC)
    while pos + 9 <= len(head):
        op = head[pos]
        (length,) = struct.unpack_from("<Q", head, pos + 1)
        body = pos + 9
        if op == OP_CHUNK and body + 8 <= len(head):
            (start_ns,) = struct.unpack_from("<Q", head, body)
            return start_ns / 1e9 if start_ns else None
        if length > 1 << 30:            # nonsense length: stop rather than seek
            return None
        pos = body + length
    return None


def open_recording(host: str, name: str, token: str):
    """A readable stream for one recording, for `rovfetch.fetch`."""
    req = urllib.request.Request(recording_url(host, name, token), method="GET")
    req.add_header("User-Agent", _UA)
    return urllib.request.urlopen(req, timeout=30)


def clock_skew(host: str) -> float | None:
    """Pi clock minus this laptop's, in seconds.

    Worth knowing before a dive rather than after one. Recordings are stamped
    with the Pi's clock and transect times are written from the laptop's, so a
    skew is a constant offset between the two -- and it is silent. The vehicle
    checked on 2026-09-08 was 3.8 seconds ahead.
    """
    t0 = time.time()
    a = _get(_base(host, LINUX2REST_PORT) + "/system/unix_time_seconds",
             timeout=8)
    t1 = time.time()
    if not a.ok:
        return None
    try:
        return float(a.body.strip()) - (t0 + t1) / 2
    except ValueError:
        return None


def read_temperature(host: str) -> tuple[float | None, float | None]:
    """(now, highest seen) in Celsius for the Pi's SoC.

    The Pi caps its own clock at about 80C. It sits in a sealed tube with no
    airflow, so this is the number behind the FrequencyCapping events that
    turned up in this programme's September recordings.
    """
    a = _get(_base(host, LINUX2REST_PORT) + "/system/temperature", timeout=8)
    if not a.ok:
        return None, None
    try:
        rows = json.loads(a.body)
        if rows:
            return (rows[0].get("temperature"),
                    rows[0].get("maximum_temperature"))
    except Exception:
        pass
    return None, None


#: The live messages worth showing before a dive, and what to pull from each.
TELEMETRY = {
    "SYS_STATUS": ("voltage_battery", "current_battery", "battery_remaining"),
    "SCALED_PRESSURE": ("press_abs", "temperature"),
    "VFR_HUD": ("alt", "heading"),
    "ATTITUDE": ("roll", "pitch", "yaw"),
    "EKF_STATUS_REPORT": ("velocity_variance", "pos_horiz_variance",
                          "compass_variance"),
    "VIBRATION": ("vibration_x", "vibration_y", "vibration_z"),
    "GPS_RAW_INT": ("fix_type", "satellites_visible"),
    "HEARTBEAT": ("system_status",),
}


def read_telemetry(host: str, want: dict | None = None) -> dict:
    """A snapshot of the vehicle's live MAVLink, read over HTTP.

    mavlink2rest serves the most recent of each message type at its own URL,
    so this is a handful of GETs and no MAVLink connection of its own. Only
    reads: asking the autopilot for something it is not already broadcasting
    means sending it a message, which this does not do.
    """
    out: dict = {}
    base = (_base(host, MAVLINK2REST_PORT)
            + "/v1/mavlink/vehicles/1/components/1/messages")
    for name, keys in (want or TELEMETRY).items():
        a = _get(f"{base}/{name}", timeout=6, limit=40_000)
        if not a.ok:
            continue
        try:
            data = json.loads(a.body)
        except Exception:
            continue
        msg = data.get("message", data)
        if isinstance(msg, dict):
            out[name] = {k: msg.get(k) for k in keys if k in msg}
    return out


def parameter_count(host: str) -> int:
    """How many parameters the autopilot has.

    The full set is *not* readable without asking for it: mavlink2rest keeps
    only the most recent PARAM_VALUE, and getting all of them means sending
    the vehicle a PARAM_REQUEST_LIST. That is a write to the vehicle bus, so
    it is deliberately not done here -- the count comes free with the one
    parameter that is already being broadcast, and capturing the whole set
    stays a decision for a human.
    """
    a = _get(_base(host, MAVLINK2REST_PORT)
             + "/v1/mavlink/vehicles/1/components/1/messages/PARAM_VALUE",
             timeout=8, limit=40_000)
    if not a.ok:
        return 0
    try:
        msg = json.loads(a.body).get("message", {})
        return int(msg.get("param_count") or 0)
    except Exception:
        return 0


# --------------------------------------------------------------------------
#  what the vehicle was: parameters, and the software running them
# --------------------------------------------------------------------------
#
# The parameters come from ArduPilot's own dataflash logs, not from asking the
# autopilot. That was not the obvious route -- the obvious one is to send a
# PARAM_REQUEST_LIST and collect the reply -- but it is the better one on
# every count that matters here.
#
# It is a **read**. Nothing is sent to the vehicle at all, so the read-only
# guarantee this module makes stays intact rather than acquiring an exception.
#
# It is **per flight**. A dataflash log opens with every parameter as it stood
# for that flight, so the question is answerable retrospectively: 80 logs going
# back to 2025 were on the vehicle when this was written, and two of them
# already disagree on how many parameters exist (1,044 against 1,014). Asking
# the autopilot only ever answers "now".
#
# It is **cheap**. The parameter block sits at the head, so 512 KiB is enough
# -- about a third of a second -- against 78 MiB for the largest whole log.

#: Where ArduPilot's dataflash logs sit, as File Browser addresses them.
DATAFLASH_FB_PATH = "/ardupilot_logs/firmware/logs"

#: Enough of a log's head to carry the parameter block. Measured: 512 KiB
#: yielded all 1,044 parameters on every log tried.
DATAFLASH_HEAD_KIB = 512


def list_dataflash(host: str, token: str = "") -> list[dict]:
    """The autopilot's own flight logs, newest first."""
    token = token or file_token(host)
    if not token:
        return []
    a = _get(_base(host, FILE_BROWSER_PORT) + "/api/resources"
             + urllib.parse.quote(DATAFLASH_FB_PATH),
             headers={"X-Auth": token}, limit=4_000_000)
    if not a.ok:
        return []
    try:
        items = json.loads(a.body).get("items", []) or []
    except Exception:
        return []
    logs = [{"name": i.get("name", ""), "size": int(i.get("size") or 0),
             "modified": i.get("modified", "")}
            for i in items
            if str(i.get("name", "")).upper().endswith(".BIN")
            and not i.get("isDir")]
    return sorted(logs, key=lambda i: i["name"], reverse=True)


def read_parameters_from_log(host: str, name: str, token: str = "", *,
                             kib: int = DATAFLASH_HEAD_KIB) -> dict:
    """Every parameter as it stood for one flight.

    Reads the head of the log and parses its PARM records. The file is
    deliberately truncated, which pymavlink handles: it stops when it runs out
    of data, and by then the parameter block is long past.
    """
    token = token or file_token(host)
    if not token:
        return {}
    url = (_base(host, FILE_BROWSER_PORT) + "/api/raw"
           + urllib.parse.quote(DATAFLASH_FB_PATH) + "/"
           + urllib.parse.quote(name) + f"?auth={urllib.parse.quote(token)}")
    a = _get(url, headers={"Range": f"bytes=0-{kib * 1024 - 1}"},
             limit=kib * 1024, binary=True, timeout=60)
    if not a.ok or not a.raw:
        return {}
    return parse_parameters(a.raw)


def parse_parameters(head: bytes) -> dict:
    """PARM records out of a dataflash log, or the head of one.

    pymavlink reads dataflash from a path rather than from bytes, so the head
    goes to a temporary file. It must be a *uniquely named* one that is closed
    before it is removed: the reader holds the handle open, and Windows will
    not unlink a file another handle still has.
    """
    import tempfile

    from pymavlink import mavutil

    fd, path = tempfile.mkstemp(prefix="utc_dataflash_", suffix=".bin")
    tmp = Path(path)
    conn = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(head)
        # pymavlink prints "bad header" to stdout for every byte past the cut,
        # and the file is cut on purpose. Thousands of lines of that would
        # bury whatever the operator was actually reading.
        with contextlib.redirect_stdout(io.StringIO()):
            conn = mavutil.mavlink_connection(str(tmp))
            out = {}
            while True:
                msg = conn.recv_match(type=["PARM"])
                if msg is None:
                    break
                out[msg.Name] = msg.Value
        return out
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        tmp.unlink(missing_ok=True)


def diff_parameters(before: dict, after: dict) -> dict:
    """What changed between two flights: name -> (before, after).

    A parameter absent from one side reads as None, which distinguishes
    "added" and "removed" from "changed" -- a firmware update moves parameters
    in and out, and that is worth telling apart from somebody turning a knob.
    """
    out = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old != new:
            out[name] = (old, new)
    return out


def parameter_history(host: str, token: str = "", *,
                      limit: int = 12) -> list[dict]:
    """Recent flights, each with what its parameters changed from the last.

    The question this exists for is "what did we change, and when?". Answered
    against the logs the vehicle already holds, so it works retrospectively --
    including for flights that happened before this programme existed.
    """
    token = token or file_token(host)
    logs = list_dataflash(host, token)[:limit]
    out: list[dict] = []
    previous: dict | None = None
    for log in reversed(logs):                  # oldest first, so diffs read forward
        params = read_parameters_from_log(host, log["name"], token)
        if not params:
            continue
        entry = {"log": log["name"], "modified": log["modified"],
                 "count": len(params)}
        if previous is not None:
            entry["changed"] = diff_parameters(previous, params)
        out.append(entry)
        previous = params
    out.reverse()                               # newest first for a reader
    return out


def read_versions(host: str) -> dict:
    """Every version that could explain a change in the data.

    BlueOS, ArduSub, the flight controller, and each installed extension with
    its tag. Behaviour has shifted underneath this programme more than once,
    and a version recorded at the time turns "why does August look different?"
    from an argument into a lookup.
    """
    out: dict = {"blueos": "", "ardusub": "", "ardusub_type": "", "board": "",
                 "extensions": [], "containers": []}

    a = _get(f"http://{host}/version-chooser/v1.0/version/current", timeout=10)
    if a.ok:
        out["blueos"] = _first_string(a.body, ("version", "tag", "name"))

    a = _get(f"http://{host}/ardupilot-manager/v1.0/firmware_info", timeout=10)
    if a.ok:
        try:
            fw = json.loads(a.body)
            out["ardusub"] = fw.get("version", "")
            out["ardusub_type"] = fw.get("type", "")
        except Exception:
            pass

    a = _get(f"http://{host}/ardupilot-manager/v1.0/board", timeout=10)
    if a.ok:
        try:
            out["board"] = json.loads(a.body).get("name", "")
        except Exception:
            pass

    a = _get(_base(host, KRAKEN_PORT) + "/v2.0/installed_extensions",
             timeout=25, limit=900_000)
    if a.ok:
        try:
            for e in json.loads(a.body) or []:
                out["extensions"].append({
                    "name": e.get("name") or e.get("identifier", ""),
                    "tag": e.get("tag") or e.get("version", ""),
                    "enabled": bool(e.get("enabled")),
                })
        except Exception:
            pass

    # The containers say what is actually running, tag and all, which is not
    # always what is installed -- an extension can be updated and not restarted.
    a = _get(_base(host, KRAKEN_PORT) + "/v2.0/container/", timeout=25,
             limit=900_000)
    if a.ok:
        try:
            for c in json.loads(a.body) or []:
                out["containers"].append({
                    "name": str(c.get("name", "")).lstrip("/"),
                    "image": c.get("image", ""),
                    "status": c.get("status", ""),
                })
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
#  the flight itself: is it armed, and what was set while it flew
# --------------------------------------------------------------------------
#
# Arming is read from the HEARTBEAT that mavlink2rest already holds. Bit 7 of
# `base_mode` is MAV_MODE_FLAG_SAFETY_ARMED, and it is the only honest marker
# of when a flight began: the pilot's own action, recorded by the autopilot,
# rather than somebody remembering to press a button in this programme.
#
# The parameters are then read the same way the rest of this module reads
# them -- out of the autopilot's own dataflash log -- but *whole* rather than
# just the head. The head carries the block ArduPilot writes when a log opens;
# a parameter changed later in the flight appears as its own PARM record
# further in, and reading only the first 512 KiB would miss exactly the
# changes worth recording.
#
# That costs a download. Measured against Nereo on 2026-09-11: a 16.7 MB log
# came down in 2.0 s at 8.2 MB/s and parsed in 0.2 s, and the three parameters
# that had moved within it were all ones the autopilot sets itself. A long
# dive's log is larger, but this happens twice a flight, at arming and after
# disarming, with the vehicle on the surface either side. Nothing is sent to
# the vehicle: this stays a read.

#: MAV_MODE_FLAG_SAFETY_ARMED.
ARMED_BIT = 0b1000_0000


def read_arm_state(host: str, timeout: float = 6.0) -> tuple[bool | None, float | None]:
    """(armed, round-trip milliseconds) from the vehicle's last HEARTBEAT.

    `None` for armed means the question could not be answered -- the vehicle
    did not reply, or replied with something unparseable. It never guesses
    "disarmed", because a recorder that ended a flight on one dropped request
    would stop recording in the middle of a transect.
    """
    t0 = time.time()
    a = _get(_base(host, MAVLINK2REST_PORT)
             + "/v1/mavlink/vehicles/1/components/1/messages/HEARTBEAT",
             timeout=timeout, limit=20_000)
    ms = (time.time() - t0) * 1000
    if not a.ok:
        return None, None
    try:
        msg = json.loads(a.body).get("message", {})
        bits = msg.get("base_mode", {})
        bits = bits.get("bits") if isinstance(bits, dict) else bits
        if bits is None:
            return None, ms
        return bool(int(bits) & ARMED_BIT), ms
    except Exception:
        return None, ms


def read_parameters_full(host: str, name: str, token: str = "", *,
                         progress: ProgressCB | None = None) -> dict:
    """Every parameter in one dataflash log, at its **last** recorded value.

    ArduPilot writes a PARM record for every parameter when a log opens, and
    another whenever one is changed afterwards. Taking the last occurrence of
    each name therefore gives the set as it stood when the log was read, which
    is what a snapshot at disarming is supposed to be.

    Streamed to a temporary file rather than held in memory: a long dive's log
    runs to tens of megabytes, and pymavlink wants a path in any case.
    """
    import tempfile

    token = token or file_token(host)
    if not token:
        return {}
    url = (_base(host, FILE_BROWSER_PORT) + "/api/raw"
           + urllib.parse.quote(DATAFLASH_FB_PATH) + "/"
           + urllib.parse.quote(name) + f"?auth={urllib.parse.quote(token)}")
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _UA)

    fd, path = tempfile.mkstemp(prefix="utc_parm_", suffix=".bin")
    tmp = Path(path)
    try:
        with os.fdopen(fd, "wb") as fh, \
                urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(min(0.95, got / total),
                             f"reading {name}  {got / 2 ** 20:,.0f} of "
                             f"{total / 2 ** 20:,.0f} MiB")
        return _parse_parameters_file(tmp)
    except Exception:
        return {}
    finally:
        tmp.unlink(missing_ok=True)


def _parse_parameters_file(path: Path) -> dict:
    """Last value of every PARM record in a dataflash log on disk."""
    from pymavlink import mavutil

    conn = None
    try:
        # pymavlink prints a line per unparseable byte, and a log the vehicle
        # is still writing ends mid-record. Thousands of those would bury
        # whatever the operator was actually reading.
        with contextlib.redirect_stdout(io.StringIO()):
            conn = mavutil.mavlink_connection(str(path))
            out: dict = {}
            while True:
                msg = conn.recv_match(type=["PARM"])
                if msg is None:
                    break
                out[msg.Name] = msg.Value
        return out
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def read_parameters_now(host: str, token: str = "", *, since_log: str = "",
                        progress: ProgressCB | None = None) -> tuple[dict, str]:
    """The parameter set as it stands, and which log(s) it was read from.

    Normally one log: the newest, read whole. When `since_log` names a log
    that is no longer the newest -- ArduPilot opened a new one part way
    through, which a reboot will do -- every log from that one onward is read
    oldest-first so that later values win. Without this a flight that rotated
    its log would take its closing snapshot from a file that stopped before
    the dive ended.
    """
    token = token or file_token(host)
    logs = list_dataflash(host, token)
    if not logs:
        return {}, ""
    take = [logs[0]]
    if since_log:
        # Names are zero-padded sequence numbers, so they sort as they run.
        newer = [x for x in logs if x["name"] >= since_log]
        if newer:
            take = sorted(newer, key=lambda x: x["name"])
    params: dict = {}
    for entry in take:
        params.update(read_parameters_full(host, entry["name"], token,
                                           progress=progress))
    return params, ", ".join(x["name"] for x in take)


def newest_dataflash(host: str, token: str = "") -> str:
    """The name of the log the autopilot is writing to now, or ""."""
    logs = list_dataflash(host, token)
    return logs[0]["name"] if logs else ""


def diff_versions(before: dict, after: dict) -> dict:
    """What moved between two `read_versions` readings.

    Flattened to one name per line -- `blueos`, `ardusub`, `extension:Madrona`,
    `container:blueos-core` -- because "what changed?" is a question about a
    component, not about the shape of the JSON it happened to arrive in. A
    component present on one side only reads as None on the other, which tells
    an installation apart from an upgrade.
    """
    def flatten(v: dict) -> dict:
        out = {}
        for key in ("blueos", "ardusub", "ardusub_type", "board"):
            if key in v:
                out[key] = v.get(key)
        # Entries are skipped rather than trusted to be dictionaries. A
        # vehicle half-way through starting an extension, or a snapshot file
        # edited by hand, has produced a list of something else before, and a
        # comparison that raises takes the whole flight record with it.
        for e in v.get("extensions") or []:
            if not isinstance(e, dict):
                continue
            name = e.get("name") or ""
            if name:
                tag = e.get("tag", "")
                out[f"extension:{name}"] = (
                    tag if e.get("enabled", True) else f"{tag} (disabled)")
        for c in v.get("containers") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name") or ""
            if name:
                out[f"container:{name}"] = c.get("image", "")
        return out

    a, b = flatten(before), flatten(after)
    return {k: (a.get(k), b.get(k))
            for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


#: Parameters ArduPilot moves by itself, which are not somebody turning a
#: knob. Barometer ground pressure is re-zeroed on arming and the statistics
#: counters tick on their own -- all three turned up in the first real log
#: this was tested against. They are still recorded, because a delta file that
#: quietly dropped readings would be worse than a noisy one, but they are
#: marked so that a real change is not lost among them.
AUTOMATIC_PARAMETERS = (
    "BARO1_GND_PRESS", "BARO2_GND_PRESS", "BARO3_GND_PRESS",
    "STAT_RUNTIME", "STAT_BOOTCNT", "STAT_FLTTIME",
    "INS_ACC1_ID", "INS_ACC2_ID", "INS_ACC3_ID",
    "INS_GYR1_ID", "INS_GYR2_ID", "INS_GYR3_ID",
    "COMPASS_DEV_ID", "COMPASS_DEV_ID2", "COMPASS_DEV_ID3",
)


def is_automatic(name: str) -> bool:
    """Does the autopilot set this one itself?"""
    return name in AUTOMATIC_PARAMETERS


# --------------------------------------------------------------------------
#  the vehicle's own side of the tether
# --------------------------------------------------------------------------
#
# The topside can prove its packets left and never say whether they arrived.
# The Pi counts every frame its Ethernet port takes in, and the counter is
# cumulative, so it survives the very outage it is read to explain: taken once
# the link returns, it says how many bytes came in while the link was down.
#
# A flat counter across a blackout means the topside's frames never reached
# the vehicle. A counter that kept climbing means they did, and the fault is
# on the way back. That single difference is worth more than anything the
# laptop can establish on its own, and it costs one GET.


#: Where linux2rest has published per-interface counters across the BlueOS
#: releases this has been run against. Tried in order, first answer wins, on
#: the same principle as the rest of this module: the API is discovered,
#: because it has moved before.
NETWORK_PATHS = (
    "/system/network",
    "/system/v1.0/network",
    "/v1.0/system/network",
)

#: Names linux2rest has used for the same counter. The service renamed
#: several between releases, and a reader that knows only one of them reports
#: a healthy zero on a vehicle that uses the other.
_NET_KEYS = {
    "rx_bytes": ("total_received_B", "total_received_b", "received_B",
                 "total_received_bytes", "rx_bytes"),
    "tx_bytes": ("total_transmitted_B", "total_transmitted_b",
                 "transmitted_B", "total_transmitted_bytes", "tx_bytes"),
    "rx_packets": ("total_packets_received", "packets_received", "rx_packets"),
    "tx_packets": ("total_packets_transmitted", "packets_transmitted",
                   "tx_packets"),
    "rx_errors": ("total_errors_on_received", "errors_on_received",
                  "rx_errors"),
    "tx_errors": ("total_errors_on_transmitted", "errors_on_transmitted",
                  "tx_errors"),
}


def _pick_int(entry: dict, names: tuple[str, ...]):
    for name in names:
        if name in entry:
            try:
                return int(entry[name])
            except (TypeError, ValueError):
                return None
    return None


#: Where each host's counters and tether diagnostics were last found, so a
#: vehicle is walked once rather than on every poll. A blackout is the worst
#: time to be trying sixteen URLs: each one costs its full timeout, and the
#: caller is usually the loop that also has to notice the vehicle disarming.
_NETWORK_PATH_FOUND: dict[str, str] = {}
_TETHER_FOUND: dict[str, tuple[int, str] | None] = {}
_TETHER_PROBED_AT: dict[str, float] = {}

#: How long before a vehicle that had no tether diagnostics is asked again.
#: Not never: the extension was started mid-flight once already, and a probe
#: every couple of minutes is cheap enough to catch that.
TETHER_REPROBE_S = 120.0


def read_interfaces(host: str, timeout: float = 2.5) -> dict[str, dict]:
    """The Pi's network counters, as {interface name: counters}.

    Cumulative since the interface came up rather than rates -- the same
    choice the topside trace makes, for the same reason. Returns {} rather
    than raising when the vehicle cannot be reached, which during an outage
    is the expected answer and not an error.
    """
    base = _base(host, LINUX2REST_PORT)
    known = _NETWORK_PATH_FOUND.get(host)
    paths = (known,) if known else NETWORK_PATHS
    for path in paths:
        a = _get(base + path, timeout=timeout)
        if not a.ok:
            continue
        try:
            body = json.loads(a.body)
        except Exception:
            continue
        rows = body if isinstance(body, list) else body.get("networks", [])
        if not isinstance(rows, list):
            continue
        out: dict[str, dict] = {}
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("interface") or "")
            if not name:
                continue
            found = {k: _pick_int(entry, names) for k, names in _NET_KEYS.items()}
            mac = entry.get("description")
            if mac:
                found["mac"] = str(mac)
            out[name] = found
        if out:
            _NETWORK_PATH_FOUND[host] = path
            return out
    return {}


def tether_interface(interfaces: dict[str, dict]) -> str:
    """Which of the Pi's interfaces is the tether. `eth0` unless it is not.

    Named rather than assumed. A vehicle that reaches the surface on a second
    port would otherwise have its idle interface measured and reported as a
    perfectly healthy zero -- the same mistake the topside made by measuring
    the bridge instead of the adapter under it.
    """
    for name in ("eth0", "end0", "enp1s0", "eth1"):
        if name in interfaces:
            return name
    best, most = "", 0
    for name, counters in interfaces.items():
        if name.startswith(("lo", "docker", "veth", "br-", "wl", "tun")):
            continue
        seen = counters.get("rx_bytes") or 0
        if seen > most:
            best, most = name, seen
    return best


# --------------------------------------------------------------------------
#  the tether's own link rate
# --------------------------------------------------------------------------
#
# The Fathom-X boards are a HomePlug pair. Each Ethernet side stays up at
# 100 Mbps whatever the powerline side is doing, which is exactly why a tether
# that has lost sync looks healthy from both computers: the number that would
# say otherwise is the rate the two modems have negotiated with each other,
# and neither operating system can see it.
#
# The `williangalvani.plc-diagnostics` extension reads it from the vehicle
# side. It is an extension rather than a service, so its port and its routes
# are whatever that release chose, and the only safe way to read it is to ask
# what it serves and keep whatever comes back.

#: Where the tether diagnostics extension has been seen to listen. 1142 is the
#: port BlueOS' own service scan reported it on during the September flight.
TETHER_PORTS = (1142, 9992)

TETHER_PATHS = (
    "/status", "/v1.0/status", "/plc", "/stats", "/v1.0/stats",
    "/diagnostics", "/api/status", "/",
)

#: Keys that have carried a negotiated rate, in Mbps. The Qualcomm firmware
#: reports the two directions separately and either one falling is the signal,
#: so both are kept.
_TETHER_KEYS = {
    "rx_mbps": ("rx_rate", "rx_mbps", "rate_rx", "receive_rate", "rx"),
    "tx_mbps": ("tx_rate", "tx_mbps", "rate_tx", "transmit_rate", "tx"),
}


def read_tether(host: str, timeout: float = 1.5) -> dict:
    """Whatever the tether diagnostics extension will say about the link.

    Returns {} when the extension is not installed or not running, which is
    not a failure -- it is a vehicle without that extension. When it does
    answer, the body is kept verbatim under `raw` beside any rate that could
    be recognised, because a key this does not know yet is still evidence
    once a person reads the file.
    """
    known = _TETHER_FOUND.get(host, "unknown")
    if known is None:
        # This vehicle has already been walked and had nothing. Ask again
        # occasionally, in case the extension has since been started, but
        # never on every poll.
        if time.time() - _TETHER_PROBED_AT.get(host, 0.0) < TETHER_REPROBE_S:
            return {}
        known = "unknown"
    candidates = ([known] if isinstance(known, tuple)
                  else [(port, path) for port in TETHER_PORTS
                        for path in TETHER_PATHS])
    _TETHER_PROBED_AT[host] = time.time()
    for port, path in candidates:
        a = _get(_base(host, port) + path, timeout=timeout)
        if not a.ok or not a.body.strip() or "json" not in a.kind.lower():
            continue
        try:
            body = json.loads(a.body)
        except Exception:
            continue
        flat = body if isinstance(body, dict) else {}
        if isinstance(body, list) and body and isinstance(body[0], dict):
            flat = body[0]
        found: dict = {"port": port, "path": path, "raw": body}
        for label, names in _TETHER_KEYS.items():
            for name in names:
                if name in flat:
                    try:
                        found[label] = float(flat[name])
                    except (TypeError, ValueError):
                        pass
                    break
        _TETHER_FOUND[host] = (port, path)
        return found
    _TETHER_FOUND[host] = None
    return {}
