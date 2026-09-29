## What this changes

<!-- One or two sentences. What does it do differently now? -->

## Why

<!-- What problem prompted it? A field observation, a bug, a new need. -->

## How it was checked

<!-- Delete what does not apply. -->

- [ ] `pytest` passes (in each program touched)
- [ ] `ruff check rov_flight_ops tests` / `ruff check rov_imagery_processing tests` passes
- [ ] Shared files changed in *both* programs, if any were touched
- [ ] Tried against a real flight folder — which one:
- [ ] Added or updated a test covering the change

## Does this touch survey data or the vehicle?

These programs move, rename and delete imagery, sometimes just before a card is
wiped, and clear logs off the vehicle. If this change writes, moves or deletes
anything, or sends anything to the vehicle, say so here and describe what
happens when it goes wrong.

- [ ] No — read-only, or GUI/docs only
- [ ] Yes — and the worst case is:
