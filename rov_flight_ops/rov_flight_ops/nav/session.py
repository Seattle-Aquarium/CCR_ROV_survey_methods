"""
The navigation session log: a manifest, and an append-only stream of events.

What this is for: answering, months later, "why did the track jump there", or
"was the origin set when that transect started", or "how much of that 14 Wh
was actually measured". Questions of that shape are only answerable if the
evidence was written down *as it happened*, with the source and the time it
arrived, and if nothing was quietly smoothed on the way in.

It sits beside the flight recorder's own files, in the same ``logs`` folder,
and reuses its conventions -- one timestamped set per flight, best-effort
writes, nothing that can fail a dive. It does **not** duplicate the recorder's
telemetry: the mcap on the vehicle already has every MAVLink message. What is
here is what the mcap cannot know -- what this program displayed, what the
operator chose, and what it could not establish.

**Two files, because they answer different questions.**

``nav_<flight>.json``   the manifest. Versions, sensor-source mapping, frames,
                        thresholds, the origin authority, the discovered
                        endpoints. Rewritten as facts are established.
``nav_<flight>.jsonl``  the events, one JSON object per line, appended and
                        flushed as they happen. Never rewritten.

JSONL for the stream is deliberate. It is append-only, so a laptop that loses
power mid-dive loses at most the last line rather than a corrupted file; it
needs no schema migration; and it can be read with a text editor on a boat.
Positions go in it at full rate while the *map* draws a decimated track --
the samples on disk are the record, and what the screen could fit is not.

**Events are written from the collector's thread.** Losing a redraw is
acceptable and losing evidence is not, so this does not go through the display
coalescing. It is bounded instead: a write failure is counted and surfaced
rather than raised, and the file is rotated if it grows past a sane size.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA

log = logging.getLogger(__name__)

#: Rotate an event file past this size. A whole survey day of 4 Hz positions
#: is a few tens of megabytes, so this is generous; the point is that a
#: runaway event never fills a field laptop's disk.
MAX_BYTES = 256 * 1024 * 1024

#: How often buffered lines are pushed to the disk. The same reasoning as the
#: flight recorder's: a laptop that loses power loses seconds, not the dive.
FLUSH_EVERY_S = 5.0

#: Events written more often than this per kind are thinned, so a fault that
#: repeats at 4 Hz cannot bury the file. Position samples are exempt: they are
#: the record.
UNTHINNED = {"rov_fix", "vessel_fix", "power"}
THIN_EVERY_S = 1.0


def _utc(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(),
                                  tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Manifest:
    """What was true about this session, recorded once and corrected as known.

    Everything in here exists because it was needed to interpret a past
    flight and was not written down at the time.
    """

    schema: str = SCHEMA
    session_id: str = ""
    flight_id: str = ""
    site: str = ""
    started: str = ""
    ended: str = ""
    mode: str = "live"
    app_version: str = ""
    profiles_version: str = ""
    host: str = ""
    #: What the vehicle reported: ArduSub, BlueOS, board, extensions.
    vehicle: dict = field(default_factory=dict)
    #: The endpoints actually discovered, and whether each was observed or
    #: assumed from a documented default.
    endpoints: dict = field(default_factory=dict)
    #: Which MAVLink message each displayed quantity came from, with its unit
    #: and frame. Written from the code's own source table, so it cannot drift
    #: from what was really read.
    sources: dict = field(default_factory=dict)
    #: The thresholds in force: gauge ranges, hysteresis, staleness.
    thresholds: dict = field(default_factory=dict)
    #: The origin mechanism, and what was confirmed.
    origin: dict = field(default_factory=dict)
    #: Laptop UTC against the vehicle's clock, so delayed telemetry can be
    #: investigated later.
    clock: dict = field(default_factory=dict)
    #: Anything the session could not establish. Deliberately part of the
    #: record: "unknown" is a finding.
    unknowns: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


class NavSession:
    """One flight's navigation record. Opened once, appended to throughout.

    Thread-safe by a single lock around the append, because the collector
    thread and the window both write to it -- the collector for measurements,
    the window for the operator's actions.
    """

    def __init__(self, logs_dir: Path, flight_id: str, *,
                 session_id: str = "", mode: str = "live") -> None:
        self.dir = Path(logs_dir)
        self.flight_id = flight_id
        self.session_id = session_id or f"{flight_id}-{int(time.time())}"
        self.manifest = Manifest(
            session_id=self.session_id, flight_id=flight_id, mode=mode,
            started=_utc())
        self._lock = threading.Lock()
        self._fh = None
        self._last_flush = 0.0
        self._last_by_kind: dict[str, float] = {}
        #: Surfaced on the page: a log that is not being written is a fact the
        #: operator has to know before they rely on it.
        self.write_failures = 0
        self.last_error = ""
        self.bytes_written = 0
        self._counts: dict[str, int] = {}

    # -- paths ------------------------------------------------------------

    @property
    def events_path(self) -> Path:
        return self.dir / f"nav_{self.flight_id}.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.dir / f"nav_{self.flight_id}.json"

    # -- lifecycle --------------------------------------------------------

    def open(self) -> bool:
        """Open the event stream. Returns whether writing is actually possible.

        A False here is not fatal to the dive and is not hidden either: the
        page shows that the navigation record is not being kept.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._rotate_if_large()
            self._fh = self.events_path.open("a", encoding="utf-8")
            self.event("session_open", {
                "session": self.session_id, "flight": self.flight_id,
                "mode": self.manifest.mode})
            self.save_manifest()
            return True
        except Exception as ex:
            self.write_failures += 1
            self.last_error = str(ex)
            log.warning("navigation session log could not be opened at %s: %s",
                        self.events_path, ex)
            return False

    def _rotate_if_large(self) -> None:
        try:
            p = self.events_path
            if p.is_file() and p.stat().st_size > MAX_BYTES:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                p.rename(p.with_name(f"{p.stem}.{stamp}{p.suffix}"))
                log.info("rotated a %s MiB navigation log",
                         MAX_BYTES // 2 ** 20)
        except Exception as ex:
            log.debug("navigation log rotation skipped: %s", ex)

    def close(self, why: str = "") -> None:
        self.manifest.ended = _utc()
        self.manifest.counts = dict(self._counts)
        self.event("session_close", {"why": why or "closed"})
        self.save_manifest()
        with self._lock:
            fh, self._fh = self._fh, None
            if fh is not None:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass

    # -- writing ----------------------------------------------------------

    def event(self, kind: str, data: dict | None = None, *,
              when: float | None = None) -> None:
        """Append one event. Never raises, never blocks on anything slow.

        Thinning applies to repeated diagnostics, not to measurements: a DVL
        dropping in and out at 4 Hz would otherwise write four lines a second
        saying the same thing, and the useful information is the first one and
        the last one.
        """
        now = time.time()
        if kind not in UNTHINNED:
            last = self._last_by_kind.get(kind)
            if last is not None and (now - last) < THIN_EVERY_S:
                self._counts[f"{kind}:thinned"] = \
                    self._counts.get(f"{kind}:thinned", 0) + 1
                return
            self._last_by_kind[kind] = now

        row = {"t": _utc(when or now), "mono": round(time.monotonic(), 4),
               "kind": kind}
        if data:
            row.update(data)
        line = json.dumps(row, default=_plain, separators=(",", ":"))

        with self._lock:
            fh = self._fh
            if fh is None:
                return
            try:
                fh.write(line + "\n")
                self.bytes_written += len(line) + 1
                self._counts[kind] = self._counts.get(kind, 0) + 1
                if (time.monotonic() - self._last_flush) > FLUSH_EVERY_S:
                    fh.flush()
                    self._last_flush = time.monotonic()
            except Exception as ex:
                self.write_failures += 1
                self.last_error = str(ex)
                # Logged once per failure, not per attempt: a full disk would
                # otherwise fill the diagnostics log too.
                if self.write_failures <= 3:
                    log.warning("navigation event could not be written: %s", ex)

    def save_manifest(self) -> bool:
        try:
            self.manifest.counts = dict(self._counts)
            tmp = self.manifest_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.manifest.to_json(), indent=1, default=_plain),
                encoding="utf-8")
            tmp.replace(self.manifest_path)
            return True
        except Exception as ex:
            self.write_failures += 1
            self.last_error = str(ex)
            log.warning("navigation manifest could not be saved: %s", ex)
            return False

    # -- what the page shows about the log itself --------------------------

    @property
    def healthy(self) -> bool:
        return self._fh is not None and self.write_failures == 0

    def status_line(self) -> str:
        if self._fh is None:
            return "Navigation log not open" + (
                f" — {self.last_error}" if self.last_error else "")
        if self.write_failures:
            return (f"Navigation log: {self.write_failures} write failure(s) "
                    f"— {self.last_error}")
        return (f"Navigation log: {sum(self._counts.values()):,} events, "
                f"{self.bytes_written / 1024:,.0f} KiB")


