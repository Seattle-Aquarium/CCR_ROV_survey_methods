# ROV video/telemetry compositing — the origin of the UTC

**Working folder:** `Coastal_Climate_Resilience\flights\testing\2026_08_21_DeepSea_light_testing`
**Session:** `33d5b025` — 2026-08-23, **65 prompts, 6,537 transcript lines** (the
longest single session in the archive; it ran out of context once and was continued)
**Outcome:** the **UTC** (Underwater Telemetry Compositing) program, now living at
`CCR_ROV_survey_methods/UTC/`.

This session is the single most valuable transcript in the archive. It contains
the full reasoning behind the UTC's design, and — critically — the **time-sync
method**, which is not obvious from the code alone.

---

## 1. The original problem

Take a 4K down-facing **GoPro HERO 12** MP4 from the ROV, synchronise it with a
BlueOS **`.mcap`** telemetry file, and produce a composite MP4 containing:

- the ROV's built-in **forward-facing Blue Robotics low-light camera** (embedded in
  the mcap) as an **inset**, and
- **MAVLink telemetry as a text overlay**,

while preserving as much of the original 4K detail as possible, even at the cost
of processing time.

**System context:** BlueRobotics BlueROV2 · ArduSub · BlueOS · Cockpit.
GoPro shot via GoPro Labs with **white balance set to Native** to avoid colour and
light washout. Outland topside power supply, **1000 W max** to the ROV.

---

## 2. The time-sync method — the key insight

Normally, GoPro and telemetry are aligned by **GoPro Labs precision time sync**
(`https://gopro.github.io/labs/control/precisiontime/`), reading **TC25** time,
which is written down in the field at transect start and end.

**This particular flight was never TC25-synced.** The solution:

> Monitor the **brightness of a patch of the down-facing GoPro video** and correlate
> it against the **ROV light power output in the mcap**. The moment the lights first
> come on underwater is an unambiguous anchor, and the several full power-down /
> power-up cycles across the flight ground-truth the alignment.

This brightness-anchor technique is the fallback whenever TC25 sync is missing,
and it is worth preserving as a method in its own right.

**Two corrections that were found the hard way:**

- Light power is **`NAMED_VALUE_FLOAT` / `Lights1`**, *not* servo output 16. The
  initial assumption (hedged at the time as "servo 16, I believe") was wrong:
  `SERVO_OUTPUT_RAW` carries only port 0, the eight thrusters. Thruster gain is
  `PilotGain`, corroborated by `STATUSTEXT` "#Gain is 30%".
- **Use the GoPro for the brightness signal, never the ROV camera.** The Blue
  Robotics low-light camera has aggressive auto-gain — luma moves only 72→83
  across a full lights-off transition. This also explains why the inset can look
  lit while `LIGHTS` reads 0%.

Sync accuracy is bounded at roughly **±1.5 s** by GoPro **auto-exposure** smearing
each light ramp — a limit of the optics, not the correlation maths.

---

## 3. The HUD, as finally designed

Telemetry text, ordered deliberately in three groups:

```
Altitude          Mode              Power
Speed             Lights            Battery (voltage)
                  Gain              Current
                  Camera tilt
                  Temp
```

**Power is computed**, not read: Watts = volts × current. The 1000 W supply ceiling
means operational values should never approach it.

**Dynamic gauges**, added after the text panel worked:
- A **compass rose** that rotates with heading, with the heading value in text beneath.
- An **inclinometer** (pitch/roll), styled after aircraft and ROV dash displays,
  with P/R values beneath.

**Final layout**, after several rounds: compass and inclinometer **stacked
vertically** and shrunk so their combined height does not exceed the inset ROV
video, placed immediately right of the inset; the telemetry text box to the right
of those. Dead space inside the text box removed so it hugs its content (~⅓ of its
width recovered). Background transparency increased slightly on both.

A **depth-vs-time plot** with a moving golden dot tracking the ROV's position
through the dive was built and then **removed** — explicitly parked as "may bring
it back later", not rejected.

