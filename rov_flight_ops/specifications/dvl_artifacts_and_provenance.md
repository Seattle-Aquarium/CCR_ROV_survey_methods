# DVL capture: artifacts and provenance

What was built for the DVL tab, what it was built from, and what has and
has not been checked. Built on branch `zhr_DVL_logging`, 1 October 2026.

## What was built

### Code — `rov_flight_ops/rov_flight_ops/`

| File | Role |
|---|---|
| `dvl/__init__.py` | the package's map |
| `dvl/protocol.py` | the DVL's formats: byte-exact line framing, parsing, flattening, the read-only command list, beam geometry, range modes |
| `dvl/cadence.py` | report intervals on two clocks; gap classification; derived columns |
| `dvl/websocket.py` | a small RFC 6455 client (standard library only) |
| `dvl/webapi.py` | the DVL's address; a keep-alive GET client; lossless reply records; RFC 3339 to nanoseconds |
| `dvl/live.py` | the shared live state the tab draws from |
| `dvl/capture.py` | one capture: five reader threads, sixteen files, the record |
| `dvl/recorder.py` | the application's handle: start, move, stop, on a lifecycle thread |
| `dvl/diagnostic.py` | Water Linked's diagnostic log, gated on a confirmed disarm |
| `dvl/simulator.py` | a stand-in DVL for tests and the bench (`python -m rov_flight_ops.dvl.simulator`) |
| `gui/dvlpage.py` | tab 3, DVL |
| `gui/app.py` | the tab inserted at 3; a capture started with each flight folder; closed with the window; DVL problems on every tab's status line |
| `settings.py` | `dvl_host`, `dvl_snapshot_hz` |

### Tests — `rov_flight_ops/tests/`

| File | Holds |
|---|---|
| `test_dvl_protocol.py` | framing at every read size, flattening of Water Linked's own example, nothing dropped, blank-not-zero, full precision, beam layout |
| `test_dvl_cadence.py` | each gap class, invalid stretches, derived columns |
| `test_dvl_websocket.py` | RFC 6455's worked key, every length encoding, masking, fragments, pings, a real handshake |
| `test_dvl_capture.py` | the capture end to end against the simulator: every byte, the index, every table, commands, gaps, snapshots, the record, live view, no replacement, full disk, failed writes, the extension, moving folders |
| `test_dvl_readonly.py` | the read-only guarantees, by what the code can build |
| `test_dvl_page.py` | the tab's position, and that it draws |
| `test_transectpage.py` | updated: the tab order now has DVL at 3 |

### Documents — `rov_flight_ops/specifications/`

This file, [README.md](README.md), [dvl_data_sources.md](dvl_data_sources.md),
[dvl_log_schema.md](dvl_log_schema.md),
[dvl_invariants_and_contracts.md](dvl_invariants_and_contracts.md),
[dvl_drop_diagnosis.md](dvl_drop_diagnosis.md),
[dvl_bench_checklist.md](dvl_bench_checklist.md). The program's
[README](../README.md) has the tab's section and the new flight-folder layout.

### Runtime artifacts

Per capture, in `<flight folder>/logs/dvl/`: the sixteen files of
[dvl_log_schema.md](dvl_log_schema.md). Per diagnostic log: the file and its
`.json` record. Settings: `dvl_host` and `dvl_snapshot_hz` in
`%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\settings.json`.

## What it was built from

