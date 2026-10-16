"""
The UGPS capture: the G2 topside's acoustic solution, receiver by receiver.

The G2 topside sits on the laptop's side of the tether, reached through the
laptop's own network port, so this reads it directly: no Pi, no tether, and
nothing to lose when the Pi drops off the network. That matters, because the
Pi doing exactly that is what grounded the 15 October 2026 flight.

It polls always, and writes while a flight folder is chosen, into
``<flight folder>/logs/ugps/`` with ``<stem>`` = ``ugps_<capture id>``:

=============================  ============================================
``<stem>_acoustic.csv``        the acoustic position, filtered and raw, with
                               each receiver's distance, signal (RSSI),
                               noise (NSD), validity and signal-to-noise
``<stem>_position.csv``        the locator's and the topside's lat/lon
``<stem>_status.csv``          the G2's status groups, battery and
                               temperatures, one row per poll
``<stem>_http.jsonl``          every read of the G2's API, verbatim
``<stem>_events.txt``          what happened, in words, in order
``<stem>.json``                the record, with a per-receiver summary
=============================  ============================================

The schema of each is in ``specifications/ugps_log_schema.md``.

**One thread** does every read and owns every file: the reads all go to one
small web server, a few a second, so there is nothing to gain from more.

**Nothing here can stop a flight being recorded.** Every failure is caught,
counted and written down rather than raised.

**It never changes the G2.** GETs only, on the paths in `g2`.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ..dvl import webapi
from ..dvl.capture import _commit, os_path
from ..dvl.protocol import cell
from . import g2

log = logging.getLogger(__name__)

CAPTURE_SCHEMA = "ccr.ugps_capture/1"

#: The G2 computes a new acoustic solution about twice a second, so reading at
#: four catches each one; the repeats are marked `fresh = 0`.
ACOUSTIC_PERIOD_S = 0.25
POSITION_PERIOD_S = 1.0
STATUS_PERIOD_S = 5.0
SLOW_PERIOD_S = 60.0
RECORD_EVERY_S = 60.0
#: After the G2 stops answering, it is asked again this often.
RETRY_S = 3.0
TIMEOUT_S = 1.5
FLUSH_EVERY_S = 1.0
#: Fresh filtered solutions kept for the window: about five minutes at the
#: G2's two a second.
HISTORY_LEN = 600
SYNC_EVERY_S = 10.0

IDENT = ("seq", "rx_utc", "rx_unix", "rx_mono_ns", "flight_id")
ACOUSTIC_COLUMNS = (*IDENT, "source", "poll_ms", "fresh", *g2.ACOUSTIC_COLUMNS)
POSITION_COLUMNS = (*IDENT, "source", "poll_ms", "fresh", *g2.POSITION_COLUMNS)
STATUS_COLUMNS = ("rx_utc", "rx_unix", "flight_id", "g2_address", "reachable",
                  "poll_ms", *g2.STATUS_COLUMNS, "errors")

FILES = {
    "acoustic": ("_acoustic.csv", "acoustic position, and each receiver's "
                                  "distance, signal, noise and validity"),
    "position": ("_position.csv", "the locator's and the topside's latitude "
                                  "and longitude"),
    "status": ("_status.csv", "the G2's status, one row per poll"),
    "http_raw": ("_http.jsonl", "every read of the G2's API, verbatim"),
    "events": ("_events.txt", "what happened, in order"),
}


def ugps_folder(flight_dir: Path | None) -> Path | None:
    """Where a flight's UGPS captures go."""
    return Path(flight_dir) / "logs" / "ugps" if flight_dir else None


def unique_capture_id(folder: Path | None, when: float | None = None) -> str:
    """`2026-10-15_103917`, local time, suffixed until nothing in `folder` uses it."""
    base = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d_%H%M%S")
    candidate, n = base, 1
    while folder is not None and any(Path(os_path(folder)).glob(f"ugps_{candidate}[._]*")):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


