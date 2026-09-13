"""
Reading BlueOS .mcap recordings.

One pass per mcap pulls out both halves of what we need:

  * the forward camera, as an H.264 elementary stream plus a per-frame
    timestamp index, and
  * the MAVLink telemetry, as a long-format CSV.

Two things about this format are easy to get wrong and expensive to discover
late, so they are handled explicitly here:

  * The mcap's own ``log_time`` is written in bursts and is NOT the video frame
    time. The real time is the ``timestamp`` field inside each
    ``foxglove.CompressedVideo`` message.
  * Topics are namespaced by MAVLink system/component id
    (``mavlink/1/1/VFR_HUD``), and a given message type can appear under several
    ids. We match on the message *type* and prefer the autopilot (1/1), rather
    than hard-coding topic strings that differ between vehicles.

Multiple mcaps per flight are normal -- BlueOS rolls a new file whenever
recording is restarted -- so everything here accepts a list and merges on the
absolute epoch timeline.
"""

from __future__ import annotations

import csv
import io
import json
import re
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mcap.reader import make_reader

ProgressCB = Callable[[float, str], None]

VIDEO_SCHEMA = "foxglove.CompressedVideo"
FIREHOSE_TOPIC = "mavlink/out"          # duplicates everything; skip it

#: message type -> fields to keep. Chosen to cover the overlay, the 1 Hz CSV,
#: and the GPS / DVL / EKF diagnostics that matter when a dive misbehaves.
WANTED: dict[str, tuple[str, ...]] = {
    # attitude & motion
    "ATTITUDE":            ("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"),
    "AHRS2":               ("roll", "pitch", "yaw", "altitude", "lat", "lng"),
    "VFR_HUD":             ("heading", "groundspeed", "airspeed", "alt", "climb", "throttle"),
    "LOCAL_POSITION_NED":  ("x", "y", "z", "vx", "vy", "vz"),
    # depth / altitude
    "SCALED_PRESSURE":     ("press_abs", "temperature"),
    "SCALED_PRESSURE2":    ("press_abs", "temperature"),
    "RANGEFINDER":         ("distance", "voltage"),
    "DISTANCE_SENSOR":     ("current_distance", "id", "orientation"),
    # position / GPS / USBL
    "GPS_RAW_INT":         ("lat", "lon", "alt", "eph", "epv", "vel", "cog",
                            "satellites_visible", "fix_type"),
    "GLOBAL_POSITION_INT": ("lat", "lon", "alt", "relative_alt", "vx", "vy", "vz", "hdg"),
    # DVL
    "VISION_POSITION_DELTA": ("time_delta_usec", "confidence"),
    # EKF
    "EKF_STATUS_REPORT":   ("velocity_variance", "pos_horiz_variance", "pos_vert_variance",
                            "compass_variance", "terrain_alt_variance", "flags"),
    "VIBRATION":           ("vibration_x", "vibration_y", "vibration_z"),
    # power
    "BATTERY_STATUS":      ("current_battery", "current_consumed", "energy_consumed"),
    "SYS_STATUS":          ("load", "voltage_battery", "current_battery", "battery_remaining"),
    "POWER_STATUS":        ("Vcc", "Vservo"),
    # state
    "HEARTBEAT":           ("custom_mode",),
    "SYSTEM_TIME":         ("time_unix_usec",),
}

#: These carry a value we synthesise rather than copy verbatim.
SPECIAL = ("NAMED_VALUE_FLOAT", "STATUSTEXT", "BATTERY_STATUS", "HEARTBEAT",
           "DISTANCE_SENSOR")

#: Minimum seconds between kept samples, per message type.
#:
#: ArduSub emits some streams very fast -- AHRS2 and SCALED_PRESSURE2 arrive at
#: ~300 Hz, which is 320,000 messages each across a 36-minute dive. Nothing
#: downstream needs that: the CSV is 1 Hz and the overlay redraws at 6 Hz. The
#: interval is checked BEFORE the JSON is parsed, so decimated messages cost
#: almost nothing -- which is what makes extraction fast rather than merely
#: producing a smaller file.
DEFAULT_MIN_INTERVAL = 0.1          # 10 Hz ceiling, comfortably above the overlay
MIN_INTERVAL: dict[str, float] = {
    "STATUSTEXT": 0.0,              # events; never drop one
    "HEARTBEAT": 0.0,               # mode changes are rare and matter
    "NAMED_VALUE_FLOAT": 0.0,       # already slow (~2 Hz) and drives the sync check
    "DISTANCE_SENSOR": 0.0,         # interleaved per sensor id; decimating loses beams
    "SYSTEM_TIME": 1.0,
}