| Source | Version | Used for |
|---|---|---|
| Water Linked documentation, `github.com/waterlinked/docs` | HEAD `8798af4` (2026-10-01); `docs/dvl/dvl-json-protocol.md` at `8d2f75d` (2026-06-11) | the TCP JSON API `json_v3.3`; time API; range modes; periodic cycling; networking; PD formats; diagnostics; changelog; the A50 transducer-numbering image |
| The DVL web GUI's JavaScript | `main.e1f6a90c.chunk.js` served by the demo DVL, software 2.7.1 | the web API paths, the `/ws` channels, the acoustic views and their units, the diagnostic-log form (a GET, durations 15/30/60/300 s) |
| Water Linked's public demo DVL, `dvl.demo.waterlinked.com` | software 2.7.1, `json_v3.2`, variant "performance", probed 2026-10-01 | real reply shapes and sizes; CRLF line endings; one-document-per-read command parsing; WebSocket rates and extra velocity fields; PD4/PD6 ports closed to the internet; `/api/collect` answers 406 "not simulated" |
| `bluerobotics/BlueOS-Water-Linked-DVL` | tag `v1.0.10` (`6022f84`, as installed per NAVIGATION.md's 18 Sept inventory) and master `cf0224f` (2026-09-30) | how the extension reads the DVL and what it sends: invalid reports dropped, one line per loop, buffer discarded on reconnect, system 255/0, `usec = ts × 1000` |
| `keenanjohnson/BlueOS-Water-Linked-DVL` (Keenan Johnson's beam splitter) | master `7cce3d4` (2025-07-21); upstream PR #54 open | the idea of per-beam distances; its beam-orientation mapping, which agrees with the transducer drawing and contradicts its own `technical_details.md` |
| `mcap_to_csv/ccr_m2c/mcap_read.py`, `feeds.py` (this repository) | branch `zhr_DVL_logging` | mcap topic naming `mavlink/<sys>/<comp>/<MESSAGE>`; that `DISTANCE_SENSOR` is recorded under 255/0 |

Measurements quoted in the documents (demo rates: velocity 4.95 /s, dead
reckoning 9.9 /s, WebSocket channels 10 /s; echo profile 18,470 bytes;
spectrum 2,877 bytes) are from that one demo session.

## Verified, and how

* **Against the simulator** (all automated, `tests/test_dvl_*.py`): every
  invariant in [dvl_invariants_and_contracts.md](dvl_invariants_and_contracts.md)
  that names a test.
* **The full program running against the simulator**, driven by a script
  and screenshotted on this laptop (250 % display scaling): the tab draws,
  connects to all three streams, classifies injected faults, and writes all
  sixteen files — including at a 270-character path, which first failed
  and is what INV-16 came from. The screenshots also caught the canvases
  drawing at a third of their size before display scaling was applied.
* **Against the public demo DVL**, by hand: every endpoint's shape, the TCP
  stream's framing and command behaviour, the WebSocket channels.
* **A capture against the public demo DVL** (`https://dvl.demo.waterlinked.com`,
  12 s, 1 snapshot/s, 1 October 2026): clean start and stop, no problems;
  57 velocity and 113 dead-reckoning reports, 487 WebSocket messages, all
  fourteen DVL-side files written; `get_config` answered, and
  `get_version_info`, `get_time_ntp` and `get_time_status` answered
  `success: false, "Command not recognised"` — the 2.7.1 behaviour the
  capture is built to log rather than trip over; the demo's periodic cycling
  (on) and an invalid stretch both called out in the events file; the
  demo's clock measured 11.4 s ahead of this laptop's, ± 81 ms.

## Not yet verified — needs a vehicle

1. Our DVLs' software version, and therefore whether `get_version_info`
   and the time commands exist on them (the capture logs either answer).
2. That the topside laptop can reach the DVL's address through the tether.
   The team reaches its web page from the laptop's browser today, which
   suggests it can.
3. The DVL's address as the extension reports it (`hostname`), and that the
   capture finds it without one being typed.
4. **Whether mavlink2rest counts messages the extension sends.** The mcaps
   record them (255/0 topics); whether mavlink2rest's REST view does is not
   known. If it does not, section 2's arrows read "never sent by this
   vehicle" and the comparison has to be done afterwards against the mcap
   (Recipe A/B of the diagnosis guide).
5. The beam positions (cover one transducer — bench checklist).
6. Whether a topside client can back-pressure the DVL's TCP server when the
   tether drops (diagnosis guide, bench experiment 2).
7. The DVL's CPU at 5 snapshots a second (bench experiment 1).
8. The diagnostic log's format, and whether the DVL's output changes while
   it records one.
9. Real sizes: the per-file figures in the schema are estimates.

## Known limits

* The capture sees the DVL from the topside. A topside network stall
  delays what it receives, but does not lose it (TCP), and is classified as
  a `delivery_stall`; a long tether outage that outlasts TCP's patience
  ends the connection, and what the DVL sent in that time to this client is
  lost — that window is visible as a gap between connections.
* PD4/PD6 are not captured (they carry nothing the JSON stream does not).
* The Monitoring tab's charts are raw canvases and are not scaled for
  high-DPI displays; the DVL tab's are. Not changed here.
* The program's navigation session log fails at paths over 260 characters
  (seen during this work); the DVL capture does not. Not changed here.
