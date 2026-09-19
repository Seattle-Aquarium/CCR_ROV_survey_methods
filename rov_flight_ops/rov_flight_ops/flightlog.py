"""
One flight, recorded from arming to disarming, without anyone pressing a button.

What this writes into a flight's ``logs`` folder, per flight:

===========================  ==================================================
``laptop_monitor_*.csv``     the topside at 1 Hz, one row a second
``laptop_monitor_*.json``    what those columns mean and which this machine
                             could actually fill
``params_*.json``            every autopilot parameter as it stood at disarming
``delta_params_*.json/txt``  the ones that moved during the flight, with the
                             value before and the value after
``versions_*.json``          BlueOS, ArduSub, the board and every extension
``delta_versions_*.json/txt``  the ones that moved during the flight
===========================  ==================================================

**Arming is the trigger, not a button.** The pilot arms to fly and disarms when
they are done; that is already the truth of when a flight happened, recorded by
the autopilot. Asking someone to also press Start here would mean the record is
missing on exactly the busy days it matters most. Arm state is read from the
HEARTBEAT mavlink2rest already holds, so watching it costs one small GET every
couple of seconds and sends the vehicle nothing.

**A brief disarm does not end a flight.** Sitting on the surface between
transects, a bump of the arm switch, a failsafe that trips and clears -- all of
these disarm the vehicle without the dive being over. Ending the flight on the
first disarm would cut one dive into four files with four sets of parameters,
none of which answers "what was set on that dive?". So a disarm opens a grace
period, and only its expiry closes the flight. Re-arming inside it carries on
the same recording, and the gap is noted in the companion file rather than
hidden.

**A dropped request is not a disarm.** `read_arm_state` returns None when it
cannot tell, and None never ends a flight. The tether drops packets -- this
programme has measured it doing so -- and a recorder that stopped on one lost
GET would stop mid-transect.

**Nothing here can fail a flight.** Every write is best-effort and every read is
wrapped. The worst outcome is a missing column or a missing file, never a
traceback in front of someone flying an ROV.

**Nothing here runs on the window's thread.** Starting to watch reads the
laptop's performance counters and asks WMI about the battery; closing a flight
waits for workers and reads every parameter off a vehicle that may not be
answering. Each used to run inside a button's callback, where a slow vehicle
froze the window for as long as it took. The window now queues a `request`
and returns; one lifecycle thread carries the requests out in order, so two
presses cannot interleave, and the page shows "starting…" or "closing…" while
one is under way.

**Every flight and every watch owns its own workers.** A `_Session` is one
flight: its CSV, its sampler, its trace and its threads, with its own stop
signal. A `_Watch` is one period of watching, likewise. Nothing is shared with
the next one, so a worker that outlives a timed join -- a timed join does not
stop a thread -- cannot be woken again by the next start, cannot write into
the next flight, and cannot overwrite its opening snapshot. A resource is
closed by the thread that uses it, as that thread exits: the CSV and the
performance counters by the sampling loop, the trace files by the trace loops,
the ICMP handle by the ping loop. Nothing closes a native handle under a call
still using it. A worker that does not stop in time is reported as such -- on
the Monitoring tab and in the diagnostics log -- rather than assumed stopped.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import blueos, diagnostics, flightfile, laptop, netdiag, nettrace
from . import wincounters as W

log = logging.getLogger(__name__)

#: How often the vehicle is asked whether it is armed. Two seconds is well
#: inside the grace period and costs the Pi one small GET; a faster poll would
#: buy nothing but load on a machine that is recording a dive.
ARM_POLL_S = 2.0

#: How often the Pi's own temperature and throttle state are read. Slower than
#: the arm poll because it moves slowly, and it is carried into every 1 Hz row
#: regardless -- held forward between reads rather than interpolated.
PI_POLL_S = 5.0

#: How often the vehicle's own end of the tether is read -- its Ethernet
#: counters, and the Fathom-X link rate when the tether diagnostics extension
#: is installed. Slower than the temperature poll because it is two GETs
#: rather than one, and because the counters are cumulative: a reading missed
#: is not a reading lost, it is a larger step in the next one.
TETHER_POLL_S = 5.0

#: How long a disarm must stand before the flight is considered over.
#: Ninety seconds covers a surface interval between transects and a failsafe
#: that clears, and is short enough that the closing snapshot is taken while
#: the vehicle is still on the tether.
DISARM_GRACE_S = 90.0

#: Rows are written as they are sampled, and flushed and synced to disk on
#: this cadence. A laptop that loses power mid-dive loses the rows since the
#: last sync -- normally under this many seconds -- rather than the flight.
#: The operating system and the drive still have the last word on that.
FLUSH_EVERY_S = 10.0

#: A vehicle reading older than this many of its own poll periods is shown as
#: unknown rather than held forward. A flat chart must not imply a current
#: measurement once the reads have stopped succeeding.
STALE_AFTER_POLLS = 3

#: The longest the closing snapshot may hold up closing a flight. Its reads
#: each have their own timeouts, but against a vehicle that has gone quiet
#: those add up to minutes. The CSV is already closed by then; this bounds
#: only how long the parameter and version record waits, and a record that
#: gave up says so.
CLOSING_SNAPSHOT_S = 90.0

#: How long closing waits for the opening snapshot, when it is still reading.
OPENING_SNAPSHOT_WAIT_S = 10.0

#: How long closing waits for a flight's own workers -- the sampling loop and
#: the trace loops -- to stop and close their files.
WORKER_JOIN_S = 5.0

#: How long stopping waits for the watcher threads. They hold nothing that
#: needs closing, so this is only to report whether they have gone -- and it
#: is short, because it is part of how long closing the window takes.
WATCH_JOIN_S = 1.0

#: How long a lifecycle request waits for a flight another thread is closing.
CLOSE_WAIT_S = 180.0


def _stamp(when: float | None = None) -> str:
    """`2026-09-11_130512`, local time -- the field's own clock, to the second.

    To the second, not the minute: stopping and restarting a recording inside
    one minute used to reuse the name and overwrite the first flight's CSV.
    """
    return datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d_%H%M%S")


def unique_flight_id(folder: Path, when: float | None = None) -> str:
    """A flight id no existing record in `folder` already uses.

    The time stamp alone is not a guarantee -- two starts in one second, or a
    laptop clock set back -- so a suffix is added until nothing in the folder
    carries the id. Every file a flight writes is then opened exclusively, so
    even a race cannot replace an earlier record.
    """
    base = _stamp(when)
    candidate, n = base, 1
    while any(Path(folder).glob(f"*_{candidate}.*")):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat(
        timespec="seconds")


@dataclass
class Snapshot:
    """The vehicle's configuration at one instant."""

    taken: float = 0.0
    parameters: dict = field(default_factory=dict)
    parameters_from: str = ""
    versions: dict = field(default_factory=dict)
    dataflash_log: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.parameters) or bool(self.versions)


