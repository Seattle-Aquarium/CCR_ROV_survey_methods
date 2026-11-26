"""
Reading BlueOS's .tlog telemetry -- the recordings from before BlueOS 1.5.

BlueOS 1.5 records each armed period as an mcap. BlueOS 1.4 and earlier do not
record mcaps at all: their MAVLink router (MAVLinkRouter, chosen in BlueOS's
own settings) writes a .tlog instead, one file per power-up, holding the same
MAVLink messages and no video. The team flies whichever release is behaving,
so every part of this program that reads a flight's telemetry has to read
either.

A tlog is a flat stream of MAVLink frames, each prefixed with the moment the
router received it as an 8-byte big-endian microsecond timestamp. There is no
index, no topics and no compression: it is read from front to back with
pymavlink, which the program already has for the autopilot's .BIN logs.

What comes out is the same long-format CSV (t, field, value, sval) that
`mcap_extract` writes, with the same field names, the same decimation and the
same special cases -- so the dive profile, the overlays and the 1 Hz export
read it without knowing which recorder made it. Three things differ from an
mcap and are dealt with here:

* pymavlink hands enums back as integers where the mcap's JSON carries their
  names; the few that anything reads are named again.
* a tlog interleaves every system on the link. The autopilot (system 1,
  component 1) is preferred for each message type, as the mcap reader prefers
  its topic -- except NAMED_VALUE_FLOAT and STATUSTEXT, which are kept from
  every sender because extensions (the Light Monitor, for one) speak through
  them.
* the cache is its own pair of files (`telemetry_tlog.csv` and its marker), so
  ROV Imagery Processing -- which shares the cache and reads only mcaps --
  never mistakes one for the other.
"""

from __future__ import annotations

import csv
import json
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .mcap_extract import (
    CANCEL_EVERY,
    WANTED,
    ExtractionCancelled,
    _CacheLock,
    _min_interval,
    source_fingerprint,
)

ProgressCB = Callable[[float, str], None]

TLOG_CSV = "telemetry_tlog.csv"
TLOG_MARKER = "extract_tlog.json"
#: Bumped whenever what is written changes, so an old cache is rebuilt.
TLOG_SCHEMA = 1

#: The message types read, beyond those `mcap_extract.WANTED` lists.
SPECIAL_TYPES = ("NAMED_VALUE_FLOAT", "STATUSTEXT", "HEARTBEAT", "DISTANCE_SENSOR",
                 "BATTERY_STATUS")
#: Kept from every sender rather than only the autopilot.
ALL_SENDERS = ("NAMED_VALUE_FLOAT", "STATUSTEXT")

#: Plausible bounds for the 8-byte header: 2015 to 2100, in microseconds.
_US_MIN = 1_420_070_400_000_000
_US_MAX = 4_102_444_800_000_000
#: No dive runs longer; bounds the search for the last timestamp.
_MAX_SPAN_US = 24 * 3600 * 1_000_000

#: MAV_GPS_FIX_TYPE, so a fix reads as it does from an mcap.
GPS_FIX_TYPE = {
    0: "GPS_FIX_TYPE_NO_GPS", 1: "GPS_FIX_TYPE_NO_FIX", 2: "GPS_FIX_TYPE_2D_FIX",
    3: "GPS_FIX_TYPE_3D_FIX", 4: "GPS_FIX_TYPE_DGPS", 5: "GPS_FIX_TYPE_RTK_FLOAT",
    6: "GPS_FIX_TYPE_RTK_FIXED", 7: "GPS_FIX_TYPE_STATIC", 8: "GPS_FIX_TYPE_PPP",
}

#: MAV_MODE_FLAG bits, so `base_mode` says "SAFETY_ARMED" as an mcap's does.
MODE_FLAGS = ((128, "MAV_MODE_FLAG_SAFETY_ARMED"),
              (64, "MAV_MODE_FLAG_MANUAL_INPUT_ENABLED"),
              (32, "MAV_MODE_FLAG_HIL_ENABLED"),
              (16, "MAV_MODE_FLAG_STABILIZE_ENABLED"),
              (8, "MAV_MODE_FLAG_GUIDED_ENABLED"),
              (4, "MAV_MODE_FLAG_AUTO_ENABLED"),
              (2, "MAV_MODE_FLAG_TEST_ENABLED"),
              (1, "MAV_MODE_FLAG_CUSTOM_MODE_ENABLED"))

#: MAV_SENSOR_ORIENTATION values the DVL's beams arrive under.
ORIENTATIONS = {25: "MAV_SENSOR_ROTATION_PITCH_270", 24: "MAV_SENSOR_ROTATION_PITCH_90",
                0: "MAV_SENSOR_ROTATION_NONE"}


def mode_flags(base_mode) -> str:
    """`base_mode` as the flag names it carries, joined with " | "."""
    try:
        bits = int(base_mode)
    except (TypeError, ValueError):
        return str(base_mode or "")
    return " | ".join(name for bit, name in MODE_FLAGS if bits & bit)


