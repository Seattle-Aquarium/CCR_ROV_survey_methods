"""A small gridded 10-m wind field for the wind map.

The NWS gridpoint is a single cell, so for a *map* we sample Open-Meteo on an
n x n lat/lon grid in one multi-location request. (Upgrade path: swap this for
HRRR GRIB via herbie-data for the native 3-km field.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .. import config
from . import geo
from ._http import fetch_json

FORECAST = "https://api.open-meteo.com/v1/forecast"


@dataclass
class WindField:
    lats: list[float]                 # south -> north
    lons: list[float]                 # west -> east
    speed_kt: list[list[float | None]]  # [lat idx][lon idx]
    dir_deg: list[list[float | None]]
    gust_kt: list[list[float | None]]
    valid_time: datetime
    bbox: tuple[float, float, float, float]
    source: str
    stale: bool = False


def get_wind_field(site: config.Site, valid_dt: datetime, tz: str, *,
                   half_lon: float = config.WIND_MAP_HALF_LON,
                   half_lat: float = config.WIND_MAP_HALF_LAT,
                   n: int = config.WIND_MAP_GRID_N,
                   force: bool = False) -> WindField | None:
    zone = ZoneInfo(tz)
    w, s, e, nth = geo.bbox_around(site.lat, site.lon, half_lon, half_lat)
    lats = [s + (nth - s) * k / (n - 1) for k in range(n)]
    lons = [w + (e - w) * k / (n - 1) for k in range(n)]

    pt_lat = [f"{la:.4f}" for la in lats for _ in lons]
    pt_lon = [f"{lo:.4f}" for _ in lats for lo in lons]

    q = urlencode({
        "latitude": ",".join(pt_lat),
        "longitude": ",".join(pt_lon),
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kn",
        "timezone": tz,
        "start_date": valid_dt.date().isoformat(),
        "end_date": valid_dt.date().isoformat(),
    })
    data, meta = fetch_json(f"{FORECAST}?{q}", force=force)
    locs = data if isinstance(data, list) else [data]
    if len(locs) < n * n:
        return None

    spd = [[None] * n for _ in range(n)]
    drc = [[None] * n for _ in range(n)]
    gst = [[None] * n for _ in range(n)]
    hour = valid_dt.astimezone(zone).hour

    for idx, loc in enumerate(locs[: n * n]):
        j, i = divmod(idx, n)            # j = lat index, i = lon index
        h = loc.get("hourly", {})
        times = h.get("time", [])
        k = next((t for t, ts in enumerate(times)
                  if datetime.fromisoformat(ts).hour == hour), None)
        if k is None:
            continue
        spd[j][i] = _g(h, "wind_speed_10m", k)
        drc[j][i] = _g(h, "wind_direction_10m", k)
        gst[j][i] = _g(h, "wind_gusts_10m", k)

    return WindField(
        lats=lats, lons=lons, speed_kt=spd, dir_deg=drc, gust_kt=gst,
        valid_time=valid_dt.astimezone(zone), bbox=(w, s, e, nth),
        source="Open-Meteo 10 m wind (best_match model)", stale=meta.stale,
    )


def _g(h: dict, key: str, i: int):
    arr = h.get(key)
    if not arr or i >= len(arr):
        return None
    return arr[i]
