# Navigation — the operator's guide

Chapter **2 Navigation** is where you sit for the whole dive. Cockpit is on the
monitor above and has the camera; this has the map, the flight instruments, the
power gauge and the navigation suite.

It is read-only until you deliberately unlock it. Nothing on this page sends
the vehicle anything unless you tick **Allow writes to this vehicle** and then
confirm a specific change.

---

## Contents

- [The one-minute version](#the-one-minute-version)
- [What is on the page](#what-is-on-the-page)
- [Before the dive](#before-the-dive)
- [The EKF origin](#the-ekf-origin) ← **read this if your track has no lat/lon**
- [Navigation profiles](#navigation-profiles)
- [The map](#the-map)
- [Waypoints](#waypoints)
- [Energy and the power gauge](#energy-and-the-power-gauge)
- [What each number is measured from](#what-each-number-is-measured-from)
- [Replay and demonstration](#replay-and-demonstration)
- [What gets written to disk](#what-gets-written-to-disk)
- [Troubleshooting](#troubleshooting)
- [Compatibility: what works on the installed stack](#compatibility-what-works-on-the-installed-stack)
- [Bench validation checklist](#bench-validation-checklist)
- [Requirement checklist](#requirement-checklist)

---

## The one-minute version

1. Choose the flight folder on **1 Monitoring**, as usual.
2. Open **2 Navigation**.
3. Under the map, check **ORIGIN**. If it says *No EKF origin*, press
   **Origin…**, type the site coordinates, tick **Allow writes**, and press
   **Set on the vehicle now**. Without this there is no latitude or longitude —
   the map falls back to a metre grid and says so.
4. Check **PROFILE**. It should be green. If it is not, press **Details…** and
   read the reasons.
5. Fly. Press **＋ Create waypoint** whenever you see something.

---

## What is on the page

```
+---------------------------+------------------+------------------+
|                           |  Flight Ops HUD  |    Power HUD     |
|          MAP              |  gauge | 3 rows  |  gauge | 3 rows   |
|                           +------------------+------------------+
|   lat/lon · source · age  |                                     |
+---------------------------+     Navigation workspace            |
| PROFILE | ORIGIN | TO …   |     sensors · estimator · profile    |
+---------------------------+-------------------------------------+
```

**Flight Ops** — a vertical altitude gauge and exactly three rows: flight
mode (with the Surftrak target when there is one), velocity, and depth.

**Power** — a fixed 0–1,000 W gauge and exactly three rows: voltage, current,
and the watt-hours used this flight.

**The strip under the map** carries the three things you must never have to go
looking for: which profile is in force, whether the origin is confirmed, and
the bearing home.

**The navigation workspace** is the sensor matrix. Its three columns are three
different questions and the difference matters:

| Column | Means |
|---|---|
| **Configured** | the parameters say this source should be used |
| **Available** | fresh, valid measurements are arriving |
| **Used** | the estimator's own behaviour shows it is being fused |

A source can be configured perfectly, delivering beautifully, and *not be used*.
That is the most confusing state there is and it is what cost us a whole day's
coordinates on 18 September 2026 — see the next section.

Symbols are `✓` `✗` `◐` (stale) `?` (cannot be established) `–` (not
applicable). Every state has a glyph as well as a colour, because a Rugged
screen in daylight loses the colour first.

---

## Before the dive

Do these in order, with the vehicle disarmed on deck:

1. **Let the parameters load.** The workspace shows *parameters have not been
   read* until they have. They come from the head of the autopilot's own
   dataflash log — nothing is sent to the vehicle to get them — and refresh
   every two minutes.
2. **Check the profile** (see below).
3. **Set the origin** (see below).
4. **Look at the DVL row** in the matrix. Its detail should say
   `POSITION_ESTIMATE`. If it says `POSITION_DELTA` you will get no
   coordinates; fix it in the Water Linked DVL extension's own page in BlueOS.

---

## The EKF origin

**This is the one that bit us.** If your dive produced a track with no latitude
or longitude, this section is why.

### What an origin is

EKF3 tracks position in a local north/east frame. `GLOBAL_POSITION_INT` — the
latitude and longitude everything downstream uses — is that local frame
projected through the **EKF origin**. With no origin, ArduPilot reports
latitude 0 and longitude 0 for the entire dive, however well the DVL is
working.

### What went wrong on 18 September 2026

The vehicle had `ORIGIN_LAT = 47.62691` and `ORIGIN_LON = -122.39018` sitting
in its parameters, and produced no coordinates at all.

Those parameters exist because an `ahrs-set-origin` Lua applet had been
installed at some point. **A parameter table created by a Lua script stays in
the vehicle's storage after the script is removed.** No script was running to
read them. The coordinates were set and nobody had told the autopilot.

The page now says exactly this when it sees that state.

### Setting it

Press **Origin…**. The dialog does two things, in this order, and the order is
the safety property:

1. **Set on the vehicle now** — sends `SET_GPS_GLOBAL_ORIGIN` and reads
   `GPS_GLOBAL_ORIGIN` back to confirm it. Immediate and verifiable. **Lost at
   the next reboot.**
2. **Save for the next boot** — writes the applet's parameters. This is
   *refused* until step 1 has succeeded.

Why refused: while the EKF has no origin, an `ahrs-set-origin` applet may be
sitting in its five-second retry loop. Its "nothing set yet" check passes as
soon as **any one** of its three parameters is non-zero, so writing them one at
a time can hand it a latitude with no longitude and lock in an origin in the
Atlantic. Once the origin is set the applet returns at `if ahrs:get_origin()`
before it reads the parameters at all, and the order stops mattering.

**Reading the origin sends the vehicle a command.** ArduPilot does not stream
`GPS_GLOBAL_ORIGIN`; it answers a `MAV_CMD_REQUEST_MESSAGE`. So confirming the
origin needs writes unlocked. It is one command and changes nothing.

### Which Lua applet to install

Install **this repository's copy**, `lua_scripts/ahrs-set-origin.lua`, not the
one on ArduPilot master.

ArduPilot's published applet contains a bug this repository already carries a
one-line fix for. Its guard reads:

```lua
if AHRS_ORIG_LAT == 0 and AHRS_ORIG_LON == 0 and AHRS_ORIG_ALT == 0 then
```

That compares the `Parameter` *object* to a number, which Lua never finds
equal, so the guard never fires. On a fresh install with the parameters still
at their defaults the script goes straight on and calls `ahrs:set_origin()`
with 0, 0, 0 — and then returns for good without retrying. The fixed copy calls
`:get()` first.

Two variants are in `lua_scripts/`:

| File | Creates | Use it if |
|---|---|---|
| `ahrs-set-origin.lua` | `AHRS_ORIG_LAT/LON/ALT` | you want the upstream names |
| `ahrs-set-origin-ORIGIN_.lua` | `ORIGIN_LAT/LON/ALT` | you want the names this fleet has been setting |

Either works. The page detects whichever family is present, writes to that one,
and **warns if more than one exists** — a leftover family from a prefix nobody
is using reads like configuration and does nothing.

`SCR_ENABLE` must be 1. The page shows it.

### The origin is not a measurement

Setting the origin does not measure where the ROV is. It declares the point the
dead reckoning is referenced to. In DVL-only mode the whole track is right
relative to itself and can sit off the true position as a block, and rotates
with any compass error. The strip says *dead-reckoned*; waypoints captured
there are flagged `dead_reckoned`.

---

## Navigation profiles

Two, selected from the workspace:

**DVL dead reckoning** — position and horizontal velocity from the DVL, depth
from the barometer, heading from the compass. No acoustic correction. This is
what you fly without a vessel.

**Acoustic position + DVL** — horizontal position from the Water Linked
acoustic solution (injected as `GPS_INPUT`), horizontal velocity still from the
DVL, depth from the barometer, heading from the compass.

"DVL only" is shorthand. Orientation and depth still come from other sensors.

### Checking

The check compares the profile's requirements against the vehicle's actual
parameters, and shows each mismatch with **the reason it matters**, not just a
name and a number. Blockers are separated from advisories.

Two things it checks that are not parameters:

- **the DVL's message type** — `POSITION_ESTIMATE` is required. See below.
- **the origin** — see above.

### Applying

**Review & apply…** shows exactly which parameters will change, from what, to
what, and why. Nothing is written until you confirm. Then each one is written
and **read back**.

Guards:

- the button is disabled unless **Allow writes to this vehicle** is ticked;
- the tick is refused while the vehicle is **armed**;
- the tick is forgotten when the program closes;
- a batch is not a transaction — if one write fails, the ones before it stay
  written and the dialog says **partially applied**. Nothing is rolled back
  automatically: once the state is uncertain, another blind write does not help.

### Why `POSITION_ESTIMATE` and not `POSITION_DELTA`

The Water Linked DVL extension can send three different MAVLink messages. Its
default is `POSITION_DELTA`, and on this firmware that cannot give you a
geographic position:

| Extension setting | MAVLink message | ArduPilot routes it to | EKF3 reaches |
|---|---|---|---|
| `POSITION_DELTA` (default) | `VISION_POSITION_DELTA` | `writeBodyFrameOdom` | **relative aiding only** |
| `POSITION_ESTIMATE` | `VISION_POSITION_ESTIMATE` | `writeExtNavData` | absolute aiding |
| `SPEED_ESTIMATE` | `VISION_SPEED_ESTIMATE` | velocity only | no position at all |

`readyToUseExtNav()` requires `extNavDataToFuse`, which only `writeExtNavData`
fills. So with `POSITION_DELTA`, `EK3_SRC1_POSXY = ExternalNav` has no external
position to consume.

**With a valid origin, relative aiding still produces a dead-reckoned lat/lon**
(`getLLH` returns origin + offset when `horiz_pos_rel` is set). So on
18 September the missing origin was the binding problem. Setting the message
type to `POSITION_ESTIMATE` as well is what gets you absolute aiding.

### EKF source sets

ArduSub 4.5.7 does support `MAV_CMD_SET_EKF_SOURCE_SET` for sets 1–3. The page
offers it **only when the destination set is actually configured**. On this
fleet `EK3_SRC2_*` and `EK3_SRC3_*` are all zero, so switching to set 2 would
select no position, no velocity and no yaw source — worse than any mismatch it
was meant to fix. The page says so rather than offering it.

Note also: the command returns `MAV_RESULT_ACCEPTED` and **nothing in MAVLink
reports which set is live**. A switch is "requested and acknowledged"; it is
active only when the measurements say so.

---

## The map

Pan by dragging, zoom with the wheel or a double-click, **Follow** keeps the
vehicle centred, **Fit** frames everything.

### Basemaps

| Layer | What it gives you |
|---|---|
| **Nautical (depth shaded)** *(default)* | bathymetric shading — darker is deeper |
| **NOAA ENC chart** | US charts with soundings and depth contours |
| **OpenStreetMap** | shoreline, piers, streets |
| **Aerial imagery** | shoreline features and moorings |
| **Seamarks** *(overlay)* | buoys, beacons, anchorages |

Tiles are cached permanently in `%LOCALAPPDATA%\CCR_ROV\map_tiles`, outside the
repository. Once a site has been looked at with a connection, it works offline.

With no tiles at all the map draws a metre grid, a scale bar, a north arrow and
the tracks — which is most of what it is for. It says *no basemap — grid only*
rather than looking broken.

**NOAA's tile service was not reachable from the machine this was written on.**
It may be fine on the Aquarium network. If it is not, the layer will simply not
draw and the footer will say why.

### Preparing a site for an offline day

Open Navigation on a connection, set the map to the site, and pan and zoom
around the survey area at the zooms you will use. Tiles are only fetched for
what is on screen — there is no bulk download, deliberately, because these are
other people's tile servers.

### Seabed depth

Tick **Seabed depth** and the track is coloured by the depth of the seabed
under the vehicle — depth below the surface minus altitude above the bottom,
both measured by the ROV. At the metre scale these surveys work at that is
better bathymetry than any public source, and it accumulates for free.

### When there is no geographic position

The map switches to a **local view**: metres from the start, a grid, a scale
bar, and a banner saying *the track's shape is real; its place on the Earth is
not known*. It does not draw a chart under a track it cannot place.

### Tracks break rather than bridge

An estimator reset, an origin change or a position jump starts a new segment,
and segments are never joined — the line between them would be a movement that
did not happen. The break is recorded in the session log with its reason.

### The vessel

In the acoustic profile a distinct vessel icon shows the boat with its bow on
**HDT true heading** — not course over ground, not the ROV's yaw, not the
direction of travel. When HDT goes stale the hull is drawn without a bow at
all. Its breadcrumb shows anchor swing.

In DVL-only mode the live vessel icon and its track are hidden even if vessel
packets keep arriving; the logs keep everything. A distinct cross-in-circle
marks the fixed origin — never a boat icon.

### To vessel / to start

- **acoustic profile:** *To vessel: 275°T · 142 m* — the geodesic initial
  bearing to where the vessel is now.
- **DVL-only:** the readout relabels itself **TO START** and measures to the
  fixed origin. Same arithmetic, different claim, so it never keeps the
  vessel's label.

It is a direction to a point, **not a course to steer**. It knows nothing about
the tether, obstacles or current. At very small separations it says
*at/near target* rather than showing a bearing that spins.

---

## Waypoints

**＋ Create waypoint** captures the ROV's position **at the instant you press
it**, saves it to disk, and *then* offers a rename. Cancelling the rename keeps
the point.

That order is deliberate: by the time you have typed "wolf eel den" the ROV has
moved, and a point recorded at the end of the typing is confidently wrong.

Saved with each point: time, coordinates, the fix's provenance and age, whether
it was dead-reckoned, the origin it was referenced to, the profile, depth and
altitude. Export to GeoJSON or CSV.

Capture is refused without a usable position, and a stale position can only be
captured deliberately — it is then flagged as stale.

**These are survey annotations, not autopilot missions.** Nothing is ever
uploaded to the vehicle.

---

## Energy and the power gauge

The gauge is fixed at **0–1,000 W** and never rescales. 900 W is marked in red,
hatched, and labelled. Above 1,000 W the marker pins and the true number stays,
with an over-range mark.

A separate chevron shows the **observed peak** for the flight. "Observed"
because telemetry cannot catch every electrical transient.

Watts are `V × I` from a single `BATTERY_STATUS` — the two arrive in the same
message, so their product is a genuine simultaneous draw. On this fleet that is
the Navigator's analog sense on the main busbar (`BATT_MONITOR` 4).

**It is not the OTPS's own protection measurement.** The 1,000 W top of the
gauge is the operating reference you fly to, not a claim that this sensor
measures the trip point. Topside supply losses, tether losses and anything
powered before the sense point are not in it.

### Watt-hours

Integrated over the intervals that actually happened, not samples × a nominal
rate. Refused: duplicate samples, stale samples, time going backwards, and gaps
longer than three seconds. A gap is recorded, not invented — the row shows
*«n»% coverage* whenever it is below 100%, so "14.2 Wh" can be read alongside
how much of the flight it covers.

The total survives a restart of the program mid-flight, and only resumes a
saved state whose session id matches — yesterday's total is not this flight's.

---

## What each number is measured from

| Shown | From | Notes |
|---|---|---|
| Altitude | `RANGEFINDER.distance` (m) | height above the **seabed**. Falls back to `DISTANCE_SENSOR.current_distance` (**cm**). A zero range is *no bottom lock*, never an altitude of zero. Never substitutes depth or MSL altitude. |
| Depth | `GLOBAL_POSITION_INT.relative_alt` (mm) | barometer, via ArduSub. Shown negative-down, e.g. `−5.0 m`. |
| Velocity | `LOCAL_POSITION_NED.vx/vy` | EKF earth-frame horizontal speed over ground. Falls back to `VFR_HUD.groundspeed` before the estimator has a solution. DVL lock loss shows *unknown*, **not zero**. |
| Flight mode | `HEARTBEAT.custom_mode` | |
| Surftrak target | `NAMED_VALUE_FLOAT` keyed **`RFTarget`** (m) | ArduSub sends nine named floats in one burst, so the key is checked every read. `ModeSurftrak` uses −1 cm for "no target", which arrives as **−0.01 m** — anything ≤ 0 is *target unavailable*. **Distinct from the fixed 0.8 m survey reference on the gauge.** |
| Voltage / current | `BATTERY_STATUS` | `current_battery = −1` means *no sensor*, not a small negative current. |
| ROV position | `GLOBAL_POSITION_INT` | or `LOCAL_POSITION_NED` projected through a **confirmed** origin, marked *dead-reckoned*. |
| Vessel position/heading | WL UGPS External `/status` | GGA and HDT freshness are its own four-second receipt window. It does **not** publish fix quality, HDOP or satellite count, and its course/speed over ground are fixed at 0 and never updated — the page marks those *unsupported* rather than showing 0. |
| Acoustic quality | `GPS_INPUT.vdop` | the Water Linked extension puts the **acoustic standard deviation in metres** there. It is not a vertical dilution of precision. |
| Satellites | `GPS_INPUT.satellites_visible` | the **topside** receiver's count, forced to ≥6 under `--ignore_gps`. Not a measure of underwater position quality. |
| EKF | `EKF_STATUS_REPORT` flags | `EKF_POS_HORIZ_ABS`, `EKF_POS_HORIZ_REL`, `EKF_CONST_POS_MODE`. |

### Freshness

Every message's age is the time since a **new** message arrived, taken from
mavlink2rest's own counter — not the time since the last successful request.
A successful HTTP GET proves the service answered; it says nothing about
whether the vehicle has sent anything. **Message health** under *Details…*
shows the counter, the measured rate and the real age for every message.

---

## Replay and demonstration

Replay drives the whole page from something that is not a vehicle, and
**cannot write to one** — the replay collector has no connection object at all,
so there is nothing a send could be called on.

Sources: a navigation session this program recorded; a transect CSV from the
extractor (any dive this year); or a synthetic dive, labelled **SYNTHETIC**
everywhere it appears, with deliberate faults — DVL lock loss, a stale vessel
heading, a power excursion past 1,000 W, an estimator reset.

**Replay speed does not change the watt-hours.** Energy integrates over the
recorded timestamps, so a dive played at eight times speed accumulates exactly
what it did at one.

---

## What gets written to disk

Into the flight folder's `logs/`:

| File | What |
|---|---|
| `nav_<flight>.json` | the manifest — versions, endpoints discovered, sensor-source mapping, thresholds, origin authority, clock provenance |
| `nav_<flight>.jsonl` | the events, one JSON object per line, appended as they happen |

And in the flight folder itself, `waypoints.json`.

JSONL because it is append-only (a laptop losing power loses at most the last
line), needs no migration, and can be read in a text editor on a boat.
Positions are logged at full rate while the map draws a decimated track — the
samples on disk are the record.

If the log cannot be written, the page says so. It does not fail the dive.

Map tiles and the energy state live in `%LOCALAPPDATA%\CCR_ROV\`, outside the
repository and outside Dropbox.

---

## Troubleshooting

**"No EKF origin — position will be local only"**
→ [The EKF origin](#the-ekf-origin). This is the common one.

**Origin parameters are set but there is still no origin**
→ No Lua applet is running to read them. The parameters survive the script's
removal. Install `lua_scripts/ahrs-set-origin.lua` and check `SCR_ENABLE = 1`,
or use **Set on the vehicle now** for this session.

**The DVL row says "POSITION_DELTA is body-frame odometry"**
→ Set the Water Linked DVL extension's message type to `POSITION_ESTIMATE` in
its own BlueOS page.

**Everything is available but "Used" says `?`**
→ The estimator has not confirmed it. Check `EKF_STATUS_REPORT` under
*Details…*. `EKF_CONST_POS_MODE` means no aiding at all.

**The altitude shows `—` and "no bottom lock"**
→ The downward range is invalid. This is correct behaviour: it will not
substitute depth or a stale value. Check the DVL and `RNGFND1_TYPE = 10`.

**`⚠ RNGFND1_ORIENT is 5000 — not an orientation`**
→ Surftrak Fixit v1.0.0-beta.2 has a confirmed bug: its `prb_bad_max` repair
logs "setting `RNGFND1_MAX_CM` to 5000" and actually calls
`set_param('RNGFND1_ORIENT', 5000)`. **Do not use that repair action.** Set
`RNGFND1_ORIENT` back to 25 (down) yourself.

**The map shows "no basemap — grid only"**
→ No connection and nothing cached for this area, or the tile source is
unreachable. The grid, scale bar and tracks still work. The footer names the
error.

**The vessel is at 0, 0 / in the Atlantic**
→ It is not. The page refuses those coordinates: the WL UGPS External
extension returns latitude 0 and longitude 0 before its first GGA, and the page
shows *no GGA within the four-second window* instead of plotting it.

**Readings are all red and say STALE**
→ The link. Check the footer line, which carries the collector's own account of
itself.

**"Apply (locked)"**
→ Tick **Allow writes to this vehicle**, with the vehicle disarmed.

---

## Compatibility: what works on the installed stack

Against the vehicle inventory of 18 September 2026: ArduSub **4.5.7 STABLE**,
BlueOS **1.5.0-beta.39**, Navigator.

| Component | Version | What was inspected | Status |
|---|---|---|---|
| ArduSub | 4.5.7 STABLE | source at `b09fafe2`; 1,020-parameter dump from a real flight | **works** |
| mavlink2rest | via BlueOS beta.39 | `status.time.counter/frequency/last_update` | **works** — freshness is taken from the counter |
| Water Linked DVL | v1.0.10 | `dvl.py`, `mavlink2resthelper.py`, `main.py` (Flask, port 9001) | **works** — `/get_status` read; message type checked |
| WL UGPS External | v1.1.0-beta.1 | `main.py` (FastAPI, container port **8080**), `topside_position.py` | **works** — `/status` read; port discovered via BlueOS |
| Water Linked UGPS | v1.0.7 | `mavlink2resthelper.py` | **works** — read through `GPS_INPUT` at the autopilot |
| Surftrak (in ArduSub) | 4.5.7 | `GCS_Mavlink.cpp`, `mode_surftrak.cpp` | **works** — `RFTarget` in metres, −0.01 = no target |
| Surftrak Fixit | v1.0.0-beta.2 | `surftrak_status.py` | **not called** — confirmed wrong-parameter bug; its damage is detected |
| Tether Diagnostics | v1.0.3 | not resolved publicly | **not used** by this page |
| Madrona / major_tom | — | not audited | **not used** by this page |
| `dvl_beam_split` | disabled | — | **not used** |

Known-absent and handled:

| Thing | Status |
|---|---|
| `EK3_GPS_TYPE` | **does not exist on 4.5.7** (`// 1 was GPS_TYPE`). The DVL extension writes it anyway; that write lands nowhere. Shown as informational, not a fault. |
| `AHRS_ORIGIN_LAT/LON/ALT` (native, 4.7+) | **not on this firmware.** Detected if present; not a reason to upgrade. |
| Vessel GNSS fix quality / HDOP / satellites | **not published** by the external extension's `/status`. Marked unsupported. |
| Vessel course/speed over ground | the extension fixes them at 0 and never updates them. Marked unsupported — 0 is not a measurement. |
| Which EKF source set is live | **no MAVLink message reports it.** A switch is "requested and acknowledged". |

**Not validated against a vehicle.** Everything above was derived from pinned
source, a real parameter dump and a real 4.4 GB recording. No live vehicle was
connected while this was written. See the next section.

---

## Bench validation checklist

With Nereo on the bench, disarmed, tether connected. Tick these before flying.

**Connection**
- [ ] The page finds the vehicle; the footer shows the host and an age.
- [ ] *Details… → Message health* lists `ATTITUDE`, `VFR_HUD`,
      `GLOBAL_POSITION_INT`, `RANGEFINDER`, `NAMED_VALUE_FLOAT`,
      `BATTERY_STATUS` with plausible rates.
- [ ] Unplug the tether: within a few seconds every reading goes stale and the
      footer says so. **Nothing goes to zero.**
- [ ] Plug it back in: readings recover without restarting the program.
- [ ] Cockpit runs at the same time, video and all, and neither stutters.

**Discovery**
- [ ] *Details…* shows the DVL and WL UGPS External ports, and whether each was
      found in BlueOS's service list or assumed.

**Instruments**
- [ ] `RFTarget` appears under message health; the Surftrak target row reads
      *target unavailable* out of Surftrak, and a number in it.
- [ ] Cover the DVL / lift the ROV: altitude goes to `—` *no bottom lock*, and
      velocity to `—` *speed unknown, not zero*.
- [ ] Depth reads negative.
- [ ] Power reads a plausible busbar draw; note it here: ______ W at idle.

**Origin** ← the important one
- [ ] The origin row says what it finds. Note it: ______________________
- [ ] **Read the origin from the vehicle** — does it report one?
- [ ] **Set on the vehicle now** with the site coordinates → does the read-back
      confirm?
- [ ] Does `GLOBAL_POSITION_INT` start reporting a real latitude and longitude?
- [ ] Does the map leave the local view and place the vehicle?
- [ ] **Save for the next boot** → read the parameters back.
- [ ] Reboot the vehicle → does the applet apply it? Watch for
      `ahrs-set-origin:` in the BlueOS messages.

**Profile**
- [ ] The DVL row's message type. Note it: ______________________
- [ ] If `POSITION_DELTA`, change it in the extension and confirm the row goes
      green and the EKF reaches `EKF_POS_HORIZ_ABS`.
- [ ] *Review & apply…* with writes locked → the button says **Apply (locked)**.
- [ ] Arm the vehicle → the unlock is refused.
- [ ] Disarm, unlock, apply one parameter → it reads back, and the session log
      records before and after.

**Map and waypoints**
- [ ] Tiles load for the site; pan and zoom are smooth.
- [ ] Disconnect the laptop from the internet → the cached area still draws.
- [ ] A waypoint captures at the press, survives a rename cancel, and survives
      restarting the program.

**Still needs the vehicle and cannot be checked here**
- the acoustic profile end to end (needs the G2, the vessel and a boat);
- `GPS_INPUT.vdop` carrying a real acoustic standard deviation;
- vessel GGA/HDT through the external extension with a real satellite compass;
- whether NOAA's tile service is reachable from the Aquarium network;
- whether the `ahrs-set-origin` applet actually fires at boot on Nereo.

---

## Requirement checklist

| Requirement | Status |
|---|---|
| Chapter 2, six tabs renumbered, Transects preserved | **done** — tested |
| Stable chapter identifiers so a saved tab cannot mis-restore | **done** — tested |
| Live map, marker with heading, breadcrumb, pan/zoom/follow/fit | **done** |
| Offline basemap + grid fallback, attribution retained | **done** — grid fallback verified; NOAA tiles unreachable from the development machine |
| Saved sites / planned transects overlay | **partial** — waypoints and origin draw; the survey plan's sites are not yet overlaid |
| GeoJSON/CSV import-export | **done** — export; import not implemented |
| Tracks break on resets, longitude wraparound handled | **done** — tested |
| Uncertainty only when actually estimated | **done** — no invented accuracy radius |
| Altitude gauge, two modes, 0.8 m at the exact midpoint, hysteresis | **done** — tested through the acceptance sequence |
| Surftrak target distinct from the survey reference, `RFTarget` by key | **done** — tested |
| Flight Ops HUD: three rows exactly | **done** |
| Power gauge fixed 0–1,000 W, 900 W band, observed peak, over-range | **done** — tested |
| Wh over real intervals, gaps/duplicates/stale refused, coverage shown | **done** — tested, and cross-checked against the extractor on a real transect to 0.4% |
| Sensor matrix: configured / available / used | **done** — tested |
| Profiles configuration-driven and versioned | **done** |
| Deliberate switching: review → apply → read back → verify, partial handled | **done** |
| Source-set switching offered only when the set is configured | **done** — tested |
| Origin: mechanism detection, staging order, saved ≠ active | **done** — tested |
| Versioned session log, manifest, append-only events | **done** — tested |
| Replay, synthetic faults, speed-independent energy, no vehicle writes | **done** — tested |
| No vehicle writes from replay, panel opening or reconnect | **done** — enforced structurally and tested |
| Bounded queues, no GUI-thread blocking, clean stop | **done** — the window never joins a worker |
| Compact fallback for small windows, minimum viewport recorded | **done** — see below |
| Hour-long soak with measurements | **done** — see `docs/` results below |
| **Live-vehicle validation** | **pending** — nothing here has touched an ROV |

### Minimum practical viewport

Measured on the development machine at 150% Windows scaling:

| Window | Logical page | Layout | Verdict |
|---|---|---|---|
| 1920×1080 | 1888×791 | side by side, rows in a column | **comfortable** |
| 1600×900 | 1568×611 | side by side, rows in a column | **good** |
| 1366×768 | 1334×479 | side by side, rows across | **usable** — the matrix scrolls |
| 1280×800 | 1248×511 | side by side, rows across | **usable** |
| 1100×740 | 1068×451 | stacked, rows across | **cramped** — below the practical minimum |

**Minimum practical: about 1280×800 logical.** Below that the sensor matrix
scrolls, which the design tries to avoid. At 150% scaling that means a
1920×1200 panel; at 100% it means a 1280×800 one.

The navigation workspace has a floor of 230 logical pixels and the HUDs give up
height first — a mismatch an operator cannot see is a mismatch that does not
exist.