def is_tlog(path: Path | str) -> bool:
    return Path(path).suffix.lower() == ".tlog"


# --------------------------------------------------------------------------
#  when a tlog ran, without reading it
# --------------------------------------------------------------------------


def tlog_span(path: Path | str) -> tuple[float | None, float | None]:
    """(start, end) in epoch seconds, from the first header and the last.

    The first record's timestamp is at byte zero; the last is found in the
    file's final megabyte as the highest plausible timestamp that is
    immediately followed by the start of a MAVLink frame -- eight payload
    bytes land in any wide range often enough that the frame byte is what
    makes the match trustworthy.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        if size < 8:
            return None, None
        with open(p, "rb") as fh:
            (start_us,) = struct.unpack(">Q", fh.read(8))
            if not _US_MIN <= start_us <= _US_MAX:
                return None, None
            tail = min(size, 1 << 20)
            fh.seek(size - tail)
            buf = fh.read(tail)
    except OSError:
        return None, None
    end_us = start_us
    limit = start_us + _MAX_SPAN_US
    for i in range(len(buf) - 9):
        if buf[i + 8] not in (0xFD, 0xFE):
            continue
        (v,) = struct.unpack_from(">Q", buf, i)
        if start_us <= v <= limit and v > end_us:
            end_us = v
    return start_us / 1e6, end_us / 1e6


def select_for_windows(tlogs: Sequence[Path], windows: Sequence[tuple[float, float]],
                       margin_s: float = 120.0
                       ) -> tuple[list[Path], list[Path], list[str]]:
    """(chosen, skipped, warnings): the tlogs whose span overlaps a window."""
    chosen, skipped, warnings = [], [], []
    for p in tlogs:
        a, b = tlog_span(p)
        if a is None:
            warnings.append(f"{Path(p).name}: no readable time at its start, "
                            f"so it is read in case it covers the transects")
            chosen.append(Path(p))
            continue
        if any(a <= hi + margin_s and b >= lo - margin_s for lo, hi in windows):
            chosen.append(Path(p))
        else:
            skipped.append(Path(p))
    return chosen, skipped, warnings


def _order(tlogs: Sequence[Path]) -> list[Path]:
    return sorted((Path(p) for p in tlogs),
                  key=lambda p: (tlog_span(p)[0] or 0.0, p.name))


# --------------------------------------------------------------------------
#  extraction
# --------------------------------------------------------------------------


@dataclass
class TlogExtract:
    cache_dir: Path
    tlogs: list[Path]
    telemetry_csv: Path
    t_start: float | None = None
    t_end: float | None = None
    telemetry_rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise ExtractionCancelled("stopped")


def _rank(src: tuple[int, int]) -> tuple[int, int]:
    """Lower is better: the autopilot first, then by system id."""
    return (0 if src == (1, 1) else 1, src[0])


def _clean(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value.rstrip("\x00").strip()
    return value


def rows_for(mt: str, m: dict, ts: str) -> list[list]:
    """One message -> its CSV rows, exactly as `mcap_extract` would write them."""
    out: list[list] = []
    if mt == "NAMED_VALUE_FLOAT":
        return [[ts, f"NVF.{_clean(m.get('name', '?'))}", m.get("value", ""), ""]]
    if mt == "STATUSTEXT":
        return [[ts, "STATUSTEXT", "", _clean(m.get("text", ""))]]
    if mt == "HEARTBEAT":
        return [[ts, "HEARTBEAT.custom_mode", m.get("custom_mode", ""), ""],
                [ts, "HEARTBEAT.base_mode", "", mode_flags(m.get("base_mode"))]]
    if mt == "DISTANCE_SENSOR":
        orient = m.get("orientation", "")
        return [[ts, f"DISTANCE_SENSOR.{m.get('id', 0)}.current_distance",
                 m.get("current_distance", ""), ORIENTATIONS.get(orient, str(orient))]]
    if mt == "BATTERY_STATUS":
        v = (m.get("voltages") or [None])[0]
        if v is not None and v != 65535:
            out.append([ts, "BATTERY_STATUS.voltage_mv", v, ""])
    # A lost bottom lock arrives as distance 0.0; it is not an altitude.
    if mt == "RANGEFINDER" and not m.get("distance"):
        return out
    for k in WANTED.get(mt, ()):
        if k not in m:
            continue
        val = m[k]
        if mt == "GPS_RAW_INT" and k == "fix_type":
            out.append([ts, f"{mt}.{k}", "", GPS_FIX_TYPE.get(val, str(val))])
        elif isinstance(val, (list, tuple, bytes)):
            continue
        elif isinstance(val, str):
            out.append([ts, f"{mt}.{k}", "", _clean(val)])
        else:
            out.append([ts, f"{mt}.{k}", val, ""])
    return out


def extract(tlogs: Sequence[Path], cache_dir: Path, *,
            progress: ProgressCB | None = None, force: bool = False,
            cancel=None) -> TlogExtract:
    """Read every tlog into one cached long-format telemetry CSV.

    Cached exactly as an mcap extraction is: used again only when its marker
    names the same files, the same sizes and modification times, and the
    current schema; the marker is removed first and written last, so an
    interrupted read is a miss next time; and one program at a time extracts
    a flight, under the same operating-system lock.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    csv_path = cache_dir / TLOG_CSV
    marker = cache_dir / TLOG_MARKER
    tlogs = [Path(p) for p in tlogs]
    if marker.is_file() and csv_path.is_file() and not force:
        try:
            prev = json.loads(marker.read_text())
            if (prev.get("schema") == TLOG_SCHEMA
                    and prev.get("sources") == source_fingerprint(tlogs)):
                if progress:
                    progress(1.0, "telemetry cache hit")
                return TlogExtract(cache_dir, tlogs, csv_path, prev.get("t_start"),
                                   prev.get("t_end"), prev.get("telemetry_rows", 0),
                                   prev.get("warnings", []))
        except Exception:
            pass
    _check(cancel)
    with _CacheLock(cache_dir):
        return _extract(tlogs, cache_dir, csv_path, marker, progress, cancel)


