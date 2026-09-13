# ROV Imagery Processing

> **"ROV Imagery Processing" is a temporary working title** and will likely
> change. The folder name, `rov_imagery_processing`, is the settled one.

Everything to do with the imagery that comes back from an ROV survey flight:
bringing photos into per-transect folders, developing GoPro raws, stamping the
telemetry banner on stills, and trimming and compositing video.

It is one of two programs split out of UTC (Underwater Telemetry Compositing):

| program | folder | what it does |
|---|---|---|
| **ROV Flight Operations** *(working title)* | `rov_flight_ops/` | the vehicle, monitoring, logs, flight reports |
| **ROV Imagery Processing** *(working title)* | `rov_imagery_processing/` | photos and video |
| UTC | `UTC/` | the original all-in-one program, kept working unchanged as a fallback |

The two new programs are **fully self-contained**. Neither imports anything
from the other or from `UTC/`: where both need the same file (the theme, the
survey plan, the telemetry reader…) each folder has its own copy. Each has its
own launcher and its own Python environment.

---

## Running it

Double-click **`run_rov_imagery_processing.bat`**.

The first run builds a private Python environment in
`%LOCALAPPDATA%\CCR_ROV\rov_imagery_processing\venv` and installs what the
program needs — a few minutes, and it needs the internet that once. After that
it starts straight away. You need Python 3.10 or newer installed, with tcl/tk.

From a terminal: `python -m rov_imagery_processing` (in the environment above).
`python launch.py --selftest` checks a build's ffmpeg, fonts, timezone data and
multi-process rendering.

---

## The window

The same window as ROV Flight Operations:

* **Tabs across the top** are the navigation, numbered in working order.
* **The title banner folds away** with the **▲** button beside the *Dark mode*
  switch; the gradient bar and both controls stay, risen to the top. **▼**
  brings it back.
* **Output boxes start one line tall.** Drag the handle on the shared log's top
  edge, or on a section's bottom edge, to open them up; double-click a handle to
  jump between one line and a comfortable height. They never shrink below one
  line, and progress and status are never inside the part that shrinks.

---

## The tabs

### 1  Flight & transects

The flight folder, and the transect times every other tab sorts and cuts by.
ROV Flight Operations saves the times into the flight folder as `surveys.json`,
so choosing the folder normally fills them in; they can also be typed, loaded
and saved here. **Preview the transects** draws the dive profile from the
flight's recordings with the transects marked — worth doing before importing
imagery, especially before a card is wiped.

Recordings are found in `logs/mcap` (where ROV Flight Operations downloads them)
or loose in `logs` (older flights).

### 2  Import photos

A GoPro card, or the flight's own `photos/GPR` and `photos/JPG`, sorted into
per-transect folders. Files on a card are **copied**; files already inside the
flight are **moved**. Previews can be bannered on the way in, and frames outside
every transect can go to `off_transect/`.

### 3  Process photos

GPR raws developed to 16-bit ProPhoto TIF through Lightroom Classic: crop,
chromatic aberration, AI Denoise, export. **Check** before **Develop**. While
Denoise runs Lightroom owns the screen, and the tabs are locked.

The Lightroom plug-in and seed catalog live where UTC put them
(`%LOCALAPPDATA%\UTC\lightroom`) and keep their names (*UTC RAW develop*), so a
laptop already set up for UTC needs no new Lightroom setup.

### 4  Banner tools

Find every `JPG_preview` / `JPG_edited` / `JPG_edited_banner` folder under a
transect, a flight or a folder of flights, and add the telemetry banner.
`JPG_edited` is never written to — its banner copies go to `JPG_edited_banner`.

### 5  Video

Trim the original 4K to each transect (stream copy, seconds), build telemetry
composites (4K / 1080p / 720p, plus a 1 Hz CSV), cut a short clip from one
video, or put two flights side by side.

---

## The flight folder

```
2026_09_16_Centennial/
    surveys.json             transect times
    logs/
        mcap/                recordings (or loose in logs/ on older flights)
        BIN/                 autopilot logs -- the telemetry fallback
    photos/
        GPR/  JPG/           as offloaded
        C3/                  C3 imagery downloaded by ROV Flight Operations
        transects/T1/
            GPR/  JPG_preview/  JPG_edited/  JPG_edited_banner/
        off_transect/
    videos/
        downward/            GoPro footage
        transects/T1/        4K trims
        composites/          telemetry composites
        clips/               short clips
```

## Cache

`%LOCALAPPDATA%\utc_cache\` holds extracted telemetry and ROV-video proxies,
per flight (gigabytes). The location is shared with UTC and ROV Flight
Operations **on purpose**: it is data, not code, and sharing it means a flight
whose telemetry was switched to the autopilot's `.BIN` log in ROV Flight
Operations is bannered and composited from that log here, and nothing is
extracted twice.

---

## Development

```
rov_imagery_processing/
    run_rov_imagery_processing.bat   launcher (builds its own environment)
    launch.py                        entry point for a packaged build
    pyproject.toml                   package and dependencies
    assets/                          fonts, logos, icon
    rov_imagery_processing/          the package
        gui/
            app.py          the window and its five tabs
            shell.py        banner fold, tabs, resizable output (same file as flight ops' copy)
            widgets.py      cards, resize grips, site/transect editors
            importpage.py, processpage.py, bannertools.py, videopage.py
        lightroom/          the RAW develop batch and its Lightroom plug-in
        pipeline.py, compose.py, overlay.py, sorting.py, ingest.py, photos.py, ...
    tests/
```

```
python -m pytest            # hermetic tests
python -m pytest --runlive  # also the scripts that need real data or a display
```