@dataclass
class Status:
    """What the recorder is doing, for the page that shows it."""

    state: str = "idle"          # idle | starting | recording | closing
    armed: bool | None = None
    flight_id: str = ""
    rows: int = 0
    started: float = 0.0
    since_disarm: float | None = None
    csv_path: Path | None = None
    by_hand: bool = False
    note: str = ""
    problem: str = ""
    #: The address the watcher is actually asking.
    host: str = ""
    #: The flight folder the open recording is being written into. Fixed for
    #: the length of the flight, whatever folder the window moves on to.
    session_dir: Path | None = None
    #: When a row last reached the CSV, and how many could not be written.
    #: "Reached" means handed to the file -- buffered, not yet on the disk.
    last_write: float = 0.0
    dropped: int = 0
    #: When the CSV was last flushed and synced to disk: the last moment the
    #: rows are known to be on the drive, as far as the OS will say.
    last_sync: float = 0.0
    #: An address typed during a flight, applied once the flight closes.
    pending_host: str = ""
    #: A lifecycle request under way -- "Starting monitoring", "Closing the
    #: recording" -- or "".
    transition: str = ""
    #: A worker that did not stop when asked. Set until the program restarts:
    #: it is still running somewhere, and that is worth knowing.
    degraded: str = ""
    #: How the last flight closed, in words.
    outcome: str = ""
    #: When the vehicle last answered the arm poll, and how long it took.
    #: `armed` alone cannot stand in for this: it holds its last value, so a
    #: vehicle that was disarmed and has since been unplugged still reads
    #: "disarmed" forever. This is the thing that goes stale.
    last_seen: float = 0.0
    link_ms: float | None = None

    @property
    def reachable(self) -> bool:
        """Did the vehicle answer recently enough to call it connected?

        Three poll intervals: one missed answer is a dropped packet, three in
        a row is the tether.
        """
        return (self.last_seen > 0
                and time.time() - self.last_seen < ARM_POLL_S * 3)

    def line(self) -> str:
        if self.problem:
            return self.problem
        if self.state == "starting":
            return f"Starting recording {self.flight_id}…"
        if self.state == "recording":
            mins = (time.time() - self.started) / 60 if self.started else 0
            if self.by_hand:
                tail = "   started by hand — press Stop to close it"
            elif self.since_disarm is None:
                tail = ""
            else:
                tail = (f"   disarmed {self.since_disarm:.0f}s ago, closing in "
                        f"{max(0, DISARM_GRACE_S - self.since_disarm):.0f}s")
            return (f"Recording {self.flight_id} — {self.rows:,} rows, "
                    f"{mins:.1f} min{tail}")
        if self.state == "closing":
            return f"Closing {self.flight_id} — reading the vehicle…"
        if self.transition:
            return f"{self.transition}…"
        if self.armed is None:
            return "Waiting for the vehicle. Nothing is being recorded."
        return "Waiting for the ROV to arm. Nothing is being recorded."


