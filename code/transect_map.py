#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
transect_map.py
last modified: 2025-11-05 
-----------------------------

Purpose:
    Creates a leaflet map of ROV tracks.

Description:
    Leaflet map of ROV tracks for three different localization options: 
    GPS-black, DVL-blue, EKF-red. Transect csv files are created using 
    the tlog_to_csv.py script.


Author:
    Megan H. Williams - Seattle Aquarium 
"""

import folium
import pandas as pd
import numpy as np
import os
from tkinter import Tk, filedialog, messagebox
from folium import Element  # for legend HTML

# --- helpers ---
def find_coord_pair(df, candidates):
    for lat_col, lon_col in candidates:
        if {lat_col, lon_col}.issubset(df.columns):
            return lat_col, lon_col
    return None, None

def clean_coords(lat_series, lon_series):
    s = pd.DataFrame({"lat": lat_series, "lon": lon_series}).copy()
    s = s[np.isfinite(s["lat"]) & np.isfinite(s["lon"])]
    s = s[(s["lat"] != 0) & (s["lon"] != 0)]
    return list(s.itertuples(index=False, name=None))

def first_valid_center(dfs):
    if not dfs:
        return 0.0, 0.0
    df = dfs[0]
    for pair in [
        ("Latitude", "Longitude"),
        ("EKFlat", "EKFlon"),
        ("EKF.lat", "EKF.lon"),
        ("EKF_lat", "EKF_lon"),
        ("DVLlat", "DVLlon"),
    ]:
        lat_col, lon_col = pair
        if {lat_col, lon_col}.issubset(df.columns):
            coords = clean_coords(df[lat_col], df[lon_col])
            if coords:
                lats, lons = zip(*coords)
                return float(np.mean(lats)), float(np.mean(lons))
    return 0.0, 0.0

def select_multiple_folders_files():
    """
    Allow user to select CSV files from multiple folders.
    Press 'Cancel' (or close dialog) to finish.
    """
    Tk().withdraw()
    all_files = []
    while True:
        files = filedialog.askopenfilenames(
            title=f"Select CSV files (selected so far: {len(all_files)})",
            filetypes=[("CSV files", "*.csv")]
        )
        if not files:
            break
        all_files.extend(list(files))
        more = messagebox.askyesno("Add more?", "Select more files from another folder?")
        if not more:
            break
    # De-duplicate while preserving order
    return list(dict.fromkeys(all_files))

def add_static_legend(m):
    legend_html = """
    <div style="
        position: fixed;
        bottom: 50px;
        left: 20px;
        z-index: 9999;
        background: white;
        border: 2px solid #444;
        padding: 10px 12px;
        font-size: 14px;
        box-shadow: 0 0 8px rgba(0,0,0,0.3);
        border-radius: 6px;">
      <div style="font-weight:600; margin-bottom:6px;">Legend</div>
      <div style="display:flex; align-items:center; margin-bottom:4px;">
        <span style="display:inline-block; width:12px; height:12px; background:black; margin-right:8px;"></span>
        GPS (Lat/Lon)
      </div>
      <div style="display:flex; align-items:center; margin-bottom:4px;">
        <span style="display:inline-block; width:12px; height:12px; background:red; margin-right:8px;"></span>
        EKF (EKFlat / EKFlon)
      </div>
      <div style="display:flex; align-items:center;">
        <span style="display:inline-block; width:12px; height:12px; background:blue; margin-right:8px;"></span>
        DVL (DVLlat / DVLlon)
      </div>
    </div>
    """
    m.get_root().html.add_child(Element(legend_html))

def create_map_with_transects():
    csv_files = select_multiple_folders_files()
    if not csv_files:
        print("No files selected.")
        return

    # Load all selected files upfront
    dfs = [pd.read_csv(p) for p in csv_files]

    # Center map; add scale bar via control_scale=True
    midpoint_lat, midpoint_lon = first_valid_center(dfs)
    m = folium.Map(location=[midpoint_lat, midpoint_lon], zoom_start=15, control_scale=True)

    for data, csv_file in zip(dfs, csv_files):
        file_name = os.path.basename(csv_file).replace(".csv", "")
        coord_sets = []

        # GPS
        if {"Latitude", "Longitude"}.issubset(data.columns):
            coords = clean_coords(data["Latitude"], data["Longitude"])
            if coords:
                coord_sets.append(("black", coords, f"{file_name} Lat/Lon"))

        # EKF (support several naming styles)
        ekf_candidates = [
            ("EKF.lat", "EKF.lon"),
            ("EKF_lat", "EKF_lon"),
            ("EKFlat", "EKFlon"),  # common in your ROV CSVs
        ]
        lat_col, lon_col = find_coord_pair(data, ekf_candidates)
        if lat_col:
            coords = clean_coords(data[lat_col], data[lon_col])
            if coords:
                coord_sets.append(("red", coords, f"{file_name} EKF ({lat_col}/{lon_col})"))

        # DVL
        if {"DVLlat", "DVLlon"}.issubset(data.columns):
            coords = clean_coords(data["DVLlat"], data["DVLlon"])
            if coords:
                coord_sets.append(("blue", coords, f"{file_name} DVLlat/DVLlon"))

        if not coord_sets:
            print(f"⚠️ No valid coordinate columns found in {file_name}. Skipping.")
            continue

        for color, coords, label in coord_sets:
            folium.PolyLine(coords, color=color, weight=2.5, opacity=1, tooltip=label).add_to(m)

    # Add legend & save
    add_static_legend(m)
    output_dir = os.path.dirname(csv_files[0]) if csv_files else os.getcwd()
    output_html = os.path.join(output_dir, "transect_map.html")
    m.save(output_html)
    print(f"✅ Map saved to {output_html}")

if __name__ == "__main__":
    create_map_with_transects()
