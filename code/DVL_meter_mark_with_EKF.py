import math
from datetime import datetime, timezone
from geopy.distance import geodesic
from pathlib import Path
import pytz
import os
import pandas as pd
from pymavlink import mavutil
import shutil


# --- Utility functions ---
def step_distance(prev_x, prev_y, x, y):
    """Euclidean step distance in meters for LOCAL_POSITION_NED (DVL)."""
    dx = x - prev_x
    dy = y - prev_y
    return math.sqrt(dx * dx + dy * dy)


def parse_jpg_timestamp(filename):
    """Parse jpg filename base 'YYYY_MM_DD_HH-MM-SS' into a datetime, or return None."""
    try:
        base = os.path.splitext(filename)[0]
        return datetime.strptime(base, "%Y_%m_%d_%H-%M-%S")
    except ValueError:
        return None


def move_meter_images(meter_records, jpg_folder, dest_folder, mode="dvl"):
    """
    Move jpgs to dest_folder based on meter_records.

    meter_records:
      - if mode == "dvl": list of dicts where each has "timestamp_dvl" (string)
      - if mode == "ekf": list of dicts where each has "timestamp_ekf" (string)
    jpg_folder: folder containing .jpg files named YYYY_MM_DD_HH-MM-SS.jpg
    dest_folder: destination folder
    mode: "dvl" or "ekf"
    """
    if not os.path.exists(dest_folder):
        os.makedirs(dest_folder)

    # build map of jpg timestamp -> filename
    jpg_times = {}
    for fname in os.listdir(jpg_folder):
        if fname.lower().endswith(".jpg"):
            ts = parse_jpg_timestamp(fname)
            if ts:
                jpg_times[ts] = fname

    if not jpg_times:
        print("No valid jpgs found in source folder.")
        return

    jpg_timestamps = sorted(jpg_times.keys())

    # choose which timestamp key in records to use
    time_key = "timestamp_dvl" if mode == "dvl" else "timestamp_ekf"
    for record in meter_records:
        tstr = record.get(time_key, "")
        if not tstr:
            # skip records that lack the requested timestamp
            continue
        try:
            marker_time = datetime.strptime(tstr, "%Y_%m_%d_%H-%M-%S")
        except ValueError:
            # skip malformed timestamps
            continue

        closest = min(jpg_timestamps, key=lambda t: abs(t - marker_time))
        closest_file = jpg_times[closest]
        src_path = os.path.join(jpg_folder, closest_file)
        dst_path = os.path.join(dest_folder, closest_file)

        if not os.path.exists(dst_path):
            shutil.move(src_path, dst_path)
            print(f"Moved {closest_file} for {mode.upper()} meter {record.get('meter_number_dvl') or record.get('meter_number_ekf')} → {dest_folder}")
        else:
            print(f"Skipped {closest_file}, already in destination.")


# --- Main ---
cwd = Path.cwd()
root = cwd.parent

data = root / "data"
results = root / "results"

logfile = str(data / input("Enter the name of your .tlog file: ").strip())

print(logfile)

try:
    mav = mavutil.mavlink_connection(logfile)
except FileNotFoundError:
    print(f"Error: File '{logfile}' not found.")
    exit(1)

pacific = pytz.timezone("US/Pacific")

# --- Initialize tracking state ---

# DVL (LOCAL_POSITION_NED) tracking (this controls emitted rows)
prev_x_dvl = None
prev_y_dvl = None
cum_dist_dvl = 0.0
prev_dist_dvl = 0.0
next_meter_dvl = 1

# EKF (GLOBAL_POSITION_INT) tracking (independent)
prev_lat_ekf = None
prev_lon_ekf = None
cum_dist_ekf = 0.0

# Keep lists of cumulative EKF distances and timestamps at each completed EKF integer-meter boundary.
# These lists grow as EKF reports cross integer meter boundaries.
ekf_completed_cums = []         # cumulative distances recorded at EKF meter completions
ekf_completed_timestamps = []   # corresponding timestamps (string) at the time they were observed

# Latest EKF metadata (most recent message)
last_ekf_timestamp_str = None
last_ekf_lat = None
last_ekf_lon = None

