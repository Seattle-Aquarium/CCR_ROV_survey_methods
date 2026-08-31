"""CCR ROV flight-plan generator.

Pulls tide, wind, swell and topside-weather data for a survey location from
NOAA/NWS (with Open-Meteo and NDBC as fallbacks), draws the figures, and
compiles an Aquarium-branded PDF flight plan with LaTeX.

The ``sources`` and ``figures`` sub-packages never import the GUI, per
docs/SEATTLE_AQUARIUM_GUI_GUIDE.md -- the pipeline is driven by a CLI here and
by the desktop app later.
"""

__version__ = "0.1.0"
