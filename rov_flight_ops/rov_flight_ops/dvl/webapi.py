"""
The DVL's web API, read with GETs on one kept-alive connection.

The web GUI is built on an HTTP API that is not in Water Linked's protocol
documentation but is what their own pages call, and it has things the TCP
stream does not: the DVL's temperature and CPU load, its warnings, how many
clients are connected to each of its outputs, its clock and NTP state, and
the two acoustic views on its diagnostics page -- each beam's echo strength
against range, and each beam's spectral density. The paths and shapes below
were read off the GUI's own JavaScript (2.7.1) and checked against the public
demo DVL on 1 October 2026.

Every answer is kept exactly: the body as the text it was when it decodes as
UTF-8, and as base64 when it does not, so nothing is lost to a decoder.

**GETs only.** The API also changes things -- configuration, network,
reboots, factory reset -- and none of those paths appears in this program.
"""

from __future__ import annotations

import base64
import http.client
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import protocol

#: Each read and what it answers, for the record and the schema. The rates
#: are this program's choices, not the DVL's.
STATUS_PATHS = (
    "/api/v1/about/status",      # temperature (°C), cpu_load, disk_free (GB)
    "/api/v1/warnings/",         # active warnings, e.g. temperature_high
    "/api/v1/outputs/",          # each output's port, format and client count
    "/api/v1/config",            # speed of sound, mounting offset, range mode, …
    "/api/v1/time",              # the DVL's clock and its NTP state
)
SLOW_PATHS = (
    "/api/v1/about",             # chip id, product, variant, software version
    "/api/v1/ip",                # configured address
    "/api/v1/ip/current",        # address in use
)
ECHO_PATH = "/api/graph"         # echo strength per beam against range
SPECTRUM_PATH = "/api/spectrum"  # spectral density per beam against frequency

#: Bodies larger than this are cut. The echo profile, the largest thing the
#: DVL serves, is about 18 KB.
BODY_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class Address:
    """Where the DVL is: a host, and how to reach its web side.

    Typed as `192.168.2.95`, `192.168.2.95:8080` (a web port), or a URL --
    `https://dvl.demo.waterlinked.com` reaches Water Linked's public demo,
    which serves its web side over TLS and its TCP stream in the clear.
    """

    host: str
    secure: bool = False
    web_port: int | None = None

    @property
    def port(self) -> int:
        return self.web_port or (443 if self.secure else protocol.WEB_PORT)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.secure else "http"
        default = 443 if self.secure else 80
        tail = "" if self.port == default else f":{self.port}"
        return f"{scheme}://{self.host}{tail}"

    def __str__(self) -> str:
        return self.base_url if (self.secure or self.web_port) else self.host

    @classmethod
    def parse(cls, text: str) -> Address | None:
        text = (text or "").strip()
        if not text:
            return None
        secure = False
        m = re.match(r"^(https?)://", text, re.I)
        if m:
            secure = m.group(1).lower() == "https"
            text = text[m.end():]
        text = text.split("/", 1)[0]
        port = None
        if text.count(":") == 1:
            host, _, p = text.partition(":")
            try:
                port = int(p)
            except ValueError:
                return None
            text = host
        if not text or any(c.isspace() for c in text):
            return None
        return cls(text, secure, port)


@dataclass
class Reply:
    """One GET, whatever happened. Never raises."""

    path: str
    t0_unix: float
    t0_mono_ns: int
    t1_unix: float = 0.0
    t1_mono_ns: int = 0
    status: int | None = None
    body: bytes = b""
    content_type: str = ""
    headers: dict = field(default_factory=dict)
    error: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def elapsed_ms(self) -> float:
        return (self.t1_mono_ns - self.t0_mono_ns) / 1e6

    def text(self) -> str | None:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def json(self):
        import json
        try:
            return json.loads(self.body)
        except Exception:
            return None

    def record(self, seq: int, source: str) -> dict:
        """The reply as one line of a capture's JSON-lines file, losslessly."""
        out = {
            "seq": seq,
            "source": source,
            "path": self.path,
            "t0_utc": utc(self.t0_unix),
            "t0_unix": round(self.t0_unix, 6),
            "t1_unix": round(self.t1_unix, 6),
            "t0_mono_ns": self.t0_mono_ns,
            "t1_mono_ns": self.t1_mono_ns,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "status": self.status,
            "error": self.error,
            "content_type": self.content_type,
            "bytes": len(self.body),
            "truncated": self.truncated,
        }
        text = self.text()
        if text is not None:
            out["body"] = text
        else:
            out["body_b64"] = base64.b64encode(self.body).decode("ascii")
        return out


def utc(unix: float) -> str:
    """ISO 8601 UTC to the microsecond -- the form every capture file uses."""
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(
        timespec="microseconds")


_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(Z|[+-]\d{2}:?\d{2})?$")


def parse_iso_ns(text: str) -> int | None:
    """RFC 3339 to integer nanoseconds since the epoch, keeping all nine digits.

    The DVL writes its clock as `2026-10-02T00:21:26.872988942Z`, and
    `datetime` stops at microseconds.
    """
    m = _ISO.match((text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        base = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                        tzinfo=timezone.utc)
    except ValueError:
        return None
    ns = int(base.timestamp()) * 1_000_000_000
    if frac:
        ns += int(frac.ljust(9, "0")[:9])
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset_s = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        ns -= sign * offset_s * 1_000_000_000
    return ns


class Session:
    """GETs against one web server, reusing one connection while it lasts.

    The acoustic views are read up to ten times a second; opening a new TCP
    connection for each would cost the DVL more than the reads do. One
    thread owns a session.
    """

    def __init__(self, address: Address, *, timeout: float = 3.0,
                 user_agent: str = "rov_flight_ops DVL capture") -> None:
        self.address = address
        self.timeout = timeout
        self.user_agent = user_agent
        self._conn: http.client.HTTPConnection | None = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = (http.client.HTTPSConnection if self.address.secure
                   else http.client.HTTPConnection)
            self._conn = cls(self.address.host, self.address.port,
                             timeout=self.timeout)
        return self._conn

    def get(self, path: str, *, limit: int = BODY_LIMIT) -> Reply:
        reply = Reply(path, time.time(), time.monotonic_ns())
        for attempt in (1, 2):
            try:
                conn = self._connection()
                conn.request("GET", path, headers={"User-Agent": self.user_agent,
                                                   "Accept": "application/json, */*"})
                resp = conn.getresponse()
                reply.status = resp.status
                reply.content_type = resp.getheader("Content-Type", "") or ""
                reply.headers = {k.lower(): v for k, v in resp.getheaders()}
                body = resp.read(limit + 1)
                if len(body) > limit:
                    reply.truncated = True
                    body = body[:limit]
                    # The rest is unread; this connection cannot be reused.
                    self.close()
                reply.body = body
                if resp.status != 200:
                    reply.error = f"HTTP {resp.status}"
                if resp.will_close:
                    self.close()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError,
                    ConnectionResetError, http.client.CannotSendRequest,
                    http.client.ResponseNotReady) as ex:
                # A kept-alive connection the server has since closed fails
                # on its first use; one fresh attempt is not a retry storm.
                self.close()
                if attempt == 2:
                    reply.error = f"{type(ex).__name__}: {ex}"
            except TimeoutError as ex:
                self.close()
                reply.error = f"timed out after {self.timeout:g} s ({ex or 'no answer'})"
                break
            except Exception as ex:
                self.close()
                reply.error = f"{type(ex).__name__}: {ex}"
                break
        reply.t1_unix, reply.t1_mono_ns = time.time(), time.monotonic_ns()
        return reply

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
