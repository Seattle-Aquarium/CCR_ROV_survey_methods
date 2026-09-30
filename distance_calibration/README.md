# Distance calibration against 100 m transect tapes

ROV Imagery Processing places a mark every metre along a transect and picks the
photograph nearest each mark. The distance comes from the vehicle's telemetry
(`rov_imagery_processing/rov_imagery_processing/metermark.py`), and each
possible source carries a calibration factor. This folder checks those factors
against transects of known length.

**The known lengths.** On the diver–ROV comparison days a 100 m tape was laid
on the seafloor and the ROV flew along it end to end, several times. The field
log records when each pass started and finished at the tape's ends. Later
processing splits each pass into 0–30, 35–65 and 70–100 m segments to match the
divers' Reef Check transects. `tape_passes.csv` lists all 40 logged passes on
ten days (Oct 2024 – Sep 2025). It marks five as unusable, with the reason for
each:
- the turn at 100 m was not logged;
- a 3-minute fragment;
- a pass that ended at 40 m after an entanglement;
- a logged start that falls inside the previous pass;
- a pass split between two vehicles.

**The measurement.** For every pass, `tape_calibration.py` reads the flight's
`.tlog` recording (using the repo's `mcap_to_csv` reader) and measures the
distance travelled with each source. It uses the imagery program's own
functions:

| Source | What is measured |
|---|---|
| `ekf_velocity` | EKF velocity (`LOCAL_POSITION_NED` vx, vy), integrated at sensor rate |
| `dvl` | DVL odometry (`VISION_POSITION_DELTA` dx, dy), summed |
| `gps_velocity` | `GLOBAL_POSITION_INT` velocity, integrated |
| `ekf_position` | `LOCAL_POSITION_NED` position, differenced |

A source is scored on a pass only if its samples span at least 90% of it.

```bash
python distance_calibration/tape_calibration.py --root path/to/flights
```

The script writes `tape_calibration_passes.csv` (one row per pass) and
`tape_calibration_summary.txt`. It needs pymavlink and numpy.

## Results (35 usable passes, 10 days)

| Source | Passes | Uncalibrated, median share of tape (IQR) | Factor implied | Factor in the program |
|---|---|---|---|---|
| EKF velocity | 28 | 99.3% (97.7–100.9%) | 1.007 | 1.010 |
| DVL odometry | 33 | 96.3% (94.4–97.8%) | 1.038 | 1.037 |
| GPS velocity | 33 | 92.0% (90.9–93.6%) | 1.087 | 1.058 |
| EKF position | 28 | 104.5% (100.6–111.9%) | 0.957 | 0.979 |

- **The two main factors check out.** For the sources the program actually
  uses (EKF velocity on 30 passes, DVL on the 5 flown without positioning),
  the program's factors agree with these measurements to within 0.3%.
- **Program as run.** First available source, calibrated: median **99.6%** of
  the tape, mean absolute error 4.3%.
- **What inflates that error.** A few passes whose logged window does not
  match exactly one pass along the tape: 2024-10-09 passes 1 and 4,
  2024-12-05 shallow 1 and 2025-08-28 T4. Every source agrees on those, so
  the vehicle really travelled that far inside the window.
- **Fallback sources.** The factors for GPS velocity and EKF position differ
  more from these measurements. The program uses them only when neither EKF
  velocity nor DVL odometry was recorded.

**Sensor rate versus 1 Hz.** On the 30 passes with EKF velocity, the script
also integrates that velocity as a 1 Hz table would: held once per second.
It places the one-metre marks both ways and matches them to photographs
taken every 3 s. 864 of 2,985 marks (**29%**) land on a different
photograph. This is why the program measures distance at sensor rate
rather than from the 1 Hz transect CSVs.
