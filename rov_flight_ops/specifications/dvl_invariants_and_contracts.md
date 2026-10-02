# DVL capture: invariants and contracts

What the capture promises, and the test that holds each promise. A change
that breaks one of these breaks a test; a promise with no test is listed as
such.

Tests are in `rov_flight_ops/tests/`: `test_dvl_protocol.py`,
`test_dvl_cadence.py`, `test_dvl_websocket.py`, `test_dvl_capture.py`,
`test_dvl_readonly.py`, `test_dvl_page.py`, plus the program-wide
`test_pifiles.py::test_only_the_named_modules_ever_send_anything_but_a_get`.

## Invariants

### The record

**INV-1  Every byte.** `_tcp.jsonl` holds exactly the bytes received from
the DVL's TCP JSON port, in order, across reconnections. Nothing is added,
removed, re-encoded or re-ordered. The only bytes the DVL sent that are not
in it are those still in flight when the capture stopped.
*Held by* `test_every_byte_comes_back_whatever_the_reads_were` (1-byte to
100 kB reads), `test_the_raw_file_is_every_byte_the_dvl_sent`,
`test_lines_split_across_writes_are_kept_whole`,
`test_every_byte_survives_a_reconnection`.

**INV-2  Bytes before meaning.** Each receive is written to the raw file
before it is split into lines or parsed. A line that fails to parse, or a
report that fails to tabulate, costs its CSV row and is noted; it never
costs the raw bytes. *Held by* the order of operations in
`DvlCapture._tcp_connection`, `test_parsing_never_raises`, and the
`try` around tabulation in `_tcp_line` (no dedicated test of a tabulation
failure).

**INV-3  The index tiles the raw file.** Index rows, in `line_no` order,
cover the raw file from byte 0 to its end with no gap or overlap; each row's
`kind` is what its bytes parse to. A fragment cut off by a disconnection is
a row of kind `partial`. *Held by*
`test_the_index_finds_every_line_in_the_raw_file`,
`test_every_byte_survives_a_reconnection`.

**INV-4  One row per report.** Every velocity line in the index has exactly
one row in `_velocity.csv` with the same `line_no`; likewise dead reckoning.
Invalid reports are rows, not omissions. *Held by*
`test_every_report_has_exactly_one_row`,
`test_an_invalid_stretch_is_counted_and_is_not_a_gap`.

**INV-5  Nothing in a report is dropped.** A field the schema does not name,
a transducer that cannot be placed, an extra field on a transducer: each
goes into `extra_json`, whole. *Held by*
`test_nothing_unexpected_is_dropped`.

**INV-6  Unknown is blank, never zero.** A field the DVL did not send is an
empty cell. *Held by* `test_a_missing_field_is_blank_not_zero`.

**INV-7  Full precision.** Every number reads back as the identical float64
or integer; non-finite values are kept as `nan`/`inf`. *Held by*
`test_cells_round_trip_through_csv_at_full_precision`.

**INV-8  Web reads are kept verbatim.** Every HTTP body, WebSocket message
and mavlink2rest body the capture interprets is also written as received
(text when UTF-8, base64 otherwise). *Held by*
`test_the_web_stream_status_and_snapshots_are_captured`.

### The DVL is only asked

**INV-9  Four questions, nothing else.** The only commands that can be
encoded are `get_config`, `get_version_info`, `get_time_status`,
`get_time_ntp`; `protocol.command_bytes` refuses everything else, and is the
only code in the package that builds a command. The names of the commands
that change the DVL appear only in `protocol.NEVER_SENT`. *Held by*
`test_the_read_only_commands_encode_as_one_document_each`,
`test_everything_else_is_refused`,
`test_no_command_is_built_anywhere_but_command_bytes`,
`test_the_commands_that_change_the_dvl_are_named_only_in_their_list`,
`test_only_read_only_commands_are_sent_one_at_a_time`.

**INV-10  One command at a time.** A command is written alone and the next
is not sent until it is answered or 3 s have passed — the DVL treats one
read as one document. *Held by*
`test_only_read_only_commands_are_sent_one_at_a_time`.

**INV-11  GETs only.** Every web request to the DVL, the extension and
mavlink2rest is a GET. *Held by* `test_every_web_request_is_a_get`, the
simulator's record of methods in
`test_the_web_stream_status_and_snapshots_are_captured`, and the
program-wide verb scan in `test_pifiles.py`.

