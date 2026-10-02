"""
The DVL's own formats, and how they are kept without being changed.

Written against Water Linked's TCP JSON API `json_v3.3` (software 2.7.2,
`docs/dvl/dvl-json-protocol.md` in github.com/waterlinked/docs) and checked
against the public demo DVL, which ran 2.7.1 / `json_v3.2` on 1 October 2026.

Four things about the stream shape everything here, and each was observed
rather than assumed:

**Lines end in CR LF, not LF.** The documentation says "delimited by newline";
every one of 298 lines from the demo ended ``\\r\\n``. The raw file keeps
whichever arrived, and the index says which.

**The DVL parses everything it receives in one read as one document.** Six
commands written together got one answer and one ``"Invalid JSON"``. So
commands go one at a time, each waiting for its answer.

**Fields come and go between firmware releases.** `covariance` arrived in 2.1,
`velocity_water` in 2.7, the time API in 2.7.2, and the web GUI's own stream
carries fields the TCP stream does not. A flattener that dropped what it did
not expect would lose exactly the data a newer DVL added, so anything outside
the known schema goes into an ``extra_json`` column, whole.

**Transducer `id` is zero-based.** Mechanical drawings number the transducers
1 to 4; the protocol's `id` is that number minus one. Columns here are named by
`id` (`t0_` … `t3_`), because `id` is what is in the data.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

#: The DVL's TCP JSON server. Configurable on the DVL (its outputs page), so
#: the capture also reads the port the DVL reports and follows it.
TCP_PORT = 16171

#: The web GUI and its API.
WEB_PORT = 80

# --------------------------------------------------------------------------
#  What this program may say to a DVL
# --------------------------------------------------------------------------

#: The only commands this program ever sends. Each one asks; none of them
#: changes anything on the DVL. `get_version_info` and the two time commands
#: arrived in 2.7.2 -- an older DVL answers them with ``success: false``,
#: which is logged as the answer it is.
READ_ONLY_COMMANDS = frozenset({
    "get_config",
    "get_version_info",
    "get_time_status",
    "get_time_ntp",
})

#: Every command the protocol documents that changes the DVL, listed so that a
#: test can prove none of them is reachable. Calibrating the gyro, resetting
#: dead reckoning or triggering a ping mid-dive would each alter the very
#: data this program exists to record.
NEVER_SENT = (
    "set_config",
    "reset_dead_reckoning",
    "calibrate_gyro",
    "trigger_ping",
    "set_time_ntp",
    "set_time_manual",
    "force_sync_ntp",
)


class CommandRefused(ValueError):
    """Raised for any command outside `READ_ONLY_COMMANDS`."""


def command_bytes(name: str) -> bytes:
    """One command, encoded exactly as it goes on the wire.

    The only place in this package that builds a command. Anything not on the
    read-only list is refused here, so a mistake elsewhere cannot reach the
    socket.
    """
    if name not in READ_ONLY_COMMANDS:
        raise CommandRefused(f"{name!r} is not a read-only DVL command")
    return json.dumps({"command": name}, separators=(",", ":")).encode() + b"\n"


# --------------------------------------------------------------------------
#  Framing, without changing a byte
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One line of the stream, exactly as it arrived.

    `data` includes its terminator. `offset` is where its first byte sits in
    the capture's stream -- the raw file -- so the raw file and the index can
    always be lined up again.
    """

    data: bytes
    offset: int
    #: The receive call that completed the line, and the one it began in. A
    #: line that straddles two reads became usable at the second.
    chunk: int
    first_chunk: int
    #: "CRLF", "LF", or "" for a fragment cut off by a disconnection.
    terminator: str

    @property
    def body(self) -> bytes:
        """The line without its terminator."""
        if self.terminator == "CRLF":
            return self.data[:-2]
        if self.terminator == "LF":
            return self.data[:-1]
        return self.data