def _plain(o):
    """Anything json cannot take, as something it can."""
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "to_json"):
        return o.to_json()
    if hasattr(o, "value"):          # an Enum
        return o.value
    return str(o)


# --------------------------------------------------------------------------
#  Reading one back
# --------------------------------------------------------------------------


def read_events(path: Path, kinds: tuple[str, ...] = ()) -> list[dict]:
    """Every event in a session log, optionally filtered by kind.

    Tolerates a truncated last line, which is what a laptop losing power
    mid-write leaves behind -- the whole reason the format is one object per
    line.
    """
    out: list[dict] = []
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue          # a torn final line
                if kinds and row.get("kind") not in kinds:
                    continue
                out.append(row)
    except FileNotFoundError:
        return []
    except Exception as ex:
        log.warning("navigation log %s could not be read: %s", path, ex)
    return out


def track_from_events(rows: list[dict], kind: str = "rov_fix"
                      ) -> list[list[tuple[float, float]]]:
    """Positions out of a session log, split into segments.

    Segments are kept apart rather than joined: the whole reason a segment
    exists is that something discontinuous happened between them, and drawing
    a line across it would invent a movement that never occurred.
    """
    segments: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        if r.get("kind") != kind:
            continue
        lat, lon = r.get("lat"), r.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        segments.setdefault(int(r.get("seg", 0)), []).append((float(lat),
                                                              float(lon)))
    return [segments[k] for k in sorted(segments)]
