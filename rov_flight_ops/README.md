# ROV Flight Operations

> **"ROV Flight Operations" is a temporary working title** and will likely
> change. The folder name, `rov_flight_ops`, is the settled one.

Everything to do with the ROV itself and what it recorded: connecting to the
vehicle, watching the laptop and the tether while it flies, recording each
flight, typing and checking the transect times, and pulling logs off the Pi —
or clearing them off it.

It is one of two programs split out of UTC (Underwater Telemetry Compositing):

| program | folder | what it does |
|---|---|---|
| **ROV Flight Operations** *(working title)* | `rov_flight_ops/` | the vehicle, monitoring, logs, flight reports |
| **ROV Imagery Processing** *(working title)* | `rov_imagery_processing/` | photos and video |
| UTC | `UTC/` | the original all-in-one program, kept working unchanged as a fallback |

The two new programs are **self-contained in their code**. Neither imports
anything from the other or from `UTC/`: where both need the same file (the
theme, the survey plan, the telemetry reader…) each folder has its own copy.
Each has its own launcher and its own Python environment, so updating one can
never break another. Two things are deliberately *not* separate:

* the **transect extractor** in `../mcap_to_csv`, which *Analyze transects*
  needs and the launcher installs from the repository, and
* the **telemetry cache** in `%LOCALAPPDATA%\utc_cache`, shared on purpose (see
  [Settings and cache](#settings-and-cache)).

Because the copies can drift, a fix to shared logic has to be made in both
folders. The GUI's shared files (`gui/shell.py`, `widgets.py`, `theme.py`,
`gradients.py`), `mcap_extract.py` and `diagnostics.py` are currently identical
in both.

---

## Running it

**Set it up once**, then start it from the desktop like anything else.

1. Install **Python 3.10 or newer**, with tcl/tk. Nothing else is needed.
2. Double-click **`run_rov_flight_ops.bat`** once and let it finish. It builds
   a private Python environment in
   `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv` and installs what the program
   needs — a few minutes, and it needs the internet that once.
3. Double-click **`Create Desktop Shortcut.bat`**. That puts
   **ROV Flight Operations** on the desktop, with the ROV icon.

After that, start it from the desktop shortcut. Steps 1 and 2 have to happen
first: the shortcut starts the program, it does not install it. (If you make
the shortcut before installing, the first click will do the install and show
you a window while it does.)

The shortcut needs **no administrator rights**, and it works out where the
repository is from its own location — so a checkout under Dropbox, OneDrive or
anywhere else with spaces in the path is fine, and nothing is written down that
can go stale. **If you move or rename the repository, run
`Create Desktop Shortcut.bat` again**: it replaces the shortcut it made last
time rather than adding a second one, so running it twice is the same as
running it once.

The shortcut points at `launch_rov_flight_ops.vbs`, which is what keeps a
command prompt off the taskbar for the rest of the day. What that gains in
tidiness it must not lose in diagnosability, so:

| | |
| --- | --- |
| **normal start** | no console. Everything the launcher prints goes to `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\launch.log`. |
| **first start, or a rebuild** | shown in a window, because it takes minutes and is worth watching. |
| **a start that fails** | a message naming `launch.log`, and an offer to start again with the window visible so the error can be read. |
| **debugging** | `run_rov_flight_ops.bat --console` runs with the console Python and leaves the window open, whatever happens. |

Anything that goes wrong once the window is up is in the diagnostics log
either way — see [Diagnostics](#diagnostics-and-reporting-a-problem).

The *Analyze transects* tab also uses the transect extractor in
`../mcap_to_csv`; the launcher installs it, and stops with an error if that
install fails rather than leaving a program that cannot extract transects.

The launcher installs the dependency versions in **`constraints.txt`** — the set
this was last tested with — so a new laptop does not get whatever is newest that
day. Startup never upgrades anything. If an environment is ever broken, run
`run_rov_flight_ops.bat --repair` to reinstall into it.

The desktop icon is drawn by **`assets/make_rov_icon.py`** and lives in
`assets/rov_flight_ops.ico`. It is an original drawing of *our* vehicle seen
head on — the red enclosures either side of the centre tube, the red tape
across the float blocks, the camera dome, and the two lights pointing down and
lit. It carries eight sizes from 16 to 256 pixels, and **each size is drawn
rather than scaled down from one bitmap**: at 16 px the icon is about ten
usable pixels of vehicle, and anything reduced to that from a detailed drawing
is a smear. Below 32 px it becomes a different, simpler drawing of the same
vehicle — one dark body, two red bars, two red blocks, the dome, two lights.

The script writes **seven options** to `assets/icon_options/`, with preview
sheets showing each at every size on a light, mid and dark desktop
(`1_at_256px.png`, `2_small_on_*.png`). To change which one the shortcut uses:

```bash
python assets/make_rov_icon.py --pick clean
```

then run `Create Desktop Shortcut.bat` again to pick up the new icon (Windows
caches icons per shortcut).

| option | |
|---|---|
| `faithful` | every part, including the thrusters |
| **`bold`** | the same with a keyline round each part — **this is the one in use** |
| `clean` | no thrusters; the calmest of the plain ones |
| `minimal` | the small drawing used at every size, so 256 and 16 are the same picture |
| `badge_blue`, `badge_teal` | on a rounded plate, which guarantees contrast on any wallpaper |
| `no_beams` | lights off, if the glow ever reads as a fault |

The keyline is what `bold` is for: flat fills that touch each other blur into
one shape when the icon is reduced, and a dark line between them survives the
reduction and holds the parts apart.

**`run_netcheck.bat`** is the stand-alone topside network check (which adapter
carries the tether, whether it is a bridge, whether Windows may power it down).
Run it once with the tether connected, before a dive.

From a terminal: `python -m rov_flight_ops` (in the environment above).

---

## The window

* **Tabs across the top** are the navigation, numbered in the order a survey
  day uses them. There is no chapter rail any more — its width goes back to the
  work.
* **The title banner folds away.** The **▲** button to the right of the
  *Dark mode* switch hides the logo, title and attribution; the gradient bar,
  the switch and the button stay and rise to the top of the window. **▼**
  brings the banner back.
* **The banner carries the three software versions** — BlueOS, ArduSub and
  Cockpit — read off the vehicle once a minute while it is connected. Open,
  they are a column beside the title; folded, a row to the left of the lamps.
  A version already read is not erased when the tether drops: the tether comes
  and goes all day and the vehicle has not changed underneath it. A dash means
  nobody has been able to read it yet. Cockpit is normally flown from the
  topside laptop rather than served off the vehicle, so it is looked for on
  this machine when the vehicle has nothing to say about it.
* **Two lamps**, *Vehicle connected* and *Logging*. Open, they sit under
  Diagnostics and the appearance switch; folded, they are in the same row. Each
  has three states rather than two, because there genuinely are three:
  **grey ○** nothing, **green ○** on and waiting, **green ●** happening now.
  *Logging* is a ring while the recorder is watching for the ROV to arm and a
  filled dot once it is writing rows — which is the difference between two
  transects and a recorder that has stopped. *Vehicle connected* goes out when
  the vehicle stops answering, which `armed` alone can never show: that holds
  its last value, so a vehicle unplugged after a disarm reads "disarmed"
  forever.
* **Output boxes start one line tall and are dragged open.** The shared log at
  the foot of the window has a handle on its top edge; every section with a
  report box has a handle on its bottom edge. Drag to resize; double-click the
  handle to jump between one line and a comfortable height. They never shrink
  below one line, and the progress bar and status line are never inside the
  part that shrinks.
* **Small sections sit side by side** where they are short enough to.
* **The recorder's state is on every tab**, at the right of the tab row:
  *Watching 192.168.2.2 for arming*, *● Recording … · rows · host*, or a warning
  in words if recording has failed — so a failure is not waiting to be noticed
  on the Monitoring tab while someone is downloading logs.

---

## 1  Monitoring

Where the operator sits during a flight.

### 1. Flight folder

Choose it **first** — before the dive, even before the ROV is connected. The
flight recorder, the network checks, the transect plan, and every download from
the Pi are filed inside it. Choosing it also starts watching for the ROV to arm
(one small read every two seconds; nothing is recorded until it arms).

### 2. The path to the vehicle  ·  3. Recording this flight

Side by side.

**The path to the vehicle** holds the **vehicle address** — the one place it is
typed. Blank means the tether address, `192.168.2.2`. It is **committed** when
you press Enter or leave the box (and before any button reads it), and then
applies everywhere at once: the recorder's watcher, the BlueOS logs tab and the
preview. A listing from the old address is cleared. If a flight is being
recorded when the address changes, that flight keeps its vehicle and the new
address applies as soon as it closes. *Check the network*, *Measure the link*
and *Save to flight folder* are the pre-dive topside checks.

**Recording this flight** starts itself when the ROV arms and closes when it has
been disarmed for 90 seconds, so a surface interval does not split one dive into
several files. *Record now* starts one by hand (a bench test, or a vehicle whose
arm state cannot be read). Everything lands in the flight folder's `logs/`.
Under the buttons it shows the **vehicle it is watching**, the **file it is
writing**, **how long ago a row last reached the disk**, and the **row rate
actually achieved**. Specifically:

* **Flight IDs are to the second** (`2026-09-16_104512`), with a suffix if one
  would ever repeat, and every flight file is created exclusively — a second
  recording can never overwrite the first.
* **A failed write is shown as a failure.** If rows stop reaching the CSV (a
  full or removed drive), the status reads *RECORDING FAILED* with the reason
  and the number of rows lost, on this tab and on every tab's status line. The
  recorder keeps trying; if the disk recovers, the alarm clears and the flight
  record notes how many rows were lost. Rows are synced to disk every 10 s.
* **A flight keeps its folder.** Choosing a different flight folder while a
  flight is being recorded asks first; the open flight finishes in its own
  folder and only the next one goes to the new folder.
* **The buttons do not wait on the recorder.** *Start monitoring*, *Record now* and
  *Stop* hand the work to the recorder's own thread and return at once; the
  button reads *Starting monitoring…* or *Closing…* and is greyed out until it
  is done, so a second press cannot queue the opposite. Closing a flight reads
  every parameter off the vehicle; against a vehicle that is not answering that
  read is given **90 seconds** and then abandoned, and the flight record says
  so. The status also shows when rows were last **synced** to disk (written is
  not the same as on the drive).
* **A worker that does not stop is reported, not assumed stopped.** If the
  sampler or the network trace is still stuck inside a read when a flight
  closes, its files are closed by it when the read returns, and the Monitoring
  card and every tab's status line say *a recorder worker is stuck*. A new
  flight still records normally — it has its own workers and files — but
  restart the program when convenient.

### 4. Live monitoring

One group of readings at a time, each on its own scale, drawn while a flight
records. Under each reading's name is its unit and the **rate it is meant to be
refreshed at** — a target: the rate actually achieved for the row is measured
and shown in section 3. The row is written once a second, but some readings are
taken on a slower cadence and held between reads — a strip that steps every five
seconds is doing exactly what it should. A vehicle-side tether reading that has
not been refreshed for three of its polls is shown as unknown rather than held
forward:

| reading | target |
|---|---|
| everything not listed below (CPU, memory, GPU, storage, network rates, ping, Cockpit, carriers…) | **1 Hz** |
| `rov_armed`, `rov_reachable`, `rov_http_ms` | 0.5 Hz (every 2 s) |
| `pi_soc_temp_c`, `pi_eth_rx_bytes`, `pi_eth_rx_errors`, `tether_link_mbps` | 0.2 Hz (every 5 s) |
| `battery_discharge_w`, `ethernet_connected`, `ethernet_link_speed_mbps` | 0.2 Hz (every 5 s) |
| `rov_arp_ok` | 0.1 Hz (every 10 s) |

**Faster than 1 Hz:** while a flight records, the tether is also traced at
**10 Hz** (adapter counters, `logs/network_fast_*.csv`) and **5 Hz** (pings with
their status codes, `logs/network_pings_*.csv`). Those are written for reading
afterwards — the Network and Tether groups say so — and are not drawn live.

The chart windows are 2, 10 and 30 minutes; the history kept for drawing now
covers the full 30.

---

## 2  Transects

Straight after the flight, with the vehicle on deck and disarmed.

### 1. Sites and transects

Type the transect times (TC-25, as written on the slate — six digits, the colons
are added for you) and press **Save**. The plan is written into the flight
folder as `surveys.json`; it is what every later step — and ROV Imagery
Processing — reads.

#### Pauses

Sometimes the vehicle is on transect and something goes wrong — Cockpit
disarms, the video glitches, a minute goes on getting the ROV back where it
was. That minute is inside the transect but was not surveyed, and its imagery
must not be filed as though it were.

**+ Add pause**, under each transect's *end* box, adds a start and an end
beside the transect's own times. Add as many as a transect needs. The row then
reads, for example, *10.5 min surveying · 1.5 min paused*.

A pause has to be inside its transect, has to run forwards, and cannot overlap
another; anything else is an error on the row rather than something quietly
clipped, because a pause typed against the wrong transect is the mistake most
worth catching. Times run on the transect's own clock, so a transect through
local midnight carries its pauses with it.

What a pause changes, everywhere:

| | |
| --- | --- |
| **GoPro stills** | a frame taken during a pause matches no transect, so it is handled as off-transect — kept in `off_transect/`, or left behind, by the policy on the import page |
| **GoPro video** | the trimmed 4K source and the telemetry composite skip the paused footage and join across it. The telemetry under the picture skips with it, so the overlay stays on the frame it belongs to |
| **per-transect CSV** | every row is **kept** and marked in the new `Survey_state` column (`transect` / `pause`), so an analysis can filter and a check on the recording still sees an unbroken stretch of telemetry. `Distance` counts only the surveying rows |
| **1 Hz telemetry CSV** | same, in a `survey_state` column |
| **C3 imagery** | **not** affected. It feeds the photogrammetry models, where more coverage is simply more to work with |
| **downloads, and the dive profile** | not affected: which recordings cover a transect is a question about when the flight happened, not about what was surveyed |

A transect with no pauses behaves in every respect exactly as it always did,
and a `surveys.json` written before pauses existed opens unchanged.

### 2. Preview the transects

Draws the dive profile with the transects marked: the gut check that the
transects were flown and recorded where they were written down. **It works
before anything is downloaded.** *Read from*:

* **Automatic** (default) — the flight folder if it has what is needed,
  otherwise the vehicle.
* **Flight folder** — the recordings in `logs/mcap` (or `logs`), or failing
  those an autopilot log in `logs/BIN`.
* **Vehicle** — the autopilot's own dataflash log (`.BIN`) straight off the Pi.

Why the `.BIN` and not the mcap from the vehicle: the mcaps on the Pi carry
their video inside them — gigabytes — while the depth trace needs a few
megabytes. A `.BIN` has no wall clock on a vehicle without GPS, only time since
boot, so it is **placed by its file modification time** (when it last grew — the
end of the flight). That is close, not exact, and the preview says so when it
has used it. It is only ever used for this preview, never to cut CSVs or imagery.

A `.BIN` copied into a flight folder by hand carries the time it was *copied*,
not recorded (measured on the 2 September flight: hours out), so the flight
folder fallback only reads `logs/BIN`, where this program's downloads put them
with the vehicle's own time preserved.

---

## 3  BlueOS logs

See what is on the Pi, download it into the flight folder, and clear old files
off it.

### 1. Files

Tick the file types, then **Search for files**:

The types are in the order a survey day reaches for them — mcap and BIN come
down after every flight without anyone thinking about it, C3 imagery and video
are the next most likely, and tlog is last because the BlueOS releases we fly
no longer write one at all:

| type | on the vehicle (BlueOS File Browser path) | downloads to |
|---|---|---|
| **mcap** | `/system_root/usr/blueos/userdata/recorder/*.mcap` | `logs/mcap/` |
| **BIN** — the autopilot's dataflash logs | `/ardupilot_logs/firmware/logs/*.BIN` | `logs/BIN/` |
| **C3 imagery** — MarineSitu C3 via Madrona | the folder chosen in Madrona (see below) | `photos/C3/` (left/, right/, center/, calibration) |
| **video** — the forward camera, extracted | `…/recorder/<recording>/*.mp4` | `logs/mcap_video/<recording>/` |
| **tlog** — older BlueOS releases only | `/ardupilot_logs/logs/**/*.tlog` | `logs/tlog/` |

**video** is the same stream that is also inside the mcap. It is called just
*video* here because on this page it is a file type to tick, and "mcap video"
read as though it were part of the mcap row above it. Its folder on disk keeps
the name it has always had, so flight folders already filed do not have to
move.

The list shows **totals first** — how many files of each type and how much room
they take, when they were recorded, which transects they cover, and how many are
already in the flight folder. **List individual files** opens the totals up into
the files (C3 imagery opens folder by folder, because it is thousands of files).

**Click to select** a whole type, a folder, or single files; click again to
unselect; Shift-click for a range. Selected rows are highlighted, and the count
and size of the selection is shown under the list. **The selection is the
"manual selection"** the two sections below use.

"Recorded" is judged on content, never on the file name or date — BlueOS
rewrites old recordings when it repairs them:

* **mcap** — start and end from the recording's own summary (a few hundred
  bytes read from the end of the file). A recording the vehicle never closed
  has no summary; its end is **estimated** from its size and marked **≈**.
* **BIN** — its first and last timestamps, placed by its file time.
* **tlog** — its first packet; **C3** — each image's file time.

Times are the vehicle's clock; if it disagrees with the laptop by more than two
minutes the list says so, because *Transects only* matching will be off by the
same amount.

The **In flight folder** column says **verified** only for a file this program
downloaded (every byte arrived and its SHA-256 was recorded, and the copy is
still that size), **same size (unverified)** for a same-size file with no such
record — copied by hand, say — **copied, still recording** for a log the vehicle
has gone on writing since (below), and **DIFFERENT SIZE** when the copy does
not match.

**C3 folder on the Pi.** Madrona saves "underneath the folder you select", so
there is no fixed path. Leave the box blank and the search looks for folders
holding `left/`, `right/` and `center/` under `/system_root/usr/blueos/userdata`,
`/system_root/usr/blueos/extensions` and `/system_root/root`. **Only those exact
folders are listed** — each one's `left/`, `right/`, `center/` and the
calibration file beside them, and nothing else in or near them. Several sessions
are listed separately and keep their own subfolders under `photos/C3/`. Or type
the folder as BlueOS's File Browser shows it: it must be a left/right/center
folder, hold them, or hold sessions that do; a broad folder like `userdata` is
refused rather than walked.

### 2. Download files  ·  3. Clean the Pi

Side by side, and chosen the same way: **File types**, crossed with a **Time
period**.

The two sections offer different periods, because they are used at different
moments. Downloading happens on the boat, straight after surfacing, with the
ROV still powered. Clearing happens at the *start of the next survey day*, once
the last day's files have been checked in the lab.

| Time period | on | what is used |
|---|---|---|
| *This flight* | Download | files whose recorded span falls in the **last stretch of recording on the vehicle**. Within a flight the vehicle may disarm, be re-armed, crash Cockpit, be rebooted or be power-cycled, and each of those starts a new mcap and a new BIN — but none of them takes five minutes. So recordings more than five minutes apart are different flights, and this takes the last group. Worked out from the mcaps and BINs; the C3 imagery and video of that flight come with it |
| *Today* | Download | everything recorded since local midnight |
| *Previous day* | Clean the Pi | the **last day before today that has any files on the vehicle** — not literally yesterday. Survey days are not consecutive, and offering to clear yesterday after a fortnight ashore would pick nothing and look broken |
| *All files* | both | every file of the types ticked |
| *Transects only* | Download | files whose recorded span overlaps a transect (±2 min). Recordings whose end time is only estimated (≈) are left out |
| *Manual selection*, or nothing chosen | both | only the files selected in the list above |

Anything selected in the list above is added to whatever a period chose. The
dated periods say what they worked out — *this flight: 09-16 14:02 to 15:31 —
the last of 2 on the vehicle* — under the panel, **before** the button is
pressed, because a period that landed on the wrong hour would otherwise only be
visible once the files were gone. All of them allow for the vehicle's clock
being out against the laptop's, and say so when it is.

*Transects only* is deliberately **not** offered for Clean the Pi: what is
being cleared is a day's files, not a transect's, and it would leave the
between-transect recordings behind.

The file types work **regardless of what the list above is showing** — a type
that has not been searched yet is listed first, automatically. A live line
under each section says exactly what the button will touch before it is
pressed.

**A log that is still being written.** The autopilot appends to its dataflash
log for as long as the ROV has power — and the ROV has to have power for any of
this to work at all — so a BIN is *bigger* by the time it has finished coming
down than the listing said it would be. Insisting the two match refused every
BIN outright. A copy that arrives **long** is therefore checked against the
vehicle rather than rejected: the file is listed again, and a copy that ends
inside what the vehicle now holds is kept, recorded at the size that actually
arrived, and marked. Such a file shows as **copied, still recording** next time
the vehicle is listed, and downloading it again once the vehicle has finished
with it gets the whole thing and verifies normally. Deleting one is warned
about by name. A copy that arrives **short** is still a failure — that is a
truncated download, which is the thing the check exists to catch.

**Download files** copies into the flight folder chosen on Monitoring, each type
into its own folder (table above). Each copy is written as `.part`, synced, and
renamed only once its size is accounted for; mcaps are checked to begin like an
mcap;
each file keeps the vehicle's modification time; and each completed copy is
recorded — vehicle path, size, time and SHA-256 — in
`logs/pi_downloads.jsonl`, which is what makes it *verified*. Files already
there and the same size are skipped, and a skipped file that was not verified
is reported as such. The drive is checked first (free space; a FAT32 drive
refused for files of 4 GiB or more). Nothing on the vehicle is changed. After a
download the flight folder is re-read, so Flight summary and Analyze transects
see the new recordings straight away.

**Clean the Pi** deletes from the vehicle, and it cannot be undone. It is guarded:

* **Only a confirmed, current "disarmed".** The vehicle's heartbeat is read until
  mavlink2rest's message counter advances, so a stale heartbeat cached from an
  autopilot that has stopped talking cannot pass as current. Armed, no answer,
  an unchanging heartbeat, or a mavlink2rest that does not report its counter
  all refuse the whole batch. During a long batch the check is repeated every
  10 s, and the batch stops the moment it fails.
* **Fresh metadata, checked immediately before deleting.** Each target's folder
  is listed again. A file that is gone, has changed size or time since it was
  listed, has no modification time, or was modified in the last two minutes (by
  the vehicle's clock) is left alone and the reason recorded. If the vehicle's
  clock cannot be read, nothing is deleted.
* **Only what belongs to its type**, one file at a time by the exact path it was
  listed under, and only inside the exact folder its type was listed from. A
  folder is removed only once a fresh listing shows it empty, and a type's own
  root folder never is.
* **A confirmation that names the vehicle**, the count and size by type, and how
  many of the files have **no verified copy** in the current flight folder.
* **A record written before anything is deleted**:
  `logs/pi_cleanup_<date>_<time>.txt` in the flight folder (or
  `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\pi_cleanup\` with no flight folder)
  is created, and the list of approved targets written and synced, before the
  first request. Each outcome — deleted, kept (with why), failed, not tried — is
  appended and synced as it happens. If the record cannot be created, nothing is
  deleted; if it cannot be written part way, the batch stops.

The intended workflow: download every flight's logs the same day; check them in
the lab; clear them off the Pi before the next flight. BlueOS's recorder sweeps
old recordings for repair even while the vehicle flies, and large mcaps with
embedded video cost the Pi bandwidth doing it — an empty recorder folder gives it
nothing to sweep.

`pifiles.py` is the **only module that can change the vehicle**; every other
module issues read-only GETs, and a test fails if a write appears anywhere else.

---

## 4  Flight summary

**Read the day** builds the flight report (PDF) from everything in the flight's
`logs`; **Recordings / What the check found / Autopilot logs / Transects** are
UTC's *Recording health* — damage checks, repaired copies, and using a `.BIN`
for telemetry when an mcap failed. It finds recordings in `logs/mcap` as well as
`logs`.

How a recording's ending is described is limited to what the logs show:

* **gcs failsafe** — the autopilot's own failsafe message, in the last 30 s of
  the recording. A failsafe earlier in the dive that cleared is not taken as the
  reason the recording ended. A timeout in seconds is quoted only when the
  flight's recorded parameters include `FS_GCS_TIMEOUT`.
* **disarmed** — a properly closed recording armed to its end with no message:
  the vehicle disarmed, and nothing recorded why. (Earlier versions called this
  "operator", which claimed more than the logs show.)
* **unexplained** — anything else, including a recording the vehicle never
  closed, where an interrupted recorder and a disarm look alike.

## 5  Analyze transects

Unchanged from UTC for now: per-transect CSVs (with tide-corrected depth) and a
map via the transect extractor, and a sensor-health report.

**Fallback: dead reckoning from the vehicle's own origin.** `DVLlat`/`DVLlon`
are normally seeded from the dive's first GPS or EKF fix. When a dive has
neither anywhere in it -- confirmed on 2026-09-17 at both Sirens of Spring and
EBM West, where `ORIGIN_LAT`/`ORIGIN_LON` were set correctly before arming
each time but the EKF never logged adopting an origin (no `ORGN` event; see
`binlog.read_origin_params`) -- this page reads whichever of the flight's own
`.BIN` logs has an origin, ORGN-confirmed or just the raw parameters, and
passes it to the extractor as `manual_origin`. The output warns when the
origin was never actually confirmed by the autopilot, because the resulting
map is dead reckoning from that starting point, not a verified fix: right
relative to itself, but the whole track can sit off the true location and
rotates with any compass error. See `mcap_to_csv/ccr_m2c/transect.py`'s
`georeference_dvl` for the mechanics, and the OTS write-up from that date for
why the origin was never applied in the first place -- most likely the script
that turns those two parameters into a real EKF origin is no longer on the
vehicle's SD card.

---

## The flight folder

```
2026_09_16_Centennial/
    surveys.json                 the transect plan (Transects > Save)
    logs/
        mcap/                    recordings downloaded from the Pi
        mcap_video/<recording>/  the forward camera, extracted by BlueOS
        BIN/                     autopilot dataflash logs
        tlog/                    telemetry logs (older BlueOS)
        laptop_monitor_*.csv     the topside, 1 Hz        } written by the
        network_fast_*.csv       adapter counters, 10 Hz  } flight recorder
        network_pings_*.csv      pings, 5 Hz              } during each flight
        network_topside_*.txt    the network at arming    }
        flight_*.json            parameters, versions and what changed
        pi_downloads.jsonl       every download: source, size, time, SHA-256
        pi_cleanup_*.txt         what Clean the Pi approved, deleted and kept
    photos/
        C3/                      C3 imagery from the Pi (left/, right/, center/, calibration)