Audio is **stripped** from the GoPro track ("we don't need to listen to the ROV
thruster whine").

---

## 4. Prototyping discipline

Because full 4K composites are slow, the session established short-form outputs
that made iteration practical — a lesson worth keeping:

- A **short template clip**: 3 s before lights-on → ramp to 100 % power → hold 3 s → cut.
- A **single still frame** for layout experiments that need no video at all.
- Resolution tiers: **4K** (archival), **1080p** (review), and a smaller
  easily-shareable size.

Validation was done by viewing an inverted test file, confirming both the time
alignment and the MAVLink text output before committing to long renders.

---

## 5. Photo banner stamping

A distinct feature added mid-session. The flight photo folders hold **GPR** (raw,
processed through Adobe Lightroom or Keenan's **Underwater Image Enhancer**, and the
source of all ecological data) and **JPG** files. The JPGs were previously discarded.

The idea: stamp mcap telemetry onto the JPGs as a **banner above the image**, so
they become diagnostic records without covering a single pixel of the imagery.

Final banner fields, after tuning: **TC-25 time, Altitude** (2 decimal places —
"those details matter here"), **Speed, Lights, Depth, Power, Mode**.
**GAIN was dropped** specifically to free horizontal space for larger text.
Original filename stem retained in the output name.

> **Orientation gotcha:** the GoPro photo camera faces **backwards** on the ROV, so
> imagery is inverted on capture to correct it. An early pass inverted the
> already-inverted images and put them back upside down. Check orientation
> assumptions before batch-processing.

Banners are only ever written to **copies**, never originals — which is why a
"remove banner" feature was later deemed unnecessary.

---

## 6. From composite tool to file manager

Two expansions turned the tool into the UTC:

**Standardising the GUI.** Multiple GUIs exist across the team; this one was
preferred, so its architecture and Seattle Aquarium brand/style usage were
extracted into **`SEATTLE_AQUARIUM_GUI_GUIDE.md`** (published as an artifact, also
committed to `CCR_ROV_survey_methods/docs/`). The dark theme in particular was
singled out as the thing to standardise on. Keenan's open-source **UIE** was
reviewed as an architectural reference; it does not follow Aquarium branding, which
was accepted as fine.

**Scope expansion** (prompt 48 onward) — from compositing to holistic ROV survey
file management: create the base folder structure after a flight; create
transect-specific folders from entered transect times; rename and move GPRs and
JPGs into them; handle team-edited exports. This is the work that became the UTC
ingest pipeline. The rename from "composite" to **UTC** happened here.

---

## 7. Field-tested, with real friction

The tool was used across **five hours of ROV testing and mock surveys**
(`2026_08_26_Lutris_shakedown`). Real problems that shaped it:

- **16 `.mcap` files** from repeated testing and reboots, all large and slow to pull
  from Dropbox. Needed to identify *which mcap overlapped* transect windows
  (12:19:57–12:28:42 through 12:43:37–12:49:26) so only that one had to be
  downloaded. This drove mcap time-range inspection in the GUI.
- A **stalled composite run** — `composite_plan.json` written, then no activity.
  Prompted questions about GPU (NVIDIA) utilisation and parallelisation; the laptop
  audibly wasn't working as hard as during a prior run.
- Which files are **too large to commit** — the PyInstaller executable in
  particular needed gitignoring.

---

## 8. What lives where now

The code from this session lives in `CCR_ROV_survey_methods/UTC/` — see
`CCR_ROV_survey_methods_HISTORY.md`. The `create_composite/` folder in this flights
directory is the working copy from the session itself.

**Folder convention settled here** (naming had been inconsistent, and tightening it
up was an explicit goal):

```
flights/<date>_<name>/
├── logs/      .mcap telemetry
├── photo/     GPR + JPG
└── video/
    ├── downward/   GoPro MP4 — the one that matters
    └── forward/    GoPro MP4 — ignored; the mcap's forward camera is preferred
```

---

## 9. Pipeline gotchas — each cost real debugging time

All are handled in the code and explained in the UTC's README, but they recur on
every new flight. Preserved here because they are expensive to rediscover.

### Telemetry and timing

- **mcap `log_time` is written in bursts and is *not* the video frame time.** Use
  the `timestamp` inside each `foxglove.CompressedVideo` message.
- **The ROV camera stream is strongly variable-rate** and unreliable to seek —
  asking for 465.259 s can return the frame at 466.708 s. Hence the constant-rate
  proxy (`rov_cfr.mp4`). Assuming uniform fps drifts **158 s over 36 minutes**.
- **Decimate high-rate MAVLink *before* `json.loads`, not after.** `AHRS2` and
  `SCALED_PRESSURE2` arrive at ~300 Hz — 320k messages per dive — and parsing is
  the entire cost.

### Video

- **GoPro autorotate is a trap with `-filter_complex`.** It rotates the frames *and*
  copies the display matrix to the output, so a player rotates the finished
  composite a second time and everything — overlays included — ends up upside
  down. Use `-display_rotation 0` and apply the rotation inside the filter.
- **Every frame of an overlay PNG sequence must be identical in size.** Montserrat
  is proportional, so sizing a footer box to its own text made it breathe as the
  clock ticked; ffmpeg then rebuilt the filter graph almost every frame, dropping
  thousands and losing overlays non-deterministically — a 40 s clip took 40
  minutes instead of 57 seconds. `overlay._assert_uniform` now refuses such a
  sequence.
- **Scan the bitstream with `bytes.find`, not a Python index loop** — `has_idr`
  over ~2 GB was ~26× slower than necessary.

### Windows / environment

- **Dropbox files can be online-only placeholders.** The mcap arrived as
  `SparseFile, ReparsePoint, Offline` and reads failed with `OSError: [Errno 22]`
  until pinned with `attrib +P -U`.
- **MAX_PATH (260) is reachable.** Flight folders sit deep in Dropbox, so temp
  files must live in the short cache path, not beside the output.
- **PyInstaller must target a top-level `launch.py`**, not `composite/gui/app.py` —
  it runs its target as `__main__`, which breaks relative imports. A windowed
  build hides the traceback entirely; `COMPOSITE_DEBUG=1` builds a console variant.
- **`ImageGrab` photographs the screen, not the window** — raise and focus the
  window first, or it silently captures whatever is behind it.
- **No Ghostscript on PATH**, so PDF rasterisation via `magick` fails.

> The session began in **R** and was **rewritten in Python** on 2026-08-24. The
> second block of gotchas above dates from that rewrite. R remains the language for
> the `CCR_benthic_analyses` statistical work; the UTC is Python.

---

*Companion transcript: `transcripts/testing__2026_08_21_DeepSea_light_testing/`
(2.3 MB — the most detailed record in the archive).
Memory files: `memory_snapshot/flights-testing-2026-08-21-DeepSea-light-testing/`.*
