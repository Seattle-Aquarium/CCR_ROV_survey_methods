# On the shelf

Parts of ROV Flight Operations that are **built, tested and kept, but not shown
in the window** — taken out in November 2026 because the field laptop's screen
is worth more to what the team uses every flight. Nothing here was deleted:

* every module is where it always was, and nothing that is shown imports it;
* its tests still run in CI, so it cannot rot while it waits;
* bringing it back is **one line in the settings file** — no code change.

## Bringing one back

Close the program, open

```
%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\settings.json
```

and list what you want back under `"unshelve"`, then start the program again:

```json
"unshelve": ["flight_summary", "analyze_transects", "survey_plan"]
```

`shelf.py` holds the list; `tests/test_field_refinements.py` checks that each
shelved module still exists.

## What is on it

| key | what it was | where its code is | what replaced it in the window |
|---|---|---|---|
| `survey_plan` | **Navigation → Survey plan**: measured lines, rotated boxes and survey grids drawn on the map, undo, GeoJSON export, and live guidance (cross-track / along-track) along a chosen line or lane. | `gui/navplanpanel.py`, `gui/navdraw.py`, `nav/plan.py`, `nav/guidance.py` | The flight numbers (altitude, depth, forward, gain, TC-25, volts/amps/watts) head the right-hand column as the heads-up display, the UGPS receivers sit under them, and navigation readiness fills the rest. The guidance cell is dropped from the strip under the map. |
| `flight_summary` | **Flight summary tab**: *Read the day* (the flight report PDF) and recording health — damage checks on mcaps, repaired copies, and choosing a `.BIN` as telemetry when an mcap failed. | `gui/summarypage.py`, `gui/healthpage.py`, `mcap_health.py` (the report itself is `flightscan.py`, `flightreport.py`, `tearsheet.py`, which stay in use) | The flight report is built automatically after a download on **BlueOS logs** (and opens when it is done), and **Build the report now** builds it by hand. |
| `analyze_transects` | **Analyze transects tab**: per-transect CSVs and a site map from the transect extractor, and a sensor-health report. | `gui/transectpage.py` (the extraction it drove now lives in `transectcsv.py`, which stays in use) | **BlueOS logs → Download files → After downloading → 1 Hz transect CSVs** makes the same CSVs into `transects/` as soon as the recordings are down, from mcaps or tlogs. The sensor-health report is not offered anywhere else for now — it is the part most likely to move into a separate post-processing program. |

## Notes for whoever brings one back

* **Survey plan.** With it back, the right-hand column is heads-up display,
  UGPS receivers, then readiness and the plan with the draggable divider
  between them, as before. A plan saved in a flight folder (`survey_plan.json`)
  is loaded again when that folder is chosen — while shelved, it is left alone.
* **Flight summary.** Recording health reads mcaps only; on a tlog flight it
  finds nothing to check. The flight report itself reads tlogs (one recording
  per armed period) since November 2026.
* **Analyze transects.** Its *Recordings* card and *Check sensors* read mcaps
  only (`ccr_m2c.health` has no tlog reader yet). Its *Extract* button still
  works on mcap flights; `transectcsv.make` is the version that reads tlogs too,
  and the tab should be switched over to it if it comes back.
