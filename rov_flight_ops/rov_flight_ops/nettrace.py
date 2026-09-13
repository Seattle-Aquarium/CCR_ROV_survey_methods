"""
The tether, ten times a second, on both sides of the bridge.

The 1 Hz row in `laptop.py` proved a link had gone away and could not say
when, where, or in which direction. Three of its limits are structural and
none of them are fixed by adding columns:

  * **The failsafe it has to explain is three seconds long.** ArduSub disarms
    after three seconds without a heartbeat from the topside. A sampler with
    a one-second period puts a one-second error bar on a three-second event,
    which is not enough to say whether the link went first or the client did.
  * **It watches one interface.** On this station that interface is a Windows
    network bridge -- a software device whose carrier state and link speed
    describe the bridge, not the adapter beneath it. The reading that would
    separate "the tether dropped" from "the bridge stopped forwarding" is the
    *pair* of them, side by side, and one column cannot hold a pair.
  * **It reduces the ping to a rate.** A rolling loss percentage tells you
    something is wrong thirty seconds after it started and never tells you
    which packet was the first to go.

So this module runs beside the 1 Hz sampler rather than replacing it, and
records three narrow things fast instead of sixty wide things slowly:

  `network_fast_*.csv`    every watched interface's counters, 10 Hz
  `network_pings_*.csv`   one row per ICMP echo, 5 Hz, with its status code
  `network_events_*.txt`  the transitions, in the order they happened

Cumulative counters are written rather than rates. A rate computed here would
hide the counter it came from, and a missed tick would land as a plausible
average instead of the visible jump it should be.

**It must never cost a flight.** Everything runs on its own threads, every
write is wrapped, and a failure anywhere stops this trace and nothing else.
The 1 Hz recorder does not depend on it and is not slowed by it.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import netdiag
from . import wincounters as W

#: Ten times a second. Chosen against the three-second failsafe rather than
#: against what the hardware could bear: it puts a 100 ms error bar on the
#: start of an outage, which is fine enough to order the topside going quiet
#: and the vehicle noticing, and those two being the wrong way round is the
#: finding this is built to reach.
#:
#: Whether the *counters* move that fast is a property of the network driver,
#: not of this module, so `calibrate()` measures it rather than assuming it.
FAST_PERIOD_S = 0.1

#: Five echoes a second. Two consecutive losses then mean the link has been
#: silent for at least 400 ms, which is a finding; at 1 Hz two losses mean two
#: seconds and the vehicle has already disarmed.
ECHO_PERIOD_S = 0.2

#: Echo timeout. Short enough that a dead link costs the thread a quarter of
#: its period rather than a whole one.
ECHO_TIMEOUT_MS = 150

#: How often the set of watched interfaces is worked out again. A bridge
#: member that is unplugged and replaced mid-flight changes the set, and
#: re-deriving it every tick would cost more than the trace does.
RESCAN_S = 20.0

#: Rows between flushes. A flight that ends in a crash should still leave the
#: seconds that led up to it on disk.
FLUSH_EVERY = 50


def _slug(alias: str) -> str:
    """An interface name as a CSV column prefix: `Network Bridge` → `bridge`.

    Lower case, spaces to underscores, and nothing that would need quoting.
    The full name is kept in the JSON sidecar, so this only has to be stable
    and readable rather than reversible.
    """
    out = "".join(c.lower() if c.isalnum() else "_" for c in alias).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "iface"


#: What is written per interface, in this order. Counters are cumulative
#: since the adapter came up.
PER_IFACE = ("rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts",
             "rx_errors", "rx_discards", "tx_errors", "tx_discards",
             "carrier", "low_power", "oper")


@dataclass
class Event:
    """One transition, with the reading that caused it."""

    when: float
    what: str
    detail: str = ""

    def line(self) -> str:
        stamp = datetime.fromtimestamp(self.when, timezone.utc)
        return f"{stamp:%H:%M:%S.%f}"[:-3] + f"  {self.what:<28} {self.detail}"


@dataclass
class _Watch:
    """The last thing seen on one interface, for spotting a change."""

    alias: str
    index: int
    slug: str
    carrier: bool | None = None
    low_power: bool = False
    oper: int = 0
    rx: int = 0
    at: float = 0.0
    #: How many consecutive fast ticks arrived with the receive counter
    #: unmoved. Interpreted only against what `calibrate` measured: on an
    #: adapter whose driver updates lazily, a few still ticks mean nothing.
    still: int = 0
    #: Ticks over the whole flight on which the receive counter did move.
    #: `still` is the current run and resets; this one only goes up.
    moved: int = 0


class Tracer:
    """The fast network trace for one flight. Start it, stop it, read it.

    Owns two threads: one walking the interface table, one pinging. Neither
    touches the 1 Hz sampler, and both are daemons, so a stop that hangs
    cannot keep the application open.
    """

    def __init__(self, *, host: str = "192.168.2.2",
                 folder: Path | None = None, flight_id: str = "",
                 period: float = FAST_PERIOD_S,
                 echo_period: float = ECHO_PERIOD_S):
        self.host = host
        self.folder = Path(folder) if folder else None
        self.flight_id = flight_id or datetime.now().strftime("%Y-%m-%d_%H%M")
        self.period = period
        self.echo_period = echo_period

        self.events: list[Event] = []
        self.problem = ""
        self.ticks = 0
        self.echoes = 0
        self.lost = 0
        #: Whether the last reading said the link was carrying traffic. Read
        #: by the GUI, which wants a light rather than a file.
        self.alive: bool | None = None

        self._watch: dict[int, _Watch] = {}
        self._order: list[_Watch] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._fast_fh = None
        self._fast_writer = None
        self._ping_fh = None
        self._ping_writer = None
        self._rows = 0
        self._ping_rows = 0
        self._started = 0.0
        self._echo_state: tuple[bool, float] | None = None

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Open the files and start both threads. False if it could not."""
        if self._threads:
            return True
        self._started = time.time()
        try:
            self._order = self._rescan()
        except Exception as ex:                       # pragma: no cover
            self.problem = f"Could not read the interface table: {ex}"
            return False
        if not self._order:
            self.problem = (f"Nothing on this machine holds a subnet "
                            f"containing {self.host}; the fast network trace "
                            f"has no interface to watch.")
            return False
        if not self._open_files():
            return False
        self._note("trace started",
                   ", ".join(f"{w.alias} (index {w.index})" for w in self._order))
        self._stop.clear()
        for target, name in ((self._fast_loop, "utc-nettrace"),
                             (self._echo_loop, "utc-netecho")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        return True

    def stop(self) -> None:
        """Stop both threads and close the files. Safe to call twice."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads = []
        self._note("trace stopped",
                   f"{self.ticks} ticks, {self.echoes} echoes, "
                   f"{self.lost} of them lost")
        self._close_files()
        self._write_sidecars()

    # ------------------------------------------------------------------
    #  which interfaces
    # ------------------------------------------------------------------

    def _rescan(self) -> list[_Watch]:
        """Work out which interfaces to follow, keeping the ones already seen.

        An interface is never dropped once it has been watched. A bridge
        member that goes away mid-flight is exactly the event worth having a
        column for, and a column that disappears with it would be the log
        deleting its own evidence.
        """
        chosen = netdiag.watched(self.host)
        with self._lock:
            for iface in chosen:
                if iface.index in self._watch:
                    continue
                watch = _Watch(alias=iface.alias, index=iface.index,
                               slug=_slug(iface.alias))
                # Two adapters can slug the same. Disambiguate by index
                # rather than silently writing into one another's columns.
                taken = {w.slug for w in self._watch.values()}
                if watch.slug in taken:
                    watch.slug = f"{watch.slug}_{iface.index}"
                self._watch[iface.index] = watch
            return sorted(self._watch.values(), key=lambda w: w.index)

    def columns(self) -> list[str]:
        return ["timestamp_utc", "elapsed_s"] + [
            f"{w.slug}_{field_}" for w in self._order for field_ in PER_IFACE]

    # ------------------------------------------------------------------
    #  the files
    # ------------------------------------------------------------------

    def _open_files(self) -> bool:
        if self.folder is None:
            self.problem = ("No flight folder, so the fast network trace has "
                            "nowhere to write.")
            return False
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            fast = self.folder / f"network_fast_{self.flight_id}.csv"
            self._fast_fh = fast.open("w", newline="", encoding="utf-8")
            self._fast_writer = csv.writer(self._fast_fh)
            self._fast_writer.writerow(self.columns())

            pings = self.folder / f"network_pings_{self.flight_id}.csv"
            self._ping_fh = pings.open("w", newline="", encoding="utf-8")
            self._ping_writer = csv.writer(self._ping_fh)
            self._ping_writer.writerow(
                ["timestamp_utc", "elapsed_s", "sequence",
                 "rtt_ms", "status_code", "status"])
            self.fast_path = fast
            self.ping_path = pings
            return True
        except Exception as ex:
            self.problem = f"Could not open the network trace files: {ex}"
            self._close_files()
            return False

    def _close_files(self) -> None:
        for handle in (self._fast_fh, self._ping_fh):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass
        self._fast_fh = self._ping_fh = None
        self._fast_writer = self._ping_writer = None

    def _write_sidecars(self) -> None:
        """The events as text, and everything static about the run as JSON."""
        if self.folder is None:
            return
        try:
            lines = [
                f"Network trace — flight {self.flight_id}",
                f"Vehicle {self.host}",
                f"{self.ticks} interface ticks at {1 / self.period:.0f} Hz, "
                f"{self.echoes} echoes at {1 / self.echo_period:.0f} Hz",
                "",
            ]
            with self._lock:
                lines += [e.line() for e in self.events]
            path = self.folder / f"network_events_{self.flight_id}.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
        try:
            payload = netdiag.snapshot(self.host, probe=False)
            payload["flight_id"] = self.flight_id
            payload["sample_period_s"] = self.period
            payload["echo_period_s"] = self.echo_period
            payload["watched"] = [
                {"alias": w.alias, "index": w.index, "column_prefix": w.slug}
                for w in self._order]
            payload["events"] = [
                {"when": datetime.fromtimestamp(e.when, timezone.utc)
                    .isoformat(timespec="milliseconds"),
                 "what": e.what, "detail": e.detail}
                for e in self.events]
            payload["counter_granularity"] = self.granularity()
            path = self.folder / f"network_trace_{self.flight_id}.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                            encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  the interface thread
    # ------------------------------------------------------------------

    def _fast_loop(self) -> None:
        tick = time.monotonic()
        rescan_at = tick + RESCAN_S
        while not self._stop.is_set():
            tick += self.period
            try:
                if time.monotonic() >= rescan_at:
                    rescan_at = time.monotonic() + RESCAN_S
                    before = len(self._watch)
                    order = self._rescan()
                    if len(self._watch) != before:
                        # A new interface appeared. Its columns are not in the
                        # header already written, so it is noted rather than
                        # added: a header that changes halfway down a CSV is
                        # worse than a missing column.
                        new = [w.alias for w in order if w not in self._order]
                        self._note("interface appeared", ", ".join(new)
                                   + " — not in this file's columns")
                self._one_tick()
            except Exception:
                pass
            time.sleep(max(0.0, tick - time.monotonic()))

    def _one_tick(self) -> None:
        now = time.time()
        found = netdiag.counters(w.index for w in self._order)
        row = [datetime.fromtimestamp(now, timezone.utc)
               .isoformat(timespec="milliseconds"),
               round(now - self._started, 3)]
        for watch in self._order:
            iface = found.get(watch.index)
            if iface is None:
                row += [None] * len(PER_IFACE)
                self._transition(watch, "interface vanished", "", now)
                continue
            row += [iface.in_octets, iface.out_octets,
                    iface.in_ucast + iface.in_nucast, iface.out_ucast,
                    iface.in_errors, iface.in_discards,
                    iface.out_errors, iface.out_discards,
                    "" if iface.connected is None else int(iface.connected),
                    int(iface.low_power), iface.oper_status]
            self._compare(watch, iface, now)
        self.ticks += 1
        if self._fast_writer is not None:
            self._fast_writer.writerow(row)
            self._rows += 1
            if self._rows % FLUSH_EVERY == 0 and self._fast_fh is not None:
                self._fast_fh.flush()

    def _compare(self, watch: _Watch, iface, now: float) -> None:
        """Notice the things worth an event, and remember for the next tick."""
        if watch.at and iface.connected != watch.carrier:
            self._transition(
                watch, "carrier " + ("came back" if iface.connected else "DROPPED"),
                f"{netdiag.MEDIA_STATE.get(iface.media_state, '?')}", now)
        if watch.at and iface.low_power != watch.low_power:
            self._transition(
                watch, "low power " + ("entered" if iface.low_power else "left"),
                "", now)
        if watch.at and iface.oper_status != watch.oper:
            self._transition(
                watch, "operational state",
                f"{netdiag.OPER_STATUS.get(watch.oper, '?')} → "
                f"{netdiag.OPER_STATUS.get(iface.oper_status, '?')}", now)
        if watch.at:
            if iface.in_octets == watch.rx:
                watch.still += 1
                if watch.still == int(round(2.0 / self.period)):
                    self._transition(watch, "receive stopped",
                                     "no bytes in for two seconds", now)
            else:
                watch.moved += 1
                if watch.still >= int(round(2.0 / self.period)):
                    self._transition(
                        watch, "receive resumed",
                        f"after {watch.still * self.period:.1f} s", now)
                watch.still = 0
        watch.carrier = iface.connected
        watch.low_power = iface.low_power
        watch.oper = iface.oper_status
        watch.rx = iface.in_octets
        watch.at = now

    def _transition(self, watch: _Watch, what: str, detail: str,
                    now: float) -> None:
        self._note(f"{watch.alias}: {what}", detail, now)

    def _note(self, what: str, detail: str = "", now: float | None = None) -> None:
        with self._lock:
            self.events.append(Event(now or time.time(), what, detail))
            # A flight that spends an hour flapping must not fill memory with
            # its own commentary. The oldest go; the file already has them.
            if len(self.events) > 4000:
                del self.events[:1000]

    # ------------------------------------------------------------------
    #  the echo thread
    # ------------------------------------------------------------------

    def _echo_loop(self) -> None:
        echo = W.Echo(self.host)
        if not echo.open():
            self._note("echo unavailable",
                       "ICMP could not be opened; the ping file will be empty")
            return
        sequence = 0
        tick = time.monotonic()
        try:
            while not self._stop.is_set():
                tick += self.echo_period
                sequence += 1
                now = time.time()
                try:
                    rtt, status = echo.ping(ECHO_TIMEOUT_MS)
                except Exception:
                    rtt, status = None, 11050
                self.echoes += 1
                if rtt is None:
                    self.lost += 1
                self._echo_transition(rtt is not None, now)
                if self._ping_writer is not None:
                    try:
                        self._ping_writer.writerow([
                            datetime.fromtimestamp(now, timezone.utc)
                            .isoformat(timespec="milliseconds"),
                            round(now - self._started, 3), sequence,
                            "" if rtt is None else rtt, status,
                            W.ICMP_STATUS.get(status, str(status))])
                        self._ping_rows += 1
                        if (self._ping_rows % FLUSH_EVERY == 0
                                and self._ping_fh is not None):
                            self._ping_fh.flush()
                    except Exception:
                        pass
                time.sleep(max(0.0, tick - time.monotonic()))
        finally:
            echo.close()

    def _echo_transition(self, ok: bool, now: float) -> None:
        """Note the first loss and the recovery, not the ones in between.

        Two consecutive results are required before either is called, so a
        single dropped echo on a link doing 22 Mbps does not fill the events
        file with noise it will never be read for.
        """
        state = self._echo_state
        if state is None:
            self._echo_state = (ok, now)
            self.alive = ok
            return
        was, since = state
        if ok == was:
            return
        if ok:
            self._note("echo returned", f"after {now - since:.1f} s of silence",
                       now)
        else:
            self._note("ECHO LOST", "no reply to ICMP", now)
        self._echo_state = (ok, now)
        self.alive = ok

    # ------------------------------------------------------------------
    #  how fast the counters really move on this machine
    # ------------------------------------------------------------------

    def granularity(self) -> dict[str, float]:
        """Share of fast ticks on which each interface's receive counter moved.

        A number near 1 means the driver updates its statistics as fast as
        this samples, and 10 Hz is buying real resolution. A number near 0.1
        on a link known to be carrying video means the driver updates about
        once a second and the extra ticks are copies -- worth knowing before
        anyone reads a 100 ms figure off the file and believes it.
        """
        out = {}
        with self._lock:
            for watch in self._order:
                if self.ticks > 1:
                    out[watch.alias] = round(
                        min(1.0, watch.moved / (self.ticks - 1)), 3)
        return out


# --------------------------------------------------------------------------
#  the bench check
# --------------------------------------------------------------------------


def calibrate(host: str = "192.168.2.2", seconds: float = 4.0,
              period: float = FAST_PERIOD_S) -> dict:
    """Measure what a fast trace would actually resolve on this station.

    Run with the tether connected and video flowing. Reports, per interface,
    how often the receive counter moved and how much traffic it saw, plus the
    round trips over the same window. That answers "is ten hertz worth it
    here" with a measurement instead of an opinion, and it takes four seconds
    on the deck before a dive.
    """
    watch = netdiag.watched(host)
    if not watch:
        return {"problem": f"nothing on this machine routes to {host}"}
    indices = [w.index for w in watch]
    names = {w.index: w.alias for w in watch}
    moved = {i: 0 for i in indices}
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    previous: dict[int, int] = {}
    ticks = 0
    echo = W.Echo(host)
    have_echo = echo.open()
    rtts: list[float] = []
    losses = 0

    deadline = time.monotonic() + seconds
    tick = time.monotonic()
    try:
        while time.monotonic() < deadline:
            tick += period
            found = netdiag.counters(indices)
            for index in indices:
                iface = found.get(index)
                if iface is None:
                    continue
                first.setdefault(index, iface.in_octets)
                if index in previous and iface.in_octets != previous[index]:
                    moved[index] += 1
                previous[index] = last[index] = iface.in_octets
            ticks += 1
            if have_echo and ticks % max(1, int(0.2 / period)) == 0:
                rtt, _status = echo.ping(ECHO_TIMEOUT_MS)
                if rtt is None:
                    losses += 1
                else:
                    rtts.append(rtt)
            time.sleep(max(0.0, tick - time.monotonic()))
    finally:
        echo.close()

    report = {
        "host": host,
        "seconds": round(seconds, 1),
        "hz": round(1 / period, 1),
        "ticks": ticks,
        "interfaces": [],
    }
    for index in indices:
        bytes_seen = last.get(index, 0) - first.get(index, 0)
        report["interfaces"].append({
            "alias": names[index],
            "index": index,
            "counter_moved_share": round(moved[index] / max(1, ticks - 1), 3),
            "mbps": round(bytes_seen * 8 / max(seconds, 0.1) / 1e6, 2),
        })
    if have_echo:
        report["echoes"] = len(rtts) + losses
        report["echo_lost"] = losses
        report["rtt_ms_median"] = (
            round(sorted(rtts)[len(rtts) // 2], 1) if rtts else None)
        report["rtt_ms_max"] = round(max(rtts), 1) if rtts else None
    return report


def calibration_text(report: dict) -> str:
    """`calibrate()` as the paragraph a person reads on deck."""
    if report.get("problem"):
        return f"Network calibration: {report['problem']}"
    lines = [f"Network calibration — {report['seconds']} s at "
             f"{report['hz']:g} Hz against {report['host']}", ""]
    for iface in report["interfaces"]:
        share = iface["counter_moved_share"]
        if share >= 0.8:
            verdict = "counters keep up; fast sampling resolves real changes"
        elif share >= 0.3:
            verdict = "counters update more slowly than this samples"
        elif iface["mbps"] > 0.05:
            verdict = ("counters update about once a second — treat sub-second "
                       "detail on this adapter as interpolation")
        else:
            verdict = "no traffic on this adapter during the check"
        lines.append(f"  {iface['alias']}")
        lines.append(f"    {iface['mbps']:g} Mbps in, counter moved on "
                     f"{share * 100:.0f}% of ticks")
        lines.append(f"    {verdict}")
    if "echoes" in report:
        lines.append("")
        lines.append(f"  ICMP: {report['echoes']} sent, {report['echo_lost']} lost, "
                     f"median {report.get('rtt_ms_median')} ms, "
                     f"max {report.get('rtt_ms_max')} ms")
    return "\n".join(lines)
