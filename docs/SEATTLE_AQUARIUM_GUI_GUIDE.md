# Seattle Aquarium — Desktop GUI Guide

A shared standard for the small desktop tools we build for field and lab work:
brand colour and type, the architecture that has held up in practice, and the
failure modes worth designing around.

Written to be handed to Claude. Drop it into a repo and say *"follow
`SEATTLE_AQUARIUM_GUI_GUIDE.md`"* and you should get a consistent result
whoever is driving.

- **Brand source of truth:** `SAQ-001_Visual-ID-Guidelines_FINAL_V1-0823` (v1, Aug 2023).
  All colour and type values below were transcribed from it directly and verified.
- **Reference implementation:** `CCR_ROV_survey_methods/UTC/` — a
  CustomTkinter app that runs multi-hour ffmpeg jobs. Every architectural
  pattern below is lifted from working code, not proposed in the abstract.

---

## How to use this with Claude

**New GUI**

> Build a desktop GUI for `<task>`, following `SEATTLE_AQUARIUM_GUI_GUIDE.md`.
> Use the palette and type hierarchy in Part 1 and the worker-thread/queue
> architecture in Part 2. Both themes, toggle in the header.

**Retrofitting an existing GUI**

> Read `SEATTLE_AQUARIUM_GUI_GUIDE.md`, then audit this GUI against it.
> Report what is off-brand or inaccessible before changing anything.

Ask for the audit first. Retrofits go wrong when colours are swapped one at a
time without re-checking contrast on the surface each one actually sits on.

---

# Part 1 — Brand

## What is fixed vs. what we decided

Keeping these apart matters: one is the Aquarium's identity, the other is our
interpretation of it for screen UI.

| | Source | Change it? |
|---|---|---|
| Palette hex values | Guidelines | No |
| Accessibility rules | Guidelines | No |
| Logo variant + misuse rules | Guidelines | No |
| Montserrat / Barlow Condensed Bold | Guidelines | No |
| Type hierarchy (weight, case) | Guidelines | No |
| Dark/light scheme mappings | Our decision | Yes, if you re-verify contrast |
| Pixel type scale, radius, spacing | Our decision | Yes, keep it consistent |

## Palette

Verified against p.18 of the guidelines. PMS and CMYK values are on that page if
you ever need print.

```
PRIMARY
  Salish          #004346    proprietary — the colour of the building
  Fathom          #0C2340
  Mediterranean   #1963B0    water; the Primary Logo colour

SECONDARY                    "bright ... marine life and habitats"
  Algae           #00C389
  Seafoam         #3CCBDA

ACCENT                       "pops of colour ... more flamboyant sea creatures"
  Purple Star     #7A5EA8
  Coral           #F58674

NEUTRAL
  White           #FFFFFF
  Pumice          #EEEEEE
  Stone           #575757
```

> **All seven colours are in play.** An earlier decision — recorded in
> `CCR_ROV_survey_methods_HISTORY.md` for the paper field datasheet — kept
> Purple Star and Coral out of the palette. **That was rescinded on
> 2026-09-05** and applies to nothing going forward: both are part of the
> toolkit. They are accents, so use them as accents.

> **Use the hex values, not the RGB line.** On p.18 Algae's RGB is printed as
> `0 | 195 | 37`, but its hex `#00C389` is `0 | 195 | 137` — the RGB line is
> missing a digit. Every other colour agrees.

**Stone tints** — Stone is the only colour the guidelines permit tinting, which
makes it the workhorse for UI chrome (borders, disabled states, hover fills):

```
10  #E6E6E6      20  #CDCDCD      40  #9B9B9B      60  #6A6A6A      80  #3A3A3A
```

## The rules that are not negotiable

1. **Text contrast ≥ 4.5:1** (AA). Large text — ≥18pt/24px, or ≥14pt/18.5px
   bold — and meaningful images may go to 3:1.
2. **Body copy on White is always Stone Gray**, even though other colours pass.
3. **No tints or shades of any brand colour except Stone Gray.**
4. **Lead with the Primary colours.** Secondary and accent colours have an
   intentional, limited role. Avoid the "full rainbow" in one application.
5. **Logo:** Mediterranean Blue on light grounds, White on dark grounds. Never
   recolour, rotate, scale disproportionately, outline, or fill it with a
   pattern; never use the wave-and-dots alone as a design element; never place
   it on a ground that fails 4.5:1.

