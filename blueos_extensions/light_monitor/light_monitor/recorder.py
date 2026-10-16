"""
The recording on the Pi: every line the controller sent, as it arrived.

Into the data folder (host `/usr/blueos/userdata/light_monitor`, which BlueOS's
File Browser shows under userdata, and which `rov_flight_ops` downloads as
"light logs"):

=================================  =========================================
``lights_<UTC start>.jsonl``       one JSON object per line. Every line from
                                   the controller, verbatim, with the Pi's
                                   clock at arrival::

                                     {"id": 812, "rx": 1760390000.123,
                                      "src": "data", "line": "{...}"}

                                   plus the extension's own records --
                                   ``"x": "session"`` at the start,
                                   ``"x": "event"`` for every event,
                                   ``"x": "vehicle"`` once a second with the
                                   vehicle power read from ArduSub
``lights_<UTC start>_1hz.csv``     the state once a second, one column per
                                   quantity (see `model.csv_columns`), for
                                   reading straight into R
=================================  =========================================

A new pair starts when the extension starts, at UTC midnight, and when the
JSONL passes `ROTATE_BYTES`. Nothing is ever deleted by the extension; old
files are cleared the way the mcaps are, from `rov_flight_ops` or BlueOS.

**Every line is written before it is interpreted** -- the serial thread calls
`line()` first and the model second -- so a parser bug costs an
interpretation, never the record. The JSONL is flushed every line and synced
every few seconds; a power cut loses at most the last few seconds.

The last `KEEP` records are also held in memory with their ids, so a client
(`rov_flight_ops`) can fetch everything after the last id it saw and keep a
copy of the same recording on the topside.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROTATE_BYTES = 200 * 1024 * 1024
SYNC_EVERY_S = 5.0
KEEP = 20000
#: Below this much free space the recording pauses rather than fill the Pi --
#: the same disk the mcaps and the autopilot's logs are written to.
MIN_FREE_BYTES = 1024 * 1024 * 1024


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d_%H%M%S")


class Recorder:

    def __init__(self, folder: Path | str, columns: list[str], *, session: dict | None = None,
                 clock=time.time, min_free_bytes: int = MIN_FREE_BYTES) -> None:
        self.folder = Path(folder)
        self.columns = columns
        self.session = session or {}
        self.clock = clock
        self.min_free_bytes = min_free_bytes
        self._lock = threading.Lock()
        self._jsonl = None
        self._csv = None
        self._csv_writer = None
        self._day = None
        self._last_sync = 0.0
        self.path: Path | None = None
        self.csv_path: Path | None = None
        self.bytes = 0
        self.next_id = 1
        self.ring: deque[dict] = deque(maxlen=KEEP)
        self.paused = ""
        self.error = ""
        self.folder.mkdir(parents=True, exist_ok=True)

    # -- files -------------------------------------------------------------

    def _open(self, now: float) -> None:
        self._close()
        stem = f"lights_{_stamp(now)}"
        self.path = self.folder / f"{stem}.jsonl"
        self.csv_path = self.folder / f"{stem}_1hz.csv"
        self._jsonl = open(self.path, "a", encoding="utf-8", newline="\n")
        new_csv = not self.csv_path.exists()
        self._csv = open(self.csv_path, "a", encoding="utf-8", newline="")
        self._csv_writer = csv.DictWriter(self._csv, fieldnames=self.columns,
                                          extrasaction="ignore")
        if new_csv:
            self._csv_writer.writeheader()
        self._day = datetime.fromtimestamp(now, timezone.utc).date()
        self.bytes = self.path.stat().st_size
        self._write({"x": "session", "rx": now, **self.session}, now)

    def _close(self) -> None:
        for fh in (self._jsonl, self._csv):
            if fh is not None:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass
        self._jsonl = self._csv = self._csv_writer = None

    def _ready(self, now: float) -> bool:
        if self._jsonl is None or self.bytes > ROTATE_BYTES or (
                datetime.fromtimestamp(now, timezone.utc).date() != self._day):
            try:
                free = shutil.disk_usage(self.folder).free
            except OSError:
                free = self.min_free_bytes
            if free < self.min_free_bytes:
                self.paused = (f"paused: only {free / 1e9:.1f} GB free on the Pi "
                               f"(needs {self.min_free_bytes / 1e9:.1f} GB)")
                self._close()
                return False
            self.paused = ""
            self._open(now)
        return True

    def _write(self, obj: dict, now: float) -> dict:
        obj = {"id": self.next_id, **obj}
        self.next_id += 1
        self.ring.append(obj)
        if self._jsonl is not None:
            text = json.dumps(obj, separators=(",", ":")) + "\n"
            self._jsonl.write(text)
            self._jsonl.flush()
            self.bytes += len(text.encode("utf-8"))
            if now - self._last_sync >= SYNC_EVERY_S:
                self._last_sync = now
                try:
                    os.fsync(self._jsonl.fileno())
                    if self._csv is not None:
                        self._csv.flush()
                        os.fsync(self._csv.fileno())
                except OSError:
                    pass
        return obj

    # -- what is recorded ------------------------------------------------

    def line(self, text: str, src: str, rx: float) -> None:
        with self._lock:
            try:
                self._ready(rx)
                self._write({"rx": round(rx, 4), "src": src, "line": text}, rx)
                self.error = ""
            except Exception as ex:                          # noqa: BLE001
                self.error = f"{type(ex).__name__}: {ex}"

    def record(self, kind: str, rx: float | None = None, **fields) -> None:
        rx = self.clock() if rx is None else rx
        with self._lock:
            try:
                self._ready(rx)
                self._write({"x": kind, "rx": round(rx, 4), **fields}, rx)
            except Exception as ex:                          # noqa: BLE001
                self.error = f"{type(ex).__name__}: {ex}"

    def csv_row(self, row: dict) -> None:
        with self._lock:
            try:
                if self._ready(self.clock()) and self._csv_writer is not None:
                    self._csv_writer.writerow(row)
            except Exception as ex:                          # noqa: BLE001
                self.error = f"{type(ex).__name__}: {ex}"

    def after(self, last_id: int, limit: int = 2000) -> tuple[list[dict], int]:
        """Records with id > last_id (oldest first), and the newest id."""
        with self._lock:
            items = [r for r in self.ring if r["id"] > last_id][:limit]
            return items, self.next_id - 1

    def close(self) -> None:
        with self._lock:
            self._close()

    # -- what the page shows -----------------------------------------------

    def status(self) -> dict:
        try:
            free = shutil.disk_usage(self.folder).free
        except OSError:
            free = None
        return {"dir": str(self.folder), "file": self.path.name if self.path else "",
                "csv": self.csv_path.name if self.csv_path else "",
                "bytes": self.bytes, "records": self.next_id - 1,
                "disk_free_gb": round(free / 1e9, 2) if free is not None else None,
                "paused": self.paused, "error": self.error}

    def files(self) -> list[dict]:
        out = []
        for p in sorted(self.folder.glob("lights_*")):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "bytes": st.st_size, "modified": st.st_mtime})
        return out