def _min_interval(mt: str) -> float:
    return MIN_INTERVAL.get(mt, DEFAULT_MIN_INTERVAL)


@dataclass
class VideoStreamInfo:
    frames: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    width: int | None = None
    height: int | None = None
    resolutions: set[str] = field(default_factory=set)


@dataclass
class ExtractResult:
    cache_dir: Path
    mcaps: list[Path]
    h264_path: Path
    frames_csv: Path
    telemetry_csv: Path
    video: VideoStreamInfo
    t_start: float | None = None      # epoch seconds, first telemetry sample
    t_end: float | None = None
    telemetry_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "mcaps": [str(m) for m in self.mcaps],
            "video_frames": self.video.frames,
            "video_first_ts": self.video.first_ts,
            "video_last_ts": self.video.last_ts,
            "width": self.video.width,
            "height": self.video.height,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "telemetry_rows": self.telemetry_rows,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
#  CDR / H.264 helpers
# --------------------------------------------------------------------------


def parse_compressed_video(b: bytes) -> tuple[float, bytes]:
    """Decode a CDR-encoded foxglove.CompressedVideo -> (epoch_seconds, payload)."""
    o = 4                                            # CDR encapsulation header
    sec, nsec = struct.unpack_from("<iI", b, o); o += 8
    ln = struct.unpack_from("<I", b, o)[0]; o += 4   # frame_id
    o += ln
    o = (o + 3) // 4 * 4
    dlen = struct.unpack_from("<I", b, o)[0]; o += 4
    return sec + nsec / 1e9, b[o:o + dlen]


#: VCL NAL types -- the ones that actually carry picture data.
_VCL = frozenset((1, 2, 3, 4, 5))


def _nal_iter(data: bytes):
    """Yield (header_index, nal_type) for each NAL unit in an Annex B buffer.

    Uses ``bytes.find`` rather than stepping a Python index. Across a dive this
    scans ~2 GB of bitstream, and a byte-at-a-time loop dominated extraction
    time; find() does the same work in C. The 3-byte start code is a suffix of
    the 4-byte one, so searching for it alone finds both.
    """
    end = len(data)
    pos = 0
    while True:
        i = data.find(b"\x00\x00\x01", pos, end)
        if i < 0:
            return
        h = i + 3
        if h >= end:
            return
        yield h, data[h] & 0x1F
        pos = h


def has_idr(data: bytes) -> bool:
    """True if this access unit opens on an IDR slice.

    Stops at the first picture NAL: parameter sets precede it, so once a slice
    is seen the answer is settled and the rest of the frame need not be walked.
    """
    for _h, t in _nal_iter(data):
        if t in _VCL:
            return t == 5
    return False


def sps_resolution(data: bytes) -> tuple[int, int] | None:
    """Width/height from the first SPS, so a mid-flight resolution change is
    detected rather than silently corrupting the merged stream."""
    for h, t in _nal_iter(data):
        if t != 7:
            continue
        try:
            return _parse_sps(data[h + 1:h + 40])
        except Exception:
            return None
    return None


