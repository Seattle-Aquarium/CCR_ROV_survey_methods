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

The two new programs are **fully self-contained**. Neither imports anything
from the other or from `UTC/`: where both need the same file (the theme, the
survey plan, the telemetry reader…) each folder has its own copy. Each has its
own launcher and its own Python environment, so updating one can never break
another.

---

## Running it

Double-click **`run_rov_flight_ops.bat`**.

The first run builds a private Python environment in
`%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv` and installs what the program
needs — a few minutes, and it needs the internet that once. After that it
starts straight away. You need Python 3.10 or newer installed, with tcl/tk.

The *Analyze transects* tab also uses the transect extractor in
`../mcap_to_csv`; the launcher installs it automatically.

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
typed; everything that talks to the ROV reads it. Blank means the tether
address, `192.168.2.2`. *Check the network*, *Measure the link* and *Save to
flight folder* are the pre-dive topside checks.

**Recording this flight** starts itself when the ROV arms and closes when it has
been disarmed for 90 seconds, so a surface interval does not split one dive into
several files. *Record now* starts one by hand (a bench test, or a vehicle whose
arm state cannot be read). Everything lands in the flight folder's `logs/`.

### 4. Live monitoring

One group of readings at a time, each on its own scale, drawn while a flight
records. Under each reading's name is its unit and **how often it is actually
refreshed**. The row is written once a second, but some readings are taken on a
slower cadence and held between reads — a strip that steps every five seconds is
doing exactly what it should:

| reading | refreshed |
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
rewrites old recordings when it repairs them: an mcap's first chunk, a `.BIN`'s
first and last timestamps with its file time, a tlog's first packet, a C3
image's file time. Times are the vehicle's clock; if it disagrees with the
laptop by more than two minutes the list says so, because *Transects only*
matching will be off by the same amount.

**C3 folder on the Pi.** Madrona saves "underneath the folder you select", so
there is no fixed path. Leave the box blank and the search looks for a folder
holding `left/`, `right/` and `center/` under `/system_root/usr/blueos/userdata`,
`/system_root/usr/blueos/extensions` and `/system_root/root`, fills the box with
what it found, and remembers it. Or type the path as BlueOS's File Browser shows
it.

### 2. Download files  ·  3. Clean the Pi

Side by side, and chosen the same way:

* **(A) File types** — mcap, mcap video, BIN, tlog, C3 imagery.
* **(B) Time period** — *All files*, *Transects only*, or *Manual selection*.

| (B) | what is used |
|---|---|
| nothing chosen, or *Manual selection* | only the files selected in the list above |
| *All files* | every file of the types in (A), **plus** anything selected above |
| *Transects only* | files of the types in (A) whose recorded span overlaps a transect (±2 min), **plus** anything selected above |

(A) and (B) work **regardless of what the list above is showing** — a type that
has not been searched yet is listed first, automatically. A live line under each
section says exactly what the button will touch before it is pressed.

**Download files** copies into the flight folder chosen on Monitoring, each type
into its own folder (table above). Files already there, same size, are skipped.
Each copy is written as `.part` and renamed only once its size matches; mcaps
are checked to begin like an mcap; each file keeps the vehicle's modification
time. The drive is checked first (free space; a FAT32 drive refused for files of
4 GiB or more). Nothing on the vehicle is changed.

**Clean the Pi** deletes from the vehicle, and it cannot be undone. It is guarded:

* **Refused while the ROV is armed.** The vehicle is asked first.
* **Never a file still being written** — anything modified in the last two
  minutes (by the vehicle's clock) is left alone and reported.
* **Only what was listed**, one file at a time by the exact path it was listed
  under. A folder is removed only once a fresh listing shows it empty, and a
  type's own root folder never is.
* **A confirmation that names the vehicle**, the count and size by type, and how
  many of the files have **no copy in the current flight folder**.
* **A record** of every file deleted, kept, or failed, written to
  `logs/pi_cleanup_<date>_<time>.txt` in the flight folder (or
  `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\pi_cleanup\` with no flight folder).

The intended workflow: download every flight's logs the same day; check them in
the lab; clear them off the Pi before the next flight. BlueOS's recorder sweeps
old recordings for repair even while the vehicle flies, and large mcaps with
embedded video cost the Pi bandwidth doing it — an empty recorder folder gives it
nothing to sweep.

`pifiles.py` is the **only module that can change the vehicle**; every other
module issues read-only GETs, and a test fails if a write appears anywhere else.

---

## 4  Flight summary

Unchanged from UTC for now: **Read the day** builds the flight report (PDF) from
everything in the flight's `logs`; **Recordings / What the check found /
Autopilot logs / Transects** are UTC's *Recording health* — damage checks,
repaired copies, and using a `.BIN` for telemetry when an mcap failed. It finds
recordings in `logs/mcap` as well as `logs`.

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
        pi_cleanup_*.txt         what Clean the Pi deleted
    photos/
        C3/                      C3 imagery from the Pi (left/, right/, center/, calibration)
```

Older flights with recordings loose in `logs/` are read exactly as before.

## Settings and cache

* `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\settings.json` — the vehicle address
  and the C3 folder, remembered between runs.
* `%LOCALAPPDATA%\utc_cache\` — extracted telemetry, per flight. This location
  is shared with UTC and ROV Imagery Processing **on purpose**: it is data, not
  code, and sharing it means a `.BIN` telemetry override chosen on Flight summary
  is honoured when imagery is bannered, and gigabytes are not extracted twice.

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
