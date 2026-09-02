"""
Reading BlueOS .mcap recordings into a per-second telemetry table.

BlueOS 1.5 records telemetry with the on-board recorder rather than writing
.tlog files, so the ingest side of the old tlog workflow had to be replaced.
The container is different but the contents are the same MAVLink stream:

  * topics are namespaced by system/component id -- ``mavlink/1/1/VFR_HUD`` --
    and the payload is JSON, ``{"header": {...}, "message": {"type": ..., ...}}``,
    so no MAVLink dialect parser is needed;
  * ``mavlink/out`` carries a duplicate of everything and is skipped;
  * ``message.log_time`` is epoch nanoseconds on the topside clock, and agrees
    with the vehicle's own ``SYSTEM_TIME`` to within a few hundred ms.

Three differences from a .tlog matter enough to be handled explicitly:

``LOCAL_POSITION_NED`` is usually absent
    Cockpit does not request ArduSub's POSITION stream, so the message that fed
    ``DVLx``/``DVLy``/``NEDz`` in the tlog workflow is simply not recorded. The
    DVL track is instead rebuilt by integrating ``VISION_POSITION_DELTA`` -- the
    body-frame position deltas the Water Linked DVL extension feeds to the EKF --
    rotated into North/East by the ``ATTITUDE`` yaw of the moment. That is the
    same quantity the autopilot integrates, taken one step earlier in the chain.
    ``LOCAL_POSITION_NED`` is still preferred whenever a recording does contain
    it, so this reader matches the tlog output exactly on such files.

``VFR_HUD.alt`` is often flat zero
    Depth therefore falls back to ``GLOBAL_POSITION_INT.relative_alt`` (the
    autopilot's own baro-derived depth, already negative-down), and below that
    to the external pressure sensor. The column ``Depth_Source`` always records
    which one produced a given row.

Averaging is per field, not per message
    Each field is averaged over its own samples in that second, then held
    forward across short gaps up to a per-field staleness limit. Headings are
    averaged as unit vectors, so a track running due north averages to 0 deg
    rather than to 180.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import pandas as pd
import pytz

from mcap.reader import make_reader
from mcap.records import Channel, Message
from mcap.stream_reader import StreamReader

log = logging.getLogger(__name__)

ProgressCB = Callable[[float, str], None]

PACIFIC_TZ = pytz.timezone("US/Pacific")

FIREHOSE_TOPIC = "mavlink/out"      # duplicates every other mavlink topic

# Camera footprint calibration -- kept identical to tlog_to_csv.py so the two
# tools' Width/Area columns stay comparable.
REFERENCE_WIDTH_M = 1.10
REFERENCE_ALT_M = 0.82
REFERENCE_AREA_M2 = 0.99            # 0.9 m x 1.10 m, after the 4606x4030 crop

# Seawater column, for the pressure-derived depth fallback.
WATER_DENSITY = 1025.0              # kg/m3
GRAVITY = 9.80665                   # m/s2

AUTOPILOT_SYSID = 1
AUTOPILOT_COMPID = 1

ARDUSUB_MODE_MAP = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    7: "CIRCLE", 9: "SURFACE", 16: "POSHOLD", 19: "MANUAL",
    20: "MOTOR_DETECT", 21: "SURFTRAK",
}

#: MAVLink messages worth reading. Everything else in the recording -- the IMU
#: streams, MANUAL_CONTROL, the service logs, the video -- is skipped, which is
#: what keeps a pass over a multi-gigabyte file down to a few seconds.
WANTED_TYPES = (
    "ATTITUDE",
    "VFR_HUD",
    "GPS_RAW_INT",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "RANGEFINDER",
    "DISTANCE_SENSOR",
    "VISION_POSITION_DELTA",
    "SCALED_PRESSURE2",
    "HEARTBEAT",
    "SYS_STATUS",
    "BATTERY_STATUS",
    "NAMED_VALUE_FLOAT",
)

#: Fields averaged over their own samples within each second.
MEAN_FIELDS = (
    "Altitude", "Width", "Area_m2", "Heading", "Roll", "Pitch", "Velocity_mps",
    "Battery_V", "Battery_A", "Battery_W", "Water_temp_C", "Pressure_abs_hPa",
    "DVL_confidence",
)

#: Fields carrying the last value seen in each second.
LAST_FIELDS = (
    "Latitude", "Longitude", "EKFlat", "EKFlon", "DVLx", "DVLy",
    "NEDz", "VFR_alt", "Relative_alt_m", "Mode_num", "Mode",
    "Battery_mAh_total", "Battery_Wh_total", "Lights_pct", "Cam_tilt",
    "GPS_fix_type", "GPS_satellites",
)

#: How many seconds a value may be held forward across a gap before the column
#: goes blank. A DVL or rangefinder that drops out should leave a hole, not a
#: flat line that makes a dead sensor look healthy.
HOLD_LIMIT = {
    "Mode": 600, "Mode_num": 600,
    "Lights_pct": 60, "Cam_tilt": 60,
    "Battery_mAh_total": 30, "Battery_Wh_total": 30,
    "Latitude": 15, "Longitude": 15, "GPS_fix_type": 15, "GPS_satellites": 15,
    "EKFlat": 10, "EKFlon": 10,
    "DVLx": 10, "DVLy": 10, "NEDz": 10,
}
DEFAULT_HOLD = 5


def calculate_width(alt_m: float) -> float:
    return REFERENCE_WIDTH_M * (alt_m / REFERENCE_ALT_M) if alt_m and alt_m > 0 else 0.0


def calculate_area(alt_m: float) -> float:
    return REFERENCE_AREA_M2 * (alt_m / REFERENCE_ALT_M) ** 2 if alt_m and alt_m > 0 else 0.0


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _finite_nz(x) -> bool:
    return _finite(x) and x != 0


def _enum(v) -> str:
    """MAVLink enums arrive as ``{"type": "GPS_FIX_TYPE_NO_GPS"}``."""
    if isinstance(v, dict):
        return str(v.get("type", ""))
    return "" if v is None else str(v)


def _brief(ex: BaseException) -> str:
    """One short line describing an exception, for a warning list.

    Some of what the mcap reader raises on a malformed file carries no message
    at all, so the first line cannot simply be indexed.
    """
    lines = str(ex).splitlines()
    return f"{type(ex).__name__}: {lines[0][:120]}" if lines else type(ex).__name__


# ---------------------------------------------------------------------------
#  File selection
# ---------------------------------------------------------------------------

#: A recording this far from the rest is treated as a stray file that happens to
#: share the folder rather than part of the same dive.
STRAY_GAP_HOURS = 12.0


@dataclass
class McapInfo:
    path: Path
    start: float | None = None      # epoch seconds
    end: float | None = None
    messages: int = 0
    error: str | None = None
    #: True when the summary would not parse and the file has to be walked from
    #: the front. The recording is still usable; only its index is lost.
    index_damaged: bool = False

    @property
    def usable(self) -> bool:
        return self.error is None and self.start is not None

    def local_span(self) -> str:
        """Human-readable local time span, for the file list in the GUI."""
        if not self.usable:
            return self.error or "unreadable"
        a = datetime.fromtimestamp(self.start, timezone.utc).astimezone(PACIFIC_TZ)
        if self.index_damaged or self.end is None:
            return f"{a:%Y-%m-%d  %H:%M:%S} -  ?      (no index; will be read in full)"
        b = datetime.fromtimestamp(self.end, timezone.utc).astimezone(PACIFIC_TZ)
        mins = (b - a).total_seconds() / 60.0
        return f"{a:%Y-%m-%d  %H:%M:%S} - {b:%H:%M:%S}  ({mins:.1f} min)"


def probe_mcaps(paths: Sequence[Path | str]) -> list[McapInfo]:
    """Read each file's summary, tolerating ones that will not open.

    A folder routinely holds a recording from another day, or one from a session
    that was cut short. Neither should take the whole run down, and the second
    is usually still worth reading -- see the recovery helpers above.
    """
    out: list[McapInfo] = []
    for p in paths:
        info = McapInfo(Path(p))
        try:
            with open(p, "rb") as f:
                s = make_reader(f).get_summary()
            if s and s.statistics and s.statistics.message_start_time:
                info.start = s.statistics.message_start_time / 1e9
                info.end = s.statistics.message_end_time / 1e9
                info.messages = s.statistics.message_count
            else:
                raise ValueError("no summary")
        except Exception as ex:
            summary_error = _brief(ex)
            # Fall back to the front of the file. Only the start time is read
            # here -- finding the end would mean walking the whole recording,
            # which the actual extraction is about to do anyway.
            try:
                with open(p, "rb") as f:
                    info.start = _first_message_time(f)
                if info.start is None:
                    raise ValueError("no messages")
                info.index_damaged = True
            except Exception:
                info.error = summary_error
        out.append(info)
    return out


def select_mcaps(paths: Sequence[Path | str]) -> tuple[list[Path], list[str]]:
    """Chronological list of the mcaps belonging to one dive, plus warnings.

    Files are grouped into runs separated by more than ``STRAY_GAP_HOURS``, and
    the largest run is kept. Merging a recording from another day would stretch
    the shared timeline across the gap between them, so a folder holding last
    week's dive alongside today's still yields today's.

    Grouping rather than "close to the median start" matters because the median
    of an even number of files is one of the files: given exactly two, a median
    test would keep whichever happened to sort second. Ties here are settled by
    total message count and then by which run came first.
    """
    infos = probe_mcaps(paths)
    warnings = [f"skipping {i.path.name}: {i.error}" for i in infos if i.error]

    good = [i for i in infos if i.usable]
    if not good:
        return [], warnings

    good.sort(key=lambda i: i.start or 0.0)

    runs: list[list[McapInfo]] = [[good[0]]]
    for prev, cur in zip(good, good[1:]):
        gap = (cur.start or 0.0) - (prev.start or 0.0)
        if gap > STRAY_GAP_HOURS * 3600:
            runs.append([])
        runs[-1].append(cur)

    keep_run = max(runs, key=lambda r: (len(r), sum(i.messages for i in r),
                                        -(r[0].start or 0.0)))
    kept_start = keep_run[0].start or 0.0

    for run in runs:
        if run is keep_run:
            continue
        for i in run:
            when = datetime.fromtimestamp(i.start or 0, timezone.utc).astimezone(PACIFIC_TZ)
            warnings.append(
                f"skipping {i.path.name}: recorded {when:%Y-%m-%d %H:%M} local, "
                f"{abs((i.start or 0) - kept_start) / 3600:.0f} h from the rest of "
                f"these files -- it looks like a different dive"
            )
    return [i.path for i in keep_run], warnings


# ---------------------------------------------------------------------------
#  Channel selection
# ---------------------------------------------------------------------------

def _msg_type(topic: str) -> str | None:
    """``mavlink/1/1/VFR_HUD`` -> ``VFR_HUD``.

    Enum sub-topics (``.../GPS_RAW_INT/fix_type``) are skipped: the value is
    already inside the parent message.
    """
    if not topic.startswith("mavlink/") or topic == FIREHOSE_TOPIC:
        return None
    parts = topic.split("/")
    return parts[3] if len(parts) == 4 else None


def _sysid_rank(topic: str) -> tuple[int, int]:
    """Prefer the autopilot when a type appears under several ids.

    DISTANCE_SENSOR arrives as both ``1/1`` (the autopilot's fused rangefinder)
    and ``255/0`` (the raw DVL beams); the former is what the tlog workflow saw.
    """
    parts = topic.split("/")
    try:
        sys_id, comp = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return (9, 9)
    return (0 if (sys_id, comp) == (AUTOPILOT_SYSID, AUTOPILOT_COMPID) else 1, sys_id)


def select_channels(reader) -> dict[str, str]:
    """Map message type -> the one topic to read it from."""
    summary = reader.get_summary()
    if summary is None:
        return {}
    counts = dict(summary.statistics.channel_message_counts) if summary.statistics else {}

    by_type: dict[str, list[tuple[tuple[int, int], int, str]]] = {}
    for ch in summary.channels.values():
        mt = _msg_type(ch.topic)
        if mt in WANTED_TYPES:
            by_type.setdefault(mt, []).append(
                (_sysid_rank(ch.topic), -counts.get(ch.id, 0), ch.topic)
            )
    return {mt: sorted(v)[0][2] for mt, v in by_type.items()}


def available_types(paths: Sequence[Path]) -> set[str]:
    """Every wanted message type present in any of these recordings.

    Several columns have a preferred source and a fallback -- speed from the DVL
    or from VFR_HUD, altitude from RANGEFINDER or DISTANCE_SENSOR. Deciding that
    per file would let a dive switch source halfway through if one recording
    happened to be missing a stream, putting two different quantities in one
    column. Reading the channel lists up front settles it once for the dive.
    """
    found: set[str] = set()
    for p in paths:
        try:
            with open(p, "rb") as fh:
                found.update(select_channels(make_reader(fh)))
        except Exception:
            # No usable summary. The channel records live in the data section
            # and are written as each topic is first used, so a short scan from
            # the front finds essentially all of them.
            try:
                with open(p, "rb") as fh:
                    found.update(_scan_channels(fh))
            except Exception:
                continue    # genuinely unreadable; select_mcaps reports it
    return found


# ---------------------------------------------------------------------------
#  Recovering a recording whose index is damaged
# ---------------------------------------------------------------------------
#
# A recording that was cut short -- the tether pulled, the vehicle powered down
# mid-write -- ends with a truncated summary section. Every indexed read fails
# on it, because the reader needs the summary to find anything. The data records
# themselves are almost always intact, and they are the ones that cannot be
# re-flown, so both the probe and the read fall back to walking the file from
# the front and stopping cleanly at the first record that will not parse.

#: How far into the file to look for channel declarations when there is no index.
_SCAN_RECORD_LIMIT = 50_000


def _scan_channels(fh, limit: int = _SCAN_RECORD_LIMIT) -> set[str]:
    """Wanted message types declared in the first ``limit`` records."""
    found: set[str] = set()
    for i, rec in enumerate(StreamReader(fh).records):
        if i >= limit:
            break
        if isinstance(rec, Channel):
            mt = _msg_type(rec.topic)
            if mt in WANTED_TYPES:
                found.add(mt)
    return found


def _iter_sequential(fh):
    """Yield ``(message_type, message, epoch_seconds)`` without using the index.

    Where a type appears under several system ids, the best-ranked topic seen so
    far wins -- the same preference the indexed path applies, except that here
    the channels arrive as the file is read rather than all at once. In practice
    the autopilot's own streams are declared within the first moments of a
    recording, so the choice settles immediately.
    """
    topics: dict[int, str] = {}
    best: dict[str, tuple[tuple[int, int], str]] = {}

    for rec in StreamReader(fh).records:
        if isinstance(rec, Channel):
            topics[rec.id] = rec.topic
            continue
        if not isinstance(rec, Message):
            continue

        topic = topics.get(rec.channel_id)
        if topic is None:
            continue
        mt = _msg_type(topic)
        if mt not in WANTED_TYPES:
            continue

        rank = _sysid_rank(topic)
        chosen = best.get(mt)
        if chosen is None or rank < chosen[0]:
            best[mt] = (rank, topic)
        elif topic != best[mt][1]:
            continue

        try:
            m = json.loads(rec.data)["message"]
        except Exception:
            continue
        yield mt, m, rec.log_time / 1e9


def _first_message_time(fh) -> float | None:
    """The log time of the first message, for a file with no usable summary."""
    for rec in StreamReader(fh).records:
        if isinstance(rec, Message):
            return rec.log_time / 1e9
    return None


def _iter_indexed(reader, chosen: dict[str, str]):
    """Yield ``(message_type, message, epoch_seconds)`` using the file's index.

    The normal path: the index lets the reader skip whole chunks that hold only
    video or service logs, which is why a pass over a multi-gigabyte recording
    takes seconds rather than minutes.
    """
    topic_type = {v: k for k, v in chosen.items()}
    for _schema, channel, message in reader.iter_messages(topics=list(topic_type)):
        mt = topic_type.get(channel.topic)
        if mt is None:
            continue
        try:
            m = json.loads(message.data)["message"]
        except Exception:
            continue
        yield mt, m, message.log_time / 1e9


def _expected_messages(reader, topics: set[str]) -> int:
    """How many messages the wanted topics hold, so progress moves smoothly
    within a file rather than sitting still through the longest stage."""
    summary = reader.get_summary()
    if not summary or not summary.statistics:
        return 0
    per_ch = dict(summary.statistics.channel_message_counts)
    return sum(per_ch.get(ch.id, 0)
               for ch in summary.channels.values() if ch.topic in topics)


# ---------------------------------------------------------------------------
#  Per-second accumulation
# ---------------------------------------------------------------------------

class _Bucket:
    """One second of telemetry, accumulating means and last-values."""

    __slots__ = ("sums", "counts", "last", "hsin", "hcos", "hn", "msgs")

    def __init__(self) -> None:
        self.sums: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.last: dict[str, object] = {}
        self.hsin = 0.0
        self.hcos = 0.0
        self.hn = 0
        self.msgs = 0

    def add(self, name: str, value) -> None:
        if not _finite(value):
            return
        self.sums[name] = self.sums.get(name, 0.0) + float(value)
        self.counts[name] = self.counts.get(name, 0) + 1

    def add_heading(self, deg: float) -> None:
        """Headings accumulate as unit vectors -- a plain mean of 359 and 1 is
        180, which would point a due-north transect due south."""
        r = math.radians(deg)
        self.hsin += math.sin(r)
        self.hcos += math.cos(r)
        self.hn += 1

    def mean(self, name: str) -> float:
        n = self.counts.get(name, 0)
        return self.sums[name] / n if n else math.nan

    def heading(self) -> float:
        if not self.hn:
            return math.nan
        return (math.degrees(math.atan2(self.hsin, self.hcos)) + 360.0) % 360.0


@dataclass
class ReadResult:
    df: pd.DataFrame
    mcaps: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dvl_source: str = "none"
    depth_sources: dict[str, int] = field(default_factory=dict)
    types_seen: dict[str, int] = field(default_factory=dict)
    t_start: float | None = None
    t_end: float | None = None


def read_mcaps(
    paths: Sequence[Path | str],
    *,
    progress: ProgressCB | None = None,
) -> ReadResult:
    """Parse one or more .mcap files as a single continuous dive log.

    Multiple recordings are normal: BlueOS rolls a new file whenever recording
    restarts, and a dive routinely spans several. They are merged on the
    absolute epoch timeline, so running state -- the DVL integration, the last
    known mode -- carries across the join rather than resetting at each file.
    """
    ordered, warnings = select_mcaps(paths)
    if not ordered:
        raise ValueError(
            "none of the .mcap files could be read:\n  " + "\n  ".join(warnings)
        )

    buckets: dict[int, _Bucket] = {}
    types_seen: dict[str, int] = {}

    # Running state, shared across files so a split recording continues
    # seamlessly rather than restarting its dead reckoning at zero.
    lat = lon = ekf_lat = ekf_lon = None
    dvl_n = dvl_e = 0.0                 # integrated VISION_POSITION_DELTA, metres
    have_vpd = False
    lpn_x = lpn_y = lpn_z = None        # LOCAL_POSITION_NED, when present
    yaw_rad = None
    vfr_alt = None
    rel_alt_m = None
    current_mode = None
    batt_mah = batt_wh = None
    lights = cam_tilt = None
    gps_fix = gps_sats = None

    # Settled once for the whole dive, so a column never changes meaning midway.
    dive_types = available_types(ordered)
    have_rangefinder = "RANGEFINDER" in dive_types
    have_battery_status = "BATTERY_STATUS" in dive_types
    velocity_from_dvl = "VISION_POSITION_DELTA" in dive_types
    # The DVL track has to be decided up front for the same reason as the rest.
    # Both sources are usually present and they do not start together -- the DVL
    # extension publishes its deltas as soon as it has bottom lock, while the
    # autopilot only publishes LOCAL_POSITION_NED once the EKF has accepted
    # them, twenty-odd seconds later. Choosing per message would fill that gap
    # with integrated deltas and the rest of the dive with the EKF's own
    # position, in the same column, with no way to tell which is which.
    use_lpn = "LOCAL_POSITION_NED" in dive_types

    total_bytes = sum(p.stat().st_size for p in ordered) or 1
    done_bytes = 0
    t_start = t_end = None

    def feed(mt: str, m: dict, t: float) -> None:
        """Fold one MAVLink message into the second it belongs to.

        Shared by the indexed and the sequential readers, so a recording whose
        index is damaged produces exactly the same columns as a clean one.
        """
        nonlocal lat, lon, ekf_lat, ekf_lon, dvl_n, dvl_e, have_vpd
        nonlocal lpn_x, lpn_y, lpn_z, yaw_rad, vfr_alt, rel_alt_m
        nonlocal current_mode, batt_mah, batt_wh, lights, cam_tilt
        nonlocal gps_fix, gps_sats, t_start, t_end

        t_start = t if t_start is None else min(t_start, t)
        t_end = t if t_end is None else max(t_end, t)
        key = int(t)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = _Bucket()
        b.msgs += 1
        types_seen[mt] = types_seen.get(mt, 0) + 1

        # ---- attitude ------------------------------------------
        if mt == "ATTITUDE":
            y = m.get("yaw")
            if _finite(y):
                yaw_rad = float(y)
                b.add_heading((math.degrees(yaw_rad) + 360.0) % 360.0)
            if _finite(m.get("roll")):
                b.add("Roll", math.degrees(m["roll"]))
            if _finite(m.get("pitch")):
                b.add("Pitch", math.degrees(m["pitch"]))

        # ---- surface GPS / USBL --------------------------------
        elif mt == "GPS_RAW_INT":
            la, lo = m.get("lat", 0) / 1e7, m.get("lon", 0) / 1e7
            if _finite_nz(la) and _finite_nz(lo):
                lat, lon = la, lo
            gps_fix = _enum(m.get("fix_type"))
            gps_sats = m.get("satellites_visible")

        # ---- fused EKF position --------------------------------
        elif mt == "GLOBAL_POSITION_INT":
            la, lo = m.get("lat", 0) / 1e7, m.get("lon", 0) / 1e7
            if _finite_nz(la) and _finite_nz(lo):
                ekf_lat, ekf_lon = la, lo
            ra = m.get("relative_alt")
            if _finite(ra):
                rel_alt_m = float(ra) / 1000.0     # already negative-down

        # ---- local position, when the recording has it ----------
        elif mt == "LOCAL_POSITION_NED":
            if _finite(m.get("x")):
                lpn_x = float(m["x"])
            if _finite(m.get("y")):
                lpn_y = float(m["y"])
            if _finite(m.get("z")):
                lpn_z = float(m["z"])

        # ---- DVL dead reckoning --------------------------------
        elif mt == "VISION_POSITION_DELTA":
            d = m.get("position_delta") or []
            dt_us = m.get("time_delta_usec") or 0
            if len(d) >= 2 and _finite(d[0]) and _finite(d[1]):
                dx, dy = float(d[0]), float(d[1])
                if yaw_rad is not None:
                    # body-frame delta -> North/East. Yaw only: the
                    # horizontal track should not inherit a pitch bias.
                    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
                    dvl_n += dx * c - dy * s
                    dvl_e += dx * s + dy * c
                    have_vpd = True
                if dt_us and dt_us > 0:
                    b.add("Velocity_mps", math.hypot(dx, dy) / (dt_us / 1e6))
            if _finite(m.get("confidence")):
                b.add("DVL_confidence", m["confidence"])

        # ---- altitude above the seabed -------------------------
        elif mt == "RANGEFINDER":
            d = m.get("distance")
            if _finite(d):
                b.add("Altitude", d)
                b.add("Width", calculate_width(d))
                b.add("Area_m2", calculate_area(d))

        elif mt == "DISTANCE_SENSOR":
            # only a fallback; RANGEFINDER is the autopilot's own
            # fused value and is preferred when both are present
            if not have_rangefinder and m.get("id", 0) == 0:
                cd = m.get("current_distance")
                if _finite(cd):
                    alt = float(cd) / 100.0
                    b.add("Altitude", alt)
                    b.add("Width", calculate_width(alt))
                    b.add("Area_m2", calculate_area(alt))

        # ---- speed / depth from the HUD ------------------------
        elif mt == "VFR_HUD":
            a = m.get("alt")
            if _finite(a):
                vfr_alt = float(a)
            gs = m.get("groundspeed")
            if _finite(gs) and not velocity_from_dvl:
                b.add("Velocity_mps", gs)

        # ---- external pressure / water temperature -------------
        elif mt == "SCALED_PRESSURE2":
            if _finite(m.get("press_abs")):
                b.add("Pressure_abs_hPa", m["press_abs"])
            if _finite(m.get("temperature")):
                b.add("Water_temp_C", m["temperature"] / 100.0)

        # ---- flight mode ---------------------------------------
        elif mt == "HEARTBEAT":
            cm = m.get("custom_mode")
            if _finite(cm):
                current_mode = int(cm)

        # ---- power ---------------------------------------------
        elif mt == "BATTERY_STATUS":
            volts = (m.get("voltages") or [None])[0]
            amps = m.get("current_battery")
            v = a_ = None
            if _finite(volts) and volts not in (65535, 0):
                v = float(volts) / 1000.0
                b.add("Battery_V", v)
            if _finite(amps) and amps != -1:
                a_ = float(amps) / 100.0
                b.add("Battery_A", a_)
            if v is not None and a_ is not None:
                b.add("Battery_W", v * a_)
            mah = m.get("current_consumed")
            if _finite(mah) and mah >= 0:
                batt_mah = float(mah)
            e = m.get("energy_consumed")             # hecto-joules
            if _finite(e) and e >= 0:
                batt_wh = float(e) * 100.0 / 3600.0

        elif mt == "SYS_STATUS":
            if not have_battery_status:
                mv = m.get("voltage_battery")
                ca = m.get("current_battery")
                v = a_ = None
                if _finite(mv) and mv > 0:
                    v = float(mv) / 1000.0
                    b.add("Battery_V", v)
                if _finite(ca) and ca != -1:
                    a_ = float(ca) / 100.0
                    b.add("Battery_A", a_)
                if v is not None and a_ is not None:
                    b.add("Battery_W", v * a_)

        # ---- pilot-set values ----------------------------------
        elif mt == "NAMED_VALUE_FLOAT":
            nm, val = m.get("name"), m.get("value")
            if _finite(val):
                if nm == "Lights1":
                    lights = float(val) * 100.0
                elif nm == "CamTilt":
                    cam_tilt = float(val)

        # ---- last-value state for this second -------------------
        if lat is not None:
            b.last["Latitude"], b.last["Longitude"] = lat, lon
        if ekf_lat is not None:
            b.last["EKFlat"], b.last["EKFlon"] = ekf_lat, ekf_lon
        if use_lpn:
            if lpn_x is not None:
                b.last["DVLx"], b.last["DVLy"] = lpn_x, lpn_y
        elif have_vpd:
            b.last["DVLx"], b.last["DVLy"] = dvl_n, dvl_e
        if lpn_z is not None:
            b.last["NEDz"] = lpn_z
        if vfr_alt is not None:
            b.last["VFR_alt"] = vfr_alt
        if rel_alt_m is not None:
            b.last["Relative_alt_m"] = rel_alt_m
        if current_mode is not None:
            b.last["Mode_num"] = current_mode
            b.last["Mode"] = ARDUSUB_MODE_MAP.get(current_mode, "UNKNOWN")
        if batt_mah is not None:
            b.last["Battery_mAh_total"] = batt_mah
        if batt_wh is not None:
            b.last["Battery_Wh_total"] = batt_wh
        if lights is not None:
            b.last["Lights_pct"] = lights
        if cam_tilt is not None:
            b.last["Cam_tilt"] = cam_tilt
        if gps_fix is not None:
            b.last["GPS_fix_type"] = gps_fix
        if gps_sats is not None:
            b.last["GPS_satellites"] = gps_sats

    for fi, path in enumerate(ordered):
        if progress:
            progress(done_bytes / total_bytes,
                     f"reading {path.name} ({fi + 1}/{len(ordered)})")
        size = path.stat().st_size
        nread = 0

        def report(n: int, expected: int, tail: str = "",
                   _path=path, _size=size, _done=done_bytes) -> None:
            if not progress or n % 20000:
                return
            within = min(1.0, n / expected) if expected else 0.5
            progress(min(0.99, (_done + _size * within) / total_bytes),
                     f"{_path.name}: {n:,}"
                     + (f"/{expected:,}" if expected else "") + f" messages{tail}")

        try:
            with open(path, "rb") as fh:
                reader = make_reader(fh)
                chosen = select_channels(reader)
                if not chosen:
                    warnings.append(f"{path.name}: no MAVLink topics, skipped")
                    done_bytes += size
                    continue
                expected = _expected_messages(reader, set(chosen.values()))
                for mt, m, t in _iter_indexed(reader, chosen):
                    nread += 1
                    report(nread, expected)
                    feed(mt, m, t)

        except Exception as ex:
            first = _brief(ex)
            if nread:
                # Something went wrong partway. Keep what the file did yield and
                # carry on; re-reading it sequentially now would double-count
                # every message already folded in.
                warnings.append(f"{path.name} stopped early: {first}")
            else:
                # Nothing came out at all, which means the summary or the chunk
                # index is unusable -- the signature of a recording that was cut
                # short. The data records themselves are normally intact.
                recovered = 0
                inner = None
                try:
                    with open(path, "rb") as fh:
                        for mt, m, t in _iter_sequential(fh):
                            recovered += 1
                            report(recovered, 0, " (no index)")
                            feed(mt, m, t)
                except Exception as ex2:
                    inner = _brief(ex2)
                if recovered:
                    warnings.append(
                        f"{path.name}: index damaged ({first}); recovered "
                        f"{recovered:,} messages by reading the file in full"
                        + (f", stopping at {inner}" if inner else ""))
                else:
                    warnings.append(f"{path.name}: unreadable ({first})")
        done_bytes += size

    if not buckets:
        raise ValueError("No MAVLink telemetry parsed from the .mcap file(s).")

    df, depth_sources = _to_frame(buckets)
    dvl_source = ("LOCAL_POSITION_NED" if use_lpn
                  else "VISION_POSITION_DELTA" if have_vpd else "none")
    if dvl_source == "none":
        warnings.append(
            "no DVL data (neither LOCAL_POSITION_NED nor VISION_POSITION_DELTA); "
            "DVLx/DVLy/DVLlat/DVLlon will be empty"
        )
    if progress:
        progress(1.0, f"{len(df):,} seconds of telemetry")

    return ReadResult(df=df, mcaps=ordered, warnings=warnings,
                      dvl_source=dvl_source, depth_sources=depth_sources,
                      types_seen=types_seen, t_start=t_start, t_end=t_end)


def _to_frame(buckets: dict[int, _Bucket]) -> tuple[pd.DataFrame, dict[str, int]]:
    """Collapse the per-second buckets into a continuous 1 Hz dataframe."""
    rows = []
    for key in sorted(buckets):
        b = buckets[key]
        row: dict[str, object] = {"Epoch": key, "Messages": b.msgs}
        for name in MEAN_FIELDS:
            row[name] = b.heading() if name == "Heading" else b.mean(name)
        for name in LAST_FIELDS:
            row[name] = b.last.get(name, np.nan)
        rows.append(row)

    df = pd.DataFrame(rows)

    # A recording can drop out mid-dive; reindexing onto a dense one-second grid
    # keeps the transect windows aligned with wall-clock time rather than with
    # however many seconds happened to contain a message.
    full = pd.RangeIndex(int(df["Epoch"].iloc[0]), int(df["Epoch"].iloc[-1]) + 1)
    df = df.set_index("Epoch").reindex(full)
    df.index.name = "Epoch"

    utc = pd.to_datetime(df.index, unit="s", utc=True)
    local = utc.tz_convert(PACIFIC_TZ)
    df["Datetime_UTC"] = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["Date"] = local.strftime("%Y-%m-%d")
    df["Time"] = local.strftime("%H:%M:%S")
    df["Messages"] = df["Messages"].fillna(0).astype(int)
    df = df.reset_index()

    for name in MEAN_FIELDS + LAST_FIELDS:
        if name in df.columns:
            df[name] = df[name].ffill(limit=HOLD_LIMIT.get(name, DEFAULT_HOLD))

    df = _add_depth(df)
    counts = df["Depth_Source"].value_counts().to_dict()
    return df, {str(k): int(v) for k, v in counts.items()}


#: A depth source has to move. Below this standard deviation, over a whole
#: recording, it is a fixed offset rather than a measurement.
MIN_DEPTH_VARIATION_M = 0.10


def _varies(values: pd.Series, threshold: float = MIN_DEPTH_VARIATION_M) -> bool:
    v = values.dropna()
    return len(v) > 1 and float(v.std()) >= threshold


def _add_depth(df: pd.DataFrame) -> pd.DataFrame:
    """Depth, negative-down, from whichever source the recording actually has.

    ``GLOBAL_POSITION_INT.relative_alt`` leads: it is the autopilot's own
    baro-derived depth and the same number UTC's overlays use.

    tlog_to_csv.py preferred ``VFR_HUD.alt``, which was right for a .tlog --
    there it carried the same value. It is not safe here. On the 2026-08-26
    vehicle that field sits at a constant -0.61 m for the whole dive: low enough
    to pass a "is it below -0.5" test, while the ROV was working at 17 m. Taking
    it produced a Depth column that was flat wrong and looked plausible.

    So a candidate now has to *vary* before it is believed. A depth that never
    moves across a whole recording is a fixed offset, not a measurement.
    """
    vfr = pd.to_numeric(df.get("VFR_alt"), errors="coerce")
    rel = pd.to_numeric(df.get("Relative_alt_m"), errors="coerce")
    ned = -pd.to_numeric(df.get("NEDz"), errors="coerce")

    depth = pd.Series(np.nan, index=df.index, dtype=float)
    source = pd.Series("", index=df.index, dtype=object)

    def fill(values: pd.Series, label: str, mask=None) -> None:
        ok = values.notna() & depth.isna()
        if mask is not None:
            ok &= mask.fillna(False)
        depth[ok] = values[ok]
        source[ok] = label

    if _varies(rel):
        fill(rel, "GLOBAL_POSITION_INT")
    if _varies(vfr):
        fill(vfr, "VFR_alt", mask=(vfr < -0.5))
    if _varies(ned):
        fill(ned, "NEDz")
    # Whatever is left: a source that did not vary is still better than nothing,
    # so they are retried without the guard before falling through to pressure.
    fill(rel, "GLOBAL_POSITION_INT")
    fill(ned, "NEDz")

    press = pd.to_numeric(df.get("Pressure_abs_hPa"), errors="coerce")
    if press.notna().any():
        # Surface pressure is taken from a low percentile of the file rather than
        # a nominal 1013.25 hPa, so a genuinely high- or low-pressure day does
        # not offset every depth in the record.
        p0 = float(np.nanpercentile(press.to_numpy(dtype=float), 1))
        fill(-(press - p0) * 100.0 / (WATER_DENSITY * GRAVITY), "SCALED_PRESSURE2")

    df["Depth"] = depth
    # mask, not replace: replacing "" with NaN on an object column triggers
    # pandas' deprecated silent downcasting
    df["Depth_Source"] = source.mask(source == "", np.nan)
    return df
