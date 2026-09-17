# CCR ROV survey methods — project history

**Repository:** [Seattle-Aquarium/CCR_ROV_telemetry_processing](https://github.com/Seattle-Aquarium/CCR_ROV_telemetry_processing)
**Local path:** `Coastal_Climate_Resilience\GitHub\CCR_ROV_survey_methods`
**Active period:** 2026-08-23 → 2026-08-29 (4 sessions here, plus the DeepSea light-testing session that seeded the UTC)
**Status at archive time:** UTC in active field use; flight-plan generator at CLI proof-of-concept; field log in production.

> **Naming note:** the local folder is `CCR_ROV_survey_methods` but the git remote
> is `CCR_ROV_telemetry_processing.git`. Same repo, different names — worth
> knowing before you go looking for it on GitHub.

This repo holds three related but separable pieces of tooling, plus the ROV
methods manuscript (documented separately in `manuscript_MEE_HISTORY.md`).

---

## 1. UTC — the ROV survey file-management GUI

`UTC/` is the largest body of code in the repo. It began as a video *compositing*
tool (see `DeepSea_light_testing_HISTORY.md` for its origin) and grew into a
general program for managing an ROV survey day end to end. **A rename was
anticipated but deferred** — "UTC" no longer describes what it does.

### What it does

| Stage | Capability |
|---|---|
| Flight setup | Create the dated folder structure for a new flight, named by site |
| Ingest | Pull imagery directly off a microSD card, filtered to transect windows |
| Telemetry | Parse `.mcap` files; extract MAVLink streams; plot depth |
| Composite | Overlay ROV camera + telemetry onto down-facing GoPro video |
| Clips | Extract short segments from any video at multiple resolutions |
| Export | CSV export of telemetry; banner-stamped JPG previews |

### Module map (`UTC/utc/`)

```
cli.py  __main__.py  gui/          entry points
survey.py                          flight/transect model  ← time-entry bug fixed here
ingest.py  discovery.py  sorting.py  fsutil.py   file movement & SD-card ingest
mcap_extract.py  telemetry.py  sync.py           mcap parsing, time alignment
compose.py  overlay.py  layout.py  gauges.py     video composite + HUD
depthplot.py  rov_video.py  videoclip.py  clips.py
photos.py  brand.py  csv_export.py  power.py  config.py  pipeline.py
ffmpeg_tools.py                    ffmpeg wrapper
```

### The SD-card ingest workflow

This was the most ambitious feature and the one that defines the tool's value —
the goal stated plainly was *"to make the workflow as absolutely efficient as
possible, especially as we conduct a lot of surveys and move around a lot of files."*

1. Create the flight folder structure (UTC-named, site-named, in a set location).
2. `.mcap` telemetry copied in manually from a thumb drive.
3. Transect start/end times entered in the GUI.
4. Point the GUI at the GoPro photo SD card → **only imagery falling inside the
   transect windows is imported**, straight from the card, bypassing a bulk copy.
5. Same for the video SD card → videos trimmed/assembled to transect overlap.

Toggles: add banner to JPG previews; skip JPG previews entirely; send
non-transect imagery to an `off_transect/` folder. Banner *removal* was
deliberately dropped — banners only ever go on copies, so deleting the folder
suffices.

The **full `.mcap` is always preserved uncut**, even though imagery is filtered.

### Time entry

Transect times are entered as six keystrokes with no colons (`123456` →
`12:34:56`). The first implementation had a digit-ordering bug that produced
`12:45:63`; this was found in field use and fixed in commit `e485fc3`.

### Software practice

Explicitly requested that best practices be led on, because the team will branch
from `main` for their own extensions and PR back. In place: `CONTRIBUTING.md`,
a PR template, a test suite under `UTC/tests/`, GitHub Actions CI, and
PyInstaller packaging (`utc.spec`, `run_UTC.bat`, `dist/`).

At archive time CI was **failing** on the most recent push
(`actions/runs/33263123542`) and that was the open thread.

### Uncommitted work in progress

At archive time the working tree had modifications to `UTC/pyproject.toml`,
`UTC/requirements.txt`, `UTC/tests/test_survey.py`, `UTC/tests/test_timeentry.py`,
`UTC/utc.spec`, and `UTC/utc/survey.py` — i.e. the time-entry fix and packaging
adjustments were still in flight.

---

## 2. ROV flight plan generator (`docs/ROV_flight_plan/`)

*Session `e05f17f2`, 2026-08-27 — one long brief, then autonomous build.*

**The problem it solves:** the flight plan was a Word document filled in by hand,
requiring manual lookup of tide charts and marine forecasts — tidal heights and
times, swell, wind direction and speed, topside conditions. Time-intensive and
repeated for every survey day.

**What was built:** a Python package that pulls the data automatically and
compiles an Aquarium-branded PDF via LaTeX.

- **Data sources:** NOAA/NWS primary, with **Open-Meteo** and **NDBC** as
  fallbacks. `sources/` holds the scrapers, `conditions.py` the assembly.
- **Figures:** three, including the centrepiece — a **24-hour continuous tidal
  curve** for the full survey day with sunrise/sunset shading and labels,
  markers delineating **float time** (on-water period, e.g. 08:00–13:00) and
  **flight time** (the ROV dive itself, e.g. 10:00–11:00).
- **Branding:** `brand.py` + `latex/` apply Seattle Aquarium visual identity.
- **Tooling:** `tools/build_basemap.py`, `build_buoy_index.py`,
  `build_station_index.py` prepare offline reference data.

```bash
python -m flightplan --lat 47.6175 --lon -122.3600 --site "Centennial Park" \
    --date 2026-08-29 --float 08:00-13:00 --flight 10:00-11:00 --out out/plan.pdf
```

Also supports `--place "Neah Bay, WA"` geocoding, `--figures-only`,
`--theme dark`, `--keep-tex`, and `--force` to bypass the response cache.

**Known limit:** NWS covers roughly *now → +7 days*. Tide predictions work for any
date, but wind/weather/waves/alerts need a near-future date.

**The next milestone, not yet built:** an interactive **map GUI** — pan/zoom,
click a survey location to set lat/lon, default view centred on Elliott Bay,
text box to snap to a named place (e.g. "Newport, Oregon"), and offline basemaps
for the US West Coast. This was the original ask and remains open.

---

## 3. ROV field log (`docs/ROV_field_tracking/`)

*Session `a7b0a26d`, 2026-08-24.*

A single-page LaTeX document (`CCR_ROV_field_log.tex` → `.pdf`) printed and taken
into the field to record transect start/end times by hand. Those times then drive
post-processing — isolating `.mcap` telemetry and survey imagery for each transect.
It is the paper counterpart to the UTC's time-entry screen.

**Design specifics:**
- Must fit **one page per day**, fully self-contained.
- Header: date, project, site, personnel, ROV/vessel, **data entry lead**.
- Pre-dive checkboxes: GoPro Labs camera sync, TC25 on screen,
  **camera/housing cleaned**, **cameras recording**.
- **Six transect blocks** (reduced from ten), each a row of
  start / end / target altitude / target speed / target light / target mode,
  with a generous notes area beneath — the space freed by dropping four transects
  went into notes.
- Palette drawn from the Seattle Aquarium style guide: **Salish, Fathom, Algae,
  Seafoam, Mediterranean, Stone, Pumice** — explicitly *not* Purple Star or Coral.
- Seattle Aquarium primary logo (MedBlue) embedded.

**Time convention:** only **TC25** time is logged, sourced from GoPro Labs
precision time sync (`https://gopro.github.io/labs/control/precisiontime/`).
An earlier "times logged as local or UTC" field was removed — TC25 is what
people actually read off the screen, and it is PST.

---

## 4. Environment

*Session `72b7e9a7`, 2026-08-24.* Verified the Python installation on the work
laptop and established that **Anaconda is not required** for this work. Relevant
because the UTC and flight-plan tools are Python, while the analysis work in
`CCR_benthic_analyses` is R.

---

## 5. Conventions to carry forward

1. **Branding is not optional.** Both the field log and the flight plan pull from
   the Seattle Aquarium visual identity guidelines
   (`visual_media\communications\presentation_graphics\SAQ-001_Visual-ID-Guidelines_FINAL_V1-0823.pdf`)
   and the logo set. `docs/SEATTLE_AQUARIUM_GUI_GUIDE.md` in this repo is the
   distilled version — **supply it at the start of any GUI or document-design session.**
2. **TC25 is the time standard** for linking imagery to telemetry.
3. **Preserve raw telemetry.** Imagery gets filtered; `.mcap` never does.
4. **The team will branch and PR** — keep CI green and the contributing guide current.
5. Hardware context: BlueRobotics **BlueROV2** with ArduSub, BlueOS, and Cockpit;
   **GoPro HERO 12** down-facing with white balance set to Native; Outland topside
   power supply, 1000 W max.

---

## 6. Open threads

- **CI is failing** on the latest push — unresolved at archive time.
- **Flight-plan map GUI** — the main unbuilt feature.
- **UTC rename** — acknowledged as needed, deliberately postponed.
- Uncommitted changes in `UTC/` (see §1).

---

*Companion transcripts: `transcripts/CCR_ROV_survey_methods/` (4 sessions).
See also `DeepSea_light_testing_HISTORY.md` for the UTC's origin, and
`manuscript_MEE_HISTORY.md` for the manuscript in this repo.*
