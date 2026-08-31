# CCR ROV flight plan

Generates an Aquarium-branded PDF flight plan for an ROV survey day: it pulls
tide, wind, swell and topside-weather data for a location from NOAA/NWS (with
Open-Meteo and NDBC as fallbacks), draws three figures, and compiles the whole
thing with LaTeX.

This is the **proof-of-concept stage** — data scraping, figure generation, and
a LaTeX PDF, driven from a CLI. The interactive map GUI is the next milestone.

---

## Quick start

```bash
pip install -r requirements.txt          # + a LaTeX toolchain with pdflatex
python -m flightplan --lat 47.6175 --lon -122.3600 --site "Centennial Park" \
    --date 2026-08-29 --float 08:00-13:00 --flight 10:00-11:00 \
    --out out/plan.pdf
```

Other ways to call it:

```bash
# geocode a place name instead of lat/lon
python -m flightplan --place "Neah Bay, WA" --date 2026-08-29 --out out/neah.pdf

# just the three figures (PDF + PNG), skip LaTeX
python -m flightplan --lat 38.318 --lon -123.048 --figures-only --out out/bodega.pdf

# dark theme, keep the .tex and build directory for debugging
python -m flightplan --lat 47.61 --lon -122.34 --theme dark --keep-tex --out out/x.pdf
```

`--pilot --tender --rov --vessel --objective` add free-text to the PDF.
`--force` bypasses the on-disk response cache.

**Forecast horizon:** NWS data covers roughly *now → +7 days*. Tide predictions
work for any date; wind/weather/waves/alerts need a near-future date.

---

## Data sources

| Field | Primary | Fallback |
|---|---|---|
| Tide curve + highs/lows | NOAA CO-OPS `predictions` (6-min + hilo, MLLW), nearest harmonic station | — |
| Wind at the point | NWS `api.weather.gov` gridpoint | Open-Meteo |
| Wind *field* (map) | Open-Meteo, sampled on a 13×13 grid | — (HRRR GRIB is the upgrade path) |
| Swell / waves | Open-Meteo Marine (covers protected water; NWS gridpoint swell is usually empty) | NDBC nearest buoy (observed) |
| Topside weather | NWS gridpoint | Open-Meteo |
| Marine narrative | NWS Coastal Waters Forecast (`CWF`) text product | `NSH` / `OFF` |
| Small Craft Advisory etc. | NWS active alerts (`/alerts/active?point=`) | — |
| Sunrise / sunset | `astral` (computed, offline) | — |

Every fetch is cached under `%LOCALAPPDATA%\CCR_ROV_flightplan_cache` so a plan
can be regenerated with no signal; the PDF footer stamps the retrieval time.

---

## Layout

```
flightplan/
  brand.py            SAQ palette + figure Theme
  config.py           defaults (home = Seattle Aquarium, windows, datum, ...)
  conditions.py       build_conditions() -> one Conditions object
  contacts.py         default contact tables (from the current Word plan)
  render.py           Conditions -> figures -> Jinja2 -> pdflatex -> PDF
  cli.py              python -m flightplan
  sources/            tides, weather, waves, marine, alerts, windfield, astro
  figures/            tide.py, windmap.py, seastate.py, style.py
  figures/assets/     land_westcoast.geojson (vendored coastline)
  latex/              flight_plan.tex.j2 + logo
  data/               tide_stations.json, ndbc_stations.json (vendored indexes)
tools/                build_station_index.py, build_buoy_index.py, build_basemap.py
tests/                offline smoke tests (python -m pytest tests)
```

`sources/` and `figures/` never import the GUI — the pipeline runs from the CLI
now and from the desktop app later (see `docs/SEATTLE_AQUARIUM_GUI_GUIDE.md`).

Regenerate the vendored indexes when they go stale:

```bash
python tools/build_station_index.py     # CO-OPS tide-prediction stations
python tools/build_buoy_index.py        # NDBC station table (West Coast subset)
```

---

## Known limitations (proof-of-concept)

- Wind-map basemap is Natural Earth 50m-ish land only, drawn flat (no
  projection); horizontal scale is stretched ~15–50 % at PNW latitudes.
- Wind field is Open-Meteo point samples, not a native model grid.
- Outer-coast subordinate tide stations fall back to the nearest *harmonic*
  station for the curve shape (labelled with distance).
- Contact tables, objectives and hazards are still placeholder / CLI-only.

## Next

1. Refine figure/PDF layout with the team.
2. CustomTkinter GUI: zoom/pan map (tkintermapview) with an offline tile DB for
   WA/OR/CA, click → lat/lon, address search → the anchor for this pipeline.
