"""
What the day's logs mean, worked out rather than read.

`flightscan` gathers and reduces; this decides. Everything here is the
reasoning that was done by hand after the 11 September flight, written down so
that it happens in eight seconds on the boat instead of an evening afterwards:

  * every recording that ended is matched to **why** it ended, because a
    recording is one armed period and the interesting question is never "why
    are there five files" but "what disarmed the vehicle five times";
  * the autopilot's own failsafe messages are read, so a ground-station
    heartbeat timeout is named as one instead of being inferred;
  * the topside's link state at that instant is put beside it, which is what
    separates *the link went and took the heartbeat with it* from *the client
    went quiet on a healthy link*;
  * the laptop's own resources across the same window are checked, so
    "the laptop was fine" is a measurement rather than a hope;
  * and the two ground stations, if the day used two, are compared on the
    numbers that differ rather than on the impression they left.

**Findings are evidence plus a claim, never a claim alone.** Every one carries
the instants and readings it came from, so a reader can disagree with the
interpretation without re-reading five gigabytes. Where the data cannot
settle something, the finding says so: a wrong confident answer costs more
than an honest "this cannot be told from here", which is the whole reason the
network columns were added in the first place.

Nothing here reads a vehicle or writes to one. It is arithmetic over files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import flightscan as S

#: ArduSub disarms when the ground station has been silent this long. Every
#: timing judgement about a failsafe is made against it.
GCS_FAILSAFE_S = 3.0

#: The autopilot messages that mean the topside stopped talking. Matched on a
#: fragment rather than the whole string, because the text carries the system
#: id and ArduPilot has reworded it between releases.
FAILSAFE_TEXTS = ("heartbeat lost", "lost manual control", "gcs failsafe")

#: How close a recording's last armed heartbeat has to be to its end for the
#: recording to be called "closed by the disarm" rather than truncated.
CLOSE_ENOUGH_S = 3.0

#: A finding's weight. Only the first two interrupt a survey day.
CRITICAL, WARNING, NOTE, GOOD = "critical", "warning", "note", "good"
_ORDER = {CRITICAL: 0, WARNING: 1, NOTE: 2, GOOD: 3}


def _hm(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%H:%M:%S")


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# --------------------------------------------------------------------------
#  findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    """One thing worth saying, with what it was worked out from."""

    level: str
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    when: float | None = None

    @property
    def rank(self) -> int:
        return _ORDER.get(self.level, 9)


@dataclass
class Disarm:
    """One moment the vehicle stopped being armed, and the case for why."""

    when: float
    recording: str = ""
    #: `gcs failsafe`, `link lost`, `operator`, or `unexplained`.
    cause: str = "unexplained"
    text: str = ""
    #: What the topside was seeing when it happened.
    link_alive: bool | None = None
    link_lost_at: float | None = None
    #: Seconds between the link going and the vehicle noticing. Near the
    #: three-second failsafe means the link went first and took the heartbeat
    #: with it; much larger, or negative, means it did not.
    lead_s: float | None = None
    laptop: dict = field(default_factory=dict)

    @property
    def explained(self) -> bool:
        return self.cause != "unexplained"


@dataclass
class Outage:
    """One stretch during which the topside could not reach the vehicle."""

    start: float
    end: float
    #: `instant` when everything stopped inside one sample, `degrading` when
    #: round trips or throughput were already suffering beforehand.
    onset: str = "instant"
    rx_before_mbps: float | None = None
    rtt_before_ms: float | None = None
    #: Where the evidence puts the break, when it can be placed at all.
    located: str = ""
    disarmed: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class GcsProfile:
    """One ground station's share of the day, measured rather than recalled."""

    name: str
    gcs: str = ""
    seconds: float = 0.0
    monitored_s: float = 0.0
    alive_s: float = 0.0
    failsafes: int = 0
    mean_rx_mbps: float | None = None
    p99_rx_mbps: float | None = None
    recordings: int = 0

    @property
    def availability_pct(self) -> float | None:
        if not self.monitored_s:
            return None
        return 100.0 * self.alive_s / self.monitored_s