class Request(threading.Event):
    """A lifecycle action queued for the recorder's own thread; set when done."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.started = False
        self.result = None
        self.error: BaseException | None = None


#: What the page says while each request is carried out.
TRANSITIONS = {
    "watch": "Starting monitoring",
    "unwatch": "Stopping monitoring",
    "record": "Starting a recording",
    "stop": "Closing the recording",
    "close": "Closing the recorder",
}


class _Watch:
    """One period of watching for arming: its own stop signal and threads."""

    def __init__(self, serial: int) -> None:
        self.serial = serial
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []


class _Session:
    """One flight, and everything it owns. Nothing is shared with the next."""

    def __init__(self, serial: int, flight_id: str, folder: Path,
                 session_dir: Path | None, host: str, manual: bool) -> None:
        self.serial = serial
        self.flight_id = flight_id
        #: The logs folder the flight writes into, fixed at arming.
        self.folder = folder
        self.session_dir = session_dir
        self.host = host
        self.manual = manual
        self.started = time.time()
        self.stop = threading.Event()
        self.done = threading.Event()
        #: Set, under the recorder's lock, by whichever path closes the flight
        #: first. Every other path then waits for `done` instead.
        self.closing = False
        self.fh = None
        self.writer = None
        self.csv_path: Path | None = None
        self.sampler: laptop.Sampler | None = None
        self.tracer: nettrace.Tracer | None = None
        self.rows = 0
        self.dropped = 0
        self.last_write = 0.0
        self.last_sync = 0.0
        self.flushed_at = time.monotonic()
        self.gaps: list[dict] = []
        self.disarm_at: float | None = None
        self.start_snap = Snapshot()
        self.threads: dict[str, threading.Thread] = {}
        self.stuck: list[str] = []


class FlightRecorder:
    """Watches for arming, and records a flight when it happens.

    Owns its own threads rather than going through the application's single
    worker: that worker is for jobs with a beginning and an end and refuses to
    start a second, and a recorder that blocked the operator from fetching
    files for the length of a dive would be worse than no recorder.

    The window uses `request`, which returns at once. The direct methods --
    `start_watching`, `stop_watching`, `start_manually`, `stop_manually` --
    block until they are done, and are what the lifecycle thread calls.
    """

    def __init__(self, *, host: str = "192.168.2.2",
                 flight_dir: Path | None = None,
                 on_change=None):
        self.host = host
        self.flight_dir = Path(flight_dir) if flight_dir else None
        #: Called on any state change, from a worker thread. The GUI must
        #: marshal it onto the main thread itself -- Tk is not thread-safe.
        self.on_change = on_change

        self.status = Status()
        #: Half an hour, the longest window the Monitoring charts offer.
        self.history = laptop.History(minutes=30)
        self.capabilities: dict[str, str] = {}

        self._tether_read_at = 0.0
        self._pi_interface = ""
        #: None until the tether diagnostics extension has been asked once.
        self._tether_ok: bool | None = None
        self._tether_seen: dict = {}
        #: The last flight record written, so the GUI can say what changed
        #: without re-reading the file it just produced.
        self._record: dict = {}

        self.status.host = host
        #: Serialises beginning and ending flights, and who watches.
        self._lock = threading.RLock()
        self._watch: _Watch | None = None
        self._session: _Session | None = None
        self._serial = 0

        self._ops: queue.Queue[Request] = queue.Queue()
        self._ops_lock = threading.Lock()
        self._ops_thread: threading.Thread | None = None
        self._last_request: Request | None = None
        self._inflight = 0

    # ------------------------------------------------------------------
    #  requests from the window
    # ------------------------------------------------------------------

    def request(self, action: str) -> Request:
        """Queue `watch`, `unwatch`, `record`, `stop` or `close`. Returns at once.

        Requests are carried out one at a time, in order, on the recorder's
        lifecycle thread. Asking again for what is already waiting at the back
        of the queue returns that request rather than queuing a second.
        """
        if action not in TRANSITIONS:
            raise ValueError(f"unknown recorder request {action!r}")
        with self._ops_lock:
            last = self._last_request
            if (last is not None and last.action == action
                    and not last.started and not last.is_set()):
                return last
            req = Request(action)
            self._last_request = req
            # Under the same lock that clears it, so a request that finishes
            # at once cannot leave the page saying "Starting…" for ever.
            self._inflight += 1
            if not self.status.transition:
                self.status.transition = TRANSITIONS[action]
            self._ops.put(req)
            if self._ops_thread is None or not self._ops_thread.is_alive():
                self._ops_thread = threading.Thread(
                    target=self._run_requests, daemon=True,
                    name="utc-recorder-lifecycle")
                self._ops_thread.start()
        return req

    @property
    def transitioning(self) -> bool:
        """True while a request is queued or being carried out."""
        return self._inflight > 0

    def _run_requests(self) -> None:
        while True:
            try:
                req = self._ops.get(timeout=30.0)
            except queue.Empty:
                with self._ops_lock:
                    if self._ops.empty():
                        self._ops_thread = None
                        return
                continue
            req.started = True
            self.status.transition = TRANSITIONS[req.action]
            diagnostics.note_activity("recorder", TRANSITIONS[req.action])
            log.info("recorder: %s", TRANSITIONS[req.action].lower())
            began = time.monotonic()
            try:
                req.result = self._perform(req.action)
            except Exception as ex:
                req.error = ex
                diagnostics.log_exception(f"recorder request {req.action}",
                                          *sys.exc_info())
                self.status.problem = (f"{TRANSITIONS[req.action]} failed: "
                                       f"{type(ex).__name__}: {ex}")
            finally:
                with self._ops_lock:
                    self._inflight -= 1
                    if self._inflight <= 0:
                        self._inflight = 0
                        self.status.transition = ""
                diagnostics.note_activity("recorder", None)
                log.info("recorder: %s finished in %.1f s (result %r)",
                         req.action, time.monotonic() - began, req.result)
                req.set()
                self._changed()

    def _perform(self, action: str):
        if action == "watch":
            self.start_watching()
            return self.watching
        if action == "record":
            if not self.watching:
                self.start_watching()
            return self.start_manually()
        if action == "stop":
            return self.stop_manually(wait=True)
        # "unwatch" and "close" both close an open flight properly.
        return self.stop_watching(finish=True)

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """Begin watching for arming. Records nothing until the ROV arms."""
        with self._lock:
            if self._watch is not None:
                return
            self._serial += 1
            watch = _Watch(self._serial)
            self._watch = watch
        if not self.capabilities:
            # Performance counters and a WMI query: slow on a busy laptop,
            # which is why this is never called from the window's thread.
            try:
                self.capabilities = W.capabilities()
            except Exception:
                diagnostics.log_exception("reading capabilities", *sys.exc_info(),
                                          level=logging.WARNING)
        with self._lock:
            if self._watch is not watch:
                return                     # stopped while the counters opened
            for target, name in ((self._watch_loop, "utc-arm-watch"),
                                 (self._tether_loop, "utc-tether")):
                thread = threading.Thread(target=target, args=(watch,),
                                          daemon=True, name=name)
                watch.threads.append(thread)
                thread.start()
        log.info("recorder: watching %s for arming (watch %d)", self.host,
                 watch.serial)

    def stop_watching(self, *, finish: bool = True,
                      timeout: float = CLOSE_WAIT_S) -> bool:
        """Stop watching. An open flight is closed properly unless told not to.

        True when everything that was running is known to have stopped and
        closed; False when something is still finishing or did not stop.
        """
        with self._lock:
            watch, self._watch = self._watch, None
        if watch is not None:
            watch.stop.set()
        clean = True
        session = self._session
        if session is not None:
            clean = self._close(session, reason="monitoring stopped",
                                write_record=finish, timeout=timeout)
        if watch is not None:
            deadline = time.monotonic() + WATCH_JOIN_S
            for thread in watch.threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            alive = [t.name for t in watch.threads if t.is_alive()]
            if alive:
                # They hold nothing, and their stop signal stays set: each
                # exits when the read it is waiting on returns.
                log.info("recorder: watch %d stopped; %s still finishing a read",
                         watch.serial, ", ".join(alive))
            else:
                log.info("recorder: watch %d stopped", watch.serial)
        self._changed()
        return clean

    @property
    def watching(self) -> bool:
        return self._watch is not None

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  the watcher
    # ------------------------------------------------------------------

    def _vehicle(self):
        """The open flight's vehicle readings, or None between flights."""
        session = self._session
        sampler = session.sampler if session is not None else None
        return sampler.vehicle if sampler is not None else None

    def _watch_loop(self, watch: _Watch) -> None:
        last_pi = 0.0
        while not watch.stop.is_set():
            try:
                armed, ms = blueos.read_arm_state(self.host)
                if watch.stop.is_set():
                    break
                self.status.armed = armed
                if ms is not None:
                    self.status.last_seen = time.time()
                    self.status.link_ms = ms
                vehicle = self._vehicle()
                if vehicle is not None:
                    vehicle.reachable = ms is not None
                    vehicle.armed = armed
                    vehicle.http_ms = ms

                if time.monotonic() - last_pi > PI_POLL_S:
                    last_pi = time.monotonic()
                    self._read_pi()

                if armed is True:
                    self._on_armed(watch)
                elif armed is False:
                    self._on_disarmed(watch)
                # armed is None: the vehicle did not answer. Deliberately no
                # action -- an unanswered question is not a disarm.
                self._changed()
            except Exception:
                diagnostics.log_exception("arm watcher", *sys.exc_info(),
                                          level=logging.WARNING)
            watch.stop.wait(ARM_POLL_S)

    def _read_pi(self) -> None:
        vehicle = self._vehicle()
        if vehicle is None:
            return
        try:
            soc, _peak = blueos.read_temperature(self.host)
            vehicle.soc_temp_c = soc
        except Exception:
            pass

    def _tether_loop(self, watch: _Watch) -> None:
        """The vehicle's end of the tether, on a thread that may block.

        A vehicle that is not answering costs 25 seconds to walk for its
        tether diagnostics the first time and eight to ask for its interface
        counters. Neither may happen on the loop that watches for arming: a
        blackout is when those reads are slowest and when arm detection
        matters most.
        """
        while not watch.stop.wait(TETHER_POLL_S):
            try:
                self._read_tether()
            except Exception:
                diagnostics.log_exception("tether reader", *sys.exc_info(),
                                          level=logging.WARNING)

    def _read_tether(self) -> None:
        """The vehicle's own count of what reached it, and the link rate.

        Cumulative counters, held forward into every row between reads. The
        reading that matters is the one taken when a link comes back: the step
        in it says how much arrived while the topside could see nothing, which
        is the difference between a tether that stopped carrying frames and a
        topside that stopped sending them.
        """
        vehicle = self._vehicle()
        if vehicle is None:
            return
        got = False
        try:
            interfaces = blueos.read_interfaces(self.host, timeout=3.0)
            name = blueos.tether_interface(interfaces)
            if name:
                counters = interfaces[name]
                vehicle.eth_rx_bytes = counters.get("rx_bytes")
                vehicle.eth_rx_errors = counters.get("rx_errors")
                self._pi_interface = name
                got = True
        except Exception:
            pass
        if got:
            self._tether_read_at = time.monotonic()
        elif time.monotonic() - self._tether_read_at > TETHER_POLL_S * STALE_AFTER_POLLS:
            # Held forward for a few polls, then shown as unknown.
            vehicle.eth_rx_bytes = vehicle.eth_rx_errors = None
        # The tether extension is asked every poll. A vehicle without it is
        # not walked every time: `read_tether` remembers the miss and asks
        # again only every couple of minutes, so an extension started after
        # the flight began is still found -- which one failed first probe
        # used to prevent for the rest of the flight.
        try:
            found = blueos.read_tether(self.host, timeout=6.0)
        except Exception:
            found = {}
        if found:
            self._tether_ok = True
            vehicle.tether_tx_mbps = found.get("tx_mbps")
            vehicle.tether_rx_mbps = found.get("rx_mbps")
            self._tether_seen = found
        else:
            if self._tether_ok is None:
                self._tether_ok = False
            vehicle.tether_tx_mbps = vehicle.tether_rx_mbps = None
        # The device list is the plainer signal: whether the extension can
        # currently hear a remote PLC node at all, separate from what rate it
        # negotiated with it. Same probe-and-remember discipline as above, and
        # deliberately a second call rather than folded into read_tether --
        # the two are different routes on the same extension and one working
        # says nothing about the other.
        try:
            devices = blueos.read_tether_devices(self.host, timeout=6.0)
        except Exception:
            devices = {}
        if devices:
            count = devices.get("count")
            vehicle.tether_remote_seen = (count or 0) >= 2
        else:
            vehicle.tether_remote_seen = None

    def _on_armed(self, watch: _Watch) -> None:
        with self._lock:
            if watch is not self._watch:
                return                     # a retired watch: not ours to act on
            session = self._session
            if session is not None:
                if (self.status.state == "recording" and not session.closing
                        and session.disarm_at is not None):
                    # Re-armed inside the grace period: one flight, with a gap
                    # in it that is recorded rather than smoothed over.
                    session.gaps.append({
                        "disarmed": _iso(session.disarm_at),
                        "rearmed": _iso(time.time()),
                        "seconds": round(time.time() - session.disarm_at, 1)})
                    session.disarm_at = None
                    self.status.since_disarm = None
                return
            if self.status.state != "idle":
                return
            self._begin_flight(manual=False)

    def _on_disarmed(self, watch: _Watch) -> None:
        with self._lock:
            if watch is not self._watch:
                return
            session = self._session
            if (session is None or session.closing or session.manual
                    or self.status.state != "recording"):
                return
            if session.disarm_at is None:
                session.disarm_at = time.time()
            self.status.since_disarm = time.time() - session.disarm_at
            if self.status.since_disarm < DISARM_GRACE_S:
                return
            if not self._claim_close(session):
                return
        # Closed on a thread of its own, so the watcher goes on polling.
        threading.Thread(target=self._finish, args=(session,),
                         kwargs={"reason": "disarmed"}, daemon=True,
                         name="utc-end-flight").start()

    # ------------------------------------------------------------------
    #  a flight
    # ------------------------------------------------------------------

    def _begin_flight(self, *, manual: bool) -> _Session | None:
        """Open the CSV and start sampling. Called with the lock held.

        The lock is held throughout, deliberately: nothing may close a flight
        that is half started. Every caller is off the window's thread.
        """
        folder = self._logs_dir()
        if folder is None:
            # The operator chose that a flight without a folder should not be
            # silently filed somewhere else. Say so loudly and keep watching:
            # choosing a folder mid-dive still catches the rest of it.
            self.status.problem = (
                ("Nothing is being recorded. " if manual else
                 "THE ROV IS ARMED AND NOTHING IS BEING RECORDED. ")
                + "Choose a flight folder on Flight & transects — the monitor "
                  "writes into its logs folder and will not guess one.")
            return None
        self.status.problem = ""
        now = time.time()
        stamp = unique_flight_id(folder, now)
        path = folder / f"laptop_monitor_{stamp}.csv"
        self._serial += 1
        session = _Session(self._serial, stamp, folder, Path(self.flight_dir),
                           self.host, manual)
        try:
            # "x": a flight record is never silently replaced.
            session.fh = path.open("x", newline="", encoding="utf-8")
            session.writer = csv.DictWriter(session.fh, fieldnames=laptop.COLUMNS,
                                            extrasaction="ignore")
            session.writer.writeheader()
            session.fh.flush()
        except Exception as ex:
            self.status.problem = (f"NOT RECORDING: could not create {path.name} "
                                   f"({ex}).")
            log.error("recorder: could not create %s: %s", path, ex)
            if session.fh is not None:
                try:
                    session.fh.close()
                except Exception:
                    pass
            return None
        session.csv_path = path
        session.last_write = time.time()

        st = self.status
        st.state = "starting"
        st.flight_id, st.started = stamp, session.started
        st.rows = st.dropped = 0
        st.last_write, st.last_sync = session.last_write, 0.0
        st.since_disarm = None
        st.by_hand = manual
        st.csv_path = path
        st.session_dir = session.session_dir
        st.note = ""
        self._session = session
        self.history.clear()
        self._changed()

        # The opening snapshot is a download, so it runs on its own thread and
        # the recording does not wait for it.
        #
        # It is started before anything else here, and that ordering is load
        # bearing. The whole value of this snapshot is that it is the vehicle
        # *as it was at arming*: everything it catches -- a parameter turned in
        # Cockpit, an extension restarted -- is something that changes during
        # the dive, so every millisecond between arming and the read is a
        # millisecond in which the "before" can become the "after". Work queued
        # ahead of it here already cost the recorder a parameter change it
        # should have seen.
        self._spawn(session, "snap-start", self._capture_start)

        try:
            session.sampler = laptop.Sampler(rov_host=self.host, flight_id=stamp)
            session.sampler.start()
        except Exception as ex:
            diagnostics.log_exception("starting the laptop sampler", *sys.exc_info())
            st.problem = (f"NOT RECORDING: the laptop sampler could not start "
                          f"({type(ex).__name__}: {ex}).")
            session.closing = True
            session.stop.set()
            self._close_csv(session)
            if session.sampler is not None:
                try:
                    session.sampler.stop(wait=False)
                except Exception:
                    pass
            self._session = None
            st.state = "idle"
            st.by_hand = False
            st.session_dir = None
            session.done.set()
            self._changed()
            return None

        self._spawn(session, "sample", self._sample_loop)
        st.state = "recording"

        # The fast network trace is its own recorder with its own threads and
        # its own files. Started after the CSV is open and the state is set, so
        # that a station where it cannot start -- no bridge, no ICMP, a folder
        # that refuses a fourth file -- still records the flight.
        tracer = nettrace.Tracer(host=self.host, folder=folder, flight_id=stamp)
        try:
            if tracer.start():
                session.tracer = tracer
            else:
                st.note = tracer.problem
        except Exception as ex:
            st.note = f"No fast network trace: {ex}"

        # What the topside network looked like when the flight opened, on its
        # own thread: the report resolves ARP and opens a TCP connection, and
        # neither of those may happen while this holds the lock that arming
        # goes through.
        self._spawn(session, "net-report", self._write_network_report)
        log.info("recorder: flight %s started %s, writing %s", stamp,
                 "by hand" if manual else "at arming", path)
        self._changed()
        return session

    @staticmethod
    def _spawn(session: _Session, name: str, target) -> threading.Thread:
        thread = threading.Thread(target=target, args=(session,), daemon=True,
                                  name=f"utc-{name}")
        session.threads[name] = thread
        thread.start()
        return thread

    def _sample_loop(self, session: _Session) -> None:
        """One row a second, on a fixed cadence rather than a drifting sleep.

        This thread is the only one that writes the CSV or reads the
        performance counters, so it is the one that closes them, on its way
        out -- never another thread while a write or a counter read is still
        under way on this one.
        """
        tick = time.monotonic()
        try:
            while not session.stop.is_set():
                tick += laptop.DEFAULT_PERIOD_S
                # A reading that fails is a blank cell -- the sampler already
                # promises that -- so a failure here is unexpected but harmless.
                try:
                    row = session.sampler.sample()
                except Exception:
                    diagnostics.log_exception("sampling a row", *sys.exc_info(),
                                              level=logging.WARNING)
                    row = None
                if row is not None:
                    if self._session is session:
                        self.history.add(row)
                    self._write_row(session, row)
                # Waiting to the next tick rather than for a second keeps the
                # cadence honest when a sample runs long; waiting on the stop
                # signal lets a close begin at once.
                session.stop.wait(max(0.0, tick - time.monotonic()))
        finally:
            self._close_csv(session)
            try:
                session.sampler.stop(wait=False)
            except Exception:
                diagnostics.log_exception("stopping the laptop sampler",
                                          *sys.exc_info(), level=logging.WARNING)

    def _capture_start(self, session: _Session) -> None:
        # Into its own flight, whenever it finishes: a slow read cannot land
        # in the flight after it.
        session.start_snap = self._snapshot()
        self._changed()

    def _snapshot(self, since_log: str = "") -> Snapshot:
        """Read the vehicle's parameters and versions. Never raises."""
        snap = Snapshot(taken=time.time())
        try:
            token = blueos.file_token(self.host)
            snap.dataflash_log = blueos.newest_dataflash(self.host, token)
            snap.parameters, snap.parameters_from = blueos.read_parameters_now(
                self.host, token, since_log=since_log)
        except Exception:
            pass
        try:
            snap.versions = blueos.read_versions(self.host)
        except Exception:
            pass
        return snap

    def _closing_snapshot(self, session: _Session) -> tuple[Snapshot, bool]:
        """The closing snapshot, given at most `CLOSING_SNAPSHOT_S`.

        (snapshot, whether it finished). A read still going when the time is
        up is left to finish on its own; its result is not used.
        """
        box: dict[str, Snapshot] = {}
        since = session.start_snap.dataflash_log

        def read(_session) -> None:
            box["snap"] = self._snapshot(since_log=since)

        thread = self._spawn(session, "snap-end", read)
        thread.join(CLOSING_SNAPSHOT_S)
        if thread.is_alive() or "snap" not in box:
            return Snapshot(taken=time.time()), False
        return box["snap"], True

    def _claim_close(self, session: _Session) -> bool:
        """Make the caller the one path that closes `session`. Lock held inside."""
        with self._lock:
            if self._session is not session or session.closing:
                return False
            session.closing = True
            session.stop.set()
            self.status.state = "closing"
            return True

    def _close(self, session: _Session, *, reason: str, write_record: bool,
               timeout: float) -> bool:
        """Close `session` here, or wait for whoever already is."""
        if self._claim_close(session):
            self._finish(session, reason=reason, write_record=write_record)
        finished = session.done.wait(timeout)
        return finished and not session.stuck

    def _finish(self, session: _Session, *, reason: str,
                write_record: bool = True) -> None:
        """Close a claimed flight: stop its workers, take the closing snapshot,
        write the record. Runs on whichever thread claimed it -- never Tk's."""
        began = time.monotonic()
        diagnostics.note_activity("recorder", f"closing {session.flight_id}")
        log.info("recorder: closing flight %s (%s)", session.flight_id, reason)
        self._changed()
        notes: list[str] = []
        try:
            sample = session.threads.get("sample")
            if sample is not None:
                sample.join(WORKER_JOIN_S)
                if sample.is_alive():
                    session.stuck.append("the laptop sampler")
            else:
                self._close_csv(session)
            if session.tracer is not None:
                try:
                    if not session.tracer.stop():
                        session.stuck.append("the network trace")
                except Exception:
                    diagnostics.log_exception("stopping the network trace",
                                              *sys.exc_info(), level=logging.WARNING)
            if session.stuck:
                notes.append(f"{' and '.join(session.stuck)} had not stopped "
                             f"{WORKER_JOIN_S:g} s after the flight closed; its "
                             f"files are closed when it does, so the last rows "
                             f"may postdate this record")

            if write_record:
                opening = session.threads.get("snap-start")
                if opening is not None:
                    opening.join(OPENING_SNAPSHOT_WAIT_S)
                    if opening.is_alive():
                        notes.append("the opening snapshot was still reading "
                                     "when the flight closed")
                end, answered = self._closing_snapshot(session)
                if not answered:
                    notes.append(f"the closing snapshot was abandoned after "
                                 f"{CLOSING_SNAPSHOT_S:g} s without an answer "
                                 f"from the vehicle")
                try:
                    self._write_files(session, end, reason=reason, notes=notes)
                except Exception as ex:
                    self.status.problem = f"Could not write the flight files: {ex}"
                    diagnostics.log_exception("writing the flight files",
                                              *sys.exc_info())
        except Exception:
            diagnostics.log_exception("closing a flight", *sys.exc_info())
        finally:
            outcome = (f"{session.flight_id} closed ({reason}): {session.rows:,} "
                       f"rows" + (f", {session.dropped:,} not written"
                                  if session.dropped else "")
                       + ("" if not notes else " — " + "; ".join(notes)))
            with self._lock:
                if self._session is session:
                    self._session = None
                    st = self.status
                    st.state = "idle"
                    st.since_disarm = None
                    st.by_hand = False
                    st.session_dir = None
                    st.outcome = outcome
                    if session.stuck:
                        st.degraded = (f"{' and '.join(session.stuck)} from "
                                       f"{session.flight_id} did not stop when "
                                       f"asked. Restart the program when "
                                       f"convenient.")
                    if st.pending_host:
                        self.host = st.pending_host
                        st.host = self.host
                        st.pending_host = ""
            session.done.set()
            diagnostics.note_activity("recorder", None)
            (log.warning if session.stuck else log.info)(
                "recorder: %s in %.1f s", outcome, time.monotonic() - began)
            self._changed()

    def _close_csv(self, session: _Session) -> None:
        fh, session.fh, session.writer = session.fh, None, None
        if fh is None:
            return
        try:
            fh.flush()
            try:
                os.fsync(fh.fileno())
                session.last_sync = time.time()
            except OSError:
                pass
            fh.close()
        except Exception as ex:
            if self._session is session:
                self.status.problem = (f"The flight CSV could not be closed "
                                       f"cleanly ({ex}); its last rows may be "
                                       f"missing.")
            log.error("recorder: closing %s failed: %s", session.csv_path, ex)
        if self._session is session:
            self.status.last_sync = session.last_sync

    # ------------------------------------------------------------------
    #  what lands in logs/
    # ------------------------------------------------------------------

    def _write_row(self, session: _Session, row: dict) -> None:
        """One row to disk. A failure is shown, counted and retried -- never hidden.

        Sensor failures are blank cells; a failed *write* is the recorder
        failing, and it says so in words on the Monitoring tab for as long as
        it lasts. The next row tries again, so a disk that recovers resumes
        the record and the note afterwards says how many rows were lost.
        """
        current = self._session is session
        st = self.status
        if session.writer is None:
            session.dropped += 1
            if current:
                st.dropped = session.dropped
            return
        try:
            session.writer.writerow(row)
            if time.monotonic() - session.flushed_at > FLUSH_EVERY_S:
                session.fh.flush()
                try:
                    os.fsync(session.fh.fileno())
                    session.last_sync = time.time()
                except OSError:
                    pass
                session.flushed_at = time.monotonic()
        except Exception as ex:
            session.dropped += 1
            diagnostics.log_exception("writing a flight row", *sys.exc_info())
            if current:
                st.dropped = session.dropped
                st.problem = (
                    f"RECORDING FAILED: rows are not reaching "
                    f"{session.csv_path.name if session.csv_path else 'the CSV'}"
                    f" ({type(ex).__name__}: {str(ex)[:80]}). {session.dropped:,} "
                    f"row(s) lost so far — check the drive.")
            return
        session.rows += 1
        session.last_write = time.time()
        if not current:
            return
        st.rows = session.rows
        st.last_write = session.last_write
        st.last_sync = session.last_sync
        if st.problem.startswith("RECORDING FAILED"):
            st.problem = ""
            st.note = (f"{session.dropped:,} row(s) could not be "
                       f"written earlier in this flight")

    def _logs_dir(self) -> Path | None:
        """The logs folder the next flight will be written into."""
        if not self.flight_dir:
            return None
        try:
            out = Path(self.flight_dir) / "logs"
            out.mkdir(parents=True, exist_ok=True)
            return out
        except Exception:
            return None

    def _write_files(self, session: _Session, end: Snapshot, *, reason: str,
                     notes: list[str] | None = None) -> None:
        """One record for the flight, and the CSVs beside it.

        Replaces the seven files an earlier version wrote. The reasons are in
        `flightfile`'s own docstring and all four came out of reading the
        11 September logs back: a parameter dump with no provenance cannot be
        compared to anything, a failed read written as a fact produced a
        confident and wrong report, an unpartitioned delta buried its own
        answer, and float64 tails made unchanged values diff as changes.
        """
        folder = session.folder
        stamp = session.flight_id
        dir_ = session.session_dir

        monitor = {
            "rows": session.rows,
            "sample_period_s": laptop.DEFAULT_PERIOD_S,
            "disarm_grace_s": DISARM_GRACE_S,
            "csv": f"laptop_monitor_{stamp}.csv",
            "columns": list(laptop.COLUMNS),
            "units": laptop.UNITS,
        }

        previous = None
        try:
            earlier = flightfile.find_previous(
                folder, stamp,
                search_root=(Path(dir_).parent if dir_ else None))
            if earlier is not None:
                previous = flightfile.compare_with_previous(
                    earlier, end.parameters or {}, end.versions or {})
        except Exception:
            previous = None

        note = "; ".join(n for n in [self.status.note if self._session is session
                                     else "", *(notes or [])] if n)
        try:
            record = flightfile.build(
                flight_id=stamp,
                started=session.started,
                ended=time.time(),
                reason=reason,
                rows=session.rows,
                computer=self._computer_name(),
                host=session.host,
                interface=self._interface_name(),
                opening=session.start_snap,
                closing=end,
                brief_disarms=session.gaps,
                capabilities=self.capabilities,
                monitor=monitor,
                network=self._network_summary(session),
                site=Path(dir_).name if dir_ else "",
                previous=previous,
                note=note,
            )
            flightfile.write(folder, record)
            self._record = record
        except Exception as ex:
            self.status.problem = f"Could not write the flight record: {ex}"
            diagnostics.log_exception("writing the flight record", *sys.exc_info())

    def _write_network_report(self, session: _Session) -> None:
        try:
            (session.folder / f"network_topside_{session.flight_id}.txt").write_text(
                netdiag.report(session.host), encoding="utf-8")
        except Exception:
            pass

    def _network_summary(self, session: _Session) -> dict:
        """What the tether looked like this flight, as one block of the JSON.

        Deliberately not a verdict. It records which interface was measured,
        whether it was a bridge, what the vehicle reported from its own end,
        and what Windows itself logged about any adapter changing state --
        and leaves the reading to whoever opens the file.
        """
        out: dict = {}
        try:
            out["topside"] = netdiag.snapshot(session.host, probe=False)
        except Exception:
            pass
        if self._pi_interface:
            out["vehicle_interface"] = self._pi_interface
        if self._tether_seen:
            out["tether_diagnostics"] = self._tether_seen
        elif self._tether_ok is False:
            out["tether_diagnostics"] = (
                "the tether diagnostics extension did not answer on this "
                "vehicle — the Fathom-X link rate is the one reading neither "
                "computer can take without it")
        tracer = session.tracer
        if tracer is not None:
            out["fast_trace"] = {
                "ticks": tracer.ticks,
                "echoes": tracer.echoes,
                "echoes_lost": tracer.lost,
                "counter_granularity": tracer.granularity(),
            }
        # Windows' own record of any adapter changing state over the flight.
        # An empty list is a finding: a carrier that never dropped leaves no
        # event, so an outage with nothing here behind it was not the cable.
        try:
            events = netdiag.ndis_events(session.started)
            out["windows_adapter_events"] = events[:40]
        except Exception:
            pass
        return out

    def _computer_name(self) -> str:
        import socket
        return socket.gethostname()

    def _interface_name(self) -> str:
        try:
            return laptop.find_rov_interface(self.host) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    #  the vehicle address
    # ------------------------------------------------------------------

    def retarget(self, host: str) -> bool:
        """Point the watcher at another address. False if it has to wait.

        While idle the change is immediate: the watcher and the tether reader
        both read `host` on every poll. During a flight it is held until the
        flight closes, so one flight's record never describes two vehicles.
        """
        host = host or "192.168.2.2"
        if host == self.host:
            self.status.pending_host = ""
            return True
        with self._lock:
            if self._session is not None or self.status.state != "idle":
                self.status.pending_host = host
                return False
            self.host = host
            self.status.host = host
            self.status.armed = None
            self._tether_ok = None
        self._changed()
        return True

    # ------------------------------------------------------------------
    #  manual override
    # ------------------------------------------------------------------

    def start_manually(self) -> bool:
        """Begin a flight without waiting for the ROV to arm.

        A flight begun by hand is ended by hand. The override exists for when
        arm detection is the thing that is not working -- a bench test, a
        vehicle whose MAVLink router has died -- so letting the disarm watcher
        close it would defeat the point. It closed a manual recording ninety
        seconds in the first time this was tried.
        """
        with self._lock:
            if self._session is not None or self.status.state != "idle":
                return False
            return self._begin_flight(manual=True) is not None

    def stop_manually(self, *, wait: bool = False) -> bool:
        """End the flight now, without waiting out the grace period.

        Returns at once and closes on a thread of its own, unless `wait`.
        """
        with self._lock:
            session = self._session
            if session is None or self.status.state != "recording":
                return False
            if not self._claim_close(session):
                return False
        if wait:
            self._finish(session, reason="stopped by hand")
            return not session.stuck
        threading.Thread(target=self._finish, args=(session,),
                         kwargs={"reason": "stopped by hand"}, daemon=True,
                         name="utc-end-flight").start()
        return True


