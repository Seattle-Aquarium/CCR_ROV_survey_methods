"""
Telemetry access and export.

The extractor writes a long-format CSV (t, field, value, sval). This module
turns that into

  * fast point-in-time lookups for the video overlay, and
  * a 1 Hz wide table for the per-flight CSV.

Zero-order hold is the right interpolation here -- these are sampled states,
not continuous signals, and averaging a flight mode or a fix type would be
meaningless. But a held value goes stale: if a stream stops (the DVL drops out,
say) we must not carry its last reading across the rest of the dive. Hence
`max_age`, after which a field reads blank.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

#: ArduSub flight modes (MAV_MODE custom_mode values).
ARDUSUB_MODES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    7: "CIRCLE", 9: "SURFACE", 16: "POSHOLD", 19: "MANUAL",
    20: "MOTOR_DETECT", 21: "SURFTRAK",
}

#: How long a held sample stays valid, per field prefix. Slow, steady streams
#: (mode, gain) tolerate a long hold; fast ones should not be stretched.
DEFAULT_MAX_AGE = 5.0
MAX_AGE = {
    "HEARTBEAT": 30.0,
    "NVF.": 30.0,
    "STATUSTEXT": 0.0,        # events, never held
    "GPS_RAW_INT": 15.0,
    "SYSTEM_TIME": 60.0,
}


def _max_age_for(field_name: str) -> float:
    for prefix, age in MAX_AGE.items():
        if field_name.startswith(prefix):
            return age
    return DEFAULT_MAX_AGE


@dataclass
class Series:
    t: np.ndarray
    v: np.ndarray | None = None          # float values
    s: list[str] | None = None           # string values

    def at(self, when: float, max_age: float) -> float | str | None:
        """Most recent sample at or before `when`, or None if too stale."""
        if len(self.t) == 0:
            return None
        i = int(np.searchsorted(self.t, when, side="right")) - 1
        if i < 0:
            return None
        if max_age > 0 and (when - self.t[i]) > max_age:
            return None
        if self.v is not None:
            val = float(self.v[i])
            return None if math.isnan(val) else val
        assert self.s is not None
        return self.s[i] or None


class TelemetryStore:
    """All telemetry for one flight, indexed by field name."""

    def __init__(self) -> None:
        self.series: dict[str, Series] = {}
        self.t_start: float | None = None
        self.t_end: float | None = None

    # ---- loading -------------------------------------------------------

    @classmethod
    def load(cls, csv_path: str | Path) -> "TelemetryStore":
        store = cls()
        times: dict[str, list[float]] = {}
        vals: dict[str, list[float]] = {}
        strs: dict[str, list[str]] = {}

        with open(csv_path, newline="") as f:
            r = csv.reader(f)
            header = next(r, None)
            for row in r:
                if len(row) < 4:
                    continue
                t_s, name, v_s, s_s = row[0], row[1], row[2], row[3]
                try:
                    t = float(t_s)
                except ValueError:
                    continue
                times.setdefault(name, []).append(t)
                if v_s != "":
                    try:
                        vals.setdefault(name, []).append(float(v_s))
                    except ValueError:
                        vals.setdefault(name, []).append(math.nan)
                    strs.setdefault(name, []).append("")
                else:
                    vals.setdefault(name, []).append(math.nan)
                    strs.setdefault(name, []).append(s_s)

        for name, ts in times.items():
            arr_t = np.asarray(ts, dtype=float)
            order = np.argsort(arr_t, kind="stable")
            arr_t = arr_t[order]
            v = np.asarray(vals[name], dtype=float)[order]
            s_list = [strs[name][i] for i in order]
            numeric = not np.all(np.isnan(v))
            store.series[name] = Series(
                t=arr_t,
                v=v if numeric else None,
                s=None if numeric else s_list,
            )
            if store.t_start is None or arr_t[0] < store.t_start:
                store.t_start = float(arr_t[0])
            if store.t_end is None or arr_t[-1] > store.t_end:
                store.t_end = float(arr_t[-1])
        return store

    # ---- raw access ----------------------------------------------------

    def get(self, name: str, when: float) -> float | str | None:
        s = self.series.get(name)
        if s is None:
            return None
        return s.at(when, _max_age_for(name))

    def num(self, name: str, when: float) -> float | None:
        v = self.get(name, when)
        return v if isinstance(v, (int, float)) else None

    def has(self, name: str) -> bool:
        return name in self.series and len(self.series[name].t) > 0

    def fields(self) -> list[str]:
        return sorted(self.series)

    # ---- derived quantities -------------------------------------------

    def sample(self, when: float) -> dict[str, float | str | None]:
        """Everything the overlay can display, at one instant."""
        n = self.num

        # depth: ArduSub's own baro-derived value, in mm below the surface
        rel = n("GLOBAL_POSITION_INT.relative_alt", when)
        depth = -rel / 1000.0 if rel is not None else None

        # altitude above the seabed
        alt = n("RANGEFINDER.distance", when)
        if alt is None:
            cm = n("DISTANCE_SENSOR.0.current_distance", when)
            alt = cm / 100.0 if cm is not None else None

        # horizontal speed: prefer the EKF's NED velocity, but that only exists
        # once relative aiding starts (after the ROV is in the water), so fall
        # back to VFR_HUD rather than showing nothing on deck.
        vx, vy = n("LOCAL_POSITION_NED.vx", when), n("LOCAL_POSITION_NED.vy", when)
        if vx is not None and vy is not None:
            speed = math.hypot(vx, vy)
        else:
            speed = n("VFR_HUD.groundspeed", when)

        # power: voltage and current arrive in the same BATTERY_STATUS message,
        # so their product is a genuine instantaneous draw.
        mv = n("BATTERY_STATUS.voltage_mv", when)
        ca = n("BATTERY_STATUS.current_battery", when)
        volts = mv / 1000.0 if mv is not None else None
        amps = None if ca is None or ca == -1 else ca / 100.0   # -1 = not measured
        watts = volts * amps if (volts is not None and amps is not None) else None

        temp = n("SCALED_PRESSURE2.temperature", when)
        if temp is None:
            temp = n("SCALED_PRESSURE.temperature", when)

        cm_ = n("HEARTBEAT.custom_mode", when)
        mode = ARDUSUB_MODES.get(int(cm_)) if cm_ is not None else None

        roll = n("ATTITUDE.roll", when)
        pitch = n("ATTITUDE.pitch", when)
        yaw = n("ATTITUDE.yaw", when)
        deg = lambda r: None if r is None else math.degrees(r)

        lights = n("NVF.Lights1", when)
        gain = n("NVF.PilotGain", when)

        return {
            "depth": depth,
            "altitude": alt,
            "speed": speed,
            "heading": n("VFR_HUD.heading", when),
            "climb": n("VFR_HUD.climb", when),
            "roll": deg(roll),
            "pitch": deg(pitch),
            "yaw": deg(yaw),
            "lights": lights * 100.0 if lights is not None else None,
            "gain": gain * 100.0 if gain is not None else None,
            "cam_tilt": n("NVF.CamTilt", when),
            "temp_c": temp / 100.0 if temp is not None else None,
            "voltage_v": volts,
            "current_a": amps,
            "power_w": watts,
            "throttle": n("VFR_HUD.throttle", when),
            "mode": mode,
        }

    def lights_series(self) -> tuple[np.ndarray, np.ndarray] | None:
        """(t, 0..1) light power, used for the sync check."""
        s = self.series.get("NVF.Lights1")
        if s is None or s.v is None or len(s.t) == 0:
            return None
        return s.t, s.v


# --------------------------------------------------------------------------
#  1 Hz export
# --------------------------------------------------------------------------

#: Columns written verbatim from the telemetry, as (csv name, field, scale).
#: Scale converts MAVLink's integer units to something a biologist can read.
EXPORT_COLUMNS: tuple[tuple[str, str, float], ...] = (
    # power
    ("voltage_V", "BATTERY_STATUS.voltage_mv", 1e-3),
    ("current_A", "BATTERY_STATUS.current_battery", 1e-2),
    ("current_consumed_mAh", "BATTERY_STATUS.current_consumed", 1.0),
    ("sys_load_pct", "SYS_STATUS.load", 1e-1),
    ("Vcc_V", "POWER_STATUS.Vcc", 1e-3),
    # depth / altitude
    ("depth_m", "GLOBAL_POSITION_INT.relative_alt", -1e-3),
    ("altitude_m", "RANGEFINDER.distance", 1.0),
    ("pressure_abs_hPa", "SCALED_PRESSURE2.press_abs", 1.0),
    ("water_temp_C", "SCALED_PRESSURE2.temperature", 1e-2),
    # attitude
    ("heading_deg", "VFR_HUD.heading", 1.0),
    ("roll_deg", "ATTITUDE.roll", math.degrees(1.0)),
    ("pitch_deg", "ATTITUDE.pitch", math.degrees(1.0)),
    ("yaw_deg", "ATTITUDE.yaw", math.degrees(1.0)),
    # velocity
    ("groundspeed_ms", "VFR_HUD.groundspeed", 1.0),
    ("climb_ms", "VFR_HUD.climb", 1.0),
    ("vel_north_ms", "LOCAL_POSITION_NED.vx", 1.0),
    ("vel_east_ms", "LOCAL_POSITION_NED.vy", 1.0),
    ("vel_down_ms", "LOCAL_POSITION_NED.vz", 1.0),
    ("pos_north_m", "LOCAL_POSITION_NED.x", 1.0),
    ("pos_east_m", "LOCAL_POSITION_NED.y", 1.0),
    ("pos_down_m", "LOCAL_POSITION_NED.z", 1.0),
    # GPS / USBL  (zero or absent unless the USBL was running)
    ("gps_lat_deg", "GPS_RAW_INT.lat", 1e-7),
    ("gps_lon_deg", "GPS_RAW_INT.lon", 1e-7),
    ("gps_alt_m", "GPS_RAW_INT.alt", 1e-3),
    ("gps_eph", "GPS_RAW_INT.eph", 1e-2),
    ("gps_satellites", "GPS_RAW_INT.satellites_visible", 1.0),
    ("global_lat_deg", "GLOBAL_POSITION_INT.lat", 1e-7),
    ("global_lon_deg", "GLOBAL_POSITION_INT.lon", 1e-7),
    # EKF health
    ("ekf_velocity_variance", "EKF_STATUS_REPORT.velocity_variance", 1.0),
    ("ekf_pos_horiz_variance", "EKF_STATUS_REPORT.pos_horiz_variance", 1.0),
    ("ekf_pos_vert_variance", "EKF_STATUS_REPORT.pos_vert_variance", 1.0),
    ("ekf_compass_variance", "EKF_STATUS_REPORT.compass_variance", 1.0),
    ("ekf_terrain_alt_variance", "EKF_STATUS_REPORT.terrain_alt_variance", 1.0),
    # DVL
    ("dvl_confidence", "VISION_POSITION_DELTA.confidence", 1.0),
    ("vibration_x", "VIBRATION.vibration_x", 1.0),
    ("vibration_y", "VIBRATION.vibration_y", 1.0),
    ("vibration_z", "VIBRATION.vibration_z", 1.0),
    # pilot inputs
    ("lights_pct", "NVF.Lights1", 100.0),
    ("gain_pct", "NVF.PilotGain", 100.0),
    ("cam_tilt", "NVF.CamTilt", 1.0),
)

#: String-valued columns.
EXPORT_STRINGS: tuple[tuple[str, str], ...] = (
    ("gps_fix_type", "GPS_RAW_INT.fix_type"),
    ("base_mode", "HEARTBEAT.base_mode"),
)


def dvl_beam_columns(store: TelemetryStore) -> list[tuple[str, str]]:
    """DISTANCE_SENSOR ids present in this flight, as (csv name, field).

    The DVL reports each beam under its own sensor id, and how many appear
    depends on the setup, so the columns are discovered rather than fixed.
    """
    out = []
    for name in store.fields():
        if name.startswith("DISTANCE_SENSOR.") and name.endswith(".current_distance"):
            sid = name.split(".")[1]
            out.append((f"dvl_range_{sid}_m", name))
    return sorted(out)
