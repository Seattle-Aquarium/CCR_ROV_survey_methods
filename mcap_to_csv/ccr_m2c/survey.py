"""
Reading a survey plan, so transects need not be retyped.

The plan is the same JSON the UTC compositing tool already uses, and the same
file works in both without editing:

    {
      "sites": [
        {
          "name": "Centennial_Park",
          "project": "testing",
          "date": "2026-08-26",
          "transects": [
            {"name": "T1", "start_tc": "12:19:57", "end_tc": "12:28:42"}
          ]
        }
      ],
      "timezone": "America/Los_Angeles"
    }

``start_tc``/``end_tc`` are local wall-clock times, which is what the transect
windows in this tool are too, so they carry straight across.

A plan may hold several sites. Each becomes its own output folder, because a
site is the unit the map is drawn for -- transects from two different places on
one map would be two specks at opposite ends of an empty ocean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .pipeline import TransectSpec

#: What UTC defaults to, and what the Seattle Aquarium surveys run in.
DEFAULT_TIMEZONE = "America/Los_Angeles"

#: This tool converts local times against US/Pacific. A plan recorded in another
#: zone would silently shift every window, so it is refused rather than guessed.
_EQUIVALENT_ZONES = {"America/Los_Angeles", "US/Pacific", "America/Vancouver"}


@dataclass
class PlannedSite:
    name: str
    project: str
    date: str                       # YYYY-MM-DD, as written in the plan
    transects: list[TransectSpec] = field(default_factory=list)

    @property
    def survey_date(self) -> str:
        """The plan's date as the YYYYMMDD this tool uses for the tide lookup."""
        return datetime.strptime(self.date, "%Y-%m-%d").strftime("%Y%m%d")

    def __str__(self) -> str:
        return (f"{self.name} ({self.project}, {self.date}): "
                f"{len(self.transects)} transect(s)")


@dataclass
class SurveyPlan:
    sites: list[PlannedSite] = field(default_factory=list)
    timezone: str = DEFAULT_TIMEZONE
    warnings: list[str] = field(default_factory=list)


def _parse_time(value: str, where: str) -> str:
    try:
        datetime.strptime(value, "%H:%M:%S")
    except (TypeError, ValueError):
        raise ValueError(f"{where}: time must be HH:MM:SS, got {value!r}") from None
    return value


def load_plan(path: Path | str) -> SurveyPlan:
    """Read a survey plan, or say clearly why it cannot be used."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        raise ValueError(f"{path.name} is not valid JSON: {ex}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected an object with a 'sites' list")

    tz = raw.get("timezone", DEFAULT_TIMEZONE)
    plan = SurveyPlan(timezone=tz)
    if tz not in _EQUIVALENT_ZONES:
        plan.warnings.append(
            f"plan timezone is {tz!r}, but transect times are read as US/Pacific; "
            "the windows will be wrong unless the recording is Pacific too"
        )

    sites = raw.get("sites")
    if not isinstance(sites, list) or not sites:
        raise ValueError(f"{path.name}: no 'sites' in the plan")

    for i, s in enumerate(sites, start=1):
        name = str(s.get("name", "")).strip()
        if not name:
            raise ValueError(f"{path.name}: site {i} has no name")
        date = str(s.get("date", "")).strip()
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"{path.name}: site {name!r} has date {date!r}, expected YYYY-MM-DD"
            ) from None

        site = PlannedSite(name=name, project=str(s.get("project", "")).strip(), date=date)
        for t in s.get("transects") or []:
            tname = str(t.get("name", "")).strip()
            if not tname:
                raise ValueError(f"{path.name}: a transect at {name!r} has no name")
            start = _parse_time(t.get("start_tc"), f"{name}/{tname}")
            end = _parse_time(t.get("end_tc"), f"{name}/{tname}")
            if start >= end:
                raise ValueError(
                    f"{path.name}: {name}/{tname} starts at {start} and ends at {end}")
            site.transects.append(TransectSpec(tname, [(start, end)]))

        if not site.transects:
            plan.warnings.append(f"site {name!r} has no transects; skipped")
            continue
        plan.sites.append(site)

    if not plan.sites:
        raise ValueError(f"{path.name}: the plan has no transects")
    return plan


def transect_ids(site: PlannedSite, *, prefix_site: bool = False) -> list[str]:
    """Output names for a site's transects.

    A plan names transects ``T1``, ``T2`` -- unique within their site but not
    across a season's worth of them. ``prefix_site`` produces
    ``Centennial_Park_T1`` instead, which is what the CSVs want when several
    sites end up in one folder.
    """
    return [f"{site.name}_{t.transect_id}" if prefix_site else t.transect_id
            for t in site.transects]
