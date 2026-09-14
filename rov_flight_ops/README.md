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
`gradients.py`) and `mcap_extract.py` are currently identical in both.

---

## Running it

Double-click **`run_rov_flight_ops.bat`**.

The first run builds a private Python environment in
`%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv` and installs what the program
needs — a few minutes, and it needs the internet that once. After that it
starts straight away. You need Python 3.10 or newer installed, with tcl/tk.

The *Analyze transects* tab also uses the transect extractor in
`../mcap_to_csv`; the launcher installs it, and stops with an error if that
install fails rather than leaving a program that cannot extract transects.

The launcher installs the dependency versions in **`constraints.txt`** — the set
this was last tested with — so a new laptop does not get whatever is newest that
day. Startup never upgrades anything. If an environment is ever broken, run
`run_rov_flight_ops.bat --repair` to reinstall into it.

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

| type | on the vehicle (BlueOS File Browser path) | downloads to |
|---|---|---|
| **mcap** | `/system_root/usr/blueos/userdata/recorder/*.mcap` | `logs/mcap/` |
| **mcap video** — the forward camera, extracted | `…/recorder/<recording>/*.mp4` | `logs/mcap_video/<recording>/` |
| **BIN** — the autopilot's dataflash logs | `/ardupilot_logs/firmware/logs/*.BIN` | `logs/BIN/` |
| **tlog** — older BlueOS releases only | `/ardupilot_logs/logs/**/*.tlog` | `logs/tlog/` |
| **C3 imagery** — MarineSitu C3 via Madrona | the folder chosen in Madrona (see below) | `photos/C3/` (left/, right/, center/, calibration) |

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
record — copied by hand, say — and **DIFFERENT SIZE** when the copy does not
match.

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

Side by side, and chosen the same way:

* **(A) File types** — mcap, mcap video, BIN, tlog, C3 imagery.
* **(B) Time period** — *All files*, *Transects only*, or *Manual selection*.

| (B) | what is used |
|---|---|
| nothing chosen, or *Manual selection* | only the files selected in the list above |
| *All files* | every file of the types in (A), **plus** anything selected above |
| *Transects only* | files of the types in (A) whose recorded span overlaps a transect (±2 min), **plus** anything selected above. For **Clean the Pi**, recordings whose end time is only estimated (≈) are left out |

(A) and (B) work **regardless of what the list above is showing** — a type that
has not been searched yet is listed first, automatically. A live line under each
section says exactly what the button will touch before it is pressed.

**Download files** copies into the flight folder chosen on Monitoring, each type
into its own folder (table above). Each copy is written as `.part`, synced, and
renamed only once its size matches; mcaps are checked to begin like an mcap;
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
  * one program at a time extracts a flight: a lock file refreshed while the
    work runs; a lock left by a crash goes stale after two minutes.
  * UTC's own extractor (unchanged) does not check fingerprints. A cache it
    writes is rebuilt once by these programs, and it can read theirs.

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
2026 independent evaluation and holds the repaired behaviour. The GUI tests use
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
