# Transect tracks figure — project history

**Working folder:** `OneDrive - Seattle Aquarium\Desktop\transect_tracks`
**Session:** `99619052` — 2026-08-17, 25 prompts
**Purpose:** builds **Figure 2** of the MEE manuscript — the ROV positioning figure.
**Status:** complete. Four-panel PDF produced; ready for LaTeX inclusion.

> This is a **standalone working folder, not a git repository.** Nothing here is
> version-controlled, which makes the Dropbox copy in this archive the only backup.

---

## 1. What the figure shows

The scientific argument is a **three-stage progression in ROV positioning quality**,
told visually — deliberately including messy data as evidence that an underwater
acoustic positioning system was **not** working well before the fixes.

| Panel | Data plotted | What it demonstrates |
|---|---|---|
| **(a)** | Raw USBL lat/lon, per-transect colours | Consistently very noisy tracks |
| **(b)** | Raw USBL lat/lon, per-transect colours | Noisy tracks *plus* two wild swings from the antenna bobbing in swell as the vessel moved |
| **(c)** | USBL (black) + DVL (blue) | Post-hardware-modification improvement; not yet EKF-fused |
| **(d)** | USBL (black) + DVL (blue) + EKF (red) | Current state — smooth, accurate, analysis-ready tracks |

The hardware story behind the panels: USBL hardware modifications, addition of a
**GNSS satellite compass**, and BlueOS Extension software improved raw USBL
performance; then fusion of USBL + DVL + compass + IMU (Navigator Flight
Controller) through **ArduSub's EKF** produced the final positioning solution.

Upstream processing: `tlog_to_csv.py` → `transect_map.py`.

---

## 2. How it was built

**Started with Leaflet.** The first ask was a simple R script plotting lat/lon from
CSVs on a Leaflet map, one colour per transect, with legend and scale bar.

**Abandoned Leaflet for print.** Leaflet pins the legend and scale to the corners at
a fixed small size — unusable in a manuscript figure. Switched to a standard R
plot zoomed to the track extent.

**Then consolidated into one PDF.** Initially each panel was its own PDF, invoked
separately via `\subfloat` in LaTeX. This produced subfigures that were too small,
because non-uniform lat/lon axis text ate the width. The fix — proposed and
executed:

> Build **all four panels in R** as a single figure, place the (a)/(b)/(c)/(d)
> captions **inside** each pane at the upper left, and emit **one PDF** that LaTeX
> includes with a single `\includegraphics`. This also allows a **single shared
> "Latitude" and "Longitude" axis title** — left of and beneath the whole figure —
> instead of repeating them per panel.

---

## 3. Figure conventions settled

- **Legend labels:** `T1`–`T4` for per-transect panels; `UGPS` / `DVL` / `EKF` for
  the source-comparison panels. Parenthetical text (`(Lat/Lon)`, `(EKFLat/EKFLon)`)
  removed as redundant. Note **UGPS**, not GPS.
- **Colour scheme:** panels (a) and (b) share per-transect colours; panels (c) and
  (d) use black / blue / red by data source.
- **Legend position:** upper right, inside the main plot box — chosen for layout
  efficiency.
- **North arrow:** above the left-hand end of the scale bar.
- **Latitude axis text tilted** to reduce the width it consumes.
- Axis text, axis titles, and legend text all enlarged — a recurring requirement
  across every figure in this body of work.
- Output as **PDF** (vector, for LaTeX) with PNG alongside for preview.

---

## 4. Files

```
transect_tracks/
├── common_figure_style.R       shared theme — sizes, fonts, colours
├── panel_builders.R            panel construction
├── plot_subfig_a.R  … _d.R     individual panels
├── plot_transects.R
├── plot_transects_combined.R   the four-panel composite
├── plot_transects_figure.R
├── subfig_a/ … subfig_d/       input CSVs per panel
├── subfig_a.pdf/.png … d       individual outputs
└── transects_combined.pdf/.png the figure actually used
```

Input CSVs carry `lat`/`lon` in decimal degrees, plus `DVLlat`/`DVLlon` and
`EKFlat`/`EKFlon` where applicable.

> **Panel-letter drift:** panels were renumbered mid-session when a new dataset was
> inserted — what had been `subfig_a` became `subfig_b`, and so on. If cross-checking
> against older notes or an older manuscript draft, verify which panel is which.

---

## 5. Carry-forward notes

- `common_figure_style.R` is the single place to change text sizing across all
  panels — use it rather than editing panels individually.
- The manuscript caption text is reproduced in the transcript and was updated
  during the session; the version in `manuscript.tex` is authoritative.
- Because this folder is outside any repo, consider moving it into
  `CCR_ROV_survey_methods` if the figure needs to live alongside the manuscript.

---

*Companion transcript: `transcripts/Desktop__transect_tracks/`.
Related: `manuscript_MEE_HISTORY.md` — this figure is Figure 2 of that paper.*
