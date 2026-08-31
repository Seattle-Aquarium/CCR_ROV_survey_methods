"""NOAA CO-OPS tide predictions.

The datagetter serves harmonic stations a prediction at any interval, so we
pull a 6-minute curve for the flight day (plus a margin so it is smooth at the
edges) and a separate high/low list to annotate.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from . import geo
from ._http import fetch_json

DATAGETTER = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


@dataclass
class TidePoint:
    time: datetime
    height_ft: float


@dataclass
class TideExtreme:
    time: datetime
    height_ft: float
    kind: str  # "H" or "L"


@dataclass
class TideData:
    station_id: str
    station_name: str
    station_distance_km: float
    datum: str
    curve: list[TidePoint]
    extremes: list[TideExtreme]
    source: str
    stale: bool = False

    def range_ft(self) -> tuple[float, float]:
        hs = [p.height_ft for p in self.curve]
        return (min(hs), max(hs)) if hs else (0.0, 0.0)

    def height_at(self, when: datetime) -> float | None:
        if not self.curve:
            return None
        times = [p.time for p in self.curve]
        i = bisect_left(times, when)
        if i <= 0:
            return self.curve[0].height_ft
        if i >= len(times):
            return self.curve[-1].height_ft
        a, b = self.curve[i - 1], self.curve[i]
        span = (b.time - a.time).total_seconds()
        if span <= 0:
            return a.height_ft
        f = (when - a.time).total_seconds() / span
        return a.height_ft + f * (b.height_ft - a.height_ft)

    def state_at(self, when: datetime) -> str:
        """'ebb', 'flood', or 'slack' from the local slope of the curve."""
        h0 = self.height_at(when - timedelta(minutes=15))
        h1 = self.height_at(when + timedelta(minutes=15))
        if h0 is None or h1 is None:
            return ""
        d = h1 - h0
        if abs(d) < 0.03:
            return "slack"
        return "flood" if d > 0 else "ebb"


def _q(station: str, begin: datetime, end: datetime, interval: str, datum: str) -> str:
    from urllib.parse import urlencode
    params = {
        "product": "predictions",
        "application": "CCR_ROV_flight_plan",
        "begin_date": begin.strftime("%Y%m%d %H:%M"),
        "end_date": end.strftime("%Y%m%d %H:%M"),
        "datum": datum,
        "station": station,
        "time_zone": "lst_ldt",
        "units": "english",
        "interval": interval,
        "format": "json",
    }
    return f"{DATAGETTER}?{urlencode(params)}"


def _parse_dt(s: str, zone: ZoneInfo) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=zone)


def get_tide(site: config.Site, day: date, tz: str, *,
             datum: str = config.TIDE_DATUM, force: bool = False) -> TideData | None:
    st = geo.nearest_tide_station(site.lat, site.lon, harmonic_only=True)
    if st is None:
        return None

    zone = ZoneInfo(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone) - timedelta(
        hours=config.TIDE_CURVE_MARGIN_H)
    end = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(
        hours=24 + config.TIDE_CURVE_MARGIN_H)

    stale = False
    curve: list[TidePoint] = []
    for interval in (str(config.TIDE_INTERVAL_MIN), "60"):
        data, meta = fetch_json(_q(st.id, start, end, interval, datum),
                                ttl=config.CACHE_TTL_STATIC_S, force=force)
        if "predictions" in data:
            curve = [TidePoint(_parse_dt(p["t"], zone), float(p["v"]))
                     for p in data["predictions"]]
            stale = meta.stale
            break

    hilo_start = datetime(day.year, day.month, day.day, tzinfo=zone) - timedelta(days=1)
    hilo_end = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=2)
    data, meta = fetch_json(_q(st.id, hilo_start, hilo_end, "hilo", datum),
                            ttl=config.CACHE_TTL_STATIC_S, force=force)
    extremes = [
        TideExtreme(_parse_dt(p["t"], zone), float(p["v"]), p.get("type", ""))
        for p in data.get("predictions", [])
    ]
    stale = stale or meta.stale

    if not curve:
        return None

    return TideData(
        station_id=st.id,
        station_name=st.name,
        station_distance_km=st.distance_km,
        datum=datum,
        curve=curve,
        extremes=extremes,
        source=f"NOAA CO-OPS predicted tide, {st.name} ({st.id}), {datum}",
        stale=stale,
    )