@dataclass
class DayReport:
    """The whole afternoon, decided."""

    day: S.FlightDay
    findings: list[Finding] = field(default_factory=list)
    disarms: list[Disarm] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)
    gcs: list[GcsProfile] = field(default_factory=list)
    reboots: list[float] = field(default_factory=list)
    topside: dict = field(default_factory=dict)
    vehicle: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    health = None                                     # ccr_m2c HealthReport
    headline: str = ""

    # ---- convenience for whoever renders this ---------------------------

    @property
    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (f.rank, f.when or 0.0))

    def of(self, level: str) -> list[Finding]:
        return [f for f in self.findings if f.level == level]

    @property
    def worst(self) -> str:
        return min((f.level for f in self.findings),
                   key=lambda lv: _ORDER.get(lv, 9), default=GOOD)


# --------------------------------------------------------------------------
#  the analysis
# --------------------------------------------------------------------------


def analyse(day: S.FlightDay) -> DayReport:
    """Turn a scanned folder into a decided one. Never raises."""
    report = DayReport(day=day)
    _find_disarms(day, report)
    _find_outages(day, report)
    _match_disarms_to_outages(report)
    _find_reboots(day, report)
    _profile_gcs(day, report)
    _summarise_topside(day, report)
    _summarise_vehicle(day, report)
    _summarise_parameters(day, report)
    _read_sensor_health(day, report)
    _raise_findings(day, report)
    _write_headline(report)
    return report


# ---- disarms --------------------------------------------------------------


def _find_disarms(day: S.FlightDay, report: DayReport) -> None:
    """Every recording's ending, and the autopilot's own account of it."""
    for recording in day.recordings:
        if not recording.end:
            continue
        failsafe = None
        for when, _severity, text in recording.statustexts:
            lowered = text.lower()
            if any(fragment in lowered for fragment in FAILSAFE_TEXTS):
                if failsafe is None or when > failsafe[0]:
                    failsafe = (when, text)
        disarm = Disarm(when=recording.armed_to or recording.end,
                        recording=recording.name)
        if failsafe is not None:
            disarm.when = failsafe[0]
            disarm.text = failsafe[1]
            disarm.cause = "gcs failsafe"
        elif recording.armed_to and abs(recording.end - recording.armed_to) <= CLOSE_ENOUGH_S:
            # Armed to the last heartbeat and then the file closed: the
            # vehicle was disarmed deliberately, or the recorder was stopped.
            disarm.cause = "operator"
        session = day.monitor_at(disarm.when)
        if session is not None:
            disarm.laptop = session.at(disarm.when)
            alive = True
            for span in session.icmp_dead:
                if span.start <= disarm.when <= span.end:
                    alive = False
                    disarm.link_lost_at = span.start
                    break
            disarm.link_alive = alive
            if disarm.link_lost_at is not None:
                disarm.lead_s = disarm.when - disarm.link_lost_at
        report.disarms.append(disarm)
    report.disarms.sort(key=lambda d: d.when)


# ---- outages --------------------------------------------------------------


def _find_outages(day: S.FlightDay, report: DayReport) -> None:
    """Every stretch the topside could not reach the vehicle.

    The 5 Hz ping log is preferred where one exists -- it resolves an outage
    to a fifth of a second against the 1 Hz row's one second -- and the 1 Hz
    row fills in for flights recorded before the fast trace existed.
    """
    spans: list[S.Span] = []
    for log in day.pings:
        spans.extend(log.outages)
    covered = [(log.start, log.end) for log in day.pings]

    for session in day.monitors:
        for span in session.icmp_dead:
            if any(a <= span.start <= b for a, b in covered):
                continue
            spans.append(span)
    spans.sort(key=lambda s: s.start)

    for span in spans:
        outage = Outage(start=span.start, end=span.end)
        session = day.monitor_at(span.start)
        if session is not None:
            before = _window(session, span.start - 20, span.start)
            outage.rx_before_mbps = _mean(before.get("network_receive_mbps"))
            outage.rtt_before_ms = _mean(before.get("rov_ping_latency_ms"))
            rising = before.get("rov_ping_latency_ms") or []
            if len(rising) >= 8 and _mean(rising[-4:]) and _mean(rising[:4]):
                if _mean(rising[-4:]) > 3 * _mean(rising[:4]):
                    outage.onset = "degrading"
        outage.located = _locate(day, span)
        report.outages.append(outage)


