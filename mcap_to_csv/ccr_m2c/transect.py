"""
Slicing the dive into transects and georeferencing the DVL track.

One CSV per transect, named for its Transect ID. A transect may cover several
time windows -- a run that was paused and resumed belongs to one transect, not
two -- and its windows are concatenated in time order.

The DVL track is dead reckoning: per-second North/East steps accumulated from
the transect's first fix. Only the steps are trusted, never the absolute
coordinate, so the track is rebuilt geodesically from a single seed position
rather than being pinned to a noisy per-second GPS.

Column order matches tlog_to_csv.py exactly through ``VFR_alt``, so anything
downstream that reads those CSVs -- the VIAME species join, the percent-cover
merge -- keeps working unchanged. Fields the .tlog era did not have are
appended after that point.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
from geopy.distance import geodesic

from .fsutil import publish

log = logging.getLogger(__name__)

DVL_SCALE = 1.0       # DVLx/DVLy are metres
MIN_STEP_M = 0.02     # ignore jitter below 2 cm
JUMP_THRESH = 5.0     # metres; larger per-second steps are flagged as EKF resets
RESEED_ON_JUMP = False  # False preserves continuous dead reckoning, drift and all

#: Written first, and byte-for-byte the tlog_to_csv.py column list.
TLOG_COLUMNS = [
    "Date", "Time", "Site_name", "Transect_number", "Transect_ID", "Mode_num", "Mode",
    "Battery_V", "Battery_A", "Battery_W",
    "Battery_mAh_used", "Battery_Wh_used",
    "Latitude", "Longitude", "EKFlat", "EKFlon",
    "DVLx", "DVLy", "DVLlat", "DVLlon",
    "Altitude", "Depth", "Depth_std", "Depth_Source",
    "Heading", "Velocity_mps", "Width", "Area_m2",
    "Distance", "NEDz", "VFR_alt",
]

#: Everything the mcap makes available that a .tlog did not carry.
EXTRA_COLUMNS = [
    "Datetime_UTC", "Roll", "Pitch", "Water_temp_C", "Pressure_abs_hPa",
    "DVL_confidence", "DVL_source", "Lights_pct", "Cam_tilt",
    "GPS_fix_type", "GPS_satellites", "Relative_alt_m", "Messages",
]

OUTPUT_COLUMNS = TLOG_COLUMNS + EXTRA_COLUMNS

_FILENAME_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_filename(name: str) -> str:
    return _FILENAME_INVALID_CHARS.sub("_", name).strip() or "transect"


def _finite(x) -> bool:
    try:
        return np.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _finite_nz(x) -> bool:
    try:
        v = float(x)
        return np.isfinite(v) and v != 0
    except (TypeError, ValueError):
        return False


@dataclass
class TransectResult:
    transect_id: str
    transect_number: int
    windows: list[tuple[str, str]]
    path: Path | None = None
    rows: int = 0
    distance_m: float = 0.0
    mean_step_m: float = 0.0
    jumps: int = 0
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def window_desc(self) -> str:
        return ", ".join(f"{s}-{e}" for s, e in self.windows)


def build_transect_mask(df_all: pd.DataFrame, windows: Sequence[tuple[str, str]]) -> pd.Series:
    """Rows whose local time-of-day falls in any of the given windows."""
    times = pd.to_datetime(df_all["Time"], format="%H:%M:%S").dt.time
    mask = pd.Series(False, index=df_all.index)
    for start_str, end_str in windows:
        t_start = datetime.strptime(start_str, "%H:%M:%S").time()
        t_end = datetime.strptime(end_str, "%H:%M:%S").time()
        mask |= (times >= t_start) & (times <= t_end)
    return mask


def dvl_steps(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Per-second North/East steps and their lengths, with jitter suppressed.

    Differences, so it makes no difference whether the local frame has been
    zeroed to a transect or still holds dive-wide coordinates.
    """
    dx = pd.to_numeric(df["DVLx"], errors="coerce").diff().fillna(0.0) * DVL_SCALE
    dy = pd.to_numeric(df["DVLy"], errors="coerce").diff().fillna(0.0) * DVL_SCALE
    step = np.sqrt(dx ** 2 + dy ** 2)

    # A stationary ROV must not accumulate distance out of sensor noise.
    dx = dx.where(step >= MIN_STEP_M, 0.0)
    dy = dy.where(step >= MIN_STEP_M, 0.0)
    return dx, dy, np.sqrt(dx ** 2 + dy ** 2)


