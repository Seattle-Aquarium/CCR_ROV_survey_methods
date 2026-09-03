"""
NOAA water level, for tide-standardised depth.

``Depth_std`` puts every transect on a common vertical datum (MLLW) so depths
taken on different days at different tide stages are comparable:

    Depth_std = -Altitude + Depth + water_level

NOAA publishes one-minute water levels; a natural cubic spline interpolates
those onto the one-second grid the telemetry uses, which is smooth enough that
the tide contributes no visible steps to a depth profile.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import requests
from scipy.interpolate import CubicSpline

#: NOAA tide stations offered in the GUI: (display label, station id).
STATIONS = [
    ("Elliott Bay (9447130)", "9447130"),
    ("Friday Harbor (9449880)", "9449880"),
    ("Neah Bay (9443090)", "9443090"),
]

_API = (
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
    "begin_date={date}&end_date={date}&station={station}"
    "&product=one_minute_water_level&datum=MLLW&time_zone=lst&units=metric&format=json"
)


def get_tide_data(api_request: str, timeout: float = 30.0) -> dict:
    response = requests.get(api_request, timeout=timeout)
    response.raise_for_status()
    return response.json()


def expand_noaa_to_seconds(df: pd.DataFrame) -> pd.DataFrame:
    """One-minute observations -> one-second series, by cubic spline."""
    times = pd.to_datetime(df["date"] + " " + df["time"])
    values = df["water_level"].to_numpy(dtype=float)

    keep = np.isfinite(values)
    times, values = times[keep], values[keep]
    if len(values) < 4:
        raise ValueError("NOAA returned too few water-level readings to interpolate")

    t0 = times.iloc[0]
    t_seconds = (times - t0).dt.total_seconds().to_numpy()

    spline = CubicSpline(t_seconds, values, bc_type="natural")
    full_seconds = np.arange(int(t_seconds[0]), int(t_seconds[-1]) + 1)
    return pd.DataFrame({
        "Datetime": t0 + pd.to_timedelta(full_seconds, unit="s"),
        "water_level": spline(full_seconds),
    })


def fetch_tide_dataframe(survey_date: str, station_id: str) -> pd.DataFrame:
    """One-second water level for ``survey_date`` (YYYYMMDD) at ``station_id``."""
    tide_data = get_tide_data(_API.format(date=survey_date, station=station_id))
    if "data" not in tide_data:
        raise ValueError(f"NOAA tide API returned no data: {tide_data}")

    wl = pd.DataFrame(tide_data["data"]).rename(
        columns={"t": "datetime", "v": "water_level"})
    wl["datetime"] = pd.to_datetime(wl["datetime"])
    wl["water_level"] = pd.to_numeric(wl["water_level"], errors="coerce")
    wl["date"] = wl["datetime"].dt.date.astype(str)
    wl["time"] = wl["datetime"].dt.strftime("%H:%M")
    return expand_noaa_to_seconds(wl)


def merge_tide(df_all: pd.DataFrame, tide_seconds_df: pd.DataFrame) -> pd.DataFrame:
    """Attach ``Depth_std`` to a per-second telemetry frame.

    NOAA reports in station local time, which is what the telemetry's
    ``Date``/``Time`` columns are in, so the two join directly.
    """
    df_all = df_all.copy()
    df_all["_dt"] = pd.to_datetime(df_all["Date"] + " " + df_all["Time"])
    merged = pd.merge_asof(
        df_all.sort_values("_dt"),
        tide_seconds_df.sort_values("Datetime").rename(columns={"Datetime": "_dt"}),
        on="_dt",
        direction="nearest",
        tolerance=pd.Timedelta("1min"),
    ).sort_values("_dt")

    merged["Depth_std"] = (
        -pd.to_numeric(merged["Altitude"], errors="coerce")
        + pd.to_numeric(merged["Depth"], errors="coerce")
        + pd.to_numeric(merged["water_level"], errors="coerce")
    )
    return merged.drop(columns=["_dt", "water_level"]).reset_index(drop=True)


def add_empty_tide(df_all: pd.DataFrame) -> pd.DataFrame:
    """``Depth_std`` as an empty column, when the tide lookup was skipped.

    Keeps the CSV schema identical whether or not the laptop had a network
    connection in the field.
    """
    df_all = df_all.copy()
    df_all["Depth_std"] = np.nan
    return df_all