### The trap worth knowing about (1): bright colours on light grounds

The bright secondary and accent colours **fail as text on light grounds at any
size**. Measured against Pumice `#EEEEEE`:

| Colour | Contrast on Pumice | Small text (4.5:1) | Large text (3:1) |
|---|---|---|---|
| Algae `#00C389` | 1.97:1 | fails | fails |
| Coral `#F58674` | 2.12:1 | fails | fails |
| Seafoam `#3CCBDA` | 1.68:1 | fails | fails |

On light grounds these are **fill and graphic colours only** — a chart series,
an icon shape, a filled chip carrying dark type. Never a text colour.

This is the most common way a Seattle Aquarium GUI ends up inaccessible: the
colours look correct in the palette and only fail in place. Rule 3 blocks the
obvious workaround of darkening them as a tint, so the light theme needs
separately chosen semantic colours — see below.

The guidelines' own accessibility grid (p.20) shows the same thing: on the
White and Pumice swatches, far fewer type colours are marked usable than on the
dark grounds.

### The trap worth knowing about (2): one colour, two states

Some widgets expose a **single** `text_color` for states with different fills —
CustomTkinter's segmented button, which backs its tab bar, is one. Reaching for
the accent as the selected fill then forces dark type to sit on it, and that
same dark type lands on the unselected tabs at **1.12:1**. The labels are
invisible, and nothing warns you.

The fix is not a second colour — there isn't one. Pick *fills* that a single
text colour reads on. Body text on surface-versus-ground works everywhere:

| | selected fill | unselected fill | contrast |
|---|---|---|---|
| Dark | `#1B3557` | `#0C2340` | 12.40:1 / 15.79:1 |
| Light | `#F7F7F7` | `#FFFFFF` | 6.75:1 / 7.23:1 |

Selection then reads as the tab merging with the panel below it, which is the
standard idiom anyway. **Before styling any multi-state control, check how many
text colours it actually accepts** — the answer changes which fills are legal.

## Gradients — the One Ocean system

A core part of the visual expression, and easy to get wrong.

| Family | Use for | Notes |
|---|---|---|
| **Deep** / **Medium** | backgrounds | pick the one that contrasts with the content on top |
| **Bright, 2-colour** | illustrations and **UI elements**, over darker backgrounds | best on smaller elements |
| **Bright, 3-colour** | larger illustrations or design elements, over darker backgrounds | needs room to breathe |
| **Alt** | *only* over a 2-colour bright gradient, or **a web button's hover state** | never with photographic content |

Specs: **-45° or 45°**, may run light-to-dark or dark-to-light. Two-colour stops
at 0 / 70 / 95; three-colour at 0 / 50 / 50 / 50 / 100.

**Never use the full One Ocean gradient in its entirety.**

For desktop GUI work, gradients belong on chrome — the banner and the rail —
not on controls. A gradient behind small type makes contrast unverifiable,
because the ground changes across the element; if you put type on one, check it
against **both** end colours (`brand.contrast`), not against the average.

### What the UTC actually does with them

*Decided 2026-09-05, after the four-chapter regrouping.*

**Exactly one gradient survives, and it earns its place.** Gradients were
trialled on the banner and on the rail and both were dropped — on the rail
because a gradient gives each of four rows a slightly different ground, which
reads as four states rather than one control; on the banner because it fought
the calm of everything below it. Banner and rail are flat surfaces that flip
with the theme.

- **The 3-colour bright rule under the banner** — Algae → Seafoam → Purple
  Star, six pixels tall. It is a single object carrying no type, which is
  exactly what p.19 sanctions a bright gradient for, and it is the one place
  the whole palette shows at once.
- **The open tool's underline on the section strip** is the 2-colour bright
  gradient, Algae → Seafoam. Same rule, smaller element.

**The four chapter buttons each carry their own brand colour.** Only Fathom is
unavailable — it is the dark-mode window ground, and a button in it is a hole.
Algae *is* available, contrary to first appearances: it is the dark mode's
action colour, but the light mode's is Mediterranean, so it was never reserved.
The set in use is **KELP** — Salish · Algae · Seafoam · Mediterranean — green
through blue, four cool ocean colours that read as one family rather than four
unrelated chips. Five alternatives live in `theme.CHAPTER_PALETTES` and
switching is one line.

