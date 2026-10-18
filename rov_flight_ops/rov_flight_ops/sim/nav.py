"""
The simulated vehicle's MAVLink, and the Navigation tab's collector for it.

**The simulator answers at the HTTP layer, not above it.** `SimMav` is a
`Mavlink2Rest` whose GETs are answered here, in mavlink2rest's own shapes
(`{"message": ..., "status": {"time": {"counter": ...}}}`), so everything a
real vehicle's answer goes through -- the counter that decides whether a
message is new, the health of each message, the "never sent by this vehicle"
case -- runs unchanged. `SimNavCollector` is the real `NavCollector` with that
connection in place of a live one and the two extensions it reads answered
from the same world. Every judgement the Navigation tab draws -- the sensor
matrix, aiding mode, the track's trust colours, position jumps -- is the
program's own, made about simulated evidence.

Writes are refused exactly as they are on a live vehicle with writes locked:
`SimMav` is built with `allow_writes=False` and never unlocked.

The parameters are the vehicle's as they stood at the end of the 15 October
2026 flight: the acoustic profile, POSITION_DELTA from the DVL, the EKF origin
on Pier 59.
"""

from __future__ import annotations

import json
import math
import time
from urllib.parse import urlsplit

from .. import paramfile
from ..nav import collector as C
from ..nav import extensions as E
from ..nav import geo
from ..nav import mav2rest as M2R
from .schedule import NAV
from .world import ORIGIN, TOPSIDE, TOPSIDE_HEADING, SimWorld

#: Never resolves, so nothing that bypasses the simulator can reach a network.
SIM_HOST = "simulated-rov.invalid"

#: The navigation parameters from the 15 October 2026 flight record: what
#: the simulated vehicle has if the reference file cannot be read.
PARAMS: dict[str, float] = {
    "AHRS_EKF_TYPE": 3.0, "AHRS_GPS_USE": 1.0, "AHRS_ORIENTATION": 16.0,
    "AHRS_ORIG_LAT": ORIGIN[0], "AHRS_ORIG_LON": ORIGIN[1], "AHRS_ORIG_ALT": 0.0,
    "ARMING_CHECK": 448.0, "BATT_MONITOR": 4.0, "COMPASS_ENABLE": 1.0,
    "COMPASS_USE": 1.0, "EK3_ENABLE": 1.0, "EK3_SRC1_POSXY": 3.0,
    "EK3_SRC1_POSZ": 1.0, "EK3_SRC1_VELXY": 6.0, "EK3_SRC1_VELZ": 0.0,
    "EK3_SRC1_YAW": 1.0, "EK3_SRC2_POSXY": 0.0, "EK3_SRC2_POSZ": 1.0,
    "EK3_SRC2_VELXY": 0.0, "EK3_SRC2_YAW": 0.0, "EK3_SRC_OPTIONS": 1.0,
    "EK3_POSNE_M_NSE": 0.5, "EK3_VELNE_M_NSE": 0.5, "EK3_POS_I_GATE": 500.0,
    "EK3_VEL_I_GATE": 500.0, "FRAME_CONFIG": 2.0, "FS_EKF_ACTION": 0.0,
    "FS_EKF_THRESH": 0.8, "FS_GCS_ENABLE": 2.0, "FS_LEAK_ENABLE": 1.0,
    "FS_PILOT_INPUT": 2.0, "FS_PILOT_TIMEOUT": 3.0, "GPS_TYPE": 14.0,
    "GPS_DELAY_MS": 200.0, "GPS_RATE_MS": 200.0, "ORIGIN_LAT": 47.62691,
    "ORIGIN_LON": -122.39018, "ORIGIN_ALT": 0.0, "RNGFND1_TYPE": 10.0,
    "RNGFND1_ORIENT": 25.0, "RNGFND1_MAX_CM": 5000.0, "RNGFND1_MIN_CM": 20.0,
    "SURFTRAK_DEPTH": -100.0, "VISO_TYPE": 1.0, "VISO_DELAY_MS": 10.0,
    "VISO_POS_M_NSE": 0.2, "VISO_VEL_M_NSE": 0.1, "VISO_YAW_M_NSE": 0.2,
    "WPNAV_SPEED": 20.0,
}

