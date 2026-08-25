# Underwater Telemetry Compositing (UTC)

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

Double-click **`run_UTC.bat`**, or from a terminal:

```
python -m utc.gui.app
```

First time only:

```
python -m pip install -r requirements.txt
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