**The banner's roadmap is keyed to those buttons.** Each of the four blurbs is
introduced by a numbered badge in its chapter's own colour, so the banner reads
as the key to the rail rather than as a sentence that happens to list four
things. It breaks after the second — the vehicle and its telemetry above, the
imagery below — and that break is fixed rather than wherever the width runs
out, because it means something. The badge also sidesteps a real constraint: Seafoam and Algae cannot be
used as *type* on a light ground (1.9:1 and 2.2:1 on Pumice), but they carry
dark type perfectly well as a *fill*.

**The logo spans the title and the roadmap together.** p.11 sets a minimum of
50px wide for digital and no maximum, asks for clear space equal to the height
of the "A" in AQUARIUM — about a sixth of the mark, which the banner's padding
covers — and requires 4.5:1 against its ground. White on the dark surface is
13:1; Mediterranean on Pumice is 5.5:1. "Seattle Aquarium" was dropped from the
attribution line: the logo beside it already says so.

> **Type on a brand colour is chosen by measuring, not from a table.**
> `theme.ink_for(colour)` returns White or Fathom, whichever scores higher.
> Seafoam is the trap: it sits mid-luminance, so White fails on it at 1.95:1
> and it needs dark type — which is also why no bright gradient can carry a
> label. Swapping the palette therefore swaps the type with it, and
> `tests/test_gradients.py` checks every palette, not just the one in use.

Rendered by `utc/gui/gradients.py`, which implements the p.19 geometry rather
than a plain ramp: the second colour is reached at 95 and the 50/50 blend lands
at 70, so the first colour holds and then moves. Drawn on a `tkinter.Canvas` —
a CustomTkinter widget over an image paints its own rectangle, because
"transparent" there means the master's flat colour, not what is behind it.

### Style switches

*Added 2026-09-06, so alternatives can be compared rather than argued about.*
Each is a named constant in `theme.py` with the variants listed beside it, and
each was trialled against the running application rather than mocked up.

| Constant | Variants | Settled on |
|---|---|---|
| `CHAPTER_BTN_STYLE` | solid · outline · leftbar · ghost | **outline** |
| `CHAPTER_BTN_SHAPE` | soft · plate · pill · square | **plate** |
| `SECTION_MARK_STYLE` | underline · hairline · outline · pill · topline | **outline** |
| `BADGE_STYLE` | solid · soft · outline · dot · bar · plain | **soft** |
| `BANNER_LAYOUT` | inline · stacked | **stacked** |
| `TITLE_STYLE` | bold · black · caps · twotone · light | **bold** |
| `CHAPTER_PALETTES` | ocean · kelp · tideline · estuary · spectrum · shore | **kelp** |

A button's *treatment* and its *shape* are separate constants. Running them
together meant the combination that was actually wanted — the outline
treatment at the plate's corner radius — could not be expressed at all.

`tests/test_nav.py` walks every button, badge, banner and title variant, so a
new one cannot be added that draws illegible type or reserves the wrong width.

> **Render variants in separate processes.** Some of these are read when a
> widget is built, not when it draws — a tab's corner radius, which grid row
> its marker sits in — so switching them live shows a half-applied state. Two
> Tk roots in one process also cannot share images. The trial harness forks
> per variant for both reasons.

> **Scale canvas work by hand.** CustomTkinter scales its widgets and fonts for
> the display (2.5× on the field laptop); a raw Canvas does not, and Tk reports
> 96 DPI regardless. Use `theme.scale_of(widget)` and `theme.scale_font`. The
> first cut of the banner came out shorter than a single card heading.

## The two schemes

Our interpretation, verified against every surface each colour actually sits on.

### Dark — Fathom ground

| Role | Hex | Notes |
|---|---|---|
| `bg` | `#0C2340` | Fathom |
| `surface` | `#132C4C` | Fathom lifted for card separation |
| `surface_alt` | `#1B3557` | nested rows |
| `border` | `#2A4A73` | |
| `text` | `#FFFFFF` | |
| `text_muted` | `#A8BBD4` | |
| `heading` | `#3CCBDA` | Seafoam |
| `accent` | `#00C389` | Algae |
| `accent_hover` | `#00A876` | |
| `accent_text` | `#0C2340` | dark type **on** the bright fill |
| `ok` / `warn` / `error` | `#00C389` / `#F58674` / `#F58674` | Algae / Coral |
| logo | white | |

