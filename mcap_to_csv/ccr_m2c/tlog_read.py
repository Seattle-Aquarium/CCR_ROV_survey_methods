"""
Reading the .tlog files recorded before BlueOS 1.5.

A tlog is a flat stream of MAVLink frames, each prefixed by an 8-byte big-endian
microsecond timestamp. No index, no topics, no compression -- so where an mcap is
opened and queried, a tlog is simply read from front to back.

What comes out is deliberately the same three-tuple the mcap readers yield,
``(message type, fields, epoch seconds)``, so the whole of the rest of the
extractor is shared: the same per-second folding, the same depth precedence, the
same transect cutting, tide standardisation, map and health report. A tlog and an
mcap of the same dive should differ only where the recordings genuinely differ.

Three things do not line up on their own and are fixed here:

* pymavlink returns enums as integers where the mcap carries their names, so a
  fix type would read ``3`` in one file and ``GPS_FIX_TYPE_3D_FIX`` in the other.
* ``HEARTBEAT`` has a field of its own called ``type``, which in the mcap's JSON
  is where the *message* name lives. Nothing downstream reads it -- the type is
  passed alongside the fields, not inside them -- but it is a trap worth naming.
* A tlog interleaves every system on the link. The topic names an mcap sorts by
  do not exist, so the sending system is read from the frame header and the same
  preference applied: the autopilot wins when a message type comes from more
  than one place.
"""

from __future__ import annotations

import logging
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

#: Plausible range for a tlog's 8-byte microsecond header, used to find the
#: last timestamp without parsing the frames leading up to it. 2015-01-01 to
#: 2100-01-01 -- wide enough for any real recording, narrow enough that random
#: payload bytes almost never pass.
_US_MIN = 1_420_070_400_000_000
_US_MAX = 4_102_444_800_000_000

#: No survey dive runs longer than this. Bounds the search for the last
#: timestamp, so a stray match cannot put the end of the file in 2087.
_MAX_DIVE_US = 24 * 60 * 60 * 1_000_000

#: MAV_GPS_FIX_TYPE, so GPS_fix_type reads the same as it does from an mcap.
_GPS_FIX_TYPE = {
    0: "GPS_FIX_TYPE_NO_GPS", 1: "GPS_FIX_TYPE_NO_FIX", 2: "GPS_FIX_TYPE_2D_FIX",
    3: "GPS_FIX_TYPE_3D_FIX", 4: "GPS_FIX_TYPE_DGPS", 5: "GPS_FIX_TYPE_RTK_FLOAT",
    6: "GPS_FIX_TYPE_RTK_FIXED", 7: "GPS_FIX_TYPE_STATIC", 8: "GPS_FIX_TYPE_PPP",
}

#: Enum fields to translate, by message and field name.
_ENUMS = {("GPS_RAW_INT", "fix_type"): _GPS_FIX_TYPE}


def is_tlog(path: Path | str) -> bool:
    return Path(path).suffix.lower() == ".tlog"


def _mavutil():
    """pymavlink, or a message saying how to get it.

    It is imported here rather than at module scope so that a checkout without
    it still reads mcaps perfectly well -- the dependency only exists for the
    older format, and most flights no longer need it.
    """
    try:
        from pymavlink import mavutil
        return mavutil
    except ImportError as ex:
        raise RuntimeError(
            "Reading .tlog files needs pymavlink, which is not installed:\n"
            "    python -m pip install pymavlink"
        ) from ex


def _rank(sysid: int, compid: int) -> tuple[int, int]:
    """Lower sorts better. The autopilot is system 1, component 1.

    Mirrors the topic-based ranking the mcap reader applies, so a message type
    arriving from two systems resolves to the same one either way.
    """
    return (0 if (sysid, compid) == (1, 1) else 1, sysid)


