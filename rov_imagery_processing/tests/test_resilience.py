"""
The shared parts of the 14 September 2026 resilience review, for this program.

The job queue, the diagnostics log and the telemetry cache lock are the same
files in ROV Flight Operations and here, and each program carries its own
copies of their tests too, so neither passes by leaning on the other. The
recorder's tests (R1, R2 and R6) belong to Flight Operations alone.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_mcap_recovery import write_mcap

from rov_imagery_processing import diagnostics, mcap_extract

PROGRAM_ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
#  R5  a Stop reaches inside one extraction
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


# --------------------------------------------------------------------------
#  R7  the cache lock is the operating system's, not a guess about time
# --------------------------------------------------------------------------


HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from rov_imagery_processing import mcap_extract as mx
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
    from rov_imagery_processing import diagnostics as D
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
#  R3  the window: job ownership and queue servicing
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
    for mod in ("shell", "app"):
        for name in ("showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(f"rov_imagery_processing.gui.{mod}.messagebox.{name}",
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
    from rov_imagery_processing.gui import shell

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
    from rov_imagery_processing.gui import shell

    monkeypatch.setattr(shell, "LOG_MAX_LINES", 120)
    monkeypatch.setattr(shell, "LOG_TRIM_LINES", 40)
    for i in range(1000):
        app._log(f"message {i}")
    lines = int(str(app.log.index("end-1c")).split(".")[0])
    assert lines <= 120
    assert app.log.get("1.0", "end-1c").endswith("message 999")


def test_a_huge_job_result_does_not_freeze_the_output_pane(app, quiet_dialogs):
    """14 September 2026, after a real flight: the Flight summary job returned
    (report, sheet), and the pane was handed the report's repr -- one 4 MB
    line -- and froze the window for good while laying it out."""

    class Huge:
        def __repr__(self):
            return "DayReport(" + "x" * 4_000_000 + ")"

    got = []
    assert app.submit(lambda p, c: (Huge(), "sheet.pdf"), "huge result",
                      on_done=got.append)
    began = time.monotonic()
    wait_idle(app)
    assert time.monotonic() - began < 10, "the pane stalled on a huge result"
    assert got and isinstance(got[0][0], Huge), "the page still gets the result"
    text = app.log.get("1.0", "end-1c")
    assert "xxxxx" * 100 not in text and text.endswith("sheet.pdf")


def test_one_enormous_line_is_cut_before_the_pane_sees_it(app):
    began = time.monotonic()
    app._log("y" * 3_000_000)
    assert time.monotonic() - began < 5
    last = app.log.get("end-1c linestart", "end-1c")
    assert len(last) < 3000 and "more characters" in last


def test_a_tk_callback_exception_is_logged_with_its_traceback(app, caplog):
    caplog.set_level(logging.ERROR, logger=diagnostics.PACKAGE)

    def explode():
        raise ZeroDivisionError("callback blew up")

    app.after(1, explode)
    pump(app, 0.2)
    hit = [r for r in caplog.records if "window callback" in r.getMessage()]
    assert hit and "ZeroDivisionError: callback blew up" in hit[0].getMessage()
    assert "Traceback" in hit[0].getMessage()