### Light — White ground

| Role | Hex | Notes |
|---|---|---|
| `bg` | `#FFFFFF` | |
| `surface` | `#EEEEEE` | Pumice |
| `surface_alt` | `#F7F7F7` | |
| `border` | `#CDCDCD` | Stone 20 |
| `text` | `#575757` | Stone — required on white |
| `text_muted` | `#6A6A6A` | Stone 60 |
| `heading` | `#004346` | Salish |
| `accent` | `#1963B0` | Mediterranean |
| `accent_hover` | `#154F8C` | |
| `accent_text` | `#FFFFFF` | |
| `ok` | `#00795A` | **not** Algae — see the trap above |
| `warn` / `error` | `#B4472F` | **not** Coral |
| logo | mediterranean | |

Elevation runs in opposite directions by design: dark lifts surfaces *toward*
the light, light drops them *away* from white. Inverting one theme to produce
the other gives muddy results.

`ok` and `warn` on light are the one place we knowingly step outside the
palette. The alternative — Algae and Coral at 2:1 — is unreadable, and tinting
them is forbidden by rule 3. Flag it in review rather than quietly reverting it.

### Measured contrast

| Pair | Dark | Light |
|---|---|---|
| body text on `bg` | 15.79:1 | 7.23:1 |
| body text on `surface` | 14.07:1 | 6.23:1 |
| muted text on `surface` | 7.19:1 | **4.66:1** |
| heading on `bg` | 8.08:1 | 11.10:1 |
| text on accent fill | 6.89:1 | 6.08:1 |
| `ok` on `surface` | 6.14:1 | **4.66:1** |
| `warn` on `surface` | 5.73:1 | **4.66:1** |

The three light-theme values at 4.66:1 clear 4.5:1 with little to spare. **If
you darken `surface` on the light theme, re-check those three** — they fail
first. Verify with the script in Appendix A; do not eyeball it.

## Typography

Two faces, both free Google Fonts:

- **Montserrat** — primary. Weights in use: ExtraBold, Bold, SemiBold, Medium,
  Regular.
- **Barlow Condensed Bold** — supporting, and **only** this weight.

### The official hierarchy

| Level | Face / weight | Tracking | Leading | Case |
|---|---|---|---|---|
| Display | Montserrat ExtraBold | +50 | 1.0× | **ALL CAPS only** |
| Title | Montserrat Bold | +50 caps / +25 sentence | 1.1× | sentence or caps |
| Header 1 | Montserrat Medium | +20 | 1.1× | **always sentence** |
| Header 2 | Montserrat SemiBold | +25 | 1.1× | **always sentence** |
| Eyebrow / Subhead | **Barlow Condensed Bold** | +75 | 1.1× | **ALL CAPS only** |
| Body copy | Montserrat Regular | +10 | 1.3× | sentence; Stone on White |

Italic and SemiBold are acceptable for emphasis in body copy.

### Mapping that onto a desktop GUI

A GUI is denser than print, and Tk exposes no letter-spacing control — **the
tracking values above cannot be applied in Tkinter**. Honour weight, case, and
relative scale, and treat tracking as print-only. (If you build in a toolkit
that does support tracking — Qt, or anything web-based — apply it.)

A practical scale, as used in the reference implementation:

```
TITLE    Montserrat Bold      22    window / app title
H1       Montserrat Medium    16    card headings          (Header 1)
H2       Montserrat SemiBold  13    sub-headings, buttons  (Header 2)
BODY     Montserrat Regular   12    labels, inputs
SMALL    Montserrat Regular   11    help text, status
EYEBROW  Barlow Cond. Bold    11    ALL-CAPS section labels
MONO     Consolas             11    log panes only
```

`RADIUS = 8`, `PAD = 12`.

Two deliberate exceptions:

- **Consolas for logs.** Column alignment in a scrolling diagnostic pane matters
  more than brand adherence.
- **Bold rather than Medium for the window title**, because at 22px on a dark
  ground Medium reads thin.

> The reference implementation does not yet use Barlow Condensed Bold, because
> Barlow is not installed on our machines. Vendor it (below) if you want
> eyebrows; otherwise omit them rather than substituting another face.

### Getting the weights to actually apply

Three traps, all of which fail *silently* — the app runs and simply looks wrong.

