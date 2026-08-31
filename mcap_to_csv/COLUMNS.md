# Transect column provenance

Every column in a transect CSV, what it means, and where the number actually
came from. Read the **Origin** column before trusting a figure: it is the
difference between a measurement and an estimate.

44 columns, one row per second, local times in US/Pacific.

## Six kinds of number

A column is only as trustworthy as its shortest path back to an instrument.

| Origin | Meaning |
| --- | --- |
| **Direct** | Read straight off one sensor, or reported by the autopilot. Unit conversion only. |
| **Fused** | The autopilot's EKF combined several sensors to produce it. Smoother than any one of them, and no longer traceable to a single instrument. |
| **Computed** | This tool calculated it from other columns. No new information — inherits the trustworthiness of its inputs. |
| **Calibrated** | Computed using a constant measured off the camera rig, not the vehicle. Wrong if the camera, lens or housing changes and the constant does not. |
| **Entered** | Typed in by whoever ran the extraction, or read from the survey plan. |
| **External** | Came from outside the vehicle entirely — the NOAA tide station. |

## The columns

In file order. The first 31 match `tlog_to_csv.py` exactly.

### Identity and time

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Date` | Local calendar date. | mcap `log_time` → US/Pacific | Computed | — |
| `Time` | Local clock time, `HH:MM:SS`. This is what transect windows are written in. | mcap `log_time` → US/Pacific | Computed | — |
| `Datetime_UTC` | The same instant in UTC, for joins that must not depend on daylight saving. | mcap `log_time` | Computed | — |
| `Site_name` | Survey site. | typed in, or from the survey plan | Entered | — |
| `Transect_number` | Order of this transect within the run, from 1. | this tool | Computed | — |
| `Transect_ID` | Transect name. Also the CSV filename. | typed in, or from the survey plan | Entered | — |
| `Messages` | How many MAVLink messages went into this second. A thin row is a dropout. | counted while reading | Computed | count |

### Vehicle state

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Mode_num` | ArduSub flight mode, as a number. | `HEARTBEAT.custom_mode` (system 1, component 1) | Direct | last |
| `Mode` | The same mode by name — `MANUAL`, `ALT_HOLD`, `SURFTRAK`. | lookup table applied to `Mode_num` | Computed | last |

### Power

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Battery_V` | Pack voltage. | `BATTERY_STATUS.voltages[0]` ÷ 1000; falls back to `SYS_STATUS.voltage_battery` | Direct | mean |
| `Battery_A` | Current draw. | `BATTERY_STATUS.current_battery` ÷ 100 | Direct | mean |
| `Battery_W` | Power draw. Genuinely instantaneous — voltage and current arrive in the same message. | `Battery_V × Battery_A`, per message | Computed | mean |
| `Battery_mAh_used` | Charge used since this transect began, not since power-on. | `BATTERY_STATUS.current_consumed` minus its value in the first row | Computed | last |
| `Battery_Wh_used` | Energy used since this transect began. | `BATTERY_STATUS.energy_consumed` × 100 ÷ 3600, minus its value in the first row | Computed | last |

### Position

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Latitude` | Surface position from the acoustic tracker. Repeats unchanged whenever the tracker has no lock. | `GPS_RAW_INT.lat` ÷ 1e7 — Water Linked UGPS, injected as `GPS_INPUT` | Direct | last |
| `Longitude` | As above. | `GPS_RAW_INT.lon` ÷ 1e7 | Direct | last |
| `EKFlat` | Fused global position. **Blank whenever the EKF has no absolute fix**, which is every dive without a locked USBL. | `GLOBAL_POSITION_INT.lat` ÷ 1e7 | Fused | last |
| `EKFlon` | As above. | `GLOBAL_POSITION_INT.lon` ÷ 1e7 | Fused | last |
| `DVLx` | Metres north of the transect start. Re-zeroed at each transect. | `LOCAL_POSITION_NED.x` when recorded — else `VISION_POSITION_DELTA` integrated and rotated by `ATTITUDE.yaw` | Fused / Computed | last |
| `DVLy` | Metres east of the transect start. | `LOCAL_POSITION_NED.y`, or the same integration | Fused / Computed | last |
| `DVLlat` | The DVL track as coordinates. Propagated once across the whole dive, so transects keep their true separation. | geodesic walk of the `DVLx`/`DVLy` steps from the dive's first valid fix | Computed | — |
| `DVLlon` | As above. | as above | Computed | — |
| `DVL_source` | Which of the two fed `DVLx`/`DVLy` on this dive. | this tool | Computed | — |

