# Vehicle reference parameters

`nereo.params` is the ArduSub configuration Nereo is meant to have. `rov_flight_ops` reads it from here (`rov_flight_ops/paramfile.py`), so a `git pull` on a field laptop is all it takes to get the current reference.

## The file

ArduPilot's standard parameter format: one `NAME,VALUE` per line, sorted by name. MAVProxy (`param load`) and Mission Planner read it as-is. When the reference changes, the pull request shows only the lines that changed.

The `#` lines at the top record where the values came from: vehicle, firmware, flight, dataflash log, and when they were read. Leave them in. They let a comparison report when the vehicle runs different firmware from the reference.

Six parameters are deliberately **left out**. The autopilot rewrites them on every flight: `BARO1/2/3_GND_PRESS` and `STAT_BOOTCNT/FLTTIME/RUNTIME`. With them in, every check would raise a false alarm.

## Refreshing it

After a flight whose configuration should become the new reference, run this from `rov_flight_ops/`, using that flight's record from its `logs/` folder:

```bash
python -m rov_flight_ops.paramfile "<flight folder>/logs/flight_<id>.json"
```

It rewrites `nereo.params` and lists every parameter that differs from the previous version. Review that list, then commit. An ArduPilot `.BIN` log works as the source too, but its header can't name the BlueOS version.

Hand edits are fine: change the value on its line. If a line doesn't parse, it's reported instead of silently skipped, and the tests fail on it.

## Current reference

From flight `2026-10-15_103918` (Pier 59, dataflash log `00000025.BIN`), ArduSub 4.5.7 STABLE on a Navigator, BlueOS 1.4.6. That flight used the **acoustic** navigation profile: `EK3_SRC1_POSXY = 3` (GPS, i.e. the UGPS position). The DVL-only profile uses `6`.
