# ROV survey flight log

`ROV_survey_flights.csv` lists every ROV survey flight from August 2022 to
September 2026, one row per transect as recorded in the field. It is the
source of the survey totals in the manuscript: **303 transects on 64 survey
days**.

| Column | Meaning |
|---|---|
| `date` | Survey date (local time, Pacific) |
| `program` | Port of Seattle, HSIL or Olympic Coast; `Other` for flights outside a survey program |
| `region` | Elliott Bay, Central Puget Sound, San Juan Islands or Strait of Juan de Fuca |
| `port_site` | Port of Seattle site number (1–8), where one was recorded |
| `location` | Site name |
| `transect` | Transect label as logged in the field (`NA`: no transect flown) |
| `vehicle` | ROV *Nereo* or *Lutris* |
| `start`, `end`, `pauses` | Transect start and end times and any mid-transect pauses, from the GoPro Labs timecode. Where the field log had none, they come from the flight's `utc_plan.json` / `surveys.json` (2026) or its per-transect telemetry (2024). Earlier transects without either are blank. |
| `counted` | Whether the row counts as a survey transect |
| `transects_counted` | Transects the row contributes to the total (usually 1; see below) |
| `note` | Why a row is not counted, or how it is counted |

**Counting rules**
- **Not counted:** rows marked `NA` (no transect flown) or aborted, and test,
  outreach and partner-demonstration flights or short aborted dives. The
  `note` column gives the reason for each.
- **Split transects:** a transect flown in two parts counts once.
- **Diver–ROV comparison days:** the ROV's passes along each 100 m tape were
  processed as three 30 m transects, to match the divers' Reef Check
  transects. The field log records these days as four passes, so the day's
  first row carries all six transects.
- **Survey day:** a calendar date on which at least one transect counted.

To reproduce the totals (standard library only):

```bash
python survey_log/survey_log.py
```

This prints totals by year, program and region, and the reasons for rows not
counted. `python survey_log/survey_log.py export --xlsx <flight log.xlsx>`
regenerates the CSV from the team's tracking spreadsheet; it needs openpyxl
and leaves out the spreadsheet's personnel and notes columns.
