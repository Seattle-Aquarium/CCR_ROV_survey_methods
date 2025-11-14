import pandas as pd
from pymavlink import mavutil
from datetime import datetime, timezone
import math
import pytz
from geopy.distance import geodesic
import os

# Constants for the area calculation
reference_width = 1.15  # Width in meters at 0.66m altitude
reference_height = 0.66  # Reference altitude in meters
reference_area = 0.9545  # Known area at reference height in square meters

# Function to calculate width based on altitude
def calculate_width(altitude):
    return reference_width * (altitude / reference_height) if altitude > 0 else 0

# Function to calculate area based on the altitude
def calculate_area(altitude):
    return reference_area * (altitude / reference_height) ** 2 if altitude > 0 else 0

# Function to calculate the total displacement (distance) from DVLx and DVLy
def calculate_distance(dvlx, dvly):
    return math.sqrt(dvlx**2 + dvly**2)

# Prompt the user to enter the tlog file path and save location
logfile = input("Enter the path to your .tlog file: ")
site_number = input("Enter the site number/ name: ")
save_location = input("Enter the path to save the transects folder: ")

transects_folder = os.path.join(save_location, "transects")
if not os.path.exists(transects_folder):
    os.makedirs(transects_folder)
    print(f"Created folder: {transects_folder}")
else:
    print(f"Folder already exists: {transects_folder}")

# Get transect start and end times
transects = []
for i in range(1, 5):
    start_time_str = input(f"Enter start time for transect {i} (HH:MM:SS) or leave blank: ")
    end_time_str = input(f"Enter end time for transect {i} (HH:MM:SS) or leave blank: ")
    if start_time_str and end_time_str:
        transects.append((start_time_str, end_time_str))
    else:
        break

# If no transects are entered, process the entire file
if not transects:
    transects = [("00:00:00", "23:59:59")]
    print("No transects entered. Processing the entire file.")

# Connect to the tlog file
try:
    mav = mavutil.mavlink_connection(logfile)
except FileNotFoundError:
    print(f"Error: File '{logfile}' not found.")
    exit(1)

# Initialize storage for data and variables
data = {}
count = {}
latest_time = None
lat, lon, dvlx, dvly, altitude, depth, heading = (None,) * 7
initial_lat, initial_lon = None, None

# Set the Pacific Time zone using pytz
pacific = pytz.timezone('US/Pacific')
file_date_str = None

# Debugging placeholders
print("Starting to process the tlog file...")

while True:
    msg = mav.recv_match(blocking=False)
    if msg is None:
        break

    if msg.get_type() == "BAD_DATA":
        continue

    if msg.get_type() == "SYSTEM_TIME":
        unix_time = msg.time_unix_usec / 1e6
        if unix_time > 0:
            latest_time = datetime.fromtimestamp(unix_time, tz=timezone.utc).astimezone(pacific)
            file_date_str = latest_time.strftime('%Y_%m_%d')

    if latest_time:
        second_key = latest_time.replace(microsecond=0)
        date = latest_time.strftime('%Y-%m-%d')
        time = latest_time.strftime('%H:%M:%S')

        if second_key not in data:
            data[second_key] = [date, time, None, None, 0, 0, 0, 0, 0, 0, 0]
            count[second_key] = 0

    if msg.get_type() == "GPS_RAW_INT":
        lat = msg.lat / 1e7
        lon = msg.lon / 1e7

        # Update only if valid GPS data is received
        if lat != 0 and lon != 0:
            initial_lat = lat if initial_lat is None else initial_lat
            initial_lon = lon if initial_lon is None else initial_lon

    elif msg.get_type() == "VFR_HUD":
        depth = msg.alt

    elif msg.get_type() == "LOCAL_POSITION_NED":
        dvlx = msg.x
        dvly = msg.y

    elif msg.get_type() == "RANGEFINDER":
        altitude = msg.distance

    elif msg.get_type() == "ATTITUDE":
        yaw = msg.yaw
        heading = (math.degrees(yaw) + 360) % 360

    # Fill missing lat/lon with last known valid values
    if latest_time:
        data[second_key][2] = lat if lat != 0 else initial_lat
        data[second_key][3] = lon if lon != 0 else initial_lon
        data[second_key][4] += dvlx if dvlx is not None else 0
        data[second_key][5] += dvly if dvly is not None else 0
        data[second_key][6] += altitude if altitude is not None else 0
        data[second_key][7] += depth if depth is not None else 0
        data[second_key][8] += heading if heading is not None else 0
        data[second_key][9] += calculate_width(altitude) if altitude is not None else 0
        data[second_key][10] += calculate_area(altitude) if altitude is not None else 0
        count[second_key] += 1

