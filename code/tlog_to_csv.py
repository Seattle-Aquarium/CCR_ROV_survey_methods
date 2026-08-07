#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
tlog_to_csv.py
last modified: 2026-07-14

Purpose
-------
Converts BlueROV2 `.tlog` files into per-transect CSVs that include time, position,
depth, altitude, area, and speed. The script produces DVL-based tracks that are
georeferenced, smoothed, and averaged at one-second intervals for analysis or mapping.

Usage
-----
    python tlog_to_csv.py

A window opens to:
  1. Add one or more .tlog files (or a whole folder of them). Multiple tlogs are
     treated as one continuous dive log (useful when a dive was split across
     several files).
  2. Enter the site name, survey date, and tide station.
  3. Choose a save location.
  4. Define one or more transects, each with a unique Transect ID (used as the
     output filename) and one or more start/end time windows (e.g. a transect
     that was paused and resumed: 10:07:41-10:13:50 and 10:35:52-10:40:07 both
     belong to the same transect).

One CSV is written per transect (named "{Transect_ID}.csv"), combining all of
that transect's time windows.

Localization sources
--------------------
- EKFlat/EKFlon (fused position) - The most accurate source, combining GPS, DVL, and IMU
  data to produce a stable, drift-corrected position (`GLOBAL_POSITION_INT`).

- GPS Latitude/Longitude (surface position) - GPS positioning from Water Linked GPS G2 system.
  Results in noisy tracks.

- DVLlat/DVLlon (LOCAL_POSITION_NED) - Provides precise local movement vectors (x = North,
  y = East) GPS-anchored. Tracks are built using these vectors rather than compass heading to
  avoid yaw drift. DVL movement is converted into geographic coordinates (`DVLlat`, `DVLlon`)
  using geodesic steps.

Depth handling
--------------
Depth follows a negative-down convention:
- Prefer *VFR_HUD.alt* when it's below -0.5 m (already negative-down).
- Otherwise, use *-LOCAL_POSITION_NED.z*.
The chosen source is noted in the `Depth_Source` column.

Field of view and area
----------------------
Altitude (from the rangefinder) is used to estimate:
- Width (m) - Scales linearly with altitude.
- Area (m2) - Scales with the square of altitude.
These values represent the approximate camera footprint on the seafloor.

Averaging
---------
Each second of data is summarized to reduce noise:
- Averaged values: altitude, heading, width, area, and velocity.
- Last recorded values: DVLx, DVLy, latitude, longitude, depth.
- Derived values include `Distance` (per-step motion) and `DVLlat/DVLlon`
  (georeferenced DVL track seeded by GPS).

Outputs
-------
Generates one CSV per transect containing:
Date, Time, Latitude, Longitude, EKFlat, EKFlon, DVLx, DVLy, DVLlat, DVLlon,
Altitude, Depth, Depth_std, Depth_Source, Heading, Velocity_mps, Width, Area_m2, Distance.

Requirements:
    pip install pandas numpy pytz pymavlink geopy requests scipy
