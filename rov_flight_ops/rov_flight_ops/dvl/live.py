"""
What the DVL tab draws: the latest of everything, and a short history.

Written by the capture's threads and read by the window, so every access goes
through one lock and the window only ever gets copies. Nothing here is
written to disk -- the files are the record; this is the view of it.

The history is sized for the longest chart window, thirty minutes at the
A50's fastest rate.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

#: Thirty minutes at fifteen reports a second.
HISTORY_LEN = 30 * 60 * 15
#: Rates are counted over this many seconds.
RATE_WINDOW_S = 10.0
#: The events the tab keeps for display; the file keeps them all.
EVENTS_KEPT = 300


@dataclass
class ChartPoint:
    """One velocity report, reduced to what the charts draw."""

    t: float                       # this laptop's wall clock, seconds
    altitude: float | None
    distance: tuple
    rssi: tuple
    nsd: tuple
    valid: bool | None
    fom: float | None
    d_tov_ms: float | None
    d_rx_ms: float | None


@dataclass
class Snapshot:
    """A copy of the live state, safe to read on the window's thread."""

    taken: float = 0.0
    address: str = ""
    address_source: str = ""
    capturing: bool = False
    folder: str = ""
    capture_id: str = ""
    tcp_connected: bool = False
    tcp_since: float = 0.0
    tcp_connections: int = 0
    tcp_error: str = ""
    tcp_port: int = 0
    ws_connected: bool = False
    ws_error: str = ""
    web_error: str = ""
    last_rx_age_s: float | None = None
    bytes_in: int = 0
    lines_in: int = 0
    kinds: dict = field(default_factory=dict)
    velocity: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)
    ws_velocity: dict = field(default_factory=dict)
    motion: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    echo: dict = field(default_factory=dict)
    spectrum: dict = field(default_factory=dict)
    mavlink: dict = field(default_factory=dict)
    rates: dict = field(default_factory=dict)
    cadence: dict = field(default_factory=dict)
    median_ms: float | None = None
    gaps: list = field(default_factory=list)
    events: list = field(default_factory=list)
    history: list = field(default_factory=list)
    temperature: list = field(default_factory=list)
    files: dict = field(default_factory=dict)
    problem: str = ""
    notes: list = field(default_factory=list)
    snapshot_hz: float = 0.0


class LiveState:
    """Shared between a capture's threads and the window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._s = Snapshot()
        self._rx: dict[str, deque] = {}
        self._history: deque[ChartPoint] = deque(maxlen=HISTORY_LEN)
        self._temps: deque[tuple[float, float]] = deque(maxlen=4000)
        self._events: deque[tuple[float, str]] = deque(maxlen=EVENTS_KEPT)
        self._gaps: deque = deque(maxlen=200)
        self._last_rx_mono = 0.0
        self.version = 0

    # -- writers ---------------------------------------------------------

    def update(self, **values) -> None:
        with self._lock:
            for k, v in values.items():
                setattr(self._s, k, v)
            self.version += 1

    def merge(self, name: str, values: dict) -> None:
        """Merge into one of the dict fields (status, mavlink, …)."""
        with self._lock:
            getattr(self._s, name).update(values)
            self.version += 1

    def count(self, kind: str, nbytes: int = 0) -> None:
        """A message of `kind` arrived now; counted for its rate."""
        now = time.monotonic()
        with self._lock:
            q = self._rx.setdefault(kind, deque())
            q.append(now)
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            self._s.kinds[kind] = self._s.kinds.get(kind, 0) + 1
            if nbytes:
                self._s.bytes_in += nbytes
            self._last_rx_mono = now

    def lines(self, n: int) -> None:
        with self._lock:
            self._s.lines_in += n

    def add_point(self, point: ChartPoint) -> None:
        with self._lock:
            self._history.append(point)
            self.version += 1

    def add_temperature(self, t: float, celsius: float) -> None:
        with self._lock:
            self._temps.append((t, celsius))

    def add_event(self, when: float, text: str) -> None:
        with self._lock:
            self._events.append((when, text))
            self.version += 1

    def add_gap(self, gap) -> None:
        with self._lock:
            self._gaps.append(gap)

    def reset_streams(self) -> None:
        """A new capture: rates and histories start again."""
        with self._lock:
            self._rx.clear()
            self._history.clear()
            self._temps.clear()
            self._gaps.clear()
            s = self._s
            s.kinds, s.bytes_in, s.lines_in = {}, 0, 0
            s.velocity, s.position, s.ws_velocity = {}, {}, {}
            s.motion, s.status, s.echo, s.spectrum, s.mavlink = {}, {}, {}, {}, {}
            s.cadence, s.median_ms, s.commands = {}, None, {}
            self.version += 1

    # -- the reader -------------------------------------------------------

    def rate(self, kind: str) -> float:
        now = time.monotonic()
        with self._lock:
            q = self._rx.get(kind)
            if not q:
                return 0.0
            n = sum(1 for t in q if now - t <= RATE_WINDOW_S)
        return n / RATE_WINDOW_S

    def snapshot(self, *, history_since: float | None = None,
                 history: bool = True) -> Snapshot:
        """A copy. `history_since` limits the history to a chart window;
        `history=False` leaves it out, for readers that only want the latest."""
        now = time.monotonic()
        with self._lock:
            s = self._s
            out = Snapshot(**{k: getattr(s, k) for k in s.__dataclass_fields__})
            for name in ("kinds", "velocity", "position", "ws_velocity", "motion",
                         "status", "commands", "echo", "spectrum", "mavlink",
                         "cadence", "files"):
                setattr(out, name, dict(getattr(s, name)))
            out.notes = list(s.notes)
            out.rates = {k: sum(1 for t in q if now - t <= RATE_WINDOW_S)
                         / RATE_WINDOW_S for k, q in self._rx.items()}
            out.last_rx_age_s = (now - self._last_rx_mono
                                 if self._last_rx_mono else None)
            if not history:
                out.history = []
            elif history_since is None:
                out.history = list(self._history)
            else:
                # Newest last, so walk back from the end and stop at the
                # window's edge rather than filtering half an hour of points.
                recent = []
                for p in reversed(self._history):
                    if p.t < history_since:
                        break
                    recent.append(p)
                recent.reverse()
                out.history = recent
            out.temperature = list(self._temps) if history else []
            out.events = list(self._events)
            out.gaps = list(self._gaps)
            out.taken = time.time()
        return out