# --------------------------------------------------------------------------
#  the human-readable halves
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True,
                                   default=str), encoding="utf-8")
    except Exception:
        pass


def _params_table(delta: dict, stamp: str, start: Snapshot,
                  end: Snapshot) -> str:
    """The parameter changes as a table somebody can read on deck.

    The ones ArduPilot sets itself are listed apart. Barometer ground pressure
    is re-zeroed at every arming, so it changes on every flight; leaving it in
    the main list would mean the file always looks as though something was
    changed, and the one flight where something actually was would look the
    same as all the others.
    """
    by_hand = {k: v for k, v in delta.items() if not blueos.is_automatic(k)}
    by_itself = {k: v for k, v in delta.items() if blueos.is_automatic(k)}

    out = [
        f"ArduSub parameter changes during flight {stamp}",
        "=" * 64,
        f"before : {_iso(start.taken)}   from {start.parameters_from or '?'}"
        f"   ({len(start.parameters):,} parameters)",
        f"after  : {_iso(end.taken)}   from {end.parameters_from or '?'}"
        f"   ({len(end.parameters):,} parameters)",
        "",
    ]
    if not delta:
        out.append("Nothing changed during this flight.")
        return "\n".join(out) + "\n"

    if by_hand:
        out += [f"CHANGED DURING THE FLIGHT  ({len(by_hand)})", "-" * 64]
        out += [_param_line(k, a, b) for k, (a, b) in by_hand.items()]
        out.append("")
    else:
        out += ["No parameter was changed by hand during this flight.", ""]
    if by_itself:
        out += [f"set by the autopilot itself  ({len(by_itself)})",
                "-" * 64]
        out += [_param_line(k, a, b) for k, (a, b) in by_itself.items()]
        out.append("")
    return "\n".join(out) + "\n"


