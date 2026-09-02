"""
ArduPilot dataflash (.BIN) logs as a telemetry source.

The autopilot writes these to its own storage, independent of BlueOS and of the
MAVLink router on the companion computer. That independence is the whole point:
on 2026-09-01 the router died at 09:40:59 and the mcap recorded no telemetry for
three of four transects, while the flight controller went on logging all of it.

Nothing here reads or writes an mcap. A BIN is converted to the same long-format
telemetry CSV that ``mcap_extract`` produces, so the banner, the dive profile
and the overlay all consume it without knowing where it came from -- and the
original recordings stay exactly as they are.

**Placing a BIN on the wall clock** is the hard part. TimeUS is microseconds
since the autopilot booted, and a submerged vehicle usually has no GPS fix, so
there is often no absolute time inside the file at all. Two ways out, in order
of preference:

* **GPS.** If any fix was logged, GPS week and millisecond give UTC directly.
* **An overlapping mcap.** MAVLink messages carry ``time_boot_ms`` stamped by
  the same autopilot that writes TimeUS, so any mcap that overlaps the BIN --
  even one whose telemetry died partway, as long as it recorded *something*
  from the autopilot -- pins the two clocks together. Transport delay only ever
  adds, so the offset is taken from the low percentile of the differences
  rather than the median, which sits several seconds late.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ProgressCB = Callable[[float, str], None]

#: Unix time of the GPS epoch (1980-01-06), and the current GPS-UTC leap offset.
_GPS_EPOCH = 315964800
_GPS_LEAP = 18

#: Servo/RC channels ArduSub drives from the pilot's controls. Confirmed
#: against a flight where both an mcap and a BIN existed: the mcap's
#: NAMED_VALUE_FLOAT "Lights1" tracks RCIN.C9 at r=+0.97 and "CamTilt" tracks
#: RCOU.C10 at r=+0.94. Both are plain 1100-1900us servo ranges.
_PWM_LO, _PWM_SPAN = 1100.0, 800.0


#: ArduPilot's RangeFinder::Status. Only Good carries a measurement; NoData
#: logs a distance of 0.00, which is not an altitude of zero and must never
#: reach the banner as one.
RFND_GOOD = 4

#: Dataflash names the instance field differently per message: BARO uses "I",
#: RFND "Instance", BAT "Inst", and the EKF logs its core as "C".
_INSTANCE_FIELDS = ("I", "Instance", "Inst", "C")


def _instance(msg) -> int:
    for name in _INSTANCE_FIELDS:
        if hasattr(msg, name):
            try:
                return int(getattr(msg, name))
            except (TypeError, ValueError):
                return 0
    return 0


def _pwm_fraction(pwm: float) -> float | None:
    if not pwm or pwm < 800:                 # 0 means "never written"
        return None
    return max(0.0, min(1.0, (pwm - _PWM_LO) / _PWM_SPAN))


@dataclass
class BinInfo:
    path: Path
    size: int = 0
    boot_first: float = 0.0          # seconds of autopilot uptime
    boot_last: float = 0.0
    messages: int = 0
    types: set[str] = field(default_factory=set)
    gps_fix: bool = False
    error: str | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.boot_last - self.boot_first)

    @property
    def usable(self) -> bool:
        return self.error is None and "CTUN" in self.types


@dataclass
class BinAlignment:
    """How a BIN's uptime maps onto the wall clock."""

    offset: float                    # wall_time = TimeUS/1e6 + offset
    method: str                      # "gps" | "mcap"
    samples: int = 0
    residual_s: float = 0.0          # spread of the estimate
    depth_agreement: float | None = None   # correlation against the mcap, if checked
    note: str = ""

    @property
    def trustworthy(self) -> bool:
        """Only vouch for an alignment that something independent confirms.

        A wrong offset files imagery into the wrong transect, which is worse
        than refusing to guess -- so a correlation below this is not "probably
        fine", it is unverified.
        """
        if self.method == "gps":
            return True
        return (self.depth_agreement or 0.0) >= 0.98


# --------------------------------------------------------------------------
#  discovery
# --------------------------------------------------------------------------


