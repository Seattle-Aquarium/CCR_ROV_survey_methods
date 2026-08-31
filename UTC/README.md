# Underwater Telemetry Compositing (UTC)

File management and telemetry overlays for ROV survey flights: create a flight's
folder structure, sort its imagery into transects, stamp telemetry onto stills,
and build one composite video per transect.

The app has three tabs, following the life of a flight:

| Tab | What it does |
|---|---|
| **New flight** | Creates the empty folder structure to offload into. |
| **Sort & composite** | Enter transect times once, then sort the imagery and/or build composites. |
| **Banner tools** | Add or remove the telemetry banner on any folder of stills, later. |

## Transects (mcap to CSV)

The **Transects** page runs the extractor in [`mcap_to_csv/`](../mcap_to_csv/)
against the flight that is already open. It reads the survey plan from Flight
setup and the recordings from the flight folder, so the transect windows are
typed once and drive both the CSVs and the video overlays — two copies of those
times drifting apart is the kind of error that only shows up when the analysis
disagrees with the footage.

It writes one CSV per transect plus a Leaflet map of the site. Column meanings
and provenance are in [COLUMNS.md](../mcap_to_csv/COLUMNS.md).

`run_UTC.bat` installs the extractor alongside UTC. If the page reports it
missing, install it by hand:

```bash
python -m pip install -e ../mcap_to_csv
```

## Folder structure

```
2026_08_25_Centennial/
    logs/                       *.mcap
    photos/
        GPR/  JPG/              drop the offload here
        transects/
            T1/
                GPR/                sorted raws
                JPG_preview/        sorted previews, banner applied
                JPG_edited/         your colour-corrected exports
                JPG_edited_banner/  generated banner copies
            off_transect/       optional home for frames outside a transect
    videos/
        downward/  forward/  composites/
```

Sorting **moves and renames** files to `YYYY_MM_DD_hh-mm-ss`, so a raw and its
preview end up with identical stems and stay paired:

```
photos/transects/T1/GPR/2026_08_25_13-23-17.GPR
photos/transects/T1/JPG_preview/2026_08_25_13-23-17.JPG
```

> **`JPG_edited` is never written to.** Those frames feed downstream ML, so
> their banner versions go to a `JPG_edited_banner` sibling instead. Removing a
> banner is then a matter of using the originals, which were never touched — a
> stamp-then-strip round trip costs two JPEG generations (measured at ~43 dB
> against ~53 dB for a single stamp), and that is not worth spending on
> analysis inputs.

---

## Composites

Combines the **downward-facing GoPro** from an ROV transect with telemetry from
the BlueOS `.mcap` recording, and writes one video per transect.

Each composite carries, along the top of the frame:

* the **ROV's forward camera** (from the mcap) as an inset,
* a **compass rose** and a **tilt indicator**, stacked, and
* a **telemetry panel** — depth, altitude, speed, flight mode, light power,
  thruster gain, camera tilt, water temperature, and power draw.

The right half of the frame is deliberately left clear.

It also writes a **1 Hz telemetry CSV** for the whole flight, labelled by
project / site / transect, with GPS, DVL and EKF diagnostics alongside the
flight data.

---

## Running it

Double-click **`run_UTC.bat`**. Nothing needs installing first beyond Python
3.10 or newer: on its first run the launcher builds a private environment in
`%LOCALAPPDATA%\CCR_ROV\venv`, installs UTC and the transect extractor into it,
and starts the app. That takes a few minutes once; after that it opens straight
away.

The environment sits outside the repo deliberately — this checkout lives in a
OneDrive folder, and a virtualenv there would be thousands of files for the sync
client to chew through forever. `run_MCAP_to_CSV.bat` shares the same
environment, so whichever runs first does the work.

The launcher tests each Python it finds rather than taking the first one on
disk. A partial install still leaves a `python.exe` that cannot find its own
standard library, and choosing it produces a misleading `_tkinter` DLL error
rather than an obvious "this Python is broken".

From a terminal instead:

```
python -m pip install -e .
python -m utc.gui.app
```

`ffmpeg` does not need installing separately — the `imageio-ffmpeg` wheel ships
a static build. A real ffmpeg on `PATH` is used in preference if present.

### Building a standalone .exe

```
python -m pip install pyinstaller
pyinstaller utc.spec
```

