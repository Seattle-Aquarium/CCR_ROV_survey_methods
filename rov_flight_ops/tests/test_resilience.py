"""
The responsiveness and resilience review of 14 September 2026, R1 to R7.

Each test holds one failure the review demonstrated -- or one its acceptance
criteria asked to be shown impossible -- against fakes: no vehicle, no real
hardware calls that can block, and nothing sent anywhere. Where a test is
about timing it waits on events rather than sleeping and hoping, and the
waits it does have are generous upper bounds, not the thing measured.

The GUI tests use the session window from conftest and are skipped without a
display, like the others.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from test_mcap_recovery import write_mcap

from rov_flight_ops import (
    blueos,
    diagnostics,
    flightlog,
    laptop,
    mcap_extract,
    nettrace,
)
from rov_flight_ops import wincounters as W

PROGRAM_ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
#  fakes for the recorder
# --------------------------------------------------------------------------


class FakeSampler:
    """Stands in for laptop.Sampler: rows on demand, and a record of stop().

    `hold` blocks sample() -- a WMI query or a counter read that does not come
    back -- until it is set.
    """

    made: list[FakeSampler] = []

    def __init__(self, *, rov_host="", flight_id=""):
        self.flight_id = flight_id
        self.vehicle = laptop.VehicleState()
        self.samples = 0
        self.stopped = threading.Event()
        self.sampling = threading.Event()
        self.hold: threading.Event | None = None
        FakeSampler.made.append(self)

    def start(self):
        pass

    def sample(self):
        self.samples += 1
        self.sampling.set()
        if self.hold is not None:
            self.hold.wait()
        row = dict.fromkeys(laptop.COLUMNS)
        row["flight_id"] = self.flight_id
        row["elapsed_time_s"] = float(self.samples)
        return row

    def stop(self, *, wait=True):
        self.stopped.set()


class NoTrace:
    def __init__(self, **kw):
        self.problem = "no trace in tests"

    def start(self):
        return False


@pytest.fixture
def fakes(monkeypatch):
    """A recorder world with nothing real behind it."""
    FakeSampler.made = []
    monkeypatch.setattr(laptop, "Sampler", FakeSampler)
    monkeypatch.setattr(nettrace, "Tracer", NoTrace)
    monkeypatch.setattr(W, "capabilities", lambda: {"test": "yes"})
    monkeypatch.setattr(flightlog.netdiag, "report", lambda host: "report")
    monkeypatch.setattr(flightlog.netdiag, "snapshot", lambda *a, **k: {})
    monkeypatch.setattr(flightlog.netdiag, "ndis_events", lambda *a, **k: [])
    monkeypatch.setattr(laptop, "find_rov_interface", lambda host: "")
    monkeypatch.setattr(blueos, "read_arm_state", lambda *a, **k: (None, None))
    monkeypatch.setattr(blueos, "read_temperature", lambda *a, **k: (None, None))
    monkeypatch.setattr(blueos, "read_interfaces", lambda *a, **k: {})
    monkeypatch.setattr(blueos, "read_tether", lambda *a, **k: {})
    monkeypatch.setattr(flightlog, "ARM_POLL_S", 0.02)
    monkeypatch.setattr(flightlog, "TETHER_POLL_S", 0.05)
    monkeypatch.setattr(flightlog, "WATCH_JOIN_S", 0.5)
    monkeypatch.setattr(flightlog.FlightRecorder, "_snapshot",
                        lambda self, since_log="": flightlog.Snapshot(taken=time.time()))
    return FakeSampler


def records(folder: Path) -> list[Path]:
    return sorted((folder / "logs").glob("flight_*.json"))


def csvs(folder: Path) -> list[Path]:
    return sorted((folder / "logs").glob("laptop_monitor_*.csv"))


# --------------------------------------------------------------------------
#  R1  start, stop and close never wait on the window's thread
# --------------------------------------------------------------------------


def test_a_request_returns_while_the_hardware_is_still_starting(tmp_path, fakes,
                                                               monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def slow_capabilities():
        entered.set()
        release.wait(10)
        return {"test": "slow"}

    monkeypatch.setattr(W, "capabilities", slow_capabilities)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    began = time.monotonic()
    req = rec.request("watch")
    assert time.monotonic() - began < 0.2, "the request waited on the hardware"
    assert entered.wait(5)
    assert rec.transitioning and rec.status.transition == "Starting monitoring"
    assert not req.is_set()
    release.set()
    assert req.wait(5) and req.result is True
    assert wait_for(lambda: not rec.transitioning)
    assert rec.status.transition == ""
    assert rec.stop_watching(finish=False)


def test_a_repeated_request_is_one_transition_and_one_recording(tmp_path, fakes,
                                                               monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(W, "capabilities", lambda: release.wait(10) or {})
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    first = rec.request("record")
    again = rec.request("record")
    third = rec.request("record")
    # The first may already be under way; the ones behind it are one request.
    assert again is third
    release.set()
    for r in (first, again):
        assert r.wait(10)
    assert rec.status.state == "recording"
    assert len(csvs(tmp_path)) == 1, "a second press started a second recording"
    stops = [rec.request("stop"), rec.request("stop"), rec.request("close")]
    for r in stops:
        assert r.wait(10)
    assert rec.status.state == "idle"
    assert len(records(tmp_path)) == 1, "the flight was finalised more than once"


def test_a_closing_snapshot_that_never_answers_still_closes_the_flight(
        tmp_path, fakes, monkeypatch):
    never = threading.Event()
    calls = []

    def snapshot(self, since_log=""):
        calls.append(since_log)
        if len(calls) > 1:                     # the closing one
            never.wait(30)
        return flightlog.Snapshot(taken=time.time(), parameters={"A": 1.0})

    monkeypatch.setattr(flightlog.FlightRecorder, "_snapshot", snapshot)
    monkeypatch.setattr(flightlog, "CLOSING_SNAPSHOT_S", 0.3)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    try:
        assert rec.start_manually()
        session = rec._session
        assert wait_for(lambda: session.rows >= 1)
        began = time.monotonic()
        assert rec.stop_manually(wait=True)
        assert time.monotonic() - began < 5, "closing waited on the vehicle"
        assert rec.status.state == "idle"
        assert session.fh is None, "the CSV was left open"
        record = records(tmp_path)[0].read_text(encoding="utf-8")
        assert "closing snapshot was abandoned" in record
        assert "abandoned" in rec.status.outcome
    finally:
        never.set()


# --------------------------------------------------------------------------
#  R2  one owner per generation, and nothing closed under a worker
# --------------------------------------------------------------------------


def test_a_stuck_sampler_keeps_its_stop_signal_and_its_file(tmp_path, fakes,
                                                           monkeypatch):
    monkeypatch.setattr(flightlog, "WORKER_JOIN_S", 0.2)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    assert rec.start_manually()
    old = rec._session
    stuck = FakeSampler.made[-1]
    assert wait_for(lambda: stuck.samples >= 1)
    stuck.hold = threading.Event()
    stuck.sampling.clear()
    assert stuck.sampling.wait(5), "the sampler did not reach the blocking read"

    assert rec.stop_manually(wait=True) is False, "a stuck worker was called stopped"
    assert old.stop.is_set()
    assert "laptop sampler" in rec.status.degraded
    assert old.fh is not None, "the CSV was closed beneath the worker writing it"
    assert not stuck.stopped.is_set(), "the counters were closed beneath a read"

    # A new flight does not touch the old one's stop signal or its file.
    assert rec.start_manually()
    new = rec._session
    assert new is not old and old.stop.is_set() and not new.stop.is_set()
    fresh = FakeSampler.made[-1]
    assert wait_for(lambda: fresh.samples >= 2)

    stuck.hold.set()                            # the read finally returns
    assert stuck.stopped.wait(5), "the old worker did not close what it owned"
    assert wait_for(lambda: old.fh is None)
    assert old.csv_path != new.csv_path
    assert rec.stop_manually(wait=True)
    with open(new.csv_path, encoding="utf-8") as fh:
        ids = {row["flight_id"] for row in csv.DictReader(fh)}
    assert ids == {new.flight_id}, "the old sampler wrote into the new flight"


def test_a_retired_watcher_cannot_start_a_flight(tmp_path, fakes, monkeypatch):
    first_call = threading.Event()
    release = threading.Event()
    seen = []

    def arm_state(host, *a, **k):
        seen.append(threading.current_thread())
        if len(seen) == 1:
            first_call.set()
            release.wait(10)
            return True, 5.0                    # the old watcher's late answer
        return False, 5.0

    monkeypatch.setattr(blueos, "read_arm_state", arm_state)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.start_watching()
    old_watch = rec._watch
    assert first_call.wait(5)
    rec.stop_watching(finish=False)             # its join times out
    assert old_watch.stop.is_set()
    rec.start_watching()
    assert rec._watch is not old_watch and not rec._watch.stop.is_set()
    assert old_watch.stop.is_set(), "starting again woke the retired watcher"
    release.set()
    for t in old_watch.threads:
        t.join(5)
    time.sleep(0.2)
    assert rec.status.state == "idle", "a retired watcher began a flight"
    assert not csvs(tmp_path)
    rec.stop_watching(finish=False)


def test_a_late_opening_snapshot_lands_only_in_its_own_flight(tmp_path, fakes,
                                                             monkeypatch):
    release = threading.Event()
    calls = []

    def snapshot(self, since_log=""):
        calls.append(since_log)
        if len(calls) == 1:
            release.wait(10)
            return flightlog.Snapshot(taken=1.0, parameters={"FIRST": 1.0})
        return flightlog.Snapshot(taken=2.0, parameters={"LATER": 2.0})

    monkeypatch.setattr(flightlog.FlightRecorder, "_snapshot", snapshot)
    monkeypatch.setattr(flightlog, "OPENING_SNAPSHOT_WAIT_S", 0.1)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    try:
        assert rec.start_manually()
        first = rec._session
        rec.stop_manually(wait=True)
        assert rec.start_manually()
        second = rec._session
        assert wait_for(lambda: second.threads["snap-start"].is_alive() is False)
        assert "LATER" in second.start_snap.parameters
        release.set()
        first.threads["snap-start"].join(5)
        assert "FIRST" in first.start_snap.parameters
        assert "FIRST" not in second.start_snap.parameters
        rec.stop_manually(wait=True)
    finally:
        release.set()


def test_disarm_stop_and_close_at_once_finalise_the_flight_once(tmp_path, fakes,
                                                               monkeypatch):
    written = []
    real = flightlog.FlightRecorder._write_files

    def counting(self, *a, **k):
        written.append(a)
        time.sleep(0.05)
        return real(self, *a, **k)

    monkeypatch.setattr(flightlog.FlightRecorder, "_write_files", counting)
    monkeypatch.setattr(flightlog, "DISARM_GRACE_S", 0.0)
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    watch = flightlog._Watch(99)
    rec._watch = watch
    assert rec.start_manually()
    session = rec._session
    session.manual = False                      # as though armed, not by hand
    session.disarm_at = time.time() - 100

    gate = threading.Barrier(3)

    def at_once(fn):
        gate.wait()
        fn()

    threads = [threading.Thread(target=at_once, args=(f,)) for f in (
        lambda: rec._on_disarmed(watch),
        lambda: rec.stop_manually(wait=True),
        lambda: rec.stop_watching(finish=True))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert session.done.wait(10)
    assert wait_for(lambda: rec.status.state == "idle")
    assert len(written) == 1, f"finalised {len(written)} times"
    assert len(records(tmp_path)) == 1


def test_lifecycle_cycles_leave_no_threads_behind(tmp_path, fakes):
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.request("watch").wait(10)
    rec.request("unwatch").wait(10)
    before = {t.ident for t in threading.enumerate()}
    for _ in range(5):
        assert rec.request("record").wait(10)
        assert rec._session is not None
        assert rec.request("close").wait(10)
        assert rec.status.state == "idle"
    assert wait_for(lambda: not [
        t for t in threading.enumerate()
        if t.ident not in before and t.name.startswith("utc-")
        and t.name != "utc-recorder-lifecycle"], timeout=10), \
        [t.name for t in threading.enumerate() if t.ident not in before]
    assert len(records(tmp_path)) == 5


@pytest.mark.skipif(W._ICMP is None, reason="ICMP is a Windows API")
def test_a_ping_still_in_flight_keeps_its_handle_until_it_returns(monkeypatch):
    pinger = W.Pinger("127.0.0.1", period=0.01)
    inside = threading.Event()
    release = threading.Event()

    def slow_ping():
        inside.set()
        release.wait(10)
        return 1.0

    monkeypatch.setattr(pinger, "_ping_once", slow_ping)
    pinger.start()
    assert pinger._handle is not None
    assert inside.wait(5)
    assert pinger.stop(wait=False) is False
    assert pinger._handle is not None, "the handle was closed under a ping"
    release.set()
    pinger._thread.join(5)
    assert pinger._handle is None, "the ping thread did not close its handle"


def test_counters_are_not_closed_beneath_a_collection(monkeypatch):
    inside = threading.Event()
    release = threading.Event()
    closed = []

    class FakePdh:
        def PdhCollectQueryData(self, q):             # noqa: N802
            inside.set()
            release.wait(10)

        def PdhCloseQuery(self, q):                   # noqa: N802
            closed.append(q)

    monkeypatch.setattr(W, "_PDH", FakePdh())
    c = W.Counters.__new__(W.Counters)
    c._query, c._scalars, c._arrays, c.missing, c._primed = "q", {}, {}, [], False
    c._lock = threading.Lock()
    collector = threading.Thread(target=c.collect)
    collector.start()
    assert inside.wait(5)
    closer = threading.Thread(target=c.close)
    closer.start()
    time.sleep(0.1)
    assert closed == [], "the query was closed during a collection"
    release.set()
    collector.join(5)
    closer.join(5)
    assert closed == ["q"]


def test_a_trace_loop_still_running_closes_its_own_file(tmp_path, monkeypatch):
    tracer = nettrace.Tracer(host="192.168.2.2", folder=tmp_path, flight_id="t")
    tracer._fast_fh = (tmp_path / "fast.csv").open("w", newline="")
    tracer._fast_writer = csv.writer(tracer._fast_fh)
    inside = threading.Event()
    release = threading.Event()

    def slow_tick():
        inside.set()
        release.wait(10)

    monkeypatch.setattr(tracer, "_one_tick", slow_tick)
    monkeypatch.setattr(tracer, "_write_sidecars", lambda: None)
    loop = threading.Thread(target=tracer._fast_loop, name="utc-nettrace")
    tracer._threads = [loop]
    loop.start()
    assert inside.wait(5)
    fh = tracer._fast_fh
    assert tracer.stop() is False
    assert not fh.closed, "the file was closed under the loop writing it"
    release.set()
    loop.join(5)
    assert fh.closed and tracer._fast_fh is None


# --------------------------------------------------------------------------
#  R5  a Stop reaches inside one extraction, and leaves nothing marked valid
# --------------------------------------------------------------------------


class StopAfter:
    """An Event that reads as set after `n` checks -- a Stop at a known point."""

    def __init__(self, n: int):
        self.n = n
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > self.n


def test_a_stop_inside_one_recording_stops_at_a_checkpoint(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    write_mcap(src, seconds=1500)               # 6,000 messages, one file
    stop = StopAfter(2)                         # entry, first file, then 2,000 in
    said = []
    with pytest.raises(mcap_extract.ExtractionCancelled):
        mcap_extract.extract([src], cache, cancel=stop,
                             progress=lambda f, m="": said.append(m))
    assert stop.checks == 3, "the stop was not noticed inside the recording"
    assert not (cache / "extract.json").exists(), "a partial cache was marked valid"
    # The lock was released and the next extraction is whole.
    res = mcap_extract.extract([src], cache)
    assert (cache / "extract.json").exists() and res.telemetry_rows > 0


def test_optional_analysis_failing_does_not_touch_the_recording(tmp_path, fakes):
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    assert rec.start_manually()
    session = rec._session
    junk = tmp_path / "recorder_20260831_165829.mcap"
    junk.write_bytes(b"not an mcap at all")
    with pytest.raises(ValueError):
        mcap_extract.extract([junk], tmp_path / "cache")
    assert rec.status.state == "recording" and rec._session is session
    assert wait_for(lambda: session.rows >= 2)
    rec.stop_manually(wait=True)


# --------------------------------------------------------------------------
#  R7  the cache lock is the operating system's, not a guess about time
# --------------------------------------------------------------------------


HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from rov_flight_ops import mcap_extract as mx
    with mx._CacheLock(Path(sys.argv[2])):
        print("locked", flush=True)
        while not Path(sys.argv[3]).exists():
            time.sleep(0.05)
    print("released", flush=True)
""")


