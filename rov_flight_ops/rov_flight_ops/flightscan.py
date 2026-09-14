"""
Everything a flight left behind, gathered and put on one clock.

A survey afternoon writes into `logs/` from four independent recorders that do
not know about each other: the vehicle's own `.mcap` (one file per armed
period), the topside 1 Hz row, the 10 Hz network trace, and the parameter and
version snapshots taken at the flight's edges. Each is written in its own
units, on its own cadence, named by its own idea of the flight.

Reading them together by hand is the work this module removes. It does three
things and nothing else:

  **Find.** Group the folder's files by the flight they belong to, tolerating
  the fact that the naming has changed once already and will change again.

  **Reduce.** Turn each file into the smallest summary that still answers a
  question -- an `.mcap` into its armed span and its autopilot messages, a
  1 Hz CSV into its runs of good and bad link, a ping log into its outages.
  A 4.2 GB recording becomes about two kilobytes.

  **Align.** Put every one of those on UTC seconds, so the disarm the vehicle
  recorded and the blackout the laptop recorded can be compared rather than
  guessed at.

**No interpretation happens here.** Nothing in this module decides that
anything went wrong; it reports spans, counts and messages. What those mean is
`flightreport`'s job, and keeping the two apart means a disagreement about
interpretation never requires re-reading five gigabytes.

Two properties are deliberate. It never modifies a file -- a recording that
took a day to make must not be at risk from a program that only wants to look
at it. And every reader is individually optional: a folder with no network
trace, or an `.mcap` the vehicle never closed, yields a report with that part
missing rather than an exception.
"""

from __future__ import annotations

import csv
import json
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcap.reader import make_reader
except ImportError:                                   # pragma: no cover
    make_reader = None                                # type: ignore

#: `laptop_monitor_2026-09-11_1314.csv` -> `2026-09-11_1314`. The stamp is
#: local time, because it is what the operator sees on the laptop clock, and
#: it is only ever used as an identifier -- every instant in this module is
#: UTC seconds. Newer flights carry seconds and, when two would otherwise
#: collide, a suffix: `2026-09-13_130512` and `2026-09-13_130512-2`.
FLIGHT_ID = re.compile(r"_(\d{4}-\d{2}-\d{2}_\d{4}(?:\d{2})?(?:-\d+)?)(?:\.|$)")

#: `recorder_20260911_202519.mcap`. The stamp is the vehicle's clock in UTC,
#: which is not the same as the laptop's -- see `blueos.clock_skew`.
RECORDER_STAMP = re.compile(r"recorder_(\d{8})_(\d{6})")

#: How long a gap in a 1 Hz series is a gap rather than jitter. Two missed
#: samples: one can be a slow disk.
SAMPLE_GAP_S = 2.5

#: Runs shorter than this are not reported as outages. A single lost echo on
#: a link carrying 22 Mbps is weather, not an event.
MIN_OUTAGE_S = 1.0


def _utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


