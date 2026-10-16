"""
The application's handle on DVL captures: start one, move it, stop it.

A flight folder being chosen starts a capture into its ``logs/dvl``; choosing
another closes that one and starts the next in the new folder; closing the
program closes the last. Opening the DVL tab with no folder chosen starts a
live view, which reads everything and writes nothing.

Starting and stopping happen on one lifecycle thread, in the order asked, so
the window never waits on them: stopping a capture joins five threads, and
one of them may be waiting out a timeout against a vehicle that has gone.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .capture import DEFAULT_SNAPSHOT_HZ, DvlCapture
from .live import LiveState

log = logging.getLogger(__name__)

#: How long stopping a capture may take before it is reported as stuck.
STOP_TIMEOUT_S = 8.0


def dvl_folder(flight_dir: Path | None) -> Path | None:
    """Where a flight's DVL captures go."""
    return Path(flight_dir) / "logs" / "dvl" if flight_dir else None


class DvlRecorder:
    """One capture at a time, owned by the application rather than a tab."""

    def __init__(self, *, vehicle_host: str = "192.168.2.2", dvl_host: str = "",
                 snapshot_hz: float = DEFAULT_SNAPSHOT_HZ,
                 flight_id: Callable[[], str] | None = None,
                 vehicle: bool = True) -> None:
        self.vehicle_host = vehicle_host or "192.168.2.2"
        self.dvl_host = dvl_host or ""
        self.snapshot_hz = float(snapshot_hz)
        self.flight_id = flight_id or (lambda: "")
        self.vehicle = vehicle
        self.live = LiveState()
        self.capture: DvlCapture | None = None
        #: "Starting the DVL capture" and the like, while a request runs.
        self.transition = ""
        #: Captures that did not stop when asked, by id.
        self.stuck: list[str] = []
        self._ops: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    #  requests
    # ------------------------------------------------------------------

    def use_folder(self, folder: Path | None) -> threading.Event:
        """Capture into `folder` (a flight's logs/dvl), or a live view for None."""
        return self._submit("Starting the DVL capture",
                            lambda: self._restart(Path(folder) if folder else None))

    def ensure_running(self, folder: Path | None) -> threading.Event | None:
        """Start a capture if none is running; leave a running one alone."""
        cap = self.capture
        if cap is not None and cap.running:
            return None
        return self.use_folder(folder)

    def stop(self) -> threading.Event:
        return self._submit("Stopping the DVL capture", self._stop_current)

    def shutdown(self, timeout: float = STOP_TIMEOUT_S + 2.0) -> bool:
        """Stop for good, waiting up to `timeout`. Called as the program closes."""
        self._closed = True
        done = self._submit("Stopping the DVL capture", self._stop_current,
                            force=True)
        finished = done.wait(timeout)
        return finished and not self.stuck

    def _submit(self, label: str, work, force: bool = False) -> threading.Event:
        done = threading.Event()
        if self._closed and not force:
            done.set()
            return done
        with self._lock:
            self._ops.put((label, work, done))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="dvl-lifecycle")
                self._thread.start()
        return done

    def _run(self) -> None:
        while True:
            try:
                label, work, done = self._ops.get(timeout=30.0)
            except queue.Empty:
                with self._lock:
                    if self._ops.empty():
                        self._thread = None
                        return
                continue
            self.transition = label
            try:
                work()
            except Exception:
                from .. import diagnostics
                diagnostics.log_exception(f"DVL {label.lower()}", *sys.exc_info())
            finally:
                self.transition = ""
                done.set()

    # ------------------------------------------------------------------
    #  the work, on the lifecycle thread
    # ------------------------------------------------------------------

    def _restart(self, folder: Path | None) -> None:
        if self._closed:
            return
        cap = self.capture
        if cap is not None and cap.running and cap.folder == folder:
            return
        self._stop_current()
        cap = DvlCapture(folder=folder, vehicle_host=self.vehicle_host,
                         dvl_override=self.dvl_host, snapshot_hz=self.snapshot_hz,
                         flight_id=self.flight_id, live=self.live,
                         vehicle=self.vehicle)
        self.capture = cap
        cap.start()

    def _stop_current(self) -> None:
        cap = self.capture
        if cap is None or not cap.threads:
            return
        if not cap.stop_event.is_set() or any(t.is_alive() for t in cap.threads):
            if not cap.stop(timeout=STOP_TIMEOUT_S):
                self.stuck.append(cap.capture_id)

    # ------------------------------------------------------------------
    #  settings, applied to the running capture as well as the next
    # ------------------------------------------------------------------

    def set_dvl_host(self, text: str) -> None:
        self.dvl_host = (text or "").strip()
        cap = self.capture
        if cap is not None:
            cap.set_override(self.dvl_host)

    def set_snapshot_hz(self, hz: float) -> None:
        self.snapshot_hz = float(hz)
        cap = self.capture
        if cap is not None:
            cap.set_snapshot_hz(self.snapshot_hz)

    def set_vehicle_host(self, host: str) -> None:
        self.vehicle_host = host or "192.168.2.2"
        cap = self.capture
        if cap is not None:
            cap.set_vehicle_host(self.vehicle_host)

    # ------------------------------------------------------------------
    #  for the window
    # ------------------------------------------------------------------

    @property
    def problem(self) -> str:
        cap = self.capture
        return cap.problem if cap is not None else ""

    @property
    def folder(self) -> Path | None:
        cap = self.capture
        return cap.folder if cap is not None else None

    @property
    def running(self) -> bool:
        cap = self.capture
        return cap is not None and cap.running
