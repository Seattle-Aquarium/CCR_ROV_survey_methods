# Contributing

This repository holds the Seattle Aquarium's ROV survey tooling. The most
actively developed part is **`UTC/`** — Underwater Telemetry Compositing, a
desktop app that manages a flight's files and puts telemetry onto its imagery.

Most of us here are scientists rather than software engineers, so this is
deliberately short. The rules exist for one reason: **UTC moves, renames and
deletes survey imagery, sometimes as the last step before a card is
reformatted.** A bug can destroy data that took a boat, a team and a weather
window to collect. Everything below follows from that.

---

## Getting set up

```bash
cd UTC
python -m pip install -r requirements.txt
python -m pip install pytest ruff
```

Run the app:

```bash
python -m utc.gui.app
```

Run the checks — this is what CI runs, and it takes about ten seconds:

```bash
pytest
ruff check utc tests
```

---

## The test suite

`pytest` runs the **automated** tests: hermetic, no flight data, no display.
They build tiny synthetic JPEGs, mcaps and video clips in a temp folder.

Some files in `tests/` are **live scripts** instead — they need a real flight
folder on this machine, or a screen to open a window on. They are skipped by
default and opted into:

```bash
pytest --runlive        # needs real data on this machine
python tests/test_pipeline_live.py   # or run one directly
```

If you add a test that needs real data, name it `*_live.py` so it stays out of
CI.

### What a test is for here

Prefer tests that pin down a property someone could plausibly break, and say in
the test name or docstring *what breaks in the field* if it regresses. The ones
worth copying the style of:

- `test_import_copies_and_leaves_the_card_untouched` — the card is the only
  copy until the import finishes.
- `test_sort_pairs_gpr_and_jpg_onto_identical_stems` — the GPR↔JPG pairing is
  what the ecological analysis relies on.
- `test_band_is_above_the_image_as_displayed` — asserts on what a *viewer*
  sees, because EXIF rotation is invisible to a check that reads raw pixels.

A test that only restates the implementation is not worth the maintenance.

---

## Branching and pull requests

`main` should always be releasable. Work on a branch:

```bash
git switch -c yourname/short-description
```

Then open a pull request. CI runs the tests, the linter, and a full PyInstaller
build on Windows — a green run means the app still packages, not just that the
tests pass.

Keep a PR to one idea. A 400-line PR doing three things is hard to review and
harder to revert when one of the three turns out to be wrong.

---

## Things that are easy to get wrong here

These have all bitten us at least once. They are in the code as comments too,
but they are worth knowing before you start.

**Rotation is metadata.** GoPro records the camera's 180° mounting as EXIF
`Orientation` / a display matrix, not in the pixels. Pillow and ffmpeg both
ignore it unless asked. Anything that composites or crops must bake the
rotation in and then neutralise the tag, or the result is upside down in some
viewers and not others. Assert on what a viewer sees.

**Never write to a source.** Cards, `JPG_edited`, and the original GPR raws are
inputs. Bannering an edited frame writes a copy to `JPG_edited_banner`; it does
not touch the original, because those frames feed downstream ML and must stay
byte-for-byte as exported.

**Re-encoding costs quality, and it compounds.** A single JPEG stamp measures
~53 dB against the original; a stamp-then-strip round trip measures ~43 dB.
Where an operation can be a stream copy or a metadata edit, make it one.

**Dropbox holds file handles.** Publishing an output can hit `WinError 32`
because Dropbox is still uploading the previous version. Retry with backoff and
say what is happening; do not fail a finished multi-hour job on a transient
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

`ruff` enforces the parts that matter and is configured in `UTC/pyproject.toml`.
Line length is not enforced — the comments in this codebase explain *why*
something is the way it is, and that is worth the width.

Write comments that say why, not what. `# increment i` is noise; `# -ss before
-i seeks on keyframes and is what makes this fast` is the reason someone will
need in six months.