def _parse_iso(text: str) -> float | None:
    """An ISO timestamp as UTC seconds, or None. Never raises."""
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _num(value):
    """A float, or None for a blank. The CSVs use blank for "not known"."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _flag(value) -> bool | None:
    if value in ("TRUE", "True", "true", "1"):
        return True
    if value in ("FALSE", "False", "false", "0"):
        return False
    return None


# --------------------------------------------------------------------------
#  spans
# --------------------------------------------------------------------------


@dataclass
class Span:
    """A stretch of time with a label. The unit every summary reduces to."""

    start: float
    end: float
    what: str = ""
    detail: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


def _runs(times: list[float], flags: list[bool], want: bool,
          min_seconds: float = 0.0) -> list[Span]:
    """Contiguous runs where `flags` equals `want`, as spans.

    The end of a run is the timestamp of the first sample that broke it, not
    the last one that matched: an outage that a sample at t=10 first sees and
    a sample at t=95 first misses lasted until 95, and reporting 94 would
    shorten every outage by one sample period for no reason.
    """
    out: list[Span] = []
    start = None
    for i, flag in enumerate(flags):
        if flag == want and start is None:
            start = times[i]
        elif flag != want and start is not None:
            out.append(Span(start, times[i]))
            start = None
    if start is not None:
        out.append(Span(start, times[-1]))
    return [s for s in out if s.seconds >= min_seconds]


# --------------------------------------------------------------------------
#  one recording
# --------------------------------------------------------------------------


@dataclass
class Recording:
    """One `.mcap`: an armed period, as the vehicle recorded it."""

    path: Path
    size_bytes: int = 0
    start: float = 0.0
    end: float = 0.0
    messages: int = 0
    channels: int = 0
    #: False when the recorder never wrote a summary -- the vehicle was cut
    #: off mid-write. The file is still readable; it just cannot be indexed.
    closed: bool = True
    armed_from: float | None = None
    armed_to: float | None = None
    #: (time, severity, text) for every STATUSTEXT. Small: a whole dive is a
    #: few dozen, and they are the autopilot saying what it did and why.
    statustexts: list[tuple[float, str, str]] = field(default_factory=list)
    #: The ground station that was flying it, as `sysid/compid`. Cockpit and
    #: QGroundControl use different component ids, so this says which client
    #: was connected without anyone having to remember.
    gcs: str = ""
    gcs_heartbeats: int = 0
    #: Gaps in the vehicle's own video stream into its recorder, which is a
    #: Pi-side measurement and says nothing about the tether.
    video_gaps: list[Span] = field(default_factory=list)
    video_frames: int = 0
    #: Autopilot uptime at the first and last sample, in seconds. A value that
    #: goes backwards between two recordings is a power cycle.
    boot_s_first: float | None = None
    boot_s_last: float | None = None
    problem: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def name(self) -> str:
        return self.path.name


#: Topics worth opening a recording for. Everything else is skipped, which is
#: what makes a 4.2 GB file readable in seconds rather than minutes.
_WANT = ("mavlink/1/1/STATUSTEXT", "mavlink/1/1/HEARTBEAT",
         "mavlink/1/1/SYSTEM_TIME")

_VIDEO = "video/"
_GCS_HEARTBEAT = re.compile(r"^mavlink/(\d+)/(\d+)/HEARTBEAT$")


def read_recording(path: Path, *, deep: bool = True) -> Recording:
    """Summarise one `.mcap`. Read-only, and never raises.

    `deep=False` reads only the summary block -- span, counts, channels -- at
    a cost of milliseconds. The default also walks the autopilot's own
    messages, which is where the arming, the failsafes and the boot counter
    live, and costs about a second per gigabyte.
    """
    out = Recording(path=path)
    try:
        out.size_bytes = path.stat().st_size
    except OSError:
        pass
    if make_reader is None:
        out.problem = "the mcap library is not available"
        return out
    try:
        with path.open("rb") as handle:
            reader = make_reader(handle)
            summary = reader.get_summary()
            if summary is None or summary.statistics is None:
                out.closed = False
                out.problem = ("the vehicle never closed this recording, so it "
                               "carries no index")
                return out
            stats = summary.statistics
            out.start = stats.message_start_time / 1e9
            out.end = stats.message_end_time / 1e9
            out.messages = stats.message_count
            out.channels = stats.channel_count
            topics = {c.topic for c in summary.channels.values()}

            for channel in summary.channels.values():
                match = _GCS_HEARTBEAT.match(channel.topic)
                if match and match.group(1) != "1":
                    count = stats.channel_message_counts.get(channel.id, 0)
                    if count > out.gcs_heartbeats:
                        out.gcs_heartbeats = count
                        out.gcs = f"{match.group(1)}/{match.group(2)}"

            if not deep:
                return out

            want = [t for t in _WANT if t in topics]
            if want:
                _read_autopilot(reader, want, out)
            video = sorted(t for t in topics if t.startswith(_VIDEO)
                           and t.endswith("/stream"))
            if video:
                _read_video(reader, video[0], out, stats)
    except Exception as ex:                           # pragma: no cover
        out.problem = f"{type(ex).__name__}: {str(ex)[:90]}"
    return out


def _read_autopilot(reader, topics: list[str], out: Recording) -> None:
    armed_times: list[float] = []
    for _schema, channel, message in reader.iter_messages(topics=topics):
        when = message.log_time / 1e9
        try:
            body = json.loads(message.data).get("message", {})
        except Exception:
            continue
        if channel.topic.endswith("STATUSTEXT"):
            severity = str(body.get("severity", {}).get("type", "")
                           ).replace("MAV_SEVERITY_", "")
            out.statustexts.append((when, severity, str(body.get("text", ""))))
        elif channel.topic.endswith("HEARTBEAT"):
            if "SAFETY_ARMED" in str(body.get("base_mode", "")):
                armed_times.append(when)
        elif channel.topic.endswith("SYSTEM_TIME"):
            boot = body.get("time_boot_ms")
            if boot is not None:
                seconds = float(boot) / 1000.0
                if out.boot_s_first is None:
                    out.boot_s_first = seconds
                out.boot_s_last = seconds
    if armed_times:
        out.armed_from, out.armed_to = armed_times[0], armed_times[-1]


def _read_video(reader, topic: str, out: Recording, stats) -> None:
    """Frame arrival gaps in the vehicle's own stream into its own recorder.

    A Pi-side measurement. The topside may have seen something completely
    different, which is the point of having both.
    """
    previous = None
    for _schema, _channel, message in reader.iter_messages(topics=[topic]):
        when = message.log_time / 1e9
        out.video_frames += 1
        if previous is not None and when - previous > 0.5:
            out.video_gaps.append(Span(previous, when, "video"))
        previous = when


# --------------------------------------------------------------------------
#  the topside 1 Hz row
# --------------------------------------------------------------------------


@dataclass
class MonitorSession:
    """One `laptop_monitor_*.csv`, reduced."""

    flight_id: str = ""
    path: Path | None = None
    start: float = 0.0
    end: float = 0.0
    rows: int = 0
    #: Times, for anything that wants to plot a column against them.
    times: list[float] = field(default_factory=list)
    columns: dict[str, list] = field(default_factory=dict)
    #: Runs where ICMP got no answer at all, and where the interface that
    #: routes to the vehicle received nothing.
    icmp_dead: list[Span] = field(default_factory=list)
    rx_dead: list[Span] = field(default_factory=list)
    unreachable: list[Span] = field(default_factory=list)
    armed: list[Span] = field(default_factory=list)
    cockpit_absent: list[Span] = field(default_factory=list)
    sample_gaps: list[Span] = field(default_factory=list)
    #: What the flight's own JSON said about itself.
    meta: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    def series(self, column: str) -> tuple[list[float], list[float]]:
        """(times, values) for one column, blanks dropped."""
        values = self.columns.get(column)
        if not values:
            return [], []
        t, v = [], []
        # strict=False deliberately: a CSV truncated by a laptop losing power
        # mid-write leaves one column shorter than the timestamps, and the
        # rows that *are* there are still the evidence. Raising here would
        # throw away a whole flight over its last partial line.
        for when, value in zip(self.times, values, strict=False):
            if value is None:
                continue
            t.append(when)
            v.append(value)
        return t, v

    def stat(self, column: str) -> dict:
        """median / p95 / max / min for one column, or {} when it is blank."""
        _t, values = self.series(column)
        if not values:
            return {}
        ordered = sorted(values)
        n = len(ordered)
        return {
            "n": n,
            "min": ordered[0],
            "median": ordered[n // 2],
            "p95": ordered[min(n - 1, int(n * 0.95))],
            "max": ordered[-1],
            "mean": sum(ordered) / n,
        }

    def at(self, when: float) -> dict:
        """The row nearest an instant, for annotating an event."""
        if not self.times:
            return {}
        i = min(len(self.times) - 1, bisect_left(self.times, when))
        return {name: values[i] for name, values in self.columns.items()}


#: Columns kept as numbers. Everything else in the row is carried as text.
_NUMERIC_HINT = ("_pct", "_mbps", "_ms", "_gb", "_mb", "_c", "_w", "_s",
                 "_rpm", "_bytes", "_count", "_mhz", "_length", "errors",
                 "discarded", "pages_per_s")


def read_monitor(path: Path) -> MonitorSession:
    """Load and reduce one topside CSV. Never raises."""
    out = MonitorSession(path=path)
    match = FLIGHT_ID.search(path.name)
    out.flight_id = match.group(1) if match else path.stem

    sidecar = path.with_suffix(".json")
    if sidecar.is_file():
        try:
            out.meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return out
    if not rows:
        return out

    names = [n for n in rows[0] if n and n != "timestamp_utc"]
    columns: dict[str, list] = {n: [] for n in names}
    times: list[float] = []
    for row in rows:
        when = _parse_iso(row.get("timestamp_utc", ""))
        if when is None:
            continue
        times.append(when)
        for name in names:
            raw = row.get(name)
            flag = _flag(raw)
            columns[name].append(flag if flag is not None else _num(raw))
    if not times:
        return out

    out.times = times
    out.columns = columns
    out.rows = len(times)
    out.start, out.end = times[0], times[-1]

    ping = columns.get("rov_ping_latency_ms") or []
    out.icmp_dead = _runs(times, [v is None for v in ping], True, MIN_OUTAGE_S)
    rx = columns.get("network_receive_mbps") or []
    out.rx_dead = _runs(times, [(v or 0.0) < 0.5 for v in rx], True, MIN_OUTAGE_S)
    reach = columns.get("rov_reachable") or []
    out.unreachable = _runs(times, [v is not True for v in reach], True,
                            MIN_OUTAGE_S)
    armed = columns.get("rov_armed") or []
    out.armed = _runs(times, [v is True for v in armed], True, 1.0)
    cockpit = columns.get("cockpit_running") or []
    if any(v is not None for v in cockpit):
        out.cockpit_absent = _runs(times, [v is not True for v in cockpit],
                                   True, 2.0)

    for i in range(1, len(times)):
        if times[i] - times[i - 1] > SAMPLE_GAP_S:
            out.sample_gaps.append(Span(times[i - 1], times[i], "sampling"))
    return out


# --------------------------------------------------------------------------
#  the fast network trace
# --------------------------------------------------------------------------


@dataclass
class PingLog:
    """One `network_pings_*.csv`: every echo, and what became of it."""

    path: Path | None = None
    flight_id: str = ""
    sent: int = 0
    lost: int = 0
    start: float = 0.0
    end: float = 0.0
    times: list[float] = field(default_factory=list)
    rtt: list[float | None] = field(default_factory=list)
    #: Runs of consecutive losses, which is what an outage looks like at
    #: 5 Hz -- resolved to a fifth of a second rather than to a second.
    outages: list[Span] = field(default_factory=list)
    #: How the losses broke down by the stack's own reason. `request timed
    #: out` is a packet that went and never came back; `destination host
    #: unreachable` is one that never left.
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.lost / self.sent if self.sent else 0.0


def read_pings(path: Path) -> PingLog:
    out = PingLog(path=path)
    match = FLIGHT_ID.search(path.name)
    out.flight_id = match.group(1) if match else path.stem
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return out
    flags: list[bool] = []
    for row in rows:
        when = _parse_iso(row.get("timestamp_utc", ""))
        if when is None:
            continue
        rtt = _num(row.get("rtt_ms"))
        out.times.append(when)
        out.rtt.append(rtt)
        flags.append(rtt is None)
        out.sent += 1
        if rtt is None:
            out.lost += 1
            label = row.get("status") or "unknown"
            out.statuses[label] = out.statuses.get(label, 0) + 1
    if out.times:
        out.start, out.end = out.times[0], out.times[-1]
        out.outages = _runs(out.times, flags, True, MIN_OUTAGE_S)
    return out


@dataclass
class FastTrace:
    """One `network_fast_*.csv`: the interfaces, ten times a second."""

    path: Path | None = None
    flight_id: str = ""
    start: float = 0.0
    end: float = 0.0
    ticks: int = 0
    #: {interface column prefix: {"rx_dead": [Span], "carrier_down": [Span],
    #: "low_power": [Span], "mbps": float}}
    interfaces: dict[str, dict] = field(default_factory=dict)
    times: list[float] = field(default_factory=list)
    rx_mbps: dict[str, list[float]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    events: list[str] = field(default_factory=list)

    @property
    def aliases(self) -> dict[str, str]:
        """Column prefix -> the adapter's real name, from the JSON sidecar."""
        return {w.get("column_prefix", ""): w.get("alias", "")
                for w in self.meta.get("watched", [])}