def _hold_lock(cache: Path, done: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(PROGRAM_ROOT), str(cache), str(done)],
        stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_a_paused_owner_in_another_process_keeps_the_lock(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    done = tmp_path / "done"
    proc = _hold_lock(cache, done)
    try:
        # However old the file looks, a live owner still owns it.
        old = time.time() - 3600
        os.utime(cache / "extract.lock", (old, old))
        with pytest.raises(mcap_extract.ExtractionBusy):
            with mcap_extract._CacheLock(cache):
                pass
    finally:
        done.touch()
        proc.wait(10)
    with mcap_extract._CacheLock(cache):
        pass


def test_an_owner_that_is_killed_leaves_a_lock_the_next_can_take(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    proc = _hold_lock(cache, tmp_path / "never")
    proc.kill()
    proc.wait(10)
    # Windows releases a dead process's locks "depending on available system
    # resources" -- promptly, but not necessarily by the time wait() returns.
    def free() -> bool:
        try:
            with mcap_extract._CacheLock(cache):
                return (cache / "extract.lock").exists()
        except mcap_extract.ExtractionBusy:
            return False
    assert wait_for(free, timeout=10), "a killed owner's lock was never released"


def test_a_former_owner_cannot_release_the_next_owners_lock(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    first = mcap_extract._CacheLock(cache)
    with first:
        pass
    assert (cache / "extract.lock").exists(), "the lock file is never deleted"
    done = tmp_path / "done"
    proc = _hold_lock(cache, done)
    try:
        first.__exit__(None, None, None)        # a stray second release
        with pytest.raises(mcap_extract.ExtractionBusy):
            with mcap_extract._CacheLock(cache):
                pass
    finally:
        done.touch()
        proc.wait(10)


# --------------------------------------------------------------------------
#  R4  diagnostics that survive the failure they record
# --------------------------------------------------------------------------


def test_secrets_are_scrubbed_from_diagnostics():
    line = ("GET /api/raw/x?auth=abc123SECRET&y=1 X-Auth: tok456SECRET "
            "{'token': 'tok789SECRET'} eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sigSECRET")
    out = diagnostics.scrub(line)
    assert "SECRET" not in out, out
    assert "auth=" in out and "X-Auth" in out
    # A line of source in a traceback is not a secret.
    assert diagnostics.scrub("token = blueos.file_token(self.host)") == \
        "token = blueos.file_token(self.host)"


def test_the_watchdog_reports_one_stall_once_and_its_recovery(caplog):
    caplog.set_level(logging.WARNING, logger=diagnostics.PACKAGE)
    dog = diagnostics.Watchdog(hang_after=1.0)
    t0 = time.monotonic()
    dog._beat = t0
    assert dog.check(now=t0 + 0.5) == ""
    assert dog.check(now=t0 + 1.5) == "stalled"
    assert dog.check(now=t0 + 5.0) == "", "one stall was reported twice"
    stall = [r for r in caplog.records if "has not run for" in r.getMessage()]
    assert len(stall) == 1 and "--- thread MainThread" in stall[0].getMessage()
    dog.beat()
    assert dog.check() == "recovered"
    assert any("running again" in r.getMessage() for r in caplog.records)


def test_a_failing_log_handler_never_raises(tmp_path):
    handler = diagnostics._Handler(tmp_path / "app.log", maxBytes=1000,
                                   backupCount=2, encoding="utf-8")

    class Broken:
        def write(self, _):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

        def close(self):
            pass

    handler.stream = Broken()
    logger = logging.getLogger("test.resilience.broken")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("this must not raise")
    finally:
        logger.removeHandler(handler)
    assert handler.failures >= 1


def test_logs_rotate_to_a_bounded_set(tmp_path):
    handler = diagnostics._Handler(tmp_path / "app.log", maxBytes=2000,
                                   backupCount=2, encoding="utf-8")
    logger = logging.getLogger("test.resilience.rotate")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        for i in range(500):
            logger.warning("line %d %s", i, "x" * 60)
    finally:
        logger.removeHandler(handler)
        handler.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["app.log", "app.log.1", "app.log.2"], names


def test_diagnostics_never_reach_the_network():
    source = Path(diagnostics.__file__).read_text(encoding="utf-8")
    for word in ("urllib", "socket", "http.client", "requests", "smtplib"):
        assert f"import {word}" not in source


CRASHER = textwrap.dedent("""
    import faulthandler, logging, sys, threading, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from rov_flight_ops import diagnostics as D
    D.HANG_AFTER_S = 1.0
    D.LOG_BYTES = 4000
    D.LOG_BACKUPS = 2
    where = Path(sys.argv[2])
    if sys.argv[3] == "unwritable":
        got = D.setup(where / "app.log" / "nope")   # a file, not a folder
        D.log_exception("test", ValueError, ValueError("x"), None)
        D.beat()
        print("survived", got is None or got.is_dir(), flush=True)
        sys.exit(0)
    D.setup(where)
    D.note_activity("job", "Downloading 3 files auth=SECRETTOKEN")
    def boom():
        raise RuntimeError("worker exploded")
    t = threading.Thread(target=boom, name="utc-test-worker")
    t.start(); t.join()
    D.beat()
    time.sleep(2.6)           # the window stalls: no beats
    D.beat()
    time.sleep(1.5)           # and recovers
    log = logging.getLogger(D.PACKAGE + ".filler")
    for i in range(400):      # rotate the log several times over
        log.info("filler %d %s", i, "y" * 80)
    print("crashing", flush=True)
    faulthandler._sigsegv()
""")


def _run_crasher(tmp_path: Path, mode: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONFAULTHANDLER="")
    return subprocess.run(
        [sys.executable, "-c", CRASHER, str(PROGRAM_ROOT), str(tmp_path), mode],
        capture_output=True, text=True, timeout=60, env=env)


def test_a_worker_exception_a_stall_and_a_crash_all_leave_evidence(tmp_path):
    proc = _run_crasher(tmp_path, "crash")
    assert "crashing" in proc.stdout, proc.stderr
    assert proc.returncode != 0, "the process was meant to crash"
    logs = sorted(tmp_path.glob("app.log*"))
    assert 1 <= len(logs) <= 3, [p.name for p in logs]
    text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
    faults = (tmp_path / "faults.log").read_text(encoding="utf-8", errors="replace")
    # The crash went to the fault file, through every log rotation before it,
    # and not into a rotated log that reused its descriptor.
    assert "Fatal Python error" in faults and "Fatal Python error" not in text
    assert "pid" in faults
    # Rotation may have carried the early lines away; what is still here must
    # be scrubbed.
    assert "SECRETTOKEN" not in text


def test_early_evidence_is_written_before_rotation(tmp_path):
    """The same run without the filler: the thread death and the stall are
    both in app.log, with the stacks and what the program was doing."""
    script = CRASHER.replace("for i in range(400)", "for i in range(0)")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(PROGRAM_ROOT), str(tmp_path), "crash"],
        capture_output=True, text=True, timeout=60)
    assert "crashing" in proc.stdout, proc.stderr
    text = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(tmp_path.glob("app.log*")))
    assert "thread utc-test-worker died" in text
    assert "RuntimeError: worker exploded" in text
    assert "has not run for" in text and "--- thread MainThread" in text
    assert "activity: job=Downloading 3 files auth=<redacted>" in text
    assert "running again after a stall" in text
    assert "SECRETTOKEN" not in text