def _window(session: S.MonitorSession, start: float, end: float) -> dict:
    out: dict[str, list] = {}
    for i, when in enumerate(session.times):
        if when < start:
            continue
        if when > end:
            break
        for name, values in session.columns.items():
            value = values[i]
            if isinstance(value, (int, float)):
                out.setdefault(name, []).append(float(value))
    return out


def _mean(values) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _locate(day: S.FlightDay, span: S.Span) -> str:
    """Where the break was, when the data can place it.

    The decisive comparison is the bridge against the adapter underneath it:
    a member still receiving while the bridge is not is a forwarding failure,
    and both silent is a link that stopped carrying frames. Flights recorded
    before those columns existed return "" -- which is honest, and is the
    reason the columns were added.
    """
    for trace in day.traces:
        if not (trace.start <= span.start <= trace.end):
            continue
        aliases = trace.aliases
        dead_prefixes = []
        live_prefixes = []
        carrier_lost = []
        for prefix, found in trace.interfaces.items():
            overlapping = [s for s in found["rx_dead"] if s.overlaps(span)]
            (dead_prefixes if overlapping else live_prefixes).append(prefix)
            if any(s.overlaps(span) for s in found["carrier_down"]):
                carrier_lost.append(aliases.get(prefix, prefix))
        if carrier_lost:
            return (f"carrier dropped on {', '.join(carrier_lost)} — "
                    f"the adapter, its driver, or the cable")
        if dead_prefixes and live_prefixes:
            dead = ", ".join(aliases.get(p, p) for p in dead_prefixes)
            live = ", ".join(aliases.get(p, p) for p in live_prefixes)
            return (f"{live} kept receiving while {dead} did not — "
                    f"a forwarding failure above the adapter, not a dead link")
        if dead_prefixes and not live_prefixes:
            return ("every watched adapter went silent together with the "
                    "carrier still up — consistent with the tether losing "
                    "sync rather than with anything on this laptop")
    return ""


def _match_disarms_to_outages(report: DayReport) -> None:
    for outage in report.outages:
        for disarm in report.disarms:
            if outage.start <= disarm.when <= outage.end + GCS_FAILSAFE_S:
                outage.disarmed = True
                break


# ---- reboots --------------------------------------------------------------


def _find_reboots(day: S.FlightDay, report: DayReport) -> None:
    """Autopilot uptime going backwards between two recordings."""
    previous = None
    for recording in day.recordings:
        if recording.boot_s_first is None:
            continue
        if previous is not None and recording.boot_s_first < previous[1]:
            report.reboots.append(recording.start - recording.boot_s_first)
        previous = (recording.start, recording.boot_s_last or 0.0)


# ---- the ground stations --------------------------------------------------

#: Component ids the clients in use identify themselves with. A vehicle flown
#: from something else reports its raw `sysid/compid`, which is still enough
#: to tell two clients apart in the comparison.
GCS_NAMES = {"240": "Cockpit", "190": "QGroundControl", "191": "MAVProxy"}


def _gcs_intervals(day: S.FlightDay) -> list[tuple[float, float, str]]:
    """Which client was connected, as contiguous intervals over the whole day.

    Not the union of the recordings. Recordings exist only while the vehicle
    is armed, and the vehicle disarms *because* the link went -- so every
    outage worth attributing falls in the gaps between them. Measuring
    availability over the recordings alone therefore reports the client that
    kept dropping the link as the one that held it best, which is exactly
    backwards and is what a first version of this did.

    Each recording's client is taken to have been connected from the end of
    the previous recording until the end of this one, and the last one runs to
    the end of the day.
    """
    usable = [r for r in day.recordings if r.gcs and r.end]
    out: list[tuple[float, float, str]] = []
    for i, recording in enumerate(usable):
        # A client holds the link from where it started -- or from the start
        # of the day, for the first one -- until the *next* client's first
        # recording begins. The gap between two recordings belongs to the
        # client that was flying before it, not the one that arrives after:
        # the vehicle disarmed into that gap, and it disarmed on the outgoing
        # client's watch. Crediting it forwards hands every outage to whoever
        # cleaned up afterwards.
        start = day.start if i == 0 else recording.start
        end = usable[i + 1].start if i + 1 < len(usable) else max(
            recording.end, day.end)
        out.append((start, end, recording.gcs))
    merged: list[tuple[float, float, str]] = []
    for start, end, gcs in out:
        if merged and merged[-1][2] == gcs:
            merged[-1] = (merged[-1][0], end, gcs)
        else:
            merged.append((start, end, gcs))
    return merged