**Weight travels in the family name, not a style flag.** Windows registers most
Montserrat weights as their own families, and Tk only knows `normal` and `bold`.
So Medium is `("Montserrat Medium", 16)`, not `("Montserrat", 16, "medium")`.
Do not add a `"bold"` style alongside a SemiBold family either — Tk already
reports Montserrat SemiBold as bold, and asking for both synthesises a
double-bold.

**A bundled TTF is invisible until it is registered.** Dropping fonts into
`assets/fonts/` is enough for Pillow, which loads them by path, but **not** for
a GUI toolkit: Windows only offers families it has registered. Register them at
startup, before any font object is created:

```python
FR_PRIVATE = 0x10
ctypes.WinDLL("gdi32").AddFontResourceExW(
    ctypes.c_wchar_p(str(ttf_path)), FR_PRIVATE, 0
)
```

`FR_PRIVATE` scopes the font to your process — no admin rights, nothing
installed permanently.

**Availability checks must match what the toolkit can see.** If "is this font
available?" counts a bundled file that was never registered, you will name a
family the toolkit cannot resolve, and it falls back to its own default rather
than to your chosen fallback — a worse result than not trying. Check the
registered set and the OS font directories, not merely "a file exists".

**Montserrat is proportional.** Where digits must line up in a column — a
telemetry readout, a table of numbers — measure the string and right-align, or
use a tabular-figures setting. Do not assume fixed advance widths.

---

# Part 2 — Architecture

Stack: **Python + CustomTkinter**, packaged with **PyInstaller**. Chosen because
it ships as a double-clickable `.exe` to colleagues who have no Python, and
because the team already reads Python.

## Module layout

```
yourapp/
    brand.py            palette, Theme dataclass, font + logo discovery
    config.py           dataclass defaults; anything a user might change
    gui/
        theme.py        (light, dark) colour pairs, type scale
        widgets.py      Card, entry, label, button, domain rows
        app.py          window assembly + worker orchestration
    <domain modules>    the actual work — no GUI imports
    pipeline.py         orchestration; returns a result, never raises
    launch.py           PyInstaller entry shim
    tests/
```

**The domain modules must not import the GUI.** The pipeline is driven by a CLI
in tests and by the GUI in the field; if it reaches for a widget, neither works.
Progress and cancellation cross that boundary as a callback and an `Event`,
nothing more.

## Colour pairs, not conditionals

CustomTkinter accepts `(light, dark)` tuples for any colour and swaps them when
the appearance mode changes. Define every colour as a pair once and theme
switching costs nothing at runtime — no re-styling pass, no widget rebuild, no
branching at each call site.

```python
def pair(attr: str) -> tuple[str, str]:
    return (getattr(LIGHT, attr), getattr(DARK, attr))

SURFACE = pair("surface")
TEXT    = pair("text")
ACCENT  = pair("accent")
```

If you are writing `if self.mode == "dark"` to pick a colour, the pair is
missing.

## Never block the UI thread

Any job over about a second goes on a worker thread. **Tk is not thread-safe** —
a worker that touches a widget produces crashes that are intermittent,
unreproducible, and hard to trace back.

```python
# --- setup -----------------------------------------------------------
self._queue  = queue.Queue()
self._cancel = threading.Event()
self._worker = None
self.after(80, self._drain)

# --- starting --------------------------------------------------------
def work():
    try:
        res = run_pipeline(
            req,
            progress=lambda f, m: self._queue.put(("progress", f, m)),
            cancel=self._cancel,
        )
        self._queue.put(("done", res))
    except Exception:
        self._queue.put(("crash", traceback.format_exc()))

self._worker = threading.Thread(target=work, daemon=True)
self._worker.start()

# --- draining, on the Tk thread --------------------------------------
def _drain(self):
    try:
        while True:
            kind, *rest = self._queue.get_nowait()
            if kind == "progress":
                frac, msg = rest
                self.progress.set(frac)
                self.status.configure(text=msg)
            elif kind == "done":
                self._finish(rest[0])
            elif kind == "crash":
                self._log("Unexpected error:\n" + rest[0])
                self._reset_buttons()
    except queue.Empty:
        pass
    self.after(80, self._drain)
```

Points that matter:

