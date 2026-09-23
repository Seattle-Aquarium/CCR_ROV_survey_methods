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

The two new programs are **self-contained in their code**. Neither imports
anything from the other or from `UTC/`: where both need the same file (the
theme, the survey plan, the telemetry reader…) each folder has its own copy.
Each has its own launcher and its own Python environment. The one thing they
deliberately share is the telemetry cache (see [Cache](#cache)). Because the
copies can drift, a fix to shared logic has to be made in both folders.

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

Almost the same window as ROV Flight Operations. Nothing here is ever plugged
into a vehicle, so the banner carries no BlueOS / ArduSub / Cockpit versions and
no vehicle or recorder lamps: there is no tether to read them over. Its two
lamps watch what this program does depend on.

* **Tabs across the top** are the navigation, numbered in working order.
* **Flight folder linked** is gray with no flight folder chosen, a ring when one
  is chosen but has no `surveys.json` in it, and lit when the transects are
  there. The ring is the one worth catching: every tab sorts and cuts by those
  times.
* **SD card connected** is gray until *Import photos* or *Video* has scanned a
  source, a ring when the source is still there but held nothing usable, and lit
  when it held photos or video. It goes out on its own when the card is pulled.
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

#### Pauses inside a transect

A transect can carry **pauses** — stretches where the vehicle was down and
recording but nothing was being surveyed, because Cockpit disarmed or the
video glitched or a minute went on getting the ROV back where it was. They are
typed in ROV Flight Operations and arrive here in `surveys.json`. Nothing has
to be done with them on this side; what they change is:

| | |
| --- | --- |
| **stills** | a frame taken during a pause matches no transect, so it is handled as off-transect — into `off_transect/` or left behind, by the policy on *Import photos* |
| **trimmed 4K** | the paused footage is cut out and the clip joined across it |
| **composites** | the same, and the telemetry, gauges and ROV inset skip with it, so the overlay stays on the frame it belongs to |
| **1 Hz CSV** | every second is kept, with a `survey_state` column reading `transect`, `pause` or `off_transect` |
| **motion check** | measured over the transect's longest *unbroken* stretch. Correlating the picture's rotation against the yaw rate assumes the two run on one clock, and they do not across a pause |

A transect with no pauses behaves exactly as it always did. The same is true of
a clip that spans two GoPro chapters with a gap between them: each segment now
carries the moment it was recorded, rather than being assumed to follow the one
before it — which is a latent bug this fixed on the way past.

### 2  Import photos

A GoPro card, or the flight's own `photos/GPR` and `photos/JPG`, sorted into
per-transect folders. Files on a card are **copied**; files already inside the
flight are **moved**. Previews can be bannered on the way in, and frames outside
every transect can go to `off_transect/`.

**Meter marks** is an option on the same tab. Tick *Work out which frame sits
at each meter* and, once the imagery is in place, each transect is measured
from the telemetry and every meter is given its own photograph. Two outputs,
each written beside that transect's imagery:

| Written | What it is |
|---|---|
| `transects/T1/GPR/meters/` | the raws that landed on a mark, moved clear of the rest |
| `transects/T1/T1_meter_marks.csv` | one row per mark: distance, time, the frame chosen, and how far off it was |
| `transects/T1/T1_track.png` | the reconstructed transect from above, marks on it, start and end labeled |

The spacing is yours to set — 1 m by default, but 0.5 m or 2 m work the same
way. A pause adds no distance: the track is still drawn across it, faintly, but
the marks stop and pick up where surveying resumes.

The frames that sit on a mark are **moved** into `meters/` inside `GPR`, so a
transect folder keeps the few frames the analysis wants apart from the many it
does not. Tick *Also file edited JPGs* and `JPG_edited/meters/` is filled the
same way, once those exports exist. Moving is not editing — nothing is
re-encoded and no JPEG generation is spent — which is what makes it safe inside
`JPG_edited`. It does move frames that downstream ML reads, so it stays off
unless asked. Re-running is safe: frames already filed are left where they are,
and `meters/` is searched when the marks are worked out again.

**Which frame a mark gets.** The nearest in time, and **each frame is used at
most once**. That second rule matters: where marks fall closer together than
the ~3 s capture cadence, a plain nearest-match hands the same photograph to
several of them and leaves the rest silently empty. A mark with no frame within
15 s is reported rather than given a distant one.

**Where the distance comes from.** Sources are tried in order and the first one
the recording actually contains is used, because they are not equally available
— a quarter of the 2024-26 archive has no `LOCAL_POSITION_NED` at all. Each
carries a scale factor measured against the 100 m tape transects:

| Source | Scale | Measured against the tape |
|---|---|---|
| `LOCAL_POSITION_NED` velocity, integrated | ×1.010 | 99.0% — essentially unbiased |
| DVL `VISION_POSITION_DELTA` magnitudes | ×1.037 | 96.5% |
| `GLOBAL_POSITION_INT` velocity | ×1.058 | fallback; reads ~4.5% under the EKF |
| `LOCAL_POSITION_NED` position, differenced | ×0.979 | last resort; noise and filter resets inflate it |

Across the 19 tape windows that describe one clean pass this lands at a median
of **100.0%** of true, mean absolute error 2.4%.

Distance is accumulated at **sensor rate**, not from a 1 Hz CSV. Measuring from
positions already resampled to one row per second discards the motion inside
each second before it is counted; on the tape transects that moved a third of
the marks far enough to pick a different photograph.

**What it tells you when something is wrong.** A timed transect has no tape to
check against, so the pass reports its own confidence rather than presenting a
single number:

* **cross-check** — every source the recording supports is measured, and a
  spread above 10% means at least one is wrong. Healthy flights sit at 2-4%.
* **straightness** — net displacement over path length. A contour-following run
  sits near 1; well below 0.5 usually means the window still holds the transit
  out to the start, or two passes.
* **coverage** — how much of the surveyed time the telemetry actually spans.
* **filter resets** — a GPS correction snapping the EKF position looks exactly
  like travel. These arrive at a normal sample interval, so they are caught by
  comparing against the vehicle's own speed, removed, and counted.

This is deliberately separate from `mcap_to_csv`: the marks need finer-grained
telemetry than the 1 Hz transect CSV carries, so the two measure independently
and their transect lengths will not agree to the meter.

**Banner tools** is the third section of this tab. It lists every
`JPG_preview` / `JPG_edited` / `JPG_edited_banner` folder in the flight folder
chosen on *Flight & transects* — no second folder to choose, and no chance of
choosing a different one — and adds the telemetry banner to the ones ticked.
`JPG_edited` is never written to: its banner copies go to `JPG_edited_banner`
beside it. Adding needs the flight's telemetry, so **Preview the transects**
once on tab 1 first.

### 3  Process photos

GPR raws developed to 16-bit ProPhoto TIF through Lightroom Classic: crop,
chromatic aberration, AI Denoise, export. **Check** before **Develop**. While
Denoise runs Lightroom owns the screen, and the tabs are locked.

The Lightroom plug-in and seed catalog live where UTC put them
(`%LOCALAPPDATA%\UTC\lightroom`) and keep their names (*UTC RAW develop*), so a
laptop already set up for UTC needs no new Lightroom setup.

### 4  Video

Trim the original 4K to each transect (stream copy, seconds), build telemetry
composites (4K / 1080p / 720p, plus a 1 Hz CSV), cut a short clip from one
video, or put two flights in one frame.

**Two videos in one frame** takes either arrangement: **side by side**, which
fills a laptop or a projector, or **stacked**, one above the other, which reads
on a phone held upright. The sources, the two start times and the duration mean
the same thing either way, so the arrangement can be changed last without
retyping anything. The format (4K / 1080p / 720p) is what *one pane* is scaled
to, so side by side is twice as wide as it says and stacked is twice as tall.
Each arrangement writes its own file — `A_vs_B_1080p.mp4` and
`A_over_B_1080p.mp4` — so building both leaves both.

**How the GoPro is lined up with the telemetry.** The transect times place the
GoPro footage by its timecode; the telemetry and ROV camera run on the
vehicle's clock. Before compositing, each transect's footage is checked
against the vehicle's own turns: the down-facing picture rotates at the yaw
rate the autopilot logs, and the lag between the two is the offset between the
clocks. When that measurement is unambiguous and the clocks disagree, the
telemetry and ROV inset are moved to match the picture and the run says so; when
it is not (a transect flown dead straight), the timecode is trusted and the run
says that instead. On 14 September 2026 the GoPro was 34.8 s behind the vehicle
— not something the light check could see on a trim flown with steady lights.

A trim starts on the keyframe before its transect (up to a second early). The
cut records that head in the file's metadata and compositing skips it; trims
cut before this was recorded have it estimated from their timecode.

Only one camera goes into the ROV inset: the busiest video stream in the
recordings (the forward camera). Recordings that also carry the Madrona cockpit
view used to have both spliced into one stream.

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

A cached extraction is used only when it was built from the **same source files
(path, size and modification time) with the current cache schema** and all its
products are present, so a recording recopied or repaired at the same path is
re-extracted rather than read from the old cache. This came out of the 13
September 2026 review of ROV Flight Operations, which shares the cache.

Only one program extracts a flight at a time: an **operating-system lock** on
`extract.lock`, held for the whole extraction and released when it ends or
when the program ends, however it ends (since the 14 September 2026 review;
before, a lock file was declared stale after two minutes without progress, and
a paused extraction could lose it). *Stop* during an extraction takes effect
within a few thousand messages, and a half-built cache is never marked valid.
**UTC's extractor takes no lock**: do not extract the same flight in UTC while
this program or ROV Flight Operations is extracting it.

## Diagnostics and reporting a problem

The program keeps its own log, on this laptop's disk, in
`%LOCALAPPDATA%\CCR_ROV\rov_imagery_processing\diagnostics\` — the
**Diagnostics** button at the top right opens it. `app.log` records start-up
facts, every job with its duration and outcome, and every unexpected error with
its traceback (including errors in button callbacks and background threads,
which used to vanish under `pythonw`); if the window stops responding for 8
seconds, every thread's stack is written there once. `faults.log` holds the
stacks if the interpreter itself crashes. Tokens are redacted and nothing is
sent anywhere.

After a freeze or crash: note the time and what you were doing, then send
`app.log`, `app.log.1` and `faults.log`. A power cut, a killed process, or
Windows ending a "not responding" program leave no fault dump — say so if that
is what happened.

The job queue and the log behind this are the same files as in ROV Flight
Operations (`gui/shell.py`, `diagnostics.py`): each job's result goes only to
that job, one failing result handler no longer stops the queue, a flood of
progress cannot starve the window, and the output pane keeps its last 2,000
lines. `tests/test_resilience.py` holds those behaviors here too.

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
            app.py          the window, its four tabs and the two banner lamps
            shell.py        banner fold, tabs, resizable output (diverged from flight ops': no versions, different lamps)
            widgets.py      cards, resize grips, site/transect editors
            importpage.py, processpage.py, videopage.py
            bannertools.py  the banner section inside importpage
        diagnostics.py      app.log, faults.log, stall watchdog (same file as flight ops' copy)
        lightroom/          the RAW develop batch and its Lightroom plug-in
        pipeline.py, compose.py, overlay.py, sorting.py, ingest.py, photos.py, ...
    tests/
```

```
python -m pytest            # hermetic tests
python -m pytest --runlive  # also the scripts that need real data or a display
```
