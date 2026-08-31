# MCAP to CSV — Transect Extractor

Turns BlueOS `.mcap` recordings into one CSV per transect, plus a Leaflet map of
every transect at the site.

This is the successor to [`code/tlog_to_csv.py`](../code/tlog_to_csv.py). BlueOS
1.5 records telemetry with its on-board recorder instead of writing `.tlog`
files, so only the ingest step changed: the transect windows, the per-second
averaging, the tide-standardised depth and the output columns are the same, and
the CSVs drop straight into the existing VIAME and percent-cover joins.

---

## Running it

**Double-click `run_MCAP_to_CSV.bat`**, the same way UTC is launched. It finds
your Python install and opens the window.

1. **Add File(s)** or **Add Folder** — pick the `.mcap` files for the dive. Each
   is listed with the local clock time it covers, and a combined span underneath.
   Several files are treated as one continuous dive; BlueOS rolls a new file
   every time recording restarts.
2. Fill in **site name**, **survey date**, **tide station**, and a **save
   location**.
3. Add a **transect** for each one you ran. The Transect ID becomes the CSV
   filename. A transect that was paused and resumed gets two time windows, not
   two transects. Times are local (Pacific), matching the span shown at the top.
4. **Run.** CSVs land in `<save location>/transects/`, with
   `transect_map.html` beside them.

### Loading a survey plan

Rather than retyping the windows, **Load plan (.json)...** reads the same survey
plan the UTC compositing tool uses, so one file drives both and the two cannot
drift apart:

```json
{
  "sites": [
    {
      "name": "Centennial_Park",
      "project": "testing",
      "date": "2026-08-26",
      "transects": [
        { "name": "T1", "start_tc": "12:19:57", "end_tc": "12:28:42" },
        { "name": "T2", "start_tc": "12:29:55", "end_tc": "12:35:15" }
      ]
    }
  ],
  "timezone": "America/Los_Angeles"
}
```

It fills in the site name, the date and every transect. On the command line it
is `--plan plan.json`, which replaces `--site`, `--date` and `--transect`. A plan
covering several sites writes one folder per site, since the map is drawn per
site. Add `--prefix-site` to name the CSVs `<site>_<transect>`.

### From a terminal

```bash
python -m ccr_m2c --inspect logs/*.mcap
```

`--inspect` is the one to reach for on an unfamiliar recording: it prints the
local span, which telemetry the file actually contains, and how complete each
column is — without doing any of the work.

```bash
python -m ccr_m2c logs/*.mcap --site Centennial_Park --date 20260826 --out ./out \
  --transect "EBM_S24_T1=10:07:41-10:13:50,10:35:52-10:40:07" \
  --transect "EBM_S24_T2=10:52:03-11:01:19"
```

Rebuild just the map from CSVs that already exist:

```bash
python -m ccr_m2c --map out/transects/*.csv --out out
```

---

## What changed from the tlog workflow

The container is different but the contents are the same MAVLink stream, so most
of the extraction carried over unchanged. Three things did not, and they affect
how the numbers should be read.

### `LOCAL_POSITION_NED` is not recorded

This is the significant one. In the tlog workflow `DVLx`/`DVLy` came straight
from `LOCAL_POSITION_NED`, the autopilot's own fused local position. Cockpit
does not request ArduSub's POSITION stream, so that message is not in the
recording at all — not under its own topic, and not in the `mavlink/out`
firehose either.

The DVL track is therefore rebuilt one step earlier in the chain, by integrating
**`VISION_POSITION_DELTA`** — the body-frame position deltas the Water Linked DVL
extension feeds to the EKF — rotated into North/East by the `ATTITUDE` yaw of
the moment:

```
North += dx·cos(yaw) − dy·sin(yaw)
East  += dx·sin(yaw) + dy·cos(yaw)
```

Two consequences worth knowing:

- **The track now depends on the compass.** The tlog version deliberately took
  its bearing from the motion vector rather than the heading, to avoid yaw
  drift. That protection is gone at the integration step, because body-frame
  deltas cannot be placed in the world without an attitude. A compass that is
  off by 10° rotates the whole track by 10°. Check `Heading` against the dive
  plan before trusting the shape of a track.