- **The worker only ever puts tuples on a queue.** No widget access.
- **`_drain` runs on the Tk thread** and is the only place widgets are touched.
- **Catch `Exception` in the worker and forward the traceback.** An uncaught
  exception on a worker thread vanishes silently and the UI simply stops.
- **`daemon=True`**, so a forgotten worker cannot keep the process alive.
- **Cancellation is a `threading.Event`** the worker polls. Never kill threads.
- **Confirm on close while a job runs**, and set the cancel event if confirmed.

## Progress that never goes backwards

A progress bar that jumps back to zero reads as a hang, and users kill the job.
Give each stage a share of the range up front and map its local 0→1 into that
slice:

```python
st.plan(discover=1, extract=22, rov=18, sync=14, csv=5, render=40)
```

Clamp the overall fraction so it is monotonic. In the reference implementation
two sequential sub-steps sharing one stage caused exactly this: the bar reset
mid-run and the tool looked hung while working perfectly.

## Long-running jobs

Two things bite on multi-hour work, both learned the hard way.

**The machine sleeps.** Windows does not count a working process as user
activity, so a laptop left alone idle-sleeps mid-job and everything pauses until
it wakes. On one measured run this cost 55 minutes and was indistinguishable
from a hang. Hold it off for the duration:

```python
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
# ... work ...
ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
```

`SetThreadExecutionState` is **per-thread**, so it must be entered on the thread
doing the work — setting it once at startup does not cover your worker. Leave
the display alone; there is no reason to burn the screen for a batch job.
Closing the lid still sleeps the machine and no program can veto that, so put
that caveat in your user docs.

**Output files are locked.** We write into Dropbox folders, so something else
may hold the file you are about to replace: Dropbox uploading the previous
version, antivirus, Explorer building a thumbnail, or Excel with the last run's
CSV still open. On Windows that is `WinError 32`, and landing on the final move
destroys a completed job at the last step.

Write to scratch, then publish in one move, with a retry:

- Wait for the lock with backoff, and **say what is happening** — "…is open in
  another program (Dropbox, Excel or antivirus?)" — so it does not look frozen.
- If it never clears, write `name (1).ext` alongside rather than discarding
  finished work, and tell the user to close the other program.
- Never leave a partial file at the destination path.

## Bulk intermediates go outside synced folders