#: Where the simulated vehicle differs from Nereo's reference file, so the
#: Parameters tab has something to show. Each kind on purpose:
DIFFERENCES = {
    # Two key parameters somebody changed and did not change back.
    "JS_GAIN_DEFAULT": 0.5,
    "JS_LIGHTS_STEPS": 4.0,
    # Two others, which only the second section lists.
    "PSC_VELXY_P": 6.0,
    "NTF_LED_BRIGHT": 1.0,
    # Ones the autopilot rewrites by itself, which are not differences.
    "STAT_RUNTIME": 1_284_311.0, "STAT_FLTTIME": 102_377.0, "STAT_BOOTCNT": 412.0,
    "BARO1_GND_PRESS": 101_822.4, "BARO2_GND_PRESS": 102_160.9,
    "COMPASS_DEC": 0.2655291, "SR0_EXTRA1": 4.0, "SR0_POSITION": 3.0,
    # And the site's origin, which belongs to the site and not to the file.
    "ORIGIN_LAT": ORIGIN[0], "ORIGIN_LON": ORIGIN[1],
}


def vehicle_params() -> dict[str, float]:
    """Every parameter the simulated vehicle has: Nereo's reference, as the
    real one would have it, with `DIFFERENCES` on top."""
    ref = paramfile.read(paramfile.reference_path("Nereo"))
    params = dict(ref.params) if ref.params else dict(PARAMS)
    params.update(PARAMS)            # the navigation set flown on 15 October
    params.update(DIFFERENCES)
    return params


#: How often the autopilot sends each message, as mavlink2rest measured them.
RATES = {
    "ATTITUDE": 10.0, "VFR_HUD": 4.0, "GLOBAL_POSITION_INT": 5.0,
    "LOCAL_POSITION_NED": 5.0, "RANGEFINDER": 9.2, "NAMED_VALUE_FLOAT": 10.0,
    "BATTERY_STATUS": 1.0, "HEARTBEAT": 1.0, "EKF_STATUS_REPORT": 2.0,
    "GPS_RAW_INT": 5.0, "SYS_STATUS": 2.0, "SCALED_PRESSURE2": 2.0,
    "VIBRATION": 2.0, "SYSTEM_TIME": 1.0, "DISTANCE_SENSOR": 9.2,
    "STATUSTEXT": 0.02,
}

#: EKF_STATUS_REPORT flag bits.
F_ATT, F_VH, F_VV, F_REL, F_ABS, F_VABS = 1, 2, 4, 8, 16, 32
F_PRED_REL, F_PRED_ABS = 256, 512
FLAGS_ABSOLUTE = F_ATT | F_VH | F_VV | F_REL | F_ABS | F_VABS | F_PRED_REL | F_PRED_ABS
FLAGS_RELATIVE = F_ATT | F_VH | F_VV | F_REL | F_VABS | F_PRED_REL

ARDUSUB_MODE = {"Stabilize": 0, "Depth Hold": 2, "Surftrak": 21,
                "Position Hold": 16, "Manual": 19}


def _iso(wall: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wall)) + \
        f".{int((wall % 1) * 1e6):06d}Z"