class _Sink:
    """One output file and its counts. Belongs to the polling thread."""

    def __init__(self, path: Path, key: str, header, problem) -> None:
        self.path = path
        self.key = key
        self.records = 0
        self.bytes = 0
        self.failed = 0
        self._problem = problem
        self.fh = None
        self.writer = None
        self._flushed = self._synced = time.monotonic()
        try:
            # "x": a capture never replaces an earlier one's file.
            self.fh = open(os_path(path), "x", newline="", encoding="utf-8")
            if header is not None:
                self.writer = csv.writer(self.fh)
                self.writer.writerow(header)
        except Exception as ex:
            self.fh = None
            problem(f"could not create {path.name}: {ex}")

    def row(self, values) -> None:
        if self.writer is None:
            return
        try:
            self.writer.writerow([cell(v) for v in values])
            self.records += 1
        except Exception as ex:
            self._fail(ex)

    def text(self, line: str) -> None:
        if self.fh is None:
            return
        try:
            self.fh.write(line)
            self.bytes += len(line)
            self.records += 1
        except Exception as ex:
            self._fail(ex)

    def _fail(self, ex: Exception) -> None:
        self.failed += 1
        self._problem(f"RECORDING FAILED for {self.path.name}: {type(ex).__name__}: "
                      f"{str(ex)[:80]} ({self.failed:,} write(s) lost so far)")

    def tick(self, force: bool = False) -> None:
        if self.fh is None:
            return
        now = time.monotonic()
        try:
            if force or now - self._flushed >= FLUSH_EVERY_S:
                self.fh.flush()
                self._flushed = now
            if force or now - self._synced >= SYNC_EVERY_S:
                os.fsync(self.fh.fileno())
                self._synced = now
        except Exception as ex:
            self._fail(ex)

    def close(self) -> None:
        fh, self.fh, self.writer = self.fh, None, None
        if fh is None:
            return
        try:
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
            fh.close()
        except Exception as ex:
            self._problem(f"{self.path.name} did not close cleanly: {ex}")

    def describe(self) -> dict:
        return {"file": self.path.name, "holds": FILES[self.key][1],
                "records": self.records, "write_failures": self.failed}


class _ReceiverStats:
    """Running summary of one receiver over fresh filtered solutions."""

    def __init__(self) -> None:
        self.fresh = 0
        self.valid = 0
        self.snr_sum = 0.0
        self.snr_min: float | None = None
        self.nsd_sum = 0.0
        self.nsd_n = 0
        self.nsd_max: float | None = None
        self.rssi_sum = 0.0

    def add(self, valid, rssi, nsd, snr) -> None:
        self.fresh += 1
        if nsd is not None:
            self.nsd_sum += nsd
            self.nsd_n += 1
            self.nsd_max = nsd if self.nsd_max is None else max(self.nsd_max, nsd)
        if valid and snr is not None:
            self.valid += 1
            self.snr_sum += snr
            self.rssi_sum += rssi
            self.snr_min = snr if self.snr_min is None else min(self.snr_min, snr)

    def describe(self) -> dict:
        r = lambda v: None if v is None else round(v, 2)
        return {
            "fresh_solutions": self.fresh,
            "valid_fraction": r(self.valid / self.fresh) if self.fresh else None,
            "snr_db_mean_when_valid": r(self.snr_sum / self.valid) if self.valid else None,
            "snr_db_min_when_valid": r(self.snr_min),
            "rssi_dbm_mean_when_valid": r(self.rssi_sum / self.valid) if self.valid else None,
            "nsd_db_mean": r(self.nsd_sum / self.nsd_n) if self.nsd_n else None,
            "nsd_db_max": r(self.nsd_max),
        }


