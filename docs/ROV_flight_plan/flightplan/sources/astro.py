"""Sunrise / sunset / civil twilight for the tide figure's day-night shading.

`astral` computes these from lat/lon/date with no network, so this works
offline and for any date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from astral import LocationInfo, sun


@dataclass
class SunTimes:
    dawn: datetime | None      # civil dawn
    sunrise: datetime | None
    noon: datetime | None
    sunset: datetime | None
    dusk: datetime | None      # civil dusk
    polar_note: str = ""       # set when the sun does not rise/set that day


def sun_times(lat: float, lon: float, day: date, tz: str) -> SunTimes:
    zone = ZoneInfo(tz)
    loc = LocationInfo(latitude=lat, longitude=lon, timezone=tz)
    try:
        s = sun.sun(loc.observer, date=day, tzinfo=zone)
        return SunTimes(
            dawn=s["dawn"], sunrise=s["sunrise"], noon=s["noon"],
            sunset=s["sunset"], dusk=s["dusk"],
        )
    except ValueError as e:
        # High latitude in mid-summer / mid-winter: sun stays up or down.
        return SunTimes(None, None, None, None, None, polar_note=str(e))
