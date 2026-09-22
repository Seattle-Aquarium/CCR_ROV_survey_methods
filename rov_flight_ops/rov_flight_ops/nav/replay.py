"""
Replay and demonstration: the page, driven by something that is not a vehicle.

Three sources, in order of how real they are:

``session``    a navigation session this program recorded. The native format,
               every field round-trips.
``transects``  the per-transect CSVs the mcap extractor writes. Real dives,
               already on disk from every survey this year, with position,
               depth, altitude, power and mode -- enough to drive the whole
               page from a flight that actually happened.
``synthetic``  generated. **Labelled synthetic everywhere it appears**, with
               deliberate faults: DVL lock loss, a stale vessel, a power
               excursion past 900 W, an estimator reset. For tests, and for
               showing somebody the page without a boat.

**Replay cannot write to a vehicle.** Not "does not" -- cannot: the replay
collector has no `Mavlink2Rest` at all, so there is no object on which a send
could be called. The profile-apply and origin-apply paths ask the collector
for a connection and get `None`, and refuse. A code path that is inert in
replay because a flag happened to be False is a path that has never been
tested; this one does not exist.

**Replay speed does not change the watt-hours.** Energy is integrated over the
*recorded* timestamps, not wall-clock time, so a dive played at eight times
speed accumulates exactly the Wh it did at one. That is the property that
makes replay usable for checking the energy arithmetic at all, and it is
tested.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import geo, power, session
from . import model as M
from .model import Fix, LinkState, NavSnapshot, Quality, Reading, Source

log = logging.getLogger(__name__)

#: Every synthetic reading carries this, so a screenshot taken in demo mode
#: can never be mistaken for a measurement.
SYNTHETIC_NOTE = "SYNTHETIC — generated, not measured"


@dataclass
class Frame:
    """One instant of a replayed flight, before it becomes a snapshot."""

    t: float                       # seconds from the start of the recording
    lat: float | None = None
    lon: float | None = None
    depth_m: float | None = None
    altitude_m: float | None = None
    speed_ms: float | None = None
    heading_deg: float | None = None
    mode: str = ""
    volts: float | None = None
    amps: float | None = None
    surftrak_target_m: float | None = None
    vessel_lat: float | None = None
    vessel_lon: float | None = None
    vessel_heading_deg: float | None = None
    #: Faults the frame is asserting, e.g. "dvl_lock_lost", "vessel_stale".
    faults: tuple = ()
    segment: int = 0
    note: str = ""


# --------------------------------------------------------------------------
#  Sources
# --------------------------------------------------------------------------


def from_transect_csv(path: Path, *, synthetic: bool = False) -> list[Frame]:
    """Frames from one of the transect extractor's CSVs.

    Column names come from `mcap_to_csv`'s own output, which this repository
    controls, so they are read by name and a missing one is simply absent
    rather than an error -- the extractor's columns have changed before and
    replay must not be what breaks.

    `DVLlat`/`DVLlon` is preferred over `Latitude`/`Longitude` because on a
    DVL-only dive the latter are empty for the whole file, which is the case
    worth being able to replay.
    """
    frames: list[Frame] = []
    t0: float | None = None
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            t = _row_time(row)
            if t is None:
                continue
            if t0 is None:
                t0 = t
            lat = _f(row.get("DVLlat")) or _f(row.get("Latitude")) \
                or _f(row.get("EKFlat"))
            lon = _f(row.get("DVLlon")) or _f(row.get("Longitude")) \
                or _f(row.get("EKFlon"))
            frames.append(Frame(
                t=t - t0,
                lat=lat if lat and M.valid_latlon(lat, lon) else None,
                lon=lon if lat and M.valid_latlon(lat, lon) else None,
                depth_m=_f(row.get("Depth")),
                altitude_m=_f(row.get("Altitude")),
                speed_ms=_f(row.get("Velocity_mps")),
                heading_deg=_f(row.get("Heading")),
                mode=(row.get("Mode") or "").title(),
                volts=_f(row.get("Battery_V")),
                amps=_f(row.get("Battery_A")),
                note="synthetic" if synthetic else ""))
    log.info("replay: %d frame(s) from %s", len(frames), path)
    return frames


def from_session_log(path: Path) -> list[Frame]:
    """Frames from a navigation session this program recorded."""
    rows = session.read_events(Path(path))
    frames: list[Frame] = []
    t0 = None
    for r in rows:
        if r.get("kind") != "rov_fix":
            continue
        t = r.get("mono")
        if not isinstance(t, (int, float)):
            continue
        if t0 is None:
            t0 = t
        frames.append(Frame(t=t - t0, lat=r.get("lat"), lon=r.get("lon"),
                            segment=int(r.get("seg", 0))))
    return frames


def _row_time(row: dict) -> float | None:
    s = row.get("Datetime_UTC") or row.get("datetime_utc") or ""
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return _f(row.get("t")) or _f(row.get("Time"))


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


# --------------------------------------------------------------------------
#  Synthetic
# --------------------------------------------------------------------------


def synthetic_dive(*, seconds: float = 600.0, hz: float = 4.0,
                   lat0: float = 47.62691, lon0: float = -122.39018,
                   faults: bool = True) -> list[Frame]:
    """A dive that exercises every branch of the display.

    Built to hit the acceptance cases rather than to look plausible:

    * altitude walks 10 → 2 → 1.5 → 0.8 → 0.3 m and back above range, so both
      gauge modes and the hysteresis band are visited;
    * power crosses 900 W and then 1,000 W, so the red band and the over-range
      mark are visited, with one brief spike that the peak must catch;
    * the DVL loses bottom lock for twenty seconds, during which speed and
      altitude must go invalid rather than to zero;
    * the vessel's HDT goes stale while its position stays good;
    * one estimator reset jumps the position, which must break the track.
    """
    frames: list[Frame] = []
    n = int(seconds * hz)
    lat, lon = lat0, lon0
    heading = 285.0
    segment = 0
    for i in range(n):
        t = i / hz
        f = t / seconds

        # A slow lawnmower, so the track has shape.
        heading = 285.0 + 25.0 * math.sin(t / 90.0)
        step = 0.35 / hz
        lat, lon = geo.destination(lat, lon, heading, step)

        # Altitude: descend, survey at 0.8, dip, climb back out of range.
        if f < 0.10:
            alt = 10.0 - (f / 0.10) * 8.0          # 10 -> 2
        elif f < 0.15:
            alt = 2.0 - ((f - 0.10) / 0.05) * 0.5  # 2 -> 1.5
        elif f < 0.22:
            alt = 1.5 - ((f - 0.15) / 0.07) * 0.7  # 1.5 -> 0.8
        elif f < 0.70:
            alt = 0.8 + 0.06 * math.sin(t / 7.0)   # survey
        elif f < 0.76:
            alt = 0.8 - ((f - 0.70) / 0.06) * 0.5  # -> 0.3
        elif f < 0.86:
            alt = 0.3 + ((f - 0.76) / 0.10) * 1.4  # -> 1.7, the hysteresis band
        else:
            alt = 1.7 + ((f - 0.86) / 0.14) * 6.0  # back up

        depth = -(12.0 + 2.0 * math.sin(t / 120.0))
        speed = 0.35 + 0.08 * math.sin(t / 11.0)

        # Power: a working baseline with thruster bursts, one of which goes
        # over the OTPS reference.
        watts = 180.0 + 90.0 * math.sin(t / 13.0) + 40.0 * math.sin(t / 3.1)
        if 0.40 < f < 0.43:
            watts += 700.0                      # into the red band
        if abs(f - 0.55) < 0.004:
            watts += 900.0                      # a spike past 1,000 W
        volts = 15.9 - 0.4 * (watts / 1000.0)
        amps = max(0.2, watts / volts)

        fault: tuple = ()
        # DVL lock loss.
        if faults and 0.30 < f < 0.33:
            fault += ("dvl_lock_lost",)
            alt_val, speed_val = None, None
        else:
            alt_val, speed_val = alt, speed

        # An estimator reset: the position jumps and the track must break.
        if faults and abs(f - 0.62) < (0.5 / n):
            segment += 1
            lat, lon = geo.destination(lat, lon, 45.0, 18.0)
            fault += ("estimator_reset",)

        vessel_lat, vessel_lon = geo.destination(
            lat0, lon0, 40.0 + 12.0 * math.sin(t / 200.0), 120.0)
        vhead = (80.0 + 30.0 * math.sin(t / 60.0)) % 360.0
        if faults and 0.45 < f < 0.52:
            fault += ("vessel_hdt_stale",)
            vhead_val = None
        else:
            vhead_val = vhead

        frames.append(Frame(
            t=t, lat=lat, lon=lon, depth_m=depth, altitude_m=alt_val,
            speed_ms=speed_val, heading_deg=heading % 360.0,
            mode="Surftrak" if 0.22 < f < 0.70 else "Depth Hold",
            volts=volts, amps=amps,
            surftrak_target_m=0.75 if 0.22 < f < 0.70 else None,
            vessel_lat=vessel_lat, vessel_lon=vessel_lon,
            vessel_heading_deg=vhead_val,
            faults=fault, segment=segment, note=SYNTHETIC_NOTE))
    return frames


# --------------------------------------------------------------------------
#  The player
# --------------------------------------------------------------------------


class ReplayCollector:
    """Presents recorded frames with the same interface as `NavCollector`.

    Deliberately a separate class rather than a mode flag on the live one.
    `NavCollector` owns a `Mavlink2Rest`; this owns nothing that can reach a
    network. The page holds one or the other and cannot tell them apart, and
    the "no vehicle writes in replay" property is structural.
    """

    #: Every attribute the page reads off a collector. Kept in one place so
    #: the two classes cannot drift apart without a test noticing.
    def __init__(self, frames: list[Frame], *, label: str = "replay",
                 speed: float = 1.0, loop: bool = False,
                 synthetic: bool = False, on_event=None) -> None:
        self.frames = frames
        self.label = label
        self.speed = max(0.05, speed)
        self.loop = loop
        self.synthetic = synthetic
        self.on_event = on_event

        #: There is no connection. Named the same as the live collector's so
        #: that anything reaching for it gets None rather than a live object.
        self.mav = None
        self.allow_writes = False
        self.services: dict = {}
        self.energy = power.EnergyMeter()
        self.params: dict[str, float] = {}
        self.params_mono: float | None = None
        self.rov_track: list = []
        self.vessel_track: list = []

        self._snapshot = NavSnapshot()
        self._i = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._segment = 0
        #: Counts frames actually published, so the energy meter's
        #: duplicate-rejection has a key that advances exactly once per frame.
        #: Deliberately not the playback index: seeking backwards must not
        #: make the meter treat replayed frames as already counted.
        self._published = 0
        self._origin: tuple[float, float] | None = None
        self._origin_confirmed = False

    # -- the same surface the page uses --------------------------------------

    def start(self, session_id: str = "") -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.energy.begin(session_id or f"replay-{int(time.time())}")
        self._thread = threading.Thread(target=self._run, name="nav-replay",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 0.0) -> bool:
        """Signal the player to stop. Does not join unless asked to.

        The same contract as `NavCollector.stop`, deliberately: the page holds
        one or the other and must not have to know which.
        """
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        if timeout > 0:
            t.join(timeout)
        alive = t.is_alive()
        if not alive:
            self._thread = None
        return not alive

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> NavSnapshot:
        return self._snapshot

    def set_parameter_reader(self, fn) -> None:
        """Ignored: replay has no vehicle to read parameters from.

        A recorded session's manifest carries the parameters that were in
        force, and `load_params` puts them in. Nothing is read live.
        """

    def load_params(self, params: dict) -> None:
        self.params = dict(params or {})
        self.params_mono = time.monotonic()

    def set_confirmed_origin(self, lat: float, lon: float) -> None:
        self._origin = (lat, lon)
        self._origin_confirmed = True

    def clear_origin(self) -> None:
        self._origin = None
        self._origin_confirmed = False

    def pause(self, on: bool = True) -> None:
        self._paused.set() if on else self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def seek(self, fraction: float) -> None:
        self._i = max(0, min(len(self.frames) - 1,
                             int(fraction * len(self.frames))))

    @property
    def progress(self) -> float:
        return self._i / max(1, len(self.frames))

    # -- playing -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.1)
                continue
            if self._i >= len(self.frames):
                if not self.loop:
                    break
                self._i = 0
                self.energy.begin(f"replay-{int(time.time())}")
                self.rov_track = []
                self.vessel_track = []
            frame = self.frames[self._i]
            self._i += 1
            self._publish(frame)

            # Wall-clock pacing only. The frame's own `t` drives the energy
            # integration, so the speed control cannot change the Wh.
            nxt = self.frames[self._i] if self._i < len(self.frames) else None
            gap = (nxt.t - frame.t) if nxt is not None else 0.25
            self._stop.wait(max(0.0, min(2.0, gap / self.speed)))
        log.info("replay finished (%s)", self.label)

    def _publish(self, f: Frame) -> None:
        prev = self._snapshot
        self._published += 1
        # Two clocks, and conflating them is a bug this had for an afternoon:
        # every reading came out "373,325 s old" and the whole page went red,
        # because a reading's age is `time.monotonic() - recv_mono` and the
        # frame's `t` starts at zero.
        #
        #   `recorded` -- the frame's own time, seconds from the start of the
        #   recording. **Energy integrates over this**, which is what makes the
        #   watt-hours independent of playback speed.
        #
        #   `now` -- this laptop's monotonic clock. **Freshness is measured
        #   against this**, because a replayed reading is as fresh as the
        #   moment it was put on the screen; a page showing "10 minutes old"
        #   for the frame it has just drawn is telling the operator nothing
        #   true about anything.
        recorded = f.t
        now = time.monotonic()
        s = NavSnapshot(mono=now, wall=time.time())
        note = SYNTHETIC_NOTE if self.synthetic else f"replay: {self.label}"
        src = Source(key="replay", message=self.label,
                     device="synthetic" if self.synthetic else "recording")

        s.link = LinkState(mode="replay", connected=True,
                           host=f"{self.label}"
                                + (" (synthetic)" if self.synthetic else ""),
                           session=self.label, last_ok_age=0.0)

        def r(v, unit="", frame="", extra=""):
            if v is None:
                return Reading(quality=Quality.INVALID, unit=unit, source=src,
                               note=extra or "not valid in this recording")
            return M.good(v, unit=unit, frame=frame, source=src,
                          recv_mono=now, note=extra or note)

        s.mode = r(f.mode or None)
        s.depth = r(f.depth_m, "m", "surface")
        s.altitude = (r(None, "m", extra="no bottom lock (replayed fault)")
                      if "dvl_lock_lost" in f.faults else r(f.altitude_m, "m",
                                                            "bottom"))
        s.speed = (r(None, "m/s", extra="DVL lock lost — speed unknown, "
                                        "not zero")
                   if "dvl_lock_lost" in f.faults else r(f.speed_ms, "m/s"))
        s.heading = r(f.heading_deg, "°")
        s.surftrak_target = (r(f.surftrak_target_m, "m", "bottom")
                             if f.surftrak_target_m else
                             Reading(quality=Quality.INVALID, unit="m",
                                     source=src,
                                     note="Surftrak has no target"))
        s.voltage = r(f.volts, "V")
        s.current = r(f.amps, "A")

        watts = self.energy.update(f.volts, f.amps, mono=recorded, fresh=True,
                                   counter=self._published)
        s.watts = r(watts, "W")
        st = self.energy.state
        s.energy_wh = r(st.wh, "Wh", extra=self.energy.note())
        s.peak_w = (r(st.peak_w, "W", extra="highest valid sample")
                    if st.peak_w > 0 else
                    Reading(quality=Quality.NEVER_RECEIVED, unit="W",
                            source=src))

        if f.lat is not None and f.lon is not None:
            if f.segment != self._segment:
                self._segment = f.segment
                if self.on_event:
                    self.on_event("track_break",
                                  {"why": "replayed estimator reset",
                                   "segment": self._segment})
            fix = Fix(lat=f.lat, lon=f.lon,
                      kind="dead" if self._origin_confirmed else "ekf",
                      quality=Quality.OK, source=src, recv_mono=now,
                      recv_time=time.time(), segment=f.segment, note=note)
            s.rov_fix = fix
            self.rov_track.append(fix)
            if len(self.rov_track) > 20_000:
                del self.rov_track[:5_000]
        else:
            s.rov_fix = prev.rov_fix

        if f.vessel_lat is not None:
            vfix = Fix(lat=f.vessel_lat, lon=f.vessel_lon, kind="vessel",
                       quality=Quality.OK, source=src, recv_mono=now,
                       recv_time=time.time(), note=note)
            s.vessel_fix = vfix
            if (not self.vessel_track
                    or abs(self.vessel_track[-1].lat - vfix.lat) > 1e-7
                    or abs(self.vessel_track[-1].lon - vfix.lon) > 1e-7):
                self.vessel_track.append(vfix)
        s.vessel_heading = (
            r(f.vessel_heading_deg, "°T")
            if f.vessel_heading_deg is not None else
            Reading(quality=Quality.STALE, unit="°T", source=src,
                    note="HDT stale — the vessel's bow direction is unknown "
                         "even though its position is not"))

        s.params = dict(self.params)
        s.params_age = (None if self.params_mono is None
                        else time.monotonic() - self.params_mono)
        s.messages = {}
        self._snapshot = s


def discover_replays(flight_dir: Path) -> list[tuple[str, Path, str]]:
    """(label, path, kind) for everything in a flight folder replay can read."""
    out: list[tuple[str, Path, str]] = []
    d = Path(flight_dir)
    logs = d / "logs"
    for p in sorted(logs.glob("nav_*.jsonl")) if logs.is_dir() else []:
        out.append((f"Navigation session {p.stem[4:]}", p, "session"))
    trans = d / "transects"
    for p in sorted(trans.glob("*.csv")) if trans.is_dir() else []:
        out.append((f"Transect {p.stem}", p, "transects"))
    return out


def load(path: Path, kind: str) -> list[Frame]:
    if kind == "session":
        return from_session_log(path)
    if kind == "transects":
        return from_transect_csv(path)
    raise ValueError(f"{kind} is not a replay source this program reads")