"""

import glob
import json
import logging
import math
import os
import re
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import requests  # requires internet access for NOAA tide API
from geopy.distance import geodesic
from pymavlink import mavutil
from scipy.interpolate import CubicSpline

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# -----------------------------
# Constants & settings
# -----------------------------
REFERENCE_WIDTH_M = 1.10
REFERENCE_ALT_M   = 0.82
REFERENCE_AREA_M2 = 0.99 #0.9m x 1.10m <--- after crop 4606x4030
PACIFIC_TZ        = pytz.timezone('US/Pacific')

DVL_SCALE    = 1.0   # if your DVLx/DVLy are meters, keep 1.0; use 0.002 if your old data expects it
MIN_STEP_M   = 0.02  # ignore tiny jitter steps (< 2 cm)
JUMP_THRESH  = 5.0   # meters; treat larger per-second jumps as EKF resets (tune as needed)
RESEED_ON_JUMP = False  # set to False to preserve continuous DVL dead-reckoning (show DVL drift)

# Which system/component to trust for autopilot values (mode + SYS_STATUS power)
AUTOPILOT_SYSID  = 1
AUTOPILOT_COMPID = 1

# NOAA tide stations offered in the GUI: (display label, station id)
STATIONS = [
    ("Elliott Bay (9447130)", "9447130"),
    ("Friday Harbor (9449880)", "9449880"),
    ("Neah Bay (9443090)", "9443090"),
]

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

# Path for persisting GUI state between runs
_CONFIG_PATH = Path(__file__).parent / ".tlog_to_csv_config.json"


def _load_gui_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_gui_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# -----------------------------
# Helpers
# -----------------------------
def calculate_width(alt_m):
    return REFERENCE_WIDTH_M * (alt_m / REFERENCE_ALT_M) if (alt_m is not None and alt_m > 0) else 0.0

def calculate_area(alt_m):
    return REFERENCE_AREA_M2 * (alt_m / REFERENCE_ALT_M) ** 2 if (alt_m is not None and alt_m > 0) else 0.0

def _finite(x):
    try:
        return np.isfinite(float(x))
    except Exception:
        return False

def _finite_nz(x):
    try:
        return np.isfinite(float(x)) and float(x) != 0
    except Exception:
        return False

_FILENAME_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')

def _sanitize_filename(name: str) -> str:
    return _FILENAME_INVALID_CHARS.sub("_", name).strip() or "transect"


# ============================================================
# GUI
# ============================================================
class ScrollableFrame(ttk.Frame):
    """A vertically scrollable frame (mouse-wheel enabled) for a growing list of widgets."""

    def __init__(self, parent, height=220, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class TransectBlock:
    """One transect: a header plus one-or-more start/end time-window rows."""

    def __init__(self, parent, index, on_remove):
        self.on_remove = on_remove
        self.window_rows = []  # list of dicts: {"frame", "start_var", "end_var"}

        self.frame = ttk.Frame(parent, relief="groove", borderwidth=1)
        self.frame.pack(fill="x", padx=4, pady=4)

        header = ttk.Frame(self.frame)
        header.pack(fill="x", padx=4, pady=(4, 0))
        self.label_var = tk.StringVar()
        ttk.Label(header, textvariable=self.label_var, font=("Helvetica", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Remove transect", command=self._remove_self).pack(side="right")

        id_row = ttk.Frame(self.frame)
        id_row.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(id_row, text="Transect ID:").pack(side="left")
        self.transect_id_var = tk.StringVar()
        ttk.Entry(id_row, textvariable=self.transect_id_var, width=30).pack(side="left", padx=(4, 0))
        ttk.Label(id_row, text="(used as the output CSV filename, e.g. EBM_S24_T4)",
                  foreground="grey").pack(side="left", padx=(6, 0))

        self.rows_frame = ttk.Frame(self.frame)
        self.rows_frame.pack(fill="x", padx=4)

        ttk.Button(self.frame, text="+ Add time window", command=self.add_window_row).pack(
            anchor="w", padx=4, pady=(2, 6))

        self.set_index(index)
        self.add_window_row()

    def set_index(self, index):
        self.index = index
        self.label_var.set(f"Transect {index}")

    def add_window_row(self):
        row = ttk.Frame(self.rows_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Start (HH:MM:SS):").pack(side="left")
        start_var = tk.StringVar()
        ttk.Entry(row, textvariable=start_var, width=10).pack(side="left", padx=(2, 10))
        ttk.Label(row, text="End (HH:MM:SS):").pack(side="left")
        end_var = tk.StringVar()
        ttk.Entry(row, textvariable=end_var, width=10).pack(side="left", padx=(2, 10))

        entry = {"frame": row, "start_var": start_var, "end_var": end_var}

        def remove_row():
            if len(self.window_rows) <= 1:
                return  # always keep at least one window row per transect
            self.window_rows.remove(entry)
            row.destroy()

        ttk.Button(row, text="x", width=2, command=remove_row).pack(side="left")
        self.window_rows.append(entry)

    def _remove_self(self):
        self.frame.destroy()
        self.on_remove(self)

    def get_windows(self):
        windows = []
        for w in self.window_rows:
            s = w["start_var"].get().strip()
            e = w["end_var"].get().strip()
            if s and e:
                windows.append((s, e))
        return windows

    def get_transect_id(self):
        return self.transect_id_var.get().strip()


def get_args_via_gui() -> dict:
    result = {}
    cfg = _load_gui_config()

    root = tk.Tk()
    root.title("Tlog to CSV — Transect Extractor")
    pad = {"padx": 10, "pady": 5}

    ttk.Label(root, text="Tlog to CSV — Transect Extractor",
              font=("Helvetica", 13, "bold")).grid(row=0, column=0, columnspan=3, pady=(14, 2), padx=14)
    ttk.Label(root, text="Add tlog file(s), fill in site info, define transect ID(s) and time window(s), then click Run.",
              foreground="grey").grid(row=1, column=0, columnspan=3, pady=(0, 4))
    ttk.Separator(root, orient="horizontal").grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=4)

    # ---- Tlog files ----
    ttk.Label(root, text="Tlog file(s):").grid(row=3, column=0, sticky="ne", **pad)
    tlog_frame = ttk.Frame(root)
    tlog_frame.grid(row=3, column=1, columnspan=2, sticky="w", **pad)

    tlog_listbox = tk.Listbox(tlog_frame, width=60, height=4, selectmode="extended")
    tlog_listbox.pack(side="top", fill="x")

    tlog_paths = []
    last_dir = cfg.get("tlog_dir", "")

    def refresh_listbox():
        tlog_listbox.delete(0, tk.END)
        for p in tlog_paths:
            tlog_listbox.insert(tk.END, p)

    def add_files():
        nonlocal last_dir
        files = filedialog.askopenfilenames(
            title="Select .tlog file(s)",
            initialdir=last_dir or str(Path.home()),
            filetypes=[("Telemetry log files", "*.tlog"), ("All files", "*.*")],
        )
        if files:
            last_dir = str(Path(files[0]).parent)
            for f in files:
                if f not in tlog_paths:
                    tlog_paths.append(f)
            refresh_listbox()

    def add_folder():
        nonlocal last_dir
        folder = filedialog.askdirectory(
            title="Select folder containing .tlog files",
            initialdir=last_dir or str(Path.home()))
        if folder:
            last_dir = folder
            found = sorted(glob.glob(os.path.join(folder, "*.tlog")))
            if not found:
                messagebox.showwarning("No tlog files", f"No .tlog files found in:\n{folder}")
            for f in found:
                if f not in tlog_paths:
                    tlog_paths.append(f)
            refresh_listbox()

    def remove_selected():
        selected = list(tlog_listbox.curselection())
        for i in reversed(selected):
            del tlog_paths[i]
        refresh_listbox()

    def clear_all():
        tlog_paths.clear()
        refresh_listbox()

    btn_row = ttk.Frame(tlog_frame)
    btn_row.pack(side="top", fill="x", pady=(4, 0))
    ttk.Button(btn_row, text="Add File(s)...", command=add_files).pack(side="left", padx=(0, 4))
    ttk.Button(btn_row, text="Add Folder...", command=add_folder).pack(side="left", padx=4)
    ttk.Button(btn_row, text="Remove Selected", command=remove_selected).pack(side="left", padx=4)
    ttk.Button(btn_row, text="Clear All", command=clear_all).pack(side="left", padx=4)

    ttk.Separator(root, orient="horizontal").grid(row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

    # ---- Site info ----
    ttk.Label(root, text="Site name:").grid(row=5, column=0, sticky="e", **pad)
    site_var = tk.StringVar(value=cfg.get("site_name", ""))
    ttk.Entry(root, textvariable=site_var, width=30).grid(row=5, column=1, sticky="w", **pad)

    ttk.Label(root, text="Survey date (YYYYMMDD):").grid(row=6, column=0, sticky="e", **pad)
    date_var = tk.StringVar(value=cfg.get("survey_date", ""))
    ttk.Entry(root, textvariable=date_var, width=30).grid(row=6, column=1, sticky="w", **pad)

    ttk.Label(root, text="Tide station:").grid(row=7, column=0, sticky="e", **pad)
    station_var = tk.StringVar(value=cfg.get("station_display", STATIONS[0][0]))
    ttk.Combobox(root, textvariable=station_var, width=28, state="readonly",
                 values=[s[0] for s in STATIONS]).grid(row=7, column=1, sticky="w", **pad)

    ttk.Label(root, text="Save location (folder):").grid(row=8, column=0, sticky="e", **pad)
    save_var = tk.StringVar(value=cfg.get("save_location", ""))
    ttk.Entry(root, textvariable=save_var, width=50).grid(row=8, column=1, **pad)

    def browse_save():
        p = filedialog.askdirectory(title="Select save location",
                                     initialdir=save_var.get().strip() or str(Path.home()))
        if p:
            save_var.set(p)

    ttk.Button(root, text="Browse...", command=browse_save).grid(row=8, column=2, **pad)

    ttk.Separator(root, orient="horizontal").grid(row=9, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

    # ---- Transects ----
    ttk.Label(root, text="Transects", font=("Helvetica", 11, "bold")).grid(
        row=10, column=0, columnspan=3, sticky="w", padx=14)
    ttk.Label(root,
              text="Give each transect a unique ID (used as the output filename). A transect\n"
                   "may have more than one start/end window (e.g. paused and resumed later).",
              foreground="grey", justify="left").grid(row=11, column=0, columnspan=3, sticky="w", padx=14)

    scroll_area = ScrollableFrame(root, height=240)
    scroll_area.grid(row=12, column=0, columnspan=3, sticky="ew", padx=10, pady=(4, 0))

    transect_blocks = []

    def remove_block(block):
        transect_blocks.remove(block)
        for idx, b in enumerate(transect_blocks, start=1):
            b.set_index(idx)

    def add_transect():
        block = TransectBlock(scroll_area.inner, len(transect_blocks) + 1, remove_block)
        transect_blocks.append(block)

    ttk.Button(root, text="+ Add Transect", command=add_transect).grid(
        row=13, column=0, columnspan=3, pady=(6, 4))

    add_transect()  # start with one transect block

    ttk.Separator(root, orient="horizontal").grid(row=14, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

    btn_frame = ttk.Frame(root)
    btn_frame.grid(row=15, column=0, columnspan=3, pady=(0, 14))

    def on_run():
        if not tlog_paths:
            messagebox.showerror("Missing input", "Please add at least one .tlog file.")
            return
        for p in tlog_paths:
            if not Path(p).is_file():
                messagebox.showerror("File not found", f"Tlog file not found:\n{p}")
                return

        site_name = site_var.get().strip()
        if not site_name:
            messagebox.showerror("Missing input", "Please enter a site name.")
            return

        survey_date = date_var.get().strip()
        try:
            datetime.strptime(survey_date, "%Y%m%d")
        except ValueError:
            messagebox.showerror("Invalid date", "Survey date must be in YYYYMMDD format.")
            return

        station_display = station_var.get()
        station_id = dict(STATIONS).get(station_display)
        if not station_id:
            messagebox.showerror("Missing input", "Please select a tide station.")
            return

        save_location = save_var.get().strip()
        if not save_location:
            messagebox.showerror("Missing input", "Please choose a save location.")
            return

        transects = []
        for block in transect_blocks:
            windows = block.get_windows()
            if not windows:
                continue
            for s, e in windows:
                try:
                    t_start = datetime.strptime(s, "%H:%M:%S").time()
                    t_end = datetime.strptime(e, "%H:%M:%S").time()
                except ValueError:
                    messagebox.showerror(
                        "Invalid time",
                        f"Transect {block.index}: times must be in HH:MM:SS format.\n"
                        f"Got start='{s}', end='{e}'.")
                    return
                if t_start >= t_end:
                    messagebox.showerror(
                        "Invalid time window",
                        f"Transect {block.index}: start time must be before end time.\n"
                        f"Got start='{s}', end='{e}'.")
                    return
            transect_id = block.get_transect_id()
            if not transect_id:
                messagebox.showerror(
                    "Missing input",
                    f"Transect {block.index}: please enter a Transect ID.")
                return
            transects.append({"transect_id": transect_id, "windows": windows})

        seen_ids = set()
        for transect in transects:
            if transect["transect_id"] in seen_ids:
                messagebox.showerror(
                    "Duplicate Transect ID",
                    f"Transect ID '{transect['transect_id']}' is used more than once.\n"
                    "Each transect needs a unique ID.")
                return
            seen_ids.add(transect["transect_id"])

        if not transects:
            if not messagebox.askyesno(
                "No transects defined",
                "No transect time windows were entered.\n\n"
                "Process the entire tlog as a single transect?"
            ):
                return
            transects = [{"transect_id": f"{survey_date}_T1", "windows": [("00:00:00", "23:59:59")]}]

        result["tlog_paths"] = list(tlog_paths)
        result["site_name"] = site_name
        result["survey_date"] = survey_date
        result["station_id"] = station_id
        result["save_location"] = save_location
        result["transects"] = transects
        result["submitted"] = True

        _save_gui_config({
            "tlog_dir": last_dir,
            "site_name": site_name,
            "survey_date": survey_date,
            "station_display": station_display,
            "save_location": save_location,
        })
        root.destroy()

    def on_cancel():
        root.destroy()

    ttk.Button(btn_frame, text="  Run  ", command=on_run).pack(side="left", padx=8)
    ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="left", padx=8)

    root.mainloop()

    if not result.get("submitted"):
        print("Cancelled.")
        sys.exit(0)

    return result


# ============================================================
# NOAA tide data
# ============================================================
def get_tide_data(api_request: str) -> dict:
    response = requests.get(api_request)
    response.raise_for_status()
    return response.json()


def expand_noaa_to_seconds(df: pd.DataFrame) -> pd.DataFrame:
    times = pd.to_datetime(df["date"] + " " + df["time"])
    values = df["water_level"].values

    t0 = times.iloc[0]
    t_seconds = (times - t0).dt.total_seconds().values

    spline = CubicSpline(t_seconds, values, bc_type="natural")

    full_seconds = np.arange(int(t_seconds[0]), int(t_seconds[-1]) + 1)
    full_times = t0 + pd.to_timedelta(full_seconds, unit="s")
    full_values = spline(full_seconds)

    return pd.DataFrame({"Datetime": full_times, "water_level": full_values})


def fetch_tide_dataframe(survey_date: str, station_id: str) -> pd.DataFrame:
    api_request = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
        f"begin_date={survey_date}&end_date={survey_date}&station={station_id}"
        "&product=one_minute_water_level&datum=MLLW&time_zone=lst&units=metric&format=json"
    )
    tide_data = get_tide_data(api_request)
    if "data" not in tide_data:
        raise ValueError(f"NOAA tide API returned no data: {tide_data}")

    noaa_wl = pd.DataFrame(tide_data["data"])
    noaa_wl.rename(columns={"t": "datetime", "v": "water_level"}, inplace=True)
    noaa_wl["datetime"] = pd.to_datetime(noaa_wl["datetime"])
    noaa_wl["water_level"] = pd.to_numeric(noaa_wl["water_level"], errors="coerce")
    noaa_wl["date"] = noaa_wl["datetime"].dt.date.astype(str)
    noaa_wl["time"] = noaa_wl["datetime"].dt.strftime("%H:%M")
    return expand_noaa_to_seconds(noaa_wl)


def merge_tide(df_all: pd.DataFrame, tide_seconds_df: pd.DataFrame) -> pd.DataFrame:
    df_all = df_all.copy()
    df_all["Datetime"] = pd.to_datetime(df_all["Date"] + " " + df_all["Time"])
    df_all = pd.merge_asof(
        df_all.sort_values("Datetime"),
        tide_seconds_df.sort_values("Datetime"),
        on="Datetime",
        direction="backward",
        tolerance=pd.Timedelta("1min")
    ).sort_values("Datetime")
    df_all["Depth_std"] = (
        -df_all["Altitude"] +
        df_all["Depth"] +
        df_all["water_level"]
    )
    df_all.drop(columns=["Datetime", "water_level"], inplace=True)
    return df_all


# ============================================================
# Tlog parsing -> per-second dataframe
# ============================================================
def process_tlogs(tlog_paths: list) -> pd.DataFrame:
    """Parses one or more .tlog files as one continuous dive log and returns
    a per-second dataframe."""

    buckets = {}
    counts = {}
    latest_time = None

    # Running "current" message values (shared across all files so a split
    # log continues seamlessly)
    lat = lon = EKFlat = EKFlon = None
    dvlx = dvly = None
    altitude = None
    heading_deg = None
    velocity = None
    vfr_alt = None
    ned_z_val = None
    current_mode = None
    sys_v_mV = None
    sys_i_cA = None
    batt_mah = None
    batt_e_100J = None

    for logfile in tlog_paths:
        log.info(f"Opening {logfile} ...")
        mav = mavutil.mavlink_connection(logfile)

        while True:
            msg = mav.recv_match(blocking=False)
            if msg is None:
                break
            if msg.get_type() == "BAD_DATA":
                continue

            ts = getattr(msg, "_timestamp", 0.0)
            if ts > 0:
                latest_time = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PACIFIC_TZ)
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
                if src_compid == AUTOPILOT_COMPID and (src_sysid == AUTOPILOT_SYSID):
                    current_mode = getattr(msg, "custom_mode", None)

            elif t == "SYS_STATUS":
                if src_sysid == AUTOPILOT_SYSID and src_compid == AUTOPILOT_COMPID:
                    sys_v_mV = getattr(msg, "voltage_battery", None)  # mV
                    sys_i_cA = getattr(msg, "current_battery", None)  # cA (0.01A)

            elif t == "BATTERY_STATUS":
                batt_mah    = getattr(msg, "current_consumed", None)
                batt_e_100J = getattr(msg, "energy_consumed", None)

            # Update per-second bucket
            b = buckets[key]

            if _finite(lat) and _finite(lon) and lat != 0 and lon != 0:
                b['Latitude'] = float(lat)
                b['Longitude'] = float(lon)
                b['GPS_valid'] = True

            if _finite(EKFlat) and _finite(EKFlon) and EKFlat != 0 and EKFlon != 0:
                b['EKFlat'] = float(EKFlat)
                b['EKFlon'] = float(EKFlon)

            if _finite(dvlx): b['DVLx'] = float(dvlx)
            if _finite(dvly): b['DVLy'] = float(dvly)

            if _finite(ned_z_val): b['NEDz'] = float(ned_z_val)
            if _finite(vfr_alt):   b['VFR_alt'] = float(vfr_alt)

            if _finite(altitude):
                b['Altitude_sum'] += float(altitude)
                b['Width_sum']    += calculate_width(altitude)
                b['Area_sum']     += calculate_area(altitude)
            if _finite(heading_deg):
                b['Heading_sum']  += float(heading_deg)
            if _finite(velocity):
                b['Velocity_sum'] += float(velocity)

            if _finite(sys_v_mV) and float(sys_v_mV) > 0:
                V = float(sys_v_mV) / 1000.0
                b['Battery_V_sum'] += V
                if _finite(sys_i_cA):
                    A = float(sys_i_cA) / 100.0
                    b['Battery_A_sum'] += A
                    b['Battery_W_sum'] += (V * A)

            if _finite(batt_mah) and float(batt_mah) >= 0:
                b['Battery_mAh_total'] = float(batt_mah)

            if _finite(batt_e_100J) and float(batt_e_100J) >= 0:
                b['Battery_Wh_total'] = (float(batt_e_100J) * 100.0) / 3600.0

            if _finite(current_mode):
                mode_num = int(current_mode)
                b['Mode_num'] = mode_num
                b['Mode'] = ARDUSUB_MODE_MAP.get(mode_num, "UNKNOWN")

            counts[key] += 1

    if not buckets:
        raise ValueError("No data parsed from tlog file(s).")

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

            'Battery_V': (b['Battery_V_sum'] / n) if n else np.nan,
            'Battery_A': (b['Battery_A_sum'] / n) if n else np.nan,
            'Battery_W': (b['Battery_W_sum'] / n) if n else np.nan,

            'Battery_mAh_total': b['Battery_mAh_total'],
            'Battery_Wh_total':  b['Battery_Wh_total'],
        })

    df_all = pd.DataFrame(rows)

    for c in ['Battery_mAh_total', 'Battery_Wh_total']:
        if c in df_all.columns:
            df_all[c] = df_all[c].ffill()

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

    return df_all


# ============================================================
# Per-transect processing & export
# ============================================================
def build_transect_mask(df_all: pd.DataFrame, windows: list) -> pd.Series:
    times = df_all['Time'].apply(lambda t: datetime.strptime(t, "%H:%M:%S").time())
    mask = pd.Series(False, index=df_all.index)
    for start_str, end_str in windows:
        t_start = datetime.strptime(start_str, "%H:%M:%S").time()
        t_end   = datetime.strptime(end_str,   "%H:%M:%S").time()
        mask = mask | ((times >= t_start) & (times <= t_end))
    return mask


def export_transect(df_all: pd.DataFrame, windows: list, transect_num: int, transect_id: str,
                     site_name: str, transects_folder: str) -> tuple:
    """Filters df_all to the given time window(s), builds the georeferenced
    DVL track, and writes one transect CSV. Returns (output_path or None, info message)."""

    mask = build_transect_mask(df_all, windows)
    df_tran = df_all.loc[mask].copy()

    window_desc = ", ".join(f"{s}-{e}" for s, e in windows)
    if df_tran.empty:
        return None, f"Transect {transect_num}: no rows in window(s) {window_desc}"

    # ---- Transect battery deltas (used since start of transect) ----
    if 'Battery_mAh_total' in df_tran.columns:
        first_mah = df_tran['Battery_mAh_total'].dropna().iloc[0] if df_tran['Battery_mAh_total'].notna().any() else np.nan
        df_tran['Battery_mAh_used'] = df_tran['Battery_mAh_total'] - first_mah

    if 'Battery_Wh_total' in df_tran.columns:
        first_wh = df_tran['Battery_Wh_total'].dropna().iloc[0] if df_tran['Battery_Wh_total'].notna().any() else np.nan
        df_tran['Battery_Wh_used'] = df_tran['Battery_Wh_total'] - first_wh

    for c in ('DVLlat', 'DVLlon'):
        df_tran[c] = np.nan

    # Zero DVL x/y to start of transect (keep as relative)
    if _finite(df_tran['DVLx'].iloc[0]) and _finite(df_tran['DVLy'].iloc[0]):
        df_tran['DVLx'] = df_tran['DVLx'] - float(df_tran['DVLx'].iloc[0])
        df_tran['DVLy'] = df_tran['DVLy'] - float(df_tran['DVLy'].iloc[0])

    # Seed DVLlat/DVLlon at first valid GPS (fallback to EKF)
    gps_mask = df_tran[['Latitude', 'Longitude']].apply(
        lambda r: _finite_nz(r['Latitude']) and _finite_nz(r['Longitude']), axis=1
    )
    seed_idx = gps_mask.idxmax() if gps_mask.any() else None
    use_ekf  = False
    if seed_idx is None:
        ekf_mask = df_tran[['EKFlat', 'EKFlon']].apply(
            lambda r: _finite_nz(r['EKFlat']) and _finite_nz(r['EKFlon']), axis=1
        )
        seed_idx = ekf_mask.idxmax() if ekf_mask.any() else None
        use_ekf  = seed_idx is not None

    seed_warning = None
    if seed_idx is not None:
        lat0 = df_tran.at[seed_idx, 'Latitude' if not use_ekf else 'EKFlat']
        lon0 = df_tran.at[seed_idx, 'Longitude' if not use_ekf else 'EKFlon']
        df_tran.loc[:seed_idx, ['DVLlat', 'DVLlon']] = [lat0, lon0]
        df_tran[['DVLlat', 'DVLlon']] = df_tran[['DVLlat', 'DVLlon']].ffill()
    else:
        seed_warning = "no GPS or EKF fix to seed lat/lon"

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

    df_tran['Distance'] = step_dist.values

    mean_step = float(step_dist.mean()) if len(step_dist) else 0.0
    n_jumps   = int(is_jump.sum()) if len(step_dist) else 0

    df_tran['Site_name'] = site_name
    df_tran['Transect_number'] = transect_num
    df_tran['Transect_ID'] = transect_id

    out_cols = [
        'Date', 'Time', 'Site_name', 'Transect_number', 'Transect_ID', 'Mode_num', 'Mode',
        'Battery_V', 'Battery_A', 'Battery_W',
        'Battery_mAh_used', 'Battery_Wh_used',
        'Latitude', 'Longitude', 'EKFlat', 'EKFlon',
        'DVLx', 'DVLy', 'DVLlat', 'DVLlon',
        'Altitude', 'Depth', 'Depth_std', 'Depth_Source',
        'Heading', 'Velocity_mps', 'Width', 'Area_m2',
        'Distance', 'NEDz', 'VFR_alt'
    ]
    for c in out_cols:
        if c not in df_tran.columns:
            df_tran[c] = np.nan
    df_tran = df_tran[out_cols]

    csv_filename = f"{_sanitize_filename(transect_id)}.csv"
    csv_full_path = os.path.join(transects_folder, csv_filename)
    df_tran.to_csv(csv_full_path, index=False)

    info = (f"Transect {transect_num} ({window_desc}): {len(df_tran)} rows saved -> "
            f"mean step = {mean_step:.2f} m; jumps > {JUMP_THRESH} m = {n_jumps}")
    if seed_warning:
        info += f" [WARNING: {seed_warning}]"
    return csv_full_path, info


# ============================================================
# ENTRY POINT
# ============================================================
def main():
    args = get_args_via_gui()

    tlog_paths      = args["tlog_paths"]
    site_name       = args["site_name"]
    survey_date     = args["survey_date"]
    station_id      = args["station_id"]
    save_location   = args["save_location"]
    transects       = args["transects"]  # list of {"transect_id": str, "windows": [(start, end), ...]}

    transects_folder = os.path.join(save_location, "transects")
    os.makedirs(transects_folder, exist_ok=True)
    log.info(f"Saving outputs to: {transects_folder}")

    log.info("Fetching NOAA tide data...")
    try:
        tide_seconds_df = fetch_tide_dataframe(survey_date, station_id)
    except Exception as e:
        log.error(f"Failed to fetch NOAA tide data: {e}")
        messagebox.showerror("NOAA tide data error", str(e))
        sys.exit(1)

    log.info(f"Processing {len(tlog_paths)} tlog file(s)...")
    try:
        df_all = process_tlogs(tlog_paths)
    except Exception as e:
        log.error(f"Failed to process tlog file(s): {e}")
        messagebox.showerror("Tlog processing error", str(e))
        sys.exit(1)

    df_all = merge_tide(df_all, tide_seconds_df)

    results = []
    for i, transect in enumerate(transects, start=1):
        out_path, info = export_transect(
            df_all, transect["windows"], i, transect["transect_id"], site_name, transects_folder)
        log.info(info)
        results.append((transect["transect_id"], transect["windows"], out_path, info))

    log.info("Done.")

    saved = [r for r in results if r[2]]
    skipped = [r for r in results if not r[2]]

    summary_lines = [f"Saved {len(saved)} of {len(results)} transect CSV(s) to:", str(transects_folder), ""]
    for transect_id, windows, out_path, info in results:
        window_desc = ", ".join(f"{s}-{e}" for s, e in windows)
        if out_path:
            summary_lines.append(f"{transect_id} ({window_desc}) -> {os.path.basename(out_path)}")
        else:
            summary_lines.append(f"{transect_id} ({window_desc}): SKIPPED (no data)")

    if skipped:
        messagebox.showwarning("Tlog to CSV complete", "\n".join(summary_lines))
    else:
        messagebox.showinfo("Tlog to CSV complete", "\n".join(summary_lines))


if __name__ == "__main__":
    main()
