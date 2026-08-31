"""Distance, bounding boxes, and nearest-station lookups against the vendored
CO-OPS and NDBC indexes."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache

from .. import config

EARTH_R_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def bbox_around(lat: float, lon: float, half_lon: float,
                half_lat: float | None = None) -> tuple[float, float, float, float]:
    """(west, south, east, north), latitude clamped to the sphere."""
    hl = half_lon if half_lat is None else half_lat
    return (lon - half_lon, max(-89.9, lat - hl),
            lon + half_lon, min(89.9, lat + hl))


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float
    kind: str          # tide: "R"/"S"   buoy: NDBC type string
    distance_km: float


@lru_cache(maxsize=4)
def _load(path_name: str) -> tuple[dict, ...]:
    path = config.DATA_DIR / path_name
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def nearest_tide_station(lat: float, lon: float, *, harmonic_only: bool = True,
                         max_km: float | None = None) -> Station | None:
    """Closest CO-OPS tide-prediction station.

    Harmonic ("R") stations return a full sub-hourly curve; subordinate ("S")
    stations only high/low. Default to harmonic so the 24-h curve is real.
    """
    cap = config.MAX_TIDE_STATION_KM if max_km is None else max_km
    best: Station | None = None
    for s in _load("tide_stations.json"):
        if harmonic_only and s["type"] != "R":
            continue
        d = haversine_km(lat, lon, s["lat"], s["lon"])
        if d > cap:
            continue
        if best is None or d < best.distance_km:
            best = Station(s["id"], s["name"], s["lat"], s["lon"], s["type"], d)
    return best


# NDBC "type" strings that actually carry a wave spectrum (excludes profiling /
# water-quality / ATON "buoys", which report no WVHT).
_WAVE_BUOY_HINTS = ("discus", "foam", "waverider", "spotter", "dwr")


def nearest_wave_buoy(lat: float, lon: float, *,
                      max_km: float | None = None) -> Station | None:
    cap = config.MAX_BUOY_KM if max_km is None else max_km
    best: Station | None = None
    for s in _load("ndbc_stations.json"):
        t = (s.get("type") or "").lower()
        if not any(h in t for h in _WAVE_BUOY_HINTS):
            continue
        d = haversine_km(lat, lon, s["lat"], s["lon"])
        if d > cap:
            continue
        if best is None or d < best.distance_km:
            best = Station(s["id"], s["name"] or s["id"].upper(),
                           s["lat"], s["lon"], s.get("type") or "", d)
    return best


def compass_point(deg: float) -> str:
    """16-point compass label for a bearing in degrees."""
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]
