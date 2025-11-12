#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
tlog_to_csv_vector_only.py
last modified: 2025-11-10

Purpose:
    Convert BlueROV2 .tlog into per-transect CSVs with robust DVL-based tracks.

Key design choices:
    - Direction from LOCAL_POSITION_NED (x=North, y=East) vectors ONLY
      (compass heading not used for propagation to avoid crab/yaw mismatch)
    - Correct compass conversion: 0° = North, CW positive
    - Negative-down depth convention:
        * Prefer VFR_HUD.alt when < -0.5 m
        * Else use -LOCAL_POSITION_NED.z
    - Per-transect seeding at first valid GPS (fallback to EKF)
    - Detect EKF origin resets (large per-second vector jumps) and re-seed
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

def _finite(x): return x is not None and np.isfinite(x)
def _finite_nz(x): return np.isfinite(x) and x != 0

# -----------------------------
# Main
# -----------------------------
def main():
    logfile_in    = input("Enter the path to your .tlog file OR folder containing .tlog files: ").strip()
    site_number   = input("Enter the site number/name: ").strip()
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
    # Store LAST sample for DVLx, DVLy, NEDz, VFR_alt; SUM for rate-like to average; and LAST for lat/lon/EKF
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
                'GPS_valid': False
            }
            counts[key] = 0

        t = msg.get_type()

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
        })

    df_all = pd.DataFrame(rows)

    # ---- Depth (negative-down) ----
    depth_vfr = pd.to_numeric(df_all['VFR_alt'], errors='coerce')
    depth_ned = -pd.to_numeric(df_all['NEDz'], errors='coerce')  # negate positive-down to negative-down
    use_vfr   = (depth_vfr < -0.5) 
    df_all['Depth']        = np.where(use_vfr & np.isfinite(depth_vfr), depth_vfr, depth_ned)
    df_all['Depth_Source'] = np.where(use_vfr & np.isfinite(depth_vfr), 'VFR_alt', 'NEDz')

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

        # Ensure columns exist
        for c in ('DVLlat', 'DVLlon'):
            df_tran[c] = np.nan

        # Here DVLx/DVLy are absolute positions in EKF local frame; we zero to the first row of the transect
        if _finite(df_tran['DVLx'].iloc[0]) and _finite(df_tran['DVLy'].iloc[0]):
            df_tran['DVLx'] = df_tran['DVLx'] - float(df_tran['DVLx'].iloc[0])
            df_tran['DVLy'] = df_tran['DVLy'] - float(df_tran['DVLy'].iloc[0])

        # Seed DVLlat/DVLlon at first valid GPS (fallback to EKF)
        gps_mask = df_tran[['Latitude','Longitude']].apply(lambda r: _finite_nz(r['Latitude']) and _finite_nz(r['Longitude']), axis=1)
        seed_idx = gps_mask.idxmax() if gps_mask.any() else None
        use_ekf  = False
        if seed_idx is None:
            ekf_mask = df_tran[['EKFlat','EKFlon']].apply(lambda r: _finite_nz(r['EKFlat']) and _finite_nz(r['EKFlon']), axis=1)
            seed_idx = ekf_mask.idxmax() if ekf_mask.any() else None
            use_ekf  = seed_idx is not None

        if seed_idx is not None:
            lat0 = df_tran.at[seed_idx, 'Latitude' if not use_ekf else 'EKFlat']
            lon0 = df_tran.at[seed_idx, 'Longitude' if not use_ekf else 'EKFlon']
            df_tran.loc[:seed_idx, ['DVLlat','DVLlon']] = [lat0, lon0]
            df_tran[['DVLlat','DVLlon']] = df_tran[['DVLlat','DVLlon']].ffill()
        else:
            # No seed available in this transect; will remain NaN
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
        angle_ccw_from_north = np.degrees(np.arctan2(dy, dx))           # CCW from North
        bearing_vector = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0         # CW from North

        # EKF reset detection: big per-second jumps
        is_jump = step_dist > JUMP_THRESH

        # Propagate geodesically using vector-only bearing
        for pos in range(1, len(df_tran)):
            # If this index is a jump, re-seed here (start a new sub-track)
            if bool(is_jump.iloc[pos]):
                lat_seed = df_tran['Latitude'].iloc[pos]
                lon_seed = df_tran['Longitude'].iloc[pos]
                if _finite_nz(lat_seed) and _finite_nz(lon_seed):
                    df_tran.at[df_tran.index[pos], 'DVLlat'] = float(lat_seed)
                    df_tran.at[df_tran.index[pos], 'DVLlon'] = float(lon_seed)
                    # Do not move on this step; next iteration will continue from this seed
                    continue
                else:
                    # If no GPS at jump, try EKF lat/lon fields
                    ekf_lat_seed = df_tran['EKFlat'].iloc[pos]
                    ekf_lon_seed = df_tran['EKFlon'].iloc[pos]
                    if _finite_nz(ekf_lat_seed) and _finite_nz(ekf_lon_seed):
                        df_tran.at[df_tran.index[pos], 'DVLlat'] = float(ekf_lat_seed)
                        df_tran.at[df_tran.index[pos], 'DVLlon'] = float(ekf_lon_seed)
                        continue
                    # else: no reseed available; will attempt to step from prior point as usual

            prev_lat = df_tran['DVLlat'].iloc[pos - 1]
            prev_lon = df_tran['DVLlon'].iloc[pos - 1]
            if not (_finite(prev_lat) and _finite(prev_lon)):
                # can't propagate without a valid origin
                continue

            step_m  = float(step_dist.iloc[pos])
            if step_m < MIN_STEP_M:
                # negligible move; keep previous coords
                df_tran.at[df_tran.index[pos], 'DVLlat'] = prev_lat
                df_tran.at[df_tran.index[pos], 'DVLlon'] = prev_lon
                continue

            bearing = float(bearing_vector.iloc[pos])  # compass degrees (0=N, CW)
            new_pos = geodesic(meters=step_m).destination((prev_lat, prev_lon), bearing)
            df_tran.at[df_tran.index[pos], 'DVLlat'] = new_pos.latitude
            df_tran.at[df_tran.index[pos], 'DVLlon'] = new_pos.longitude

        # Per-transect outputs & debug
        df_tran['Distance']        = step_dist.values

        print(f"Transect {i+1}: mean step = {step_dist.mean():.2f} m; jumps > {JUMP_THRESH} m = {int(is_jump.sum())}")
        
        # Console summary only (not saved to CSV)
        mean_step = float(step_dist.mean())
        n_jumps   = int(is_jump.sum())
        print(f"Transect {i+1}: mean step = {mean_step:.2f} m; jumps > {JUMP_THRESH} m = {n_jumps}")

        out_cols = [
            'Date','Time','Latitude','Longitude','EKFlat','EKFlon',
            'DVLx','DVLy','DVLlat','DVLlon',
            'Altitude','Depth','Depth_Source','Heading','Velocity_mps',
            'Width','Area_m2','Distance','NEDz','VFR_alt'
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
