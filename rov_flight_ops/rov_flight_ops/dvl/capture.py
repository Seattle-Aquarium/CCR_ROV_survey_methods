"""
One DVL capture: the threads that read the DVL, and the files they write.

A capture runs from the moment a flight folder is chosen until the program
closes or another folder is chosen, whether the ROV is armed or not -- the
DVL streams either way, and the stretches around a flight (the descent, a
surface interval, the deck) are where a fault is often easiest to see. Each
row carries the flight the recorder had open at that moment, so a capture can
still be cut by flight.

Into ``<flight folder>/logs/dvl/``, with ``<stem>`` = ``dvl_<capture id>``:

===============================  ==========================================
``<stem>_tcp.jsonl``             every byte of the TCP JSON stream, as it
                                 arrived, across reconnections
``<stem>_tcp_index.csv``         one row per line of it: where it sits in
                                 the file, when it arrived, what it was
``<stem>_velocity.csv``          the velocity-and-transducer reports
``<stem>_deadreckoning.csv``     the dead-reckoning reports
``<stem>_commands.csv``          the read-only questions asked, and answers
``<stem>_ws.jsonl``              every message of the web GUI's stream
``<stem>_ws_velocity.csv``       its velocity messages
``<stem>_ws_motion.csv``         its other channels: orientation, filter
``<stem>_http.jsonl``            every read of the DVL's web API, verbatim
``<stem>_status.csv``            those reads, one row per poll
``<stem>_echo.jsonl``            echo strength against range, per beam
``<stem>_spectrum.jsonl``        spectral density against frequency, per beam
``<stem>_vehicle.jsonl``         every read of the vehicle side, verbatim:
                                 the BlueOS DVL extension and mavlink2rest
``<stem>_mavlink.csv``           what reached MAVLink from the extension
``<stem>_events.txt``            what happened, in words, in order
``<stem>.json``                  the record: what, when, how much, settings
===============================  ==========================================

The schema of each is in ``specifications/dvl_log_schema.md``.

**Every byte reaches the disk before it is interpreted.** The TCP thread
writes what `recv` returned to the raw file first, then splits it into lines,
then parses them. A parser bug can cost a CSV row; it cannot cost the record.

**Each file belongs to one thread**, which opens it, writes it and closes it
on its way out -- the rule the flight recorder follows, for the same reason:
nothing closes a handle under a call still using it. The events file is the
one shared file, and it is written under a lock.

**The DVL and the vehicle are read on different threads.** The extension and
mavlink2rest live on the Pi; when the Pi is off the tether every read of them
costs a timeout, and those must never hold up the reads of the DVL itself.

**Nothing here can stop a flight being recorded.** The capture has its own
threads and its own files, and every failure in it is caught, counted, shown
and written down rather than raised.

**It never changes the DVL.** It sends the four questions in
`protocol.READ_ONLY_COMMANDS`, one at a time, and issues GETs.
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import platform
import shutil
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import cadence as C
from . import protocol as P
from . import webapi, websocket
from .live import ChartPoint, LiveState

log = logging.getLogger(__name__)

CAPTURE_SCHEMA = "ccr.dvl_capture/1"

#: How often the DVL's web API is asked for its status: temperature,
#: warnings, output clients, configuration and clock. A few hundred bytes each.
STATUS_PERIOD_S = 2.0
#: How often the things that do not change in a dive are read again.
SLOW_PERIOD_S = 60.0
#: How often the extension and mavlink2rest are read. The MAVLink counters
#: are cumulative, so a slower poll loses no message, only resolution in time.
VEHICLE_PERIOD_S = 2.0
#: After the extension fails to answer, it is asked again this much later.
EXTENSION_RETRY_S = 30.0
#: A TCP stream silent this long is reconnected. The DVL reports dead
#: reckoning at 5 Hz even with no bottom lock, so five seconds of nothing is
#: a fault, not a quiet seabed.
TCP_SILENCE_S = 5.0
#: The web stream sends orientation at 10 Hz.
WS_SILENCE_S = 10.0
CONNECT_TIMEOUT_S = 3.0
RECV_TIMEOUT_S = 0.5
RECONNECT_MAX_S = 5.0
#: A handshake that is refused is a DVL without the web stream; asking again
#: every few seconds would only fill the events file.
WS_REFUSED_RETRY_S = 60.0
COMMAND_TIMEOUT_S = 3.0
#: Asked once a connection opens, in this order, then on the cadences below.
INITIAL_COMMANDS = ("get_config", "get_version_info", "get_time_ntp",
                    "get_time_status")
TIME_STATUS_EVERY_S = 30.0
CONFIG_EVERY_S = 60.0
#: Files are flushed this often and synced to the drive this often.
FLUSH_EVERY_S = 1.0
SYNC_EVERY_S = 10.0
MANIFEST_EVERY_S = 60.0
DISK_CHECK_S = 30.0
#: Below this, the acoustic snapshots -- most of the bytes -- stop.
DISK_LOW_BYTES = 2 * 1024 ** 3
#: Below this, nothing more is written, and the tab says so in red.
DISK_CRITICAL_BYTES = 256 * 1024 ** 2
#: The choices offered for acoustic snapshots, per second. 0 is off.
SNAPSHOT_RATES = (0.0, 1.0, 2.0, 5.0, 10.0)
DEFAULT_SNAPSHOT_HZ = 5.0
#: A DVL that reaches this is five degrees from its thermal shutdown.
HOT_C = 50.0

#: The MAVLink messages the stock Water Linked DVL extension sends, which it
#: addresses as system 255, component 0 (its own templates), and the
#: autopilot's own view of the rangefinder.
EXTENSION_MESSAGES = ("VISION_POSITION_DELTA", "GLOBAL_VISION_POSITION_ESTIMATE",
                      "VISION_POSITION_ESTIMATE", "VISION_SPEED_ESTIMATE",
                      "DISTANCE_SENSOR")
AUTOPILOT_MESSAGES = ("RANGEFINDER", "DISTANCE_SENSOR")

#: The columns that say where and when a row came from, first in every CSV.
IDENT = ("line_no", "conn", "rx_utc", "rx_unix", "rx_mono_ns", "flight_id")
WS_IDENT = ("seq", "conn", "rx_utc", "rx_unix", "rx_mono_ns", "flight_id")

INDEX_COLUMNS = ("line_no", "conn", "chunk", "first_chunk", "offset", "length",
                 "terminator", "rx_utc", "rx_unix", "rx_mono_ns",
                 "lines_in_chunk", "kind", "parse_error")
VELOCITY_COLUMNS = (*IDENT,
                    *(c for c in P.VELOCITY_REPORT_COLUMNS if c != "extra_json"),
                    *C.DERIVED_VELOCITY_COLUMNS, "extra_json")
POSITION_COLUMNS = (*IDENT,
                    *(c for c in P.POSITION_REPORT_COLUMNS if c != "extra_json"),
                    *C.DERIVED_POSITION_COLUMNS, "extra_json")
COMMAND_COLUMNS = ("conn", "command", "sent_utc", "sent_unix", "sent_mono_ns",
                   "reply_line_no", "reply_utc", "reply_unix", "rtt_ms",
                   "outcome", "success", "error_message", "result_json")
WS_VELOCITY_COLUMNS = (*WS_IDENT, *P.WS_VELOCITY_COLUMNS)
WS_MOTION_COLUMNS = (*WS_IDENT, *P.WS_MOTION_COLUMNS)
STATUS_COLUMNS = (
    "rx_utc", "rx_unix", "flight_id", "dvl_address", "reachable", "poll_ms",
    "temperature_c", "cpu_load", "disk_free_root_gb", "disk_free_data_gb",
    "n_warnings", "warnings",
    "json_port", "json_format", "json_clients", "pd6_clients", "pd4_clients",
    "serial_format",
    "cfg_speed_of_sound", "cfg_mounting_rotation_offset", "cfg_acoustic_enabled",
    "cfg_dark_mode_enabled", "cfg_range_mode", "cfg_periodic_cycling_enabled",
    "dvl_clock_utc", "dvl_clock_minus_laptop_ms", "clock_uncertainty_ms",
    "ntp_enabled", "ntp_server", "ntp_synchronized",
    "ext_reachable", "ext_status", "ext_enabled", "ext_should_send",
    "ext_rangefinder", "ext_orientation", "ext_hostname", "ext_age_s",
    "errors",
)
MAVLINK_COLUMNS = ("rx_utc", "rx_unix", "flight_id", "system", "component",
                   "message", "counter", "frequency_hz", "fresh",
                   "vehicle_last_update_unix", "error")

#: The files, by key, with what each holds. The manifest lists them so.
FILES = {
    "tcp_raw": ("_tcp.jsonl", "every byte of the TCP JSON stream, verbatim"),
    "tcp_index": ("_tcp_index.csv", "one row per line of the TCP stream"),
    "velocity": ("_velocity.csv", "velocity-and-transducer reports"),
    "deadreckoning": ("_deadreckoning.csv", "dead-reckoning reports"),
    "commands": ("_commands.csv", "read-only commands sent, and their answers"),
    "ws_raw": ("_ws.jsonl", "every web-stream message, verbatim"),
    "ws_velocity": ("_ws_velocity.csv", "web-stream velocity messages"),
    "ws_motion": ("_ws_motion.csv", "web-stream orientation and filter channels"),
    "http_raw": ("_http.jsonl", "every read of the DVL's web API, verbatim"),
    "status": ("_status.csv", "the DVL's web-API status, one row per poll"),
    "echo": ("_echo.jsonl", "echo strength against range, per beam"),
    "spectrum": ("_spectrum.jsonl", "spectral density against frequency, per beam"),
    "vehicle_raw": ("_vehicle.jsonl", "every read of the DVL extension and "
                                      "mavlink2rest, verbatim"),
    "mavlink": ("_mavlink.csv", "DVL messages as counted by mavlink2rest"),
    "events": ("_events.txt", "what happened, in order"),
}


def unique_capture_id(folder: Path | None, when: float | None = None) -> str:
    """`2026-10-01_174512`, local time, suffixed until nothing in `folder` uses it."""
    base = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d_%H%M%S")
    candidate, n = base, 1
    while folder is not None and any(Path(os_path(folder)).glob(f"dvl_{candidate}[._]*")):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _utc(ns: int) -> str:
    return webapi.utc(ns / 1e9)


def os_path(path) -> str:
    """A path the operating system will open however deep it is.

    Windows refuses paths over 260 characters unless long paths are enabled
    on the machine, and a flight folder deep in a synchronised tree plus
    ``logs\\dvl\\dvl_<id>_deadreckoning.csv`` gets close. The extended-length
    prefix lifts the limit for that one call and changes nothing else.
    """
    text = str(path)
    if os.name == "nt" and len(text) >= 240 and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(text)
    return text


_COMMIT: str | None = None


def _commit() -> str:
    """This program's git commit, read once."""
    global _COMMIT
    if _COMMIT is None:
        try:
            from .. import diagnostics
            _COMMIT = diagnostics.git_commit() or ""
        except Exception:
            _COMMIT = ""
    return _COMMIT