def _extract(tlogs, cache_dir, csv_path, marker, progress, cancel) -> TlogExtract:
    from pymavlink import mavutil

    marker.unlink(missing_ok=True)
    sources = source_fingerprint(tlogs)
    ordered = _order(tlogs)
    wanted = sorted(set(WANTED) | set(SPECIAL_TYPES))
    res = TlogExtract(cache_dir, ordered, csv_path)
    total = sum(p.stat().st_size for p in ordered) or 1
    done = 0
    tmp = csv_path.with_name(csv_path.name + ".part")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "field", "value", "sval"])
        for n, path in enumerate(ordered):
            _check(cancel)
            size = max(1, path.stat().st_size)
            if progress:
                progress(done / total, f"reading {path.name} ({n + 1}/{len(ordered)})")
            best: dict[str, tuple] = {}
            last_kept: dict[tuple, float] = {}
            seen = 0
            try:
                conn = mavutil.mavlink_connection(str(path))
                while True:
                    try:
                        msg = conn.recv_match(type=wanted, blocking=False)
                    except Exception:
                        # A torn frame at the end of a file the router never
                        # closed: everything before it stands.
                        break
                    if msg is None:
                        break
                    mt = msg.get_type()
                    if mt == "BAD_DATA":
                        continue
                    seen += 1
                    if seen % CANCEL_EVERY == 0:
                        _check(cancel)
                    if progress and seen % 50_000 == 0:
                        try:
                            within = min(1.0, conn.offset / size)
                        except Exception:
                            within = 0.0
                        progress(min(0.99, (done + size * within) / total),
                                 f"{path.name}: {res.telemetry_rows:,} rows")
                    t = float(getattr(msg, "_timestamp", 0.0) or 0.0)
                    if not t:
                        continue
                    try:
                        src = (msg.get_srcSystem(), msg.get_srcComponent())
                    except Exception:
                        src = (1, 1)
                    if mt not in ALL_SENDERS:
                        chosen = best.get(mt)
                        if chosen is None or _rank(src) < _rank(chosen):
                            best[mt] = src
                        elif src != chosen:
                            continue
                    # Decimated per sensor for DISTANCE_SENSOR, as the mcap
                    # reader keeps every beam; per type for everything else.
                    key = (mt, getattr(msg, "id", None) if mt == "DISTANCE_SENSOR" else None)
                    iv = _min_interval(mt)
                    if iv > 0.0:
                        if t - last_kept.get(key, -1e9) < iv:
                            continue
                        last_kept[key] = t
                    res.t_start = t if res.t_start is None else min(res.t_start, t)
                    res.t_end = t if res.t_end is None else max(res.t_end, t)
                    m = msg.to_dict()
                    m.pop("mavpackettype", None)
                    rows = rows_for(mt, m, f"{t:.6f}")
                    w.writerows(rows)
                    res.telemetry_rows += len(rows)
            except ExtractionCancelled:
                raise
            except Exception as ex:
                res.warnings.append(f"{path.name} stopped early: {type(ex).__name__}: "
                                    f"{str(ex).splitlines()[0][:100] if str(ex) else ''}")
            done += size
    tmp.replace(csv_path)
    _check(cancel)
    payload = {"schema": TLOG_SCHEMA, "sources": sources,
               "tlogs": [str(p) for p in ordered], "t_start": res.t_start,
               "t_end": res.t_end, "telemetry_rows": res.telemetry_rows,
               "warnings": res.warnings}
    part = marker.with_name(marker.name + ".part")
    part.write_text(json.dumps(payload, indent=2))
    part.replace(marker)
    if progress:
        progress(1.0, f"read {res.telemetry_rows:,} telemetry rows from "
                      f"{len(ordered)} tlog(s)")
    return res