def _clean(value):
    """pymavlink hands back char arrays with their padding still attached."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value.rstrip("\x00").strip()
    return value


def iter_tlog(path: Path | str, wanted: tuple[str, ...]) -> Iterator[tuple[str, dict, float]]:
    """Yield ``(message type, fields, epoch seconds)`` for the wanted types.

    Filtering happens inside pymavlink rather than here: a dive's tlog holds
    close to a million frames and most of them are of no interest, and building
    a dict for each one costs more than the rest of the read put together.
    """
    conn = _mavutil().mavlink_connection(str(path))
    best: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}

    while True:
        try:
            msg = conn.recv_match(type=list(wanted), blocking=False)
        except Exception:
            # A truncated or corrupt frame near the end of a file that was never
            # closed. Everything read up to here still stands.
            break
        if msg is None:
            break
        if msg.get_type() == "BAD_DATA":
            continue

        t = getattr(msg, "_timestamp", 0.0)
        if not t:
            continue

        mt = msg.get_type()
        try:
            src = (msg.get_srcSystem(), msg.get_srcComponent())
        except Exception:
            src = (1, 1)

        # Accept only the best-ranked source seen so far for this type. The
        # autopilot's own streams start within the first moments of a link, so
        # in practice the choice settles immediately.
        rank = _rank(*src)
        chosen = best.get(mt)
        if chosen is None or rank < chosen[0]:
            best[mt] = (rank, src)
        elif src != best[mt][1]:
            continue

        fields = msg.to_dict()
        fields.pop("mavpackettype", None)
        for (m_name, f_name), table in _ENUMS.items():
            if mt == m_name and f_name in fields:
                fields[f_name] = table.get(fields[f_name], str(fields[f_name]))
        if mt == "NAMED_VALUE_FLOAT":
            fields["name"] = _clean(fields.get("name"))

        yield mt, fields, float(t)


def probe_tlog(path: Path | str) -> tuple[float | None, float | None, str | None]:
    """``(start, end, error)`` in epoch seconds, without reading the frames.

    Every record is prefixed by its own timestamp, so the first is at byte zero
    and the last can be found by scanning the tail for the highest plausible
    one. A tlog carries no index and no message count, and walking a 40 MB file
    just to fill in a file-list entry is not worth the wait.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        if size < 8:
            return None, None, "file is empty"
        with open(p, "rb") as f:
            start_us = struct.unpack(">Q", f.read(8))[0]
            if not _US_MIN <= start_us <= _US_MAX:
                return None, None, "no timestamp at the start (not a tlog?)"

            tail = min(size, 1 << 20)
            f.seek(size - tail)
            buf = f.read(tail)

        # A timestamp alone is not enough to go on: eight arbitrary payload
        # bytes land inside any plausible range often enough to produce an end
        # time years after the start. Every record is a timestamp *immediately
        # followed by* a MAVLink frame, so requiring the start-of-frame byte as
        # well is what makes the match trustworthy.
        end_us = start_us
        limit = start_us + _MAX_DIVE_US
        for i in range(len(buf) - 9):
            if buf[i + 8] not in (0xFD, 0xFE):        # MAVLink v2, v1
                continue
            v = struct.unpack_from(">Q", buf, i)[0]
            if start_us <= v <= limit and v > end_us:
                end_us = v
        return start_us / 1e6, end_us / 1e6, None
    except Exception as ex:
        return None, None, f"{type(ex).__name__}: {ex}".splitlines()[0][:120]


def describe(path: Path | str) -> str:
    """One line for a file list, matching the mcap probe's shape."""
    start, end, err = probe_tlog(path)
    if err or start is None:
        return err or "unreadable"
    a = datetime.fromtimestamp(start, timezone.utc)
    b = datetime.fromtimestamp(end or start, timezone.utc)
    return f"{a:%Y-%m-%d %H:%M:%S} - {b:%H:%M:%S}"


#: How far into a tlog to look for which message types it carries. Streams
#: start at different moments -- the DVL's only once it has bottom lock, the
#: EKF's local position twenty-odd seconds after that -- so the scan has to run
#: well past the start of the recording to see them all. Reading the whole file
#: twice would double the slowest part of the extraction for no further gain.
_TYPE_SCAN_SECONDS = 300.0
_TYPE_SCAN_FRAMES = 400_000


def scan_types(path: Path | str, wanted: tuple[str, ...]) -> set[str]:
    """Which of ``wanted`` this tlog actually carries.

    A tlog has no channel list, so the only way to know is to look. The answer
    decides which source feeds altitude and speed for the whole dive, and
    getting it wrong lets two sources feed one column at once.
    """
    found: set[str] = set()
    remaining = set(wanted)
    try:
        conn = _mavutil().mavlink_connection(str(path))
    except Exception:
        return found

    first_t = None
    for i in range(_TYPE_SCAN_FRAMES):
        if not remaining:
            break
        try:
            msg = conn.recv_match(type=list(remaining), blocking=False)
        except Exception:
            break
        if msg is None:
            break
        mt = msg.get_type()
        if mt == "BAD_DATA":
            continue
        found.add(mt)
        remaining.discard(mt)

        t = getattr(msg, "_timestamp", 0.0)
        if t:
            if first_t is None:
                first_t = t
            elif t - first_t > _TYPE_SCAN_SECONDS:
                break
    return found
