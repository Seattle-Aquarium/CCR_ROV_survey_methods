"""Topside weather and 10-m wind at the survey point.

Primary: NWS api.weather.gov gridpoint (official, but forecast-only, ~now..+7d,
and some inland/Sound cells carry no marine layers). Fallback: Open-Meteo,
which also covers recent past dates and protected water.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from .. import config
from ._http import fetch_json
from ._timeseries import expand_layer, on_date

NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"
NWS_ZONES_MARINE = "https://api.weather.gov/zones?type=marine&point={lat},{lon}"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


@dataclass
class HourWx:
    time: datetime
    temp_f: float | None = None
    sky_pct: float | None = None
    pop_pct: float | None = None
    precip_mm: float | None = None
    wind_kt: float | None = None
    wind_dir: float | None = None
    gust_kt: float | None = None


@dataclass
class WeatherData:
    hours: list[HourWx]
    source: str
    stale: bool = False
    marine_zone_id: str = ""
    marine_zone_name: str = ""

    # -- summaries over the on-water part of the day ------------------------
    def _slice(self, lo: int, hi: int) -> list[HourWx]:
        return [h for h in self.hours if lo <= h.time.hour <= hi] or self.hours

    def temp_range_f(self, lo: int = 6, hi: int = 20) -> tuple[float, float] | None:
        v = [h.temp_f for h in self._slice(lo, hi) if h.temp_f is not None]
        return (min(v), max(v)) if v else None

    def max_pop(self) -> float | None:
        v = [h.pop_pct for h in self.hours if h.pop_pct is not None]
        return max(v) if v else None

    def total_precip_mm(self) -> float | None:
        v = [h.precip_mm for h in self.hours if h.precip_mm is not None]
        return sum(v) if v else None

    def max_gust_kt(self) -> float | None:
        v = [h.gust_kt for h in self.hours if h.gust_kt is not None]
        return max(v) if v else None

    def mean_sky_pct(self, lo: int = 6, hi: int = 20) -> float | None:
        v = [h.sky_pct for h in self._slice(lo, hi) if h.sky_pct is not None]
        return mean(v) if v else None

    def sky_summary(self) -> str:
        s = self.mean_sky_pct()
        if s is None:
            return "n/a"
        return ("clear" if s < 12 else "mostly clear" if s < 30
                else "partly cloudy" if s < 60 else "mostly cloudy" if s < 88
                else "overcast")

    def at(self, when: datetime) -> HourWx | None:
        if not self.hours:
            return None
        times = [h.time for h in self.hours]
        i = min(range(len(times)), key=lambda k: abs((times[k] - when).total_seconds()))
        return self.hours[i]


# ---------------------------------------------------------------------------
#  NWS
# ---------------------------------------------------------------------------

def _nws_marine_zone(lat: float, lon: float, force: bool) -> tuple[str, str]:
    """(zone id, name). The plain-language forecast comes from sources.marine."""
    try:
        data, _ = fetch_json(NWS_ZONES_MARINE.format(lat=lat, lon=lon),
                             ttl=config.CACHE_TTL_STATIC_S, force=force)
        feats = data.get("features") or []
        if not feats:
            return "", ""
        p = feats[0]["properties"]
        return p.get("id", ""), p.get("name", "")
    except Exception:
        return "", ""


def _from_nws(site: config.Site, day: date, zone: ZoneInfo, force: bool) -> WeatherData | None:
    pts, _ = fetch_json(NWS_POINTS.format(lat=site.lat, lon=site.lon),
                        ttl=config.CACHE_TTL_STATIC_S, force=force)
    grid_url = pts["properties"]["forecastGridData"]
    grid, meta = fetch_json(grid_url, force=force)
    gp = grid["properties"]

    def series(name):
        return expand_layer(gp.get(name, {}), zone)

    temp = on_date(series("temperature"), day, zone)
    sky = on_date(series("skyCover"), day, zone)
    pop = on_date(series("probabilityOfPrecipitation"), day, zone)
    qpf = on_date(series("quantitativePrecipitation"), day, zone)
    wspd = on_date(series("windSpeed"), day, zone)
    wdir = on_date(series("windDirection"), day, zone)
    wgst = on_date(series("windGust"), day, zone)

    if len(temp) < 6 and len(wspd) < 6:
        return None  # date outside the NWS forecast horizon

    hours = []
    for h in range(24):
        t = datetime(day.year, day.month, day.day, h, tzinfo=zone)
        hours.append(HourWx(
            time=t, temp_f=temp.get(h), sky_pct=sky.get(h), pop_pct=pop.get(h),
            precip_mm=qpf.get(h), wind_kt=wspd.get(h), wind_dir=wdir.get(h),
            gust_kt=wgst.get(h),
        ))

    zid, zname = _nws_marine_zone(site.lat, site.lon, force)
    return WeatherData(
        hours=hours,
        source=f"NWS api.weather.gov gridpoint {pts['properties'].get('gridId','')} "
               f"{pts['properties'].get('gridX','')},{pts['properties'].get('gridY','')}",
        stale=meta.stale, marine_zone_id=zid, marine_zone_name=zname,
    )


# ---------------------------------------------------------------------------
#  Open-Meteo
# ---------------------------------------------------------------------------

def _from_open_meteo(site: config.Site, day: date, zone: ZoneInfo, tz: str,
                     force: bool) -> WeatherData | None:
    from urllib.parse import urlencode
    q = urlencode({
        "latitude": site.lat, "longitude": site.lon,
        "hourly": ",".join((
            "temperature_2m", "cloud_cover", "precipitation",
            "precipitation_probability", "wind_speed_10m",
            "wind_direction_10m", "wind_gusts_10m")),
        "wind_speed_unit": "kn", "temperature_unit": "fahrenheit",
        "timezone": tz, "start_date": day.isoformat(), "end_date": day.isoformat(),
    })
    data, meta = fetch_json(f"{OPEN_METEO}?{q}", force=force)
    h = data.get("hourly")
    if not h or not h.get("time"):
        return None
    hours = []
    for i, ts in enumerate(h["time"]):
        t = datetime.fromisoformat(ts).replace(tzinfo=zone)
        hours.append(HourWx(
            time=t,
            temp_f=_g(h, "temperature_2m", i),
            sky_pct=_g(h, "cloud_cover", i),
            pop_pct=_g(h, "precipitation_probability", i),
            precip_mm=_g(h, "precipitation", i),
            wind_kt=_g(h, "wind_speed_10m", i),
            wind_dir=_g(h, "wind_direction_10m", i),
            gust_kt=_g(h, "wind_gusts_10m", i),
        ))
    zid, zname = _nws_marine_zone(site.lat, site.lon, force)
    return WeatherData(
        hours=hours, source="Open-Meteo (best_match model)", stale=meta.stale,
        marine_zone_id=zid, marine_zone_name=zname,
    )


def _g(h: dict, key: str, i: int):
    arr = h.get(key)
    if not arr or i >= len(arr):
        return None
    return arr[i]


# ---------------------------------------------------------------------------

def get_weather(site: config.Site, day: date, tz: str, *,
                force: bool = False, prefer: str = "auto") -> WeatherData | None:
    zone = ZoneInfo(tz)
    if prefer in ("auto", "nws"):
        try:
            wd = _from_nws(site, day, zone, force)
            if wd is not None:
                return wd
        except Exception:
            if prefer == "nws":
                raise
    return _from_open_meteo(site, day, zone, tz, force)
