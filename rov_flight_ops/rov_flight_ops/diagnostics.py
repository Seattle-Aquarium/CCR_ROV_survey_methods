"""
Diagnostics: a record of what went wrong, kept where it can be found afterwards.

The program runs under ``pythonw``, which has no console. Before this module,
an exception in a button's callback, a worker thread that died, or a window
that stopped answering left nothing behind -- Tk printed the traceback to a
stderr that does not exist. This keeps that evidence, bounded, on the laptop's
own disk.

What is written, into ``%LOCALAPPDATA%\\CCR_ROV\\<program>\\diagnostics``:

===================  =========================================================
``app.log``          the program's own log: start-up facts, jobs started and
                     finished, recorder transitions, and every unexpected
                     exception with its traceback. Rotated at 1 MB, five kept.
``faults.log``       Python's `faulthandler` output: the stacks of every thread
                     if the interpreter itself crashes. Rotated only at
                     start-up, so the file it writes to is never closed under
                     it.
===================  =========================================================

It is never on the flight drive. A drive that fills up or is pulled out is one
of the failures this is meant to record, and diagnostics there would fail with
it.

**A stalled window is recorded, not just a crashed one.** The window beats a
heartbeat once a second through Tk's own event loop; a watchdog thread notices
when the beats stop, and writes every thread's stack to ``app.log`` once per
stall -- with what the program said it was doing -- then notes how long the
stall lasted when the beats come back. The watchdog never touches Tk.

**Limits.** `faulthandler` catches a crash inside the interpreter (an access
violation in native code, say) but not a power cut, a killed process, or
Windows ending a program that is "not responding". A watchdog written in
Python cannot report a stall that also stops Python threads from running.
Neither sends anything anywhere: everything stays in the folder above.

Nothing here may stop the program. A folder that cannot be written falls back
to the temporary directory, and then to nothing, and says so in the log it
could open, or not at all.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import re
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

#: The program this copy belongs to -- the module is duplicated into each.
PACKAGE = __name__.split(".")[0]

LOG_NAME = "app.log"
FAULT_NAME = "faults.log"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5
FAULT_BYTES = 1_000_000
#: Per-process fallback logs (written when another copy of the program holds
#: app.log) are deleted after this many days.
KEEP_DAYS = 14

#: How often the window beats, and how long without a beat counts as a stall.
#: Eight seconds is long enough that a slow redraw is not reported and short
#: enough that Windows' own "not responding" (five seconds without input being
#: read) is caught while it is still happening.
HEARTBEAT_S = 1.0
HANG_AFTER_S = 8.0
#: At most this many stall reports per run; a laptop that stalls every minute
#: needs the first few, not a full disk.
MAX_HANG_REPORTS = 20
#: Frames kept per thread in a stall report.
STACK_FRAMES = 40

#: The same exception from the same place is logged in full this many times
#: in any ten minutes; after that it is counted, and the count is logged.
REPEAT_LIMIT = 5
REPEAT_WINDOW_S = 600.0

log = logging.getLogger(PACKAGE)

_lock = threading.RLock()
_folder: Path | None = None
_problem = ""
_fault_fh = None
_watchdog: Watchdog | None = None
_activity: dict[str, str] = {}
_repeats: dict[tuple, list] = {}


# --------------------------------------------------------------------------
#  keeping secrets out
# --------------------------------------------------------------------------

#: The vehicle's File Browser hands out a session token that travels as
#: ``?auth=`` and ``X-Auth:``; the rest are the usual suspects. Values are
#: replaced, names are kept, so a line still says what it was about. ``=``
#: counts only without spaces (a query string, not a line of source code in a
#: traceback); ``:`` with or without them (a header, a dict).
_SECRET_RE = re.compile(
    r"(?i)\b(auth|x-auth|token|access_token|authorization|password|passwd|"
    r"secret|api[_-]?key|cookie)(=|[\"']?\s*:\s*(?:bearer\s+)?[\"']?)"
    r"([^\s\"'&,;()]+)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")


def scrub(text: str) -> str:
    """The text with anything that looks like a credential replaced."""
    if not text:
        return text
    text = _JWT_RE.sub("<token>", text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


class _Scrubbed(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class _Handler(logging.handlers.RotatingFileHandler):
    """A rotating log that cannot raise into the program it is recording.

    Two copies of the program open at once share ``app.log``, and Windows
    will not rename a file another process has open. Rotation that fails
    moves this process's lines into a file of its own rather than letting
    the shared one grow without limit.
    """

    failures = 0

    def doRollover(self) -> None:          # noqa: N802 - logging's name
        try:
            super().doRollover()
        except OSError:
            try:
                if self.stream:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            base = Path(self.baseFilename)
            if f".{os.getpid()}." not in base.name:
                self.baseFilename = str(base.with_name(
                    f"{base.stem}.{os.getpid()}{base.suffix}"))
            try:
                self.stream = self._open()
            except Exception:
                self.failures += 1

    def handleError(self, record) -> None:  # noqa: N802 - logging's name
        # Never print (there is no console) and never raise: a full or
        # vanished disk must not turn logging into the thing that fails.
        self.failures += 1


# --------------------------------------------------------------------------
#  set-up
# --------------------------------------------------------------------------


def default_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "state")
    return Path(base) / "CCR_ROV" / PACKAGE / "diagnostics"


def folder() -> Path | None:
    """Where diagnostics are being written, or None if nowhere could be."""
    return _folder


def problem() -> str:
    """Why diagnostics are not where they should be, or ""."""
    return _problem


def setup(where: Path | None = None, *, watchdog: bool = True,
          hooks: bool = True) -> Path | None:
    """Start recording diagnostics. Safe to call twice; never raises."""
    global _folder, _problem
    with _lock:
        if _folder is not None:
            return _folder
        tried = [Path(where)] if where else [
            default_folder(),
            Path(tempfile.gettempdir()) / "CCR_ROV" / PACKAGE / "diagnostics"]
        for candidate in tried:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                handler = _Handler(candidate / LOG_NAME, maxBytes=LOG_BYTES,
                                   backupCount=LOG_BACKUPS, encoding="utf-8",
                                   delay=False)
            except Exception as ex:
                _problem = f"could not write diagnostics to {candidate}: {ex}"
                continue
            handler.setFormatter(_Scrubbed(
                "%(asctime)s.%(msecs)03d %(levelname)-7s %(threadName)s "
                "%(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
            _folder = candidate
            break
        if _folder is None:
            log.addHandler(logging.NullHandler())
            return None
        if _problem:
            log.warning("diagnostics fell back to %s (%s)", _folder, _problem)
        _prune(_folder)
        _start_faulthandler(_folder)
        log.info("---- %s starting ----", PACKAGE)
        for line in environment_lines():
            log.info("%s", line)
        if hooks:
            install_hooks()
        if watchdog:
            start_watchdog()
        return _folder


def _prune(where: Path) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in where.glob("app.*.log*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _start_faulthandler(where: Path) -> None:
    """Point faulthandler at a file that stays open for the whole run.

    faulthandler keeps the file *descriptor*, not the file object: if the
    object were closed -- by a rotation, say -- a later crash would be
    written into whatever the operating system had reused that number for.
    So this file is rotated only here, before it is opened, and the object is
    held in a module global until the process ends.
    """
    global _fault_fh
    if _fault_fh is not None:
        return
    path = where / FAULT_NAME
    try:
        if path.is_file() and path.stat().st_size > FAULT_BYTES:
            path.replace(where / (FAULT_NAME + ".1"))
    except OSError:
        pass
    try:
        _fault_fh = open(path, "a", encoding="utf-8")   # noqa: SIM115 - held open
        _fault_fh.write(f"\n---- {PACKAGE} pid {os.getpid()} started "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ----\n")
        _fault_fh.flush()
        faulthandler.enable(file=_fault_fh, all_threads=True)
    except Exception as ex:
        log.warning("faulthandler not enabled: %s", ex)


def environment_lines() -> list[str]:
    """Version, commit, interpreter and machine -- what a report needs first."""
    lines = [f"program {PACKAGE} {_version(PACKAGE)} commit {git_commit() or '?'}",
             f"python {sys.version.split()[0]} ({sys.executable})",
             f"platform {platform.platform()} machine {platform.machine()}",
             f"pid {os.getpid()}"]
    for dist in ("customtkinter", "psutil", "pywin32", "mcap"):
        v = _version(dist)
        if v != "?":
            lines.append(f"{dist} {v}")
    return lines


def _version(dist: str) -> str:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return "?"


def git_commit(start: Path | None = None) -> str:
    """The checked-out commit, read from .git without running git. "" if none."""
    here = Path(start or __file__).resolve()
    for parent in [here, *here.parents]:
        git = parent / ".git"
        if not git.is_dir():
            continue
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref:"):
                return head[:12]
            ref = head.split(None, 1)[1]
            path = git / ref
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()[:12]
            packed = git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split()[0][:12]
        except OSError:
            return ""
        return ""
    return ""


# --------------------------------------------------------------------------
#  exceptions
# --------------------------------------------------------------------------


def log_exception(where: str, exc_type, exc, tb, *, level=logging.ERROR) -> None:
    """Log an exception with its traceback, rate-limited by where it came from."""
    try:
        frames = traceback.extract_tb(tb) if tb is not None else []
        origin = (frames[-1].filename, frames[-1].lineno) if frames else ("", 0)
        key = (where, getattr(exc_type, "__name__", str(exc_type)), *origin)
        now = time.monotonic()
        with _lock:
            seen = _repeats.setdefault(key, [now, 0, 0])   # window start, logged, held
            if now - seen[0] > REPEAT_WINDOW_S:
                if seen[2]:
                    log.log(level, "%s: %d more of %s were not logged in full",
                            where, seen[2], key[1])
                seen[:] = [now, 0, 0]
            if seen[1] >= REPEAT_LIMIT:
                seen[2] += 1
                return
            seen[1] += 1
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.log(level, "%s: unexpected %s\n%s%s", where, key[1], text,
                activity_text())
    except Exception:
        pass


def install_hooks() -> None:
    """Uncaught exceptions on any thread go to the log, then to the old hook."""
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def on_sys(exc_type, exc, tb):
        log_exception("uncaught exception", exc_type, exc, tb, level=logging.CRITICAL)
        try:
            previous_sys(exc_type, exc, tb)
        except Exception:
            pass

    def on_thread(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log_exception(f"thread {name} died", args.exc_type, args.exc_value,
                      args.exc_traceback, level=logging.CRITICAL)
        try:
            previous_thread(args)
        except Exception:
            pass

    sys.excepthook = on_sys
    threading.excepthook = on_thread


# --------------------------------------------------------------------------
#  what the program says it is doing
# --------------------------------------------------------------------------


def note_activity(key: str, text: str | None) -> None:
    """Record (or with None, clear) what one part of the program is doing.

    Written into stall reports and exception reports, so a stack can be read
    against "Downloading 40 files" rather than guessed at.
    """
    with _lock:
        if text:
            _activity[key] = scrub(str(text))[:200]
        else:
            _activity.pop(key, None)


def activity_text() -> str:
    with _lock:
        if not _activity:
            return ""
        return "activity: " + "; ".join(f"{k}={v}" for k, v in sorted(_activity.items())) + "\n"


# --------------------------------------------------------------------------
#  stalls
# --------------------------------------------------------------------------


def thread_stacks(limit: int = STACK_FRAMES) -> str:
    """Every Python thread's current stack, innermost frames last."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        out.append(f"--- thread {names.get(ident, '?')} ({ident})")
        out.extend(line.rstrip("\n") for line in
                   traceback.format_list(traceback.extract_stack(frame, limit=limit)))
    return "\n".join(out)