def georeference_dvl(df_tran: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, str | None]:
    """Turn relative DVL North/East metres into a geodesic lat/lon track.

    Returns the frame with ``DVLlat``/``DVLlon`` filled, the per-step distance,
    and a warning if there was no fix to seed from.
    """
    df_tran = df_tran.copy()
    for c in ("DVLlat", "DVLlon"):
        df_tran[c] = np.nan

    # Zero the local frame to the start of the transect, so DVLx/DVLy read as
    # displacement along this transect rather than from wherever the dive began.
    if _finite(df_tran["DVLx"].iloc[0]) and _finite(df_tran["DVLy"].iloc[0]):
        df_tran["DVLx"] = df_tran["DVLx"] - float(df_tran["DVLx"].iloc[0])
        df_tran["DVLy"] = df_tran["DVLy"] - float(df_tran["DVLy"].iloc[0])

    # Seed at the first valid surface fix, falling back to the EKF.
    seed_idx, use_ekf = None, False
    gps_mask = df_tran["Latitude"].map(_finite_nz) & df_tran["Longitude"].map(_finite_nz)
    if gps_mask.any():
        seed_idx = gps_mask.idxmax()
    else:
        ekf_mask = df_tran["EKFlat"].map(_finite_nz) & df_tran["EKFlon"].map(_finite_nz)
        if ekf_mask.any():
            seed_idx, use_ekf = ekf_mask.idxmax(), True

    seed_warning = None
    if seed_idx is not None:
        lat0 = df_tran.at[seed_idx, "EKFlat" if use_ekf else "Latitude"]
        lon0 = df_tran.at[seed_idx, "EKFlon" if use_ekf else "Longitude"]
        df_tran.loc[:seed_idx, ["DVLlat", "DVLlon"]] = [lat0, lon0]
        df_tran[["DVLlat", "DVLlon"]] = df_tran[["DVLlat", "DVLlon"]].ffill()
    else:
        seed_warning = "no GPS or EKF fix to seed lat/lon"

    dx, dy, step_dist = dvl_steps(df_tran)

    # Bearing from the motion vector itself (0 = North, clockwise), not from the
    # compass -- that is what keeps the track free of yaw drift.
    bearing_vector = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0
    is_jump = step_dist > JUMP_THRESH

    lat_col = df_tran["DVLlat"].to_numpy(dtype=float, copy=True)
    lon_col = df_tran["DVLlon"].to_numpy(dtype=float, copy=True)
    steps = step_dist.to_numpy(dtype=float)
    bearings = bearing_vector.to_numpy(dtype=float)
    gps_lat = pd.to_numeric(df_tran["Latitude"], errors="coerce").to_numpy(dtype=float)
    gps_lon = pd.to_numeric(df_tran["Longitude"], errors="coerce").to_numpy(dtype=float)
    ekf_lat = pd.to_numeric(df_tran["EKFlat"], errors="coerce").to_numpy(dtype=float)
    ekf_lon = pd.to_numeric(df_tran["EKFlon"], errors="coerce").to_numpy(dtype=float)
    jumps = is_jump.to_numpy(dtype=bool)

    for pos in range(1, len(df_tran)):
        if RESEED_ON_JUMP and jumps[pos]:
            if _finite_nz(gps_lat[pos]) and _finite_nz(gps_lon[pos]):
                lat_col[pos], lon_col[pos] = gps_lat[pos], gps_lon[pos]
                continue
            if _finite_nz(ekf_lat[pos]) and _finite_nz(ekf_lon[pos]):
                lat_col[pos], lon_col[pos] = ekf_lat[pos], ekf_lon[pos]
                continue

        prev_lat, prev_lon = lat_col[pos - 1], lon_col[pos - 1]
        if not (np.isfinite(prev_lat) and np.isfinite(prev_lon)):
            continue
        if steps[pos] < MIN_STEP_M:
            lat_col[pos], lon_col[pos] = prev_lat, prev_lon
            continue
        moved = geodesic(meters=float(steps[pos])).destination(
            (prev_lat, prev_lon), float(bearings[pos]))
        lat_col[pos], lon_col[pos] = moved.latitude, moved.longitude

    df_tran["DVLlat"] = lat_col
    df_tran["DVLlon"] = lon_col
    return df_tran, step_dist, seed_warning