def test_an_unwritable_diagnostics_folder_is_survived(tmp_path):
    (tmp_path / "app.log").write_text("a file where a folder should be")
    proc = _run_crasher(tmp_path, "unwritable")
    assert proc.returncode == 0, proc.stderr
    assert "survived True" in proc.stdout


# --------------------------------------------------------------------------
#  R3 and R6  the window: job ownership, queue servicing, background reads
# --------------------------------------------------------------------------


def pump(app, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.005)


def pump_until(app, predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.update()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Ticker:
    """A Tk timer that counts its own runs: proof the event loop is turning."""

    def __init__(self, app, every_ms=20):
        self.app, self.every, self.count, self.on = app, every_ms, 0, True
        app.after(every_ms, self._tick)

    def _tick(self):
        if self.on:
            self.count += 1
            self.app.after(self.every, self._tick)

    def stop(self):
        self.on = False


@pytest.fixture
def quiet_dialogs(monkeypatch):
    said = []
    for mod in ("shell", "app", "monitorpage", "logspage", "transectpage"):
        for name in ("showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(f"rov_flight_ops.gui.{mod}.messagebox.{name}",
                                lambda *a, **k: said.append(a))
    return said


def wait_idle(app, timeout=30):
    assert pump_until(app, lambda: not app.busy and app._queue.empty(), timeout)
    pump(app, 0.1)


def test_a_result_handler_that_raises_does_not_stop_later_jobs(app, quiet_dialogs,
                                                              caplog):
    caplog.set_level(logging.ERROR, logger=diagnostics.PACKAGE)

    def broken(_res):
        raise RuntimeError("renderer broke")

    assert app.submit(lambda p, c: "A", "job A", on_done=broken)
    wait_idle(app)
    got = []
    assert app.submit(lambda p, c: (p(0.5, "halfway"), "B")[1], "job B",
                      on_done=got.append), "the failed handler left the job busy"
    wait_idle(app)
    assert got == ["B"]
    assert any("renderer broke" in r.getMessage() for r in caplog.records)


def test_a_finished_jobs_result_never_reaches_the_next_job(app, quiet_dialogs):
    got_a, got_b = [], []
    assert app.submit(lambda p, c: "result A", "job A", on_done=got_a.append)
    job = app._job
    job.thread.join(5)                          # finished; result still queued
    assert app.busy, "a job whose result is still queued counted as finished"
    assert app.submit(lambda p, c: "result B", "job B",
                      on_done=got_b.append) is False
    wait_idle(app)
    assert got_a == ["result A"] and got_b == []
    assert app.submit(lambda p, c: "result B", "job B", on_done=got_b.append)
    wait_idle(app)
    assert got_a == ["result A"] and got_b == ["result B"]


def test_a_flood_of_progress_keeps_the_result_and_the_timers(app, quiet_dialogs,
                                                            monkeypatch):
    from rov_flight_ops.gui import shell

    slices = []
    real = shell.Shell._drain_pass

    def timed(self):
        t0 = time.monotonic()
        real(self)
        slices.append(time.monotonic() - t0)

    monkeypatch.setattr(shell.Shell, "_drain_pass", timed)
    app.after(shell.DRAIN_EVERY_MS, app._drain)     # patched method takes effect
    got = []

    def flood(progress, cancel):
        for i in range(60_000):
            progress(i / 60_000, f"step {i}")
        return "finished"

    ticker = Ticker(app)
    assert app.submit(flood, "flood", on_done=got.append)
    wait_idle(app, timeout=60)
    ticker.stop()
    assert got == ["finished"], "the result was lost in the flood"
    assert ticker.count > 5, "timers did not run while progress flooded in"
    assert max(slices) < 0.5, f"one drain pass held the loop {max(slices):.2f} s"


def test_nothing_is_delivered_once_the_window_is_closing(app, quiet_dialogs):
    got = []
    assert app.submit(lambda p, c: "late", "late job", on_done=got.append)
    app._job.thread.join(5)
    app._closing = True
    try:
        app._drain()
        assert got == [], "a callback ran on a closing window"
    finally:
        app._closing = False
        app.after(10, app._drain)
    wait_idle(app)
    assert got == ["late"]


def test_the_output_pane_stays_bounded(app, monkeypatch):
    from rov_flight_ops.gui import shell

    monkeypatch.setattr(shell, "LOG_MAX_LINES", 120)
    monkeypatch.setattr(shell, "LOG_TRIM_LINES", 40)
    for i in range(1000):
        app._log(f"message {i}")
    lines = int(str(app.log.index("end-1c")).split(".")[0])
    assert lines <= 120
    assert app.log.get("1.0", "end-1c").endswith("message 999")


def test_a_tk_callback_exception_is_logged_with_its_traceback(app, caplog):
    caplog.set_level(logging.ERROR, logger=diagnostics.PACKAGE)

    def explode():
        raise ZeroDivisionError("callback blew up")

    app.after(1, explode)
    pump(app, 0.2)
    hit = [r for r in caplog.records if "window callback" in r.getMessage()]
    assert hit and "ZeroDivisionError: callback blew up" in hit[0].getMessage()
    assert "Traceback" in hit[0].getMessage()


def test_stop_and_start_from_the_page_never_block_the_window(app, quiet_dialogs,
                                                            tmp_path, fakes,
                                                            monkeypatch):
    release = threading.Event()
    calls = []

    def snapshot(self, since_log=""):
        calls.append(since_log)
        if len(calls) > 1:
            release.wait(15)
        return flightlog.Snapshot(taken=time.time())

    monkeypatch.setattr(flightlog.FlightRecorder, "_snapshot", snapshot)
    monkeypatch.setattr(W, "capabilities", lambda: release.wait(15) or {})
    monitor = app.pages["monitor"]
    before_rec, before_dir = app.recorder, app.flight_dir
    app.flight_dir = tmp_path
    app.recorder = rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    monkeypatch.setattr(monitor, "_host", lambda: "test")
    try:
        ticker = Ticker(app)
        began = time.monotonic()
        monitor._toggle_manual()                 # Record now: capabilities block
        assert time.monotonic() - began < 0.3
        assert monitor.manual_btn.cget("state") == "disabled"
        pump(app, 0.4)
        assert ticker.count >= 5 and rec.transitioning
        release.set()
        assert pump_until(app, lambda: rec.status.state == "recording"
                          and not rec.transitioning)
        release.clear()

        began = time.monotonic()
        monitor._toggle_manual()                 # Stop: the closing snapshot blocks
        assert time.monotonic() - began < 0.3
        before = ticker.count
        pump(app, 0.4)
        assert ticker.count >= before + 5, "the window froze while closing"
        assert rec.status.state == "closing"
        release.set()
        assert pump_until(app, lambda: rec.status.state == "idle"
                          and not rec.transitioning)
        assert len(records(tmp_path)) == 1
        ticker.stop()
    finally:
        release.set()
        rec.stop_watching(finish=False)
        app.recorder, app.flight_dir = before_rec, before_dir


def test_closing_the_window_waits_for_the_recorder_without_freezing(
        app, quiet_dialogs, tmp_path, fakes, monkeypatch):
    release = threading.Event()
    calls = []

    def snapshot(self, since_log=""):
        calls.append(since_log)
        if len(calls) > 1:
            release.wait(15)
        return flightlog.Snapshot(taken=time.time())

    monkeypatch.setattr(flightlog.FlightRecorder, "_snapshot", snapshot)
    monkeypatch.setattr("rov_flight_ops.gui.app.messagebox.askyesno",
                        lambda *a, **k: True)
    closed = []
    monkeypatch.setattr(app, "close_now", lambda: closed.append(time.monotonic()))
    before_rec = app.recorder
    app.recorder = rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    try:
        assert rec.start_manually()
        began = time.monotonic()
        assert app.before_close() is False       # the window stays up for now
        assert time.monotonic() - began < 0.3
        ticker = Ticker(app)
        pump(app, 0.4)
        assert ticker.count >= 5 and not closed
        release.set()
        assert pump_until(app, lambda: bool(closed))
        assert rec.status.state == "idle" and len(records(tmp_path)) == 1
        ticker.stop()
    finally:
        release.set()
        app._close_request = None
        app.recorder = before_rec


def test_a_slow_folder_scan_does_not_block_and_a_stale_one_is_dropped(
        app, quiet_dialogs, tmp_path, monkeypatch):
    from rov_flight_ops import discovery

    a, b = tmp_path / "2026_09_13_Alki", tmp_path / "2026_09_14_Centennial"
    a.mkdir()
    b.mkdir()
    release = threading.Event()
    real = discovery.discover

    def slow(root):
        if Path(root) == a:
            release.wait(15)
        return real(root)

    monkeypatch.setattr("rov_flight_ops.gui.app.discovery.discover", slow)
    monkeypatch.setattr(app, "_arm_monitor", lambda: None)
    monkeypatch.setattr("rov_flight_ops.gui.app.messagebox.askyesno",
                        lambda *a, **k: True)
    before = app.flight_dir
    try:
        ticker = Ticker(app)
        began = time.monotonic()
        app.use_flight(a)
        assert time.monotonic() - began < 1.0, "choosing a folder waited on the scan"
        pump(app, 0.3)
        assert ticker.count >= 5 and app.discovery is None
        app.use_flight(b)                         # moved on before a finished
        assert pump_until(app, lambda: app.discovery is not None)
        assert Path(app.discovery.root) == b.resolve()
        release.set()
        pump(app, 0.5)
        assert Path(app.discovery.root) == b.resolve(), \
            "the old folder's scan replaced the new"
        ticker.stop()
    finally:
        release.set()
        app.flight_dir = before