# Latest GPS_RAW_INT (latest fix)
gps_lat = None
gps_lon = None

# Accumulate final per-DVL-meter records here (each row corresponds to one DVL meter event)
records = []

print("Processing telemetry log...")

# Process the .tlog sequentially
while True:
    msg = mav.recv_match(blocking=False)
    if msg is None:
        break
    if msg.get_type() == "BAD_DATA":
        continue

    # Use the message timestamp if present
    timestamp = getattr(msg, "_timestamp", 0.0)
    if timestamp <= 0:
        # Skip messages without valid timestamps (consistent with earlier scripts)
        continue
    current_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(pacific)

    # --- GPS_RAW_INT -> update latest GPS fix (for DVL rows) ---
    if msg.get_type() == "GPS_RAW_INT":
        gps_lat = msg.lat / 1e7
        gps_lon = msg.lon / 1e7
        continue

    # --- GLOBAL_POSITION_INT -> update EKF cumulative independently (use geodesic for consistency) ---
    if msg.get_type() == "GLOBAL_POSITION_INT":
        new_lat_ekf = msg.lat / 1e7
        new_lon_ekf = msg.lon / 1e7

        # update latest EKF metadata
        last_ekf_timestamp_str = current_time.strftime("%Y_%m_%d_%H-%M-%S")
        last_ekf_lat = new_lat_ekf
        last_ekf_lon = new_lon_ekf

        # if we have a previous EKF point, compute precise geodesic step and add to cumulative
        if prev_lat_ekf is not None and prev_lon_ekf is not None:
            step_m = geodesic((prev_lat_ekf, prev_lon_ekf), (new_lat_ekf, new_lon_ekf)).meters
            cum_dist_ekf += step_m

            # Determine how many whole EKF meters have been completed now (floor of cumulative)
            # previous completed count:
            prev_completed_count = len(ekf_completed_cums)
            current_completed_count = int(math.floor(cum_dist_ekf))

            # For each newly completed integer meter (if any), append the currently observed cumulative value
            # and the current timestamp. This preserves the observed cumulative EKF distance at completion.
            for m in range(prev_completed_count + 1, current_completed_count + 1):
                ekf_completed_cums.append(cum_dist_ekf)
                ekf_completed_timestamps.append(last_ekf_timestamp_str)

        # store for next EKF step
        prev_lat_ekf = new_lat_ekf
        prev_lon_ekf = new_lon_ekf
        continue

    # --- LOCAL_POSITION_NED -> DVL updates and emit rows for each DVL meter crossed ---
    if msg.get_type() == "LOCAL_POSITION_NED":
        x_dvl = msg.x
        y_dvl = msg.y

        # if this is the first DVL point, just store and wait
        if prev_x_dvl is None or prev_y_dvl is None:
            prev_x_dvl = x_dvl
            prev_y_dvl = y_dvl
            continue

        # compute DVL step and update cumulative DVL distance
        step_m = step_distance(prev_x_dvl, prev_y_dvl, x_dvl, y_dvl)
        cum_dist_dvl += step_m

        # While we have crossed integer meter boundaries according to DVL, emit a row for each
        while cum_dist_dvl >= next_meter_dvl:
            # DVL increment: difference between this cumulative and the cumulative at last DVL meter
            incr_dvl = cum_dist_dvl - prev_dist_dvl

            # EKF reporting for this DVL row:
            # - meter_number_ekf is number of EKF meters completed so far
            meter_number_ekf = len(ekf_completed_cums)

            # - timestamp_ekf: most recent GLOBAL_POSITION_INT timestamp seen (may be None)
            timestamp_ekf = ekf_completed_timestamps[-1] if ekf_completed_timestamps else (last_ekf_timestamp_str or "")

            # - cumulative_dist_ekf: current cumulative EKF distance (full precision)
            cumulative_dist_ekf_val = cum_dist_ekf

            # - increment_ekf: difference between the last two completed EKF cumulative distances
            if len(ekf_completed_cums) >= 2:
                increment_ekf_val = ekf_completed_cums[-1] - ekf_completed_cums[-2]
            elif len(ekf_completed_cums) == 1:
                # only one completed EKF meter — difference from zero
                increment_ekf_val = ekf_completed_cums[-1]
            else:
                # no EKF completed meters yet — fallback to current cum_dist_ekf
                increment_ekf_val = cum_dist_ekf

            # Use last known EKF lat/lon values (may be None if not yet seen)
            ekf_lat_value = last_ekf_lat
            ekf_lon_value = last_ekf_lon

            # timestamp for this DVL meter (format used previously)
            ts_dvl_str = current_time.strftime("%Y_%m_%d_%H-%M-%S")

            # Construct record exactly (no rounding)
            record = {
                "meter_number_dvl": next_meter_dvl,
                "timestamp_dvl": ts_dvl_str,
                "cumulative_dist_dvl": cum_dist_dvl,
                "increment_dvl": incr_dvl,
                "x_dvl": x_dvl,
                "y_dvl": y_dvl,
                "gps_lat": gps_lat if gps_lat is not None else "",
                "gps_lon": gps_lon if gps_lon is not None else "",
                "meter_number_ekf": meter_number_ekf,
                "timestamp_ekf": timestamp_ekf if timestamp_ekf is not None else "",
                "cumulative_dist_ekf": cumulative_dist_ekf_val,
                "increment_ekf": increment_ekf_val,
                "lat_ekf": ekf_lat_value if ekf_lat_value is not None else "",
                "lon_ekf": ekf_lon_value if ekf_lon_value is not None else ""
            }

            records.append(record)

            # advance DVL bookkeeping
            prev_dist_dvl = cum_dist_dvl
            next_meter_dvl += 1

        # update previous DVL point for next delta
        prev_x_dvl = x_dvl
        prev_y_dvl = y_dvl
        continue