**INV-12  The diagnostic log needs a confirmed disarm.** `/api/collect`
appears only in `dvl/diagnostic.py`; `diagnostic.collect` sends nothing
unless `armed_state()` returns exactly (disarmed, current), a description is
given and the duration is one of Water Linked's. *Held by*
`test_the_diagnostic_request_lives_in_one_module`,
`test_a_diagnostic_log_is_refused_unless_confirmed_disarmed`,
`test_a_diagnostic_log_needs_a_description_and_a_real_duration`.

### Files

**INV-13  Nothing is replaced.** Every capture file is created exclusively
(`x` mode); an existing file of the same name is left alone and the failure
reported. Capture ids are unique per folder. *Held by*
`test_nothing_is_ever_replaced`,
`test_a_second_capture_in_the_same_second_gets_its_own_files`.

**INV-14  No flight folder, no files.** A live view writes nothing anywhere.
*Held by* `test_a_live_view_writes_nothing`.

**INV-15  The record is whole or absent.** The capture's `.json` is written
to a temporary file and moved into place, so a reader never sees half of
one. `state` is `closed` only after a clean stop. *Held by*
`test_the_record_says_what_was_captured` (state and contents; the atomic
replace is by construction).

**INV-16  Long paths open.** Paths of 240 characters or more are opened with
Windows' extended-length prefix. *Held by*
`test_long_paths_get_the_extended_prefix_on_windows`, and was found by a
real failure at a 270-character path.

**INV-17  The disk is not filled.** Below 2 GiB free on the flight folder's
drive, acoustic snapshots pause; below 256 MiB, nothing more is written and
the tab and every tab's status line say so in red. Rows not written for want
of disk are counted in the record. *Held by*
`test_a_full_disk_stops_writing_and_says_so`.

### Failure

**INV-18  Failures are shown, counted and cleared — never raised.** A write
that fails sets the capture's problem (shown on the DVL tab and in the
status line of every tab) and counts the loss; the next good write clears it
and leaves a note of how many were lost. An exception in a capture thread is
logged to the diagnostics log and shown, and stops that thread only.
*Held by* `test_a_failed_write_is_shown_counted_and_cleared`.

**INV-19  It cannot stop a flight being recorded.** The capture shares no
thread, file or lock with the flight recorder. It reads the recorder's
current flight id and arm state, and nothing else. *By construction*; the
flight recorder's own tests run unchanged.

**INV-20  It never holds up the window.** Starting, moving and stopping a
capture run on the DVL lifecycle thread; the tab reads copies of the live
state and draws only while on screen. Closing the program waits at most
12 s for the capture, on the window's own loop. *Held by*
`test_choosing_another_folder_closes_one_capture_and_opens_the_next` (the
lifecycle), the page tests (drawing from copies); the timing bound is by
construction.

### Interpretation

**INV-21  Gaps are judged on the DVL's clocks, with thresholds recorded.**
A gap is an interval on the DVL's `time_of_validity` longer than
max(2.5 × median, median + 150 ms) of the last 51 ordinary intervals, or an
interval on this laptop's clock more than max(250 ms, 2 × median) longer
than the DVL's. Classification:

| Kind | Condition |
|---|---|
| `missing_reports` | DVL interval is a gap and `d_tov − time` > max(50 ms, median / 2) |
| `dvl_quiet` | DVL interval is a gap otherwise |
| `delivery_stall` | DVL interval normal, laptop interval long |
| `clock_step` | DVL interval ≤ 0 |

Gaps do not enter the median. Nothing is judged before 5 intervals. The
thresholds are written into every capture record. *Held by*
`test_dvl_cadence.py`, and end to end by
`test_each_kind_of_gap_is_put_on_its_own_side`.

**INV-22  Invalid is not a gap.** Reports with `velocity_valid` false are
counted as invalid reports and stretches, not as gaps. *Held by*
`test_invalid_stretches_are_counted_apart_from_gaps`,
`test_an_invalid_stretch_is_counted_and_is_not_a_gap`.

## Contracts

### `dvl.protocol` — pure

* `LineSplitter.feed(data, chunk) -> list[Line]`: returns the lines
  completed by `data`; never alters a byte. `flush()` returns the
  unterminated remainder or None. `offset` is the stream offset of the next
  byte.
* `parse(body) -> (obj | None, error)`: never raises.
* `kind_of(obj) -> str`: never raises.
* `flatten_*(obj) -> dict`: keys exactly the matching `*_COLUMNS` (less the
  identity columns the capture adds); never raises on any JSON value.
* `command_bytes(name) -> bytes`: one JSON document and one `\n`; raises
  `CommandRefused` for anything outside `READ_ONLY_COMMANDS`.