def _profile_gcs(day: S.FlightDay, report: DayReport) -> None:
    by_gcs: dict[str, GcsProfile] = {}
    for recording in day.recordings:
        if not recording.gcs:
            continue
        compid = recording.gcs.split("/")[-1]
        name = GCS_NAMES.get(compid, f"sysid {recording.gcs}")
        profile = by_gcs.setdefault(name, GcsProfile(name=name,
                                                     gcs=recording.gcs))
        profile.seconds += recording.seconds
        profile.recordings += 1

    intervals = _gcs_intervals(day)
    for profile in by_gcs.values():
        windows = [(a, b) for a, b, gcs in intervals if gcs == profile.gcs]
        rx: list[float] = []
        for session in day.monitors:
            for i, when in enumerate(session.times):
                if not any(a <= when <= b for a, b in windows):
                    continue
                profile.monitored_s += 1.0
                ping = (session.columns.get("rov_ping_latency_ms") or [None])[i] \
                    if i < len(session.columns.get("rov_ping_latency_ms") or []) else None
                if ping is not None:
                    profile.alive_s += 1.0
                value = (session.columns.get("network_receive_mbps") or [None])[i] \
                    if i < len(session.columns.get("network_receive_mbps") or []) else None
                if value:
                    rx.append(float(value))
        if rx:
            rx.sort()
            profile.mean_rx_mbps = sum(rx) / len(rx)
            profile.p99_rx_mbps = rx[min(len(rx) - 1, int(len(rx) * 0.99))]
        profile.failsafes = sum(
            1 for d in report.disarms
            if d.cause == "gcs failsafe"
            and any(a <= d.when <= b + GCS_FAILSAFE_S for a, b in windows))
    report.gcs = sorted(by_gcs.values(), key=lambda p: -p.seconds)


# ---- the topside ----------------------------------------------------------

#: The readings that would show a laptop failing to keep up, and the reading
#: that would show it being held back. Checked together, because "the laptop
#: was fine" is a claim about all of them and not about any one.
_PRESSURE = {
    "cpu_usage_pct": ("CPU", 95.0),
    "cpu_max_core_usage_pct": ("hottest core", 99.0),
    "ram_usage_pct": ("memory", 90.0),
    "pagefile_usage_pct": ("pagefile", 80.0),
    "disk_active_pct": ("disk", 95.0),
    "disk_queue_length": ("disk queue", 8.0),
    "gpu_usage_pct": ("GPU", 97.0),
}


