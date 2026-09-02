# Underwater Telemetry Compositing (UTC)

File management and telemetry overlays for ROV survey flights: create a flight's
folder structure, sort its imagery into transects, stamp telemetry onto stills,
and build one composite video per transect.

The app has five screens on a left-hand rail, following the life of a flight:

| Screen | What it does |
|---|---|
| **Flight setup** | Create a flight's folders, then enter its transect times once. Draws a dive profile with the transects marked, so a mistyped time is obvious before anything is processed. |
| **Import photos** | Pull stills off the camera card straight into transect folders, renamed and bannered. Copies, never moves. |
| **Video** | Trim each transect from the GoPro, build composites, and cut short shareable clips. |
| **Recording health** | Check each `.mcap` for damage, repair the ones the vehicle never closed, and — when a recording is beyond saving — read telemetry from the autopilot's own `.BIN` log instead. |
| **Banner tools** | Add the telemetry banner to any folder of stills, later. |

## Folder structure

```
2026_08_25_Centennial/
    logs/                       *.mcap, *.BIN
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
        downward/  forward/     source GoPro footage
        transects/T1/           per-transect trims
        composites/             finished composites
        clips/                  short shareable cuts
    utc_plan.json               sites and transect times
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

### For collaborators: nothing to install

Hand them **`Underwater-Telemetry-Compositing.exe`** and they double-click it.
There is nothing else to install — no Python, no ffmpeg, no fonts, no
timezone database. It is one self-contained file (~96 MB) carrying its own
copy of everything:

| bundled | why it has to be |
|---|---|
| Python 3.13 runtime | the whole point of the single file |
| ffmpeg (static) | every trim, composite and clip shells out to it |
| Montserrat | the Aquarium brand face, for the GUI and the photo banner |
| `tzdata` | Windows ships no IANA database, and without one every transect time resolves to the wrong instant |
| `pymavlink` | reads the autopilot's `.BIN` dataflash logs |
| `mcap`, `PyAV`, Pillow, NumPy, CustomTkinter | telemetry, video, imagery, GUI |

Requirements on their side:

* **Windows 10 or 11, 64-bit.** The build is Windows-only; macOS or Linux
  would need its own build from the same spec.
* **Disk space.** The app is small but its working cache is not — reading one
  dive writes several GB per flight under `%LOCALAPPDATA%`, and a 5 GB
  recording can produce ~12 GB of intermediates.
* **An NVIDIA GPU is optional.** UTC runs a two-frame trial encode to find out
  whether NVENC really works and falls back to the CPU encoder when it does
  not — it is a speed difference, not a requirement.
* **First launch shows a SmartScreen warning**, because the executable is not
  code-signed: *More info* → *Run anyway*. Tell partners to expect this, or it
  reads as the file being unsafe.

If a partner reports trouble, have them run the build's own health check:

```
Underwater-Telemetry-Compositing.exe --selftest
```

It verifies the bundled ffmpeg, fonts and timezone database, that `pymavlink`
imports, and that overlay rendering really does run across processes — then
writes the result to `%TEMP%\utc_selftest.txt` for them to send on. A windowed
build discards stdout, so the file is the point.

### For development

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

Produces `dist/Underwater-Telemetry-Compositing.exe` (~96 MB), which needs no Python install
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

Entries are saved to `utc_plan.json` in the flight folder and reloaded
automatically next time, so a re-run at a different resolution needs no retyping.

> Transect names must be **unique across the whole plan**, not just within one
> site, and a reused name is now rejected by validation. Imagery is filed by
> transect name alone, so two sites that both call a transect `T1` land in one
> folder and cannot be told apart afterwards — which happened on 2026-08-31 with
> two ROVs flown the same day. If a second vehicle flew, number its transects
> onward (`T5`) rather than restarting at `T1`.

### 3. Output

Tick any combination of 4K / 1080p / 720p. Videos land in
`videos/composites/`, named:

```
YYYY-MM-DD_project_site_transect_resolution.mp4
2026-08-24_HSIL_Centennial_T1_1080p.mp4
```

The 1 Hz CSV lands in `logs/`.

### How long it takes, and where the time goes

Measured on a 20-core laptop, one 10-minute transect (3,600 overlay frames):

| overlay workers | wall clock | frames/s | vs one core |
|---|---|---|---|
| 1 | 613.8 s | 5.9 | — |
| 4 | 176.0 s | 20.5 | 3.5× |
| 8 | 158.6 s | 22.7 | 3.9× |
| 12 *(default)* | 115.7 s | 31.1 | **5.3×** |
| 16 | 103.3 s | 34.8 | 5.9× |

Drawing the overlay was the pipeline's only serial stretch — ffmpeg already
uses every core when it encodes, and the trims are stream copies bound by the
disk. Panels are now drawn across processes: telemetry is sampled and footers
formatted in the parent, so workers receive plain data and neither the
telemetry store nor the caller's footer callback has to be picklable.

The default leaves two cores free (capped at 12) because a run lasts tens of
minutes and the machine is normally still in use. Override with
`AppConfig.overlay_workers` or the `UTC_OVERLAY_WORKERS` environment variable.
Scaling is well short of linear — past a dozen workers, PNG compression and the
disk take over — so 16 buys little over 12 while making the laptop sluggish.

Sequences under 400 frames stay on one process: starting a pool costs more than
it saves. If a pool cannot start at all, the run falls back to one core and
says so rather than failing.

> Parallel and serial output is verified byte-identical, frame for frame — a
> composite must not depend on how many cores drew it.

**Trimming is deliberately not parallelised.** It is an ffmpeg stream copy: one
flight wrote 9.19 GB across three transects in about 24 seconds, roughly
400-500 MB/s, which is the disk's limit rather than the CPU's. Running them at
once would divide that bandwidth, not multiply it.

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

## When a recording fails

**Arming the ROV starts a new `.mcap`; disarming closes it.** The filename is
the arm time in UTC, which is why a day's folder holds a file per arm/disarm
cycle, some of them seconds long. Verified against the autopilot's own arm
events on two flights: `recorder_20260901_161800.mcap` was created at the
09:18:00 arm to the second, and closed one second after the 09:30:40 disarm.

The corollary matters: **the close depends on the disarm arriving over
MAVLink.** Break that path and the recorder never closes the file. Both
failures we have seen are that, and the fix differs:

| symptom | cause | what UTC does |
|---|---|---|
| `.mcap` rejected as corrupt (`RecordLengthLimitExceeded`); the BIN stops at the same instant | power lost mid-dive — the recorder was killed before it could write its footer | **Reads it anyway.** A scan walks the record headers to find the last good byte, then feeds the file plus a synthetic footer to the reader. The recording is opened read-only and never modified. |
| `.mcap` runs long past the disarm and is truncated; the BIN keeps logging normally | the MAVLink router died — the vehicle flew on, but nothing reached the recorder | The mcap holds video but no telemetry for the rest of the dive. **Switch that flight to the `.BIN`** on *Recording health*. |

Which of the two it is takes seconds to tell: compare where the BIN ends
against where the mcap ends.

### Reading the autopilot's own log

The flight controller writes `.BIN` dataflash logs to its own storage,
independent of BlueOS and of the MAVLink router — so they survive exactly the
failure that empties an mcap. *Recording health* lists them, places them on the
wall clock, and can make one the flight's telemetry source; everything
downstream (banner, dive profile, overlay, CSV) then reads it without knowing
the difference. **"Back to mcap" undoes it**, and the mcaps are never written to.

Placing a BIN on the clock is the hard part, because `TimeUS` is only
microseconds since the autopilot booted and a submerged vehicle rarely has a
GPS fix. Two routes, in order of preference:

* **GPS**, when any fix was logged — week and millisecond give UTC directly.
* **An overlapping mcap.** MAVLink carries `time_boot_ms` stamped by the same
  autopilot that writes `TimeUS`, so a recording that overlaps the log pins the
  two clocks together — including a recording whose telemetry died partway,
  which is the case that matters.

Two things that alignment gets right, both learned the hard way:

* **Each recording is judged alone.** A day's folder holds several power
  cycles and every one restarts `time_boot_ms` at zero; pooling them produced
  an offset 25 minutes wrong that looked entirely plausible.
* **It refuses to vouch for itself without corroboration.** Which recording
  shares a BIN's boot session is decided by whether the two dive profiles agree
  on the autopilot's clock — an axis no choice of offset can fake. Below
  r = 0.98 the alignment is reported as unverified rather than used. A wrong
  offset files imagery into the wrong transect, which is worse than a blank.

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
  eight thrusters); light power is `NAMED_VALUE_FLOAT` / `Lights1`. In a
  dataflash log the same signal is `RCIN.C9` and camera tilt is `RCOU.C10`,
  confirmed against a flight carrying both (r = +0.97 and +0.94).
* **Altitude has no status field over MAVLink.** When the DVL loses bottom lock
  ArduPilot reports `RANGEFINDER.distance = 0.00`, which is not an altitude of
  zero. The dataflash log does carry a status, and shows those samples are
  `NoData` — about one in eight while flying a transect. Both readers now drop
  them, so the banner shows the last good value rather than a false 0.00 m.
* `BARO` in a dataflash log has **two instances**: `[0]` is the pressure inside
  the electronics tube (~89 kPa, reads as +15 m of altitude) and `[1]` is the
  water sensor. Read together they correlate with nothing. Depth is
  `-CTUN.Alt`, `-POS.RelHomeAlt`, or `-BARO[1].Alt`.
* Dataflash logs angles in **degrees**; MAVLink uses **radians**.
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
        layout.py           flight folder structure and scaffolding
        discovery.py        finding inputs in a flight folder
        survey.py           sites, transects, TC-25 resolution
        mcap_extract.py     mcap -> H.264 + telemetry, incl. truncated files
        mcap_health.py      structural check and repaired copies
        binlog.py           ArduPilot .BIN as a telemetry source
        telemetry.py        indexed lookup + export columns
        ingest.py           card scan and import into transect folders
        sorting.py          sorting an existing offload into transects
        photos.py           telemetry stamped onto flight stills
        depthplot.py        dive profile with transects marked
        rov_video.py        exact-PTS remux + constant-rate proxy
        videoclip.py        per-transect trims
        clips.py            short shareable clips and GIFs
        gauges.py           compass and tilt drawing
        overlay.py          telemetry panel and overlay sequences
        compose.py          ffmpeg composition
        ffmpeg_tools.py     locating ffmpeg, probing, NVENC detection
        csv_export.py       1 Hz CSV
        sync.py             light-based verification
        pipeline.py         orchestration
        selftest.py         --selftest health check for a packaged build
        fsutil.py           lock-tolerant publishing of finished files
        power.py            keeps the machine awake during a run
        gui/                CustomTkinter app
    tests/
```

