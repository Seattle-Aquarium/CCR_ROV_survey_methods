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

    steps = len(CORE_PROBES) + len(FILE_PROBES) + 1
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