Produces `dist/Underwater-Telemetry-Compositing.exe` (~87 MB), which needs no Python install
and can be handed to a colleague directly. Windows SmartScreen will warn about
an unsigned executable the first time: *More info* → *Run anyway*.

The build output is **git-ignored**; do not commit it.

Two things about the build worth knowing:

* The spec targets `launch.py`, not `utc/gui/app.py`. PyInstaller runs its
  target as `__main__`, so aiming it at the module breaks that module's relative
  imports (`attempted relative import with no known parent package`). A
  top-level script that imports the package keeps the package context intact.
* A windowed build discards stdout and stderr, so a startup failure leaves no
  trace whatsoever. Build a console variant to find out why:

  ```
  set COMPOSITE_DEBUG=1
  pyinstaller utc.spec
  dist\Underwater-Telemetry-Compositing-debug.exe
  ```

---

## The workflow

### 1. Flight folder

Point the app at the folder for one dive. The expected layout is:

```
2026_08_24_Centennial/
    logs/                 recorder_*.mcap
    photos/
    videos/
        downward/         GoPro MP4s   <- composited
        forward/          GoPro MP4s   <- ignored
```

Older layouts (`video/`, `downward/video/`, mcaps loose in the root) are
recognised too. Whatever it finds is listed in the panel — **read it before
running.** Compositing the wrong camera is an expensive mistake to discover an
hour into an encode, so discovery reports rather than assumes.

Several mcaps per flight is normal (BlueOS rolls a new file each time recording
restarts); they are merged onto one timeline in chronological order.

### 2. Sites and transects

