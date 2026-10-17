"""
The simulator's own flight folder, so nothing it does can land in a real one.

While the simulator is on, the program's flight folder is this one, under
``%LOCALAPPDATA%\\CCR_ROV\\rov_flight_ops\\simulator``. Everything a page
writes into "the flight folder" -- the Navigation tab's session log, a saved
survey plan, a network check -- goes here instead of into the folder the
operator chose, which is put back untouched when the simulator is switched
off. It is emptied each time the simulator starts.

It is seeded with what a flight folder holds mid-survey: the survey plan the
lanes are flown from, and the transects already flown, as the Transects tab
would have them typed in.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from ..nav import plan as P
from ..survey import Site, SurveyPlan, Transect, plan_path
from .world import LANE_EAST, LANE_NORTH, LANE_WEST, ORIGIN, SimWorld

#: The lanes, as a survey grid on the Navigation tab's plan: 40 m by 12 m,
#: lanes east-west at 3 m, the first along the pier flown westward.
GRID_NAME = "Pier 59 slip lanes"


def root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CCR_ROV" / "rov_flight_ops" / "simulator"


def folder_for(world: SimWorld) -> Path:
    day = datetime.fromtimestamp(world.t0_wall)
    return root() / "flights" / f"{day:%Y_%m_%d}_SIM_Pier59"


def prepare(world: SimWorld) -> Path:
    """Empty the simulator's folder and seed it. Returns the flight folder."""
    flights = root() / "flights"
    if flights.exists():
        # Only ever this folder: it is ours, and never anything chosen.
        if not flights.resolve().is_relative_to(root().resolve()):
            raise RuntimeError(f"refusing to empty {flights}: it is not the "
                               f"simulator's own folder")
        shutil.rmtree(flights, ignore_errors=True)
    folder = folder_for(world)
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    survey_plan(world).save(plan_path(folder, for_writing=True))
    nav_plan().save(folder / P.FILENAME)
    (folder / "README.txt").write_text(
        "This is the ROV flight simulator's flight folder, not a real flight.\n"
        "It is emptied every time the simulator is switched on.\n",
        encoding="utf-8")
    return folder


def grid() -> P.Grid:
    width = (LANE_NORTH[-1] - LANE_NORTH[0]) + (LANE_NORTH[1] - LANE_NORTH[0])
    return P.Grid(
        name=GRID_NAME, anchor=P.Anchor(*ORIGIN),
        center=((LANE_WEST + LANE_EAST) / 2, (LANE_NORTH[0] + LANE_NORTH[-1]) / 2),
        length_m=LANE_EAST - LANE_WEST, width_m=width, rotation_deg=90.0,
        spacing_m=LANE_NORTH[1] - LANE_NORTH[0], start_corner=2,
        note="the simulator's survey: four lanes in the slip north of Pier 59")


def nav_plan() -> P.Plan:
    plan = P.Plan(name="Pier 59 slip (simulated)", site="pier59")
    plan.add(grid())
    return plan


def survey_plan(world: SimWorld) -> SurveyPlan:
    """The transects flown so far, one per lane, as typed on the Transects tab."""
    day = datetime.fromtimestamp(world.t0_wall)
    transects = []
    for n, (lane, start, end) in enumerate(world.lane_windows(world.t()), start=1):
        a = datetime.fromtimestamp(world.wall(start))
        b = datetime.fromtimestamp(world.wall(end))
        transects.append(Transect(f"T{n}", f"{a:%H:%M:%S}", f"{b:%H:%M:%S}"))
        del lane
    return SurveyPlan([Site(name="Pier 59 slip (simulated)", project="Simulator",
                            date=f"{day:%Y-%m-%d}", transects=transects)])