class SimMav(M2R.Mavlink2Rest):
    """mavlink2rest on the simulated vehicle, answering in its own JSON."""

    def __init__(self, world: SimWorld, *, system: int = 1, component: int = 1,
                 timeout: float = 4.0) -> None:
        super().__init__(SIM_HOST, system=system, component=component,
                         allow_writes=False, timeout=timeout)
        self.world = world

    def _fetch(self, url: str, timeout: float | None = None) -> M2R.Answer:
        t = self.world.t()
        if self.world.pi_silent(t):
            # The Pi has dropped off the network: nothing answers at all.
            return M2R.Answer(False, None, error="timed out", seconds=self.timeout)
        path = urlsplit(url).path
        if path.rstrip("/") == "/v1/mavlink/vehicles":
            return self._json({"1": {"components": {"1": {}, "194": {}}},
                               "255": {"components": {"0": {}}}})
        if path.rstrip("/") == "/v1/mavlink":
            return self._json({"vehicles": {"1": {}, "255": {}}})
        if path == "/info":
            return self._json({"service": {"name": "mavlink2rest (simulated)",
                                           "version": "t0.11.25"}})
        parts = path.strip("/").split("/")
        if len(parts) == 8 and parts[:3] == ["v1", "mavlink", "vehicles"]:
            try:
                sysid, comp = int(parts[3]), int(parts[5])
            except ValueError:
                return M2R.Answer(False, 404, error="HTTP 404")
            got = message(self.world, sysid, comp, parts[7], t)
            if got is None:
                return M2R.Answer(False, 404, error="HTTP 404")
            body, rate, flowing = got
            counter = self.world.count((sysid, comp, parts[7]), rate, t, flowing)
            return self._json({"message": body, "status": {"time": {
                "counter": counter, "frequency": rate,
                "first_update": _iso(self.world.wall(0.0)),
                "last_update": _iso(self.world.wall(t))}}})
        return M2R.Answer(False, 404, error="HTTP 404")

    @staticmethod
    def _json(obj) -> M2R.Answer:
        return M2R.Answer(True, 200, json.dumps(obj), seconds=0.004)

    # Refused whatever the write lock says: a simulated vehicle has nowhere
    # for a command to go, and pretending it was accepted would teach the
    # wrong thing.
    def request_message(self, message_id: int, *, reason: str = "") -> M2R.Answer:
        raise M2R.VehicleWriteRefused(
            f"REQUEST_MESSAGE({message_id}) was refused: this is the ROV flight "
            f"simulator, which sends nothing to any vehicle")

    def send(self, payload: dict, *, what: str) -> M2R.Answer:
        raise M2R.VehicleWriteRefused(
            f"{what} was refused: this is the ROV flight simulator, which sends "
            f"nothing to any vehicle")


# --------------------------------------------------------------------------
#  the messages
# --------------------------------------------------------------------------


def _nav_fault(world: SimWorld, t: float) -> tuple[str, float]:
    f, p = world.fault(NAV, t)
    return (f.key if f else ""), p


def _jump_offset(key: str, p: float) -> tuple[float, float]:
    """The estimator reset: a 15 m jump to the north-east that converges back."""
    if key != "estimator_reset":
        return 0.0, 0.0
    into = p * 30.0
    if into < 3.0:
        return 0.0, 0.0
    k = 15.0 * math.exp(-(into - 3.0) / 9.0)
    return k * 0.8, k * 0.6