def _param_line(name: str, before, after) -> str:
    return (f"  {name:<24s} {_v(before):>18s}  ->  {_v(after):<18s}"
            + ("   (added)" if before is None else
               "   (removed)" if after is None else ""))


def _v(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        # Trailing zeros on a parameter read as significance it does not have.
        return f"{value:.10g}"
    return str(value)


def _versions_table(delta: dict, stamp: str, start: Snapshot,
                    end: Snapshot) -> str:
    out = [
        f"Software version changes during flight {stamp}",
        "=" * 64,
        f"before : {_iso(start.taken)}",
        f"after  : {_iso(end.taken)}",
        "",
    ]
    if not delta:
        out += ["Nothing changed during this flight.", "",
                "Versions as flown:", "-" * 64]
        v = end.versions or start.versions
        out += [f"  {'BlueOS':<28s} {v.get('blueos', '?')}",
                f"  {'ArduSub':<28s} {v.get('ardusub', '?')} "
                f"({v.get('ardusub_type', '')})",
                f"  {'board':<28s} {v.get('board', '?')}"]
        for e in v.get("extensions") or []:
            mark = "" if e.get("enabled", True) else "   (disabled)"
            out.append(f"  {('ext: ' + str(e.get('name', ''))):<28s} "
                       f"{e.get('tag', '')}{mark}")
        return "\n".join(out) + "\n"

    out += [f"CHANGED DURING THE FLIGHT  ({len(delta)})", "-" * 64]
    for name, (a, b) in delta.items():
        out.append(f"  {name:<34s} {_v(a):>22s}  ->  {_v(b)}")
    out.append("")
    return "\n".join(out) + "\n"