# --- Finalize output ---

# If no DVL-generated rows, nothing to save
if not records:
    print("No DVL meter marks detected. No CSV written.")
    exit(0)

df = pd.DataFrame(records)

# Ensure columns order exactly as requested
columns = [
    "meter_number_dvl", "timestamp_dvl", "cumulative_dist_dvl", "increment_dvl",
    "x_dvl", "y_dvl", "gps_lat", "gps_lon",
    "meter_number_ekf", "timestamp_ekf", "cumulative_dist_ekf", "increment_ekf",
    "lat_ekf", "lon_ekf"
]
df = df[columns]

results.mkdir(parents=True, exist_ok=True)

# Write CSV
filename = os.path.splitext(os.path.basename(logfile))[0] + "_dvl_ekf_combined.csv"
csv_path = os.path.join(results, filename)
df.to_csv(csv_path, index=False)
print(f"\n✅ Combined DVL–EKF CSV saved to: {csv_path}")

# --- Optional: move jpgs based on DVL or EKF meter marks ---
choice = input("Move jpgs based on which meter marks? Enter 'dvl' or 'ekf' (or 'n' to skip): ").strip().lower()
if choice not in ("dvl", "ekf", "n"):
    print("Invalid choice; skipping image move.")
    choice = "n"

if choice in ("dvl", "ekf"):
    jpg_folder = data / "images"
    dest_folder = results / "meter_marks"

    # Build meter_records_for_move:
    # - If dvl: use the records list (they already include timestamp_dvl and meter_number_dvl)
    # - If ekf: construct a list of dicts from ekf_completed_timestamps & ekf_completed_cums
    if choice == "dvl":
        meter_records_for_move = records
    else:
        # create EKF-style records for moving images (one per completed EKF meter)
        meter_records_for_move = []
        for idx, (cum_val, ts) in enumerate(zip(ekf_completed_cums, ekf_completed_timestamps), start=1):
            meter_records_for_move.append({
                "meter_number_ekf": idx,
                "timestamp_ekf": ts,
                "cumulative_dist_ekf": cum_val,
                "lat_ekf": None,
                "lon_ekf": None
            })

    dest_folder.mkdir(parents=True, exist_ok=True)

    move_meter_images(meter_records_for_move, jpg_folder, dest_folder, mode=choice)

print("Done.")