Caching: intermediates go to `%LOCALAPPDATA%\utc_cache\` (an existing
`ccr_composite_cache\` from before the rename is reused rather than rebuilt),
deliberately **outside** the flight folder so Dropbox does not sync disposable
working files to the whole team. Budget generously — a 5.3 GB recording
produced ~12 GB of intermediates (raw H.264, muxed proxy, constant-rate proxy,
telemetry CSV, overlay frames). A second run skips straight to compositing.
Delete that folder to force a rebuild.

---

## Tests

```
pytest                  the automated suite: hermetic, no flight data, seconds
pytest --runlive        also the scripts needing real flights or a display
ruff check utc tests    lint
```

Both run in CI on every push and pull request (`.github/workflows/utc-ci.yml`,
Python 3.11 and 3.13), which also builds the executable.

The suite is hermetic by design — it builds its own mcaps, breaks them the same
way a real recorder does, and synthesises dataflash messages, so none of it
needs a flight folder. The files that *do* need real data or a screen
(`*_live.py`, `debug_*`, the visual renderers, the GUI smoke test) are skipped
unless `--runlive` is given; collecting them on a machine without the data cost
about ninety seconds and then failed for reasons unrelated to the change.

Worth knowing which test guards what, since several exist because something
went wrong in the field:

| file | what it protects |
|---|---|
| `test_survey.py` | TC-25 parsing, DST, midnight-crossing transects, chapter spanning. Also that a missing timezone database **raises** rather than silently returning a time eight hours wrong. |
| `test_mcap_recovery.py` | Reading a recording the vehicle never closed, without modifying it. |
| `test_binlog.py` | The dataflash reader: barometer instances, degrees vs radians, a lost bottom lock that is not an altitude of zero, and refusing an unverified clock. |
| `test_photos.py` | The banner is never written twice, orientation is baked correctly, and an already-bannered folder says so plainly. |
| `test_fsutil.py` | Publishing over files locked by Excel or Dropbox. |
| `test_timeentry.py` | The six-keystroke time field, against a real Tk widget. |
