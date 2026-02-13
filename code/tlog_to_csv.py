#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
tlog_to_csv.py
last modified: 2026-02-13

Purpose
-------
Converts BlueROV2 `.tlog` files into per-transect CSVs that include time, position,
depth, altitude, area, and speed. The script produces DVL-based tracks that are
georeferenced, smoothed, and averaged at one-second intervals for analysis or mapping.

Localization sources
--------------------
• EKFlat/EKFlon (fused position) – The most accurate source, combining GPS, DVL, and IMU
  data to produce a stable, drift-corrected position (`GLOBAL_POSITION_INT`). 

• GPS Latitude/Longitude (surface position) – GPS positioning from Water Linked GPS G2 system.
  Results in noisy tracks.

• DVLlat/DVLlon (LOCAL_POSITION_NED) – Provides precise local movement vectors (x = North,
  y = East) GPS-anchored. Tracks are built using these vectors rather than compass heading to 
  avoid yaw drift. DVL movement is converted into geographic coordinates (`DVLlat`, `DVLlon`)
  using geodesic steps.

Depth handling
--------------
Depth follows a negative-down convention:
• Prefer *VFR_HUD.alt* when it’s below –0.5 m (already negative-down).
• Otherwise, use *–LOCAL_POSITION_NED.z*.
The chosen source is noted in the `Depth_Source` column.

Field of view and area
----------------------
Altitude (from the rangefinder) is used to estimate:
• Width (m) – Scales linearly with altitude.  
• Area (m²) – Scales with the square of altitude.  
These values represent the approximate camera footprint on the seafloor.

Averaging
---------
Each second of data is summarized to reduce noise:
• Averaged values: altitude, heading, width, area, and velocity.  
• Last recorded values: DVLx, DVLy, latitude, longitude, depth.  
• Derived values include `Distance` (per-step motion) and `DVLlat/DVLlon`
  (georeferenced DVL track seeded by GPS).

