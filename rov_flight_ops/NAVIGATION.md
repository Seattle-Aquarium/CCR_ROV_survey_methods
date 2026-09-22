# Navigation — the operator's guide

Chapter **2 Navigation** is where you sit for the whole dive. Cockpit is on the
monitor above and has the camera; this has the map, the survey plan and the
navigation diagnostics.

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
- [The survey plan](#the-survey-plan)
- [Following a line](#following-a-line)
- [Waypoints](#waypoints)
- [Energy, and where the flight gauges went](#energy-and-where-the-flight-gauges-went)
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
2. Open **2 Navigation**. The map opens on Pier 59 whether or not anything is
   connected — the basemap is in the repository, not on a tile server.
3. In **Navigation readiness**, check the profile. Press **Start…** to say how
   this dive begins and to set the origin. Without an origin there is no
   latitude or longitude — the map falls back to a metre grid and says so.
4. Read the matrix. Four columns, four different questions; a red mark in
   **Fused** with green in **Recv** and **Valid** means a healthy sensor the
   estimator is not using.
5. Draw the survey in **Survey plan** — a line, a box, a grid — and select one
   to fly it.
6. Fly. Press **＋ Waypoint** whenever you see something.

---

## What is on the page

```
+-------------------------------------+---------------------------+
|                                     |  Navigation readiness     |
|                                     |  profile · Start… Health… |
|                MAP                  |  matrix, one row a source |
|                                     +---------------------------+
|                                     |  Survey plan              |
|   lat/lon · source · age            |  tools · features         |
+-------------------------------------+  inspector: metres, °T    |
| GUIDANCE  |  SITE / ORIGIN | TO SITE|                           |
+-------------------------------------+---------------------------+
```

**The flight and power gauges are gone.** The altitude gauge, the 0–1,000 W
power gauge and their rows used to sit here; live, in the water, nobody looked
at them — Cockpit is on the monitor above with the same numbers on it — and
they were taking the space the map and the diagnostics needed.

**None of that telemetry stopped being collected.** The collector still gathers
altitude, depth, speed, voltage, current, watts and watt-hours on every cycle,
and navigation, the session log, replay, the flight summary and the exported
CSVs all still read them. What went is the gauge widgets on this page, not the
measurements behind them. `gui/navgauges.py` is still in the tree, still
tested, and currently drawn by nothing — if these want a home later, it is
waiting.

**Navigation readiness** is the profile selector and the sensor matrix. Its
four columns are four different questions, and conflating any two of them is
how a whole day's coordinates get lost:

| Column | Means |
|---|---|
| **Conf** | the parameters say this source should be used |
| **Recv** | samples are arriving at all — from the message counter, so a cached HTTP reply is not mistaken for a new measurement |
| **Valid** | the measurement itself says it means something: a bottom lock, a fix type above zero, a range greater than nought |
| **Fused** | the estimator's own behaviour supports it being used, with the basis behind **Health…** |

The fourth column used to say "Used" and inferred it from general estimator
flags, from which parameters were selected, and from a fresh derived heading.
None of those is evidence that a *particular* sensor is being fused, so **Fused
shows `?` whenever that cannot be established** — which is most of the time,
because ArduPilot publishes aiding mode rather than per-instance fusion. A
question mark is an honest answer; a tick would not be.

**DVL position and DVL velocity are separate rows.** In the acoustic profile
the DVL supplies velocity while position comes from the acoustics, and one row
could not say that.

Symbols are `✓` `✗` `◐` (partly) `?` (cannot be established) `–` (not
applicable). Every state has a glyph as well as a colour, because a Rugged
screen in daylight loses the colour first. A detail too long for its column is
cut with an ellipsis; **Health…** has the whole sentence.

**The strip under the map** carries the three things you must never go looking
for: the guidance for the line being flown, whether the origin is confirmed,
and the bearing back to the site.

---

## Before the dive

Do these in order, with the vehicle disarmed on deck:

1. **Let the parameters load.** The readiness card shows *parameters have not
   been read* until they have. They come from the head of the autopilot's own
   dataflash log — nothing is sent to the vehicle to get them — and refresh
   every two minutes.
2. **Check the profile** (see below).
3. **Set the origin** (see below).
4. **Look at the DVL position row** in the matrix. `POSITION_ESTIMATE` gets
   you absolute aiding. `POSITION_DELTA` gets you relative aiding, which with
   a confirmed origin is still a usable dead-reckoned position — so it is an
   advisory here, not a blocker. Change it in the Water Linked DVL extension's
   own page in BlueOS if you want absolute.
5. **Check the map is ready.** Press **Offline…**. The Pier 59 layers say
   *ships with the program* and need no preparation; any other layer tells you
   how much of the survey radius it actually holds.

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

Two, selected in the readiness card:

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

| Layer | What it gives you | Offline |
|---|---|---|
| **Pier 59 chart (offline)** *(default)* | shoreline, piers and seamarks, rendered locally from OpenStreetMap data | **bundled, z14–19** |
| **Pier 59 imagery (offline)** | USGS aerial imagery of the site | **bundled, z13–16** |
| **NOAA ENC chart** | US charts with soundings and depth contours | fetched |
| **OpenStreetMap** | shoreline, piers, streets | fetched |
| **Aerial imagery** | shoreline features and moorings | fetched |
| **Seamarks** *(overlay)* | buoys, beacons, anchorages | fetched |

**The two Pier 59 layers ship inside the repository**, in
`assets/maps/pier59/`: two MBTiles files covering 200 m around
47.6075661, −122.3438752, about 600 KiB together. A laptop that has only ever
pulled from Git has a map of the site. They are opened read-only (`immutable=1`)
and live outside the tile cache, so clearing the cache cannot reach them, and
they are never queued for fetching — a bundled tile is in the pack or it is
nowhere.

`assets/maps/pier59/manifest.json` records, per layer, the source, the URL it
was built from, the licence, the build date, the bounds, the zoom range, the
byte count and a SHA-256. A test checks the checksums against the files in Git
and that neither layer came from the standard `tile.openstreetmap.org` service,
whose terms do not permit bulk prefetching. The chart is rendered locally from
Overpass data (© OpenStreetMap contributors, ODbL); the imagery is USGS, public
domain.

**Past a layer's top zoom the view is enlarged, not sharper.** The map goes to
z21 — 0.05 m per pixel, the scale a plan with 2 m lanes is drawn at — and above
the pack's own detail each tile is its ancestor cropped and scaled with
nearest-neighbour, so it goes visibly blocky. That is deliberate: smooth
interpolation would invent edges that look like resolution the pack does not
have. Three doublings is the limit; past that a blank grid is honester.

Fetched layers are cached permanently in `%LOCALAPPDATA%\CCR_ROV\map_tiles`,
outside the repository.

With no tiles at all the map draws a metre grid, a scale bar, a north arrow and
the tracks — which is most of what it is for. It says *no basemap — grid only*
rather than looking broken.

**NOAA's tile service was not reachable from the machine this was written on.**
It may be fine on the Aquarium network. If it is not, the layer will simply not
draw and the footer will say why.

### Preparing a site for an offline day

Press **Offline…**. For each layer it reports how many of the tiles covering
the survey radius are actually held, at the zooms a survey uses (z14–19), and
what is missing. The Pier 59 layers report *ships with the program — always
available* and cannot be prepared: there is nothing to fetch, and offering a
progress bar that can only reach zero would be worse than saying so.

For a fetched layer the dialog estimates the tile count and the bytes before it
starts, because that is somebody's tethering allowance and somebody else's tile
server.

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

### To vessel / to site

- **acoustic profile:** *TO VESSEL: 275°T · 142 m* — the geodesic initial
  bearing to where the vessel is now.
- **DVL-only:** the readout relabels itself **TO SITE** and measures to the
  launch site. Same arithmetic, different claim, so it never keeps the
  vessel's label, and it says *dead-reckoned, drifts as a whole* when that is
  what the position is.

It is a direction to a point, **not a course to steer**. It knows nothing about
the tether, obstacles or current. At very small separations it says
*at/near target* rather than showing a bearing that spins.

---

## The survey plan

A drawing surface for the survey itself: measured lines, rotated boxes and the
lanes that fill them. **Nothing here is ever written to a vehicle.** It is not
an autopilot mission; it is the plan you fly by hand, drawn to scale on the
chart, saved beside the flight.

### Drawing

Pick a tool, draw, press Escape to cancel. **Pan and draw are separate modes,
always** — a tool is armed deliberately and Escape disarms it. That is the
difference between a map you can lean on and one where a stray click adds a
vertex to somebody's survey. The cursor changes with the mode and the mode is
named on screen rather than inferred from which modifier is held.

| Tool | Draw it | Gives you |
|---|---|---|
| **Line** | click the start, click the end | length in metres, bearing in °T |
| **Polyline** | click each point, double-click or Enter to finish | per-segment length and bearing |
| **Rectangle** | drag a box; Shift-drag a corner to rotate | L × W in m, area in m², orientation °T |
| **Survey grid** | drag a box; lanes fill it | the rectangle plus its lanes |
| **Circle** | click the centre, drag the radius | radius, diameter, area |
| **Polygon** | click each corner, double-click or Enter to close | area and perimeter |

New vertices snap to existing ones within a dozen pixels. Undo and redo cover
every edit. The plan autosaves into the flight folder and exports to GeoJSON
(WGS84 only — an export in any other CRS is refused rather than silently
reprojected).

### Metres, never pixels

Every number comes from the geometry, not from the screen. A rectangle is
stored as centre, length, width and rotation — **not** as four corners — so no
sequence of handle drags can shear it into a parallelogram, which is exactly
what storing corners allows. Dimensions are drawn live on the edges while you
drag, from the same `measurements()` call the inspector uses, so the two cannot
disagree.

Type an exact figure into the inspector when clicking is not good enough: "30.0
m at 125°T" with either endpoint held fixed, a rotation to the degree, a lane
spacing to the centimetre.

The one thing pixels decide is whether a label *fits*. An edge too short on
screen to carry its dimension legibly is left unlabelled rather than stacked on
its neighbours, and the block in the middle of an area is held back until the
box is big enough for its number of lines. Zoom in and they come back; the
inspector has them at any zoom.

### The edge-offset rule

Every choice here is defensible and they give different answers, so it is
stated rather than left to the code:

> Lanes are inset **half a spacing** from each edge, and the remainder is
> distributed evenly between them. For a width `W` and a requested spacing `S`
> the lane count is `max(1, ceil(W / S))`, and the **effective** spacing is
> `(W − S) / (count − 1)` — never *more* than what was asked for.

So a width that does not divide by the spacing gets slightly tighter lanes
rather than a bare strip along one edge. **2 m in a 5 m width gives three lanes
at 1.5 m**, not two at 2 m with a metre unswept. The inspector reports the
effective spacing whenever it differs from the requested one.

A 30 × 20 m box at 2 m gives ten lanes at exactly 2 m, the first and last 1 m
from their edges.

Lanes run along the length or across it, start from any of the four corners,
and reverse alternately — which is what an ROV actually flies. An optional
second pass runs at right angles to the first. Lanes can be marked done or
skipped, because resuming an interrupted grid is the normal case, not the
exception.

### Coverage is not lane-flying

**Flying the centre lines is not surveying every square metre.** Nothing in
this module knows how wide the camera sees, so by default the plan reports:

> *coverage not established — no effective swath width has been given, and
> flying a lane's centre line does not survey the strip either side of it*

Supply a **Swath** in the inspector and it will compute overlap, any gap
between lanes, and the uncovered strip at each edge — labelled as what it is:
planned coverage from a stated swath width. It is **not** observed coverage,
and it accounts for nothing about altitude, attitude, visibility or what was
actually flown.

---

## Following a line

Select a line, a polyline segment or a grid lane and press **Fly this line**.
The strip under the map becomes the guidance:

```
FOLLOWING EBM box · lane 3
1.1 m right
16 / 30 m along · 14 m to run · 53% of 30 m
```

**"Right" means right of the intended track, looking along it** — the line's
own direction of travel, not the vehicle's bow. A vehicle crabbing sideways in
a current still gets the correct side, which a bow-referenced number would not
give.

**Along-track progress is a projection onto the line, not distance flown.**
Wandering off and back does not inflate it, and it does not run backwards when
the vehicle does — the readout shows the current projection while the
completion figure keeps the furthest point reached.

**No relative turn is offered.** The vehicle compass's north reference has not
been verified against anything, and a magnetic heading read against a true
bearing is about 15° wrong at Seattle. The line's bearing is reported
separately from any heading, and the two are never subtracted for you.

**Nothing claims arrival from a stale position.** Guidance needs a current fix;
without one the strip says why instead of showing a number. A position gap is
counted and adds no path length, and an estimator jump is not counted as
distance flown.

The corridor is a **stated width**, not an accuracy claim: it colours the
readout when you are inside it and says what it is. It knows nothing about the
tether, obstacles or current.

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

## Energy, and where the flight gauges went

**There is no longer a power gauge on any page.** This chapter used to carry
one, and an altitude gauge; they went in the September 2026 restructure,
because the space mattered more than the dials did and Cockpit already shows
the pilot the same numbers.

Everything below still describes how the numbers are computed, because the
computation is still running: `nav/power.py` is unchanged, the collector reads
it every cycle, and the watt-hour total goes into the session log, the flight
summary and the exported CSVs. Only the dial went.

The gauge itself is still in `gui/navgauges.py` and still tested — fixed at
**0–1,000 W**, never rescaling, 900 W marked in red and hatched, the marker
pinning above 1,000 W with the true number kept and an over-range mark, and a
separate chevron for the **observed peak** ("observed" because telemetry cannot
catch every electrical transient). Nothing draws it today.

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
whether the vehicle has sent anything. **Message health** under *Health…*
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

**The DVL position row says "relative aiding"**
→ The extension is sending `POSITION_DELTA`, which reaches `writeBodyFrameOdom`
and so gives relative rather than absolute aiding. **With a confirmed origin
that is still a usable dead-reckoned position**, which is why it is an advisory
and not a blocker. For absolute aiding, set the Water Linked DVL extension's
message type to `POSITION_ESTIMATE` in its own BlueOS page.

**Recv and Valid are green but "Fused" says `?`**
→ Nothing observable confirms the estimator is using that particular sensor,
and ArduPilot publishes aiding mode rather than per-instance fusion, so the
honest answer is a question mark. Press **Health…** for the basis behind the
verdict and for `EKF_STATUS_REPORT`. `EKF_CONST_POS_MODE` means no horizontal
aiding at all — that one *is* a fault.

**The map is blank and says "no basemap — grid only"**
→ You are on a fetched layer with nothing cached. Switch to **Pier 59 chart
(offline)**, which is in the repository and always there.

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
- [ ] *Health… → Message health* lists `ATTITUDE`, `VFR_HUD`,
      `GLOBAL_POSITION_INT`, `RANGEFINDER`, `NAMED_VALUE_FLOAT`,
      `BATTERY_STATUS` with plausible rates.
- [ ] Unplug the tether: within a few seconds every reading goes stale and the
      footer says so. **Nothing goes to zero.**
- [ ] Plug it back in: readings recover without restarting the program.
- [ ] Cockpit runs at the same time, video and all, and neither stutters.

**Discovery**
- [ ] *Health…* shows the DVL and WL UGPS External ports, and whether each was
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
| Saved sites / planned transects overlay | **done** — imported from GeoJSON and drawn dashed beneath everything live. `survey.Site` carries names, dates and transect *times* and no coordinates, so there was no existing format to reuse |
| GeoJSON/CSV import-export | **done** — waypoints and tracks export; survey plans import, with wrong-way-round coordinates named rather than plotted |
| Tracks break on resets, longitude wraparound handled | **done** — tested |
| Uncertainty only when actually estimated | **done** — no invented accuracy radius |
| Altitude gauge, two modes, 0.8 m at the exact midpoint, hysteresis | **done** — tested through the acceptance sequence |
| Surftrak target distinct from the survey reference, `RFTarget` by key | **done** — tested |
| Flight Ops HUD: three rows exactly | **withdrawn** — the flight and power gauges were taken off this page in the September 2026 restructure and are not drawn anywhere now. The widgets and their tests remain in `gui/navgauges.py`; the telemetry behind them is still collected and still used |
| Power gauge fixed 0–1,000 W, 900 W band, observed peak, over-range | **built and tested, not displayed** — see above |
| Wh over real intervals, gaps/duplicates/stale refused, coverage shown | **done** — tested, and cross-checked against the extractor on a real transect to 0.4% |
| Sensor matrix: configured / receiving / valid / fused | **done** — tested. Four columns, not three: the old "used" inferred fusion from general estimator flags, which is not evidence about any particular sensor |
| Profiles configuration-driven and versioned | **done** |
| Deliberate switching: review → apply → read back → verify, partial handled | **done** |
| Source-set switching offered only when the set is configured | **done** — tested |
| Origin: mechanism detection, staging order, saved ≠ active | **done** — tested |
| Versioned session log, manifest, append-only events | **done** — tested |
| Replay, synthetic faults, speed-independent energy, no vehicle writes | **done** — tested |
| No vehicle writes from replay, panel opening or reconnect | **done** — enforced structurally and tested |
| Bounded queues, no GUI-thread blocking, clean stop | **done** — the window never joins a worker |
| Compact fallback for small windows, minimum viewport recorded | **done** — see below |
| Hour-long soak with measurements | **done** — see below |
| **Live-vehicle validation** | **pending** — nothing here has touched an ROV |

### Measured cost

A 20-minute soak with the page replaying a looped 30-minute dive, on the
development machine (Windows 11, 20 logical cores, 150% display scaling,
1920×1080 window), running the real `mainloop()` rather than a driven update
loop. "Tick lateness" is how late the page's own 250 ms timer actually fires,
which is the number that means *did the UI stall*.

| | at 1× (a real flight's rate) | at 4× |
|---|---|---|
| CPU, mean | **17.9%** of one core (~0.9% of the machine) | 18% |
| Memory, start → end | 170 → 224 MiB | 170 → 224 MiB |
| Memory growth, second half | **+0.6 MiB** — it plateaus | −4 MiB |
| Tick lateness, median | **21 ms** | 21 ms |
| Tick lateness, p99 | 27 ms | 30 ms |
| Tick lateness, worst | 64 ms | 71 ms |
| Ticks over 500 ms late | **0** | 0 |
| Threads, start → end | 52 → 44 (they retire) | 53 → 43 |

Memory settles at about 220 MiB and stops; threads go down rather than up; the
page holds its 250 ms cadence with a worst case of 64 ms late across 2,224
ticks. CPU is flat between 1× and 4× because the cost is the redraw, not the
polling.

**It was three times worse before it was measured.** The first soak found 47%
of a core and a median tick 143 ms late, which is a visibly sluggish page. A
profile found three things, none of which would have been guessed:

* `xy()` asked Tk for the canvas size on **every plotted point** — 1,262 Tk
  round-trips per redraw for a number that cannot change during one. The
  projection origin is now computed once per draw.
* every label on the page was reconfigured every tick, including the eight
  matrix rows in nine that had not changed. CustomTkinter's `configure` reads
  the current value back out of Tk first, so this cost about 46 ms a tick.
  Labels now compare before they set.
* every visible tile was converted into a fresh `PhotoImage` on every redraw,
  and every *absent* tile was looked for on disk again each time.

Together those took Tk round-trips per redraw from 386,000 to 28,000.

### Minimum practical viewport

Measured on the development machine at 150% Windows scaling:

| Window | Logical page | Layout | Verdict |
|---|---|---|---|
| 1920×1080 | 1888×791 | map beside one column | **comfortable** |
| 1600×900 | 1568×611 | map beside one column | **good** |
| 1366×768 | 1334×479 | map beside one column | **usable** — the matrix scrolls |
| 1280×800 | 1248×511 | map beside one column | **usable** |
| 1100×740 | 1068×451 | stacked | **cramped** — below the practical minimum |

**Minimum practical: about 1280×800 logical.** Below that the sensor matrix
scrolls, which the design tries to avoid. At 150% scaling that means a
1920×1200 panel; at 100% it means a 1280×800 one.

Below 1100 logical pixels wide the side column moves under the map rather than
beside it. Within the column, readiness and the survey plan share the height in
a **uniform** grid group with floors of 260 and 240 logical pixels — without
`uniform` Tk gives each row its requested height first and splits only the
remainder, and the plan panel asks for far more than a matrix of labels does,
which left the readiness card one row tall.
