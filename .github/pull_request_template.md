## What this changes

<!-- One or two sentences. What does it do differently now? -->

## Why

<!-- What problem prompted it? A field observation, a bug, a new need. -->

## How it was checked

<!-- Delete what does not apply. -->

- [ ] `pytest` passes
- [ ] `ruff check utc tests` passes
- [ ] Tried against a real flight folder — which one:
- [ ] Added or updated a test covering the change

## Does this touch survey data?

UTC moves, renames and deletes imagery, sometimes just before a card is wiped.
If this change writes, moves or deletes anything, say so here and describe what
happens when it goes wrong.

- [ ] No — read-only, or GUI/docs only
- [ ] Yes — and the worst case is:
