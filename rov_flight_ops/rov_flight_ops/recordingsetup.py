"""
What the vehicle will record its telemetry as, and whether it is set up to.

The team flies two BlueOS releases, and they record differently:

* **BlueOS 1.5** records each armed period as an **mcap** (its recorder
  service, under ``/usr/blueos/userdata/recorder``), video and all.
* **BlueOS 1.4 and earlier** record no mcaps. The MAVLink router writes a
  **tlog** -- one per power-up, under ``/ardupilot_logs/logs`` -- *but only
  the MAVLinkRouter router does* (MAVProxy does too, into a folder of its
  own). MAVLink-Server and MAVP2P write none. BlueOS remembers the router
  across a change of release, so a vehicle stepped back from 1.5 -- where the
  router is commonly MAVLink-Server -- to 1.4 keeps flying on MAVLink-Server
  and records nothing at all. That has happened, and nobody noticed until
  the logs were wanted.

From BlueOS 1.4.8 the router also has a **tlog mode** (off, while armed,
always), which must not be off.

This module reads all of it -- the version, the router, the routers on offer,
the tlog mode where there is one, and whether the newest recording of the
expected kind is actually growing -- and says in one line whether the next
flight will be recorded, and what to change if not.

**It can change one thing on the vehicle: the router.** `set_router` POSTs to
BlueOS's own ardupilot-manager (exactly what its *MAVLink Endpoints* page
does), only after a *current* heartbeat confirms the ROV is disarmed --
switching restarts the router, which drops every MAVLink connection for a few
seconds. It is one of the three modules allowed to send the vehicle anything
but a GET (with `pifiles` and `nav/mav2rest`); a test holds the list.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import blueos

#: The routers that write tlogs on BlueOS 1.4.x, best first.
TLOG_ROUTERS = ("MAVLinkRouter", "MAVProxy")
#: The one to switch to.
PREFERRED_TLOG_ROUTER = "MAVLinkRouter"

#: Where each kind of recording lands, as BlueOS's File Browser addresses it.
TLOG_FB_PATH = "/ardupilot_logs/logs"

#: A recording that has not grown for longer than this, by the vehicle's own
#: clock, is not the one being written now.
FRESH_S = 120.0

OK, CHECK, BAD, UNKNOWN = "ok", "check", "bad", "unknown"

_API = "/ardupilot-manager/v1.0"


def version_tuple(version: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) from "1.4.6", "v1.5.0-beta.39" and the like."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def expected_format(version: str) -> str:
    """"mcap" for BlueOS 1.5 and later, "tlog" before it, "" if unknown."""
    v = version_tuple(version)
    if v is None:
        return ""
    return "mcap" if v[:2] >= (1, 5) else "tlog"


@dataclass
class RecordingSetup:
    """What one look at the vehicle found."""

    host: str = ""
    blueos: str = ""
    #: "mcap", "tlog" or "".
    expects: str = ""
    router: str = ""
    routers: list[str] = field(default_factory=list)
    #: BlueOS 1.4.8 and later: "disabled", "while_armed", "always". None when
    #: this BlueOS has no such setting (every tlog-writing router then writes).
    tlog_mode: str | None = None
    #: The newest recording of the expected kind, and how long ago it last
    #: grew by the vehicle's clock (None when that could not be told).
    newest: str = ""
    newest_size: int = 0
    newest_age_s: float | None = None
    level: str = UNKNOWN
    #: One line for the board.
    summary: str = ""
    #: What to do about it, if anything.
    advice: str = ""
    #: The router to switch to, when switching is the fix; else "".
    fix_router: str = ""
    read_at: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def growing(self) -> bool | None:
        if self.newest_age_s is None:
            return None
        return self.newest_age_s <= FRESH_S


def _json(answer):
    if not answer.ok:
        return None
    try:
        return json.loads(answer.body)
    except Exception:
        return None


def read(host: str, *, files: bool = True, timeout: float = 5.0) -> RecordingSetup:
    """Look at the vehicle once. Read-only, and never raises."""
    out = RecordingSetup(host=host, read_at=time.time())
    a = blueos._get(f"http://{host}/version-chooser/v1.0/version/current",
                    timeout=timeout)
    if a.ok:
        out.blueos = blueos._first_string(a.body, ("version", "tag", "name"))
    else:
        out.errors.append(f"version: {a.error}")
    out.expects = expected_format(out.blueos)

    got = _json(blueos._get(f"http://{host}{_API}/preferred_router", timeout=timeout))
    if isinstance(got, str):
        out.router = got
    elif isinstance(got, dict):
        out.router = str(got.get("router") or got.get("name") or "")
    got = _json(blueos._get(f"http://{host}{_API}/available_routers", timeout=timeout))
    if isinstance(got, list):
        out.routers = [str(r.get("name") if isinstance(r, dict) else r) for r in got]
    a = blueos._get(f"http://{host}{_API}/tlog_mode", timeout=timeout)
    if a.ok:
        mode = _json(a)
        out.tlog_mode = str(mode) if mode is not None else None

    if files and out.expects:
        try:
            _newest(out, host)
        except Exception as ex:              # pragma: no cover - best effort
            out.errors.append(f"files: {type(ex).__name__}: {ex}")
    judge(out)
    return out


def _newest(out: RecordingSetup, host: str) -> None:
    """Find the newest recording of the expected kind, and how fresh it is."""
    from . import pifiles

    token = blueos.file_token(host)
    if not token:
        out.errors.append("the File Browser would not open a session")
        return
    if out.expects == "tlog":
        files, _why = pifiles.walk(host, token, TLOG_FB_PATH, depth=3)
        files = [f for f in files if f["name"].lower().endswith(".tlog")]
    else:
        items, _why = pifiles.list_dir(host, token, blueos.RECORDER_FB_PATH)
        files = [f for f in (items or []) if not f["is_dir"]
                 and f["name"].lower().endswith(".mcap")]
    files = [f for f in files if f.get("modified")]
    if not files:
        return
    f = max(files, key=lambda f: f["modified"])
    out.newest, out.newest_size = f["name"], int(f.get("size") or 0)
    skew = blueos.clock_skew(host)
    if skew is not None:
        out.newest_age_s = max(0.0, time.time() + skew - f["modified"])


def judge(out: RecordingSetup) -> None:
    """Fill in level, summary, advice and fix_router from what was read."""
    out.fix_router = ""
    if not out.blueos:
        out.level, out.summary = UNKNOWN, "BlueOS not read yet"
        out.advice = ""
        return
    v = f"BlueOS {out.blueos}"
    fresh = out.growing
    newest = ""
    if out.newest:
        age = out.newest_age_s
        newest = (f"newest {out.newest}"
                  + (", growing" if fresh else
                     f", last grew {age / 60:.0f} min ago" if age is not None else ""))

    if out.expects == "mcap":
        out.summary = f"{v} → records mcaps" + (f" · {newest}" if newest else "")
        out.level = OK if fresh else CHECK if out.newest else UNKNOWN
        out.advice = ("" if fresh else
                      "No mcap is growing yet — normal before arming; after "
                      "arming, check BlueOS's recorder.")
        return

    if out.expects != "tlog":
        out.level, out.summary = UNKNOWN, f"{v}: recording format not known"
        return

    router = out.router or "no router reported"
    writes = out.router in TLOG_ROUTERS
    mode_off = out.tlog_mode == "disabled"
    if not out.router:
        out.level = CHECK
        out.summary = f"{v} → records tlogs · router not reported"
        out.advice = "Check BlueOS → Autopilot Firmware → MAVLink router."
    elif not writes:
        out.level = BAD
        out.summary = f"{v} needs tlogs; router {router} writes none"
        target = (PREFERRED_TLOG_ROUTER if not out.routers
                  or PREFERRED_TLOG_ROUTER in out.routers
                  else next((r for r in TLOG_ROUTERS if r in out.routers), ""))
        out.fix_router = target
        out.advice = (f"Switch the router to {target} (disarmed only) or nothing "
                      f"will be recorded this flight." if target else
                      "No tlog-writing router is installed on this vehicle.")
    elif mode_off:
        out.level = BAD
        out.summary = f"{v} → tlogs, router {router}, but its tlog mode is OFF"
        out.advice = ("Set BlueOS → MAVLink router → tlog mode to “always” "
                      "(or “while armed”).")
    else:
        mode = f", tlog mode {out.tlog_mode}" if out.tlog_mode else ""
        out.level = OK if (fresh or fresh is None) else CHECK
        out.summary = (f"{v} → records tlogs · router {router}{mode}"
                       + (f" · {newest}" if newest else ""))
        if fresh is False:
            out.advice = ("The newest tlog is not growing — restart the router "
                          "or BlueOS, and check again.")


# --------------------------------------------------------------------------
#  the one write: switching the router
# --------------------------------------------------------------------------


def _post(url: str, timeout: float = 20.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="POST", data=b"")
    req.add_header("User-Agent", blueos._UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as ex:
        try:
            detail = ex.read(400).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return False, f"HTTP {ex.code} {detail}".strip()
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:100]}"


def set_router(host: str, router: str, *, settle_s: float = 3.0) -> tuple[bool, str]:
    """Switch BlueOS's MAVLink router, with the ROV confirmed disarmed.

    Returns (done, what happened). Refused -- nothing sent -- unless a current
    heartbeat says the vehicle is disarmed, and unless the router is one this
    vehicle offers. Read back afterwards: "done" means BlueOS now reports the
    new router, not just that the request was accepted.
    """
    if router not in TLOG_ROUTERS:
        return False, f"{router} is not a router this program switches to"
    offered = _json(blueos._get(f"http://{host}{_API}/available_routers", timeout=6))
    if isinstance(offered, list):
        names = [str(r.get("name") if isinstance(r, dict) else r) for r in offered]
        if names and router not in names:
            return False, f"{router} is not available on this vehicle ({', '.join(names)})"
    disarmed, why = blueos.confirm_disarmed(host)
    if not disarmed:
        return False, f"Not changed: {why}. The router can only be switched disarmed."
    ok, detail = _post(f"http://{host}{_API}/preferred_router?router="
                       + urllib.parse.quote(router))
    if not ok:
        return False, f"BlueOS refused the change: {detail}"
    deadline = time.monotonic() + max(settle_s, 0.0) + 10.0
    now_router = ""
    while time.monotonic() < deadline:
        time.sleep(1.0)
        got = _json(blueos._get(f"http://{host}{_API}/preferred_router", timeout=5))
        now_router = got if isinstance(got, str) else ""
        if now_router == router:
            return True, (f"The router is now {router}. MAVLink connections drop "
                          f"for a few seconds while it restarts; a new tlog starts "
                          f"under {TLOG_FB_PATH}.")
    return False, (f"BlueOS accepted the change but still reports "
                   f"{now_router or 'no router'}; check its MAVLink page.")