def read_fast(path: Path) -> FastTrace:
    """Load the 10 Hz trace and reduce each interface to its outages.

    The receive counters are cumulative, so a rate is a difference here rather
    than in the file -- which is the right way round: a missed tick shows as a
    larger step instead of a plausible average.
    """
    out = FastTrace(path=path)
    match = FLIGHT_ID.search(path.name)
    out.flight_id = match.group(1) if match else path.stem
    for name, attr in (("network_trace_", "meta"), ("network_events_", "events")):
        sibling = path.with_name(path.name.replace("network_fast_", name))
        sibling = sibling.with_suffix(".json" if attr == "meta" else ".txt")
        if not sibling.is_file():
            continue
        try:
            if attr == "meta":
                out.meta = json.loads(sibling.read_text(encoding="utf-8"))
            else:
                out.events = [ln for ln in
                              sibling.read_text(encoding="utf-8").splitlines()
                              if ln.strip()]
        except Exception:
            pass

    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return out
    if not rows:
        return out
    prefixes = sorted({n.rsplit("_rx_bytes", 1)[0] for n in rows[0]
                       if n.endswith("_rx_bytes")})
    times: list[float] = []
    raw: dict[str, list] = {p: [] for p in prefixes}
    carrier: dict[str, list] = {p: [] for p in prefixes}
    low: dict[str, list] = {p: [] for p in prefixes}
    for row in rows:
        when = _parse_iso(row.get("timestamp_utc", ""))
        if when is None:
            continue
        times.append(when)
        for prefix in prefixes:
            raw[prefix].append(_num(row.get(f"{prefix}_rx_bytes")))
            carrier[prefix].append(_num(row.get(f"{prefix}_carrier")))
            low[prefix].append(_num(row.get(f"{prefix}_low_power")))
    if not times:
        return out
    out.times = times
    out.ticks = len(times)
    out.start, out.end = times[0], times[-1]

    for prefix in prefixes:
        counts = raw[prefix]
        rates: list[float] = [0.0]
        moving: list[bool] = [True]
        for i in range(1, len(counts)):
            a, b = counts[i - 1], counts[i]
            dt = times[i] - times[i - 1]
            if a is None or b is None or dt <= 0 or b < a:
                rates.append(0.0)
                moving.append(True)       # unknown is not evidence of silence
                continue
            rates.append((b - a) * 8 / dt / 1e6)
            moving.append(b > a)
        out.rx_mbps[prefix] = rates
        live = [r for r in rates if r > 0]
        out.interfaces[prefix] = {
            "rx_dead": _runs(times, moving, False, MIN_OUTAGE_S),
            "carrier_down": _runs(times, [c == 0 for c in carrier[prefix]],
                                  True, MIN_OUTAGE_S),
            "low_power": _runs(times, [v == 1 for v in low[prefix]], True,
                               MIN_OUTAGE_S),
            "mean_mbps": (sum(live) / len(live)) if live else 0.0,
            "peak_mbps": max(rates) if rates else 0.0,
        }
    return out