### Depth and altitude

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Altitude` | Metres above the seabed. Drives `Width` and `Area_m2`. | `RANGEFINDER.distance` — the DVL A50's own range; falls back to `DISTANCE_SENSOR` id 0 ÷ 100 | Direct | mean |
| `Depth` | Metres, **negative down**. | first available of `VFR_HUD.alt` (< −0.5), `GLOBAL_POSITION_INT.relative_alt` ÷ 1000, −`LOCAL_POSITION_NED.z`, or derived from `SCALED_PRESSURE2` | Fused | last |
| `Depth_Source` | Which of those four answered, row by row. | this tool | Computed | — |
| `Depth_std` | Seabed depth on the MLLW datum, so dives at different tide stages compare. | −`Altitude` + `Depth` + NOAA water level | External / Computed | — |
| `NEDz` | Local-frame z, positive down. Blank when the message is not recorded. | `LOCAL_POSITION_NED.z` | Fused | last |
| `VFR_alt` | The HUD's altitude field. Reads a flat zero on some vehicle configurations. | `VFR_HUD.alt` | Fused | last |
| `Relative_alt_m` | The autopilot's own baro-derived depth, negative down. | `GLOBAL_POSITION_INT.relative_alt` ÷ 1000 | Fused | last |
| `Pressure_abs_hPa` | Absolute water pressure. Independent of the EKF, which makes it a useful cross-check on depth. | `SCALED_PRESSURE2.press_abs` (external Bar30) | Direct | mean |
| `Water_temp_C` | Water temperature at the depth sensor. | `SCALED_PRESSURE2.temperature` ÷ 100 | Direct | mean |

### Attitude and motion

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Heading` | Degrees from north. A yaw error here rotates the whole DVL track. | `ATTITUDE.yaw` → degrees (compass + gyro + accelerometer) | Fused | circular mean |
| `Roll` | Degrees. | `ATTITUDE.roll` → degrees | Fused | mean |
| `Pitch` | Degrees. | `ATTITUDE.pitch` → degrees | Fused | mean |
| `Velocity_mps` | Speed over ground. Cleaner than the HUD's figure, which carries filter spikes. | `VISION_POSITION_DELTA` horizontal magnitude ÷ its own `time_delta_usec`; falls back to `VFR_HUD.groundspeed` | Direct | mean |
| `Distance` | Metres travelled during this second. Sum it for transect length. | change in `DVLx`/`DVLy` from the previous row; steps under 2 cm count as zero | Computed | — |
| `DVL_confidence` | The DVL's own confidence in its bottom lock, as a percentage. | `VISION_POSITION_DELTA.confidence` | Direct | mean |

### Camera footprint

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Width` | Metres of seabed across the frame. | `1.10 m × (Altitude ÷ 0.82 m)` — scales linearly with altitude | **Calibrated** | mean of samples |
| `Area_m2` | Square metres of seabed in the frame, at that instant. | `0.99 m² × (Altitude ÷ 0.82 m)²` — scales with the square of altitude | **Calibrated** | mean of samples |

### Pilot settings and fix status

| Column | What it is | Where it comes from | Origin | Per second |
| --- | --- | --- | --- | --- |
| `Lights_pct` | Light output as a percentage. | `NAMED_VALUE_FLOAT "Lights1"` × 100 | Direct | last |
| `Cam_tilt` | Camera tilt setting. | `NAMED_VALUE_FLOAT "CamTilt"` | Direct | last |
| `GPS_fix_type` | Fix state of the acoustic tracker. `NO_GPS` means the positions are dead reckoning. | `GPS_RAW_INT.fix_type` | Direct | last |
| `GPS_satellites` | Locator count the tracker reports. | `GPS_RAW_INT.satellites_visible` | Direct | last |

---

## Rules that apply to every column

The recording arrives at many different rates; the CSV is one row per second.

### Averaged, or last seen

Continuous quantities are averaged over **their own samples** in that second —
not over every message, so a busy second does not weight one sensor more than
another. Sampled states take the **last** value instead; averaging a flight mode
or a fix type would be meaningless.

`Heading` is averaged as unit vectors. A plain mean of 359° and 1° is 180°,
which would point a due-north transect due south.

### How long a value is held

When a stream stops, its last value carries forward — but only so far. Past the
limit the cell goes blank, so a dead sensor leaves a hole rather than a flat line
that looks healthy.

| Columns | Held for |
| --- | --- |
| `Mode`, `Mode_num` | 600 s |
| `Lights_pct`, `Cam_tilt` | 60 s |
| `Battery_mAh_total`, `Battery_Wh_total` | 30 s |
| `Latitude`, `Longitude`, `GPS_*` | 15 s |
| `EKFlat`, `EKFlon` | 10 s |
| `DVLx`, `DVLy`, `NEDz` | 10 s |
| everything else | 5 s |

### The camera constants

`Width` and `Area_m2` are the only columns that depend on a measurement made
outside the vehicle: a reference frame of **1.10 m × 0.90 m** (0.99 m²) at an
altitude of **0.82 m**, after the 4606×4030 crop.

> If the camera, lens, housing or crop changes, these become quietly wrong —
> nothing in the data will look unusual. Update `REFERENCE_WIDTH_M`,
> `REFERENCE_AREA_M2` and `REFERENCE_ALT_M` in [ccr_m2c/mcap_read.py](ccr_m2c/mcap_read.py).

### Two traps in the footprint

**Do not sum `Area_m2`** to get ground covered. At survey speed that counts the
same patch once per second the ROV was over it, inflating the total by orders of
magnitude. For ground covered use mean `Width` × total `Distance`.

The per-second `Area_m2` is the mean of the individual sample areas, not the area
at the mean altitude. Because area goes as altitude squared the two differ, and
the mean of samples is the honest one.

---

To check the navigation behind any dive — which sensors the EKF actually had, and
how they behaved — run:

```bash
python -m ccr_m2c --health <file.mcap>
```
