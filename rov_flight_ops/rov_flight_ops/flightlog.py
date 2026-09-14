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
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import blueos, flightfile, laptop, netdiag, nettrace
from . import wincounters as W

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

    state: str = "idle"          # idle | recording | closing | disconnected
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
    last_write: float = 0.0
    dropped: int = 0
    #: An address typed during a flight, applied once the flight closes.
    pending_host: str = ""

    def line(self) -> str:
        if self.problem:
            return self.problem
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
        if self.armed is None:
            return "Waiting for the vehicle. Nothing is being recorded."
        return "Waiting for the ROV to arm. Nothing is being recorded."


class FlightRecorder:
    """Watches for arming, and records a flight when it happens.

    Owns its own threads rather than going through the application's single
    worker: that worker is for jobs with a beginning and an end and refuses to
    start a second, and a recorder that blocked the operator from fetching
    files for the length of a dive would be worse than no recorder.
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

        self._sampler: laptop.Sampler | None = None
        self._fh = None
        self._writer = None
        self._rows = 0
        self._flushed_at = 0.0

        self._start_snap = Snapshot()
        self._log_at_arm = ""
        #: The flight folder of the flight in progress. `flight_dir` is where
        #: the *next* flight goes; this is where the current one is going.
        self._session_dir: Path | None = None
        self._last_sync = 0.0
        self._tether_read_at = 0.0
        #: The fast network trace for the flight in progress, when one is
        #: running. None between flights, and None on a station where it
        #: could not start -- which must not stop the flight being recorded.
        self.tracer: nettrace.Tracer | None = None
        self._pi_interface = ""
        #: None until the tether diagnostics extension has been asked once.
        self._tether_ok: bool | None = None
        self._tether_seen: dict = {}
        #: The last flight record written, so the GUI can say what changed
        #: without re-reading the file it just produced.
        self._record: dict = {}
        self._gaps: list[dict] = []
        self._disarm_at: float | None = None
        #: A flight begun by hand is ended by hand. The override exists for
        #: when arm detection is the thing that is not working -- a bench
        #: test, a vehicle whose MAVLink router has died -- so letting the
        #: disarm watcher close it would defeat the point. It closed a manual
        #: recording ninety seconds in the first time this was tried.
        self._manual = False

        self.status.host = host
        self._stop = threading.Event()
        self._watch: threading.Thread | None = None
        self._tether: threading.Thread | None = None
        self._sample: threading.Thread | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """Begin watching for arming. Records nothing until the ROV arms."""
        if self._watch is not None:
            return
        self._stop.clear()
        self.capabilities = W.capabilities()
        self._watch = threading.Thread(target=self._watch_loop, daemon=True,
                                       name="utc-arm-watch")
        self._watch.start()
        self._tether = threading.Thread(target=self._tether_loop, daemon=True,
                                        name="utc-tether")
        self._tether.start()

    def stop_watching(self, *, finish: bool = True) -> None:
        """Stop watching. An open flight is closed properly unless told not to."""
        self._stop.set()
        if self._watch is not None:
            self._watch.join(timeout=5.0)
        if self._tether is not None:
            # Longer than the others: a read already in flight against an
            # unreachable vehicle has its own timeouts to run out first.
            self._tether.join(timeout=10.0)
            self._tether = None
            self._watch = None
        if self.status.state == "recording":
            if finish:
                self._end_flight(reason="monitoring stopped")
            else:
                self._close_csv()

    @property
    def watching(self) -> bool:
        return self._watch is not None and self._watch.is_alive()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  the watcher
    # ------------------------------------------------------------------

    def _watch_loop(self) -> None:
        last_pi = 0.0
        while not self._stop.is_set():
            try:
                armed, ms = blueos.read_arm_state(self.host)
                self.status.armed = armed
                if self._sampler is not None:
                    self._sampler.vehicle.reachable = ms is not None
                    self._sampler.vehicle.armed = armed
                    self._sampler.vehicle.http_ms = ms

                if time.monotonic() - last_pi > PI_POLL_S:
                    last_pi = time.monotonic()
                    self._read_pi()


                if armed is True:
                    self._on_armed()
                elif armed is False:
                    self._on_disarmed()
                # armed is None: the vehicle did not answer. Deliberately no
                # action -- an unanswered question is not a disarm.
                self._changed()
            except Exception:
                pass
            self._stop.wait(ARM_POLL_S)

    def _read_pi(self) -> None:
        if self._sampler is None:
            return
        try:
            soc, _peak = blueos.read_temperature(self.host)
            self._sampler.vehicle.soc_temp_c = soc
        except Exception:
            pass

    def _tether_loop(self) -> None:
        """The vehicle's end of the tether, on a thread that may block.

        A vehicle that is not answering costs 25 seconds to walk for its
        tether diagnostics the first time and eight to ask for its interface
        counters. Neither may happen on the loop that watches for arming: a
        blackout is when those reads are slowest and when arm detection
        matters most.
        """
        while not self._stop.wait(TETHER_POLL_S):
            try:
                self._read_tether()
            except Exception:
                pass

    def _read_tether(self) -> None:
        """The vehicle's own count of what reached it, and the link rate.

        Cumulative counters, held forward into every row between reads. The
        reading that matters is the one taken when a link comes back: the step
        in it says how much arrived while the topside could see nothing, which
        is the difference between a tether that stopped carrying frames and a
        topside that stopped sending them.
        """
        if self._sampler is None:
            return
        vehicle = self._sampler.vehicle
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
            found = blueos.read_tether(self.host, timeout=2.5)
        except Exception:
            found = {}
        if found:
            self._tether_ok = True
            rate = found.get("rx_mbps")
            if rate is None:
                rate = found.get("tx_mbps")
            vehicle.tether_mbps = rate
            self._tether_seen = found
        else:
            if self._tether_ok is None:
                self._tether_ok = False
            vehicle.tether_mbps = None

    def _on_armed(self) -> None:
        with self._lock:
            if self.status.state == "recording":
                if self._disarm_at is not None:
                    # Re-armed inside the grace period: one flight, with a gap
                    # in it that is recorded rather than smoothed over.
                    self._gaps.append({
                        "disarmed": _iso(self._disarm_at),
                        "rearmed": _iso(time.time()),
                        "seconds": round(time.time() - self._disarm_at, 1)})
                    self._disarm_at = None
                    self.status.since_disarm = None
                return
            if self.status.state != "idle":
                return
            self._begin_flight()

    def _on_disarmed(self) -> None:
        with self._lock:
            if self.status.state != "recording" or self._manual:
                return
            if self._disarm_at is None:
                self._disarm_at = time.time()
            self.status.since_disarm = time.time() - self._disarm_at
            if self.status.since_disarm >= DISARM_GRACE_S:
                self._end_flight(reason="disarmed")

    # ------------------------------------------------------------------
    #  a flight
    # ------------------------------------------------------------------

    def _begin_flight(self) -> None:
        """Open the CSV and start sampling. Called with the lock held."""
        folder = self._logs_dir()
        if folder is None:
            # The operator chose that a flight without a folder should not be
            # silently filed somewhere else. Say so loudly and keep watching:
            # choosing a folder mid-dive still catches the rest of it.
            self.status.problem = (
                ("Nothing is being recorded. " if self._manual else
                 "THE ROV IS ARMED AND NOTHING IS BEING RECORDED. ")
                + "Choose a flight folder on Flight & transects — the monitor "
                  "writes into its logs folder and will not guess one.")
            return
        self.status.problem = ""
        now = time.time()
        stamp = unique_flight_id(folder, now)
        self.status.flight_id = stamp
        self.status.started = now
        self.status.rows = 0
        self.status.dropped = 0
        self.status.last_write = 0.0
        self._rows = 0
        self._gaps = []
        self._disarm_at = None
        self.status.since_disarm = None
        self.history.clear()

        path = folder / f"laptop_monitor_{stamp}.csv"
        try:
            # "x": a flight record is never silently replaced.
            self._fh = path.open("x", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(self._fh, fieldnames=laptop.COLUMNS,
                                          extrasaction="ignore")
            self._writer.writeheader()
            self._fh.flush()
        except Exception as ex:
            self.status.problem = (f"NOT RECORDING: could not create {path.name} "
                                   f"({ex}).")
            self._fh = None
            return
        # Everything this flight writes goes here until it closes, whatever
        # folder is chosen in the meantime.
        self._session_dir = Path(self.flight_dir)
        self.status.session_dir = self._session_dir
        self.status.csv_path = path
        self.status.last_write = time.time()
        self._flushed_at = time.monotonic()

        self._sampler = laptop.Sampler(rov_host=self.host, flight_id=stamp)
        self._sampler.start()
        self.status.state = "recording"

        self._sample = threading.Thread(target=self._sample_loop, daemon=True,
                                        name="utc-sampler")
        self._sample.start()

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
        threading.Thread(target=self._capture_start, daemon=True,
                         name="utc-snap-start").start()

        # The fast network trace is its own recorder with its own threads and
        # its own files. Started after the CSV is open and the state is set, so
        # that a station where it cannot start -- no bridge, no ICMP, a folder
        # that refuses a fourth file -- still records the flight.
        self.tracer = nettrace.Tracer(host=self.host, folder=folder,
                                      flight_id=stamp)
        try:
            if not self.tracer.start():
                self.status.note = self.tracer.problem
                self.tracer = None
        except Exception as ex:
            self.status.note = f"No fast network trace: {ex}"
            self.tracer = None

        # What the topside network looked like when the flight opened, on its
        # own thread: the report resolves ARP and opens a TCP connection, and
        # neither of those may happen while this holds the lock that arming
        # goes through.
        threading.Thread(target=self._write_network_report, args=(folder, stamp),
                         daemon=True, name="utc-net-report").start()
        self._changed()

    def _sample_loop(self) -> None:
        """One row a second, on a fixed cadence rather than a drifting sleep."""
        tick = time.monotonic()
        while not self._stop.is_set() and self.status.state == "recording":
            tick += laptop.DEFAULT_PERIOD_S
            sampler = self._sampler
            if sampler is None:
                break
            # A reading that fails is a blank cell -- the sampler already
            # promises that -- so a failure here is unexpected but harmless.
            try:
                row = sampler.sample()
                self.history.add(row)
            except Exception:
                row = None
            if row is not None:
                self._write_row(row)
            # Sleeping to the next tick rather than for a second keeps the
            # cadence honest when a sample runs long.
            time.sleep(max(0.0, tick - time.monotonic()))

    def _capture_start(self) -> None:
        self._start_snap = self._snapshot()
        self._log_at_arm = self._start_snap.dataflash_log
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

    def _end_flight(self, *, reason: str) -> None:
        """Close the CSV, take the closing snapshot, write the deltas."""
        if self.status.state != "recording":
            return
        self.status.state = "closing"
        self._changed()
        if self._sample is not None:
            self._sample.join(timeout=5.0)
            self._sample = None
        self._close_csv()
        if self.tracer is not None:
            try:
                self.tracer.stop()
            except Exception:
                pass
        if self._sampler is not None:
            self._sampler.stop()
            self._sampler = None

        end = self._snapshot(since_log=self._log_at_arm)
        try:
            self._write_files(end, reason=reason)
        except Exception as ex:
            self.status.problem = f"Could not write the flight files: {ex}"
        self.status.state = "idle"
        self.status.since_disarm = None
        self._disarm_at = None
        self._manual = False
        self.status.by_hand = False
        self._session_dir = None
        self.status.session_dir = None
        if self.status.pending_host:
            self.host = self.status.pending_host
            self.status.host = self.host
            self.status.pending_host = ""
        self._changed()

    def _close_csv(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._fh.close()
            except Exception as ex:
                self.status.problem = (f"The flight CSV could not be closed "
                                       f"cleanly ({ex}); its last rows may be "
                                       f"missing.")
        self._fh = None
        self._writer = None

    # ------------------------------------------------------------------
    #  what lands in logs/
    # ------------------------------------------------------------------

    def _write_row(self, row: dict) -> None:
        """One row to disk. A failure is shown, counted and retried -- never hidden.

        Sensor failures are blank cells; a failed *write* is the recorder
        failing, and it says so in words on the Monitoring tab for as long as
        it lasts. The next row tries again, so a disk that recovers resumes
        the record and the note afterwards says how many rows were lost.
        """
        if self._writer is None:
            self.status.dropped += 1
            return
        try:
            self._writer.writerow(row)
            if time.monotonic() - self._flushed_at > FLUSH_EVERY_S:
                self._fh.flush()
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                self._flushed_at = time.monotonic()
        except Exception as ex:
            self.status.dropped += 1
            self.status.problem = (
                f"RECORDING FAILED: rows are not reaching "
                f"{self.status.csv_path.name if self.status.csv_path else 'the CSV'}"
                f" ({type(ex).__name__}: {str(ex)[:80]}). {self.status.dropped:,} "
                f"row(s) lost so far — check the drive.")
            return
        self._rows += 1
        self.status.rows = self._rows
        self.status.last_write = time.time()
        if self.status.problem.startswith("RECORDING FAILED"):
            self.status.problem = ""
            self.status.note = (f"{self.status.dropped:,} row(s) could not be "
                                f"written earlier in this flight")

    def _logs_dir(self) -> Path | None:
        # The flight in progress keeps its own folder to the end.
        base = self._session_dir if self._session_dir is not None else self.flight_dir
        if not base:
            return None
        try:
            out = Path(base) / "logs"
            out.mkdir(parents=True, exist_ok=True)
            return out
        except Exception:
            return None

    def _write_files(self, end: Snapshot, *, reason: str) -> None:
        """One record for the flight, and the CSVs beside it.

        Replaces the seven files an earlier version wrote. The reasons are in
        `flightfile`'s own docstring and all four came out of reading the
        11 September logs back: a parameter dump with no provenance cannot be
        compared to anything, a failed read written as a fact produced a
        confident and wrong report, an unpartitioned delta buried its own
        answer, and float64 tails made unchanged values diff as changes.
        """
        folder = self._logs_dir()
        if folder is None:
            return
        session = self._session_dir or self.flight_dir
        stamp = self.status.flight_id
        start = self._start_snap

        monitor = {
            "rows": self._rows,
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
                search_root=(Path(session).parent if session else None))
            if earlier is not None:
                previous = flightfile.compare_with_previous(
                    earlier, end.parameters or {}, end.versions or {})
        except Exception:
            previous = None

        try:
            record = flightfile.build(
                flight_id=stamp,
                started=self.status.started,
                ended=time.time(),
                reason=reason,
                rows=self._rows,
                computer=self._computer_name(),
                host=self.host,
                interface=self._interface_name(),
                opening=start,
                closing=end,
                brief_disarms=self._gaps,
                capabilities=self.capabilities,
                monitor=monitor,
                network=self._network_summary(),
                site=Path(session).name if session else "",
                previous=previous,
                note=self.status.note or "",
            )
            flightfile.write(folder, record)
            self._record = record
        except Exception as ex:
            self.status.problem = f"Could not write the flight record: {ex}"

    def _write_network_report(self, folder: Path, stamp: str) -> None:
        try:
            (folder / f"network_topside_{stamp}.txt").write_text(
                netdiag.report(self.host), encoding="utf-8")
        except Exception:
            pass

    def _network_summary(self) -> dict:
        """What the tether looked like this flight, as one block of the JSON.

        Deliberately not a verdict. It records which interface was measured,
        whether it was a bridge, what the vehicle reported from its own end,
        and what Windows itself logged about any adapter changing state --
        and leaves the reading to whoever opens the file.
        """
        out: dict = {}
        try:
            out["topside"] = netdiag.snapshot(self.host, probe=False)
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
        if self.tracer is not None:
            out["fast_trace"] = {
                "ticks": self.tracer.ticks,
                "echoes": self.tracer.echoes,
                "echoes_lost": self.tracer.lost,
                "counter_granularity": self.tracer.granularity(),
            }
        # Windows' own record of any adapter changing state over the flight.
        # An empty list is a finding: a carrier that never dropped leaves no
        # event, so an outage with nothing here behind it was not the cable.
        try:
            events = netdiag.ndis_events(self.status.started)
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
        if self.status.state in ("recording", "closing"):
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
        """Begin a flight without waiting for the ROV to arm."""
        with self._lock:
            if self.status.state != "idle":
                return False
            self._manual = True
            self.status.by_hand = True
            self._begin_flight()
            if self.status.state != "recording":
                self._manual = False
                self.status.by_hand = False
            return self.status.state == "recording"

    def stop_manually(self) -> bool:
        """End the flight now, without waiting out the grace period."""
        with self._lock:
            if self.status.state != "recording":
                return False
            threading.Thread(
                target=lambda: self._end_flight(reason="stopped by hand"),
                daemon=True, name="utc-end-flight").start()
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