# --------------------------------------------------------------------------
#  the parameter and version snapshots
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    """What the vehicle was, at one instant, as far as anyone could tell."""

    flight_id: str = ""
    path: Path | None = None
    taken: float | None = None
    parameters: dict[str, float] = field(default_factory=dict)
    read_from: str = ""
    versions: dict = field(default_factory=dict)
    #: Whether each half actually read. A snapshot taken while the tether was
    #: down records emptiness, and emptiness recorded as fact is how a log
    #: comes to claim that every extension was uninstalled mid-dive.
    parameters_ok: bool = False
    versions_ok: bool = False
    #: Deltas, when the file carries them.
    changed_by_operator: dict = field(default_factory=dict)
    changed_by_autopilot: dict = field(default_factory=dict)
    version_changes: dict = field(default_factory=dict)
    note: str = ""


def _versions_look_read(versions: dict) -> bool:
    """Did this versions block actually come back, or is it a hole?

    A real read names BlueOS and lists containers. A read that failed leaves
    the keys present and empty, which is indistinguishable from a vehicle
    that has no extensions unless somebody decides -- here -- that it is not.
    """
    if not versions:
        return False
    return bool(versions.get("blueos")) or bool(versions.get("containers"))


def read_snapshots(folder: Path) -> dict[str, Snapshot]:
    """Every parameter/version snapshot in a logs folder, by flight id.

    Reads both layouts: the consolidated `flight_<id>.json` written now, and
    the four separate files written before it. A folder holding both is read
    as the consolidated one, which is the newer and the more complete.
    """
    out: dict[str, Snapshot] = {}

    for path in sorted(folder.glob("flight_*.json")):
        match = FLIGHT_ID.search(path.name)
        if not match:
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snap = Snapshot(flight_id=match.group(1), path=path)
        params = body.get("parameters") or {}
        snap.parameters = params.get("values") or {}
        snap.read_from = params.get("read_from", "")
        snap.parameters_ok = bool(snap.parameters)
        snap.taken = _parse_iso(params.get("taken") or body.get("ended") or "")
        versions = body.get("versions") or {}
        snap.versions = versions.get("values") or {}
        snap.versions_ok = bool(versions.get("read", _versions_look_read(snap.versions)))
        changes = body.get("changes") or {}
        snap.changed_by_operator = changes.get("parameters_by_operator") or {}
        snap.changed_by_autopilot = changes.get("parameters_by_autopilot") or {}
        snap.version_changes = changes.get("versions") or {}
        snap.note = body.get("note", "")
        out[snap.flight_id] = snap

    for path in sorted(folder.glob("params_*.json")):
        match = FLIGHT_ID.search(path.name)
        if not match or match.group(1) in out:
            continue
        flight_id = match.group(1)
        snap = Snapshot(flight_id=flight_id, path=path)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            snap.parameters = body.get("parameters") or {}
            snap.read_from = body.get("read_from", "")
            snap.taken = _parse_iso(body.get("taken", ""))
            snap.parameters_ok = bool(snap.parameters)
        except Exception:
            pass
        versions_path = folder / f"versions_{flight_id}.json"
        if versions_path.is_file():
            try:
                body = json.loads(versions_path.read_text(encoding="utf-8"))
                snap.versions = body.get("versions") or {}
                snap.versions_ok = _versions_look_read(snap.versions)
                if snap.taken is None:
                    snap.taken = _parse_iso(body.get("taken", ""))
            except Exception:
                pass
        delta_path = folder / f"delta_params_{flight_id}.json"
        if delta_path.is_file():
            try:
                body = json.loads(delta_path.read_text(encoding="utf-8"))
                for name, change in (body.get("changes") or {}).items():
                    target = (snap.changed_by_autopilot
                              if change.get("set_by_autopilot")
                              else snap.changed_by_operator)
                    target[name] = change
            except Exception:
                pass
        delta_path = folder / f"delta_versions_{flight_id}.json"
        if delta_path.is_file():
            try:
                body = json.loads(delta_path.read_text(encoding="utf-8"))
                snap.version_changes = body.get("changes") or {}
            except Exception:
                pass
        out[flight_id] = snap
    return out