class _Sink:
    """One output file and its counts. Belongs to the thread that made it."""

    def __init__(self, capture: DvlCapture, key: str, *, binary: bool = False,
                 header=None) -> None:
        self.capture = capture
        self.key = key
        self.path: Path | None = None
        self.fh = None
        self.writer = None
        self.records = 0
        self.bytes = 0
        self.failed = 0
        self.dropped = 0
        self._flushed = time.monotonic()
        self._synced = time.monotonic()
        folder = capture.folder
        if folder is None:
            return
        self.path = folder / f"{capture.stem}{FILES[key][0]}"
        try:
            # "x": a capture never replaces an earlier one's file.
            self.fh = (open(os_path(self.path), "xb") if binary else
                       open(os_path(self.path), "x", newline="", encoding="utf-8"))
            if not binary:
                self.writer = csv.writer(self.fh)
                if header:
                    self.writer.writerow(header)
        except Exception as ex:
            capture.fail(f"could not create {self.path.name}: {ex}")
            self.fh = None
        capture._register(self)

    def _ok_to_write(self) -> bool:
        if self.fh is None:
            return False
        if self.capture.disk_state == "critical":
            self.dropped += 1
            return False
        return True

    def _failed(self, ex: Exception) -> None:
        self.failed += 1
        self.capture.fail(f"RECORDING FAILED for {self.path.name if self.path else self.key}: "
                          f"{type(ex).__name__}: {str(ex)[:80]} "
                          f"({self.failed:,} write(s) lost so far)")

    def write(self, data: bytes) -> bool:
        if not self._ok_to_write():
            return False
        try:
            self.fh.write(data)
            self.bytes += len(data)
            self.records += 1
            if self.failed:
                self.capture.recovered(self)
            return True
        except Exception as ex:
            self._failed(ex)
            return False

    def row(self, values) -> bool:
        if not self._ok_to_write() or self.writer is None:
            return False
        try:
            self.writer.writerow([P.cell(v) for v in values])
            self.records += 1
            if self.failed:
                self.capture.recovered(self)
            return True
        except Exception as ex:
            self._failed(ex)
            return False

    def json_line(self, obj: dict) -> bool:
        if not self._ok_to_write():
            return False
        try:
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=True) + "\n"
            self.fh.write(text)
            self.bytes += len(text)
            self.records += 1
            if self.failed:
                self.capture.recovered(self)
            return True
        except Exception as ex:
            self._failed(ex)
            return False

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
            self._failed(ex)

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
            self.capture.fail(f"{self.path.name if self.path else self.key} "
                              f"did not close cleanly: {ex}")

    def describe(self) -> dict:
        return {"file": self.path.name if self.path else None,
                "holds": FILES[self.key][1], "records": self.records,
                "bytes_written": self.bytes, "write_failures": self.failed,
                "dropped_disk_full": self.dropped}


class _TcpState:
    """The TCP thread's running state, across its connections."""

    def __init__(self) -> None:
        self.conn = 0
        self.chunk = 0
        self.line_no = 0
        self.inflight: dict | None = None
        self.prev_velocity: dict | None = None
        self.prev_position: dict | None = None