Cache to `%LOCALAPPDATA%\<app>_cache\`, never into the Dropbox project folder.
Writing gigabytes of disposable working files into a shared folder pushes them
to the whole team.

## Report, don't assume

When a tool discovers its own inputs, **show what it found and let the user
confirm** before starting expensive work. Guessing wrong is only discovered an
hour into a job. The reference implementation lists every file it located, with
sizes and a note of anything ambiguous.

Related: detect **cloud placeholder files**. A Dropbox online-only file looks
normal — right name, full size — but every read streams from the network, which
turns minutes into hours and looks exactly like a hang. Check the Windows
attributes and refuse to start:

```python
_FILE_ATTRIBUTE_OFFLINE               = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN        = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
```

## Packaging

```
pyinstaller yourapp.spec        # -> dist/YourApp.exe
```

Two things that will otherwise cost you an afternoon:

- **Point the spec at a top-level `launch.py`, not at your GUI module.**
  PyInstaller runs its target as `__main__`, which breaks relative imports
  inside a package (`attempted relative import with no known parent package`).
  A shim that imports the package keeps the package context intact.
- **A windowed build discards stdout and stderr**, so a startup crash leaves no
  trace at all. Keep a console variant behind an environment flag.

Git-ignore `build/` and `dist/`. The executable is ~87 MB — a build artefact,
and heading toward GitHub's 100 MB hard limit.

---

# Part 3 — Layout and components

## Window structure

Header / body / footer, with the body carrying the weight:

- **Header** — app name, Aquarium logo, theme toggle (right).
- **Body** — numbered cards in the order the job is actually done.
- **Footer** — primary action, cancel, progress bar, status line, log pane.

**Order the interface by the order of the work.** The reference app reads: pick
folder → confirm what was found → describe the work → choose outputs → run. A
first-time user can follow it top to bottom without instruction.

## Navigation: a left rail, not tabs

Once an app has more than about three screens, put the page list in a
**vertical rail down the left**, not a strip of tabs across the top. Pages then
grow downward, so a fifth or sixth costs no horizontal room and no label gets
truncated. There is room for a one-line subtitle under each name, which is
often the difference between a person guessing and knowing.

CustomTkinter's `CTkTabview` only does horizontal, so the rail is a fixed-width
column of buttons beside a content area that raises one page at a time — about
120 lines. Copy `UTC/utc/gui/nav.py`.

Three things about it are load-bearing, and all three were bugs first.

**Selection is shown by fill and a stripe, never by type colour.** A segmented
button — and this rail — offers a *single* text colour for both states, so an
accent fill would force dark type, which then sits at about **1.1:1** on the
unselected rows. Invisible. Body text on surface-versus-ground clears 6.7:1 in
both themes; mark the selection with an accent stripe instead.

**Park the slack in one spacer row.** Give a row below the last entry
`weight=1`. Without it the rail shares its leftover height out among the rows,
which pulls each label away from its own subtitle.

**A `CTkFrame` defaults to 200px in both directions.** With `grid_propagate(False)`
it *keeps* that. A 4px-wide accent stripe built as a frame silently made every
rail row 200px tall — 351px once display scaling was applied — and pushed the
last page off the bottom of the window. Pass an explicit `height=1` and let the
row set the height.

> That last one is why layout is worth checking **programmatically** rather than
> by eye: a screenshot showed something looked wrong, but `winfo_height()` gave
> the number, the row, and the cause in one line. See the testing note below.

## Cards

One `Card` per step: heading in `heading`, optional subtitle in `text_muted`,
body frame beneath. `surface` ground, 1px `border`, `RADIUS` corners, `PAD`
padding. Nested rows use `surface_alt` so structure reads without extra rules.

Card headings are Header 1 (sentence case). An ALL-CAPS Barlow Condensed Bold
eyebrow above a heading is the brand-correct way to label a group — use it where
a section genuinely needs a category label, not as decoration.

## Buttons

Three kinds, and no more:

| Kind | Use | Treatment |
|---|---|---|
| `primary` | the one action that starts work | `accent` fill, `accent_text` type |
| `ghost` | secondary actions | transparent, `border`, `text` |
| `danger` | remove / destroy | transparent, `border`, `warn` type |

One primary button per screen. If two things look equally like the main action,
neither reads as it.

## Inputs

Give fields more contrast than the surface behind them (`#FFFFFF` on light,
`#0A1E36` on dark). **Validate as the user types** and show the result inline,
beside the field:

- valid → the computed consequence in `ok` ("2.1 min")
- invalid → what is wrong, in `warn`
- empty → nothing at all

Inline feedback beats a dialog on submit — users fix errors while their
attention is still on the field.

## Spacing

Use the grid geometry manager with explicit `grid_columnconfigure(..., weight=1)`
on the stretching column. Consistent `PAD` between sections, tighter (3–4px)
between rows in a list.

---

# Part 4 — Words

Copy is part of the interface.

- **Say what happened and what to do.** Not "Error: invalid input" but
  "Start time is after end time — check the transect times."
- **Name things as the user names them.** "Flight folder", "transect", "site" —
  not "input directory" or "segment".
- **Buttons say what they do.** "Run", "Add transect", "Remove site".
- **No apologies, no blame.** State the situation and the fix.
- **Warnings are for things the user can act on.** Everything else goes in the log.
- **Sentence case** for headers and body, per the type hierarchy. Reserve
  ALL CAPS for Barlow Condensed Bold eyebrows and Display.

---

# Part 5 — Review checklist

**Brand**
- [ ] Every colour traces to the palette, or is a Stone tint
- [ ] No tints/shades of non-Stone brand colours
- [ ] Body copy on white is Stone
- [ ] Algae / Seafoam / Coral used as text **only** on dark grounds
- [ ] Primary colours lead; no "full rainbow"
- [ ] Correct logo variant per theme; logo not altered or on a failing ground
- [ ] Montserrat, with a detected fallback; Barlow Condensed Bold for eyebrows
- [ ] Weights requested by family name, not a style flag
- [ ] Bundled fonts registered at startup, and verified to resolve rather than
      silently fall back
- [ ] Headers sentence case; ALL CAPS only for eyebrow / display

**Accessibility**
- [ ] Contrast script run; every pair passes on the surface it sits on
- [ ] Both themes checked — not just the one you developed in
- [ ] Keyboard focus is visible
- [ ] Colour is never the only signal (pair it with text or an icon)
- [ ] Multi-state controls checked for how many text colours they accept,
      and both states measured — not just the selected one