class UgpsCapture:
    """The application's one reader of the G2. Polls always; writes while a
    flight folder is set."""

    def __init__(self, host: str = g2.DEFAULT_HOST, *, flight_id=None,
                 timeout: float = TIMEOUT_S) -> None:
        self.flight_id = flight_id or (lambda: "")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._want_host = (host or g2.DEFAULT_HOST).strip()
        self._want_folder: Path | None = None
        self._folder_changed = False
        # Read by the window, under the lock.
        self._live = {"connected": False, "error": "", "last_ok": None,
                      "acoustic": None, "status": None, "polls": 0, "failures": 0}
        self.problem = ""
        # The polling thread's own state.
        self.host = self._want_host
        self.folder: Path | None = None
        self.capture_id = ""
        self._sinks: dict[str, _Sink] = {}
        self._seq = {"acoustic": 0, "position": 0, "http_raw": 0}
        self._last_row: dict[str, dict] = {}
        self._receivers = [_ReceiverStats() for _ in range(g2.N_RECEIVERS)]
        #: (monotonic s, position_valid, per receiver (valid, snr, nsd, rssi)),
        #: one per fresh filtered solution, for the window's receiver row.
        self._history: deque = deque(maxlen=HISTORY_LEN)
        self._polls: dict[str, dict] = {}
        self._absent: dict[str, float] = {}
        self._about: dict | None = None
        self._config: dict | None = None
        self._config_at_start: dict | None = None
        self._receivers_config = None
        self._groups: dict[str, str] = {}
        self._flights: list[dict] = []
        self._last_flight = ""
        self._started_unix = 0.0
        self._down = False
        self._retry_at = 0.0

    # ------------------------------------------------------------------
    #  control, from any thread
    # ------------------------------------------------------------------

    def ensure_running(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="ugps")
            self._thread.start()

    def use_folder(self, folder: Path | None) -> None:
        """Write into `folder` (a flight's logs/ugps) from now on; None stops writing."""
        with self._lock:
            self._want_folder = Path(folder) if folder else None
            self._folder_changed = True
        self._wake.set()

    def set_host(self, host: str) -> None:
        with self._lock:
            self._want_host = (host or g2.DEFAULT_HOST).strip()
        self._wake.set()

    def shutdown(self, wait: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th is not None:
            th.join(wait)
        return th is None or not th.is_alive()

    def snapshot(self) -> dict:
        """The latest of everything, for a window. A copy."""
        with self._lock:
            out = dict(self._live)
            out["host"] = self.host
            out["folder"] = str(self.folder) if self.folder else ""
            out["capture_id"] = self.capture_id
            out["problem"] = self.problem
            out["receivers"] = [s.describe() for s in self._receivers]
            out["history"] = list(self._history)
            return out

    # ------------------------------------------------------------------
    #  the loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        session: webapi.Session | None = None
        due = {"acoustic": 0.0, "position": 0.0, "status": 0.0, "slow": 0.0,
               "record": 0.0}
        try:
            while not self._stop.is_set():
                with self._lock:
                    host = self._want_host
                    changed = self._folder_changed
                    folder = self._want_folder
                    self._folder_changed = False
                if session is None or host != self.host:
                    if session is not None:
                        session.close()
                        self._event("G2 address", f"{self.host} → {host}")
                    self.host = host
                    addr = webapi.Address.parse(host) or webapi.Address(g2.DEFAULT_HOST)
                    session = webapi.Session(addr, timeout=self.timeout,
                                             user_agent="rov_flight_ops UGPS capture")
                    self._down, self._retry_at = False, 0.0
                    due = dict.fromkeys(due, 0.0)
                if changed:
                    self._switch_folder(folder)
                    due["slow"] = due["record"] = 0.0

                now = time.monotonic()
                if self._down:
                    if now >= self._retry_at:
                        if self._read(session, "status", g2.STATUS_PATHS["status"]) is not None:
                            self._up()
                            due = dict.fromkeys(due, 0.0)
                        else:
                            self._retry_at = now + RETRY_S
                else:
                    self._cycle(session, due, now)
                self._note_flight()
                if self.folder is not None and time.monotonic() >= due["record"]:
                    due["record"] = time.monotonic() + RECORD_EVERY_S
                    self._write_record("recording")
                for s in self._sinks.values():
                    s.tick()
                nxt = (self._retry_at if self._down
                       else min(due["acoustic"], due["position"], due["status"], due["slow"]))
                self._wake.wait(max(0.02, nxt - time.monotonic()))
                self._wake.clear()
        except Exception:
            from .. import diagnostics
            diagnostics.log_exception("the UGPS capture", *sys.exc_info())
            self._set_problem("the UGPS capture stopped on an unexpected error; "
                              "see the diagnostics log")
        finally:
            if session is not None:
                session.close()
            self._close_files()

    def _cycle(self, session, due: dict, now: float) -> None:
        """One pass of whatever is due. Stops at the first sign the G2 has gone."""
        if now >= due["slow"]:
            due["slow"] = now + SLOW_PERIOD_S
            if not self._slow(session):
                return
        if now >= due["status"]:
            due["status"] = now + STATUS_PERIOD_S
            if not self._status(session):
                return
        if now >= due["position"]:
            due["position"] = now + POSITION_PERIOD_S
            for source, path in g2.POSITION_PATHS.items():
                if not self._poll_row(session, "position", source, path, g2.parse_position):
                    return
        if now >= due["acoustic"]:
            # Kept on its cadence rather than drifting by each read's length.
            due["acoustic"] = max(due["acoustic"] + ACOUSTIC_PERIOD_S, now)
            for source, path in g2.ACOUSTIC_PATHS.items():
                if not self._poll_row(session, "acoustic", source, path, g2.parse_acoustic):
                    return

    # ------------------------------------------------------------------
    #  reads
    # ------------------------------------------------------------------

    def _read(self, session, kind: str, path: str):
        """GET `path`, write it verbatim, count it. The reply, or None if the G2
        did not answer at all (which marks it down)."""
        if path in self._absent and time.monotonic() < self._absent[path]:
            return False
        reply = session.get(path)
        self._seq["http_raw"] += 1
        sink = self._sinks.get("http_raw")
        if sink is not None:
            sink.text(json.dumps(reply.record(self._seq["http_raw"], kind),
                                 ensure_ascii=False, separators=(",", ":")) + "\n")
        c = self._polls.setdefault(path, {"ok": 0, "http_errors": 0, "no_answer": 0})
        if reply.status is None:
            c["no_answer"] += 1
            self._down_because(reply.error or "no answer")
            return None
        with self._lock:
            self._live["polls"] += 1
        if reply.status == 404:
            # A path this firmware does not serve; asked again now and then.
            if path not in self._absent:
                self._event("not served", f"{path} answers 404; asking again every "
                                          f"{SLOW_PERIOD_S:g} s")
            self._absent[path] = time.monotonic() + SLOW_PERIOD_S
        else:
            self._absent.pop(path, None)
        if not reply.ok:
            c["http_errors"] += 1
            return reply
        c["ok"] += 1
        with self._lock:
            self._live["connected"] = True
            self._live["error"] = ""
            self._live["last_ok"] = time.monotonic()
        return reply

    def _poll_row(self, session, key: str, source: str, path: str, parse) -> bool:
        reply = self._read(session, key, path)
        if reply is None:
            return False
        if reply is False or not reply.ok:
            return True
        obj = reply.json()
        if obj is None:
            return True
        row = parse(obj)
        tag = f"{key}:{source}"
        fresh = row != self._last_row.get(tag)
        self._last_row[tag] = row
        if key == "acoustic" and source == "filtered" and fresh:
            for i, st in enumerate(self._receivers):
                st.add(row.get(f"r{i}_valid"), row.get(f"r{i}_rssi"),
                       row.get(f"r{i}_nsd"), row.get(f"r{i}_snr_db"))
            sample = (time.monotonic(), row.get("position_valid"),
                      tuple((row.get(f"r{i}_valid"), row.get(f"r{i}_snr_db"),
                             row.get(f"r{i}_nsd"), row.get(f"r{i}_rssi"))
                            for i in range(g2.N_RECEIVERS)))
            with self._lock:
                self._live["acoustic"] = dict(row, rx_unix=reply.t1_unix)
                self._history.append(sample)
        self._seq[key] += 1
        sink = self._sinks.get(key)
        if sink is not None:
            cols = g2.ACOUSTIC_COLUMNS if key == "acoustic" else g2.POSITION_COLUMNS
            sink.row([self._seq[key], webapi.utc(reply.t1_unix), round(reply.t1_unix, 6),
                      reply.t1_mono_ns, self.flight_id(), source,
                      round(reply.elapsed_ms, 3), fresh,
                      *(row.get(c) for c in cols)])
        return True

    def _status(self, session) -> bool:
        replies, errors, began = {}, [], time.monotonic()
        for name, path in g2.STATUS_PATHS.items():
            r = self._read(session, "status", path)
            if r is None:
                return False
            if r is False:
                continue
            if r.ok:
                replies[name] = r.json()
            else:
                errors.append(f"{path}: {r.error}")
        row = g2.parse_status(replies.get("status"), replies.get("temperature"),
                              replies.get("status_report"))
        self._group_changes(replies.get("status_report"))
        with self._lock:
            self._live["status"] = row
        sink = self._sinks.get("status")
        if sink is not None:
            now = time.time()
            sink.row([webapi.utc(now), round(now, 6), self.flight_id(), self.host, True,
                      round((time.monotonic() - began) * 1000, 1),
                      *(row.get(c) for c in g2.STATUS_COLUMNS), "; ".join(errors)])
        return True

    def _slow(self, session) -> bool:
        got = {}
        for path in g2.SLOW_PATHS:
            r = self._read(session, "config", path)
            if r is None:
                return False
            if r and r.ok:
                got[path] = r.json()
        about = got.get("/api/v1/about")
        if about is not None and about != self._about:
            self._about = about
            self._event("G2", json.dumps(about, separators=(",", ":"))[:300])
        config = got.get("/api/v1/config/generic")
        if config is not None:
            if self._config is not None and config != self._config:
                diff = {k: (self._config.get(k), config.get(k))
                        for k in set(self._config) | set(config)
                        if self._config.get(k) != config.get(k)}
                self._event("G2 configuration changed",
                            "; ".join(f"{k}: {a!r} → {b!r}" for k, (a, b) in sorted(diff.items())))
            self._config = config
            if self._config_at_start is None:
                self._config_at_start = config
        if "/api/v1/config/receivers/" in got:
            self._receivers_config = got["/api/v1/config/receivers/"]
        return True

    # ------------------------------------------------------------------
    #  state changes
    # ------------------------------------------------------------------

    def _down_because(self, why: str) -> None:
        if not self._down:
            self._event("G2 not answering", f"{self.host}: {why}")
        self._down = True
        self._retry_at = time.monotonic() + RETRY_S
        with self._lock:
            self._live["connected"] = False
            self._live["error"] = why
            self._live["failures"] += 1

    def _up(self) -> None:
        self._down = False
        self._event("G2 answering", self.host)

    def _group_changes(self, report) -> None:
        if not isinstance(report, list):
            return
        for g in report:
            if not isinstance(g, dict):
                continue
            name = str(g.get("status_group") or "?")
            state = str(g.get("status") or "?")
            before = self._groups.get(name)
            if before != state:
                if before is not None or state != "ok":
                    msg = (g.get("message") or "").strip()
                    self._event(f"status {name}", f"{before or 'unknown'} → {state}"
                                + (f" ({msg})" if msg else ""))
                self._groups[name] = state

    def _note_flight(self) -> None:
        try:
            fid = self.flight_id() or ""
        except Exception:
            fid = ""
        if fid != self._last_flight:
            if self._last_flight:
                self._event("flight recording", f"{self._last_flight} closed")
            if fid:
                self._event("flight recording", f"{fid} began")
                self._flights.append({"flight_id": fid,
                                      "seen_utc": webapi.utc(time.time())})
            self._last_flight = fid

    def _set_problem(self, text: str) -> None:
        with self._lock:
            self.problem = text
        log.warning("UGPS capture: %s", text)

    # ------------------------------------------------------------------
    #  files
    # ------------------------------------------------------------------

    def _switch_folder(self, folder: Path | None) -> None:
        if folder == self.folder and self._sinks:
            return
        self._close_files()
        self.folder = folder
        if folder is None:
            return
        try:
            Path(os_path(folder)).mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            self._set_problem(f"could not make {folder}: {ex}")
            self.folder = None
            return
        self.capture_id = unique_capture_id(folder)
        stem = f"ugps_{self.capture_id}"
        headers = {"acoustic": ACOUSTIC_COLUMNS, "position": POSITION_COLUMNS,
                   "status": STATUS_COLUMNS, "http_raw": None, "events": None}
        for key, (suffix, _) in FILES.items():
            self._sinks[key] = _Sink(folder / f"{stem}{suffix}", key, headers[key],
                                     self._set_problem)
        self._receivers = [_ReceiverStats() for _ in range(g2.N_RECEIVERS)]
        self._polls, self._flights = {}, []
        self._config_at_start = self._config
        self._started_unix = time.time()
        ev = self._sinks["events"]
        ev.text(f"UGPS capture {self.capture_id}\ntimes are UTC; see {stem}.json "
                f"for the record\n\n")
        self._event("capture started", f"writing {folder}")
        self._event("G2 address", self.host)
        # What was learned before this folder was chosen, so the log stands alone.
        if self._about is not None:
            self._event("G2", json.dumps(self._about, separators=(",", ":"))[:300])
        if self._down:
            self._event("G2 not answering", self.host)
        if self._last_flight:
            self._event("flight recording", f"{self._last_flight} is being recorded")
            self._flights.append({"flight_id": self._last_flight,
                                  "seen_utc": webapi.utc(time.time())})

    def _close_files(self) -> None:
        if not self._sinks:
            return
        self._event("capture stopped",
                    "; ".join(f"{k}: {s.records:,}" for k, s in self._sinks.items()
                              if k != "events"))
        self._write_record("closed")
        for s in self._sinks.values():
            s.close()
        self._sinks = {}

    def _event(self, what: str, detail: str = "") -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{stamp}  {what:<30} {detail}".rstrip() + "\n"
        sink = self._sinks.get("events")
        if sink is not None:
            sink.text(line)
            sink.tick(force=True)
        log.info("UGPS %s %s", what, detail)

    def _write_record(self, state: str) -> None:
        if self.folder is None or not self.capture_id:
            return
        now = time.time()
        rec = {
            "schema": CAPTURE_SCHEMA,
            "schema_document": "rov_flight_ops/specifications/ugps_log_schema.md",
            "capture_id": self.capture_id,
            "state": state,
            "started_utc": webapi.utc(self._started_unix),
            "ended_utc": webapi.utc(now) if state == "closed" else None,
            "seconds": round(now - self._started_unix, 1),
            "folder": str(self.folder),
            "computer": platform.node(),
            "program": {"name": "rov_flight_ops", "commit": _commit(),
                        "python": platform.python_version(), "platform": platform.platform()},
            "laptop_clock": {"resolution_s": time.get_clock_info("time").resolution},
            "g2_address": self.host,
            "about": self._about,
            "config_at_start": self._config_at_start,
            "config_at_end": self._config,
            "receivers_config": self._receivers_config,
            "periods_s": {"acoustic": ACOUSTIC_PERIOD_S, "position": POSITION_PERIOD_S,
                          "status": STATUS_PERIOD_S, "slow": SLOW_PERIOD_S},
            "receivers": {f"r{i}": s.describe() for i, s in enumerate(self._receivers)},
            "polls": self._polls,
            "files": {k: s.describe() for k, s in self._sinks.items()},
            "flights_seen": self._flights,
            "problem": self.problem,
        }
        path = self.folder / f"ugps_{self.capture_id}.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(os_path(tmp), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1, default=str)
            os.replace(os_path(tmp), os_path(path))
        except Exception as ex:
            self._set_problem(f"could not write {path.name}: {ex}")
