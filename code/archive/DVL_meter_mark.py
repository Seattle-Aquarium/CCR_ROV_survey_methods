# A script that uses tlogs to identify when the ROV moved by one meter and collect the associated images

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

    # Collect jpgs with timestamps
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

    # Match each meter mark to nearest jpg timestamp
    for record in meter_records:
        marker_time = datetime.strptime(record["timestamp"], "%Y_%m_%d_%H-%M-%S")

        # Find closest timestamp
        closest = min(jpg_timestamps, key=lambda t: abs(t - marker_time))
        closest_file = jpg_times[closest]

        src_path = str(os.path.join(jpg_folder, closest_file))
        dst_path = str(os.path.join(dest_folder, closest_file))

        if not os.path.exists(dst_path):  # Avoid overwriting duplicates
            shutil.move(src_path, dst_path)
            print(f"Moved {closest_file} for meter {record['meter_number']} → {dest_folder}")
        else:
            print(f"Skipped {closest_file}, already in destination.")

def process_tlog(logfile):
    # Connect to the tlog
    try:
        mav = mavutil.mavlink_connection(logfile)
    except FileNotFoundError:
        print(f"Error: File '{logfile}' not found.")
        exit(1)

    positions = []

    while True:
        # Look only at LOCAL_POSITION_NED messages, this will go faster
        msg = mav.recv_match(type="LOCAL_POSITION_NED", blocking=False)
        if msg is None:
            break

        timestamp = getattr(msg, "_timestamp", 0.0)
        if timestamp > 0:
            positions.append((timestamp, msg.x, msg.y))

    return positions

def positions_to_meter_records(positions):
    """
    This is the core algorithm. It takes a list of (timestamp, x, y) tuples and returns a list of meter records.
    """
    pacific = pytz.timezone("US/Pacific")

    # Tracking variables
    prev_x, prev_y = None, None
    cumulative_distance = 0.0
    previous_distance = 0.0
    next_meter = 1
    meter_records = []

    for timestamp, x, y in positions:
        current_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(pacific)

        if prev_x is not None and prev_y is not None:
            # Increment cumulative distance
            cumulative_distance += step_distance(prev_x, prev_y, x, y)
            print(f"Cumulative Distance: {cumulative_distance}, {current_time.strftime('%Y_%m_%d_%H-%M-%S')}")

            # Check if we've passed one or more whole meters
            while cumulative_distance >= next_meter:
                delta = cumulative_distance - previous_distance
                print(f"meter: {next_meter}")
                meter_records.append({
                    "meter_number": next_meter,
                    "timestamp": current_time.strftime("%Y_%m_%d_%H-%M-%S"),
                    "cumulative_dist": cumulative_distance,
                    "increment": delta,
                    "x": x,
                    "y": y
                })
                previous_distance = cumulative_distance
                next_meter += 1

        prev_x, prev_y = x, y

    return meter_records

def main():
    # Prompt user for input/output
    logfile = input("Enter the path to your .tlog file: ").strip()
    save_location = input("Enter the path to save the meter marker CSV: ").strip()

    # Process the logfile
    positions = process_tlog(logfile)
    meter_records = positions_to_meter_records(positions)

    # Save results
    df = pd.DataFrame(meter_records, columns=["meter_number", "timestamp", "cumulative_dist", "increment", "x", "y"])
    if not df.empty:
        filename = os.path.splitext(os.path.basename(logfile))[0] + "_meter_markers.csv"
        csv_path = os.path.join(save_location, filename)
        df.to_csv(csv_path, index=False)
        print(f"Meter marker timestamps saved to: {csv_path}")
    else:
        print("No meter marks detected.")

    # Optional image moving
    choice = input("Move jpgs based on meter marks? (y/n): ").strip().lower()
    if choice == "y" and meter_records:
        jpg_folder = input("Enter the folder containing jpgs: ").strip()
        dest_folder = input("Enter the destination folder for jpgs: ").strip()
        move_images_based_on_markers(meter_records, jpg_folder, dest_folder)

if __name__ == "__main__":
    main()