class _BitReader:
    def __init__(self, data: bytes):
        # strip emulation prevention bytes
        out = bytearray()
        i = 0
        while i < len(data):
            if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
                out += data[i:i + 2]
                i += 3
            else:
                out.append(data[i])
                i += 1
        self.d = bytes(out)
        self.pos = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.d[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise ValueError("bad exp-golomb")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _parse_sps(rbsp: bytes) -> tuple[int, int]:
    r = _BitReader(rbsp)
    profile_idc = r.u(8)
    r.u(8)                     # constraint flags + reserved
    r.u(8)                     # level_idc
    r.ue()                     # sps_id
    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = r.ue()
        if chroma_format_idc == 3:
            r.u(1)
        r.ue(); r.ue(); r.u(1)
        if r.u(1):             # seq_scaling_matrix_present
            for i in range(8 if chroma_format_idc != 3 else 12):
                if r.u(1):
                    last, nxt = 8, 8
                    for _ in range(min(16 if i < 6 else 64, 64)):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    r.ue()                     # log2_max_frame_num_minus4
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.u(1); r.se(); r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()                     # max_num_ref_frames
    r.u(1)
    w_mbs = r.ue() + 1
    h_map = r.ue() + 1
    frame_mbs_only = r.u(1)
    if not frame_mbs_only:
        r.u(1)
    r.u(1)                     # direct_8x8_inference
    crop_l = crop_r = crop_t = crop_b = 0
    if r.u(1):                 # frame_cropping
        crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()
    sub_w = 2 if chroma_format_idc in (1, 2) else 1
    sub_h = 2 if chroma_format_idc == 1 else 1
    width = w_mbs * 16 - sub_w * (crop_l + crop_r)
    height = (2 - frame_mbs_only) * h_map * 16 - sub_h * (crop_t + crop_b)
    return width, height


# --------------------------------------------------------------------------
#  Channel selection
# --------------------------------------------------------------------------


def _msg_type(topic: str) -> str | None:
    """'mavlink/1/1/VFR_HUD' -> 'VFR_HUD'. Enum sub-topics like
    '.../GPS_RAW_INT/fix_type' are skipped -- the value is already inside the
    parent message."""
    if not topic.startswith("mavlink/") or topic == FIREHOSE_TOPIC:
        return None
    parts = topic.split("/")
    if len(parts) != 4:
        return None
    return parts[3]


def _sysid_rank(topic: str) -> tuple[int, int]:
    """Prefer the autopilot (system 1, component 1) when a message type appears
    under several ids -- DISTANCE_SENSOR shows up as both 1/1 and 255/0."""
    parts = topic.split("/")
    try:
        sys_id, comp = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return (9, 9)
    return (0 if (sys_id, comp) == (1, 1) else 1, sys_id)


def select_channels(reader) -> tuple[dict[str, str], list[str]]:
    """Map message type -> chosen topic, plus the video topic list."""
    summary = reader.get_summary()
    if summary is None:
        return {}, []

    counts: dict[int, int] = {}
    if summary.statistics:
        counts = dict(summary.statistics.channel_message_counts)

    by_type: dict[str, list[tuple[tuple[int, int], int, str]]] = {}
    video_topics: list[str] = []
    for ch in summary.channels.values():
        schema = summary.schemas.get(ch.schema_id)
        if schema and VIDEO_SCHEMA in schema.name:
            video_topics.append(ch.topic)
            continue
        mt = _msg_type(ch.topic)
        if mt is None:
            continue
        if mt in WANTED or mt in SPECIAL:
            by_type.setdefault(mt, []).append(
                (_sysid_rank(ch.topic), -counts.get(ch.id, 0), ch.topic)
            )

    chosen = {mt: sorted(v)[0][2] for mt, v in by_type.items()}
    return chosen, sorted(video_topics)


#: How many messages of a truncated file to read before deciding which channels
#: to keep. BlueOS declares its channels in the opening chunks, so this only has
#: to be comfortably past them -- it is not a sample of the whole recording.
_DISCOVERY_MESSAGES = 200_000


def select_channels_streaming(
    path: Path, health: McapHealth, limit: int = _DISCOVERY_MESSAGES
) -> tuple[dict[str, str], list[str]]:
    """`select_channels` for a recording that has no index to read.

    Same ranking as the indexed path -- prefer the autopilot, then the busiest
    channel -- learned from a bounded first pass instead of the summary. Doing
    it up front rather than as topics appear matters: a better channel
    discovered late would already have had the worse one's rows written.
    """
    from mcap.reader import NonSeekingReader

    schema_of: dict[str, str] = {}
    counts: dict[str, int] = {}
    with open_repaired(path, health) as f:
        for i, (schema, channel, _msg) in enumerate(
                NonSeekingReader(f).iter_messages()):
            schema_of.setdefault(channel.topic, schema.name if schema else "")
            counts[channel.topic] = counts.get(channel.topic, 0) + 1
            if i >= limit:
                break

    by_type: dict[str, list[tuple[tuple[int, int], int, str]]] = {}
    video_topics: list[str] = []
    for topic, sname in schema_of.items():
        if VIDEO_SCHEMA in sname:
            video_topics.append(topic)
            continue
        mt = _msg_type(topic)
        if mt is None or (mt not in WANTED and mt not in SPECIAL):
            continue
        by_type.setdefault(mt, []).append(
            (_sysid_rank(topic), -counts.get(topic, 0), topic)
        )
    chosen = {mt: sorted(v)[0][2] for mt, v in by_type.items()}
    return chosen, sorted(video_topics)


# --------------------------------------------------------------------------
#  Extraction
# --------------------------------------------------------------------------


def extract(
    mcaps: Sequence[Path],
    cache_dir: Path,
    *,
    progress: ProgressCB | None = None,
    force: bool = False,
) -> ExtractResult:
    """Extract every mcap into one merged cache directory.

    mcaps are processed in chronological order (by first message time) so the
    concatenated H.264 stream and the telemetry share a single, monotonic
    epoch timeline.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    h264_path = cache_dir / "rov_raw.h264"
    frames_csv = cache_dir / "rov_frames.csv"
    telem_csv = cache_dir / "telemetry.csv"
    marker = cache_dir / "extract.json"

    mcaps = [Path(m) for m in mcaps]
    if marker.is_file() and not force:
        try:
            prev = json.loads(marker.read_text())
            if prev.get("mcaps") == [str(m) for m in mcaps] and frames_csv.is_file():
                if progress:
                    progress(1.0, "telemetry cache hit")
                vi = VideoStreamInfo(
                    frames=prev["video_frames"], first_ts=prev["video_first_ts"],
                    last_ts=prev["video_last_ts"], width=prev["width"],
                    height=prev["height"],
                )
                return ExtractResult(
                    cache_dir, mcaps, h264_path, frames_csv, telem_csv, vi,
                    prev.get("t_start"), prev.get("t_end"),
                    prev.get("telemetry_rows", 0), prev.get("warnings", []),
                )
        except Exception:
            pass  # unreadable marker: just re-extract

    ordered, warnings = select_mcaps(mcaps)
    vi = VideoStreamInfo()
    if not ordered:
        raise ValueError(
            "none of the .mcap files could be read:\n  "
            + "\n  ".join(warnings)
        )
    t_start = t_end = None
    n_rows = 0
    total_bytes = sum(m.stat().st_size for m in ordered) or 1
    done_bytes = 0

    with open(h264_path, "wb") as fh264, \
         open(frames_csv, "w", newline="") as ff, \
         open(telem_csv, "w", newline="") as ft:

        fw = csv.writer(ff)
        fw.writerow(["i", "ts", "nbytes", "byte_offset", "is_idr", "source"])
        tw = csv.writer(ft)
        tw.writerow(["t", "field", "value", "sval"])

        frame_i = 0
        byte_off = 0

        for mi, mpath in enumerate(ordered):
            if progress:
                progress(done_bytes / total_bytes,
                         f"reading {mpath.name} ({mi + 1}/{len(ordered)})")
            # A recording the vehicle never closed has no index, so it has to be
            # read straight through. Everything else is unchanged: the reader is
            # swapped, not the loop.
            health = None
            try:
                health = scan_health(mpath)
            except Exception:
                pass
            truncated = bool(health and health.recoverable)
            if truncated:
                warnings.append(
                    f"{mpath.name}: the recorder never closed this file "
                    f"(power lost mid-dive?). Reading it anyway -- the "
                    f"last {health.lost_bytes:,} bytes of "
                    f"{health.size / 1e9:.2f} GB were never written."
                )

            try:
                f = (open_repaired(mpath, health) if truncated
                     else open(mpath, "rb"))
                with f:
                    if truncated:
                        from mcap.reader import NonSeekingReader
                        reader = NonSeekingReader(f)
                        chosen, video_topics = select_channels_streaming(
                            mpath, health)
                        if not video_topics:
                            warnings.append(f"{mpath.name}: no video stream")
                        # NonSeekingReader has no topic filter, so the loop
                        # below skips what we did not choose.
                        keep = set(chosen.values()) | set(video_topics)
                        topics = None
                        if not keep:
                            warnings.append(
                                f"{mpath.name}: nothing recognisable, skipped")
                            done_bytes += mpath.stat().st_size
                            continue
                    else:
                        keep = None
                        reader = make_reader(f)
                        chosen, video_topics = select_channels(reader)
                        if not video_topics:
                            warnings.append(f"{mpath.name}: no video stream")
                        topics = list(chosen.values()) + video_topics
                        if not topics:
                            warnings.append(
                                f"{mpath.name}: nothing recognisable, skipped")
                            done_bytes += mpath.stat().st_size
                            continue

                    topic_type = {v: k for k, v in chosen.items()}
                    last_kept: dict[str, float] = {}
                    nread = 0

                    # How many messages we expect to iterate, so progress advances
                    # smoothly *within* a file. Without this a single-mcap flight --
                    # the common case -- would sit at one fraction for the whole of
                    # the longest stage and look hung.
                    # A truncated file has no summary to count from, so estimate
                    # from its chunks -- the bar only needs to move smoothly,
                    # not to be exact.
                    summary = None if truncated else reader.get_summary()
                    expected = (health.chunks * 300) if truncated else 0
                    if summary and summary.statistics:
                        per_ch = dict(summary.statistics.channel_message_counts)
                        want = set(topics)
                        expected = sum(
                            per_ch.get(ch.id, 0)
                            for ch in summary.channels.values() if ch.topic in want
                        )
                    expected = max(1, expected)
                    size = mpath.stat().st_size
                    stream = (reader.iter_messages() if truncated
                              else reader.iter_messages(topics=topics))
                    for _schema, channel, message in stream:
                        if keep is not None and channel.topic not in keep:
                            continue
                        nread += 1
                        if nread % 20000 == 0 and progress:
                            within = min(1.0, nread / expected)
                            frac = (done_bytes + size * within) / total_bytes
                            progress(min(0.99, frac),
                                     f"{mpath.name}: {nread:,}/{expected:,} messages")

                        if channel.topic in video_topics:
                            ts, data = parse_compressed_video(message.data)
                            if vi.width is None:
                                if (res := sps_resolution(data)) is not None:
                                    vi.width, vi.height = res
                                    vi.resolutions.add(f"{res[0]}x{res[1]}")
                            fh264.write(data)
                            fw.writerow([frame_i, f"{ts:.9f}", len(data), byte_off,
                                         int(has_idr(data)), mpath.name])
                            byte_off += len(data)
                            frame_i += 1
                            vi.first_ts = ts if vi.first_ts is None else min(vi.first_ts, ts)
                            vi.last_ts = ts if vi.last_ts is None else max(vi.last_ts, ts)
                            continue

                        mt = topic_type.get(channel.topic)
                        if mt is None:
                            continue
                        t = message.log_time / 1e9
                        t_start = t if t_start is None else min(t_start, t)
                        t_end = t if t_end is None else max(t_end, t)

                        # decimate before parsing -- json.loads on 300 Hz streams is
                        # what makes extraction slow, not the writing
                        iv = _min_interval(mt)
                        if iv > 0.0:
                            if t - last_kept.get(mt, -1e9) < iv:
                                continue
                            last_kept[mt] = t

                        n_rows += _write_telemetry(tw, mt, t, message.data)

            except Exception as ex:
                # a file that fails partway still leaves whatever it wrote;
                # report it and carry on with the remaining recordings
                warnings.append(
                    f"{mpath.name} stopped early: {type(ex).__name__}: "
                    f"{str(ex).splitlines()[0][:100]}"
                )
            done_bytes += mpath.stat().st_size

        vi.frames = frame_i

    if len(vi.resolutions) > 1:
        warnings.append(
            "ROV camera resolution changes mid-flight "
            f"({', '.join(sorted(vi.resolutions))}); the inset may be unreliable"
        )

    res = ExtractResult(cache_dir, ordered, h264_path, frames_csv, telem_csv, vi,
                        t_start, t_end, n_rows, warnings)
    marker.write_text(json.dumps(res.to_json(), indent=2))
    if progress:
        progress(1.0, f"extracted {vi.frames:,} frames, {n_rows:,} telemetry rows")
    return res


def _write_telemetry(tw, mt: str, t: float, raw: bytes) -> int:
    try:
        m = json.loads(raw)["message"]
    except Exception:
        return 0
    ts = f"{t:.6f}"
    n = 0

    if mt == "NAMED_VALUE_FLOAT":
        tw.writerow([ts, f"NVF.{m.get('name', '?')}", m.get("value", ""), ""])
        return 1
    if mt == "STATUSTEXT":
        tw.writerow([ts, "STATUSTEXT", "", m.get("text", "")])
        return 1
    if mt == "HEARTBEAT":
        tw.writerow([ts, "HEARTBEAT.custom_mode", m.get("custom_mode", ""), ""])
        tw.writerow([ts, "HEARTBEAT.base_mode", "", str(m.get("base_mode", ""))])
        return 2
    if mt == "DISTANCE_SENSOR":
        # several sensors share this message; key each by its id
        sid = m.get("id", 0)
        orient = m.get("orientation", {})
        orient = orient.get("type", "") if isinstance(orient, dict) else str(orient)
        tw.writerow([ts, f"DISTANCE_SENSOR.{sid}.current_distance",
                     m.get("current_distance", ""), orient])
        return 1
    if mt == "BATTERY_STATUS":
        v = (m.get("voltages") or [None])[0]
        if v is not None and v != 65535:
            tw.writerow([ts, "BATTERY_STATUS.voltage_mv", v, ""])
            n += 1

    # MAVLink's RANGEFINDER carries no status field, so a lost bottom lock
    # arrives as distance 0.0 -- indistinguishable from a reading except that
    # it is not one. The dataflash log, which does have a status field, shows
    # these are NoData for about one sample in eight during a transect. Writing
    # them through puts "ALT 0.00 m" on the imagery.
    if mt == "RANGEFINDER" and not m.get("distance"):
        return n

    for k in WANTED.get(mt, ()):
        if k not in m:
            continue
        val = m[k]
        if isinstance(val, dict):                 # enums arrive as {"type": "..."}
            tw.writerow([ts, f"{mt}.{k}", "", val.get("type", "")])
        elif isinstance(val, (list, tuple)):
            continue
        elif isinstance(val, str):
            tw.writerow([ts, f"{mt}.{k}", "", val])
        else:
            tw.writerow([ts, f"{mt}.{k}", val, ""])
        n += 1
    return n


#: A recording separated from the rest of the flight by more than this is
#: treated as a stray file that happens to share the folder, not part of the dive.
STRAY_GAP_HOURS = 12.0


# --------------------------------------------------------------------------
#  Recovering a recording the vehicle never closed
# --------------------------------------------------------------------------
#
# BlueOS writes each chunk header with a placeholder length and backfills it
# when the chunk closes, then writes DATA_END, the summary and a footer. Lose
# power mid-dive and the file ends with a chunk header whose length is still
# 0xFFFF_FFFF_FFFF_FFFF and no footer at all.
#
# Every reader then refuses the whole file, because the index it wants lives at
# the end. But the data before that point is intact -- on the recording this was
# written for, 100% of 4.73 GB was valid and only the last 3 kB was the stub.
# Rather than lose an hour of a dive to three missing kilobytes, the good extent
# is found and the missing tail is supplied on the fly. The file on disk is
# opened read-only and never altered.

MCAP_MAGIC = b"\x89MCAP0\r\n"
_MAX_RECORD = 2 ** 32
_OP_DATA_END, _OP_FOOTER, _OP_CHUNK = 15, 2, 6


@dataclass
class McapHealth:
    """What a linear pass over the record headers found."""

    good_end: int                 # byte offset just past the last valid record
    size: int
    complete: bool                # has a proper footer and end magic
    first_time: float | None = None
    last_time: float | None = None
    chunks: int = 0

    @property
    def recoverable(self) -> bool:
        return not self.complete and self.chunks > 0

    @property
    def lost_bytes(self) -> int:
        return max(0, self.size - self.good_end)


def scan_health(path: Path) -> McapHealth:
    """Walk the record headers, seeking rather than reading the payloads.

    Cheap even on multi-gigabyte files -- nine bytes are read per record.
    Chunk headers carry their own message time range, so the span comes out of
    the same pass without decompressing anything.
    """
    path = Path(path)
    size = path.stat().st_size
    pos = len(MCAP_MAGIC)
    first = last = None
    chunks = 0
    with open(path, "rb") as f:
        if f.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
            return McapHealth(good_end=0, size=size, complete=False)
        while pos + 9 <= size:
            f.seek(pos)
            hdr = f.read(9)
            if len(hdr) < 9:
                break
            op = hdr[0]
            ln = int.from_bytes(hdr[1:], "little")
            if op == 0 or op > 15 or ln > _MAX_RECORD or pos + 9 + ln > size:
                break
            if op == _OP_CHUNK:
                # message_start_time, message_end_time: the first two fields
                head = f.read(16)
                if len(head) == 16:
                    a, b = struct.unpack("<QQ", head)
                    if a:
                        first = a / 1e9 if first is None else first
                        last = b / 1e9
                chunks += 1
            pos += 9 + ln
    # The closing magic is not a record, so a healthy file always ends with
    # exactly those eight bytes left unwalked. Requiring pos == size instead
    # would call every intact recording truncated -- and then "repair" it by
    # appending a second footer to one it already had.
    complete = False
    if size - pos == len(MCAP_MAGIC):
        with open(path, "rb") as f:
            f.seek(pos)
            complete = f.read(len(MCAP_MAGIC)) == MCAP_MAGIC
    return McapHealth(good_end=size if complete else pos, size=size,
                      complete=complete, first_time=first, last_time=last,
                      chunks=chunks)


def _synthetic_tail() -> bytes:
    """DATA_END, an empty summary, and a footer -- what the recorder never wrote."""
    out = bytearray()
    out += bytes([_OP_DATA_END]) + struct.pack("<Q", 4) + struct.pack("<I", 0)
    footer = struct.pack("<QQI", 0, 0, 0)      # no summary, no offsets, crc 0
    out += bytes([_OP_FOOTER]) + struct.pack("<Q", len(footer)) + footer
    out += MCAP_MAGIC
    return bytes(out)


class _RepairedStream(io.RawIOBase):
    """The valid bytes of a truncated mcap, followed by a synthetic tail.

    Sequential only, which is all a non-seeking reader needs, and it means no
    multi-gigabyte copy has to be made to read a damaged recording.
    """

    def __init__(self, path: Path, good_end: int):
        self._f = open(path, "rb")
        self._end = int(good_end)
        self._tail = _synthetic_tail()
        self._pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        want = len(b)
        if self._pos < self._end:
            data = self._f.read(min(want, self._end - self._pos))
        else:
            off = self._pos - self._end
            data = self._tail[off:off + want]
        b[:len(data)] = data
        self._pos += len(data)
        return len(data)

    def close(self) -> None:
        try:
            self._f.close()
        finally:
            super().close()


def open_repaired(path: Path, health: McapHealth):
    """A buffered stream over a truncated recording, ready for a linear read."""
    return io.BufferedReader(_RepairedStream(Path(path), health.good_end),
                             buffer_size=1 << 20)


@dataclass
class McapInfo:
    path: Path
    start: float | None = None
    end: float | None = None
    error: str | None = None
    #: True when the times came from the filename rather than the file, because
    #: the file is still a cloud placeholder.
    estimated: bool = False
    cloud: bool = False
    #: The recorder never closed this file, but its data is readable.
    truncated: bool = False
    health: McapHealth | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.start is not None


#: BlueOS names recordings ``recorder_YYYYMMDD_HHMMSS`` in UTC.
_NAME_TIME = re.compile(r"(\d{8})_(\d{6})")


def name_start_utc(path: Path) -> float | None:
    """A recording's start time, from its name alone.

    Worth having because it needs no read at all: a file still in the cloud can
    be judged irrelevant and skipped, instead of forcing gigabytes over the
    network just to discover it covers the wrong hour.
    """
    m = _NAME_TIME.search(Path(path).stem)
    if not m:
        return None
    try:
        return datetime.strptime(
            m.group(1) + m.group(2), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def probe_mcaps(
    mcaps: Sequence[Path], *, open_cloud: bool = False
) -> list[McapInfo]:
    """Read each file's summary, tolerating ones that will not open.

    A folder can contain a recording from another day, or a truncated file from
    a crashed session. Neither should take the whole run down with it.

    Cloud placeholders are timed from their names rather than opened: reading
    the summary means touching the file, and Dropbox answers that by
    downloading the whole thing.
    """
    from .discovery import is_cloud_placeholder

    out: list[McapInfo] = []
    for m in mcaps:
        info = McapInfo(Path(m))
        info.cloud = is_cloud_placeholder(info.path)
        if info.cloud and not open_cloud:
            info.start = name_start_utc(info.path)
            info.estimated = True
            if info.start is None:
                info.error = "still in the cloud, and its name carries no time"
            out.append(info)
            continue
        try:
            with open(m, "rb") as f:
                s = make_reader(f).get_summary()
            if s and s.statistics and s.statistics.message_start_time:
                info.start = s.statistics.message_start_time / 1e9
                info.end = s.statistics.message_end_time / 1e9
            else:
                raise ValueError("no summary")
        except Exception as ex:
            # No usable index. Before writing the recording off, look at what is
            # actually in it: a dive cut short by a power loss still holds all
            # its telemetry, and refusing the file loses the whole flight.
            first_err = f"{type(ex).__name__}: {ex}".split("\n")[0][:120]
            try:
                h = scan_health(info.path)
            except Exception:
                h = None
            if h is not None and h.recoverable and h.first_time:
                info.start, info.end = h.first_time, h.last_time
                info.health, info.truncated = h, True
            else:
                info.error = first_err
        out.append(info)
    return out


#: How long a recording is assumed to run when its end is unknown, used only
#: for a placeholder whose successor cannot bound it.
_ASSUMED_RUN_S = 4 * 3600


def select_for_windows(
    mcaps: Sequence[Path],
    windows: Sequence[tuple[float, float]],
    *,
    margin_s: float = 120.0,
) -> tuple[list[Path], list[McapInfo], list[str]]:
    """The recordings that actually overlap the work, plus what was skipped.

    A day of testing and rebooting leaves a folder full of recordings. Merging
    all of them stretches one shared timeline across hours of irrelevant data,
    and -- worse -- requires every one of them to be downloaded first. Only the
    recordings covering the transects are needed.

    Returns (chosen, skipped_infos, warnings).
    """
    infos = probe_mcaps(mcaps)
    warnings = [f"skipping {i.path.name}: {i.error}" for i in infos if i.error]
    # Say this before the extraction rather than after it: an operator who sees
    # a short transect wants to know the recorder died, not to wonder whether
    # the times were typed wrong.
    warnings += [
        f"{i.path.name} was never closed by the recorder (power lost "
        f"mid-dive?) -- reading it anyway; the last "
        f"{i.health.lost_bytes:,} bytes are missing"
        for i in infos if i.truncated and i.health
    ]
    good = sorted((i for i in infos if i.usable), key=lambda i: i.start or 0.0)
    if not good or not windows:
        return [i.path for i in good], [], warnings

    # A recording whose end is unknown is bounded by the next one starting:
    # the recorder only restarts after stopping.
    for a, b in zip(good, good[1:], strict=False):   # pairwise: the last has no successor
        if a.end is None and a.start is not None:
            a.end = b.start
    if good and good[-1].end is None and good[-1].start is not None:
        good[-1].end = good[-1].start + _ASSUMED_RUN_S

    lo = min(w[0] for w in windows) - margin_s
    hi = max(w[1] for w in windows) + margin_s

    chosen, skipped = [], []
    for i in good:
        s, e = i.start or 0.0, i.end or 0.0
        (chosen if (s <= hi and e >= lo) else skipped).append(i)

    if skipped:
        warnings.append(
            f"{len(skipped)} of {len(good)} recording(s) fall outside the "
            f"transect times and were not read"
        )
    return [i.path for i in chosen], skipped, warnings


def select_mcaps(mcaps: Sequence[Path]) -> tuple[list[Path], list[str]]:
    """Chronological list of the mcaps that belong to one dive, plus warnings.

    Drops files that cannot be read, and files whose recording time sits far
    away from the rest -- merging a stray recording from another day would
    stretch the shared timeline across the gap between them, and the ROV video
    built on that timeline would be nonsense.
    """
    infos = probe_mcaps(mcaps)
    warnings: list[str] = []

    for i in infos:
        if i.error:
            warnings.append(f"skipping {i.path.name}: {i.error}")

    good = [i for i in infos if i.usable]
    if not good:
        return [], warnings

    good.sort(key=lambda i: i.start or 0.0)
    starts = [i.start for i in good if i.start is not None]
    median = starts[len(starts) // 2]

    keep, stray = [], []
    for i in good:
        assert i.start is not None
        (keep if abs(i.start - median) <= STRAY_GAP_HOURS * 3600 else stray).append(i)

    for i in stray:
        when = datetime.fromtimestamp(i.start or 0, timezone.utc)
        warnings.append(
            f"skipping {i.path.name}: recorded {when:%Y-%m-%d %H:%M} UTC, which is "
            f"{abs((i.start or 0) - median) / 3600:.0f} h away from the rest of "
            f"this flight -- it looks like it belongs to a different dive"
        )

    return [i.path for i in keep], warnings


def _order_by_start(mcaps: Sequence[Path]) -> list[Path]:
    """Chronological order. Unreadable files sort last and are skipped later."""
    kept, _ = select_mcaps(mcaps)
    return kept