def message(world: SimWorld, sysid: int, comp: int, name: str, t: float):
    """(body, rate Hz, flowing) for one message, or None if never sent."""
    v = world.vehicle(t)
    key, p = _nav_fault(world, t)
    wall = world.wall(t)
    boot_ms = int((t + 3600.0) * 1000)

    if (sysid, comp) == (255, 0):
        return _extension_message(world, name, t, v)
    if (sysid, comp) != (1, 1):
        return None

    heading = v.heading
    if key == "compass_interference":
        heading = (heading + 20.0 * math.sin(t / 2.3) + 8.0 * math.sin(t / 0.9)) % 360.0
    dn, de = _jump_offset(key, p)
    north, east = v.north + dn, v.east + de

    if name == "HEARTBEAT":
        base = 81 | (128 if v.armed else 0)
        return ({"type": "HEARTBEAT", "autopilot": {"type": "MAV_AUTOPILOT_ARDUPILOTMEGA"},
                 "mavtype": {"type": "MAV_TYPE_SUBMARINE"},
                 "base_mode": {"bits": base}, "custom_mode": ARDUSUB_MODE.get(v.mode, 2),
                 "system_status": {"type": "MAV_STATE_ACTIVE" if v.armed
                                   else "MAV_STATE_STANDBY"},
                 "mavlink_version": 3}, RATES[name], True)
    if name == "ATTITUDE":
        return ({"type": "ATTITUDE", "time_boot_ms": boot_ms,
                 "roll": math.radians(v.roll), "pitch": math.radians(v.pitch),
                 "yaw": math.radians(((heading + 180) % 360) - 180),
                 "rollspeed": 0.0, "pitchspeed": 0.0, "yawspeed": 0.0},
                RATES[name], True)
    if name == "VFR_HUD":
        return ({"type": "VFR_HUD", "airspeed": 0.0, "groundspeed": v.speed,
                 "heading": int(round(heading)) % 360, "throttle": 50,
                 "alt": -v.depth, "climb": -v.v_down}, RATES[name], True)
    if name == "GLOBAL_POSITION_INT":
        lat, lon = geo.offset_ned(ORIGIN[0], ORIGIN[1], north, east)
        return ({"type": "GLOBAL_POSITION_INT", "time_boot_ms": boot_ms,
                 "lat": int(round(lat * 1e7)), "lon": int(round(lon * 1e7)),
                 "alt": int(-v.depth * 1000), "relative_alt": int(-v.depth * 1000),
                 "vx": int(v.v_north * 100), "vy": int(v.v_east * 100),
                 "vz": int(v.v_down * 100), "hdg": int(heading * 100)},
                RATES[name], True)
    if name == "LOCAL_POSITION_NED":
        vn, ve = v.v_north, v.v_east
        if key == "dvl_lock_lost":
            # No DVL velocity: the estimator's own velocity wanders.
            vn += 0.06 * math.sin(t / 1.7)
            ve += 0.06 * math.cos(t / 2.1)
        return ({"type": "LOCAL_POSITION_NED", "time_boot_ms": boot_ms,
                 "x": north, "y": east, "z": v.depth,
                 "vx": vn, "vy": ve, "vz": v.v_down}, RATES[name], True)
    if name == "RANGEFINDER":
        dist = 0.0 if key == "dvl_lock_lost" else v.altitude
        return ({"type": "RANGEFINDER", "distance": dist, "voltage": 0.0},
                RATES[name], True)
    if name == "DISTANCE_SENSOR":
        cm = 0 if key == "dvl_lock_lost" else int(round(v.altitude * 100))
        return ({"type": "DISTANCE_SENSOR", "time_boot_ms": boot_ms,
                 "min_distance": 20, "max_distance": 5000, "current_distance": cm,
                 "mavtype": {"type": "MAV_DISTANCE_SENSOR_UNKNOWN"}, "id": 1,
                 "orientation": {"type": "MAV_SENSOR_ROTATION_PITCH_270"},
                 "covariance": 0}, RATES[name], True)
    if name == "NAMED_VALUE_FLOAT":
        # ArduSub cycles nine names through this one message; the burst is read
        # on whichever is newest, and one read in nine is the Surftrak target.
        names = ("CamTilt", "CamPan", "TetherTrn", "Lights1", "Lights2",
                 "PilotGain", "InputHold", "RollPitch", "RFTarget")
        which = names[int(t * 10) % len(names)]
        value = {"RFTarget": v.surftrak_target if v.surftrak_target else -0.01,
                 "PilotGain": 0.5, "Lights1": 1700.0, "Lights2": 1700.0,
                 "CamTilt": 1500.0}.get(which, 0.0)
        return ({"type": "NAMED_VALUE_FLOAT", "time_boot_ms": boot_ms,
                 "name": which, "value": value}, RATES[name], True)
    if name == "BATTERY_STATUS":
        mv = int(v.volts * 1000)
        return ({"type": "BATTERY_STATUS", "id": 0,
                 "voltages": [mv] + [65535] * 9,
                 "current_battery": int(v.amps * 100), "current_consumed": -1,
                 "energy_consumed": -1, "battery_remaining": -1,
                 "temperature": 32767}, RATES[name], True)
    if name == "EKF_STATUS_REPORT":
        flags = FLAGS_ABSOLUTE
        vel_var, pos_var, comp_var = 0.08, 0.12, 0.05
        if key == "acoustic_lost" and p * 30 >= 4:
            flags, pos_var = FLAGS_RELATIVE, 0.6
        if key == "vessel_nmea_lost" and p * 30 >= 8:
            flags, pos_var = FLAGS_RELATIVE, 0.5
        if key == "dvl_lock_lost":
            vel_var = 0.55
        if key == "compass_interference":
            comp_var = 0.82
        if key == "estimator_reset" and 3 <= p * 30 < 9:
            pos_var = 0.9
        return ({"type": "EKF_STATUS_REPORT", "flags": {"bits": flags},
                 "velocity_variance": vel_var, "pos_horiz_variance": pos_var,
                 "pos_vert_variance": 0.04, "compass_variance": comp_var,
                 "terrain_alt_variance": 0.0, "airspeed_variance": 0.0},
                RATES[name], True)
    if name == "GPS_RAW_INT":
        lost = (key == "acoustic_lost"
                or (key == "vessel_nmea_lost" and p * 30 >= 4))
        lat, lon = geo.offset_ned(ORIGIN[0], ORIGIN[1], v.north, v.east)
        std = 0.42 + 0.08 * math.sin(t / 7.0)
        if key == "ugps_weak_receiver":
            std = 0.9 + 0.2 * math.sin(t / 3.0)
        if key == "ugps_noisy_receiver":
            std = 0.75 + 0.15 * math.sin(t / 2.0)
        return ({"type": "GPS_RAW_INT", "time_usec": int(wall * 1e6),
                 # A number, as the extension's GPS_INPUT arrives: 0 when the
                 # acoustic solution is not valid.
                 "fix_type": 0 if lost else 3,
                 "lat": 0 if lost else int(lat * 1e7),
                 "lon": 0 if lost else int(lon * 1e7),
                 "alt": int(-v.depth * 1000), "eph": 100, "epv": 65535,
                 "vel": 0, "cog": 0, "satellites_visible": 22,
                 "hdop": 0.8, "vdop": std}, RATES[name], True)
    if name == "SYS_STATUS":
        return ({"type": "SYS_STATUS", "voltage_battery": int(v.volts * 1000),
                 "current_battery": int(v.amps * 100), "battery_remaining": -1,
                 "load": 420, "drop_rate_comm": 0, "errors_comm": 0},
                RATES[name], True)
    if name == "SCALED_PRESSURE2":
        press = 1013.25 + v.depth * 100.5
        return ({"type": "SCALED_PRESSURE2", "time_boot_ms": boot_ms,
                 "press_abs": press, "press_diff": 0.0, "temperature": 1290},
                RATES[name], True)
    if name == "VIBRATION":
        return ({"type": "VIBRATION", "vibration_x": 0.6, "vibration_y": 0.7,
                 "vibration_z": 0.9, "clipping_0": 0, "clipping_1": 0,
                 "clipping_2": 0}, RATES[name], True)
    if name == "SYSTEM_TIME":
        return ({"type": "SYSTEM_TIME", "time_unix_usec": int(wall * 1e6),
                 "time_boot_ms": boot_ms}, RATES[name], True)
    if name == "STATUSTEXT":
        leaking = key == "leak_detected"
        text = "Leak Detected" if leaking else "EKF3 IMU0 is using GPS"
        return ({"type": "STATUSTEXT",
                 "severity": {"type": "MAV_SEVERITY_CRITICAL" if leaking
                              else "MAV_SEVERITY_INFO"},
                 "text": list(text.ljust(50, chr(0))), "id": 0, "chunk_seq": 0},
                # ArduSub repeats the leak warning; the rest is said once.
                0.5 if leaking else RATES[name], True)
    # GPS_GLOBAL_ORIGIN and AUTOPILOT_VERSION come only when asked for.
    return None


