"""
A flight recorder that records a simulated flight, and writes nothing.

It has the face of `flightlog.FlightRecorder` -- `status`, `history`,
`watching`, `request`, `retarget` -- which is all the Monitoring tab and the
banner's lamps ever read, so they show a simulated flight exactly as they
would show a real one: Recording, rows counting up, the Logging lamp lit.
What it does not have is anything that reaches a vehicle or a disk. Its rows
come from `sim.laptop`; nothing is written anywhere, and the Monitoring tab
says so.

The ten minutes before the simulator was switched on are made up front, so the
charts open full.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .. import flightlog
from .. import laptop as L
from .laptop import LaptopModel
from .world import SimWorld

log = logging.getLogger(__name__)

#: What the vehicle is called wherever the recorder's host is shown.
HOST = "simulated ROV"


def flight_id(world: SimWorld) -> str:
    return "SIM_" + datetime.fromtimestamp(world.t0_wall).strftime("%Y-%m-%d_%H%M%S")


class SimRecorder:
    """`FlightRecorder`'s face, over a simulated flight."""

    def __init__(self, world: SimWorld, *, flight_dir=None) -> None:
        self.world = world
        self.host = HOST
        self.flight_dir = flight_dir
        self.dvl_host = ""
        self.on_change = None
        self.capabilities: dict[str, str] = {}
        self.history = L.History(minutes=30)
        self.status = flightlog.Status(host=HOST)
        self.model = LaptopModel(world, flight_id=flight_id(world))
        self._watching = True
        self._recording = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: (wall time, depth m, altitude m) once a second, for the Transects
        #: tab's dive profile.
        self.trace: list[tuple[float, float, float]] = []
        self._t_next = 0.0

    # -- life ------------------------------------------------------------------

    def start(self) -> SimRecorder:
        now = self.world.t()
        st = self.status
        st.flight_id = self.model.flight_id
        st.started = self.world.wall(0.0)
        st.state = "recording"
        st.note = "simulated — nothing is written"
        # The flight so far, so the charts open on ten minutes of it.
        t = 0.0
        while t < now - 0.5:
            self._add(t)
            t += 1.0
        self._t_next = t
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sim-recorder")
        self._thread.start()
        return self

    def shutdown(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self.world.t()
            while self._t_next <= now:
                self._add(self._t_next)
                self._t_next += 1.0
            self._stop.wait(max(0.05, self._t_next - self.world.t()))

    def _add(self, t: float) -> None:
        try:
            row = self.model.row(t)
        except Exception:
            log.exception("simulated laptop row at t=%.1f", t)
            return
        v = self.world.vehicle(t)
        self.trace.append((self.world.wall(t), v.depth, v.altitude))
        if len(self.trace) > 4 * 3600:
            del self.trace[:3600]
        st = self.status
        reachable = row.get("rov_reachable") == "TRUE"
        if reachable:
            st.last_seen = self.world.wall(t)
            ms = row.get("rov_http_ms")
            st.link_ms = ms if isinstance(ms, (int, float)) else st.link_ms
            st.armed = row.get("rov_armed") == "TRUE"
        # Known only once the vehicle has said so: while the Pi is silent the
        # recorder still believes the last arm state it read.
        _tau, since = self.world.mission_time(t)
        st.since_disarm = since if row.get("rov_armed") == "FALSE" else None
        if not self._watching or not self._recording:
            return
        self.history.add(row)
        st.rows += 1
        st.last_write = self.world.wall(t)
        st.last_sync = self.world.wall(t)

    # -- the face the window reads ---------------------------------------------

    @property
    def watching(self) -> bool:
        return self._watching

    @property
    def transitioning(self) -> bool:
        return False

    def request(self, action: str) -> flightlog.Request:
        if action not in flightlog.TRANSITIONS:
            raise ValueError(f"unknown recorder request {action!r}")
        req = flightlog.Request(action)
        req.started = True
        st = self.status
        if action == "watch":
            self._watching = True
            if self._recording:
                st.state = "recording"
            req.result = True
        elif action in ("unwatch", "close"):
            self._watching = False
            st.state = "idle"
            st.outcome = "simulated flight — nothing was written"
            req.result = True
        elif action == "record":
            self._watching = self._recording = True
            st.state, st.by_hand = "recording", True
            req.result = True
        elif action == "stop":
            self._recording = False
            st.state, st.by_hand = "idle", False
            st.outcome = "simulated flight stopped by hand — nothing was written"
            req.result = True
        req.set()
        return req

    def retarget(self, host: str) -> bool:
        return True

    def start_manually(self) -> bool:
        self.request("record")
        return True

    def stop_manually(self, *, wait: bool = False) -> bool:
        self.request("stop")
        return True

    def stop_watching(self, **_kw) -> bool:
        self.request("unwatch")
        return True

    def depth_trace(self, since: float | None = None):
        return [x for x in self.trace if since is None or x[0] >= since]


def wait_for_rows(rec: SimRecorder, n: int, timeout: float = 5.0) -> bool:
    """For tests: True once the recorder has at least `n` rows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rec.status.rows >= n:
            return True
        time.sleep(0.02)
    return False