class LineSplitter:
    """Cuts a byte stream into lines and remembers where each one sits.

    Fed whatever each `recv` returned. Nothing is stripped, merged or decoded
    here: concatenating every `Line.data` it returns, in order, plus the
    fragment `flush` returns, gives back exactly the bytes that were fed.
    """

    def __init__(self, offset: int = 0) -> None:
        #: The stream offset of the next byte to arrive.
        self.offset = offset
        self._buf = bytearray()
        self._buf_offset = offset
        self._buf_chunk: int | None = None

    def feed(self, data: bytes, chunk: int) -> list[Line]:
        out: list[Line] = []
        if not data:
            return out
        if not self._buf:
            self._buf_offset = self.offset
            self._buf_chunk = chunk
        self._buf += data
        self.offset += len(data)
        start = 0
        while True:
            end = self._buf.find(b"\n", start)
            if end < 0:
                break
            piece = bytes(self._buf[start:end + 1])
            term = "CRLF" if piece.endswith(b"\r\n") else "LF"
            out.append(Line(piece, self._buf_offset + start, chunk,
                            self._buf_chunk if start == 0 else chunk, term))
            start = end + 1
        if start:
            del self._buf[:start]
            self._buf_offset += start
            # What is left began in this chunk.
            self._buf_chunk = chunk
        return out

    def flush(self) -> Line | None:
        """The unterminated tail, if any, at the end of a connection."""
        if not self._buf:
            return None
        line = Line(bytes(self._buf), self._buf_offset,
                    self._buf_chunk if self._buf_chunk is not None else -1,
                    self._buf_chunk if self._buf_chunk is not None else -1, "")
        self._buf.clear()
        self._buf_offset = self.offset
        self._buf_chunk = None
        return line

    @property
    def pending(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------
#  Parsing
# --------------------------------------------------------------------------


def parse(body: bytes) -> tuple[object | None, str]:
    """(the decoded JSON, an error). Never raises.

    `NaN` and `Infinity` are accepted, as Python's decoder does by default:
    the DVL stopped emitting them in 2.4.0, and a capture from an older DVL
    should still load rather than lose the line.
    """
    if not body.strip():
        return None, "empty line"
    try:
        return json.loads(body), ""
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"


def kind_of(obj) -> str:
    """What a decoded line is: "velocity", "position_local", "response", ...

    The `type` field when there is one, so a report type a newer DVL adds is
    named rather than lumped in with garbage.
    """
    if not isinstance(obj, dict):
        return "not_an_object"
    kind = obj.get("type")
    if isinstance(kind, str) and kind:
        return kind
    if "response_to" in obj:
        return "response"
    return "no_type"


#: Report types that are a velocity-and-transducer report. `velocity_water`
#: is the water-tracking variant (2.7 and later), with the same fields.
VELOCITY_TYPES = ("velocity", "velocity_water")


# --------------------------------------------------------------------------
#  Flattening into rows
# --------------------------------------------------------------------------

#: Booleans are written 1 / 0, as the other flight CSVs write them.
def cell(value):
    """One value as it goes into a CSV cell.

    Floats are left to `csv`, which writes the shortest text that reads back
    as the same float -- nothing is rounded. `None` is an empty cell.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)                     # "nan", "inf": kept, not blanked
    return value


def _num(value):
    """A number from the data, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _extras(obj: dict, known) -> str:
    rest = {k: v for k, v in obj.items() if k not in known}
    if not rest:
        return ""
    return json.dumps(rest, separators=(",", ":"), sort_keys=True)


#: The axes of the 3 x 3 covariance, in the order they are written.
_AXES = ("x", "y", "z")
COVARIANCE_COLUMNS = tuple(f"cov_{a}{b}" for a in _AXES for b in _AXES)


def _covariance(value) -> dict:
    out = {c: None for c in COVARIANCE_COLUMNS}
    if (isinstance(value, list) and len(value) == 3
            and all(isinstance(r, list) and len(r) == 3 for r in value)):
        for i, a in enumerate(_AXES):
            for j, b in enumerate(_AXES):
                out[f"cov_{a}{b}"] = _num(value[i][j])
    return out


#: Transducer ids the A50 uses.
TRANSDUCER_IDS = (0, 1, 2, 3)


def _transducers(items, fields: tuple[str, ...], valid_key: str) -> tuple[dict, list]:
    """Per-transducer columns, placed by `id`, and anything that would not place.

    A transducer with no id, an id outside 0-3, or an id seen twice is not
    guessed into a column -- it is returned whole, for `extra_json`.
    """
    out: dict = {}
    for tid in TRANSDUCER_IDS:
        for f in fields:
            out[f"t{tid}_{f}"] = None
        out[f"t{tid}_snr_db"] = None
    unplaced: list = []
    seen: set[int] = set()
    if not isinstance(items, list):
        return out, ([items] if items is not None else [])
    for item in items:
        tid = item.get("id") if isinstance(item, dict) else None
        if (isinstance(tid, bool) or not isinstance(tid, int)
                or tid not in TRANSDUCER_IDS or tid in seen):
            unplaced.append(item)
            continue
        seen.add(tid)
        for f in fields:
            v = item.get(valid_key if f == "valid" else f)
            out[f"t{tid}_{f}"] = v if isinstance(v, bool) else _num(v)
        rssi, nsd = _num(item.get("rssi")), _num(item.get("nsd"))
        if rssi is not None and nsd is not None:
            # Derived: signal over the transducer's own noise floor. Both are
            # dBm, so the difference is a ratio in dB.
            out[f"t{tid}_snr_db"] = rssi - nsd
        extra = {k: v for k, v in item.items()
                 if k not in ("id", valid_key, *fields)}
        if extra:
            unplaced.append({"id": tid, **extra})
    return out, unplaced


#: Per-transducer fields of the TCP report, in column order. `valid` is the
#: report's `beam_valid`.
TRANSDUCER_FIELDS = ("velocity", "distance", "rssi", "nsd", "valid")


def _transducer_columns(fields) -> list[str]:
    cols = []
    for tid in TRANSDUCER_IDS:
        cols += [f"t{tid}_{f}" for f in fields]
        cols.append(f"t{tid}_snr_db")
    return cols


#: What the DVL said in a velocity-and-transducer report, one column each.
VELOCITY_REPORT_COLUMNS = (
    "type", "format",
    "time_ms", "vx", "vy", "vz", "fom", "altitude",
    "velocity_valid", "status", "status_high_temperature",
    "time_of_validity_us", "time_of_transmission_us",
    *COVARIANCE_COLUMNS,
    *_transducer_columns(TRANSDUCER_FIELDS),
    "n_transducers",
    "extra_json",
)

_VELOCITY_KNOWN = {"type", "format", "time", "vx", "vy", "vz", "fom",
                   "altitude", "velocity_valid", "status", "time_of_validity",
                   "time_of_transmission", "covariance", "transducers"}


def flatten_velocity(obj: dict) -> dict:
    """A velocity-and-transducer report as one row. Nothing is dropped."""
    row = {
        "type": obj.get("type"),
        "format": obj.get("format"),
        "time_ms": _num(obj.get("time")),
        "vx": _num(obj.get("vx")),
        "vy": _num(obj.get("vy")),
        "vz": _num(obj.get("vz")),
        "fom": _num(obj.get("fom")),
        "altitude": _num(obj.get("altitude")),
        "velocity_valid": (obj.get("velocity_valid")
                           if isinstance(obj.get("velocity_valid"), bool) else None),
        "status": _num(obj.get("status")),
        "time_of_validity_us": _num(obj.get("time_of_validity")),
        "time_of_transmission_us": _num(obj.get("time_of_transmission")),
    }
    status = row["status"]
    # Bit 0 is the only bit the protocol defines: the DVL is hot and about to
    # shut itself down. The whole mask is kept in `status` regardless.
    row["status_high_temperature"] = (bool(int(status) & 1)
                                      if isinstance(status, int) else None)
    row.update(_covariance(obj.get("covariance")))
    beams, unplaced = _transducers(obj.get("transducers"),
                                   ("velocity", "distance", "rssi", "nsd", "valid"),
                                   "beam_valid")
    row.update(beams)
    items = obj.get("transducers")
    row["n_transducers"] = len(items) if isinstance(items, list) else None
    extra = {k: v for k, v in obj.items() if k not in _VELOCITY_KNOWN}
    if unplaced:
        extra["transducers_unplaced"] = unplaced
    row["extra_json"] = (json.dumps(extra, separators=(",", ":"), sort_keys=True)
                         if extra else "")
    return row


#: A dead-reckoning report, one column per field.
POSITION_REPORT_COLUMNS = (
    "type", "format", "ts", "x", "y", "z", "std", "roll", "pitch", "yaw",
    "status", "extra_json",
)

_POSITION_KNOWN = {"type", "format", "ts", "x", "y", "z", "std", "roll",
                   "pitch", "yaw", "status"}


def flatten_position(obj: dict) -> dict:
    row = {k: (obj.get(k) if k in ("type", "format") else _num(obj.get(k)))
           for k in POSITION_REPORT_COLUMNS if k != "extra_json"}
    row["extra_json"] = _extras(obj, _POSITION_KNOWN)
    return row


#: The web GUI's own velocity message (`/ws`, channel "velocity"). It is the
#: same measurement as the TCP report -- `time_of_validity` joins them -- but
#: carries three things the TCP report does not: whether the DVL is in the
#: middle of a periodic-cycling check (`carrying_out_periodic_cycling`),
#: which range configuration it is running (`run_config`), and whether it is
#: water tracking. It lacks the TCP report's status, format and transmission
#: time. Field names differ too (`std` for `fom`, `cov`, `is_valid`).
WS_VELOCITY_COLUMNS = (
    "time_ms", "time_of_validity_us", "vx", "vy", "vz", "std", "altitude",
    "velocity_valid", "carrying_out_periodic_cycling", "run_config",
    "is_watertracking",
    *COVARIANCE_COLUMNS,
    *_transducer_columns(TRANSDUCER_FIELDS),
    "n_transducers",
    "extra_json",
)

_WS_VELOCITY_KNOWN = {"time", "time_of_validity", "vx", "vy", "vz", "std",
                      "cov", "altitude", "transducers", "velocity_valid",
                      "carrying_out_periodic_cycling", "run_config",
                      "is_watertracking"}


def _flag(value):
    return value if isinstance(value, bool) else None


def flatten_ws_velocity(payload: dict) -> dict:
    row = {
        "time_ms": _num(payload.get("time")),
        "time_of_validity_us": _num(payload.get("time_of_validity")),
        "vx": _num(payload.get("vx")),
        "vy": _num(payload.get("vy")),
        "vz": _num(payload.get("vz")),
        "std": _num(payload.get("std")),
        "altitude": _num(payload.get("altitude")),
        "velocity_valid": _flag(payload.get("velocity_valid")),
        "carrying_out_periodic_cycling": _flag(
            payload.get("carrying_out_periodic_cycling")),
        "run_config": _num(payload.get("run_config")),
        "is_watertracking": _flag(payload.get("is_watertracking")),
    }
    row.update(_covariance(payload.get("cov")))
    beams, unplaced = _transducers(payload.get("transducers"),
                                   ("velocity", "distance", "rssi", "nsd", "valid"),
                                   "is_valid")
    # The web stream writes validity as 0/1 rather than true/false.
    for tid in TRANSDUCER_IDS:
        v = beams.get(f"t{tid}_valid")
        if isinstance(v, int) and not isinstance(v, bool):
            beams[f"t{tid}_valid"] = bool(v)
    row.update(beams)
    items = payload.get("transducers")
    row["n_transducers"] = len(items) if isinstance(items, list) else None
    extra = {k: v for k, v in payload.items() if k not in _WS_VELOCITY_KNOWN}
    if unplaced:
        extra["transducers_unplaced"] = unplaced
    row["extra_json"] = (json.dumps(extra, separators=(",", ":"), sort_keys=True)
                         if extra else "")
    return row


#: The web stream's other channels. Vectors are written to v0..v2; a scalar
#: to v0; anything else whole, to `value_json`.
WS_MOTION_COLUMNS = ("channel", "v0", "v1", "v2", "value_json")


def flatten_ws_motion(channel: str, payload) -> dict:
    row = {"channel": channel, "v0": None, "v1": None, "v2": None,
           "value_json": ""}
    if (isinstance(payload, list) and 1 <= len(payload) <= 3
            and all(_num(v) is not None for v in payload)):
        for i, v in enumerate(payload):
            row[f"v{i}"] = v
    elif _num(payload) is not None:
        row["v0"] = payload
    else:
        row["value_json"] = json.dumps(payload, separators=(",", ":"),
                                       sort_keys=True)
    return row


#: What the web stream's channels mean, for the schema and the tab.
WS_CHANNELS = {
    "velocity": "the velocity report, with the web stream's own extra fields",
    "roll_pitch_yaw": "orientation from the DVL's IMU/AHRS, degrees [roll, pitch, yaw]",
    "position_local": "dead-reckoned position, metres [x, y, z]",
    "position_local_std": "dead-reckoned position standard deviation, metres",
    "fusion_velocity": "the dead-reckoning filter's velocity, m/s [x, y, z]",
    "fusion_velocity_std": "the dead-reckoning filter's velocity standard deviation, m/s",
    "fusion_reset": "dead reckoning was reset",
}


# --------------------------------------------------------------------------
#  The beams
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Beam:
    """Where one transducer points, in the DVL's own frame."""

    id: int
    number: int
    #: Clockwise from the DVL's +x (forward, the LED; away from the cable),
    #: looking down. The diagonals, because the A50's four beams are.
    azimuth_deg: float


#: From Water Linked's transducer-numbering drawing of the A50's face
#: (`WL-21035-3_DVL-A50_Front_1600_transducers_crop.jpg` in their docs):
#: the cable -- the DVL's aft, -x -- is at the top of the picture, and the
#: picture is the face seen from below, so its right is starboard. Numbers 1
#: and 2 are beside the cable, 3 and 4 opposite it.
#:
#: That puts id 0 (transducer 1) aft-starboard, id 1 aft-port, id 2
#: forward-port and id 3 forward-starboard. It agrees with Keenan Johnson's
#: beam-splitter code and not with his technical_details.md, which disagree
#: with each other. **It is derived from a photograph, and the bench checklist
#: asks for it to be confirmed by covering one transducer.**
BEAMS = (
    Beam(0, 1, 135.0),
    Beam(1, 2, 225.0),
    Beam(2, 3, 315.0),
    Beam(3, 4, 45.0),
)

_COMPASS = {45: "forward-starboard", 135: "aft-starboard",
            225: "aft-port", 315: "forward-port",
            0: "forward", 90: "starboard", 180: "aft", 270: "port"}


def beam_azimuth_on_vehicle(beam: Beam, mounting_offset_deg: float | None) -> float:
    """Where a beam points on the vehicle, given the DVL's mounting offset.

    The offset is "the clockwise angle from the forward axis of the vehicle to
    the forward axis of the DVL" (Water Linked, axes.md), so a beam's bearing on
    the vehicle is its bearing on the DVL plus the offset.
    """
    return (beam.azimuth_deg + (mounting_offset_deg or 0.0)) % 360.0


def beam_position(beam: Beam, mounting_offset_deg: float | None) -> str:
    """"aft-starboard" and the like, or a bearing when it is not a diagonal."""
    az = beam_azimuth_on_vehicle(beam, mounting_offset_deg)
    nearest = round(az / 45.0) * 45 % 360
    if abs(az - nearest) < 1.0 and nearest in _COMPASS:
        return _COMPASS[nearest]
    return f"{az:.0f}° from forward"


# --------------------------------------------------------------------------
#  Range modes
# --------------------------------------------------------------------------

#: The A50's range modes, from Water Linked's range-mode page: lowest and
#: highest altitude (m) and the update rate it quotes. Shown beside the rate
#: actually achieved, so a slow stream can be told from a deep one.
RANGE_MODES = {
    0: (0.05, 0.6, "15 Hz"),
    1: (0.3, 3.0, "10 Hz"),
    2: (1.5, 14.0, "5–6 Hz"),
    3: (7.7, 36.0, "7–8 Hz"),
    4: (15.0, None, "2–4 Hz"),
}