class Watchdog:
    """Notices when the window's heartbeat stops, from a thread of its own."""

    def __init__(self, hang_after: float | None = None, poll: float = 1.0,
                 max_reports: int | None = None):
        self.hang_after = HANG_AFTER_S if hang_after is None else hang_after
        self.poll = poll
        self.max_reports = MAX_HANG_REPORTS if max_reports is None else max_reports
        self.reports = 0
        self.recoveries = 0
        #: None until the window beats for the first time: building the
        #: window takes several seconds on a field laptop, and that is start-up,
        #: not a stall.
        self._beat: float | None = None
        self._stalled_from: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Scheduling lateness of the heartbeat itself: the Tk loop's own
        #: measure of how long a callback waited for its turn.
        self.worst_late_s = 0.0

    def beat(self, late_s: float = 0.0) -> None:
        self._beat = time.monotonic()
        if late_s > self.worst_late_s:
            self.worst_late_s = late_s

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="diag-watchdog")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.check()
            except Exception:
                pass

    def check(self, now: float | None = None) -> str:
        """"stalled", "recovered" or "" -- and the report written for it."""
        now = time.monotonic() if now is None else now
        if self._beat is None:
            return ""
        silent = now - self._beat
        if self._stalled_from is None:
            if silent < self.hang_after:
                return ""
            self._stalled_from = self._beat
            if self.reports >= self.max_reports:
                return "stalled"
            self.reports += 1
            log.warning("the window has not run for %.1f s (stall %d). "
                        "Every thread's stack follows.\n%s%s",
                        silent, self.reports, activity_text(), thread_stacks())
            return "stalled"
        if self._beat > self._stalled_from:
            lasted = self._beat - self._stalled_from
            self._stalled_from = None
            self.recoveries += 1
            log.warning("the window is running again after a stall of about "
                        "%.1f s", lasted)
            return "recovered"
        return ""


def start_watchdog() -> Watchdog:
    global _watchdog
    with _lock:
        if _watchdog is None:
            _watchdog = Watchdog()
            _watchdog.start()
        return _watchdog


def beat(late_s: float = 0.0) -> None:
    """Called by the window's heartbeat. Nothing happens without a watchdog."""
    dog = _watchdog
    if dog is not None:
        dog.beat(late_s)


def open_folder() -> Path | None:
    """Open the diagnostics folder in Explorer; returns it, or None."""
    where = _folder or default_folder()
    try:
        where.mkdir(parents=True, exist_ok=True)
        os.startfile(where)                              # noqa: S606
    except Exception:
        pass
    return where