def list_bins(folder: Path) -> list[Path]:
    """Every dataflash log under a flight folder, newest name last."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    hits = {p.resolve() for p in folder.rglob("*.BIN") if p.is_file()}
    hits |= {p.resolve() for p in folder.rglob("*.bin") if p.is_file()}
    return sorted(hits)


def probe_bin(path: Path) -> BinInfo:
    """Uptime span and message inventory, without decoding every field."""
    from pymavlink import mavutil

    path = Path(path)
    info = BinInfo(path)
    try:
        info.size = path.stat().st_size
        conn = mavutil.mavlink_connection(str(path))
        first = last = None
        while True:
            msg = conn.recv_match()
            if msg is None:
                break
            info.messages += 1
            info.types.add(msg.get_type())
            us = getattr(msg, "TimeUS", None)
            if us:
                s = us / 1e6
                first = s if first is None else min(first, s)
                last = s if last is None else max(last, s)
            # A fix with no week number is no clock: ArduSub with an
            # external (UGPS) source logs Status without GWk, and that cannot
            # place the log on the wall clock.
            if (msg.get_type() == "GPS" and getattr(msg, "Status", 0) >= 3
                    and getattr(msg, "GWk", 0)):
                info.gps_fix = True
        info.boot_first, info.boot_last = first or 0.0, last or 0.0
    except Exception as ex:
        info.error = f"{type(ex).__name__}: {ex}".split("\n")[0][:120]
    return info


def probe_bins(paths: Sequence[Path]) -> list[BinInfo]:
    return [probe_bin(p) for p in paths]


# --------------------------------------------------------------------------
#  placing the log on the wall clock
# --------------------------------------------------------------------------


def align_from_gps(path: Path) -> BinAlignment | None:
    """Offset from a GPS fix, when the vehicle ever had one."""
    from pymavlink import mavutil

    conn = mavutil.mavlink_connection(str(path))
    offs = []
    while True:
        msg = conn.recv_match(type=["GPS"])
        if msg is None:
            break
        if getattr(msg, "Status", 0) < 3 or not getattr(msg, "GWk", 0):
            continue
        wall = _GPS_EPOCH + msg.GWk * 604800 + msg.GMS / 1000.0 - _GPS_LEAP
        offs.append(wall - msg.TimeUS / 1e6)
    if not offs:
        return None
    offs.sort()
    mid = offs[len(offs) // 2]
    return BinAlignment(offset=mid, method="gps", samples=len(offs),
                        residual_s=offs[-1] - offs[0],
                        note=f"from {len(offs)} GPS fixes")


#: MAVLink messages that carry the autopilot's own uptime.
_BOOT_TOPICS = ("mavlink/1/1/ATTITUDE", "mavlink/1/1/VFR_HUD",
                "mavlink/1/1/GLOBAL_POSITION_INT")


def _bin_depth_track(path: Path):
    """(uptime_seconds, depth_m) straight off the autopilot's own clock."""
    import numpy as np
    from pymavlink import mavutil

    conn = mavutil.mavlink_connection(str(path))
    t, d = [], []
    while True:
        msg = conn.recv_match(type=["CTUN"])
        if msg is None:
            break
        t.append(msg.TimeUS / 1e6)
        d.append(-float(msg.Alt))
    return np.asarray(t), np.asarray(d)


def _pairs_from_mcap(m: Path):
    """(offsets, depth-on-uptime) from a single recording."""
    import json

    from mcap.reader import NonSeekingReader

    from . import mcap_extract as mx

    health = mx.scan_health(m)
    if not (health.complete or health.recoverable):
        return [], []
    boot, depth = [], []
    opener = (mx.open_repaired(m, health) if health.recoverable
              else open(m, "rb"))
    with opener as f:
        for _s, ch, msg in NonSeekingReader(f).iter_messages():
            if ch.topic not in _BOOT_TOPICS:
                continue
            d = json.loads(msg.data)["message"]
            bms = d.get("time_boot_ms")
            if not bms:
                continue
            boot.append(msg.log_time / 1e9 - bms / 1000.0)
            if "relative_alt" in d:
                depth.append((bms / 1000.0, -d["relative_alt"] / 1000.0))
    return boot, depth


def align_from_mcap(
    path: Path,
    mcaps: Sequence[Path],
    *,
    verify: bool = True,
) -> BinAlignment | None:
    """Pin the BIN's uptime to the wall clock using an overlapping recording.

    A recording whose telemetry died partway is still perfectly good for this,
    which is what makes the method useful at all: the very failure that makes
    the BIN necessary leaves behind exactly the overlap needed to place it.

    Each recording is judged **on its own**. A day's folder holds recordings
    from several power cycles, and every cycle restarts ``time_boot_ms`` at
    zero -- pooling them produced an offset 25 minutes wrong. Which recording
    shares this BIN's boot session is decided by whether the two dive profiles
    agree on the autopilot's clock, an axis no offset can fake.
    """
    import numpy as np

    bt, bd = _bin_depth_track(path)
    best: BinAlignment | None = None
    for m in mcaps:
        try:
            boot, depth = _pairs_from_mcap(m)
        except Exception:
            continue
        if not boot:
            continue
        a = np.asarray(boot)
        # Delay is one-sided: the recorder can only ever see a message later
        # than the autopilot stamped it, so the floor of the differences is the
        # truth and the median runs several seconds late.
        al = BinAlignment(
            offset=float(np.percentile(a, 1)), method="mcap", samples=len(a),
            residual_s=float(np.percentile(a, 50) - np.percentile(a, 1)),
            note=f"{m.name}, {len(a):,} paired timestamps")
        if verify:
            al.depth_agreement = _agreement(bt, bd, depth)
            if al.depth_agreement is not None:
                al.note += f", depth match r={al.depth_agreement:.4f}"
        if best is None or (al.depth_agreement or -1) > (best.depth_agreement or -1):
            best = al
    return best