def _summarise_topside(day: S.FlightDay, report: DayReport) -> None:
    out: dict = {"sessions": len(day.monitors), "pressure": [], "stats": {}}
    if not day.monitors:
        report.topside = out
        return
    merged: dict[str, list[float]] = {}
    for session in day.monitors:
        for name in session.columns:
            _t, values = session.series(name)
            numeric = [float(v) for v in values if isinstance(v, (int, float))
                       and not isinstance(v, bool)]
            if numeric:
                merged.setdefault(name, []).extend(numeric)
    for name, values in merged.items():
        values.sort()
        n = len(values)
        out["stats"][name] = {
            "n": n, "min": values[0], "median": values[n // 2],
            "p95": values[min(n - 1, int(n * 0.95))], "max": values[-1],
            "mean": sum(values) / n,
        }
    for name, (label, limit) in _PRESSURE.items():
        stat = out["stats"].get(name)
        if stat and stat["p95"] >= limit:
            out["pressure"].append(
                f"{label} reached {stat['p95']:.0f} at the 95th percentile")

    power = set()
    for session in day.monitors:
        power.update(v for v in (session.columns.get("power_source") or [])
                     if isinstance(v, str) and v)
    # power_source is text, so it arrives as None from the numeric parse; read
    # it back off the raw file instead of guessing.
    out["power"] = sorted(power)
    battery = out["stats"].get("battery_discharge_w")
    if battery:
        out["on_battery"] = battery["median"] > 1.0
    throttle = out["stats"].get("cpu_power_limit_throttle")
    if throttle and throttle["median"] >= 1.0:
        out["power_limited"] = True
    ceiling = out["stats"].get("cpu_max_freq_ceiling_pct")
    if ceiling:
        out["freq_ceiling_pct"] = ceiling["median"]

    errors = 0.0
    for name in ("network_errors_received", "network_errors_sent"):
        stat = out["stats"].get(name)
        if stat:
            errors += stat["max"]
    out["nic_errors"] = errors
    discards = out["stats"].get("network_packets_discarded")
    if discards:
        out["nic_discards_moved"] = discards["max"] > discards["min"]
    report.topside = out


# ---- the vehicle ----------------------------------------------------------


def _summarise_vehicle(day: S.FlightDay, report: DayReport) -> None:
    out: dict = {}
    temps: list[float] = []
    throttled = False
    for session in day.monitors:
        _t, values = session.series("pi_soc_temp_c")
        temps.extend(float(v) for v in values if isinstance(v, (int, float)))
        for value in session.columns.get("pi_throttling") or []:
            if value is True:
                throttled = True
    if temps:
        temps.sort()
        out["soc_temp_c"] = {"median": temps[len(temps) // 2],
                             "max": temps[-1]}
    out["throttled"] = throttled
    out["video_gap_seconds"] = sum(
        s.seconds for r in day.recordings for s in r.video_gaps)
    out["video_gaps"] = sum(len(r.video_gaps) for r in day.recordings)
    out["recordings"] = len(day.recordings)
    out["recorded_seconds"] = day.recorded_seconds
    for snapshot in day.snapshots.values():
        if snapshot.versions_ok and snapshot.versions:
            out["versions"] = snapshot.versions
            break
    report.vehicle = out


# ---- parameters and versions ----------------------------------------------


def _summarise_parameters(day: S.FlightDay, report: DayReport) -> None:
    out: dict = {"operator_changes": {}, "autopilot_changes": {},
                 "version_changes": {}, "failed_reads": [], "count": 0}
    for flight_id, snapshot in sorted(day.snapshots.items()):
        out["count"] = max(out["count"], len(snapshot.parameters))
        for name, change in snapshot.changed_by_operator.items():
            out["operator_changes"].setdefault(name, []).append(
                (flight_id, change))
        for name in snapshot.changed_by_autopilot:
            out["autopilot_changes"][name] = (
                out["autopilot_changes"].get(name, 0) + 1)
        if not snapshot.versions_ok:
            out["failed_reads"].append(flight_id)
        elif snapshot.version_changes:
            out["version_changes"][flight_id] = snapshot.version_changes
    # A version "change" computed against a snapshot that failed to read is
    # not a change. Drop it rather than report twenty uninstalled extensions.
    for flight_id in list(out["version_changes"]):
        if flight_id in out["failed_reads"]:
            out["version_changes"].pop(flight_id, None)
        elif _looks_like_a_failed_read(out["version_changes"][flight_id]):
            out["version_changes"].pop(flight_id, None)
            out["failed_reads"].append(flight_id)
    report.parameters = out


#: How many components have to vanish at once before a version diff is read as
#: a failed snapshot rather than as somebody uninstalling things.
MASS_DISAPPEARANCE = 4


def _looks_like_a_failed_read(changes: dict) -> bool:
    """Did one side of this diff simply not answer?

    Extensions are installed and removed one or two at a time, by a person, on
    purpose. A diff in which most of the vehicle's software appears or
    disappears at once is not that -- it is a snapshot taken while the tether
    was down, with the emptiness recorded as fact.

    Worth catching even when the snapshot's own versions block looks healthy,
    because a flight's *opening* snapshot can be the failed one while its
    closing snapshot is fine, and the diff between them is then confidently
    wrong in exactly this shape. The 11 September logs reported twenty
    extensions and containers as removed mid-dive; none were.
    """
    empty_side = 0
    for value in changes.values():
        # Two shapes are in the wild: `diff_versions` returns (before, after)
        # tuples, and the delta file on disk writes {"before":…, "after":…}.
        # A guard that understood only one of them silently did nothing,
        # which is how the false diff survived the first attempt at this.
        if isinstance(value, dict):
            before, after = value.get("before"), value.get("after")
        elif isinstance(value, (list, tuple)):
            before, after = (list(value) + [None, None])[:2]
        else:
            continue
        if bool(before) != bool(after):
            empty_side += 1
    return empty_side >= MASS_DISAPPEARANCE


# ---- sensor health --------------------------------------------------------


def _read_sensor_health(day: S.FlightDay, report: DayReport) -> None:
    """The EKF and the instruments behind it, via the transect extractor.

    Optional: the extractor is a sibling package and a checkout without it
    still produces a report, minus this section.
    """
    paths = [r.path for r in day.recordings if r.closed and r.seconds > 60]
    if not paths:
        return
    try:
        from ccr_m2c.health import read_health  # noqa: PLC0415
    except Exception:
        return
    try:
        report.health = read_health(paths)
    except Exception:
        report.health = None


# ---- findings -------------------------------------------------------------


def _raise_findings(day: S.FlightDay, report: DayReport) -> None:
    add = report.findings.append

    failsafes = [d for d in report.disarms if d.cause == "gcs failsafe"]
    if failsafes:
        led = [d for d in failsafes if d.lead_s is not None
               and 0 <= d.lead_s <= 20]
        detail = (
            f"The vehicle disarmed itself {len(failsafes)} time"
            f"{'s' if len(failsafes) != 1 else ''} because the ground station "
            f"stopped heartbeating for more than {GCS_FAILSAFE_S:.0f} seconds. "
            f"Each recording ends there: the recorder writes only while armed, "
            f"so the broken files are the disarms, not a recorder fault.")
        if led:
            detail += (f" In {len(led)} of them the topside link had already "
                       f"gone silent {min(d.lead_s for d in led):.0f}"
                       f"–{max(d.lead_s for d in led):.0f} s earlier, so the "
                       f"link went first and took the heartbeat with it.")
        add(Finding(CRITICAL, "Ground-station failsafe disarmed the vehicle",
                    detail,
                    [f"{_hm(d.when)}  {d.text or 'heartbeat lost'}"
                     f"  ({d.recording})" for d in failsafes],
                    failsafes[0].when))

    real = [o for o in report.outages if o.seconds >= 2.0]
    if real:
        total = sum(o.seconds for o in real)
        longest = max(real, key=lambda o: o.seconds)
        located = {o.located for o in real if o.located}
        add(Finding(
            CRITICAL if any(o.disarmed for o in real) else WARNING,
            f"The tether went silent {len(real)} time"
            f"{'s' if len(real) != 1 else ''}",
            f"{_dur(total)} of the monitored day had no path to the vehicle. "
            f"The longest ran {_dur(longest.seconds)} from {_hm(longest.start)}."
            + (" " + "; ".join(sorted(located)) if located else
               " Nothing in these logs can place the break: the flight predates "
               "the per-adapter columns that would separate a bridge that "
               "stopped forwarding from a tether that stopped carrying."),
            [f"{_hm(o.start)}  {_dur(o.seconds)}"
             f"{'  (vehicle disarmed)' if o.disarmed else ''}"
             f"{'  onset: ' + o.onset if o.onset != 'instant' else ''}"
             for o in real[:8]],
            real[0].start))

    pressure = report.topside.get("pressure") or []
    if pressure:
        add(Finding(WARNING, "The laptop was under pressure",
                    "; ".join(pressure) + ".", []))
    elif day.monitors:
        stats = report.topside.get("stats", {})
        cpu = stats.get("cpu_usage_pct", {})
        ram = stats.get("ram_available_gb", {})
        bits = []
        if cpu:
            bits.append(f"CPU median {cpu['median']:.0f}%, "
                        f"95th {cpu['p95']:.0f}%")
        if ram:
            bits.append(f"{ram['min']:.1f} GB of memory free at its lowest")
        if report.topside.get("nic_errors") == 0:
            bits.append("zero network adapter errors all day")
        add(Finding(GOOD, "The laptop kept up",
                    "Nothing on the topside ran out: " + ", ".join(bits) + ". "
                    "Whatever interrupted the link, it was not this machine "
                    "failing to keep pace.", []))

    if report.topside.get("on_battery"):
        watts = report.topside["stats"].get("battery_discharge_w", {})
        add(Finding(NOTE, "Flown on battery",
                    f"The laptop ran on its own battery, drawing about "
                    f"{watts.get('median', 0):.0f} W. On battery Windows is "
                    f"free to power-manage the network adapter and the PCI "
                    f"Express link; on mains it is not. Worth holding "
                    f"constant before comparing two flights.", []))

    if report.topside.get("power_limited"):
        ceiling = report.topside.get("freq_ceiling_pct")
        add(Finding(NOTE, "The processor was power-limited throughout",
                    f"Windows held the clock ceiling at "
                    f"{ceiling:.0f}% of maximum for the whole day. Constant, "
                    f"so it explains nothing that changed — but it is the "
                    f"headroom this laptop actually has." if ceiling else
                    "Windows reported a sustained power limit all day.", []))

    if len(report.gcs) > 1:
        best = max(report.gcs, key=lambda p: p.availability_pct or 0)
        worst = min(report.gcs, key=lambda p: p.availability_pct or 100)
        if best is not worst and best.availability_pct is not None:
            add(Finding(
                WARNING, f"{worst.name} and {best.name} did not behave alike",
                f"{worst.name} held the link "
                f"{worst.availability_pct:.0f}% of its monitored time against "
                f"{best.name}'s {best.availability_pct:.0f}%, with "
                f"{worst.failsafes} failsafe"
                f"{'s' if worst.failsafes != 1 else ''} against "
                f"{best.failsafes}."
                + (f" {worst.name} was also pulling "
                   f"{worst.mean_rx_mbps:.0f} Mbps down the tether against "
                   f"{best.mean_rx_mbps:.0f}."
                   if worst.mean_rx_mbps and best.mean_rx_mbps else ""),
                [f"{p.name}: {p.recordings} recording(s), {_dur(p.seconds)}, "
                 f"{p.failsafes} failsafe(s)" for p in report.gcs]))

    if report.reboots:
        add(Finding(NOTE, f"The vehicle was power-cycled "
                          f"{len(report.reboots)} time"
                          f"{'s' if len(report.reboots) != 1 else ''}",
                    "Autopilot uptime went backwards between recordings. "
                    "Useful when reading the files: recordings either side of "
                    "a reboot do not share a boot counter, and the statistics "
                    "parameters reset with it.",
                    [_hm(t) for t in report.reboots]))

    failed = report.parameters.get("failed_reads") or []
    if failed:
        add(Finding(WARNING, "A vehicle snapshot came back empty",
                    f"{len(failed)} snapshot"
                    f"{'s' if len(failed) != 1 else ''} recorded no BlueOS "
                    f"version and no containers. That is a read taken while "
                    f"the vehicle was unreachable, not a vehicle with nothing "
                    f"installed — any version comparison against it has been "
                    f"suppressed rather than reported as twenty extensions "
                    f"disappearing.",
                    failed))

    operator = report.parameters.get("operator_changes") or {}
    if operator:
        add(Finding(WARNING, f"{len(operator)} parameter"
                             f"{'s' if len(operator) != 1 else ''} changed "
                             f"during the day",
                    "Changed by somebody rather than by the autopilot's own "
                    "bookkeeping.",
                    [f"{name}: {ch.get('before')} → {ch.get('after')}"
                     for name, (flight, ch) in
                     ((n, v[0]) for n, v in sorted(operator.items()))][:10]))
    elif day.snapshots:
        auto = len(report.parameters.get("autopilot_changes") or {})
        add(Finding(GOOD, "No parameter was changed by hand",
                    f"The {report.parameters.get('count', 0)} ArduSub "
                    f"parameters came back identical at every snapshot except "
                    f"the {auto} the autopilot maintains itself — barometer "
                    f"ground pressure and the statistics counters, which move "
                    f"on every flight.", []))

    changes = report.parameters.get("version_changes") or {}
    if changes:
        add(Finding(NOTE, "Software versions moved",
                    "Between one snapshot and the next.",
                    [f"{flight}: {name} {a} → {b}"
                     for flight, found in changes.items()
                     for name, (a, b) in list(found.items())[:6]]))

    if report.health is not None:
        try:
            concerns = report.health.concerns()
        except Exception:
            concerns = []
        for concern in concerns[:6]:
            add(Finding(WARNING, _health_title(concern), concern, []))

    gaps = report.vehicle.get("video_gaps") or 0
    if gaps > 20:
        add(Finding(WARNING, "The vehicle's own video stream stalled",
                    f"{gaps} gaps of half a second or more in the frames "
                    f"reaching the recorder, {_dur(report.vehicle.get('video_gap_seconds', 0))} "
                    f"in total. This is measured on the Pi, so it is the "
                    f"vehicle's own pipeline rather than anything on the "
                    f"tether — a background job on the companion computer "
                    f"will do it.", []))

    for problem in day.problems[:5]:
        add(Finding(WARNING, "A recording could not be read fully", problem, []))

    temp = (report.vehicle.get("soc_temp_c") or {}).get("max")
    if temp and temp >= 78:
        add(Finding(WARNING, "The companion computer ran hot",
                    f"The Pi's SoC peaked at {temp:.0f} °C; throttling starts "
                    f"near 80.", []))
    elif temp:
        add(Finding(GOOD, "The companion computer stayed cool",
                    f"The Pi's SoC peaked at {temp:.0f} °C, against the 80 °C "
                    f"where throttling begins.", []))


#: The concerns `ccr_m2c.health` raises, as short titles. Its own text is a
#: full paragraph aimed at a survey lead, which is right for the body and far
#: too long for a heading -- and truncating a sentence mid-word, which a first
#: version did, reads as a bug rather than as a summary.
_HEALTH_TITLES = (
    ("absolute horizontal position", "No absolute position fix all dive"),
    ("compass", "The compass was fighting the filter"),
    ("vibration", "Vibration above the ArduPilot limit"),
    ("clipping", "The accelerometers clipped"),
    ("dead reckoning", "Position is dead reckoning only"),
    ("altitude", "The altitude source dropped out"),
    ("gps", "GPS reported unhealthy"),
    ("dvl", "The DVL reported unhealthy"),
    ("odometry", "The DVL's odometry reported unhealthy"),
    ("rangefinder", "The altitude rangefinder reported unhealthy"),
)


def _health_title(concern: str) -> str:
    lowered = concern.lower()
    for fragment, title in _HEALTH_TITLES:
        if fragment in lowered:
            return title
    first = concern.split(".")[0]
    return first if len(first) <= 62 else first[:59].rsplit(" ", 1)[0] + "…"


def _write_headline(report: DayReport) -> None:
    day = report.day
    parts = [f"{len(day.recordings)} recording"
             f"{'s' if len(day.recordings) != 1 else ''}",
             _dur(day.recorded_seconds) + " armed",
             f"{day.bytes_recorded / 2 ** 30:.1f} GiB"]
    failsafes = sum(1 for d in report.disarms if d.cause == "gcs failsafe")
    if failsafes:
        parts.append(f"{failsafes} ground-station failsafe"
                     f"{'s' if failsafes != 1 else ''}")
    real = [o for o in report.outages if o.seconds >= 2.0]
    if real:
        parts.append(f"{_dur(sum(o.seconds for o in real))} with no link")
    report.headline = " · ".join(parts)