# --------------------------------------------------------------------------
#  a whole afternoon
# --------------------------------------------------------------------------


@dataclass
class FlightDay:
    """Everything one flight folder holds, reduced and on one clock."""

    folder: Path
    flight_dir: Path | None = None
    recordings: list[Recording] = field(default_factory=list)
    monitors: list[MonitorSession] = field(default_factory=list)
    pings: list[PingLog] = field(default_factory=list)
    traces: list[FastTrace] = field(default_factory=list)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    topside_reports: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    # ---- the clock -----------------------------------------------------

    @property
    def start(self) -> float:
        starts = [s.start for s in self._all_spans() if s.start]
        return min(starts) if starts else 0.0

    @property
    def end(self) -> float:
        ends = [s.end for s in self._all_spans() if s.end]
        return max(ends) if ends else 0.0

    def _all_spans(self):
        return [*self.recordings, *self.monitors, *self.pings, *self.traces]

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def recorded_seconds(self) -> float:
        return sum(r.seconds for r in self.recordings)

    @property
    def monitored_seconds(self) -> float:
        return sum(m.seconds for m in self.monitors)

    @property
    def bytes_recorded(self) -> int:
        return sum(r.size_bytes for r in self.recordings)

    def monitor_at(self, when: float) -> MonitorSession | None:
        for session in self.monitors:
            if session.start <= when <= session.end:
                return session
        return None

    @property
    def site(self) -> str:
        """The site, from the flight folder's name or its saved plan."""
        if self.flight_dir is None:
            return ""
        for name in ("survey_plan.json", "plan.json"):
            path = self.flight_dir / name
            if not path.is_file():
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sites = body.get("sites") or []
            if sites and isinstance(sites, list):
                return str(sites[0].get("name") or "")
        stem = self.flight_dir.name
        parts = stem.split("_")
        return " ".join(parts[3:]) if len(parts) > 3 else stem


