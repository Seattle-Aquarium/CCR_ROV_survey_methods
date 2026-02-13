# A script that uses tlogs to identify when the ROV moved by one meter and collect the associated image

import math
from datetime import datetime, timezone
import pytz
import os
import pandas as pd
from pymavlink import mavutil
import shutil

# Function to calculate step distance between consecutive LOCAL_POSITION_NED samples
def step_distance(prev_x, prev_y, x, y):
    dx = x - prev_x
    dy = y - prev_y
    return math.sqrt(dx**2 + dy**2)

# Function to parse jpg filenames into datetime objects
def parse_jpg_timestamp(filename):
    try:
        base = os.path.splitext(filename)[0]
        return datetime.strptime(base, "%Y_%m_%d_%H-%M-%S")
    except ValueError:
        return None

# Function to move jpgs based on meter marker timestamps
def move_images_based_on_markers(meter_records, jpg_folder, dest_folder):
    if not os.path.exists(dest_folder):
        os.makedirs(dest_folder)

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

    for record in meter_records:
        marker_time = datetime.strptime(record["timestamp"], "%Y_%m_%d_%H-%M-%S")
        closest = min(jpg_timestamps, key=lambda t: abs(t - marker_time))
        closest_file = jpg_times[closest]

        src_path = os.path.join(jpg_folder, closest_file)
        dst_path = os.path.join(dest_folder, closest_file)

        if not os.path.exists(dst_path):
            shutil.move(src_path, dst_path)
            print(f"Moved {closest_file} for meter {record['meter_number']} → {dest_folder}")
        else:
            print(f"Skipped {closest_file}, already in destination.")

def process_tlog(logfile):
    try:
        mav = mavutil.mavlink_connection(logfile)
    except FileNotFoundError:
        print(f"Error: File '{logfile}' not found.")
        exit(1)

    positions = []

    while True:
        msg = mav.recv_match(type="LOCAL_POSITION_NED", blocking=False)
        if msg is None:
            break

        timestamp = getattr(msg, "_timestamp", 0.0)
        if timestamp > 0:
            positions.append((timestamp, msg.x, msg.y))

    return positions

def positions_to_meter_records(positions):
    """
    Core algorithm:
    - Uses cumulative horizontal distance
    - Interpolates meter crossings across telemetry gaps
    - Flags low-confidence meters
    """
    pacific = pytz.timezone("US/Pacific")

    prev_x, prev_y = None, None
    prev_time = None                      # === ADDED ===

    cumulative_distance = 0.0
    next_meter = 1
    meter_records = []

    for timestamp, x, y in positions:
        current_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(pacific)

        if prev_x is not None and prev_y is not None:
            step_dist_m = step_distance(prev_x, prev_y, x, y)
            cumulative_distance += step_dist_m

            gap_seconds = timestamp - prev_time if prev_time is not None else 0.0  # === ADDED ===

            # === CHANGED: interpolate meter crossings ===
            while cumulative_distance >= next_meter:
                # Fractional distance between samples
                dist_into_step = next_meter - (cumulative_distance - step_dist_m)
                frac = dist_into_step / step_dist_m if step_dist_m > 0 else 0.0

                interp_timestamp = prev_time + frac * gap_seconds
                interp_time = datetime.fromtimestamp(
                    interp_timestamp, tz=timezone.utc
                ).astimezone(pacific)

                # === ADDED: quality flagging ===
                if gap_seconds > 3:
                    quality_flag = "poor"
                elif gap_seconds > 1:
                    quality_flag = "ok"
                else:
                    quality_flag = "good"

                meter_records.append({
                    "meter_number": next_meter,
                    "timestamp": interp_time.timestamp(),
                    "strftime": interp_time.strftime("%Y_%m_%d_%H-%M-%S"),
                    "cumulative_dist": round(cumulative_distance, 4),
                    "step_distance_m": round(step_dist_m, 4),
                    "gap_seconds": round(gap_seconds, 2),
                    "quality_flag": quality_flag,
                    "x": x,
                    "y": y
                })

                next_meter += 1

        prev_x, prev_y = x, y
        prev_time = timestamp                  # === ADDED ===

    return meter_records

def main():
    logfile = input("Enter the path to your .tlog file: ").strip()
    save_location = input("Enter the path to save the meter marker CSV: ").strip()

    positions = process_tlog(logfile)
    meter_records = positions_to_meter_records(positions)

    df = pd.DataFrame(
        meter_records,
        columns=[
            "meter_number",
            "timestamp",
            "strftime",
            "cumulative_dist",
            "step_distance_m",
            "gap_seconds",
            "quality_flag",
            "x",
            "y"
        ]
    )

    if not df.empty:
        filename = os.path.splitext(os.path.basename(logfile))[0] + "_meter_markers.csv"
        csv_path = os.path.join(save_location, filename)
        df.to_csv(csv_path, index=False)
        print(f"Meter marker timestamps saved to: {csv_path}")
    else:
        print("No meter marks detected.")

    choice = input("Move jpgs based on meter marks? (y/n): ").strip().lower()
    if choice == "y" and meter_records:
        jpg_folder = input("Enter the folder containing jpgs: ").strip()
        dest_folder = input("Enter the destination folder for jpgs: ").strip()
        move_images_based_on_markers(meter_records, jpg_folder, dest_folder)

if __name__ == "__main__":
    main()
