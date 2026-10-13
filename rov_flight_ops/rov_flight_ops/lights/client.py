"""
Reading the Light Monitor extension, and keeping a copy of its recording.

Two loops on one thread:

* **state**, twice a second: `/api/v1/state`, the whole picture as one JSON
  document -- the same document the extension's own page draws. Kept as the
  latest snapshot for the Lights tab and the Navigation row.
* **records**, every two seconds while a flight folder is chosen: everything
  the extension has recorded since the last id seen, appended to
  ``<flight>/logs/lights/lights_topside_<start>.jsonl``. That is a byte-for-
  byte copy of the lines in the extension's recording on the Pi, so a flight
  whose Pi files are never downloaded still has them -- the same insurance
  the DVL capture gives. Records missed across a tether drop are fetched when
  it comes back, as long as the extension still holds them (its last 20,000
  records, roughly half an hour); older ones are only on the Pi, and the gap
  is written into the copy as a ``"x": "gap"`` record so it cannot pass for
  continuity.

**Read-only towards the vehicle.** GETs only. Releasing the controller's port
for mpremote is the extension page's job, not this program's.

Nothing here touches Tk: the window reads `snapshot()` on its own thread.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: Fixed in the extension's Dockerfile (LIGHT_MONITOR_PORT); host networking.
DEFAULT_PORT = 9420
STATE_EVERY_S = 0.5
RECORDS_EVERY_S = 2.0
TIMEOUT_S = 2.5
_UA = "rov_flight_ops Lights (Seattle Aquarium CCR)"
_LIMIT = 4_000_000


def lights_folder(flight_dir: Path | None) -> Path | None:
    return None if flight_dir is None else Path(flight_dir) / "logs" / "lights"


@dataclass
class Status:
    url: str = ""
    connected: bool = False
    error: str = ""
    last_ok: float | None = None            # time.monotonic()
    polls: int = 0
    failures: int = 0
    copy_path: str = ""
    copied: int = 0
    gaps: int = 0
    notes: list[str] = field(default_factory=list)

    def age(self) -> float | None:
        return None if self.last_ok is None else time.monotonic() - self.last_ok


def _get_json(url: str, timeout: float = TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(_LIMIT).decode("utf-8", "replace"))


class LightsClient:
    """The application's one reader of the Light Monitor extension."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, *,
                 fetch=_get_json) -> None:
        self.host = host
        self.port = port
        self._fetch = fetch
        self._lock = threading.Lock()
        self._state: dict | None = None
        self.status = Status(url=self.base)
        self.folder: Path | None = None
        self._copy = None
        self._last_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- addresses and control ---------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def set_host(self, host: str, port: int | None = None) -> None:
        with self._lock:
            if host == self.host and (port is None or port == self.port):
                return
            self.host = host
            self.port = port or self.port
            self.status.url = self.base
            self._state = None
            self._last_id = 0                       # a different extension

    def use_folder(self, folder: Path | None) -> None:
        """Copy the recording into `folder` from now on (None: stop copying)."""
        with self._lock:
            if folder == self.folder:
                return
            self._close_copy()
            self.folder = Path(folder) if folder else None

    def ensure_running(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="lights")
            self._thread.start()

    def shutdown(self, wait: float = 3.0) -> bool:
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(wait)
        with self._lock:
            self._close_copy()
        return th is None or not th.is_alive()

    # -- what the window reads -----------------------------------------------

    def snapshot(self) -> tuple[dict | None, Status]:
        """(the extension's latest state or None, how the connection is)."""
        with self._lock:
            st = Status(**{k: getattr(self.status, k) for k in Status.__dataclass_fields__})
            st.notes = list(self.status.notes)
            return self._state, st

    # -- the loop ----------------------------------------------------------------

    def _run(self) -> None:
        next_records = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.poll_state()
            if self.folder is not None and t0 >= next_records:
                next_records = t0 + RECORDS_EVERY_S
                self.poll_records()
            self._stop.wait(max(0.05, STATE_EVERY_S - (time.monotonic() - t0)))

    def poll_state(self) -> None:
        url = f"{self.base}/api/v1/state"
        try:
            state = self._fetch(url)
            if not isinstance(state, dict) or "lights" not in state:
                raise ValueError("not a Light Monitor state document")
            with self._lock:
                self._state = state
                self.status.connected = True
                self.status.error = ""
                self.status.last_ok = time.monotonic()
                self.status.polls += 1
        except Exception as ex:                                  # noqa: BLE001
            with self._lock:
                self.status.connected = False
                self.status.failures += 1
                self.status.error = _why(ex)

    def poll_records(self) -> None:
        url = f"{self.base}/api/v1/records?after={self._last_id}&limit=2000"
        try:
            doc = self._fetch(url)
        except Exception as ex:                                  # noqa: BLE001
            with self._lock:
                self.status.error = self.status.error or _why(ex)
            return
        records = doc.get("records") or []
        newest = doc.get("newest")
        with self._lock:
            if isinstance(newest, int) and newest < self._last_id:
                # The extension restarted: its ids began again.
                self._note("the extension restarted; its record ids started again")
                self._last_id = 0
                return
            if not records:
                return
            first = records[0].get("id")
            if self._last_id and isinstance(first, int) and first > self._last_id + 1:
                self.status.gaps += 1
                self._write({"x": "gap", "after": self._last_id, "before": first,
                             "rx": time.time(),
                             "why": "records no longer held by the extension; "
                                    "the Pi's recording has them"})
            for r in records:
                self._write(r)
                if isinstance(r.get("id"), int):
                    self._last_id = max(self._last_id, r["id"])
            self.status.copied += len(records)
            if self._copy is not None:
                self._copy.flush()

    # -- the topside copy ----------------------------------------------------

    def _write(self, record: dict) -> None:
        if self.folder is None:
            return
        if self._copy is None:
            self.folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = self.folder / f"lights_topside_{stamp}.jsonl"
            self._copy = open(path, "a", encoding="utf-8", newline="\n")
            self.status.copy_path = str(path)
            self._copy.write(json.dumps({"x": "topside", "rx": time.time(), "from": self.base,
                                         "after": self._last_id}) + "\n")
        self._copy.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _close_copy(self) -> None:
        if self._copy is not None:
            try:
                self._copy.close()
            except Exception:
                pass
        self._copy = None

    def _note(self, text: str) -> None:
        self.status.notes = (self.status.notes + [f"{time.strftime('%H:%M:%S')} {text}"])[-20:]


def _why(ex: Exception) -> str:
    if isinstance(ex, urllib.error.URLError):
        reason = getattr(ex, "reason", ex)
        return f"not answering ({reason})"
    return str(ex) or type(ex).__name__
