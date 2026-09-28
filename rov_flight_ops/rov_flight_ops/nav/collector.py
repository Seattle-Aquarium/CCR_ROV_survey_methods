"""
One background reader for the whole Navigation chapter.

Every gauge, the map, the sensor matrix and the session log read the same
`NavSnapshot`. Nothing else on the page opens a socket. That is not tidiness:
this program has frozen in the field before, and the two mechanisms behind it
are exactly what a page of independent pollers reintroduces -- work on the
window's thread, and an unbounded backlog of redraws.

So, the same three rules `gui/shell.py` states for jobs, applied to telemetry:

**Nothing here touches a widget.** The collector thread produces snapshots and
puts the newest one in a slot. The window reads that slot on its own timer.
There is no callback into Tk from this thread, and no lock the window can
block on -- the slot is a single reference swap, which CPython makes atomic.

**The newest state wins; a backlog is dropped, not queued.** A window that
stalls for two seconds must come back to the current picture, not replay forty
stale ones. The *display* is coalesced. The *log* is not: events go to the
session log from this thread as they happen, so a stall loses redraws and
never loses evidence.

**Every read is bounded and every failure is recorded.** One slow GET cannot
hold the cadence: the poll loop has a deadline, and a group that overruns is
skipped next time round rather than allowed to pile up.

The cadence is tiered, because the source rates differ by two orders of
magnitude and asking for everything at the fastest one would be pointless load
on a Pi that is also running Cockpit, Madrona and the recorder. Roughly 4 Hz
for the flight instruments, 0.5 Hz for estimator state, 0.5 Hz for the
extensions' own HTTP APIs.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from . import extensions as E
from . import mav2rest, power
from . import model as M
from . import trust as TR
from .model import Fix, LinkState, NavSnapshot, Quality, Reading, Source

log = logging.getLogger(__name__)

#: How often the flight instruments are read. Four times a second is fast
#: enough that an altitude change is on the screen before the pilot has
#: finished making it, and slow enough to be four small GETs rather than forty.
FAST_PERIOD_S = 0.25
#: Estimator state, GPS input and the rest.
SLOW_PERIOD_S = 2.0
#: The extensions' own HTTP APIs, which are Python web apps on the Pi.
EXT_PERIOD_S = 2.0
#: How often the parameter set is re-read. Expensive (it reads the head of a
#: dataflash log) and it only changes when somebody changes it.
PARAM_PERIOD_S = 120.0

#: Backoff after a failed cycle, growing to this cap. A vehicle that is off
#: must not be hammered, and a tether that flaps must not be given up on.
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 15.0

#: How much track the collector keeps in memory. At 4 Hz this is about forty
#: minutes of ROV positions; the *log* keeps every sample, so this bound
#: costs nothing but the tail of the drawn breadcrumb.
JUMPS_MAX = 40
TRACK_MAX = 10_000

#: A position more than this far from the previous one, in the time between
#: them, is a jump rather than a movement: an estimator reset, an origin
#: change, or a new source. The track is broken rather than drawn across it.
JUMP_SPEED_MS = 12.0
JUMP_MIN_M = 5.0

#: ArduSub flight modes. Copied from `telemetry.ARDUSUB_MODES` rather than
#: imported so that the live page and the post-flight reader can be changed
#: independently -- they answer different questions and have drifted before.
ARDUSUB_MODES = {
    0: "Stabilize", 1: "Acro", 2: "Depth Hold", 3: "Auto", 4: "Guided",
    7: "Circle", 9: "Surface", 16: "Position Hold", 19: "Manual",
    20: "Motor Detect", 21: "Surftrak",
}

#: The named float ArduSub publishes the Surftrak target in, in meters.
#: `ModeSurftrak` uses -1 cm as its invalid marker, so the value arrives as
#: -0.01 whenever there is no target -- which is most of the time, including
#: when Surftrak is not the active mode.
RFTARGET_KEY = "RFTarget"
RFTARGET_INVALID_MAX = 0.0


@dataclass
class TrackPoint:
    lat: float
    lon: float
    mono: float
    wall: float
    segment: int
    kind: str = "ekf"
    #: What the position was resting on when it was recorded: one of
    #: `trust.STATES`. Stored per point rather than derived at draw time
    #: because the estimator's state at 10:04 is not recoverable from a
    #: snapshot taken at 10:31, and a track recoloured by the *present* state
    #: would quietly relabel history.
    trust: str = "unknown"


@dataclass
class Jump:
    """A position discontinuity, and what the position rested on either side.

    Recorded rather than inferred later: by the time an operator asks "what
    happened at 10:04", the estimator has moved on and the evidence is gone.
    """

    mono: float
    wall: float
    meters: float
    seconds: float
    segment: int
    from_trust: str = "unknown"
    to_trust: str = "unknown"

    def line(self) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(self.wall))
        move = f"{self.meters:.0f} m in {self.seconds:.1f} s"
        if self.from_trust == self.to_trust:
            return f"{when} — position jumped {move} ({self.from_trust})"
        return (f"{when} — position jumped {move}, "
                f"{self.from_trust} to {self.to_trust}")


class NavCollector:
    """Reads one vehicle, publishes snapshots. Started and stopped explicitly.

    Lifecycle mirrors `flightlog._Session`: a collector owns its thread and
    its stop signal, nothing is shared with the next one, and a worker that
    outlives its join is reported rather than assumed gone.
    """

    def __init__(self, host: str, *, allow_writes: bool = False,
                 on_event=None) -> None:
        self.host = host
        #: Set by the operator's unlock, and never true in replay.
        self.allow_writes = allow_writes
        #: Called from the collector thread with (kind, dict) for the session
        #: log. Must not touch widgets.
        self.on_event = on_event

        self.mav = mav2rest.Mavlink2Rest(host, allow_writes=allow_writes)
        self.services: dict[str, E.Service] = {}
        self._vessel_reader = E.VesselReader()
        self.energy = power.EnergyMeter()

        #: Which navigation profile the operator has chosen. The collector
        #: does not decide it and never acts on it -- it is here only so a
        #: track point can record what the position rested on, and in the
        #: acoustic profile that includes whether an acoustic fix was recent.
        #: Set by the page; "" means do not make that distinction.
        self.profile_key = ""

        #: The newest snapshot. One reference, swapped whole.
        self._snapshot = NavSnapshot()
        self._link = LinkState(mode="off", host=host)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_mono = 0.0

        #: The drawn track. Deques so the bound is free.
        self.rov_track: deque[TrackPoint] = deque(maxlen=TRACK_MAX)
        #: Position jumps, newest last. Bounded, like everything else that
        #: grows with flight time.
        self.jumps: list[Jump] = []
        self.vessel_track: deque[TrackPoint] = deque(maxlen=TRACK_MAX)
        self._segment = 0
        self._last_rov: TrackPoint | None = None
        self._last_vessel: TrackPoint | None = None

        #: Parameters, read on the slow cadence by an injected reader.
        self.params: dict[str, float] = {}
        self.params_mono: float | None = None
        self._read_params = None
        #: The in-flight parameter read, so two never overlap.
        self._param_thread: threading.Thread | None = None
        #: Origin known to the collector, for the NED projection. Set only
        #: from a confirmed origin -- never from a saved parameter.
        self._origin: tuple[float, float] | None = None
        self._origin_confirmed = False

        self._next = {"fast": 0.0, "slow": 0.0, "ext": 0.0, "param": 0.0}
        self._backoff = 0.0
        self._dvl = E.DvlStatus()
        self._vessel = E.VesselStatus()
        self._dropped = 0

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def start(self, session_id: str = "") -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_mono = time.monotonic()
        self._link = LinkState(mode="live", host=self.host,
                               session=session_id or f"{int(time.time())}")
        self.energy.begin(self._link.session)
        self._thread = threading.Thread(target=self._run, name="nav-collector",
                                        daemon=True)
        self._thread.start()
        log.info("navigation collector started against %s", self.host)

    def stop(self, timeout: float = 0.0) -> bool:
        """Ask the collector to stop. Returns whether the thread has finished.

        **The default does not join, and the window must never pass a
        timeout.** The loop can be inside an HTTP GET to a vehicle that has
        stopped answering, and that GET has seconds of timeout left to run; a
        join on the window's thread would hold the whole application for those
        seconds, which is precisely the freeze this program has been bitten by
        before. The thread is a daemon with bounded timeouts on every request,
        so once it is signaled it goes away on its own and can hurt nothing
        on the way out -- it holds no file the next run needs and writes to no
        widget.

        A positive `timeout` joins, and is for tests that want the thread
        provably gone before they assert on something it touches.
        """
        self._stop.set()
        self._link.mode = "off"
        t = self._thread
        if t is None:
            return True
        if timeout > 0:
            t.join(timeout)
        alive = t.is_alive()
        if not alive:
            self._thread = None
        elif timeout > 0:
            log.warning("navigation collector did not stop within %.1f s",
                        timeout)
        return not alive

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> NavSnapshot:
        """The newest picture. Safe to call from the window's thread."""
        return self._snapshot

    def set_parameter_reader(self, fn) -> None:
        """Inject how parameters are read, so this module never imports blueos.

        Keeps the collector testable with a dict, and keeps the expensive
        dataflash read -- which is `blueos`'s job and already written -- out
        of here.
        """
        self._read_params = fn

    def set_confirmed_origin(self, lat: float, lon: float) -> None:
        """Adopt an origin that has actually been read back from the vehicle.

        Only a *confirmed* origin gets here. Projecting local NED through a
        coordinate somebody typed but never applied would draw a track that
        looks authoritative and is somewhere else entirely.
        """
        if self._origin != (lat, lon):
            self._break_track("origin changed")
        self._origin = (lat, lon)
        self._origin_confirmed = True

    def clear_origin(self) -> None:
        if self._origin is not None:
            self._break_track("origin cleared")
        self._origin = None
        self._origin_confirmed = False

    def _break_track(self, why: str) -> None:
        """Start a new track segment, so nothing is drawn across the jump."""
        self._segment += 1
        self._last_rov = None
        self._emit("track_break", {"why": why, "segment": self._segment})

    def _emit(self, kind: str, data: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            log.exception("navigation event sink raised on %s", kind)

    # ------------------------------------------------------------------
    #  the loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._discover()
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    self._cycle(t0)
                    self._backoff = 0.0
                    self._link.problem = ""
                except Exception as ex:
                    self._link.problem = str(ex) or type(ex).__name__
                    self._backoff = min(BACKOFF_MAX_S,
                                        max(BACKOFF_START_S, self._backoff * 2))
                    log.warning("navigation cycle failed: %s", ex)
                    self._emit("collector_error", {"error": self._link.problem})
                # Sleep to the next due time, or the backoff, whichever is
                # longer. Waiting on the stop event rather than sleeping means
                # a stop is immediate rather than up to a period late.
                due = min(self._next.values())
                wait = max(0.02, min(due - time.monotonic(), FAST_PERIOD_S))
                self._stop.wait(max(wait, self._backoff))
        except Exception:
            log.exception("navigation collector died")
            self._link.problem = "the collector stopped unexpectedly"
        finally:
            self._link.connected = False
            log.info("navigation collector finished")

    def _discover(self) -> None:
        try:
            self.services = E.discover(self.host)
            found = {k: (s.port, s.via) for k, s in self.services.items()}
            log.info("navigation extensions: %s", found)
            self._emit("discovery", {"services": {
                k: {"name": s.name, "port": s.port, "via": s.via,
                    "note": s.note} for k, s in self.services.items()}})
        except Exception as ex:
            log.warning("extension discovery failed: %s", ex)
            self.services = {}
        try:
            picked = self.mav.choose_vehicle()
            if picked and picked != (self.mav.system, self.mav.component):
                log.info("navigation: using vehicle %s/%s", *picked)
                self.mav.system, self.mav.component = picked
        except Exception as ex:
            log.warning("vehicle selection failed: %s", ex)

    def _cycle(self, now: float) -> None:
        fast = slow = None
        if now >= self._next["fast"]:
            self._next["fast"] = now + FAST_PERIOD_S
            fast = self.mav.read_many(mav2rest.FAST)
        if now >= self._next["slow"]:
            self._next["slow"] = now + SLOW_PERIOD_S
            slow = self.mav.read_many(mav2rest.SLOW)
        if now >= self._next["ext"]:
            self._next["ext"] = now + EXT_PERIOD_S
            self._read_extensions()
        if now >= self._next["param"] and self._read_params is not None:
            self._next["param"] = now + PARAM_PERIOD_S
            self._refresh_params()
        if fast is None and slow is None:
            return
        self._assemble(fast or {}, slow or {}, now)

    def _read_extensions(self) -> None:
        dvl_svc = self.services.get("dvl")
        if dvl_svc is not None:
            try:
                before = self._dvl.message_type
                self._dvl = E.read_dvl(self.host, dvl_svc)
                if before and self._dvl.message_type and before != self._dvl.message_type:
                    self._emit("dvl_message_type", {
                        "from": before, "to": self._dvl.message_type})
            except Exception as ex:
                log.debug("DVL status read failed: %s", ex)
        ext_svc = self.services.get("ugps_external")
        if ext_svc is not None:
            try:
                self._vessel = self._vessel_reader.read(self.host, ext_svc)
            except Exception as ex:
                log.debug("vessel status read failed: %s", ex)

    def _refresh_params(self) -> None:
        """Re-read the parameters, on a thread of their own.

        Not on the collector's thread. Reading them means fetching the head of
        the autopilot's newest dataflash log over File Browser -- a third of a
        second against a healthy vehicle, and seconds against a busy or
        half-answering one. Doing that inside the poll cycle would hold the
        4 Hz instruments for its whole duration, every two minutes, which is
        exactly the sort of periodic hitch that gets reported as "the page
        freezes" and is miserable to track down.

        One at a time: if the previous read has not finished, this one is
        skipped rather than queued. A vehicle slow enough to overlap a
        two-minute cadence does not need two readers fighting over it.
        """
        worker = self._param_thread
        if worker is not None and worker.is_alive():
            log.debug("parameter refresh skipped: the previous one is running")
            return
        self._param_thread = threading.Thread(
            target=self._read_params_worker, name="nav-params", daemon=True)
        self._param_thread.start()

    def _read_params_worker(self) -> None:
        try:
            got = self._read_params()
        except Exception as ex:
            log.warning("parameter refresh failed: %s", ex)
            return
        if not got or self._stop.is_set():
            return
        if self.params:
            moved = {k: (self.params.get(k), v) for k, v in got.items()
                     if k in self.params and abs(v - self.params[k]) > 1e-9}
            # Drift matters: another extension or Cockpit may have changed the
            # navigation configuration under us. It is reported, never
            # corrected -- an automatic correction loop fighting another
            # writer is how a vehicle ends up in a state nobody chose.
            watched = {k: v for k, v in moved.items()
                       if k.startswith(("EK3_", "AHRS_", "VISO_", "GPS_",
                                        "RNGFND1_", "ORIGIN_"))}
            if watched:
                log.info("navigation parameters changed: %s", watched)
                self._emit("param_drift", {
                    "changed": {k: {"from": a, "to": b}
                                for k, (a, b) in watched.items()}})
        self.params = got
        self.params_mono = time.monotonic()

    # ------------------------------------------------------------------
    #  turning messages into readings
    # ------------------------------------------------------------------

    def _assemble(self, fast: dict, slow: dict, now: float) -> None:
        prev = self._snapshot
        s = NavSnapshot(mono=now, wall=time.time())

        got = {**prev.messages, **self.mav.health}
        s.messages = dict(got)
        read = list(fast.values()) + list(slow.values())
        any_fresh = any(x.fresh for x in read)
        if read:
            self._link.connected = any_fresh or any(not x.error for x in read)
        if any_fresh:
            self._link.last_ok_age = 0.0
        elif self._link.last_ok_age is not None:
            self._link.last_ok_age = now - self._started_mono
        self._link.dropped = self._dropped
        s.link = self._link

        # -- attitude and mode --------------------------------------------
        att = fast.get("ATTITUDE")
        if att is not None and att.fresh:
            src = Source("mav:ATTITUDE", "ATTITUDE", self.mav.system,
                         self.mav.component)
            for key, field_ in (("roll", "roll"), ("pitch", "pitch"),
                                ("yaw", "yaw")):
                v = att.num(field_)
                if v is not None:
                    setattr(s, key, M.good(math.degrees(v), unit="°",
                                           source=src, recv_mono=att.mono))
        else:
            s.roll, s.pitch, s.yaw = prev.roll, prev.pitch, prev.yaw

        hud = fast.get("VFR_HUD")
        if hud is not None and hud.fresh:
            src = Source("mav:VFR_HUD", "VFR_HUD", self.mav.system,
                         self.mav.component)
            hdg = hud.num("heading")
            if hdg is not None:
                s.heading = M.good(float(hdg) % 360.0, unit="°", source=src,
                                   recv_mono=hud.mono,
                                   note="vehicle compass heading; not a "
                                        "geodesic bearing")
            climb = hud.num("climb")
            if climb is not None:
                s.climb = M.good(climb, unit="m/s", source=src,
                                 recv_mono=hud.mono)
        else:
            s.heading, s.climb = prev.heading, prev.climb

        hb = fast.get("HEARTBEAT")
        if hb is not None and hb.fresh:
            src = Source("mav:HEARTBEAT", "HEARTBEAT", self.mav.system,
                         self.mav.component)
            cm = hb.num("custom_mode")
            if cm is not None:
                name = ARDUSUB_MODES.get(int(cm))
                s.mode = (M.good(name, source=src, recv_mono=hb.mono)
                          if name else
                          M.good(f"mode {int(cm)}", source=src,
                                 recv_mono=hb.mono,
                                 note="not a mode this program has a name for"))
            base = hb.get("base_mode")
            armed = _armed_from_base_mode(base)
            if armed is not None:
                s.armed = M.good(armed, source=src, recv_mono=hb.mono)
        else:
            s.mode, s.armed = prev.mode, prev.armed

        # -- depth, altitude, speed ---------------------------------------
        s.depth = self._depth(fast, prev)
        s.altitude = self._altitude(fast, slow, prev)
        s.surftrak_target = self._surftrak_target(fast, prev)
        s.speed = self._speed(fast, prev)

        # -- power ----------------------------------------------------------
        self._power(fast, prev, s, now)

        # -- position ---------------------------------------------------------
        self._position(fast, prev, s, now)

        # -- the navigation suite ----------------------------------------------
        s.dvl = self._dvl_readings(fast, slow, now)
        s.ekf = self._ekf_readings(slow, now)
        s.vessel = E.vessel_readings(self._vessel)
        s.ugps = self._ugps_readings(slow, now)
        s.params = dict(self.params)
        s.params_age = (None if self.params_mono is None
                        else now - self.params_mono)

        self._snapshot = s

    # -- individual instruments -------------------------------------------

    def _depth(self, fast: dict, prev: NavSnapshot) -> Reading:
        """Depth below the surface, negative down, from the barometer.

        `GLOBAL_POSITION_INT.relative_alt` is millimeters above the home
        altitude and is what ArduSub derives from the depth sensor -- so it is
        already negative underwater and needs only a scale. It is *not*
        `alt` (MSL, which is meaningless with no origin) and it is not the
        NED origin's z.
        """
        if not fast:
            return prev.depth
        g = fast.get("GLOBAL_POSITION_INT")
        if g is None or not g.fresh:
            return prev.depth
        rel = g.num("relative_alt")
        if rel is None:
            return prev.depth
        src = Source("mav:GLOBAL_POSITION_INT.relative_alt",
                     "GLOBAL_POSITION_INT", self.mav.system,
                     self.mav.component, device="barometer via ArduSub")
        return M.good(rel / 1000.0, unit="m", frame="surface",
                      source=src, recv_mono=g.mono,
                      note="negative is below the surface")

    def _altitude(self, fast: dict, slow: dict, prev: NavSnapshot) -> Reading:
        """Height above the seabed, from the downward range only.

        Three things are *not* this: depth below the surface, altitude above
        the EKF origin, and MSL altitude. Nothing here falls back to any of
        them -- an invalid range shows as invalid, because a gauge that
        silently swaps in a different kind of height is worse than a blank one.

        `RANGEFINDER.distance` is the autopilot's own, already in meters and
        already through whatever filtering ArduSub applies, so it is preferred.
        `DISTANCE_SENSOR.current_distance` is **centimeters** and comes from
        two different senders on this fleet -- the DVL at 255/0 and the
        autopilot's echo at 1/1 -- which is why it is a named fallback rather
        than merged in.
        """
        if not fast and not slow:
            return prev.altitude
        rf = fast.get("RANGEFINDER")
        if rf is not None and rf.fresh:
            d = rf.num("distance")
            src = Source("mav:RANGEFINDER", "RANGEFINDER", self.mav.system,
                         self.mav.component, device="downward range")
            if d is None:
                return M.invalid("RANGEFINDER carried no distance", source=src)
            if d <= 0.0:
                return M.invalid("no bottom lock (range reads zero)",
                                 value=d, unit="m", source=src)
            return M.good(d, unit="m", frame="bottom", source=src,
                          recv_mono=rf.mono)

        ds = slow.get("DISTANCE_SENSOR")
        if ds is not None and ds.fresh:
            cm = ds.num("current_distance")
            sid = ds.num("id")
            src = Source("mav:DISTANCE_SENSOR", "DISTANCE_SENSOR",
                         self.mav.system, self.mav.component,
                         instance=int(sid) if sid is not None else None)
            if cm is None or cm <= 0:
                return M.invalid("no bottom lock", unit="m", source=src)
            return M.good(cm / 100.0, unit="m", frame="bottom", source=src,
                          recv_mono=ds.mono,
                          note="from DISTANCE_SENSOR (cm), not RANGEFINDER")
        return prev.altitude

    def _surftrak_target(self, fast: dict, prev: NavSnapshot) -> Reading:
        """The Surftrak setpoint, and only when it genuinely exists.

        Two traps, both closed here.

        `NAMED_VALUE_FLOAT` is one message carrying nine different variables
        on this firmware -- ArduSub's `send_info()` emits CamTilt, CamPan,
        TetherTrn, Lights1, Lights2, PilotGain, InputHold, RollPitch and
        RFTarget back to back. mavlink2rest keeps the most recent, whichever
        that is. So the name is checked every read, and a reply holding some
        other variable is not a target sample.

        `ModeSurftrak` uses -1 cm for "no target", which arrives as -0.01 m.
        Anything at or below zero is the absence of a target, not a target
        below the seabed.
        """
        nv = fast.get("NAMED_VALUE_FLOAT")
        if nv is None or not nv.fresh:
            return prev.surftrak_target
        if nv.get("name") != RFTARGET_KEY:
            # Not an error: the burst simply landed on another variable.
            return prev.surftrak_target
        v = nv.num("value")
        src = Source("mav:NAMED_VALUE_FLOAT/RFTarget", "NAMED_VALUE_FLOAT",
                     self.mav.system, self.mav.component, device="Surftrak")
        if v is None:
            return prev.surftrak_target
        if v <= RFTARGET_INVALID_MAX:
            return M.invalid(
                "Surftrak has no target — ArduSub publishes −0.01 m when the "
                "target is unset or has been reset",
                value=v, unit="m", source=src)
        return M.good(v, unit="m", frame="bottom", source=src,
                      recv_mono=nv.mono)

    def _speed(self, fast: dict, prev: NavSnapshot) -> Reading:
        """Horizontal speed over the ground, in m/s.

        Prefers the EKF's earth-frame NED velocity, whose horizontal magnitude
        is a genuine speed over ground regardless of how the vehicle is
        attitudes. `VFR_HUD.groundspeed` is the fallback for before the
        estimator has a horizontal solution -- on deck, mostly.

        A DVL that has lost bottom lock stops contributing to the EKF, and the
        EKF's velocity then decays rather than reading zero; that is the
        estimator's honest answer and is shown as such. What must never happen
        is a hard zero presented as a measured speed, which is what reading the
        DVL's own report directly would give.
        """
        if not fast:
            return prev.speed
        lp = fast.get("LOCAL_POSITION_NED")
        if lp is not None and lp.fresh:
            vx, vy = lp.num("vx"), lp.num("vy")
            if vx is not None and vy is not None:
                src = Source("mav:LOCAL_POSITION_NED.v", "LOCAL_POSITION_NED",
                             self.mav.system, self.mav.component,
                             device="EKF earth-frame velocity")
                return M.good(math.hypot(vx, vy), unit="m/s", frame="ned",
                              source=src, recv_mono=lp.mono,
                              note="EKF horizontal velocity over ground")
        hud = fast.get("VFR_HUD")
        if hud is not None and hud.fresh:
            gs = hud.num("groundspeed")
            if gs is not None:
                src = Source("mav:VFR_HUD.groundspeed", "VFR_HUD",
                             self.mav.system, self.mav.component)
                return M.good(gs, unit="m/s", source=src, recv_mono=hud.mono,
                              note="VFR_HUD groundspeed — the EKF has no "
                                   "horizontal velocity solution yet")
        return prev.speed

    def _power(self, fast: dict, prev: NavSnapshot, s: NavSnapshot,
               now: float) -> None:
        b = fast.get("BATTERY_STATUS")
        if b is None:
            s.voltage, s.current, s.watts = prev.voltage, prev.current, prev.watts
            s.energy_wh, s.peak_w = prev.energy_wh, prev.peak_w
            return
        src = Source("mav:BATTERY_STATUS", "BATTERY_STATUS", self.mav.system,
                     self.mav.component, device="ROV busbar (BATT_MONITOR 4)")

        # `voltages` is an array of cell millivolts; ArduPilot puts the pack
        # voltage in the first element and 65535 ("not used") in the rest.
        volts = None
        arr = b.get("voltages")
        if isinstance(arr, list) and arr:
            mv = arr[0]
            if isinstance(mv, (int, float)) and 0 < mv < 65535:
                volts = mv / 1000.0
        if volts is None:
            mv = b.num("voltage_battery")
            if mv is not None and 0 < mv < 65535:
                volts = mv / 1000.0

        ca = b.num("current_battery")
        amps = None if ca is None or ca <= -1 else ca / 100.0

        if volts is not None:
            s.voltage = M.good(volts, unit="V", source=src, recv_mono=b.mono)
        else:
            s.voltage = M.invalid("no usable pack voltage", source=src)
        if amps is not None:
            s.current = M.good(amps, unit="A", source=src, recv_mono=b.mono)
        else:
            s.current = M.invalid(
                "current_battery is −1: no current sensor reading", source=src)

        watts = self.energy.update(volts, amps, mono=b.mono, fresh=b.fresh,
                                   counter=b.counter)
        if watts is None:
            s.watts = M.invalid("voltage and current are not both usable",
                                source=src)
        elif b.fresh:
            s.watts = M.good(watts, unit="W", source=src, recv_mono=b.mono,
                             note="busbar V × I from one BATTERY_STATUS")
        else:
            s.watts = M.good(watts, unit="W", source=src,
                             recv_mono=b.mono).staled("no new BATTERY_STATUS")

        st = self.energy.state
        s.energy_wh = M.good(st.wh, unit="Wh", source=src, recv_mono=now,
                             note=self.energy.note())
        s.peak_w = (M.good(st.peak_w, unit="W", source=src, recv_mono=now,
                           note="highest valid sample this flight")
                    if st.peak_w > 0 else
                    Reading(quality=Quality.NEVER_RECEIVED, unit="W",
                            source=src, note="no valid power sample yet"))

    # -- position -----------------------------------------------------------

    def _position(self, fast: dict, prev: NavSnapshot, s: NavSnapshot,
                  now: float) -> None:
        """The ROV's position, from the best source that is actually valid.

        Order of preference, and why:

        1. `GLOBAL_POSITION_INT` — the estimator's own global answer, already
           through the origin and every source the EKF is fusing. On this
           fleet it reads 0/0 whenever the EKF has no origin, which
           `valid_latlon` rejects, so the fallback is reached exactly when it
           should be.
        2. `LOCAL_POSITION_NED` projected through a **confirmed** origin. Right
           relative to itself, drifting as a whole. Only ever used with an
           origin this program has read back from the vehicle.
        3. Nothing. The map shows a local-meter view and says so, rather than
           claiming a geographic position it does not have.

        **A cycle that did not read the fast group does not age it.** The two
        groups run on different cadences, and a slow-only cycle has not looked
        at the position at all -- concluding from that that it has gone stale
        would blink the map to "last known" every couple of seconds on a
        perfectly healthy vehicle. The same applies to the vessel, which is
        read on the extension cadence.
        """
        if not fast:
            s.local_ned = prev.local_ned
            s.rov_fix = prev.rov_fix
            s.vessel_fix = prev.vessel_fix
            s.vessel_heading = prev.vessel_heading
            return

        lp = fast.get("LOCAL_POSITION_NED")
        if lp is not None and lp.fresh:
            n, e, d = lp.num("x"), lp.num("y"), lp.num("z")
            if n is not None and e is not None:
                s.local_ned = M.good(
                    (n, e, d), unit="m", frame="ned",
                    source=Source("mav:LOCAL_POSITION_NED",
                                  "LOCAL_POSITION_NED", self.mav.system,
                                  self.mav.component),
                    recv_mono=lp.mono)
        else:
            s.local_ned = prev.local_ned

        fix = None
        g = fast.get("GLOBAL_POSITION_INT")
        if g is not None and g.fresh:
            lat, lon = g.num("lat"), g.num("lon")
            if lat is not None and lon is not None:
                lat, lon = lat * 1e-7, lon * 1e-7
                if M.valid_latlon(lat, lon):
                    fix = Fix(lat=lat, lon=lon, kind="ekf", quality=Quality.OK,
                              source=Source("mav:GLOBAL_POSITION_INT",
                                            "GLOBAL_POSITION_INT",
                                            self.mav.system, self.mav.component),
                              recv_mono=g.mono, recv_time=g.wall,
                              segment=self._segment)

        if fix is None and self._origin_confirmed and self._origin:
            ned = s.local_ned
            if ned.ok and isinstance(ned.value, tuple):
                from . import geo
                n, e = ned.value[0], ned.value[1]
                lat, lon = geo.offset_ned(self._origin[0], self._origin[1], n, e)
                fix = Fix(lat=lat, lon=lon, kind="dead", quality=Quality.OK,
                          source=Source("derived:NED+origin",
                                        "LOCAL_POSITION_NED"),
                          recv_mono=ned.recv_mono, recv_time=ned.recv_time,
                          segment=self._segment,
                          note="dead-reckoned from the confirmed EKF origin")

        if fix is not None:
            s.rov_fix = fix
            # After `s.rov_fix` is set, so the judgement is made about the
            # position actually being recorded.
            self._add_rov_point(fix, now,
                                TR.track_state(s, now,
                                               profile_key=self.profile_key)[0])
        elif prev.rov_fix is not None:
            # Keep the last position, clearly marked. A vanished marker tells
            # the operator nothing; a stale one tells them where it was.
            s.rov_fix = replace(prev.rov_fix, quality=Quality.STALE,
                                note="no current position")

        # -- the vessel ------------------------------------------------------
        v = self._vessel
        if v.lat is not None and v.lon is not None:
            vfix = Fix(lat=v.lat, lon=v.lon, kind="vessel", quality=Quality.OK,
                       source=Source("ext:ugps_external", "/status"),
                       recv_mono=v.mono, recv_time=time.time(),
                       segment=self._segment)
            s.vessel_fix = vfix
            self._add_vessel_point(vfix, now)
        elif prev.vessel_fix is not None:
            s.vessel_fix = replace(prev.vessel_fix, quality=Quality.STALE,
                                   note="no current vessel position")
        s.vessel_heading = E.vessel_readings(v).get("heading", M.unknown)

    def _add_rov_point(self, fix: Fix, now: float,
                       trust: str = "unknown") -> None:
        last = self._last_rov
        if last is not None:
            from . import geo
            gap = max(1e-3, fix.recv_mono - last.mono) if fix.recv_mono else 1.0
            d = geo.distance_m(last.lat, last.lon, fix.lat, fix.lon)
            if d > JUMP_MIN_M and d / gap > JUMP_SPEED_MS:
                self._break_track(
                    f"position jumped {d:.0f} m in {gap:.1f} s")
                fix = replace(fix, segment=self._segment)
                # Kept for the map and for "What changed?", with what the
                # position was resting on either side of the jump. A jump is
                # an observation; what caused it is a separate question.
                self.jumps.append(Jump(
                    mono=fix.recv_mono or now, wall=time.time(),
                    meters=d, seconds=gap, segment=self._segment,
                    from_trust=last.trust, to_trust=trust))
                while len(self.jumps) > JUMPS_MAX:
                    self.jumps.pop(0)
        p = TrackPoint(fix.lat, fix.lon, fix.recv_mono or now,
                       fix.recv_time or time.time(), fix.segment, fix.kind,
                       trust)
        self.rov_track.append(p)
        self._last_rov = p
        self._emit("rov_fix", fix.to_json())

    def _add_vessel_point(self, fix: Fix, now: float) -> None:
        last = self._last_vessel
        # Anchor swing is the point of the vessel track: identical successive
        # positions are dropped so the deque is not filled by a stationary
        # boat, but nothing older is ever discarded for overlapping.
        if last is not None and abs(last.lat - fix.lat) < 1e-7 \
                and abs(last.lon - fix.lon) < 1e-7:
            return
        p = TrackPoint(fix.lat, fix.lon, fix.recv_mono or now,
                       fix.recv_time or time.time(), fix.segment, "vessel")
        self.vessel_track.append(p)
        self._last_vessel = p
        self._emit("vessel_fix", fix.to_json())

    # -- the suite ----------------------------------------------------------

    def _dvl_readings(self, fast: dict, slow: dict, now: float) -> dict[str, Reading]:
        d = self._dvl
        src = Source("ext:dvl", "/get_status", device="Water Linked DVL")
        out: dict[str, Reading] = {}
        if not d.reachable:
            out["extension"] = Reading(
                quality=Quality.NEVER_RECEIVED, source=src,
                note=d.error or "the DVL extension did not answer")
        else:
            out["extension"] = M.good(d.status_text or "running", source=src,
                                      recv_mono=d.mono)
            out["enabled"] = (M.good(d.enabled, source=src, recv_mono=d.mono)
                              if d.enabled is not None else M.unknown)
            if d.message_type:
                usable = d.message_type == "POSITION_ESTIMATE"
                out["message_type"] = M.good(
                    d.message_type, source=src, recv_mono=d.mono,
                    note=("absolute external-nav position — EKF3 can enter "
                          "absolute aiding"
                          if usable else
                          "body-frame odometry only — EKF3 stays in relative "
                          "aiding and cannot produce a geographic position "
                          "from this alone"))
                if not usable:
                    out["message_type"] = out["message_type"].invalid(
                        out["message_type"].note)
            if d.cached_origin:
                out["cached_origin"] = M.good(
                    d.cached_origin, source=src, recv_mono=d.mono,
                    note="the extension's own saved value — not proof the EKF "
                         "origin was set")

        # Bottom lock, judged from the autopilot's range rather than the
        # extension: it is the observation that matters to the estimator.
        rf = fast.get("RANGEFINDER")
        if rf is not None:
            dist = rf.num("distance")
            out["bottom_lock"] = (
                M.good(True, source=Source("mav:RANGEFINDER", "RANGEFINDER"),
                       recv_mono=rf.mono)
                if rf.fresh and dist and dist > 0 else
                M.invalid("no valid downward range",
                          source=Source("mav:RANGEFINDER", "RANGEFINDER")))
        return out

    def _ekf_readings(self, slow: dict, now: float) -> dict[str, Reading]:
        out: dict[str, Reading] = {}
        e = slow.get("EKF_STATUS_REPORT")
        if e is None or not e.fresh:
            return out
        src = Source("mav:EKF_STATUS_REPORT", "EKF_STATUS_REPORT",
                     self.mav.system, self.mav.component)
        flags = e.get("flags")
        names = _flag_names(flags)
        out["flags"] = M.good(", ".join(names) if names else "none",
                              source=src, recv_mono=e.mono)
        # The three that decide whether the map has anything to draw.
        out["horiz_pos_abs"] = M.good("EKF_POS_HORIZ_ABS" in names, source=src,
                                      recv_mono=e.mono,
                                      note="absolute horizontal position")
        out["horiz_pos_rel"] = M.good("EKF_POS_HORIZ_REL" in names, source=src,
                                      recv_mono=e.mono,
                                      note="relative horizontal position")
        out["const_pos_mode"] = M.good(
            "EKF_CONST_POS_MODE" in names, source=src, recv_mono=e.mono,
            note="the estimator has no position aiding at all")
        for key, field_ in (("velocity_variance", "velocity_variance"),
                            ("pos_horiz_variance", "pos_horiz_variance"),
                            ("pos_vert_variance", "pos_vert_variance"),
                            ("compass_variance", "compass_variance")):
            v = e.num(field_)
            if v is not None:
                out[key] = M.good(v, source=src, recv_mono=e.mono)
        return out

    def _ugps_readings(self, slow: dict, now: float) -> dict[str, Reading]:
        """The acoustic solution, read as the autopilot received it."""
        out: dict[str, Reading] = {}
        g = slow.get("GPS_RAW_INT")
        src = Source("mav:GPS_RAW_INT", "GPS_RAW_INT", self.mav.system,
                     self.mav.component,
                     device="Water Linked UGPS via GPS_INPUT")
        if g is None:
            return out
        st = E.acoustic_from_gps_input(g)
        fix_ok = st.fix_type is not None and st.fix_type >= 2
        out["fix"] = (
            M.good(f"fix type {st.fix_type}", source=src, recv_mono=g.mono)
            if fix_ok else
            M.invalid(
                "no acoustic position — the extension sets fix_type 0 when "
                "the acoustic solution is invalid",
                value=st.fix_type, source=src))
        if st.std_m is not None:
            out["std_m"] = M.good(
                st.std_m, unit="m", source=src, recv_mono=g.mono,
                note="acoustic standard deviation, carried in GPS_INPUT.vdop "
                     "by the Water Linked extension — not a vertical DOP")
        if st.satellites is not None:
            out["satellites"] = M.good(
                st.satellites, source=src, recv_mono=g.mono,
                note="the topside receiver's satellites, and forced to at "
                     "least 6 under --ignore_gps — not a measure of "
                     "underwater position quality")
        if st.lat is not None:
            out["position"] = M.good((st.lat, st.lon), frame="wgs84",
                                     source=src, recv_mono=g.mono)
        return out


def _armed_from_base_mode(base) -> bool | None:
    """MAV_MODE_FLAG_SAFETY_ARMED out of whatever shape base_mode arrived in.

    mavlink2rest renders bitmasks as either an integer or a `{"bits": n}`
    object depending on the dialect build, so both are handled rather than
    one being assumed.
    """
    if isinstance(base, dict):
        base = base.get("bits", base.get("value"))
    if isinstance(base, bool) or not isinstance(base, (int, float)):
        return None
    return bool(int(base) & 128)


def _flag_names(flags) -> set[str]:
    """EKF_STATUS_REPORT flags, from the string or bitfield mavlink2rest sends."""
    if isinstance(flags, str):
        return {f.strip() for f in flags.split("|") if f.strip()}
    if isinstance(flags, dict):
        inner = flags.get("bits", flags.get("type"))
        if isinstance(inner, str):
            return {f.strip() for f in inner.split("|") if f.strip()}
        flags = inner
    if isinstance(flags, (int, float)):
        bits = int(flags)
        table = ((1, "EKF_ATTITUDE"), (2, "EKF_VELOCITY_HORIZ"),
                 (4, "EKF_VELOCITY_VERT"), (8, "EKF_POS_HORIZ_REL"),
                 (16, "EKF_POS_HORIZ_ABS"), (32, "EKF_POS_VERT_ABS"),
                 (64, "EKF_POS_VERT_AGL"), (128, "EKF_CONST_POS_MODE"),
                 (256, "EKF_PRED_POS_HORIZ_REL"),
                 (512, "EKF_PRED_POS_HORIZ_ABS"), (1024, "EKF_UNINITIALIZED"))
        return {name for bit, name in table if bits & bit}
    return set()