def _agreement(bt, bd, mcap_depth) -> float | None:
    """Do the BIN and the recording describe the same dive?

    Both series are indexed by the autopilot's uptime, so this asks whether
    they came from the same power cycle -- something no choice of offset can
    fake, and the only independent check available when there is no GPS.
    """
    import numpy as np

    md = np.asarray(mcap_depth)
    if len(md) < 20 or len(bt) < 20:
        return None
    # Only judge where the two actually overlap; a recording from another
    # session interpolates to a flat edge value and would score misleadingly.
    lo, hi = float(md[:, 0].min()), float(md[:, 0].max())
    if hi <= float(bt.min()) or lo >= float(bt.max()):
        return None
    inside = md[(md[:, 0] >= bt.min()) & (md[:, 0] <= bt.max())]
    if len(inside) < 20:
        return None
    interp = np.interp(inside[:, 0], bt, bd)
    if float(np.std(interp)) < 1e-6 or float(np.std(inside[:, 1])) < 1e-6:
        return None
    return float(np.corrcoef(interp, inside[:, 1])[0, 1])


def align(
    path: Path,
    mcaps: Sequence[Path] = (),
    *,
    verify: bool = True,
) -> BinAlignment | None:
    """Best available placement of a BIN on the wall clock."""
    gps = align_from_gps(path)
    if gps is not None:
        return gps
    if mcaps:
        return align_from_mcap(path, mcaps, verify=verify)
    return None


# --------------------------------------------------------------------------
#  conversion
# --------------------------------------------------------------------------

#: Minimum seconds between kept samples, matching the mcap path's ceiling.
MIN_INTERVAL = 0.1

#: The message types worth reading. Anything else in the log is diagnostics
#: for a different purpose.
WANTED_TYPES = ("CTUN", "RFND", "BAT", "ATT", "MODE", "BARO", "RCIN", "RCOU",
                "XKF1", "POS")


def _rows(msg, t: float):
    """One dataflash record -> the CSV rows UTC's telemetry store expects.

    The field names on the right are MAVLink's, because that is what every
    consumer already reads. Two traps are handled here:

    * ``BARO`` has two instances. Instance 0 is the pressure inside the
      electronics tube (~89 kPa, reads as +15 m of altitude); only instance 1
      is the water sensor. Averaging them, or taking whichever arrives first,
      produces a depth trace that correlates with nothing.
    * dataflash logs angles in degrees, MAVLink in radians.
    """
    ty = msg.get_type()
    if ty == "CTUN":
        # Alt is the EKF's altitude in metres, negative below the surface --
        # the same quantity GLOBAL_POSITION_INT reports in millimetres.
        yield ("GLOBAL_POSITION_INT.relative_alt", float(msg.Alt) * 1000.0, "")
        yield ("VFR_HUD.climb", float(getattr(msg, "CRt", 0)) / 100.0, "")
    elif ty == "RFND":
        # Only a Good reading is a reading. The DVL loses bottom lock for
        # roughly one sample in eight while flying a transect, and ArduPilot
        # logs those as Stat=NoData with Dist=0.00 -- which, written through,
        # puts "ALT 0.00 m" on about one photo in eight.
        if _instance(msg) == 0 and int(getattr(msg, "Stat", RFND_GOOD)) == RFND_GOOD:
            yield ("RANGEFINDER.distance", float(msg.Dist), "")
    elif ty == "BAT":
        if _instance(msg) == 0:
            yield ("BATTERY_STATUS.voltage_mv", float(msg.Volt) * 1000.0, "")
            yield ("BATTERY_STATUS.current_battery", float(msg.Curr) * 100.0, "")
    elif ty == "ATT":
        yield ("ATTITUDE.roll", math.radians(float(msg.Roll)), "")
        yield ("ATTITUDE.pitch", math.radians(float(msg.Pitch)), "")
        yield ("ATTITUDE.yaw", math.radians(float(msg.Yaw)), "")
        yield ("VFR_HUD.heading", float(msg.Yaw) % 360.0, "")
    # MODE is not emitted here: write_telemetry_csv re-states the held mode
    # on a timer, because dataflash records it only on change.
    elif ty == "BARO":
        if _instance(msg) == 1:                # the water sensor, not the tube
            yield ("SCALED_PRESSURE2.temperature", float(msg.Temp) * 100.0, "")
    elif ty == "RCIN":
        if (f := _pwm_fraction(float(getattr(msg, "C9", 0)))) is not None:
            yield ("NVF.Lights1", f, "")
    elif ty == "RCOU":
        if (f := _pwm_fraction(float(getattr(msg, "C10", 0)))) is not None:
            yield ("NVF.CamTilt", f, "")
    elif ty == "XKF1":
        if _instance(msg) == 0:                # the primary EKF core only
            yield ("LOCAL_POSITION_NED.vx", float(msg.VN), "")
            yield ("LOCAL_POSITION_NED.vy", float(msg.VE), "")