### `dvl.cadence` — pure, one owner

* `Cadence.add(...) -> (gaps, note)`: deterministic for a given sequence;
  not thread-safe — the TCP thread owns its capture's `Cadence`.
* `derived_velocity`, `derived_position`: differences only; None when either
  operand is missing.

### `dvl.websocket.Client` — one thread

* `connect()` raises `OSError` or `WebSocketError`; on success the
  handshake's accept key has been checked.
* `receive() -> (opcode, payload)`: whole messages; answers pings; raises
  `Closed` on a close frame, `TimeoutError` on a receive timeout (the
  connection stays usable), `WebSocketError` on a protocol breach or a
  message over 4 MB.

### `dvl.webapi.Session` — one thread

* `get(path) -> Reply`: never raises; one keep-alive connection, one retry on
  a connection the server had closed; `Reply.record()` is lossless.

### `dvl.live.LiveState` — any thread

* Writers: `update`, `merge`, `count`, `add_*`. Readers: `snapshot()` returns
  a copy, safe to use without the lock.

### `dvl.capture.DvlCapture`

* `start()` once, then `stop(timeout)` once. `stop` returns True when every
  thread has ended within the timeout; a thread that has not is named in
  `stuck` and in the record, and closes its own files when it ends.
* `set_override`, `set_snapshot_hz`, `set_vehicle_host` may be called from
  any thread while running.
* Never raises from its threads; problems are in `problem`, `notes`, the
  events file and the record.

### `dvl.recorder.DvlRecorder`

* `use_folder`, `ensure_running`, `stop` queue work on the lifecycle thread
  and return an `Event` set when it is done; they never block the caller.
  Requests run one at a time, in order.
* `use_folder(f)` with a capture already running into `f` does nothing.
* `shutdown(timeout)` blocks; after it, no new capture starts.

### `gui.dvlpage.DvlPage` and `gui.app.App`

* Choosing a flight folder starts (or moves) the capture: `App._arm_dvl`,
  called at the end of `_arm_monitor`.
* `DvlPage.refresh` starts a live view only when the DVL tab is the one on
  screen. Choosing a folder refreshes every page; a page refreshed in
  passing must not start a capture of its own. (Found when a live view
  started by one test went on reading through another test's fakes.)
* Closing the window stops the capture on its own thread and waits for it on
  the window's loop, at most 12 s.

### `dvl.diagnostic.collect`

* Raises `Refused` before any network traffic unless disarmed-and-current,
  described, and a valid duration. Otherwise returns a `Result`, writing the
  file as `.part` and renaming it only when complete, and always writing the
  sidecar record.

## Threads and what each owns

One file, one thread: each file is opened, written and closed by the thread
listed. The events file is the one shared file, written under a lock.

| Thread | Reads | Owns |
|---|---|---|
| `dvl-tcp` | the DVL's TCP JSON port; sends the read-only commands | `_tcp.jsonl`, `_tcp_index.csv`, `_velocity.csv`, `_deadreckoning.csv`, `_commands.csv` |
| `dvl-ws` | the DVL's `/ws` | `_ws.jsonl`, `_ws_velocity.csv`, `_ws_motion.csv` |
| `dvl-status` | the DVL's status, configuration, clock, identity; disk space; rewrites the record every 60 s | `_http.jsonl`, `_status.csv` |
| `dvl-acoustic` | `/api/graph`, `/api/spectrum` at the snapshot rate | `_echo.jsonl`, `_spectrum.jsonl` |
| `dvl-vehicle` | the BlueOS extension's `/get_status`; mavlink2rest counters; sets the DVL's address from the extension | `_vehicle.jsonl`, `_mavlink.csv` |
| `dvl-lifecycle` | — | starts and stops captures |

The DVL and the vehicle are read on separate threads so that a Pi off the
tether — whose every read costs a timeout — never delays reading the DVL.

## Lifecycle

```
         flight folder chosen                     another folder chosen
 (none) ───────────────────────▶ capturing(A) ──────────────────────────▶ capturing(B)
   │                                  │                                     │
   │ DVL tab opened, no folder        │ program closing                     │
   ▼                                  ▼                                     ▼
 live view ── folder chosen ──▶ capturing   stopped (record state: closed)
```

Inside a capture the TCP and WebSocket readers each loop: connect → read
until silence (5 s TCP, 10 s WebSocket), error, close, an address change or
stop → reconnect with back-off up to 5 s (60 s after a refused WebSocket
handshake). A change of the DVL's reported TCP port, or of its address,
ends a back-off wait at once.