def _extension_message(world: SimWorld, name: str, t: float, v):
    """What the BlueOS DVL extension has sent, as system 255 component 0.

    It forwards only valid reports, so its rate follows the simulated DVL's
    and stops when the DVL tab's fault stops the reports or the extension.
    """
    from .dvl import extension_rate
    rate, running = extension_rate(world, t)
    if name == "VISION_POSITION_DELTA":
        return ({"type": "VISION_POSITION_DELTA", "time_usec": int(world.wall(t) * 1e6),
                 "time_delta_usec": 150000, "angle_delta": [0.0, 0.0, 0.0],
                 "position_delta": [v.v_fwd * 0.15, v.v_right * 0.15, 0.0],
                 "confidence": 90.0}, 0.73 * rate, running and rate > 0)
    if name == "DISTANCE_SENSOR":
        return ({"type": "DISTANCE_SENSOR", "current_distance": int(v.altitude * 100),
                 "min_distance": 5, "max_distance": 5000, "id": 1,
                 "orientation": {"type": "MAV_SENSOR_ROTATION_PITCH_270"}},
                rate, running and rate > 0)
    return None                    # POSITION_DELTA mode: nothing else is sent


# --------------------------------------------------------------------------
#  the collector
# --------------------------------------------------------------------------


class SimNavCollector(C.NavCollector):
    """The Navigation tab's own collector, reading the simulated vehicle."""

    def __init__(self, world: SimWorld, *, on_event=None) -> None:
        super().__init__(SIM_HOST, allow_writes=False, on_event=on_event)
        self.world = world
        self.mav = SimMav(world)
        self.set_parameter_reader(vehicle_params)

    def start(self, session_id: str = "") -> None:
        super().start(session_id)
        self._link.host = "simulated ROV"

    def _discover(self) -> None:
        self.services = {
            "dvl": E.Service("Water Linked DVL", port=32768, found=True, via="helper"),
            "ugps_external": E.Service("WL UGPS External", port=32770, found=True,
                                       via="helper"),
            "ugps": E.Service("Water Linked UGPS", via="absent",
                              note="not found in the BlueOS service list"),
        }
        self._emit("discovery", {"services": {
            k: {"name": s.name, "port": s.port, "via": s.via, "note": s.note}
            for k, s in self.services.items()}})

    def _read_extensions(self) -> None:
        t = self.world.t()
        now = time.monotonic()
        if self.world.pi_silent(t):
            return                 # the extensions live on the Pi too
        key, p = _nav_fault(self.world, t)
        self._dvl = E.DvlStatus(
            reachable=True, enabled=True, status_text="Running",
            hostname="192.168.2.95", orientation=1, rangefinder=True,
            message_type="POSITION_DELTA",
            cached_origin=(ORIGIN[0], ORIGIN[1]), mono=now)
        gga = key != "vessel_nmea_lost"
        hdt = gga and key != "vessel_hdt_stale"
        wobble = 0.0000004 * math.sin(t / 11.0)
        prev = self._vessel
        heading = TOPSIDE_HEADING + 0.25 * math.sin(t / 17.0) if hdt else None
        lat = TOPSIDE[0] + wobble if gga else None
        lon = TOPSIDE[1] + wobble if gga else None
        fingerprint = f"{lat}|{lon}|{heading}"
        self._vessel = E.VesselStatus(
            reachable=True, gga_ok=gga, hdt_ok=hdt, inject_ok=gga, lat=lat,
            lon=lon, heading_deg=heading, ugps_host="192.168.2.94",
            send_rate=1.0, mono=now, fingerprint=fingerprint,
            changed_mono=(now if fingerprint != prev.fingerprint
                          else prev.changed_mono or now))