- **Only yaw is used**, not the full attitude. Roll and pitch are left out
  deliberately: the DVL deltas already arrive in the vehicle frame, and folding
  in a pitch bias would tilt the horizontal track for no gain.

If a recording *does* contain `LOCAL_POSITION_NED` — a future BlueOS release, or
a dive flown from QGroundControl — it is used verbatim instead, and the output
matches the tlog workflow exactly. The `DVL_source` column records which
happened, per row.

### Depth comes from a different message

`VFR_HUD.alt` reads a flat zero in these recordings, so the tlog rule ("prefer
`VFR_HUD.alt` when it is below −0.5 m") almost never fires. Depth falls through
to `GLOBAL_POSITION_INT.relative_alt` — the autopilot's own baro-derived depth,
already negative-down — and below that to the external pressure sensor
(`SCALED_PRESSURE2`), with surface pressure taken from a low percentile of the
file rather than a nominal 1013.25 hPa.

The `Depth_Source` column records which source produced each row. The preference
order is unchanged, so a recording where `VFR_HUD` works still behaves as before.

### `NEDz` is empty

It was `LOCAL_POSITION_NED.z`. The column is kept so the schema still matches,
and stays blank unless the recording carries that message.

---

## Checking the navigation: `--health`

```bash
python -m ccr_m2c --health logs/*.mcap
```

The transects are only as good as the navigation behind them, and a recording
says a great deal about that. This reports which aiding sources the EKF actually
had, its own innovation variances, sensor health, the compass, vibration, and
the warnings the autopilot raised — then says which of those are worth acting on.

The line to read first is **absolute horizontal position**. If it says
`NO -- dead reckoning only`, the EKF never accepted a GPS or USBL fix and every
horizontal position in the output came from the DVL. That was the case on
2026-08-26: `EKF_POS_HORIZ_ABS` was never set once in 38 minutes.

Innovation variances are how the filter reports that it is fighting a sensor
before anything visibly breaks. Below 1.0 it is accepting the reading; above,
it is rejecting or straining. `compass_variance` matters more than it looks —
a yaw error rotates the entire DVL track about its start point, and no amount
of good DVL data corrects for it.

One judgement is built in. Without GPS or a locked USBL, ArduSub reports the
**AHRS** health bit unhealthy for the whole dive: it means "no absolute
position", not "the attitude solution is broken". Flagging that as a fault would
cry wolf on every survey the team flies, so it is annotated instead of raised —
unless the dive *did* have an absolute fix, in which case it is a real concern.

---

## What the DVL is good for, and what it is not

`Altitude` and `Velocity_mps` both come from the Water Linked DVL A50, and
neither drifts — they are instantaneous measurements, not integrals. Only
position accumulates error.

**Altitude** is `RANGEFINDER`, which on this vehicle *is* the A50: compared
against the DVL's own `DISTANCE_SENSOR` on the 2026-08-26 dive the two agree to
a median of 8 mm. Its real limitation is dropouts, not drift — that dive had 48
gaps longer than a second, the worst 11.9 s, where the DVL lost bottom lock.
Values are held for 5 s and then go blank, so `Altitude`, `Width` and `Area_m2`
have holes rather than stale numbers across the long ones.

**Velocity** is derived from `VISION_POSITION_DELTA`, the A50's own measured
displacement over its own time delta. It is preferred over `VFR_HUD.groundspeed`
because it is the cleaner signal: on that dive the two agree closely (bias
−0.003 m/s, RMS 0.047 m/s, r = 0.913) but the EKF and HUD both spike to
1.31 m/s where the DVL peaks at 0.65 m/s. Those spikes are filter transients,
not the vehicle.

**Position does drift**, because it is the integral of velocity and any small
bias accumulates. The only axis that can be checked independently is the
vertical, since the pressure sensor is a separate instrument: integrating the
DVL's vertical deltas and comparing against pressure depth gives **−6.3 m over
37 minutes, about 0.14 m/min**, equivalent to a velocity bias of roughly
2 mm/s. (The EKF's depth tracks pressure to a median of 0.28 m, so the
reference is sound.)

Treat that as an order of magnitude, not a horizontal figure. A DVL's vertical
channel is its weakest, and ArduSub uses the barometer for depth regardless, so
that axis is never corrected in normal operation. Horizontal drift could not be
measured on this dive: the USBL never locked, and the ROV did not return to its
start, so there is no closure to check. If the same 2 mm/s applied horizontally
it would be about 1 m over a nine-minute transect — small against a 59 m
transect, but it accumulates across a dive.

To measure horizontal drift properly, either fly a closed loop (start and finish
on the same point, and the gap between them is the error) or get the USBL
locked, which gives an independent fix to compare against.

---

## Where the transects sit relative to each other

The DVL track is dead reckoning, so it has to be pinned to a real coordinate
somewhere. The obvious place is each transect's first surface fix — which is
what the tlog workflow did, and what this tool did at first.

It is wrong whenever the USBL has not locked. A Water Linked unit with no fix
reports **one static position for the whole dive**, so every transect gets
seeded at the identical coordinate and they stack on top of one another. Their
real separation is lost — even though the DVL measured it. The autopilot's local
frame runs continuously *between* transects as well as during them, so the
distance from the end of one to the start of the next is known.

So the track is propagated once across the whole dive from a single seed, and
each transect keeps its slice. On the 2026-08-26 Centennial Park dive that is
the difference between three transects piled on one point and T2 starting where
T1 ended, with T3 about 80 m east.

`DVLx`/`DVLy` are still re-zeroed to each transect's start afterwards, so those
columns mean exactly what they did in the tlog workflow, and `Distance` still
measures only the transect itself. Only `DVLlat`/`DVLlon` change.

What this does **not** fix is absolute accuracy: with a static surface fix the
whole set can still be offset from the true position, and the shape rotates with
any compass error. It is the geometry between transects that becomes
trustworthy, not the position on the earth.

---

## Recordings that were cut short

A recording whose session ended abruptly — the tether pulled, the vehicle
powered down mid-write — is left with a truncated summary section at the end of
the file. Every indexed reader fails on it outright, because the index is what
tells the reader where anything is.

The data records are almost always intact, and a dive cannot be re-flown, so a
file that will not open normally is walked from the front instead, stopping
cleanly at the first record that will not parse. There is one such file in the
shakedown folder: `recorder_20260504_231243.mcap` yields nothing at all through
the normal path, and 179 seconds of telemetry this way.

You will see it in two places. The file list shows
`no index; will be read in full` instead of an end time — the start time is
still read, so the file still sorts and still groups with the right dive. And
the run reports `index damaged (...); recovered N messages by reading the file
in full`. The recovery is slower than an indexed read, because nothing can be
skipped.

---

## Two smaller improvements

- **Headings average correctly around north.** The tlog script took a plain
  arithmetic mean of the per-second heading samples, so a transect running due
  north — samples at 359° and 1° — averaged to 180°, pointing due south.
  Headings are now averaged as unit vectors.
- **Each field is averaged over its own samples.** The tlog script divided every
  running total by the count of *all* messages in that second. It happened to
  come out right, because it also accumulated each held value once per message,
  but the two only stay in step by coincidence. Fields are now averaged over
  their own samples and then held forward across short gaps, up to a per-field
  staleness limit — a DVL that drops out leaves blanks rather than a flat line.

---

## Output

One CSV per transect, named `{Transect_ID}.csv`.

The first 31 columns are **byte-for-byte the tlog_to_csv.py column list, in
order**, so anything reading those files keeps working:

```
Date, Time, Site_name, Transect_number, Transect_ID, Mode_num, Mode,
Battery_V, Battery_A, Battery_W, Battery_mAh_used, Battery_Wh_used,
Latitude, Longitude, EKFlat, EKFlon, DVLx, DVLy, DVLlat, DVLlon,
Altitude, Depth, Depth_std, Depth_Source, Heading, Velocity_mps,
Width, Area_m2, Distance, NEDz, VFR_alt
```

Then what the mcap makes available that a tlog did not:

```
Datetime_UTC, Roll, Pitch, Water_temp_C, Pressure_abs_hPa,
DVL_confidence, DVL_source, Lights_pct, Cam_tilt,
GPS_fix_type, GPS_satellites, Relative_alt_m, Messages
```

**[COLUMNS.md](COLUMNS.md) documents all 44** — what each one means, which
MAVLink message it came from, and whether it was read from a sensor, fused by the
EKF, computed here, or scaled by a camera-calibration constant. The short version:

| Column | Source | Notes |
| --- | --- | --- |
| `Latitude`/`Longitude` | `GPS_RAW_INT` | Water Linked UGPS surface position |
| `EKFlat`/`EKFlon` | `GLOBAL_POSITION_INT` | blank until the EKF has a position |
| `DVLx`/`DVLy` | see above | metres North/East, zeroed to the transect start |
| `DVLlat`/`DVLlon` | derived | the DVL steps propagated geodesically from the first fix |
| `Altitude` | `RANGEFINDER` | metres above the seabed; `DISTANCE_SENSOR` id 0 as fallback |
| `Depth` | see above | negative-down |
| `Depth_std` | `Depth` + NOAA | `−Altitude + Depth + water_level`, MLLW |
| `Width`/`Area_m2` | derived | camera footprint, from `Altitude` |
| `Velocity_mps` | `VISION_POSITION_DELTA` | DVL step over its own time delta |
| `Messages` | — | MAVLink messages behind that second; a thin row is a dropout |

### The map

`transect_map.html` sits beside the CSVs. Transects are coloured individually;
the localisation source (DVL / EKF / GPS) is a switch that redraws all of them,
so two transects never share a colour. Click a track for duration, distance,
depth range, mean altitude and swath covered. Satellite, ocean and street
basemaps. It needs a network connection the first time it is opened — Leaflet
and the tiles are fetched, the track data is inlined.

`Swath covered` is mean width × distance travelled. Do not sum the `Area_m2`
column to get it: at survey speed that counts the same patch of seabed once per
second the ROV was over it.

---

## Installing

From a checkout, in whatever environment you use:

```bash
python -m pip install -e .
```

That puts the dependencies in place and makes the tool runnable from any
directory, as `python -m ccr_m2c ...` or just `ccr-m2c ...`. `-e` means editable,
so a `git pull` updates the tool with no reinstall.

If you would rather not install anything, `python -m pip install -r
requirements.txt` still works — but then every command has to be run from this
folder, because that is the only place Python can find the package.

Python 3.10+. tkinter ships with Python on Windows and macOS; on Debian/Ubuntu
`sudo apt install python3-tk`.

### If the launcher cannot find a usable Python

`run_MCAP_to_CSV.bat` does not simply take the first `python.exe` it finds on
disk — it makes each candidate prove it can import tkinter and this tool's
packages first, and uses the one on your PATH ahead of any guess.

That check exists because of a real failure. This machine carries a partial
Python install at `%LOCALAPPDATA%\Programs\Python\Python313` — `python.exe` and
a few DLLs, with no `Lib\` directory at all. It cannot find its own standard
library, so it silently borrows Anaconda's, and then fails on
`ImportError: DLL load failed while importing _tkinter`, which reads like a
broken tkinter rather than a broken Python. Any launcher that picks an
interpreter by checking whether the file exists will choose it.

If the app reports missing packages, it prints the exact interpreter and the
`pip install -r requirements.txt` line to run. If it reports no usable Python,
install one from python.org with the "tcl/tk and IDLE" option ticked.

> The same partial install will be picked by `UTC/run_UTC.bat`, which lists
> `Python313` first and does not verify it.

### Building a standalone .exe (optional)

The batch file is the intended way to run this. `ccr_m2c.spec` is included for the
day someone wants a copy that runs without Python at all — it mirrors UTC's
spec, but **it has not been built or tested here**, so treat the first build as
something to verify rather than to hand straight to a teammate:

```bash
python -m pip install pyinstaller
pyinstaller ccr_m2c.spec
```

Set `M2C_DEBUG=1` first to build a console variant — a windowed build discards
stdout, so a startup failure leaves no trace, and the console build is how you
find out why.

## Tests

```bash
python -m pytest tests -q
```

They build small synthetic `.mcap` files with the same JSON-over-MAVLink layout
the recorder produces, so nothing here needs a real dive.