def write_telemetry_csv(
    path: Path,
    alignment: BinAlignment,
    dest: Path,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> int:
    """Convert one BIN into UTC's telemetry CSV. Returns the row count."""
    from pymavlink import mavutil

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = mavutil.mavlink_connection(str(path))
    size = max(1, Path(path).stat().st_size)
    last: dict[tuple, float] = {}
    cur_mode: float | None = None
    last_mode = -1e9
    n = 0
    seen = 0
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "field", "value", "sval"])
            while True:
                msg = conn.recv_match(type=list(WANTED_TYPES))
                if msg is None:
                    break
                if cancel is not None and cancel.is_set():
                    from .ffmpeg_tools import CancelledError
                    raise CancelledError("cancelled")
                seen += 1
                ty = msg.get_type()
                t = getattr(msg, "TimeUS", 0) / 1e6 + alignment.offset

                if ty == "MODE":
                    cur_mode = float(getattr(msg, "ModeNum", 0))

                # Decimate per instance, not per type. BARO logs the tube and
                # the water sensor alternately under one name, so a shared key
                # drops every other record -- and because they alternate, it is
                # systematically the same sensor that disappears.
                key = (ty, _instance(msg))
                if ty != "MODE" and t - last.get(key, -1e9) < MIN_INTERVAL:
                    continue
                last[key] = t
                ts = f"{t:.6f}"
                for fieldname, value, sval in _rows(msg, t):
                    w.writerow([ts, fieldname, value, sval])
                    n += 1

                # ArduPilot logs MODE only when it changes, because the mode
                # genuinely holds until the next record. The telemetry store
                # expires a held sample after 30 s, though, so re-state it --
                # otherwise every transect flown without a mode change reports
                # no mode at all.
                if cur_mode is not None and t - last_mode >= 1.0:
                    w.writerow([ts, "HEARTBEAT.custom_mode", cur_mode, ""])
                    last_mode = t
                    n += 1
                if progress and seen % 50_000 == 0:
                    progress(min(0.99, conn.offset / size),
                             f"{Path(path).name}: {n:,} rows")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if progress:
        progress(1.0, f"{Path(path).name}: {n:,} rows")
    return n


#: When this sits in a flight's cache, UTC reads telemetry from it instead of
#: from the mcap. Written only when someone explicitly chooses BIN on the
#: Recording health page, and removed by the same switch -- so a flight never
#: silently changes telemetry source between runs.
OVERRIDE_CSV = "telemetry_bin.csv"
OVERRIDE_META = "telemetry_bin.json"


def override_active(cache_dir: Path) -> dict | None:
    """Provenance of the BIN override in this cache, or None."""
    import json

    csv_path = Path(cache_dir) / OVERRIDE_CSV
    if not csv_path.is_file():
        return None
    meta = {}
    try:
        meta = json.loads((Path(cache_dir) / OVERRIDE_META).read_text())
    except Exception:
        pass
    meta["csv"] = str(csv_path)
    return meta


def write_override(
    cache_dir: Path,
    bins: Sequence[Path],
    alignment: BinAlignment,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> dict:
    """Convert BIN logs into this flight's telemetry and make them the source."""
    import json
    from datetime import datetime

    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / OVERRIDE_CSV
    if len(bins) != 1:
        raise ValueError("one BIN at a time: each has its own boot clock")
    rows = write_telemetry_csv(bins[0], alignment, dest,
                               progress=progress, cancel=cancel)
    meta = {
        "source": str(bins[0]),
        "rows": rows,
        "offset": alignment.offset,
        "method": alignment.method,
        "depth_agreement": alignment.depth_agreement,
        "note": alignment.note,
        "written": datetime.now().isoformat(timespec="seconds"),
    }
    (cache / OVERRIDE_META).write_text(json.dumps(meta, indent=2))
    return meta


def clear_override(cache_dir: Path) -> None:
    for name in (OVERRIDE_CSV, OVERRIDE_META):
        (Path(cache_dir) / name).unlink(missing_ok=True)


def covers(info: BinInfo, alignment: BinAlignment,
           windows: Sequence[tuple[str, float, float]]) -> list[str]:
    """Names of the transects this log spans, once placed on the clock."""
    lo = info.boot_first + alignment.offset
    hi = info.boot_last + alignment.offset
    return [n for n, a, b in windows if a >= lo and b <= hi]
