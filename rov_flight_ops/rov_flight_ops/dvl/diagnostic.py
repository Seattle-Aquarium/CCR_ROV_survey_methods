"""
Water Linked's diagnostic log: the DVL's own capture for their support team.

The DVL's web GUI has a page that records 15 seconds to 5 minutes of the
DVL's internal logs and measurements and hands back a file for Water Linked
support. It is the deepest view of the DVL anyone outside Water Linked can
ask for, and the one thing on the DVL that this program cannot capture any
other way -- so it is here, but only on request:

* **Only with the vehicle confirmed disarmed.** What the DVL does to its live
  output while it records is not documented. The capture running beside it
  will show it if it does anything; a vehicle in the middle of a transect is
  not where to find out.
* **Only by a person pressing the button**, with a description, because Water
  Linked's form requires one and because what was happening is half of what
  makes the file useful.

It is a GET -- `/api/collect?desc=…&t=…`, exactly as the DVL's own form sends
it (an HTML form with no method) -- and it changes nothing on the DVL. The
demo DVL does not simulate it (it answers "406 - Sorry not simulated"), so
the shape of the file that comes back has not been seen yet: it is saved as
it arrives, named from the DVL's `Content-Disposition` when there is one and
from its first bytes when there is not, with its SHA-256 recorded beside it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import webapi
from .capture import os_path

#: The durations Water Linked's form offers, in seconds. 15 is its default.
DURATIONS_S = (15, 30, 60, 300)
DEFAULT_DURATION_S = 15
COLLECT_PATH = "/api/collect"
#: On top of the capture itself, how long the DVL may take to package it.
PACKAGING_ALLOWANCE_S = 120.0


class Refused(RuntimeError):
    """The vehicle was not confirmed disarmed, or the request was incomplete."""


@dataclass
class Result:
    path: str = ""
    sidecar: str = ""
    bytes: int = 0
    sha256: str = ""
    status: int | None = None
    content_type: str = ""
    filename_from_dvl: str = ""
    seconds_requested: int = 0
    description: str = ""
    started_utc: str = ""
    finished_utc: str = ""
    elapsed_s: float = 0.0
    error: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.path)


def require_disarmed(armed_state: Callable[[], tuple[bool | None, bool]]) -> None:
    """Raise `Refused` unless the vehicle is known, now, to be disarmed.

    `armed_state` returns (armed, current): armed is None when nobody knows,
    and current is False when the last answer is too old to trust. Only
    (False, True) passes -- an unknown arm state is not a disarmed one.
    """
    try:
        armed, current = armed_state()
    except Exception as ex:
        raise Refused(f"could not read the arm state: {ex}") from ex
    if armed is None or not current:
        raise Refused("the vehicle's arm state is not known right now — it has "
                      "to be confirmed disarmed. Is monitoring running and the "
                      "tether connected?")
    if armed:
        raise Refused("the vehicle is ARMED. Disarm it first.")


def query(description: str, seconds: int) -> str:
    """The query string, as the DVL's own form builds it."""
    params = [("desc", description)]
    if seconds != DEFAULT_DURATION_S:
        params.append(("t", str(seconds)))
    return urllib.parse.urlencode(params)


def _extension(content_type: str, filename: str, head: bytes) -> str:
    if filename and "." in filename:
        return "." + filename.split(".", 1)[1][:16]
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head.startswith(b"\x1f\x8b"):
        return ".tar.gz" if "tar" in content_type else ".gz"
    if len(head) > 262 and head[257:262] == b"ustar":
        return ".tar"
    if "json" in content_type:
        return ".json"
    if content_type.startswith("text/"):
        return ".txt"
    return ".bin"


def _disposition_name(value: str) -> str:
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value or "", re.I)
    if not m:
        return ""
    name = urllib.parse.unquote(m.group(1)).strip()
    # Never a path: only the name, and nothing that could climb out.
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:120]


def collect(address: webapi.Address, *, seconds: int, description: str,
            folder: Path, armed_state: Callable[[], tuple[bool | None, bool]],
            cancel: threading.Event | None = None,
            progress: Callable[[float, str], None] | None = None) -> Result:
    """Ask the DVL for a diagnostic log and save it into `folder`.

    Checks the arm state first and raises `Refused` before anything is sent.
    Everything after that is recorded in the returned `Result` rather than
    raised; the file lands as ``.part`` and is renamed only once complete.
    """
    description = (description or "").strip()
    if not description:
        raise Refused("Water Linked's form needs a description of what is "
                      "happening; type one first.")
    if seconds not in DURATIONS_S:
        raise Refused(f"{seconds} s is not one of Water Linked's durations "
                      f"({', '.join(str(d) for d in DURATIONS_S)} s)")
    require_disarmed(armed_state)

    folder = Path(folder)
    os.makedirs(os_path(folder), exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    res = Result(seconds_requested=seconds, description=description,
                 started_utc=webapi.utc(time.time()))
    t0 = time.monotonic()
    part = folder / f"dvl_diagnostic_{stamp}.part"
    cls = (http.client.HTTPSConnection if address.secure
           else http.client.HTTPConnection)
    conn = cls(address.host, address.port, timeout=seconds + PACKAGING_ALLOWANCE_S)
    digest = hashlib.sha256()
    head = b""
    try:
        if progress:
            progress(0.0, f"Asking the DVL for a {seconds} s diagnostic log…")
        conn.request("GET", f"{COLLECT_PATH}?{query(description, seconds)}",
                     headers={"User-Agent": "rov_flight_ops DVL capture"})
        resp = conn.getresponse()
        res.status = resp.status
        res.content_type = resp.getheader("Content-Type", "") or ""
        res.headers = {k.lower(): v for k, v in resp.getheaders()}
        res.filename_from_dvl = _disposition_name(
            resp.getheader("Content-Disposition", ""))
        if resp.status != 200:
            body = resp.read(4000)
            res.error = (f"the DVL answered HTTP {resp.status}: "
                         f"{body.decode('utf-8', 'replace').strip()[:200]}")
            return res
        with open(os_path(part), "xb") as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    res.error = "stopped before the file had arrived"
                    break
                chunk = resp.read(65536)
                if not chunk:
                    break
                if len(head) < 512:
                    head += chunk[:512 - len(head)]
                fh.write(chunk)
                digest.update(chunk)
                res.bytes += len(chunk)
                if progress:
                    progress(0.5, f"{res.bytes / 1e6:.1f} MB received…")
        if res.error:
            _remove(part)
            return res
        final = part.with_name(f"dvl_diagnostic_{stamp}"
                               f"{_extension(res.content_type, res.filename_from_dvl, head)}")
        os.replace(os_path(part), os_path(final))
        res.path = str(final)
        res.sha256 = digest.hexdigest()
    except Exception as ex:
        res.error = f"{type(ex).__name__}: {ex}"
        _remove(part)
    finally:
        conn.close()
        res.elapsed_s = round(time.monotonic() - t0, 1)
        res.finished_utc = webapi.utc(time.time())
        sidecar = folder / f"dvl_diagnostic_{stamp}.json"
        try:
            with open(os_path(sidecar), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({**asdict(res), "address": str(address),
                                     "request": f"GET {COLLECT_PATH}?"
                                                f"{query(description, seconds)}"},
                                    indent=2, sort_keys=True))
            res.sidecar = str(sidecar)
        except Exception:
            pass
    return res


def _remove(path: Path) -> None:
    try:
        os.remove(os_path(path))
    except OSError:
        pass