# Average the values for each second
for second_key in data:
    if count[second_key] > 0:
        data[second_key][4:] = [x / count[second_key] for x in data[second_key][4:]]

# Backfill missing Latitude and Longitude
for second_key, row in data.items():
    if row[2] is None or row[3] is None:
        row[2] = initial_lat
        row[3] = initial_lon


# Function to calculate DVLlat and DVLlon
def add_dvl_coordinates(df, scale_factor=0.002):
    # Set the first row's DVLlat and DVLlon equal to the Latitude and Longitude
    df['DVLlat'] = df['Latitude']
    df['DVLlon'] = df['Longitude']

    # Apply cumulative changes to DVLlat and DVLlon for subsequent rows
    for i in range(1, len(df)):
        # Get the DVLx and DVLy (displacements in meters) and scale them down
        dvlx = df.at[i, 'DVLx'] * scale_factor
        dvly = df.at[i, 'DVLy'] * scale_factor

        # Calculate the total displacement (distance) from DVLx and DVLy
        distance = calculate_distance(dvlx, dvly)

        # Use the heading from the CSV (assumed to be in degrees) for the bearing
        bearing = df.at[i, 'Heading']

        # Get the previous latitude and longitude
        previous_latlon = (df.at[i - 1, 'DVLlat'], df.at[i - 1, 'DVLlon'])

        # Calculate the new latitude and longitude using geopy's geodesic function
        new_position = geodesic(meters=distance).destination(previous_latlon, bearing)

        # Store the new DVLlat and DVLlon in the dataframe
        df.at[i, 'DVLlat'] = new_position.latitude
        df.at[i, 'DVLlon'] = new_position.longitude

    return df

# Process transects and adjust DVLx/DVLy
for i, (start_time_str, end_time_str) in enumerate(transects):
    start_time = datetime.strptime(start_time_str, '%H:%M:%S').time()
    end_time = datetime.strptime(end_time_str, '%H:%M:%S').time()
    transect_data = [row for row in data.values() if start_time <= datetime.strptime(row[1], '%H:%M:%S').time() <= end_time]

    if len(transect_data) > 0:
        initial_dvlx = transect_data[0][4]
        initial_dvly = transect_data[0][5]

        for idx, row in enumerate(transect_data):
            if idx > 0:
                row[4] -= initial_dvlx
                row[5] -= initial_dvly

        transect_data[0][4] = 0
        transect_data[0][5] = 0

    df_transect = pd.DataFrame(transect_data, columns=['Date', 'Time', 'Latitude', 'Longitude', 
                                                       'DVLx', 'DVLy', 'Altitude', 'Depth', 'Heading', 'Width', 'Area_m2'])

    # Add DVLlat and DVLlon columns
    df_transect = add_dvl_coordinates(df_transect)

    # Reorder columns to place DVLlat and DVLlon after DVLx and DVLy
    reordered_columns = ['Date', 'Time', 'Latitude', 'Longitude', 
                         'DVLx', 'DVLy', 'DVLlat', 'DVLlon', 
                         'Altitude', 'Depth', 'Heading', 'Width', 'Area_m2']
    df_transect = df_transect[reordered_columns]

    # Save to CSV
    csv_filename = f"{file_date_str}_{site_number}_T{i+1}.csv"
    csv_full_path = os.path.join(transects_folder, csv_filename)
    df_transect.to_csv(csv_full_path, index=False)
    print(f"Transect {i+1} saved to '{csv_full_path}'.")
