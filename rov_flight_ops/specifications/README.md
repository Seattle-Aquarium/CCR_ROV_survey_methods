# Specifications

The written design behind parts of ROV Flight Operations, for the people
who will maintain the code and the people who will analyse what it records.
The code's own docstrings explain *how*; these explain *what is promised*,
in a form that can be checked against the code and the files it writes.

## DVL capture (tab 3, `rov_flight_ops/dvl/`)

| Document | What it answers |
|---|---|
| [dvl_data_sources.md](dvl_data_sources.md) | Everything the Water Linked DVL A50 will give a topside laptop, where each piece comes from, what is captured, and what the DVL does **not** expose at all |
| [dvl_log_schema.md](dvl_log_schema.md) | Every file a capture writes, every column in it, its type, unit and meaning, and how the files join |
| [dvl_invariants_and_contracts.md](dvl_invariants_and_contracts.md) | The guarantees the capture makes (every byte, read-only, never replaces a file, …), the module contracts, the threading model, and the tests that hold each one |
| [dvl_drop_diagnosis.md](dvl_drop_diagnosis.md) | How to use a capture to find where DVL messages are being lost between the DVL and the mcap, with worked recipes |
| [dvl_artifacts_and_provenance.md](dvl_artifacts_and_provenance.md) | What was built, what it was built from (documents, versions, commits, probes of the demo DVL), and what has not yet been checked against a real vehicle |
| [dvl_bench_checklist.md](dvl_bench_checklist.md) | The checks to run once on Nereo or Lutris before trusting a capture |

## The UGPS capture (`rov_flight_ops/ugps/`, the Navigation tab's receiver row)

| Document | What it answers |
|---|---|
| [ugps_log_schema.md](ugps_log_schema.md) | What is read from the G2 topside and how often, every file and column the capture writes, how the receiver row judges the four receivers, and the extension's own logs on the Pi |

## Live monitoring (tab 1, `rov_flight_ops/bands.py`)

| Document | What it answers |
|---|---|
| [monitoring_bands.md](monitoring_bands.md) | What normal, unusual and bad are for every Live monitoring reading, how each is judged, where the numbers came from, and how often real flights were normal |

## The ROV flight simulator (`rov_flight_ops/sim/`, the banner's switch)

| Document | What it answers |
|---|---|
| [simulator.md](simulator.md) | What the simulator is, what each tab shows in it, every fault it rotates through and how each is calibrated, and the guarantees that keep it away from a real vehicle and a real flight folder |

## Keeping these true

A change to the capture's files or behaviour changes these documents in the
same commit. In particular:

* a column added, removed or renamed → `dvl_log_schema.md`, and the
  capture's `CAPTURE_SCHEMA` (`ccr.dvl_capture/1`) if an analysis written
  against the old layout would now read the wrong thing;
* a new way of talking to the DVL → `dvl_data_sources.md` and the read-only
  invariants in `dvl_invariants_and_contracts.md`, and the test that enforces
  them (`tests/test_dvl_readonly.py`);
* anything confirmed or contradicted on a vehicle → the "not yet verified"
  list in `dvl_artifacts_and_provenance.md` and the bench checklist.
