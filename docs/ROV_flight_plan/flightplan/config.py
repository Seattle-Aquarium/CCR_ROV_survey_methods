"""Defaults a user might reasonably want to change, in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The whole supported geography is Pacific time; keep it simple and explicit.
# (A future multi-timezone build can swap this for a lat/lon lookup.)
TIMEZONE = "America/Los_Angeles"

# Home base: Seattle Aquarium, Pier 59, Elliott Bay. The GUI map opens here.
HOME_LAT = 47.6074
HOME_LON = -122.3435
HOME_NAME = "Seattle Aquarium (Pier 59)"

# How the day is split, unless overridden:
#   float  = time on the water, bow to stern      (e.g. 08:00-13:00)
#   flight = the ROV actually in the water surveying (e.g. 10:00-11:00)
DEFAULT_FLOAT = ("08:00", "13:00")
DEFAULT_FLIGHT = ("10:00", "11:00")

# Tide predictions
TIDE_DATUM = "MLLW"                 # what boaters and divers plan against
TIDE_INTERVAL_MIN = 6              # sub-hourly curve resolution
TIDE_CURVE_MARGIN_H = 6           # fetch a margin each side so the curve is smooth at 00:00 / 24:00

# Wind map domain: half-width / half-height in degrees around the site. Wider
# than tall so the figure sits landscape across the page. The GUI will let the
# user widen it.
WIND_MAP_HALF_LON = 0.62
WIND_MAP_HALF_LAT = 0.42
WIND_MAP_GRID_N = 13               # sampling-grid points per axis

# Nearest-feature search caps
MAX_TIDE_STATION_KM = 120.0
MAX_BUOY_KM = 160.0

# Alerts we call out on the figures and in a banner. Everything active is
# listed; these get the red treatment.
SERIOUS_ALERT_KEYWORDS = (
    "small craft", "gale", "storm warning", "hurricane force",
    "special marine", "hazardous seas", "tsunami", "thunderstorm",
    "tornado", "ashfall", "freezing spray",
)

# HTTP
USER_AGENT = (
    "CCR-ROV-flight-plan/0.1 "
    "(github.com/Seattle-Aquarium/CCR_ROV_survey_methods; z.randell@seattleaquarium.org)"
)
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

# On-disk response cache -- outside any synced folder, per the GUI guide.
CACHE_DIR = Path(
    os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home()
) / "CCR_ROV_flightplan_cache"
CACHE_TTL_FORECAST_S = 60 * 60      # forecasts refresh hourly
CACHE_TTL_STATIC_S = 60 * 60 * 24 * 30  # station metadata etc.

# Package data
PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
FIG_ASSET_DIR = PKG_DIR / "figures" / "assets"
LATEX_DIR = PKG_DIR / "latex"


@dataclass
class Site:
    """Where the ROV goes in the water."""

    lat: float
    lon: float
    name: str = ""

    def __post_init__(self) -> None:
        if not (-90 <= self.lat <= 90) or not (-180 <= self.lon <= 180):
            raise ValueError(f"lat/lon out of range: {self.lat}, {self.lon}")
        if not self.name:
            self.name = f"{self.lat:.4f}, {self.lon:.4f}"


@dataclass
class Contacts:
    """Contact tables carried straight into the PDF. Filled from a TOML/JSON
    file later; the defaults below are the ones in the current Word plan."""

    aquarium: list[dict] = field(default_factory=list)
    collaborators: list[dict] = field(default_factory=list)
    emergency: list[dict] = field(default_factory=list)
