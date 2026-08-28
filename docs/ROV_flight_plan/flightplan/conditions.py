"""Gather every data source into one ``Conditions`` object.

Each fetch is isolated: a source that fails or has no data for the date leaves
its field ``None`` and adds a line to ``warnings`` rather than sinking the run.
Figures and the LaTeX template read only from here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .sources import alerts as alerts_mod
from .sources import astro, marine, tides, waves, weather, windfield
from .sources.alerts import Alert
from .sources.astro import SunTimes
from .sources.marine import MarineNarrative
from .sources.tides import TideData
from .sources.waves import WaveData
from .sources.weather import WeatherData
from .sources.windfield import WindField


@dataclass
class Window:
    start: datetime
    end: datetime

    @property
    def mid(self) -> datetime:
        return self.start + (self.end - self.start) / 2

    def label(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(text: str, day: date, tz: str) -> Window:
    """'08:00-13:00' -> aware datetimes on ``day`` (end rolls to next day if <= start)."""
    zone = ZoneInfo(tz)
    a, _, b = text.replace("–", "-").partition("-")
    sh, sm = (int(x) for x in a.strip().split(":"))
    eh, em = (int(x) for x in b.strip().split(":"))
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=zone)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=zone)
    if end <= start:
        end += timedelta(days=1)
    return Window(start, end)


@dataclass
class Conditions:
    site: config.Site
    day: date
    tz: str
    float_window: Window
    flight_window: Window
    tide: TideData | None = None
    sun: SunTimes | None = None
    weather: WeatherData | None = None
    waves: WaveData | None = None
    wind_field: WindField | None = None
    marine: MarineNarrative | None = None
    alerts: list[Alert] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    stale: bool = False

    # -- convenience for the summary table --------------------------------
    @property
    def serious_alerts(self) -> list[Alert]:
        return [a for a in self.alerts if a.serious]

    def wind_at_flight(self):
        if not self.weather:
            return None
        return self.weather.at(self.flight_window.mid)

    def waves_at_flight(self):
        if not self.waves:
            return None
        return self.waves.at(self.flight_window.mid)

    def tide_at(self, when: datetime):
        return self.tide.height_at(when) if self.tide else None


def build_conditions(site: config.Site, day: date, *,
                     float_window: str | Window,
                     flight_window: str | Window,
                     tz: str = config.TIMEZONE,
                     force: bool = False) -> Conditions:
    fw = float_window if isinstance(float_window, Window) else parse_window(float_window, day, tz)
    gw = flight_window if isinstance(flight_window, Window) else parse_window(flight_window, day, tz)

    c = Conditions(site=site, day=day, tz=tz, float_window=fw, flight_window=gw)

    def step(name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            c.warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    c.tide = step("tide", lambda: tides.get_tide(site, day, tz, force=force))
    if c.tide is None:
        c.warnings.append("tide: no harmonic station within range / no curve returned")

    c.sun = step("sun", lambda: astro.sun_times(site.lat, site.lon, day, tz))

    c.weather = step("weather", lambda: weather.get_weather(site, day, tz, force=force))
    if c.weather is None:
        c.warnings.append("weather: no NWS or Open-Meteo data for this date")

    c.waves = step("waves", lambda: waves.get_waves(site, day, tz, force=force))
    if c.waves is None:
        c.warnings.append("waves: Open-Meteo Marine returned nothing")

    c.wind_field = step(
        "wind_field",
        lambda: windfield.get_wind_field(site, gw.mid, tz, force=force))

    c.marine = step("marine", lambda: marine.get_marine_narrative(
        site.lat, site.lon, force=force))

    c.alerts = step("alerts", lambda: alerts_mod.get_alerts(
        site.lat, site.lon, tz, force=force)) or []

    c.stale = any(getattr(x, "stale", False)
                  for x in (c.tide, c.weather, c.waves, c.wind_field) if x)
    return c
