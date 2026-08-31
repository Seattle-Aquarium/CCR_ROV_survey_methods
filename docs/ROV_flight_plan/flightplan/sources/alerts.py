"""Active NWS watches / warnings / advisories for the survey point.

`api.weather.gov/alerts/active?point=` returns every alert whose zone or polygon
contains the point -- marine (Small Craft Advisory, Gale Warning, Special Marine
Warning) and land (Severe Thunderstorm, Dense Fog, ...) alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from ._http import fetch_json

ACTIVE = "https://api.weather.gov/alerts/active?point={lat},{lon}"

_SEV_RANK = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}


@dataclass
class Alert:
    event: str
    severity: str
    urgency: str
    headline: str
    description: str
    instruction: str
    onset: datetime | None
    ends: datetime | None
    sender: str

    @property
    def serious(self) -> bool:
        e = self.event.lower()
        return any(k in e for k in config.SERIOUS_ALERT_KEYWORDS)


def _dt(s: str | None, zone: ZoneInfo) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(zone)
    except ValueError:
        return None


def get_alerts(lat: float, lon: float, tz: str, *, force: bool = False) -> list[Alert]:
    zone = ZoneInfo(tz)
    try:
        data, _ = fetch_json(ACTIVE.format(lat=lat, lon=lon), ttl=1800, force=force)
    except Exception:
        return []
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        out.append(Alert(
            event=p.get("event", ""),
            severity=p.get("severity", "Unknown"),
            urgency=p.get("urgency", ""),
            headline=p.get("headline", ""),
            description=(p.get("description") or "").strip(),
            instruction=(p.get("instruction") or "").strip(),
            onset=_dt(p.get("onset") or p.get("effective"), zone),
            ends=_dt(p.get("ends") or p.get("expires"), zone),
            sender=p.get("senderName", ""),
        ))
    out.sort(key=lambda a: (_SEV_RANK.get(a.severity, 5), a.event))
    return out