Outputs
-------
Generates one CSV per transect containing:
Date, Time, Latitude, Longitude, EKFlat, EKFlon, DVLx, DVLy, DVLlat, DVLlon,
Altitude, Depth, Depth_Source, Heading, Velocity_mps, Width, Area_m2, Distance.
"""


import os
import glob
import math
import pytz
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pymavlink import mavutil
from geopy.distance import geodesic
import requests # requires internet access for API interface
from scipy.interpolate import CubicSpline

# -----------------------------
# Constants & settings
# -----------------------------
REFERENCE_WIDTH_M = 1.15
REFERENCE_ALT_M   = 0.66
REFERENCE_AREA_M2 = 0.9545
PACIFIC_TZ        = pytz.timezone('US/Pacific')

DVL_SCALE    = 1.0   # if your DVLx/DVLy are meters, keep 1.0; use 0.002 if your old data expects it
MIN_STEP_M   = 0.02  # ignore tiny jitter steps (< 2 cm)
JUMP_THRESH  = 5.0   # meters; treat larger per-second jumps as EKF resets (tune as needed)
RESEED_ON_JUMP = False  # set to False to preserve continuous DVL dead-reckoning (show DVL drift)

# Which system/component to trust for autopilot values (mode + SYS_STATUS power)
AUTOPILOT_SYSID  = 1
AUTOPILOT_COMPID = 1

# -----------------------------
# ArduSub mode mapping
# -----------------------------
ARDUSUB_MODE_MAP = {
    0:  "STABILIZE",
    1:  "ACRO",
    2:  "ALT_HOLD",
    3:  "AUTO",
    4:  "GUIDED",
    7:  "CIRCLE",
    9:  "SURFACE",
    16: "POSHOLD",
    19: "MANUAL",
    20: "MOTOR_DETECT",
    21: "SURFTRAK",
}

# -----------------------------
# Helpers
# -----------------------------
def calculate_width(alt_m):
    return REFERENCE_WIDTH_M * (alt_m / REFERENCE_ALT_M) if (alt_m is not None and alt_m > 0) else 0.0

def calculate_area(alt_m):
    return REFERENCE_AREA_M2 * (alt_m / REFERENCE_ALT_M) ** 2 if (alt_m is not None and alt_m > 0) else 0.0

def pick_tlog_path(user_input):
    p = os.path.expanduser(user_input)
    if os.path.isdir(p):
        matches = sorted(glob.glob(os.path.join(p, "*.tlog")))
        if not matches:
            raise FileNotFoundError(f"No .tlog files found in folder: {p}")
        return matches[0]
    if not os.path.isfile(p):
        raise FileNotFoundError(f"File not found: {p}")
    return p

def _finite(x):
    try:
        return np.isfinite(float(x))
    except Exception:
        return False

def _finite_nz(x):
    try:
        return np.isfinite(float(x)) and float(x) != 0
    except Exception as e:
        print("Bad EKF value:", repr(x), "error:", e)
        return False

# -----------------------------
# Main
# -----------------------------
def main():
    logfile_in    = input("Enter the path to your .tlog file OR folder containing .tlog files: ").strip()
    site_number   = input("Enter the site number/name: ").strip()

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    survey_date = input("Enter survey date (YYYYMMDD): ").strip()
    print("Which station?")
    print(" - (1) Elliott Bay (9447130)")
    print(" - (2) Friday Harbor (9449880)")
    print(" - (3) Neah Bay (9443090)")
    station_choice = input("Enter station number: ")

    if station_choice == "1":
        station_id = "9447130"
    elif station_choice == "2":
        station_id = "9449880"
    elif station_choice == "3":
        station_id = "9443090"

    noaa_tide_api_request = f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date={survey_date}&end_date={survey_date}&station={station_id}&product=one_minute_water_level&datum=MLLW&time_zone=lst&units=metric&format=json"
    
    def get_tide_data(api_request):
        response = requests.get(api_request)
        if response.status_code == 200:
            tide_data = response.json()
            return tide_data
        else:
            print(f"Error: {response.status_code}")
            return None

    tide_chart = get_tide_data(noaa_tide_api_request)

    # ----------------------------
    # Convert NOAA JSON to DataFrame
    # ----------------------------

    noaa_wl = pd.DataFrame(tide_chart["data"])

    noaa_wl.rename(columns={
        "t": "datetime",
        "v": "water_level"
    }, inplace=True)

    noaa_wl["datetime"] = pd.to_datetime(noaa_wl["datetime"])
    noaa_wl["water_level"] = pd.to_numeric(noaa_wl["water_level"], errors="coerce")

    noaa_wl["date"] = noaa_wl["datetime"].dt.date.astype(str)
    noaa_wl["time"] = noaa_wl["datetime"].dt.strftime("%H:%M")

    # ----------------------------
    # NOAA spline interpolation
    # ----------------------------
    def expand_noaa_to_seconds(df):

        times = pd.to_datetime(df["date"] + " " + df["time"])
        values = df["water_level"].values

        t0 = times.iloc[0]
        t_seconds = (times - t0).dt.total_seconds().values

        spline = CubicSpline(t_seconds, values, bc_type="natural")

        full_seconds = np.arange(
            int(t_seconds[0]),
            int(t_seconds[-1]) + 1
        )

        full_times = t0 + pd.to_timedelta(full_seconds, unit="s")
        full_values = spline(full_seconds)

        return pd.DataFrame({
            "Datetime": full_times,
            "water_level": full_values
        })

    noaa_seconds_df = expand_noaa_to_seconds(noaa_wl)

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    save_location = input("Enter the path to save the transects folder: ").strip()

    logfile = pick_tlog_path(logfile_in)
    transects_folder = os.path.join(save_location, "transects")
    os.makedirs(transects_folder, exist_ok=True)
    print(f"Saving outputs to: {transects_folder}")

    # Get transect windows
    transects = []
    for i in range(1, 7):
        s = input(f"Enter start time for transect {i} (HH:MM:SS) or leave blank: ").strip()
        e = input(f"Enter end time for transect {i} (HH:MM:SS) or leave blank: ").strip()
        if s and e:
            transects.append((s, e))
        else:
            break
    if not transects:
        transects = [("00:00:00", "23:59:59")]
        print("No transects entered — processing the entire file.")

    print("Opening tlog...")
    mav = mavutil.mavlink_connection(logfile)

    # Per-second buckets
    buckets = {}
    counts  = {}
    latest_time = None
    file_date_str = None

    # Running "current" message values
    lat = lon = EKFlat = EKFlon = None
    dvlx = dvly = None
    altitude = None
    heading_deg = None   # not used for propagation, but kept in output
    velocity = None
    vfr_alt = None
    ned_z_val = None

    # Mode (HEARTBEAT from autopilot component)
    current_mode = None

    # SYS_STATUS for instantaneous power (from autopilot sys/comp)
    sys_v_mV = None  # SYS_STATUS.voltage_battery (mV)
    sys_i_cA = None  # SYS_STATUS.current_battery (cA = 0.01A)

    # BATTERY_STATUS for cumulative totals
    batt_mah    = None   # BATTERY_STATUS.current_consumed (mAh)
    batt_e_100J = None   # BATTERY_STATUS.energy_consumed (100 Joules units)

    print("Processing tlog...")
    while True:
        msg = mav.recv_match(blocking=False)
        if msg is None:
            break
        if msg.get_type() == "BAD_DATA":
            continue

        ts = getattr(msg, "_timestamp", 0.0)
        if ts > 0:
            latest_time = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PACIFIC_TZ)
            file_date_str = latest_time.strftime("%Y_%m_%d")
        if latest_time is None:
            continue

        key = latest_time.replace(microsecond=0)
        if key not in buckets:
            buckets[key] = {
                'Date': latest_time.strftime("%Y-%m-%d"),
                'Time': latest_time.strftime("%H:%M:%S"),
                'Latitude': None, 'Longitude': None,
                'EKFlat': None, 'EKFlon': None,

                'DVLx': None, 'DVLy': None,           # LAST in second
                'Altitude_sum': 0.0,                  # SUM for average
                'Heading_sum': 0.0,                   # SUM for average (kept for QA)
                'Width_sum': 0.0,
                'Area_sum': 0.0,
                'Velocity_sum': 0.0,

                'NEDz': None,                         # LAST in second
                'VFR_alt': None,                      # LAST in second
                'GPS_valid': False,

                'Mode_num': None,
                'Mode': None,

                # Battery instantaneous (from SYS_STATUS)
                'Battery_V_sum': 0.0,
                'Battery_A_sum': 0.0,
                'Battery_W_sum': 0.0,

                # Battery cumulative (from BATTERY_STATUS)
                'Battery_mAh_total': None,
                'Battery_Wh_total': None,
            }
            counts[key] = 0

        t = msg.get_type()

        # Source IDs (safe)
        try:
            src_sysid  = msg.get_srcSystem()
            src_compid = msg.get_srcComponent()
        except Exception:
            src_sysid = src_compid = None

        if t == "GPS_RAW_INT":
            lat = msg.lat / 1e7
            lon = msg.lon / 1e7

        elif t == "GLOBAL_POSITION_INT":
            EKFlat = msg.lat / 1e7
            EKFlon = msg.lon / 1e7

        elif t == "ATTITUDE":
            yaw = getattr(msg, "yaw", None)
            if yaw is not None and np.isfinite(yaw):
                heading_deg = (math.degrees(float(yaw)) + 360.0) % 360.0

        elif t == "VFR_HUD":
            vfr_alt  = getattr(msg, "alt", None)           # typically negative-down
            velocity = getattr(msg, "groundspeed", None)

        elif t == "LOCAL_POSITION_NED":
            dvlx = getattr(msg, "x", dvlx)                 # meters (N)
            dvly = getattr(msg, "y", dvly)                 # meters (E)
            ned_z_val = getattr(msg, "z", None)            # positive down

        elif t == "RANGEFINDER":
            altitude = getattr(msg, "distance", altitude)  # meters AGL

        elif t == "HEARTBEAT":
            # Only trust autopilot heartbeat component (and sysid if you want)
            if src_compid == AUTOPILOT_COMPID and (src_sysid == AUTOPILOT_SYSID):
                current_mode = getattr(msg, "custom_mode", None)

        elif t == "SYS_STATUS":
            # Instantaneous voltage/current from autopilot sys/comp only
            if src_sysid == AUTOPILOT_SYSID and src_compid == AUTOPILOT_COMPID:
                sys_v_mV = getattr(msg, "voltage_battery", None)  # mV
                sys_i_cA = getattr(msg, "current_battery", None)  # cA (0.01A)

        elif t == "BATTERY_STATUS":
            # Cumulative totals (often best here if populated)
            batt_mah    = getattr(msg, "current_consumed", None)
            batt_e_100J = getattr(msg, "energy_consumed", None)

        # Update per-second bucket
        b = buckets[key]

        # Keep LAST valid fixes for position/ekf
        if _finite(lat) and _finite(lon) and lat != 0 and lon != 0:
            b['Latitude'] = float(lat)
            b['Longitude'] = float(lon)
            b['GPS_valid'] = True

        if _finite(EKFlat) and _finite(EKFlon) and EKFlat != 0 and EKFlon != 0:
            b['EKFlat'] = float(EKFlat)
            b['EKFlon'] = float(EKFlon)

        # DVL position: keep LAST in the second
        if _finite(dvlx): b['DVLx'] = float(dvlx)
        if _finite(dvly): b['DVLy'] = float(dvly)

        # Keep LAST NEDz/VFR_alt
        if _finite(ned_z_val): b['NEDz'] = float(ned_z_val)
        if _finite(vfr_alt):   b['VFR_alt'] = float(vfr_alt)

        # SUM rate-like / derived values for averaging later
        if _finite(altitude):
            b['Altitude_sum'] += float(altitude)
            b['Width_sum']    += calculate_width(altitude)
            b['Area_sum']     += calculate_area(altitude)
        if _finite(heading_deg):
            b['Heading_sum']  += float(heading_deg)
        if _finite(velocity):
            b['Velocity_sum'] += float(velocity)

        # ---- Battery instantaneous aggregation (from SYS_STATUS) ----
        if _finite(sys_v_mV) and float(sys_v_mV) > 0:
            V = float(sys_v_mV) / 1000.0
            b['Battery_V_sum'] += V
            if _finite(sys_i_cA):
                A = float(sys_i_cA) / 100.0
                b['Battery_A_sum'] += A
                b['Battery_W_sum'] += (V * A)

        # ---- Battery cumulative totals (from BATTERY_STATUS) ----
        if _finite(batt_mah) and float(batt_mah) >= 0:
            b['Battery_mAh_total'] = float(batt_mah)

        if _finite(batt_e_100J) and float(batt_e_100J) >= 0:
            # 1 unit = 100 Joules; Wh = J / 3600
            b['Battery_Wh_total'] = (float(batt_e_100J) * 100.0) / 3600.0

        # Keep LAST autopilot mode in this second
        if _finite(current_mode):
            mode_num = int(current_mode)
            b['Mode_num'] = mode_num
            b['Mode'] = ARDUSUB_MODE_MAP.get(mode_num, "UNKNOWN")

        counts[key] += 1

    if not buckets:
        print("No data parsed from tlog.")
        return

    # Build DataFrame from seconds (compute averages where needed)
    rows = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        n = max(counts[key], 1)
        rows.append({
            'Date': b['Date'],
            'Time': b['Time'],
            'Latitude': b['Latitude'],
            'Longitude': b['Longitude'],
            'EKFlat': b['EKFlat'],
            'EKFlon': b['EKFlon'],
            'DVLx': b['DVLx'],
            'DVLy': b['DVLy'],
            'Altitude': (b['Altitude_sum'] / n) if n else np.nan,
            'Heading': (b['Heading_sum']  / n) if n else np.nan,
            'Width':   (b['Width_sum']    / n) if n else np.nan,
            'Area_m2': (b['Area_sum']     / n) if n else np.nan,
            'Velocity_mps': (b['Velocity_sum'] / n) if n else np.nan,
            'NEDz': b['NEDz'],
            'VFR_alt': b['VFR_alt'],
            'GPS_valid': b['GPS_valid'],
            'Mode_num': b['Mode_num'],
            'Mode': b['Mode'],

            # Battery instantaneous (SYS_STATUS)
            'Battery_V': (b['Battery_V_sum'] / n) if n else np.nan,
            'Battery_A': (b['Battery_A_sum'] / n) if n else np.nan,
            'Battery_W': (b['Battery_W_sum'] / n) if n else np.nan,

            # Battery cumulative (BATTERY_STATUS)
            'Battery_mAh_total': b['Battery_mAh_total'],
            'Battery_Wh_total':  b['Battery_Wh_total'],
        })

    df_all = pd.DataFrame(rows)

    # Battery cumulative counters can arrive intermittently; carry forward
    for c in ['Battery_mAh_total', 'Battery_Wh_total']:
        if c in df_all.columns:
            df_all[c] = df_all[c].ffill()

    # Fill mode forward so every second inherits last known autopilot mode
    if 'Mode_num' in df_all.columns:
        df_all['Mode_num'] = df_all['Mode_num'].ffill()
    if 'Mode' in df_all.columns:
        df_all['Mode'] = df_all['Mode'].ffill().fillna("UNKNOWN")

    # ---- Depth (negative-down) ----
    depth_vfr = pd.to_numeric(df_all['VFR_alt'], errors='coerce')
    depth_ned = -pd.to_numeric(df_all['NEDz'], errors='coerce')  # negate positive-down to negative-down
    use_vfr   = (depth_vfr < -0.5)
    df_all['Depth']        = np.where(use_vfr & np.isfinite(depth_vfr), depth_vfr, depth_ned)
    df_all['Depth_Source'] = np.where(use_vfr & np.isfinite(depth_vfr), 'VFR_alt', 'NEDz')

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    df_all["Datetime"] = pd.to_datetime(df_all["Date"] + " " + df_all["Time"])
    df_all = pd.merge_asof(
        df_all.sort_values("Datetime"),
        noaa_seconds_df.sort_values("Datetime"),
        on="Datetime",
        direction="backward",
        tolerance=pd.Timedelta("1min")
    ).sort_values("Datetime")
    df_all["Depth_std"] = (
        -df_all["Altitude"] +
        df_all["Depth"] +
        df_all["water_level"]
    )
    df_all.drop(columns=["Datetime","water_level"], inplace=True)
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # ---- Per-transect processing & export ----
    file_date_str = file_date_str or datetime.now(PACIFIC_TZ).strftime("%Y_%m_%d")

    for i, (start_time_str, end_time_str) in enumerate(transects):
        # Filter to transect time window
        try:
            t_start = datetime.strptime(start_time_str, "%H:%M:%S").time()
            t_end   = datetime.strptime(end_time_str,   "%H:%M:%S").time()
        except ValueError:
            print(f"Transect {i+1}: invalid time format, skipping.")
            continue

        mask = df_all['Time'].apply(lambda t: t_start <= datetime.strptime(t, "%H:%M:%S").time() <= t_end)
        df_tran = df_all.loc[mask].copy()

        if df_tran.empty:
            print(f"Transect {i+1}: no rows in window {start_time_str}–{end_time_str}")
            continue

        # ---- Transect battery deltas (used since start of transect) ----
        if 'Battery_mAh_total' in df_tran.columns:
            first_mah = df_tran['Battery_mAh_total'].dropna().iloc[0] if df_tran['Battery_mAh_total'].notna().any() else np.nan
            df_tran['Battery_mAh_used'] = df_tran['Battery_mAh_total'] - first_mah

        if 'Battery_Wh_total' in df_tran.columns:
            first_wh = df_tran['Battery_Wh_total'].dropna().iloc[0] if df_tran['Battery_Wh_total'].notna().any() else np.nan
            df_tran['Battery_Wh_used'] = df_tran['Battery_Wh_total'] - first_wh

        # Ensure columns exist
        for c in ('DVLlat', 'DVLlon'):
            df_tran[c] = np.nan

        # Zero DVL x/y to start of transect (keep as relative)
        if _finite(df_tran['DVLx'].iloc[0]) and _finite(df_tran['DVLy'].iloc[0]):
            df_tran['DVLx'] = df_tran['DVLx'] - float(df_tran['DVLx'].iloc[0])
            df_tran['DVLy'] = df_tran['DVLy'] - float(df_tran['DVLy'].iloc[0])

        # Seed DVLlat/DVLlon at first valid GPS (fallback to EKF)
        gps_mask = df_tran[['Latitude','Longitude']].apply(
            lambda r: _finite_nz(r['Latitude']) and _finite_nz(r['Longitude']), axis=1
        )
        seed_idx = gps_mask.idxmax() if gps_mask.any() else None
        use_ekf  = False
        if seed_idx is None:
            ekf_mask = df_tran[['EKFlat','EKFlon']].apply(
                lambda r: _finite_nz(r['EKFlat']) and _finite_nz(r['EKFlon']), axis=1
            )
            seed_idx = ekf_mask.idxmax() if ekf_mask.any() else None
            use_ekf  = seed_idx is not None

        if seed_idx is not None:
            lat0 = df_tran.at[seed_idx, 'Latitude' if not use_ekf else 'EKFlat']
            lon0 = df_tran.at[seed_idx, 'Longitude' if not use_ekf else 'EKFlon']
            df_tran.loc[:seed_idx, ['DVLlat','DVLlon']] = [lat0, lon0]
            df_tran[['DVLlat','DVLlon']] = df_tran[['DVLlat','DVLlon']].ffill()
        else:
            print(f"Transect {i+1}: no GPS or EKF fix to seed lat/lon.")

        # Compute per-step deltas (N/E) and distances
        dx = df_tran['DVLx'].diff().fillna(0.0) * DVL_SCALE   # North step (m)
        dy = df_tran['DVLy'].diff().fillna(0.0) * DVL_SCALE   # East  step (m)
        step_dist = np.sqrt(dx**2 + dy**2)

        # Zero-motion guard (suppress jitter)
        dx = dx.where(step_dist >= MIN_STEP_M, 0.0)
        dy = dy.where(step_dist >= MIN_STEP_M, 0.0)
        step_dist = np.sqrt(dx**2 + dy**2)

        # Vector-only compass bearing (from North, CW)
        bearing_vector = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0

        # EKF reset detection: big per-second jumps
        is_jump = step_dist > JUMP_THRESH

        # Propagate geodesically using vector-only bearing
        for pos in range(1, len(df_tran)):
            if RESEED_ON_JUMP and bool(is_jump.iloc[pos]):
                lat_seed = df_tran['Latitude'].iloc[pos]
                lon_seed = df_tran['Longitude'].iloc[pos]
                if _finite_nz(lat_seed) and _finite_nz(lon_seed):
                    df_tran.at[df_tran.index[pos], 'DVLlat'] = float(lat_seed)
                    df_tran.at[df_tran.index[pos], 'DVLlon'] = float(lon_seed)
                    continue
                else:
                    ekf_lat_seed = df_tran['EKFlat'].iloc[pos]
                    ekf_lon_seed = df_tran['EKFlon'].iloc[pos]
                    if _finite_nz(ekf_lat_seed) and _finite_nz(ekf_lon_seed):
                        df_tran.at[df_tran.index[pos], 'DVLlat'] = float(ekf_lat_seed)
                        df_tran.at[df_tran.index[pos], 'DVLlon'] = float(ekf_lon_seed)
                        continue

            prev_lat = df_tran['DVLlat'].iloc[pos - 1]
            prev_lon = df_tran['DVLlon'].iloc[pos - 1]
            if not (_finite(prev_lat) and _finite(prev_lon)):
                continue

            step_m = float(step_dist.iloc[pos])
            if step_m < MIN_STEP_M:
                df_tran.at[df_tran.index[pos], 'DVLlat'] = prev_lat
                df_tran.at[df_tran.index[pos], 'DVLlon'] = prev_lon
                continue

            bearing = float(bearing_vector.iloc[pos])  # 0=N, CW
            new_pos = geodesic(meters=step_m).destination((prev_lat, prev_lon), bearing)
            df_tran.at[df_tran.index[pos], 'DVLlat'] = new_pos.latitude
            df_tran.at[df_tran.index[pos], 'DVLlon'] = new_pos.longitude

        # Per-transect outputs & debug
        df_tran['Distance'] = step_dist.values

        mean_step = float(step_dist.mean()) if len(step_dist) else 0.0
        n_jumps   = int(is_jump.sum()) if len(step_dist) else 0
        print(f"Transect {i+1}: mean step = {mean_step:.2f} m; jumps > {JUMP_THRESH} m = {n_jumps}")

        out_cols = [
            'Date','Time','Mode_num','Mode',
            'Battery_V','Battery_A','Battery_W',
            'Battery_mAh_used','Battery_Wh_used',
            'Latitude','Longitude','EKFlat','EKFlon',
            'DVLx','DVLy','DVLlat','DVLlon',
            'Altitude','Depth','Depth_std','Depth_Source',
            'Heading','Velocity_mps','Width','Area_m2',
            'Distance','NEDz','VFR_alt'
        ]

        for c in out_cols:
            if c not in df_tran.columns:
                df_tran[c] = np.nan
        df_tran = df_tran[out_cols]

        csv_filename = f"{file_date_str}_{site_number}_T{i+1}.csv"
        csv_full_path = os.path.join(transects_folder, csv_filename)
        df_tran.to_csv(csv_full_path, index=False)
        print(f"Transect {i+1} saved → {csv_full_path}")

    print("Done.")

if __name__ == "__main__":
    main()