def export_transect(
    df_all: pd.DataFrame,
    windows: Sequence[tuple[str, str]],
    transect_num: int,
    transect_id: str,
    site_name: str,
    transects_folder: Path | str,
    *,
    dvl_source: str = "",
    site_frame: bool = False,
) -> TransectResult:
    """Filter to the transect's window(s), build the track, write one CSV.

    ``site_frame`` says the caller has already propagated ``DVLlat``/``DVLlon``
    across the whole dive, so this transect keeps its true position relative to
    the others rather than being re-seeded at the dive's one surface fix. See
    ``pipeline.run`` for why that matters.
    """
    result = TransectResult(transect_id=transect_id, transect_number=transect_num,
                            windows=list(windows))

    df_tran = df_all.loc[build_transect_mask(df_all, windows)].copy()
    if df_tran.empty:
        result.message = (f"Transect {transect_num} ({transect_id}): "
                          f"no rows in window(s) {result.window_desc}")
        return result
    df_tran = df_tran.reset_index(drop=True)

    # Battery use since the start of this transect, not since power-on.
    for total, used in (("Battery_mAh_total", "Battery_mAh_used"),
                        ("Battery_Wh_total", "Battery_Wh_used")):
        if total in df_tran.columns and df_tran[total].notna().any():
            first = df_tran[total].dropna().iloc[0]
            df_tran[used] = df_tran[total] - first
        else:
            df_tran[used] = np.nan

    have_site_track = (
        site_frame
        and {"DVLlat", "DVLlon"}.issubset(df_tran.columns)
        and df_tran["DVLlat"].notna().any()
    )
    if have_site_track:
        # Positions are already right; only the local frame is re-zeroed, so
        # DVLx/DVLy still read as displacement along this transect.
        _, _, step_dist = dvl_steps(df_tran)
        if _finite(df_tran["DVLx"].iloc[0]) and _finite(df_tran["DVLy"].iloc[0]):
            df_tran["DVLx"] = df_tran["DVLx"] - float(df_tran["DVLx"].iloc[0])
            df_tran["DVLy"] = df_tran["DVLy"] - float(df_tran["DVLy"].iloc[0])
        seed_warning = None
    else:
        df_tran, step_dist, seed_warning = georeference_dvl(df_tran)
    if seed_warning:
        result.warnings.append(seed_warning)

    df_tran["Distance"] = step_dist.to_numpy()
    df_tran["Site_name"] = site_name
    df_tran["Transect_number"] = transect_num
    df_tran["Transect_ID"] = transect_id
    df_tran["DVL_source"] = dvl_source

    for c in OUTPUT_COLUMNS:
        if c not in df_tran.columns:
            df_tran[c] = np.nan
    df_tran = df_tran[OUTPUT_COLUMNS]

    folder = Path(transects_folder)
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / f"{sanitize_filename(transect_id)}.csv"
    tmp = out_path.with_name(out_path.name + ".part")
    df_tran.to_csv(tmp, index=False)
    out_path = publish(tmp, out_path, log=lambda m: result.warnings.append(m))

    result.path = out_path
    result.rows = len(df_tran)
    result.distance_m = float(np.nansum(step_dist.to_numpy()))
    result.mean_step_m = float(np.nanmean(step_dist.to_numpy())) if len(step_dist) else 0.0
    result.jumps = int((step_dist > JUMP_THRESH).sum())
    result.message = (
        f"Transect {transect_num} ({transect_id}, {result.window_desc}): "
        f"{result.rows} rows, {result.distance_m:.1f} m travelled, "
        f"mean step {result.mean_step_m:.2f} m, jumps > {JUMP_THRESH:.0f} m: {result.jumps}"
    )
    if seed_warning:
        result.message += f"  [WARNING: {seed_warning}]"
    return result


def whole_log_window(df_all: pd.DataFrame) -> tuple[str, str]:
    """The full local-time span of the log, for a 'process everything' run."""
    return str(df_all["Time"].iloc[0]), str(df_all["Time"].iloc[-1])