**Architecture**
- [ ] Work runs off the Tk thread
- [ ] No widget touched from a worker
- [ ] Worker exceptions forwarded, not swallowed
- [ ] Cancellation works, and closing mid-run prompts
- [ ] Progress is monotonic
- [ ] Sleep held off for long jobs, on the worker thread
- [ ] Output files published atomically, locks tolerated
- [ ] Caches outside synced folders
- [ ] Domain modules do not import the GUI

**Packaging**
- [ ] Spec targets a `launch.py` shim
- [ ] `build/` and `dist/` git-ignored
- [ ] Debug console build available

---

## Checking the layout without screenshots

Verify layout by asking the widgets, not by photographing the screen:

```python
page.winfo_width(), page.winfo_height()      # zero-sized? wider than the window?
holder.winfo_y() + holder.winfo_height()     # does the last row fit in the rail?
```

Two reasons this beats a screenshot. It gives a *number and a cause* rather than
an impression — the 200px frame default above was found this way after a
screenshot only hinted at it. And `ImageGrab` captures a screen *region*: if the
app is not frontmost at the instant of the grab it silently photographs whatever
is, which on a real desktop can be a colleague's private browser tab. That has
happened; do not keep screenshots in an automated suite.

Realise the window first (`geometry(...)`, then `update()` a few times) and bail
out if it never comes up, or every check fails for the wrong reason.

---

# Appendix A — Contrast checker

Run this against your theme before shipping. It is the check the eye cannot do.

```python
"""Verify every colour pair in both themes against WCAG AA."""
from yourapp import brand


def _lum(hex_colour: str) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = brand.hex_to_rgb(hex_colour)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check() -> bool:
    ok = True
    for label, th in (("DARK", brand.DARK), ("LIGHT", brand.LIGHT)):
        print(f"--- {label} ---")
        pairs = [
            ("body on bg",       th.text,        th.bg,      4.5),
            ("body on surface",  th.text,        th.surface, 4.5),
            ("muted on surface", th.text_muted,  th.surface, 4.5),
            ("heading on bg",    th.heading,     th.bg,      3.0),
            ("text on accent",   th.accent_text, th.accent,  4.5),
            ("ok on surface",    th.ok,          th.surface, 4.5),
            ("warn on surface",  th.warn,        th.surface, 4.5),
        ]
        for name, fg, bg, need in pairs:
            r = contrast(fg, bg)
            good = r >= need
            ok &= good
            print(f"  {name:<18} {r:5.2f}:1  need {need}  "
                  f"{'PASS' if good else '** FAIL **'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if check() else 1)
```

# Appendix B — Starting points

Copy these from the reference implementation and adapt:

| File | What it gives you |
|---|---|
| `UTC/utc/brand.py` | palette, `Theme`, font + logo discovery — copy as-is |
| `UTC/utc/gui/theme.py` | `(light, dark)` pairs, type scale — copy as-is |
| `UTC/utc/gui/widgets.py` | `Card`, `entry`, `label`, `button`, `TimeEntry` — copy, then add domain widgets |
| `UTC/utc/gui/nav.py` | the left rail — copy as-is |
| `UTC/utc/gui/app.py` | window assembly, worker/queue orchestration — read, then adapt |
| `UTC/utc/fsutil.py` | lock-tolerant publishing |
| `UTC/utc/power.py` | keeping the machine awake |
| `UTC/utc/layout.py` | one module owning the folder structure, with a `PROTECTED` set for folders nothing may write to |

`brand.py` and `theme.py` carry no ROV-specific content and should be copied
unchanged, so a fix to a colour propagates rather than diverging per app.

**Fonts.** Montserrat and Barlow Condensed are free Google Fonts under the SIL
Open Font License, which permits bundling. Vendor the TTFs into `assets/fonts/`
(~240 KB per weight) and include the licence notice, so a packaged `.exe` renders
on-brand on a field laptop that has neither installed. **Vendoring alone is not
enough for the GUI** — register them at startup, as described under Typography.

---

*Maintained alongside `CCR_ROV_survey_methods/UTC/`. Brand values from
SAQ-001_Visual-ID-Guidelines_FINAL_V1-0823 (v1, Aug 2023). When the guidelines
are revised, `brand.py` is the file to update first.*
