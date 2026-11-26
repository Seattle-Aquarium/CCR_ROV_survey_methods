"""
Parts of the program that are built and tested but not shown -- "on the shelf".

Some of what this program grew is not used on a survey day any more, and the
space it took on the field laptop's screen is worth more to what is. Rather
than delete it -- every one of these was built for a reason and may be wanted
back, or moved into a post-processing program of its own -- it is taken out
of the window and left in the code, working:

* its modules stay where they are, imported by nothing that is shown;
* its tests keep running, so it cannot quietly rot while it waits;
* bringing it back is one line in the settings file, no code change.

To bring something back, add its key to ``"unshelve"`` in
``%LOCALAPPDATA%\\CCR_ROV\\rov_flight_ops\\settings.json`` and start the program
again, e.g.::

    "unshelve": ["flight_summary", "analyze_transects"]

``SHELF.md`` beside the package says what each one is, where its code lives,
and what replaced it in the window.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Shelved:
    key: str
    title: str
    #: What it was, in a sentence.
    what: str
    #: Where its code is, relative to the package.
    modules: tuple[str, ...]
    #: What took its place in the window, if anything.
    instead: str = ""


SHELVED: dict[str, Shelved] = {s.key: s for s in (
    Shelved("survey_plan", "Navigation: survey plan",
            "Drawing measured lines, rotated boxes and survey grids on the map, "
            "and live guidance along a chosen line or lane.",
            ("gui/navplanpanel.py", "gui/navdraw.py", "nav/plan.py",
             "nav/guidance.py"),
            "The flight numbers (the heads-up display) and the UGPS receivers "
            "head the right-hand column; readiness fills the rest of it."),
    Shelved("flight_summary", "Flight summary tab",
            "Read the day (the flight report PDF) and recording health: damage "
            "checks, repaired copies, and a .BIN as telemetry when an mcap "
            "failed.",
            ("gui/summarypage.py", "gui/healthpage.py", "mcap_health.py"),
            "The flight report is built automatically after a download on "
            "BlueOS logs, and can be built there by hand."),
    Shelved("analyze_transects", "Analyze transects tab",
            "Per-transect CSVs with a map, and a sensor-health report, driven "
            "from the flight that is open.",
            ("gui/transectpage.py",),
            "The 1 Hz transect CSVs are made by BlueOS logs → Download files "
            "(tick “1 Hz transect CSVs”), from the mcaps or tlogs."),
)}


def unshelved(settings: Mapping | None) -> set[str]:
    """The shelved parts the settings file asks to have back."""
    if not settings:
        return set()
    asked = settings.get("unshelve") or []
    if isinstance(asked, str):
        asked = [asked]
    return {str(k).strip() for k in asked if str(k).strip() in SHELVED}


def enabled(settings: Mapping | None, key: str) -> bool:
    """Is this shelved part in the window? Anything not on the shelf is."""
    if key not in SHELVED:
        return True
    return key in unshelved(settings)
