"""Swell and sea state.

Primary forecast: Open-Meteo Marine (global wave model; unlike the NWS
gridpoint it also resolves protected water like Puget Sound). Cross-check:
NWS gridpoint swell layers where the cell carries them (outer coast).
Observed: the nearest NDBC wave buoy's latest report.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .. import config
from . import geo
from ._http import fetch, fetch_json
from ._timeseries import expand_layer, on_date

MARINE = "https://marine-api.open-meteo.com/v1/marine"
NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"
NDBC_RT = "https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt"
NDBC_SPEC = "https://www.ndbc.noaa.gov/data/realtime2/{sid}.spec"

M_TO_FT = 3.2808399
MS_TO_KT = 1.9438445


@dataclass
class WaveHour:
    time: datetime
    wave_ft: float | None = None          # significant height, combined sea
    swell_ft: float | None = None
    swell_period_s: float | None = None
    swell_dir_deg: float | None = None
    wind_wave_ft: float | None = None


@dataclass
class BuoyObs:
    station_id: str
    name: str
    distance_km: float
    time: datetime | None = None
    wvht_ft: float | None = None
    dom_period_s: float | None = None
    mean_dir_deg: float | None = None
    wind_kt: float | None = None
    gust_kt: float | None = None
    swell_ft: float | None = None
    swell_period_s: float | None = None
    swell_dir_deg: float | None = None
    wind_wave_ft: float | None = None


@dataclass
class WaveData:
    hours: list[WaveHour]
    source: str
    stale: bool = False
    buoy: BuoyObs | None = None
    nws_swell_available: bool = False

    def max_wave_ft(self) -> float | None:
        v = [h.wave_ft for h in self.hours if h.wave_ft is not None]
        return max(v) if v else None

    def at(self, when: datetime) -> WaveHour | None:
        if not self.hours:
            return None
        return min(self.hours, key=lambda h: abs((h.time - when).total_seconds()))

    def negligible(self, threshold_ft: float = 0.7) -> bool:
        m = self.max_wave_ft()
        return m is not None and m < threshold_ft


# ---------------------------------------------------------------------------

def _marine_forecast(site: config.Site, day: date, zone: ZoneInfo, tz: str,
                     force: bool) -> tuple[list[WaveHour], bool]:
    from urllib.parse import urlencode
    q = urlencode({
        "latitude": site.lat, "longitude": site.lon,
        "hourly": ",".join((
            "wave_height", "wave_period", "wave_direction",
            "swell_wave_height", "swell_wave_period", "swell_wave_direction",
            "wind_wave_height")),
        "timezone": tz, "start_date": day.isoformat(), "end_date": day.isoformat(),
    })
    data, meta = fetch_json(f"{MARINE}?{q}", force=force)
    h = data.get("hourly")
    if not h or not h.get("time"):
        return [], meta.stale
    out = []
    for i, ts in enumerate(h["time"]):
        out.append(WaveHour(
            time=datetime.fromisoformat(ts).replace(tzinfo=zone),
            wave_ft=_ft(h, "wave_height", i),
            swell_ft=_ft(h, "swell_wave_height", i),
            swell_period_s=_get(h, "swell_wave_period", i),
            swell_dir_deg=_get(h, "swell_wave_direction", i),
            wind_wave_ft=_ft(h, "wind_wave_height", i),
        ))
    return out, meta.stale


def _ft(h, key, i):
    v = _get(h, key, i)
    return None if v is None else v * M_TO_FT


def _get(h, key, i):
    arr = h.get(key)
    if not arr or i >= len(arr) or arr[i] is None:
        return None
    return arr[i]


def _nws_swell_present(site: config.Site, day: date, zone: ZoneInfo, force: bool) -> bool:
    """True only if the NWS cell carries a *usable* swell series for the day --
    outer-coast cells often expose the layer with a single zero-valued entry."""
    try:
        pts, _ = fetch_json(NWS_POINTS.format(lat=site.lat, lon=site.lon),
                            ttl=config.CACHE_TTL_STATIC_S, force=force)
        grid, _ = fetch_json(pts["properties"]["forecastGridData"], force=force)
        gp = grid["properties"]
        got = on_date(expand_layer(gp.get("primarySwellHeight", {}), zone), day, zone)
        return len(got) >= 3 and max(got.values()) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
#  NDBC observed
# ---------------------------------------------------------------------------

def _parse_ndbc_realtime(text: str, zone: ZoneInfo) -> dict | None:
    """First data row with a real WVHT, as a dict of floats."""
    cols = None
    for line in text.splitlines():
        if line.startswith("#YY"):            # column names (the "#yr ..." line is units)
            cols = line.lstrip("#").split()
            continue
        if line.startswith("#") or not line.strip() or cols is None:
            continue
        vals = line.split()
        if len(vals) < len(cols):
            continue
        row = dict(zip(cols, vals))
        if row.get("WVHT", "MM") == "MM":
            continue
        try:
            t = datetime(int(row["YY"]), int(row["MM"]), int(row["DD"]),
                         int(row["hh"]), int(row["mm"]), tzinfo=ZoneInfo("UTC")
                         ).astimezone(zone)
        except (ValueError, KeyError):
            t = None
        return {"_time": t, **{k: _num(v) for k, v in row.items()}}
    return None


def _parse_ndbc_spec(text: str, zone: ZoneInfo) -> dict | None:
    cols = None
    for line in text.splitlines():
        if line.startswith("#YY"):
            cols = line.lstrip("#").split()
            continue
        if line.startswith("#") or not line.strip() or cols is None:
            continue
        vals = line.split()
        if len(vals) < len(cols):
            continue
        row = dict(zip(cols, vals))
        if row.get("SwH", "MM") == "MM":
            continue
        return row                       # raw strings; SwD/WWD are compass points
    return None


def _num(v: str):
    try:
        return float(v)
    except ValueError:
        return None


def get_buoy(site: config.Site, tz: str, *, force: bool = False) -> BuoyObs | None:
    st = geo.nearest_wave_buoy(site.lat, site.lon)
    if st is None:
        return None
    zone = ZoneInfo(tz)
    try:
        rt = fetch(NDBC_RT.format(sid=st.id), ttl=1800, force=force).text
    except Exception:
        return BuoyObs(st.id, st.name, st.distance_km)
    row = _parse_ndbc_realtime(rt, zone)
    if row is None:
        return BuoyObs(st.id, st.name, st.distance_km)

    obs = BuoyObs(
        station_id=st.id, name=st.name, distance_km=st.distance_km,
        time=row.get("_time"),
        wvht_ft=_mul(row.get("WVHT"), M_TO_FT),
        dom_period_s=row.get("DPD"),
        mean_dir_deg=row.get("MWD"),
        wind_kt=_mul(row.get("WSPD"), MS_TO_KT),
        gust_kt=_mul(row.get("GST"), MS_TO_KT),
    )
    try:
        spec = _parse_ndbc_spec(fetch(NDBC_SPEC.format(sid=st.id), ttl=1800,
                                      force=force).text, zone)
        if spec:
            obs.swell_ft = _mul(_num(spec.get("SwH")), M_TO_FT)
            obs.swell_period_s = _num(spec.get("SwP"))
            obs.swell_dir_deg = _compass_to_deg(spec.get("SwD"))
            obs.wind_wave_ft = _mul(_num(spec.get("WWH")), M_TO_FT)
    except Exception:
        pass
    return obs


def _mul(v, k):
    return None if v is None else v * k


_COMPASS = {p: i * 22.5 for i, p in enumerate(
    ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
     "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"])}


def _compass_to_deg(v):
    """NDBC .spec direction is a 16-point compass string ('S', 'WNW', ...)."""
    if isinstance(v, (int, float)):
        return v
    return _COMPASS.get(str(v).strip().upper()) if v is not None else None


# ---------------------------------------------------------------------------

def get_waves(site: config.Site, day: date, tz: str, *,
              force: bool = False) -> WaveData | None:
    zone = ZoneInfo(tz)
    hours, stale = _marine_forecast(site, day, zone, tz, force)
    if not hours:
        return None
    return WaveData(
        hours=hours,
        source="Open-Meteo Marine (wave model)",
        stale=stale,
        buoy=get_buoy(site, tz, force=force),
        nws_swell_available=_nws_swell_present(site, day, zone, force),
    )