def scan(folder: Path, *, deep: bool = True, progress=None) -> FlightDay:
    """Read a flight's `logs` folder, or the flight folder holding one.

    `progress(fraction, message)` is called as recordings are read, because
    they are the slow part and a survey day is several gigabytes of them.
    """
    folder = Path(folder)
    flight_dir = None
    if (folder / "logs").is_dir():
        flight_dir, folder = folder, folder / "logs"
    elif folder.name == "logs":
        flight_dir = folder.parent
    day = FlightDay(folder=folder, flight_dir=flight_dir)
    if not folder.is_dir():
        day.problems.append(f"{folder} is not a folder")
        return day

    # Recordings downloaded by the BlueOS logs tab land in logs/mcap; older
    # flights have them loose in logs. Both are read, once each.
    mcaps = sorted({*folder.glob("*.mcap"), *(folder / "mcap").glob("*.mcap")},
                   key=lambda p: p.name)
    for i, path in enumerate(mcaps):
        if progress:
            progress(i / max(1, len(mcaps)) * 0.8,
                     f"Reading {path.name} ({path.stat().st_size / 2 ** 30:.1f} GB)")
        day.recordings.append(read_recording(path, deep=deep))
    day.recordings.sort(key=lambda r: r.start or 0.0)

    if progress:
        progress(0.85, "Reading the topside logs")
    for path in sorted(folder.glob("laptop_monitor_*.csv")):
        day.monitors.append(read_monitor(path))
    day.monitors.sort(key=lambda m: m.start)
    for path in sorted(folder.glob("network_pings_*.csv")):
        day.pings.append(read_pings(path))
    day.pings.sort(key=lambda p: p.start)
    for path in sorted(folder.glob("network_fast_*.csv")):
        day.traces.append(read_fast(path))
    day.traces.sort(key=lambda t: t.start)
    day.topside_reports = sorted(folder.glob("network_topside_*.txt"))

    if progress:
        progress(0.95, "Reading the parameter snapshots")
    day.snapshots = read_snapshots(folder)

    for recording in day.recordings:
        if recording.problem:
            day.problems.append(f"{recording.name}: {recording.problem}")
    if progress:
        progress(1.0, "Read")
    return day