Add a site (name, project, date), then its transects. Times are **TC-25** —
the clock the GoPro displays after a
[GoPro Labs precision time](https://gopro.github.io/labs/control/precisiontime/)
sync, as written down in the field. `hh:mm:ss`; the duration is shown as you
type, and obviously wrong entries are flagged.

Multiple sites per flight folder are supported.

Entries are saved to `composite_plan.json` in the flight folder and reloaded
automatically next time, so a re-run at a different resolution needs no retyping.

### 3. Output

Tick any combination of 4K / 1080p / 720p. Videos land in
`videos/composites/`, named:

```
YYYY-MM-DD_project_site_transect_resolution.mp4
2026-08-24_HSIL_Centennial_T1_1080p.mp4
```

The 1 Hz CSV lands in `logs/`.

### While a run is going

A full flight is tens of minutes of encoding, so two things are worth knowing.

**Sleep.** Windows does not count a working process as user activity, so a
laptop left alone will idle-sleep mid-encode and the run pauses until it wakes.
On one test run that cost 55 minutes and looked exactly like a hang. The tool
now asks Windows to stay awake while it works. **Closing the lid still sleeps
the machine** — no program can override that — so leave the lid open on a long
run. The screen is allowed to switch off, which is fine.

**Files open elsewhere.** Outputs are written into a Dropbox folder, so
something else may be holding the file the tool is about to replace: Dropbox
uploading the previous version, antivirus, or Excel with the last run's CSV
still open. The tool waits for the lock to clear and says so. If it never
clears, it writes `…(1).mp4` alongside rather than throwing away the encode, and
tells you to close the other program.

---

## How the clocks are tied together

Two independent mappings, which is what makes the result checkable:

**TC-25 → video.** Every GoPro MP4 carries the timecode of its first frame, so a
transect time maps to a position inside a chapter by subtraction. Exact, and
needs no timezone. A transect spanning a chapter boundary is rendered in parts
and joined.

**TC-25 → mcap.** The mcap is stamped in UTC epoch, so this needs the local UTC
offset. That is **derived from the flight date** (via the IANA zone, so PST/PDT
is handled) rather than typed in — a mistyped offset would look exactly like a
good run until someone noticed the depth readout disagreeing with the picture.

**The check.** The derived mapping is then verified against a signal both
recorders see: the ROV's own lights. They are ramped to full at the start of a
dive and back to zero before ascending, and the downward GoPro goes from
near-black to lit when that happens. If the timecode and the lights disagree by
more than a few seconds, the run reports it.

Two traps that check deliberately avoids:

* Brightness is *not* linear in light power — altitude above the seabed and
  scene albedo move it too. So it scores agreement between "GoPro is dark" and
  "lights are off", not a correlation of raw values.
* Near the surface the relationship inverts: the GoPro can be bright *while the
  lights are off*, which is exactly what makes a naive correlation lock onto the
  wrong answer.

The ROV's own forward camera is useless for this — its auto-gain is aggressive
enough that whole-frame brightness barely moves across a full lights-off
transition (72 → 83 on the 2026-08-21 flight).

If the camera was never synced, there is no timecode track and the app says so
rather than guessing.

---

## The telemetry CSV

One row per second across the whole recorded span, so descents, ascents and
between-transect manoeuvring stay in the record. Rows outside a transect are
labelled `off_transect`.

Columns: UTC and TC-25 time, date, project/site/transect, **power (V × A)**,
voltage, current, depth, altitude, pressure, water temperature, heading, roll,
pitch, yaw, ground speed, climb, NED velocity and position, GPS (lat/lon/alt/
fix/satellites — zero unless the USBL was running), EKF variances, DVL
confidence and per-beam ranges, vibration, light power, gain and camera tilt.

Values are held forward from the last sample — these are sampled states, not
continuous signals — but only up to a staleness limit. A DVL that drops out
leaves blanks rather than a flat line, so a dead sensor cannot look healthy.

---

## Layout and appearance

The GUI follows the Seattle Aquarium visual identity (v1, Aug 2023): Montserrat
throughout, with a dark scheme on Fathom and a light scheme on White/Pumice with
Stone body copy. Both respect the guidelines' contrast rules. Toggle top-right.

Overlay geometry and colours live in `utc/config.py` (`Layout`), and the
panel contents in `PANEL_ROWS`.

---

## Notes on the source footage

* The GoPro is mounted inverted and carries a **−180° rotation flag**. Relying
  on ffmpeg's autorotate is a trap: it rotates the frames fed to the filter graph
  *and* copies the matrix onto the output, so a player rotates the finished
  composite a second time and everything — overlays included — appears upside
  down. We neutralise the input matrix and apply the rotation ourselves.
* Video is HEVC Main 10. The pipeline stays 10-bit for 4K and 1080p so the tonal
  range that shooting with Native white balance exists to preserve survives.
* Light power is **not** on servo 16. `SERVO_OUTPUT_RAW` carries only port 0 (the
  eight thrusters); light power is `NAMED_VALUE_FLOAT` / `Lights1`.
* The mcap's `log_time` is written in bursts and is *not* the video frame time —
  that lives inside each `foxglove.CompressedVideo` message.
* The ROV camera stream is strongly variable-rate, which makes it unreliable to
  seek (asking for 465.259 s can return the frame at 466.708 s). It is therefore
  resampled once per flight to a constant-rate proxy.

---

## Layout of this folder

```
UTC/
    run_UTC.bat             double-click launcher
    utc.spec                PyInstaller build
    requirements.txt
    assets/                 logos, app icon
    utc/
        brand.py            Seattle Aquarium palette, fonts, logos
        config.py           layout, encoding, panel contents
        discovery.py        finding inputs in a flight folder
        mcap_extract.py     mcap -> H.264 + telemetry
        rov_video.py        exact-PTS remux + constant-rate proxy
        telemetry.py        indexed lookup + export columns
        survey.py           sites, transects, TC-25 resolution
        sync.py             light-based verification
        gauges.py           compass and tilt drawing
        overlay.py          telemetry panel and overlay sequences
        compose.py          ffmpeg composition
        csv_export.py       1 Hz CSV
        photos.py           telemetry stamped onto flight stills
        pipeline.py         orchestration
        fsutil.py           lock-tolerant publishing of finished files
        power.py            keeps the machine awake during a run
        gui/                CustomTkinter app
    tests/
```

Caching: intermediates (~4 GB per flight) go to
`%LOCALAPPDATA%\utc_cache\` (an existing `ccr_composite_cache\` from before the rename is reused rather than rebuilt), deliberately **outside** the flight
folder so Dropbox does not sync disposable working files to the whole team. A
second run skips straight to compositing. Delete that folder to force a rebuild.

---

## Tests

```
python tests/test_survey.py          TC-25 parsing and transect resolution
python tests/test_fsutil.py          publishing over files locked by Excel/Dropbox
python tests/test_discovery_live.py  discovery against the real flight folders
python tests/test_render_visual.py   panel + gauge convention grid (writes PNGs)
python tests/test_gui_smoke.py       constructs the GUI, screenshots both themes
```

`test_survey.py` is the one to run after touching timecode logic — it covers
DST, transects crossing midnight, chapter spanning, and filename sanitising.
