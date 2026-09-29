# Contributing

This repository holds the Seattle Aquarium's ROV survey tooling. The actively
developed parts are two desktop applications:

- **`rov_flight_ops/`**: ROV Flight Operations. It records each flight, maps
  the vehicle, logs transect times, downloads and clears the vehicle's logs,
  and writes flight reports.
- **`rov_imagery_processing/`**: ROV Imagery Processing. It imports, develops,
  banners, trims and composites the survey imagery.

They share the transect extractor in `mcap_to_csv/`.

Most of us here are scientists rather than software engineers, so this is
deliberately short. The rules exist for one reason: **these programs move,
rename and delete survey data.** Imagery is imported just before a card is
reformatted, and old logs are cleared off the vehicle. A bug can destroy data
that took a boat, a team and a weather window to collect. Everything below
follows from that.

---

## Getting set up

Each program is self-contained, with its own environment and its own tests,
and neither imports the other. Work in the one you are changing:

```bash
cd rov_flight_ops            # or rov_imagery_processing
python -m pip install -e ".[dev]"
python -m pip install -e ../mcap_to_csv    # rov_flight_ops only
```

Run the app:

```bash
python -m rov_flight_ops     # or python -m rov_imagery_processing
```

For everyday use, the double-click launcher in each folder builds a private
environment instead.

Run the checks. This is what CI runs:

```bash
pytest -q
ruff check rov_flight_ops tests    # or rov_imagery_processing tests
```

Shared files are **duplicated, not imported**. The two programs each carry a
copy of `gui/shell.py`, `widgets.py`, `theme.py`, `gradients.py`, `brand.py`,
`mcap_extract.py` and `diagnostics.py`. A fix to shared logic has to be made
in both folders.

---

## The test suite

`pytest` runs the **automated** tests. They are hermetic: no flight data, and
a fake BlueOS vehicle where one is needed. They build tiny synthetic JPEGs,
mcaps and video clips in a temp folder.

Some tests are **live** instead. They need a real flight folder on this
machine, or a screen to open a window on. They are skipped by default and
opted into:

```bash
pytest --runlive
```

If you add a test that needs real data, mark it live so it stays out of CI.

### What a test is for here

Prefer tests that pin down a property someone could plausibly break. Say in
the test name or docstring *what breaks in the field* if it regresses. These
are the ones worth copying the style of:

- `test_import_copies_and_leaves_the_card_untouched`: the card is the only
  copy until the import finishes.
- `test_sort_pairs_gpr_and_jpg_onto_identical_stems`: the GPR↔JPG pairing is
  what the ecological analysis relies on.
- `test_band_is_above_the_image_as_displayed`: asserts on what a *viewer*
  sees, because EXIF rotation is invisible to a check that reads raw pixels.

A test that only restates the implementation is not worth the maintenance.

**Change a GUI? Take a screenshot.** A CustomTkinter page can report perfect
geometry and still draw nothing. Only grabbing the pixels catches that.

---

## Branching and pull requests

`main` should always be releasable. Work on a branch:

```bash
git switch -c yourname/short-description
```

Then open a pull request. CI lints and tests both programs on Windows, with
Python 3.11 and 3.13, whenever either program or the extractor changes.

Keep a PR to one idea. A 400-line PR doing three things is hard to review and
harder to revert when one of the three turns out to be wrong.

---

## Things that are easy to get wrong here

These have all bitten us at least once. They are in the code as comments too,
but they are worth knowing before you start.

**Only one module may change the vehicle.** In `rov_flight_ops`, every request
to BlueOS is a read, except in `pifiles.py` (download and clear) and the
Navigation page's guarded profile and origin writes. A test enforces this.
Keep it that way.

**Rotation is metadata.** GoPro records the camera's 180° mounting as EXIF
`Orientation` / a display matrix, not in the pixels. Pillow and ffmpeg both
ignore it unless asked. Anything that composites or crops must bake the
rotation in and then neutralise the tag. Otherwise the result is upside down in
some viewers and not others. Assert on what a viewer sees.

**Never write to a source.** Cards, `JPG_edited`, and the original GPR raws are
inputs. Bannering an edited frame writes a copy to `JPG_edited_banner` and
leaves the original alone. Those frames feed downstream ML and must stay
byte-for-byte as exported.

**Re-encoding costs quality, and it compounds.** A single JPEG stamp measures
~53 dB against the original; a stamp-then-strip round trip measures ~43 dB.
Where an operation can be a stream copy or a metadata edit, make it one.

**Dropbox holds file handles.** Publishing an output can hit `WinError 32`
because Dropbox is still uploading the previous version. Retry with backoff and
say what is happening. Do not fail a finished multi-hour job on a transient
lock.

**Cloud placeholders are not files.** A Dropbox online-only file has the right
name and size but streams from the network on every read. Check the Windows
attributes and refuse to start, rather than appearing to hang for hours.

**Judge coverage in units, not ratios.** Floating point leaves a fully covered
transect a hair under 1.0. A warning that fires at 100% teaches people to
ignore warnings.

**Proportional type clips silently.** Montserrat's digits are not even the same
width as each other. Size any fixed-width layout against the *widest* content
a field can produce, never against the values in front of you.

---

## Style

`ruff` enforces the parts that matter and is configured in each program's
`pyproject.toml`. Line length is not enforced. The comments in this codebase
explain *why* something is the way it is, and that is worth the width.

Write comments that say why, not what. `# increment i` is noise; `# -ss before
-i seeks on keyframes and is what makes this fast` is the reason someone will
need in six months.
