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

import json
import socket
import ssl
import time
import urllib.error
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

#: Endpoints worth trying for a directory listing or a download. None of these
#: is promised; the probe reports which (if any) answer.
FILE_PROBES = (
    "/file-browser/api/resources/userdata/recorder",
    "/filebrowser/api/resources/userdata/recorder",
    "/api/resources/userdata/recorder",
    "/recorder-extractor/get_status",
    "/recorder-extractor/list",
    "/recorder/list",
)

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

        out += [f"vehicle at   : {self.host}",
                f"BlueOS       : {self.version or 'unknown'}",
                f"vehicle type : {self.vehicle or 'unknown'}",
                f"range reads  : {_range_word(self.range_supported)}", ""]
        out += ["before a dive:",
                f"    space    : {self.space.verdict()[1]}",
                f"    the Pi   : {self.platform.note()}"]
        if self.platform.first_event:
            out.append(f"    throttled: {self.platform.first_event} .. "
                       f"{self.platform.last_event}")
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
         limit: int = 64_000) -> Answer:
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
                          body=raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        return Answer(url=url, ok=False, status=ex.code,
                      seconds=time.time() - t0, error=f"HTTP {ex.code}")
    except Exception as ex:
        return Answer(url=url, ok=False, seconds=time.time() - t0,
                      error=f"{type(ex).__name__}: {str(ex)[:80]}")


def find_host(hosts: Iterable[str] = DEFAULT_HOSTS,
              timeout: float = 2.0) -> str | None:
    """The first candidate with something listening on port 80."""
    for h in hosts:
        try:
            with socket.create_connection((h, 80), timeout=timeout):
                return h
        except OSError:
            continue
    return None


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
    # is the whole point.
    served = [a for a in out.answers if a.ok and "recorder" in a.url]
    if served:
        r = _get(served[0].url, headers={"Range": "bytes=0-1023"}, limit=2048)
        out.range_supported = (r.status == 206)
        out.answers.append(r)
    tick("range support…")

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


def _first_ok(base: str, paths: Iterable[str],
              sink: list | None = None) -> Answer | None:
    """The first candidate endpoint that answers, or None.

    Every attempt is appended to `sink` when one is given, misses included.
    Which candidates were tried and what they returned is the whole point of
    running the probe beside a real vehicle -- a reading that quietly fell
    through to the third candidate is something to know.
    """
    for path in paths:
        a = _get(base + path)
        if sink is not None:
            sink.append(a)
        if a.ok:
            return a
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


@dataclass
class Readiness:
    """Everything checked before a dive, in one place."""

    host: str = ""
    reachable: bool = False
    version: str = ""
    space: Space = field(default_factory=Space)
    platform: Platform = field(default_factory=Platform)
    planned_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.reachable and self.space.verdict(self.planned_seconds)[0]

    def lines(self) -> list[str]:
        if not self.reachable:
            return ["No vehicle answered. Check the tether."]
        out = [self.space.verdict(self.planned_seconds)[1], self.platform.note()]
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
    a = _get(f"http://{found}/version-chooser/v1.0/version/current")
    if a.ok:
        out.version = _first_string(a.body, ("version", "tag", "name"))
    out.space = read_space(found)
    out.platform = read_platform(found)
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
        rep = probe(host=found)
        space = read_space(found)
        plat = read_platform(found)
        params, source = read_parameters(found)
        ok, verdict = space.verdict(planned_seconds)
        snap.update({
            "blueos_version": rep.version,
            "vehicle_type": rep.vehicle,
            "services": rep.services,
            "disk": {"path": space.path, "free_bytes": space.free_bytes,
                     "total_bytes": space.total_bytes, "source": space.source,
                     "enough_room": ok, "verdict": verdict},
            "platform": {"model": plat.model, "ram_used": plat.ram_used,
                         "throttle_events": plat.throttle,
                         "throttling_now": plat.occurring,
                         "first_event": plat.first_event,
                         "last_event": plat.last_event},
            "parameters": params,
            "parameters_from": source,
            "parameter_count": len(params),
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