class DvlCapture:
    """One capture session. `start` it, read it, `stop` it -- once.

    `folder` None is a live view: everything is read and shown, nothing is
    written. `vehicle` False leaves the Pi alone -- no extension, no
    mavlink2rest -- which is what the tests and a bench DVL want.
    """

    def __init__(self, *, folder: Path | None, vehicle_host: str = "192.168.2.2",
                 dvl_override: str = "",
                 snapshot_hz: float = DEFAULT_SNAPSHOT_HZ,
                 flight_id: Callable[[], str] | None = None,
                 live: LiveState | None = None,
                 vehicle: bool = True,
                 tcp_port: int = P.TCP_PORT) -> None:
        self.folder = Path(folder) if folder else None
        self.vehicle_host = vehicle_host or "192.168.2.2"
        self.snapshot_hz = float(snapshot_hz)
        self.live = live or LiveState()
        self._flight_id_fn = flight_id or (lambda: "")
        self._vehicle = vehicle
        self.capture_id = ""
        self.stem = ""
        self.started = 0.0
        self.ended = 0.0
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.stuck: list[str] = []
        self.problem = ""
        self.notes: list[str] = []
        self.disk_state = "ok"
        self.disk_free: int | None = None
        #: Where the TCP JSON stream is asked for. The DVL's outputs page is
        #: read as soon as the web API answers, and a different port there wins.
        self.tcp_port = int(tcp_port)

        self._override = (dvl_override or "").strip()
        self.address: webapi.Address | None = webapi.Address.parse(self._override)
        self.address_source = "typed on the DVL tab" if self.address else ""
        #: What the vehicle thread last learned from the extension, for the
        #: status rows: replaced whole, never edited, so a reader on another
        #: thread always sees one consistent reading.
        self.extension: dict = {}
        self._ext_values: dict = {}
        self._ext_at = 0.0

        self._sinks: dict[str, _Sink] = {}
        self._sinks_lock = threading.Lock()
        self._events_fh = None
        self._events_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._tcp = _TcpState()

        # What the record says about the stream. Written by the thread that
        # owns each part and read when the manifest is written.
        self.cadence = C.Cadence()
        self.connections: list[dict] = []
        self.ws_connections: list[dict] = []
        self.command_outcomes: dict[str, dict] = {}
        self.kinds: dict[str, int] = {}
        self.stream_offset = 0
        self.about: dict = {}
        self.config_first: dict = {}
        self.config_last: dict = {}
        self.time_first: dict = {}
        self.snapshot_rates: list[dict] = []
        self.diagnostic_logs: list[dict] = []
        self.flights_seen: list[dict] = []

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        self.started = time.time()
        if self.folder is not None:
            try:
                os.makedirs(os_path(self.folder), exist_ok=True)
            except Exception as ex:
                self.fail(f"NOT RECORDING THE DVL: could not create {self.folder} ({ex})")
                self.folder = None
        self.capture_id = unique_capture_id(self.folder, self.started)
        self.stem = f"dvl_{self.capture_id}"
        if self.folder is not None:
            try:
                self._events_fh = open(
                    os_path(self.folder / f"{self.stem}{FILES['events'][0]}"),
                    "x", encoding="utf-8")
                self._events_fh.write(
                    f"DVL capture {self.capture_id}\n"
                    f"times are UTC; see {self.stem}.json for the record\n\n")
                self._events_fh.flush()
            except Exception as ex:
                self.fail(f"could not create the events file: {ex}")
        self.live.reset_streams()
        self.live.update(capturing=self.folder is not None,
                         folder=str(self.folder) if self.folder else "",
                         capture_id=self.capture_id,
                         address=str(self.address) if self.address else "",
                         address_source=self.address_source,
                         snapshot_hz=self.snapshot_hz, problem="", notes=[],
                         tcp_connected=False, ws_connected=False,
                         tcp_connections=0, tcp_error="", ws_error="",
                         web_error="", files={})
        self.snapshot_rates.append({"from_utc": webapi.utc(self.started),
                                    "hz": self.snapshot_hz})
        self.event("capture started",
                   (f"writing {self.folder}" if self.folder else
                    "live view only — no flight folder, nothing is written"))
        self._check_disk()
        self.write_manifest(final=False)
        targets = [(self._tcp_loop, "dvl-tcp"), (self._ws_loop, "dvl-ws"),
                   (self._status_loop, "dvl-status"),
                   (self._acoustic_loop, "dvl-acoustic")]
        if self._vehicle:
            targets.append((self._vehicle_loop, "dvl-vehicle"))
        for target, name in targets:
            thread = threading.Thread(target=self._guarded, args=(target, name),
                                      daemon=True, name=name)
            self.threads.append(thread)
            thread.start()
        log.info("dvl: capture %s started (%s)", self.capture_id,
                 self.folder or "live view")
        return True

    def stop(self, timeout: float = 8.0) -> bool:
        """Stop every thread and close every file. True if all of them did."""
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.stuck = [t.name for t in self.threads if t.is_alive()]
        self.ended = time.time()
        if self.stuck:
            self.note(f"{', '.join(self.stuck)} had not stopped {timeout:g} s after "
                      f"the capture closed; its files are closed when it does")
        self.event("capture stopped",
                   f"{self.stream_offset:,} TCP bytes, "
                   f"{sum(self.kinds.values()):,} lines, "
                   f"{len(self.connections)} TCP connection(s)")
        self.write_manifest(final=True)
        with self._events_lock:
            fh, self._events_fh = self._events_fh, None
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        self.live.update(tcp_connected=False, ws_connected=False, capturing=False)
        log.info("dvl: capture %s stopped%s", self.capture_id,
                 f"; stuck: {self.stuck}" if self.stuck else "")
        return not self.stuck

    @property
    def running(self) -> bool:
        return bool(self.threads) and not self.stop_event.is_set()

    def _guarded(self, target, name: str) -> None:
        try:
            target()
        except Exception as ex:
            from .. import diagnostics
            diagnostics.log_exception(f"DVL capture thread {name}", *sys.exc_info())
            self.fail(f"the DVL {name} reader stopped: {type(ex).__name__}: {ex}")

    # ------------------------------------------------------------------
    #  settings that change while running
    # ------------------------------------------------------------------

    def set_override(self, text: str) -> None:
        """A typed DVL address; blank goes back to asking the extension."""
        self._override = (text or "").strip()
        addr = webapi.Address.parse(self._override)
        if addr is not None:
            self._set_address(addr, "typed on the DVL tab")
        else:
            # The vehicle thread finds it again from the extension.
            self._set_address(None, "")

    def set_snapshot_hz(self, hz: float) -> None:
        hz = max(0.0, float(hz))
        if hz == self.snapshot_hz:
            return
        self.snapshot_hz = hz
        self.snapshot_rates.append({"from_utc": webapi.utc(time.time()), "hz": hz})
        self.live.update(snapshot_hz=hz)
        self.event("acoustic snapshot rate", f"{hz:g} per second" if hz else "off")

    def set_vehicle_host(self, host: str) -> None:
        host = host or "192.168.2.2"
        if host != self.vehicle_host:
            self.vehicle_host = host
            self.extension = {}
            self.event("vehicle address", host)

    def _set_address(self, addr: webapi.Address | None, source: str) -> None:
        if addr == self.address and source == self.address_source:
            return
        before = self.address
        self.address = addr
        self.address_source = source
        self.live.update(address=str(addr) if addr else "", address_source=source)
        if addr != before:
            self.event("DVL address",
                       f"{addr} ({source})" if addr else "unknown — waiting for one")

    # ------------------------------------------------------------------
    #  the record
    # ------------------------------------------------------------------

    def flight_id(self) -> str:
        try:
            return self._flight_id_fn() or ""
        except Exception:
            return ""

    def event(self, what: str, detail: str = "") -> None:
        now = time.time()
        self.live.add_event(now, f"{what}" + (f" — {detail}" if detail else ""))
        with self._events_lock:
            fh = self._events_fh
            if fh is None:
                return
            try:
                stamp = datetime.fromtimestamp(now, timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S.%f")[:-3]
                fh.write(f"{stamp}  {what:<30} {detail}\n")
                fh.flush()
            except Exception:
                pass

    def fail(self, message: str) -> None:
        """A problem worth showing in red until it clears."""
        if message != self.problem:
            log.warning("dvl: %s", message)
            self.event("PROBLEM", message)
        self.problem = message
        self.live.update(problem=message)

    def recovered(self, sink: _Sink) -> None:
        """A write succeeded after failures; clear the alarm that sink raised."""
        if (self.problem.startswith("RECORDING FAILED") and sink.path is not None
                and sink.path.name in self.problem):
            self.note(f"{sink.path.name}: {sink.failed:,} write(s) were lost "
                      f"earlier in this capture")
            self.problem = ""
            self.live.update(problem="")

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
            self.live.update(notes=list(self.notes))

    def _register(self, sink: _Sink) -> None:
        with self._sinks_lock:
            self._sinks[sink.key] = sink
        if sink.path is not None:
            self.live.merge("files", {sink.key: str(sink.path)})

    def note_diagnostic(self, record: dict) -> None:
        self.diagnostic_logs.append(record)
        self.event("diagnostic log", json.dumps(record, sort_keys=True))
        self.write_manifest(final=False)

    def write_manifest(self, *, final: bool) -> None:
        """The capture's record, replaced whole each time it is written."""
        if self.folder is None:
            return
        with self._manifest_lock:
            text = None
            # The counts it copies are being added to by the stream threads;
            # a dict that grows while it is copied raises, and the next try
            # simply sees the newer count.
            for _attempt in range(5):
                try:
                    text = json.dumps(self._manifest(final), indent=2,
                                      sort_keys=True, default=str)
                    break
                except RuntimeError:
                    time.sleep(0.01)
            if text is None:
                return
            try:
                path = self.folder / f"{self.stem}.json"
                tmp = path.with_name(path.name + ".tmp")
                with open(os_path(tmp), "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(os_path(tmp), os_path(path))
            except Exception as ex:
                self.fail(f"could not write {self.stem}.json: {ex}")

    def _manifest(self, final: bool) -> dict:
        with self._sinks_lock:
            files = {k: s.describe() for k, s in self._sinks.items()}
        if self.folder is not None:
            files["events"] = {"file": f"{self.stem}{FILES['events'][0]}",
                               "holds": FILES["events"][1]}
        clock = time.get_clock_info("time")
        return {
            "schema": CAPTURE_SCHEMA,
            "capture_id": self.capture_id,
            "state": "closed" if final else "recording",
            "started_utc": webapi.utc(self.started) if self.started else None,
            "ended_utc": webapi.utc(self.ended) if final and self.ended else None,
            "seconds": round((self.ended or time.time()) - self.started, 1)
            if self.started else None,
            "folder": str(self.folder),
            "program": {"name": "rov_flight_ops", "commit": _commit(),
                        "python": platform.python_version(),
                        "platform": platform.platform()},
            "laptop_clock": {"implementation": clock.implementation,
                             "resolution_s": clock.resolution,
                             "monotonic": time.get_clock_info("monotonic").implementation,
                             "note": "rx_unix is this laptop's wall clock; "
                                     "rx_mono_ns its monotonic clock"},
            "computer": socket.gethostname(),
            "vehicle_host": self.vehicle_host,
            "vehicle_side_read": self._vehicle,
            "dvl_address": str(self.address) if self.address else None,
            "dvl_address_source": self.address_source,
            "tcp_port": self.tcp_port,
            "extension": dict(self.extension),
            "about": dict(self.about),
            "config_at_start": self.config_first,
            "config_at_end": self.config_last,
            "time_at_start": self.time_first,
            "snapshot_rates": list(self.snapshot_rates),
            "disk_free_bytes": self.disk_free,
            "files": files,
            "lines_by_kind": dict(self.kinds),
            "tcp_bytes": self.stream_offset,
            "tcp_connections": [dict(c) for c in self.connections],
            "ws_connections": [dict(c) for c in self.ws_connections],
            "commands": {k: dict(v) for k, v in self.command_outcomes.items()},
            "cadence": self.cadence.stats.as_dict(),
            "flights_seen": list(self.flights_seen),
            "diagnostic_logs": list(self.diagnostic_logs),
            "problem": self.problem,
            "notes": list(self.notes),
            "stuck_threads": self.stuck,
            "read_only_commands": sorted(P.READ_ONLY_COMMANDS),
            "schema_document": "rov_flight_ops/specifications/dvl_log_schema.md",
        }

    def _check_disk(self) -> None:
        if self.folder is None:
            return
        try:
            free = shutil.disk_usage(self.folder).free
        except Exception:
            return
        self.disk_free = free
        state = ("critical" if free < DISK_CRITICAL_BYTES else
                 "low" if free < DISK_LOW_BYTES else "ok")
        if state == self.disk_state:
            return
        self.disk_state = state
        gib = free / 1024 ** 3
        if state == "critical":
            self.fail(f"DVL CAPTURE STOPPED WRITING: {gib:.2f} GiB free on the "
                      f"flight folder's drive")
            return
        if self.problem.startswith("DVL CAPTURE STOPPED WRITING"):
            self.problem = ""
            self.live.update(problem="")
        if state == "low":
            self.event("disk low", f"{gib:.2f} GiB free — acoustic snapshots paused "
                                   f"until there are {DISK_LOW_BYTES / 1024 ** 3:g}")
            self.note(f"acoustic snapshots were paused for want of disk space "
                      f"({gib:.2f} GiB free)")
        else:
            self.event("disk", f"{gib:.1f} GiB free — writing everything")

    # ------------------------------------------------------------------
    #  the TCP JSON stream
    # ------------------------------------------------------------------

    def _tcp_loop(self) -> None:
        sinks = {
            "raw": _Sink(self, "tcp_raw", binary=True),
            "index": _Sink(self, "tcp_index", header=INDEX_COLUMNS),
            "velocity": _Sink(self, "velocity", header=VELOCITY_COLUMNS),
            "position": _Sink(self, "deadreckoning", header=POSITION_COLUMNS),
            "commands": _Sink(self, "commands", header=COMMAND_COLUMNS),
        }
        backoff, last_error, last_error_at = 1.0, "", 0.0
        try:
            while not self.stop_event.is_set():
                addr = self.address
                if addr is None:
                    self.stop_event.wait(1.0)
                    continue
                port = self.tcp_port
                try:
                    sock = socket.create_connection((addr.host, port),
                                                    timeout=CONNECT_TIMEOUT_S)
                except OSError as ex:
                    err = f"cannot connect to {addr.host}:{port}: {ex}"
                    self.live.update(tcp_connected=False, tcp_error=err)
                    if err != last_error or time.monotonic() - last_error_at > 60:
                        self.event("TCP not connected", err)
                        last_error, last_error_at = err, time.monotonic()
                    # The DVL's outputs page may name another port, or a new
                    # address may be typed: either ends the wait at once.
                    until = time.monotonic() + backoff
                    while (time.monotonic() < until and not self.stop_event.is_set()
                           and self.tcp_port == port and self.address == addr):
                        self.stop_event.wait(0.1)
                    backoff = min(backoff * 2, RECONNECT_MAX_S)
                    continue
                backoff, last_error = 1.0, ""
                self._tcp_connection(sock, addr, port, sinks)
        finally:
            for sink in sinks.values():
                sink.close()

    def _tcp_connection(self, sock: socket.socket, addr, port: int,
                        sinks: dict) -> None:
        st = self._tcp
        st.conn += 1
        conn = st.conn
        record = {"conn": conn, "host": addr.host, "port": port,
                  "opened_utc": webapi.utc(time.time()), "closed_utc": None,
                  "bytes": 0, "lines": 0, "reason": ""}
        self.connections.append(record)
        reason = "the connection failed before any data"
        try:
            sock.settimeout(RECV_TIMEOUT_S)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "SIO_KEEPALIVE_VALS"):
                    sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10_000, 2_000))
            except OSError:
                pass
            self.live.update(tcp_connected=True, tcp_since=time.time(),
                             tcp_error="", tcp_connections=conn, tcp_port=port)
            self.event("TCP connected", f"{addr.host}:{port} (connection {conn})")
            splitter = P.LineSplitter(offset=self.stream_offset)
            pending = deque(INITIAL_COMMANDS)
            st.inflight = None
            next_status = time.monotonic() + TIME_STATUS_EVERY_S
            next_config = time.monotonic() + CONFIG_EVERY_S
            last_rx = time.monotonic()
            reason = "capture stopped"
            while not self.stop_event.is_set():
                if self.address != addr:
                    reason = "the DVL address changed"
                    break
                now = time.monotonic()
                if st.inflight is None:
                    if now >= next_status:
                        pending.append("get_time_status")
                        next_status = now + TIME_STATUS_EVERY_S
                    if now >= next_config:
                        pending.append("get_config")
                        next_config = now + CONFIG_EVERY_S
                    if pending:
                        self._send_command(sock, pending.popleft(), conn, sinks)
                elif now - st.inflight["sent_mono"] > COMMAND_TIMEOUT_S:
                    self._command_row(sinks, st.inflight, None, "no answer")
                    st.inflight = None
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    if time.monotonic() - last_rx > TCP_SILENCE_S:
                        reason = f"nothing received for {TCP_SILENCE_S:g} s"
                        break
                    continue
                except OSError as ex:
                    reason = f"connection error: {ex}"
                    break
                if not data:
                    reason = "the DVL closed the connection"
                    break
                rx_ns = time.time_ns()
                rx_mono = time.monotonic_ns()
                last_rx = time.monotonic()
                st.chunk += 1
                # The bytes first, before anything tries to understand them.
                sinks["raw"].write(data)
                self.stream_offset += len(data)
                record["bytes"] += len(data)
                lines = splitter.feed(data, st.chunk)
                for line in lines:
                    self._tcp_line(line, conn, rx_ns, rx_mono, len(lines), sinks)
                record["lines"] += len(lines)
                self.live.count("tcp_chunk", len(data))
                self.live.lines(len(lines))
                for sink in sinks.values():
                    sink.tick()
            tail = splitter.flush()
            if tail is not None:
                self._tcp_line(tail, conn, time.time_ns(), time.monotonic_ns(), 1,
                               sinks, partial=True)
        finally:
            try:
                sock.close()
            except Exception:
                pass
            if st.inflight is not None:
                self._command_row(sinks, st.inflight, None, "connection closed")
                st.inflight = None
            record["closed_utc"] = webapi.utc(time.time())
            record["reason"] = reason
            for sink in sinks.values():
                sink.tick(force=True)
            self.live.update(tcp_connected=False, tcp_error=reason)
            self.event("TCP disconnected",
                       f"connection {conn}: {reason} "
                       f"({record['bytes']:,} bytes, {record['lines']:,} lines)")

    def _send_command(self, sock, name: str, conn: int, sinks: dict) -> None:
        data = P.command_bytes(name)
        sent = {"asked": name, "conn": conn, "sent_ns": time.time_ns(),
                "sent_mono_ns": time.monotonic_ns(), "sent_mono": time.monotonic()}
        try:
            sock.sendall(data)
        except OSError as ex:
            self._command_row(sinks, sent, None, f"not sent: {ex}")
            return
        self._tcp.inflight = sent

    def _command_row(self, sinks: dict, sent: dict, reply: dict | None,
                     outcome: str) -> None:
        name = sent.get("asked", "")
        sent_ns = sent.get("sent_ns")
        row = [sent.get("conn"), name,
               _utc(sent_ns) if sent_ns else "",
               round(sent_ns / 1e9, 6) if sent_ns else "",
               sent.get("sent_mono_ns", "")]
        if reply is not None:
            obj = reply["obj"]
            rtt = ((reply["rx_mono"] - sent["sent_mono_ns"]) / 1e6
                   if sent.get("sent_mono_ns") else None)
            result = obj.get("result")
            row += [reply["line_no"], _utc(reply["rx_ns"]),
                    round(reply["rx_ns"] / 1e9, 6), rtt, outcome,
                    obj.get("success"), obj.get("error_message", ""),
                    json.dumps(result, separators=(",", ":"), sort_keys=True)
                    if result is not None else ""]
        else:
            row += ["", "", "", "", outcome, "", "", ""]
        sinks["commands"].row(row)
        if name:
            self.command_outcomes[name] = {
                "outcome": outcome,
                "success": reply["obj"].get("success") if reply else None,
                "error_message": reply["obj"].get("error_message", "") if reply else "",
                "at_utc": webapi.utc(time.time())}
            self.live.merge("commands", {name: dict(self.command_outcomes[name])})

    def _tcp_line(self, line: P.Line, conn: int, rx_ns: int, rx_mono: int,
                  in_chunk: int, sinks: dict, partial: bool = False) -> None:
        st = self._tcp
        st.line_no += 1
        line_no = st.line_no
        rx_unix = rx_ns / 1e9
        rx_utc = _utc(rx_ns)
        if partial:
            obj, err, kind = None, "unterminated at the end of the connection", "partial"
        else:
            obj, err = P.parse(line.body)
            kind = (P.kind_of(obj) if obj is not None
                    else "empty" if err == "empty line" else "unparsed")
        sinks["index"].row([line_no, conn, line.chunk, line.first_chunk,
                            line.offset, len(line.data), line.terminator,
                            rx_utc, round(rx_unix, 6), rx_mono, in_chunk, kind, err])
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        self.live.count(kind)
        if obj is None:
            return
        ident = [line_no, conn, rx_utc, round(rx_unix, 6), rx_mono, self.flight_id()]
        try:
            if kind in P.VELOCITY_TYPES:
                self._velocity(obj, ident, rx_unix, rx_mono, sinks)
            elif kind == "position_local":
                self._position(obj, ident, rx_mono, sinks)
            elif kind == "response":
                self._response(obj, line_no, rx_ns, rx_mono, sinks)
        except Exception as ex:
            # One malformed report costs its CSV row. The raw file has it.
            from .. import diagnostics
            diagnostics.log_exception("DVL report", *sys.exc_info(),
                                      level=logging.WARNING)
            self.note(f"a {kind} report on line {line_no} could not be "
                      f"tabulated ({type(ex).__name__}); it is in the raw file")

    def _velocity(self, obj: dict, ident: list, rx_unix: float, rx_mono: int,
                  sinks: dict) -> None:
        st = self._tcp
        row = P.flatten_velocity(obj)
        derived = C.derived_velocity(row, st.prev_velocity, rx_unix, rx_mono)
        sinks["velocity"].row(
            ident + [row[c] for c in P.VELOCITY_REPORT_COLUMNS if c != "extra_json"]
            + [derived[c] for c in C.DERIVED_VELOCITY_COLUMNS] + [row["extra_json"]])
        st.prev_velocity = {**row, "_rx_mono_ns": rx_mono}
        gaps, note = self.cadence.add(
            tov_us=row["time_of_validity_us"], time_ms=row["time_ms"],
            rx_mono_ns=rx_mono, rx_unix=rx_unix, valid=row["velocity_valid"])
        for gap in gaps:
            self.live.add_gap(gap)
            self.event(f"gap: {gap.kind}", gap.text())
        if note:
            self.event("velocity validity", note)
        valid = row["velocity_valid"]
        self.live.count("velocity_valid" if valid else "velocity_invalid")
        self.live.update(velocity={**row, **derived, "rx_unix": rx_unix},
                         cadence=self.cadence.stats.as_dict(),
                         median_ms=self.cadence.median_ms)
        self.live.add_point(ChartPoint(
            t=rx_unix, altitude=row["altitude"],
            distance=tuple(row[f"t{i}_distance"] for i in P.TRANSDUCER_IDS),
            rssi=tuple(row[f"t{i}_rssi"] for i in P.TRANSDUCER_IDS),
            nsd=tuple(row[f"t{i}_nsd"] for i in P.TRANSDUCER_IDS),
            valid=valid, fom=row["fom"], d_tov_ms=derived["d_tov_ms"],
            d_rx_ms=derived["d_rx_ms"]))

    def _position(self, obj: dict, ident: list, rx_mono: int, sinks: dict) -> None:
        st = self._tcp
        row = P.flatten_position(obj)
        derived = C.derived_position(row, st.prev_position, rx_mono)
        sinks["position"].row(
            ident + [row[c] for c in P.POSITION_REPORT_COLUMNS if c != "extra_json"]
            + [derived[c] for c in C.DERIVED_POSITION_COLUMNS] + [row["extra_json"]])
        st.prev_position = {**row, "_rx_mono_ns": rx_mono}
        self.live.update(position={**row, **derived})

    def _response(self, obj: dict, line_no: int, rx_ns: int, rx_mono: int,
                  sinks: dict) -> None:
        st = self._tcp
        reply = {"obj": obj, "line_no": line_no, "rx_ns": rx_ns, "rx_mono": rx_mono}
        to = obj.get("response_to")
        sent = st.inflight
        if sent is not None and (to == sent["asked"] or to == ""):
            self._command_row(sinks, sent, reply, "answered")
            st.inflight = None
            name = sent["asked"]
        else:
            # An answer to nothing this capture asked: another client's
            # question, if the DVL ever broadcasts them. Kept as it came.
            self._command_row(sinks, {"asked": to or "", "conn": st.conn}, reply,
                              "unsolicited")
            name = to or ""
        result = obj.get("result")
        if name == "get_config" and obj.get("success") and isinstance(result, dict):
            self._config_seen(result, "TCP get_config")
        elif name == "get_version_info" and obj.get("success") and isinstance(result, dict):
            self.about = {**self.about, **result}

    # ------------------------------------------------------------------
    #  the web GUI's stream
    # ------------------------------------------------------------------

    def _ws_loop(self) -> None:
        sinks = {
            "raw": _Sink(self, "ws_raw"),
            "velocity": _Sink(self, "ws_velocity", header=WS_VELOCITY_COLUMNS),
            "motion": _Sink(self, "ws_motion", header=WS_MOTION_COLUMNS),
        }
        conn, seq, backoff, last_error = 0, 0, 1.0, ""
        try:
            while not self.stop_event.is_set():
                addr = self.address
                if addr is None:
                    self.stop_event.wait(1.0)
                    continue
                client = websocket.Client(addr.host, port=addr.port, path="/ws",
                                          secure=addr.secure, timeout=CONNECT_TIMEOUT_S)
                try:
                    client.connect()
                except (OSError, websocket.WebSocketError) as ex:
                    err = f"{type(ex).__name__}: {ex}"
                    refused = isinstance(ex, websocket.WebSocketError)
                    self.live.update(ws_connected=False, ws_error=err)
                    if err != last_error:
                        self.event("web stream not connected", err)
                        last_error = err
                    self.stop_event.wait(WS_REFUSED_RETRY_S if refused else backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_S)
                    continue
                conn += 1
                backoff, last_error = 1.0, ""
                record = {"conn": conn, "opened_utc": webapi.utc(time.time()),
                          "closed_utc": None, "messages": 0, "reason": ""}
                self.ws_connections.append(record)
                self.live.update(ws_connected=True, ws_error="")
                self.event("web stream connected",
                           f"{addr.base_url}/ws (connection {conn})")
                client.settimeout(RECV_TIMEOUT_S)
                last = time.monotonic()
                reason = "capture stopped"
                try:
                    while not self.stop_event.is_set():
                        if self.address != addr:
                            reason = "the DVL address changed"
                            break
                        try:
                            opcode, payload = client.receive()
                        except TimeoutError:
                            if time.monotonic() - last > WS_SILENCE_S:
                                reason = f"nothing received for {WS_SILENCE_S:g} s"
                                break
                            continue
                        except (websocket.WebSocketError, OSError) as ex:
                            reason = f"{type(ex).__name__}: {ex}"
                            break
                        rx_ns, rx_mono = time.time_ns(), time.monotonic_ns()
                        last = time.monotonic()
                        seq += 1
                        record["messages"] += 1
                        self._ws_message(opcode, payload, seq, conn, rx_ns,
                                         rx_mono, sinks)
                        for sink in sinks.values():
                            sink.tick()
                finally:
                    client.close()
                    record["closed_utc"] = webapi.utc(time.time())
                    record["reason"] = reason
                    self.live.update(ws_connected=False, ws_error=reason)
                    self.event("web stream disconnected",
                               f"connection {conn}: {reason} "
                               f"({record['messages']:,} messages)")
        finally:
            for sink in sinks.values():
                sink.close()

    def _ws_message(self, opcode: int, payload: bytes, seq: int, conn: int,
                    rx_ns: int, rx_mono: int, sinks: dict) -> None:
        rx_unix = rx_ns / 1e9
        rec = {"seq": seq, "conn": conn, "rx_utc": _utc(rx_ns),
               "rx_unix": round(rx_unix, 6), "rx_mono_ns": rx_mono,
               "opcode": opcode}
        text = None
        if opcode == websocket.OP_TEXT:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is not None:
            rec["text"] = text
        else:
            rec["b64"] = base64.b64encode(payload).decode("ascii")
        sinks["raw"].json_line(rec)
        self.live.count("ws")
        if text is None:
            return
        obj, _err = P.parse(payload)
        if not isinstance(obj, dict):
            return
        channel = str(obj.get("channel") or "")
        body = obj.get("payload")
        ident = [seq, conn, rec["rx_utc"], rec["rx_unix"], rx_mono, self.flight_id()]
        try:
            if channel == "velocity" and isinstance(body, dict):
                row = P.flatten_ws_velocity(body)
                sinks["velocity"].row(ident + [row[c] for c in P.WS_VELOCITY_COLUMNS])
                self.live.update(ws_velocity=row)
                self.live.count("ws_velocity")
                if row.get("carrying_out_periodic_cycling"):
                    self.live.count("ws_periodic_cycling")
            elif channel:
                row = P.flatten_ws_motion(channel, body)
                sinks["motion"].row(ident + [row[c] for c in P.WS_MOTION_COLUMNS])
                self.live.merge("motion", {channel: body})
                self.live.count(f"ws_{channel}")
        except Exception as ex:
            self.note(f"a web-stream {channel or '?'} message could not be "
                      f"tabulated ({type(ex).__name__}); it is in the raw file")

    # ------------------------------------------------------------------
    #  the DVL's web API: status, configuration, clock
    # ------------------------------------------------------------------

    def _status_loop(self) -> None:
        sinks = {"raw": _Sink(self, "http_raw"),
                 "status": _Sink(self, "status", header=STATUS_COLUMNS)}
        session: webapi.Session | None = None
        seq = 0
        next_slow = 0.0
        next_disk = time.monotonic() + DISK_CHECK_S
        next_manifest = time.monotonic() + MANIFEST_EVERY_S
        last_flight = ""
        prev: dict = {}
        try:
            while not self.stop_event.is_set():
                began = time.monotonic()
                fid = self.flight_id()
                if fid != last_flight:
                    if fid:
                        self.flights_seen.append({"flight_id": fid,
                                                  "seen_utc": webapi.utc(time.time())})
                        self.event("flight recording", f"{fid} began (flight recorder)")
                    else:
                        self.event("flight recording", f"{last_flight} closed")
                    last_flight = fid
                if began >= next_disk:
                    next_disk = began + DISK_CHECK_S
                    self._check_disk()

                values: dict = {"errors": []}
                addr = self.address
                if addr is not None:
                    if session is None or session.address != addr:
                        if session is not None:
                            session.close()
                        session = webapi.Session(addr, timeout=3.0)
                    paths = list(webapi.STATUS_PATHS)
                    if began >= next_slow:
                        paths += list(webapi.SLOW_PATHS)
                        next_slow = began + SLOW_PERIOD_S
                    reachable = False
                    interrupted = False
                    for path in paths:
                        if self.stop_event.is_set():
                            interrupted = True
                            break
                        reply = session.get(path)
                        seq += 1
                        sinks["raw"].json_line(reply.record(seq, "dvl"))
                        if reply.ok:
                            reachable = True
                            self._interpret(path, reply, values)
                        else:
                            values["errors"].append(f"{path}: {reply.error}")
                            if reply.status is None:
                                # Not answering at all: the rest would only
                                # time out the same way.
                                break
                    if interrupted:
                        # Stopped half-way through a poll: a row with half
                        # its columns blank would read as a DVL that stopped
                        # answering. The replies already read are in the
                        # raw file.
                        break
                    values["reachable"] = reachable
                    self.live.update(web_error="" if reachable else
                                     "; ".join(values["errors"])[:200])
                else:
                    values["reachable"] = False
                    values["errors"].append("DVL address unknown")

                ext = self._ext_values
                if ext:
                    values.update(ext)
                    values["ext_age_s"] = round(time.monotonic() - self._ext_at, 1)
                self._status_events(prev, values)
                prev = values
                now_ns = time.time_ns()
                values["poll_ms"] = (time.monotonic() - began) * 1000.0
                row = {**values, "rx_utc": _utc(now_ns),
                       "rx_unix": round(now_ns / 1e9, 6), "flight_id": fid,
                       "dvl_address": str(addr) if addr else "",
                       "errors": "; ".join(values["errors"])}
                sinks["status"].row([row.get(c) for c in STATUS_COLUMNS])
                self.live.merge("status", row)
                if isinstance(values.get("temperature_c"), (int, float)):
                    self.live.add_temperature(now_ns / 1e9, values["temperature_c"])
                for sink in sinks.values():
                    sink.tick()
                if time.monotonic() >= next_manifest:
                    next_manifest = time.monotonic() + MANIFEST_EVERY_S
                    self.write_manifest(final=False)
                self.stop_event.wait(max(0.2, STATUS_PERIOD_S
                                         - (time.monotonic() - began)))
        finally:
            if session is not None:
                session.close()
            for sink in sinks.values():
                sink.close()

    def _interpret(self, path: str, reply: webapi.Reply, values: dict) -> None:
        data = reply.json()
        if path == "/api/v1/about/status" and isinstance(data, dict):
            values["temperature_c"] = data.get("temperature")
            values["cpu_load"] = data.get("cpu_load")
            disk = data.get("disk_free") or {}
            if isinstance(disk, dict):
                values["disk_free_root_gb"] = disk.get("root")
                values["disk_free_data_gb"] = disk.get("data")
        elif path == "/api/v1/warnings/" and isinstance(data, list):
            ids = [str(w.get("id") or w.get("message") or w) if isinstance(w, dict)
                   else str(w) for w in data]
            values["n_warnings"] = len(ids)
            values["warnings"] = "; ".join(ids)
        elif path == "/api/v1/outputs/" and isinstance(data, list):
            for out in data:
                if not isinstance(out, dict):
                    continue
                oid = out.get("id")
                if oid == "tcp_water_linked":
                    values["json_clients"] = out.get("clients")
                    values["json_format"] = out.get("format")
                    try:
                        values["json_port"] = int(out.get("port"))
                    except (TypeError, ValueError):
                        values["json_port"] = out.get("port")
                elif oid == "tcp_pd6":
                    values["pd6_clients"] = out.get("clients")
                elif oid == "tcp_pd4":
                    values["pd4_clients"] = out.get("clients")
                elif oid == "serial":
                    values["serial_format"] = out.get("format")
            port = values.get("json_port")
            if isinstance(port, int) and 0 < port < 65536 and port != self.tcp_port:
                self.event("TCP port", f"the DVL reports its JSON output on {port}; "
                                       f"following it")
                self.tcp_port = port
        elif path == "/api/v1/config" and isinstance(data, dict):
            for k in ("speed_of_sound", "mounting_rotation_offset", "acoustic_enabled",
                      "dark_mode_enabled", "range_mode", "periodic_cycling_enabled"):
                values[f"cfg_{k}"] = data.get(k)
            self._config_seen(data, "web API")
        elif path == "/api/v1/time" and isinstance(data, dict):
            values["ntp_enabled"] = data.get("ntp_enabled")
            values["ntp_server"] = data.get("ntp_server")
            values["ntp_synchronized"] = data.get("ntp_synchronized")
            ns = webapi.parse_iso_ns(str(data.get("time") or ""))
            if ns is not None:
                values["dvl_clock_utc"] = data.get("time")
                mid_ms = (reply.t0_unix + reply.t1_unix) / 2 * 1000.0
                values["dvl_clock_minus_laptop_ms"] = ns / 1e6 - mid_ms
                values["clock_uncertainty_ms"] = reply.elapsed_ms / 2
            if not self.time_first:
                self.time_first = dict(data)
        elif path == "/api/v1/about" and isinstance(data, dict):
            if not self.about:
                self.event("DVL", " · ".join(str(data.get(k)) for k in
                                             ("product_name", "variant", "version")
                                             if data.get(k)))
            self.about = {**data, **{k: v for k, v in self.about.items()
                                     if k not in data}}

    def _config_seen(self, cfg: dict, via: str) -> None:
        if not self.config_first:
            self.config_first = dict(cfg)
        if self.config_last:
            changed = [f"{k}: {self.config_last.get(k)!r} → {cfg.get(k)!r}"
                       for k in sorted(set(cfg) | set(self.config_last))
                       if cfg.get(k) != self.config_last.get(k)]
            if changed:
                self.event("DVL configuration changed", f"({via}) " + "; ".join(changed))
        self.config_last = dict(cfg)

    def _status_events(self, prev: dict, now: dict) -> None:
        """The transitions in the status poll worth a line in the events file."""
        def changed(key):
            return key in now and key in prev and now.get(key) != prev.get(key)

        if now.get("cfg_periodic_cycling_enabled") is True and (
                prev.get("cfg_periodic_cycling_enabled") is not True):
            self.event("periodic cycling is ON",
                       "the DVL drops measurements every 10 s while it checks "
                       "its bottom lock")
        if changed("json_clients"):
            self.event("clients on the DVL's JSON output",
                       f"{prev.get('json_clients')} → {now.get('json_clients')} "
                       f"(this capture is one of them)")
        if changed("warnings"):
            self.event("DVL warnings", now.get("warnings") or "none")
        if changed("ntp_synchronized"):
            self.event("DVL clock", "NTP synchronized" if now.get("ntp_synchronized")
                       else "NTP NOT synchronized")
        if changed("reachable"):
            self.event("DVL web API", "answering" if now.get("reachable")
                       else "not answering")
        t = now.get("temperature_c")
        p = prev.get("temperature_c")
        if isinstance(t, (int, float)) and t >= HOT_C and not (
                isinstance(p, (int, float)) and p >= HOT_C):
            self.event("DVL HOT", f"{t:.1f} °C — it shuts itself down at 55 °C")

    # ------------------------------------------------------------------
    #  the acoustic views
    # ------------------------------------------------------------------

    def _acoustic_loop(self) -> None:
        sinks = {"echo": _Sink(self, "echo"), "spectrum": _Sink(self, "spectrum")}
        session: webapi.Session | None = None
        seq = 0
        due = time.monotonic()
        try:
            while not self.stop_event.is_set():
                hz = self.snapshot_hz
                addr = self.address
                if hz <= 0 or addr is None or self.disk_state != "ok":
                    self.stop_event.wait(0.5)
                    due = time.monotonic()
                    continue
                now = time.monotonic()
                if now < due:
                    self.stop_event.wait(min(0.5, due - now))
                    continue
                # Behind is not made up with a burst: the next read is a
                # period after this one started, or now, whichever is later.
                due = max(due + 1.0 / hz, now)
                if session is None or session.address != addr:
                    if session is not None:
                        session.close()
                    session = webapi.Session(addr, timeout=3.0)
                for path, key in ((webapi.ECHO_PATH, "echo"),
                                  (webapi.SPECTRUM_PATH, "spectrum")):
                    reply = session.get(path)
                    seq += 1
                    sinks[key].json_line(reply.record(seq, "dvl"))
                    if reply.ok:
                        data = reply.json()
                        if isinstance(data, dict):
                            self.live.update(**{key: {**data, "t": reply.t1_unix,
                                                      "ms": reply.elapsed_ms}})
                            self.live.count(key)
                    elif reply.status is None:
                        break
                for sink in sinks.values():
                    sink.tick()
        finally:
            if session is not None:
                session.close()
            for sink in sinks.values():
                sink.close()

    # ------------------------------------------------------------------
    #  the vehicle side: the extension, and what reached MAVLink
    # ------------------------------------------------------------------

    def _vehicle_loop(self) -> None:
        from ..nav.mav2rest import Mavlink2Rest
        sinks = {"raw": _Sink(self, "vehicle_raw"),
                 "mavlink": _Sink(self, "mavlink", header=MAVLINK_COLUMNS)}
        ext_session: webapi.Session | None = None
        ext_next = 0.0
        seq = 0
        readers: dict = {}
        prev: dict = {}
        #: key -> (consecutive "never sent" answers, when to ask next). A
        #: message this vehicle has never sent -- POSITION_DELTA when the
        #: extension sends estimates -- is asked about every half minute
        #: rather than every poll, and rejoins the poll the moment it appears.
        absent: dict = {}
        host = None
        try:
            while not self.stop_event.is_set():
                began = time.monotonic()
                if host != self.vehicle_host:
                    host = self.vehicle_host
                    readers = {
                        (255, 0): Mavlink2Rest(host, system=255, component=0,
                                               timeout=2.0),
                        (1, 1): Mavlink2Rest(host, system=1, component=1,
                                             timeout=2.0),
                    }
                    prev.clear()
                    absent.clear()
                    ext_next = 0.0

                if began >= ext_next:
                    ext_session, reply, values = self._read_extension(ext_session, host)
                    if reply is not None:
                        seq += 1
                        sinks["raw"].json_line(reply.record(seq, "dvl_extension"))
                    if not values.get("ext_reachable"):
                        ext_next = began + EXTENSION_RETRY_S
                    self._extension_seen(values)

                vehicle_down = False
                fid = self.flight_id()
                for (sysid, comp), names in (((255, 0), EXTENSION_MESSAGES),
                                             ((1, 1), AUTOPILOT_MESSAGES)):
                    if vehicle_down:
                        break
                    reader = readers[(sysid, comp)]
                    for name in names:
                        if self.stop_event.is_set():
                            break
                        key = f"{sysid}/{comp}/{name}"
                        misses, next_at = absent.get(key, (0, 0.0))
                        if misses >= 3 and time.monotonic() < next_at:
                            continue
                        s = reader.read(name)
                        if s.error == "never sent by this vehicle":
                            absent[key] = (misses + 1, time.monotonic() + 30.0)
                        else:
                            absent.pop(key, None)
                        now_ns = time.time_ns()
                        sinks["mavlink"].row([_utc(now_ns), round(now_ns / 1e9, 6),
                                              fid, sysid, comp, name, s.counter,
                                              s.frequency, s.fresh, s.vehicle_time,
                                              s.error])
                        if s.fresh and s.message:
                            seq += 1
                            sinks["raw"].json_line({
                                "seq": seq, "source": "mavlink2rest",
                                "system": sysid, "component": comp, "message": name,
                                "rx_utc": _utc(now_ns),
                                "rx_unix": round(now_ns / 1e9, 6),
                                "counter": s.counter, "frequency_hz": s.frequency,
                                "body": s.message})
                        rate = None
                        if s.counter is not None and key in prev:
                            c0, t0 = prev[key]
                            if s.counter >= c0 and now_ns > t0:
                                rate = (s.counter - c0) / ((now_ns - t0) / 1e9)
                        if s.counter is not None:
                            prev[key] = (s.counter, now_ns)
                        self.live.merge("mavlink", {key: {
                            "counter": s.counter, "frequency": s.frequency,
                            "rate": rate, "error": s.error}})
                        # A vehicle that is not answering at all would cost a
                        # timeout per message; one is enough to know.
                        if (s.error and s.error != "never sent by this vehicle"
                                and s.counter is None):
                            vehicle_down = s.error
                            break
                self.live.merge("mavlink", {"_down": vehicle_down or ""})
                for sink in sinks.values():
                    sink.tick()
                self.stop_event.wait(max(0.2, VEHICLE_PERIOD_S
                                         - (time.monotonic() - began)))
        finally:
            if ext_session is not None:
                ext_session.close()
            for sink in sinks.values():
                sink.close()

    def _read_extension(self, session, host: str):
        """The BlueOS DVL extension's `/get_status`: (session, reply, values).

        The extension's port is found once, through BlueOS's own register of
        services. Its `hostname` is where the extension believes the DVL is,
        and is what the capture connects to unless an address was typed.
        """
        port = self.extension.get("port")
        if port is None:
            try:
                from ..nav import extensions as X
                svc = X.discover(host, timeout=2.0)["dvl"]
                self.extension = {**self.extension, "name": svc.name,
                                  "port": svc.port, "found_via": svc.via,
                                  "note": svc.note}
                port = svc.port
            except Exception as ex:
                self.extension = {**self.extension, "error": str(ex)}
            if port is None:
                return session, None, {"ext_reachable": False}
        address = webapi.Address(host, web_port=port)
        if session is None or session.address != address:
            if session is not None:
                session.close()
            session = webapi.Session(address, timeout=1.5)
        reply = session.get("/get_status")
        values: dict = {"ext_reachable": reply.ok}
        data = reply.json() if reply.ok else None
        if isinstance(data, dict):
            values.update({
                "ext_status": data.get("status"),
                "ext_enabled": data.get("enabled"),
                "ext_should_send": data.get("should_send"),
                "ext_rangefinder": data.get("rangefinder"),
                "ext_orientation": data.get("orientation"),
                "ext_hostname": str(data.get("hostname") or ""),
            })
        else:
            values["ext_reachable"] = False
        return session, reply, values

    def _extension_seen(self, values: dict) -> None:
        before = self._ext_values
        self._ext_values = dict(values)
        self._ext_at = time.monotonic()
        if values.get("ext_reachable"):
            self.extension = {**self.extension,
                              "last_status": values.get("ext_status"),
                              "should_send": values.get("ext_should_send"),
                              "hostname": values.get("ext_hostname")}
        if before.get("ext_status") != values.get("ext_status") and (
                before or values.get("ext_status")):
            self.event("DVL extension", f"{before.get('ext_status')!r} → "
                                        f"{values.get('ext_status')!r}")
        if bool(before.get("ext_reachable")) != bool(values.get("ext_reachable")):
            self.event("DVL extension", "answering" if values.get("ext_reachable")
                       else "not answering")
        if not self._override:
            hostname = str(values.get("ext_hostname") or "")
            found = (webapi.Address.parse(hostname.split(":")[0])
                     if hostname else None)
            if found is not None:
                self._set_address(found, "the BlueOS DVL extension")
            elif self.address is None:
                self.live.update(tcp_error=(
                    "DVL address unknown — the BlueOS DVL extension has not "
                    "said where the DVL is. Type it on the DVL tab."))