```

Flight files are named by flight ID — `laptop_monitor_2026-09-16_104512.csv` —
and older flights named to the minute (`…_1045.csv`) are still read and linked.
Older flights with recordings loose in `logs/` are read exactly as before.

## Settings and cache

* `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\settings.json` — the vehicle address
  and the C3 folder, remembered between runs.
* `%LOCALAPPDATA%\utc_cache\` — extracted telemetry, per flight. This location
  is shared with UTC and ROV Imagery Processing **on purpose**: it is data, not
  code, and sharing it means a `.BIN` telemetry override chosen on Flight summary
  is honoured when imagery is bannered, and gigabytes are not extracted twice.
  So that sharing is safe:
  * a cached extraction is used only when its marker records the **same source
    files — path, size and modification time — and the current cache schema**,
    and every product file is present. A recording recopied, repaired or still
    growing at the same path is re-extracted, not read from the old cache.
  * the marker is removed before extraction and written last, atomically, so an
    interrupted extraction is a cache miss rather than a half-written table.
  * one program at a time extracts a flight: an **operating-system lock** on
    `extract.lock`, held for the whole extraction and released when it ends —
    or when the program ends, however it ends. Nothing is inferred from how
    long ago progress was reported, and the lock file is never deleted.
  * *Stop* during an extraction takes effect within a few thousand messages;
    the half-built cache is left without its marker, so it is never used.
  * **UTC's extractor takes no lock and does not check fingerprints.** A cache
    it writes is rebuilt once by these programs, and it can read theirs — but
    do not extract the same flight in UTC while this program or ROV Imagery
    Processing is extracting it.

## Diagnostics and reporting a problem

The program keeps its own log, on this laptop's disk and never on the flight
drive, in

```
%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\diagnostics\
```

The **Diagnostics** button at the top right, beside *Dark mode*, opens it.
(If that folder cannot be written, the program falls back to
`%TEMP%\CCR_ROV\rov_flight_ops\diagnostics\`, and failing that runs without
one — it never stops recording over it.)

* `app.log` — start-up facts (program version and git commit, Python,
  Windows), every job started, stopped and finished with its duration, every
  recorder transition (*starting a recording*, *closing flight …*, how it
  closed), and **every unexpected error with its traceback** — including errors
  in button callbacks and in background threads, which used to vanish because
  the program has no console. Repeats of the same error are counted rather
  than logged in full. Rotated at 1 MB; five old files kept.
* **Stalls.** If the window stops responding for 8 seconds, the stacks of
  every thread are written to `app.log` once, with what the program said it was
  doing, and the stall's length is logged when it recovers.
* `faults.log` — what Python writes if the interpreter itself crashes: the
  stack of every thread at the moment of the crash.
* Vehicle File Browser tokens, and anything else that looks like a credential,
  are replaced with `<redacted>`. Nothing is sent anywhere.

**After a freeze or a crash**, before starting the program again if you can:

1. Note the time, and what you were doing (which tab, which button, recording
   or not, was the tether up).
2. Press *Diagnostics* (or open the folder above) and send `app.log`,
   `app.log.1` and `faults.log`, with the flight's `logs/` folder if a flight was
   being recorded.
3. If Windows showed "not responding" and you ended the program, say so: that
   leaves no fault dump, only the stall report in `app.log`.

**What this cannot catch.** A power cut, a killed process, or Windows ending a
program that was "not responding" leave no fault dump. A stall that also stops
Python's own threads (deep inside a driver, say) cannot be reported by the
watchdog, and appears only as a gap in `app.log`.

---

## To confirm on Nereo (first on-site test)

These are written against BlueOS's documented behaviour and a fake vehicle in
the tests; each wants one look at the real thing:

- [ ] **C3 folder** — does the search find Madrona's folder? If not, type it in
      (File Browser path) and confirm left/right/center + calibration list.
- [ ] **mcap video** — are the extracted MP4s in `recorder/<recording>/`?
- [ ] **tlog** — expected absent on current BlueOS (the list says so); fine.
- [ ] **Clean the Pi** — delete one old, already-downloaded file; confirm it is
      gone in BlueOS's File Browser and the record file is written. Then arm the
      vehicle and confirm the delete is refused.
- [ ] **Heartbeat freshness** — with the vehicle on and disarmed, Clean the Pi
      must *not* refuse with "does not report whether its heartbeat is current".
      If it does, this vehicle's mavlink2rest lacks the `status.time.counter`
      block and deleting will need another freshness check before it can work.
- [ ] **mcap end times** — list mcaps; closed recordings should show end times
      without ≈. One still being recorded (or never closed) should show ≈.
- [ ] **Failure cases worth one try each** (from the 13 September review):
      pull the tether while disarmed and try Clean the Pi (refused); change the
      vehicle address while watching (the Monitoring card shows the new host);
      choose a second flight folder mid-recording (asked, and the flight
      finishes in its original folder).
- [ ] **Preview from Vehicle** — after a flight, preview with *Vehicle*, then
      download the mcaps and preview with *Flight folder*; the two traces should
      sit within a few seconds of each other.
- [ ] **Rates** — arm, confirm charts fill and the Hz labels read sensibly.
- [ ] **Responsiveness** (from the 14 September review), with Cockpit and the
      cameras running: Start/Stop monitoring and Record now/Stop several times
      each — the window keeps redrawing and the buttons grey out while each
      runs; a brief disarm and re-arm (one flight); a long disarm (closes on
      its own); pull the tether mid-recording and press Stop (closes within
      about two minutes, record notes the abandoned snapshot); close the window
      mid-recording (the window stays up while it closes, then goes); open a
      large C3 listing with *List individual files*. Afterwards, open
      *Diagnostics* and check `app.log` has no stall reports or errors you did
      not expect, and read the achieved row rate on Monitoring.

---

## Development

```
rov_flight_ops/
    run_rov_flight_ops.bat   launcher (builds its own environment on first run)
    run_netcheck.bat         topside network check
    launch.py                entry point for a packaged build
    pyproject.toml           package and dependencies
    assets/                  fonts, logos, icon
    rov_flight_ops/          the package
        gui/
            app.py           the window and its five tabs
            shell.py         banner fold, tabs, resizable output (same file as imagery's copy)
            widgets.py       cards, resize grips, site/transect editors
            monitorpage.py   Monitoring sections 2-4
            transectsetup.py Transects tab
            logspage.py      BlueOS logs tab
            summarypage.py, healthpage.py   Flight summary
            transectpage.py  Analyze transects
        pifiles.py           Pi files: list, spans, choose, download, delete
        diagnostics.py       app.log, faults.log, stall watchdog (same file as imagery's copy)
        previewsource.py     where the transect preview's depth comes from
        telemetry_cache.py   the telemetry reader (the flight-ops part of UTC's pipeline)
        blueos.py, flightlog.py, laptop.py, netdiag.py, nettrace.py, ...
    tests/
```

```
python -m pytest            # hermetic tests, including a fake BlueOS vehicle
python -m pytest --runlive  # also the scripts that need real data or a display
```

`tests/test_review_fixes.py` reproduces each failure case from the 13 September
2026 independent evaluation and holds the repaired behaviour;
`tests/test_resilience.py` does the same for the 14 September responsiveness
review, with event barriers rather than sleeps, and real subprocesses for the
cache lock and the crash log. The GUI tests use
a temporary `LOCALAPPDATA`, so they never touch this laptop's settings or
cache, and only skip when there is genuinely no display.

CI: `.github/workflows/rov-programs-ci.yml` lints and tests this program (on
Python 3.11 and 3.13, and again with `constraints.txt`) and ROV Imagery
Processing, on Windows, whenever either folder or the extractor changes.

**Not yet done:** a packaged `.exe` build for this program (there is a
`launch.py` entry point but no spec or build check), and a telemetry-only
extraction mode for the preview — extraction still writes the ROV video stream,
which costs disk work the preview does not need.

## Changes after the 13 September 2026 review

An independent evaluation of this program found ten defects. Each was checked
against the code, confirmed, and fixed:

| | finding | what changed |
|---|---|---|
| F01 | Clean the Pi could delete when the arm state was unknown, from a stale heartbeat, or from a stale listing | current-heartbeat confirmation, re-checked during the batch; fresh metadata per file; unreadable clock refuses |
| F02 | automatic C3 discovery could sweep in unrelated files | exact C3 folders only; broad typed folders refused; deletes re-check containment |
| F03 | two recordings in one minute overwrote each other | IDs to the second, unique per folder, files created exclusively |
| F04 | a disk-write failure left the recorder looking healthy | visible *RECORDING FAILED* with rows lost, last write time, periodic sync |
| F05 | switching flight folder mid-flight split one flight's files; old plan and Analyze folder persisted; downloads not rescanned | fixed session folder; confirm before switching; plan reset; Analyze follows; rescan after download |
| F06 | a replaced or growing mcap could be read from a stale cache | fingerprinted, schema-versioned, atomically published, locked cache |
| F07 | a changed vehicle address did not reach the running watcher | one committed address, applied to the recorder (deferred mid-flight) and the logs tab |
| F08 | mcap end times were size estimates treated as known | read from the mcap summary; estimates marked ≈ and never used to choose deletions |
| F09 | "already copied" was size only; the deletion record was written after the fact | SHA-256 download manifest and *verified* status; write-ahead, synced deletion record |
| F10 | the report called unexplained endings "operator" and matched failsafes anywhere in a recording | evidence-limited causes; failsafe must be at the ending; timeout quoted only when recorded |

Also from the review: a CI workflow for this program, a launcher that detects and
repairs a missing extractor, a tested `constraints.txt`, the `pywin32` platform
marker in `requirements.txt`, stale tether readings shown as unknown, and a
tether-diagnostics probe that is retried rather than given up on for the whole
flight. One defect the review did not flag was fixed at the same time: the split
had renamed the flight record's schema label to `rov_flight_ops.flight/1`; it is
back to `utc.flight/1`, the same as UTC's.

## Changes after the 14 September 2026 responsiveness review

A second review looked for causes of freezes and of one crash. Its seven
findings were each checked against the code and confirmed. The crash itself
was not diagnosed — nothing from the incident was available — so the work is
in two halves: remove the freezes and races that were found, and make sure the
next incident leaves evidence.

| | finding | what changed |
|---|---|---|
| R1 | Start/Stop monitoring, Record now/Stop and closing the window did hardware setup, thread joins and the closing vehicle snapshot inside the button callback, freezing the window — unbounded against a vehicle that was not answering | a recorder lifecycle thread carries out queued requests in order; buttons grey out while one runs; the closing snapshot has a 90 s budget; closing the window keeps it responsive and says truthfully if the recorder did not finish |
| R2 | a timed join was treated as a stopped thread: a restart cleared a still-running worker's stop signal, a late opening snapshot could land in the next flight, and the counters, ICMP handle and trace files could be closed beneath a worker still using them | every watch and every flight has its own stop signal, workers and files; one path closes a flight; resources are closed by the thread that uses them; a stuck worker is reported as *degraded* |
| R3 | one exception in a result handler stopped the job queue for good, and a finished job's queued result could be handed to the next job's callback | per-job ids and callbacks; a job is running until its result is delivered; the queue is serviced in a protected, time-bounded pass with progress coalesced; a Stop is reported as a stop, not an error |
| R4 | no record of callback errors, thread deaths or stalls under `pythonw` | `diagnostics.py`: rotating `app.log`, `faults.log`, a stall watchdog, credential scrubbing, and the Diagnostics button — see [Diagnostics](#diagnostics-and-reporting-a-problem) |
| R5 | Stop in Analyze transects waited for the whole site; extraction ignored Stop | Stop reaches inside extraction and the transect extractor at its next progress report; partial caches are never marked valid |
| R6 | choosing a flight folder, and every visit to BlueOS logs, walked the disk on the window's thread; the output pane re-read itself on every line and grew without limit | folder scans and copy checks run in the background, and a scan for a folder already left is dropped; large folders open in batches; the output pane keeps its last 2,000 lines (the log keeps everything) |
| R7 | the extraction lock went "stale" after two minutes without progress, so a paused extractor could lose it and then delete its successor's lock | an operating-system lock, released by the OS if the owner dies, never deleted |

Measured on this development laptop (not the field laptop) with the real
counters and an unreachable vehicle address: the worst gap between runs of a
50 ms window timer was about 80 ms while recording, and 155 ms across a
100-second Stop whose closing snapshot got no answer. That is a check on this
machine, not a guarantee for another.

## Changes after the 16 September 2026 field test

The first round of changes driven by running the program on a survey day
rather than at a desk.

| | what was wrong | what changed |
|---|---|---|
| F1 | opening the window, the report box in *The path to the vehicle* twitched back and forth — the first thing anyone saw | CustomTkinter re-decides five times a second whether a scrollbar is needed, and asks the question of the lines *on screen*. In a box one line tall that is a loop: showing the horizontal bar costs a line of height, the longest line scrolls out of view, Tk reports nothing to scroll sideways, the bar goes, the line comes back. Measured off the video at 4.8 changes a second — exactly CustomTkinter's 200 ms poll. The poll is stopped and the question asked of the *text* instead, which a scrollbar cannot change |
| F2 | a transect can be interrupted mid-survey, and the imagery from that stretch was filed as survey imagery | **pauses** — see [Sites and transects](#1-sites-and-transects) |
| F3 | *mcap video* read as part of the mcap row above it | called **video**. Its folder on disk is unchanged |
| F4 | the file types were in no useful order | mcap, BIN, C3 imagery, video, tlog — the order a survey day reaches for them |
| F5 | "(A)" and "(B)" on the two action panels | removed |
| F6 | **every BIN download failed**: *58,484,320 bytes arrived, the vehicle reported 58,479,625* | the autopilot appends to its log for as long as the ROV has power, and the ROV has to have power to download at all. A copy that arrives long is now checked against the vehicle and kept; one that arrives short is still a failure. See [Download files](#2-download-files--3-clean-the-pi) |
| F7 | choosing what to download or clear meant *All files* or picking by hand | **This flight**, **Today** and **Previous day**, each worked out from the files on the vehicle and each showing what it decided before the button is pressed. *Transects only* is gone from Clean the Pi |
| F8 | the vehicle's software versions were recorded in the flight's files but never on screen | BlueOS, ArduSub and Cockpit in the banner, in both its states |
| F9 | nothing said at a glance that the vehicle was connected and the recording was running | two lamps in the banner, with three states each |
| F10 | dragging a window edge left the interface catching up in stuttered jumps | below |
| F11 | the program started from a batch file in the repository, and left a command prompt on the taskbar all day | a desktop shortcut with its own icon — see [Running it](#running-it) |

### F10: what the resizing actually cost

Measured on this development laptop, as the time to service one resize step
(`update_idletasks` after a geometry change) with an hour of monitor history
loaded. A bare CustomTkinter window of the same widget count costs about 19 ms,
which is the floor none of this can get under.

Medians of four interleaved runs; this machine varies by 10–20% between runs,
so the ratio is the honest number rather than any single figure.

| tab | before | after |
|---|---|---|
| Monitoring | 113–146 ms | 44–55 ms |
| Transects | 116–187 ms | 40–55 ms |
| BlueOS logs | 117–161 ms | 37–45 ms |
| Flight summary | 119–131 ms | 37–50 ms |
| Analyze transects | 121–148 ms | 33–37 ms |

Four changes, in the order they mattered:

1. **Only the open tab is laid out.** The five pages shared one grid cell and
   were all still managed, so every resize measured and re-laid-out five tabs
   to show one. They are taken out of the grid now and put back when chosen.
   This was about two thirds of the whole cost.

   The price is that a tab's first appearance pays for its whole layout at
   once — 242 ms against 43 ms, measured — so at start-up the program opens
   each tab in turn, one per turn of the event loop, and comes back to the
   first. That brings the first click back to about 70 ms, and the tabs flick
   past once while the window is starting. It warms them by *opening* them,
   not by gridding and ungridding them without showing them: that shortcut
   lays a page out but never lets it finish mapping, and every widget inside a
   canvas-embedded frame — which is every card on every tab, because each tab
   scrolls — is then left believing it was never shown. The tab opened, its
   geometry was right to the pixel, and it drew nothing at all. Nothing in the
   widget tree gave it away; only a screenshot did.
2. **Repaints driven by `<Configure>` are coalesced** to one a frame, with one
   accurate pass after the last event — so the final size is drawn from the
   size it ended at, not from whichever event landed on a frame boundary. The
   live charts draw at a third of the resolution while the edge is moving and
   at full resolution the moment it stops.
3. **The banner moves instead of redrawing.** A width change moves three
   things — the ground, the gradient rule and the controls — and nothing else
   in it depends on the width. 25.7 ms per frame became 1.5 ms.
4. **CustomTkinter's scrollbars no longer flush the layout** on every redraw
   (`gui/ctk_tuning.py`, which is the one place this program reaches into
   another library's internals, and is written to do nothing at all if a
   future CustomTkinter does not look the way it expects).

The remaining cost is CustomTkinter redrawing each visible widget's rounded
rectangle as it resizes, which is inherent to the framework. Getting under
about 35 ms would mean changing framework, and that is not worth it for this
program.
