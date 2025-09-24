import folium
import pandas as pd
import os
from tkinter import Tk
from tkinter.filedialog import askopenfilenames
import random

# Function to generate a random color in hex format
def get_random_color():
    return "#{:06x}".format(random.randint(0, 0xFFFFFF))

# Function to load CSV files and create a map with multiple transects
def create_map_with_transects():
    # Hide Tkinter root window
    Tk().withdraw()
    
    # Ask user to select multiple CSV files
    csv_files = askopenfilenames(title="Select CSV Files", filetypes=[("CSV files", "*.csv")])
    
    if not csv_files:
        print("No files selected.")
        return
    
    # Initialize map (centered based on first file's midpoint)
    data_first = pd.read_csv(csv_files[0])
    midpoint_lat = data_first['Latitude'].mean()
    midpoint_lon = data_first['Longitude'].mean()
    m = folium.Map(location=[midpoint_lat, midpoint_lon], zoom_start=15)
    
    # Loop through selected CSV files and add transect lines to the map
    for csv_file in csv_files:
        data = pd.read_csv(csv_file)
        
        # Create a list of lat/lon tuples for the transect (Latitude/Longitude)
        transect_coords = list(zip(data['Latitude'], data['Longitude']))

        # Create a list of EKFlat/lon tuples for the transect (Latitude/Longitude)
        EKF_coords = list(zip(data['EKF.lat'], data['EKF.lon']))
        
        # Create a list of DVLlat/DVLlon tuples for the transect (if columns exist)
        #dvl_coords = list(zip(data['DVLlat'], data['DVLlon']))
       
        # Get file name for labeling (without extension)
        file_name = os.path.basename(csv_file).replace('.csv', '')
        
        # Generate colors for both transects (Latitude/Longitude and DVLlat/DVLlon)
        latlon_color = "black"
        EKF_color = "red"
        #dvl_color = "blue"
        
        # Add the Latitude/Longitude transect line to the map
        folium.PolyLine(transect_coords, color=latlon_color, weight=2.5, opacity=1, tooltip=f"{file_name} Lat/Lon").add_to(m)
        
        # Add the EKFlat/EKFlon transect line to the map
        folium.PolyLine(EKF_coords, color=EKF_color, weight=2.5, opacity=1, tooltip=f"{file_name} EKF.lat/EKF.lon").add_to(m)

        # Add the DVLlat/DVLlon transect line to the map
        #folium.PolyLine(dvl_coords, color=dvl_color, weight=2.5, opacity=1, tooltip=f"{file_name} DVLlat/DVLlon").add_to(m)
    
    # Save the map to the same directory as the first CSV file
    output_html = os.path.join(os.path.dirname(csv_files[0]), 'combined_transect_map.html')
    m.save(output_html)
    
    print(f"Map saved to {output_html}")

# Run the function to create the map
create_map_with_transects()
