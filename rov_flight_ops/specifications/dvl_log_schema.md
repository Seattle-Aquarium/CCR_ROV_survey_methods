# DVL capture: the files and their columns

Schema `ccr.dvl_capture/1`. Everything a capture writes, column by column.
What each source *is* is in [dvl_data_sources.md](dvl_data_sources.md); the
guarantees about these files are in
[dvl_invariants_and_contracts.md](dvl_invariants_and_contracts.md).

## Where, and what a capture is

A **capture** runs from the moment a flight folder is chosen (Monitoring,
section 2) until the program closes or another folder is chosen — armed or
not. Its files go in

```
<flight folder>/logs/dvl/dvl_<capture id>_<part>.<ext>
```

`<capture id>` is the local time it started, to the second
(`2026-10-01_174512`), with `-2`, `-3`, … added if a capture in that folder
already used it. No capture file is ever replaced: every file is created
exclusively.

A day with several flights and one program session is **one capture**. Each
row carries `flight_id` — the flight the flight recorder had open when the
row was written, blank between flights — so a capture is cut by flight with
a filter, not by finding the right file.

| File | Format | One row / line per | Size, roughly |
|---|---|---|---|
| `_tcp.jsonl` | the TCP stream, **verbatim** | every byte, in order | 3–7 KB/s |
| `_tcp_index.csv` | CSV | line of the TCP stream | 1 KB/s |
| `_velocity.csv` | CSV | velocity-and-transducer report | 2–5 KB/s |
| `_deadreckoning.csv` | CSV | dead-reckoning report | 1 KB/s |
| `_commands.csv` | CSV | read-only command sent | tiny |
| `_ws.jsonl` | JSON lines | WebSocket message | 7–12 KB/s |
| `_ws_velocity.csv` | CSV | WebSocket velocity message | 1.5–4 KB/s |
| `_ws_motion.csv` | CSV | other WebSocket message | 3 KB/s |
| `_http.jsonl` | JSON lines | web-API read | 1.5 KB/s |
| `_status.csv` | CSV | status poll (every 2 s) | <1 KB/s |
| `_echo.jsonl` | JSON lines | echo-profile snapshot | **32–37 KB each: 155–185 KB/s at 5 /s** |
| `_spectrum.jsonl` | JSON lines | spectrum snapshot | 12–23 KB/s at 5 /s |
| `_vehicle.jsonl` | JSON lines | extension / mavlink2rest read | <1 KB/s |
| `_mavlink.csv` | CSV | MAVLink counter read | <1 KB/s |
| `_events.txt` | text | event | tiny |
| `.json` | JSON | the capture (its record) | tiny |

**600–670 MB an hour with snapshots at 5 /s**, measured over the two
captures of the first dive (15 October 2026, captures
`2026-10-15_092531` and `2026-10-15_103917`: 404 MB in 36 min and 252 MB in
25 min) — up to 870 MB an hour while the DVL is answering throughout, since
the snapshots stop when it does not. The echo profiles are about eight bytes
in ten. Without snapshots, 70–125 MB an hour. The rates above are from the
same captures, per second of the DVL answering; a faster report rate (the
A50 runs at 2–15 Hz with altitude) moves the TCP and velocity files.

## Conventions, in every file

* **Two clocks.** `rx_unix` is this laptop's wall clock, seconds since 1970
  UTC, to the microsecond; `rx_utc` is the same instant as ISO 8601 UTC.
  `rx_mono_ns` is this laptop's monotonic clock in nanoseconds: it never
  steps, so use it for intervals on the laptop's side. The DVL's clock is in
  its own fields (`time_of_validity_us`, `time_of_transmission_us`, `ts`).
* **Receipt time is when `recv` returned.** Several lines that arrived in one
  read share one `rx_*`; `lines_in_chunk` in the index says how many.
* **Blank means unknown, never zero.** A field the DVL did not send is an
  empty cell.
* **Booleans are `1` / `0`.**
* **Numbers are not rounded.** Floats are written in Python's shortest
  round-trip form; reading one back gives the identical float64.
  Non-finite values are written `nan` / `inf`.
* **Nothing is dropped.** Every report object's fields that this schema does
  not name are written, whole, as JSON in `extra_json`. A transducer entry
  that cannot be placed in a `t<id>_` column (no id, an id outside 0–3, a
  repeated id) or carries extra fields goes into `extra_json` under
  `transducers_unplaced`.
* **Transducers are named by protocol `id`**: `t0_` … `t3_`. The transducer
  number on Water Linked's drawing is `id + 1`. Where each points is in
  [dvl_data_sources.md](dvl_data_sources.md#the-beams).
* **Text is UTF-8.** CSVs are written by Python's `csv` module (comma,
  double-quote quoting, CRLF row ends), with a header row.

## `_tcp.jsonl` — the TCP stream, verbatim

Every byte received from the DVL's TCP JSON port, in the order received,
across reconnections, before anything interprets it. It is newline-delimited
JSON in practice — each line one report or answer, ending `\r\n` — but it is
**not guaranteed to be valid JSON lines**: a connection that drops mid-line
leaves that fragment, and the next connection's first line follows straight
on. The index says where every line, and every fragment, is.

```python
pd.read_json(path, lines=True)       # works unless a connection was cut mid-line;
                                     # use the index (below) when it was
```

## `_tcp_index.csv` — where every line is

| Column | Type | Meaning |
|---|---|---|
| `line_no` | int | 1, 2, 3, … across the whole capture. The join key to `_velocity.csv` and `_deadreckoning.csv` |
| `conn` | int | which TCP connection, 1, 2, … |
| `chunk` | int | the receive call that completed the line |
| `first_chunk` | int | the receive call it began in (≠ `chunk` when it straddled two) |
| `offset` | int | byte offset of the line's first byte in `_tcp.jsonl` |
| `length` | int | bytes, terminator included |
| `terminator` | `CRLF` / `LF` / blank | blank for a fragment |
| `rx_utc`, `rx_unix`, `rx_mono_ns` | | when the line became complete |
| `lines_in_chunk` | int | lines completed by that receive call |
| `kind` | text | the line's `type` (`velocity`, `velocity_water`, `position_local`, `response`, or whatever a newer DVL sends), or `unparsed`, `empty`, `partial`, `no_type`, `not_an_object` |
| `parse_error` | text | why it would not parse |

The lines tile the raw file exactly: each `offset` is the previous
`offset + length`, and the last ends at the file's size.

```python
raw = open(stem + "_tcp.jsonl", "rb").read()
idx = pd.read_csv(stem + "_tcp_index.csv")
line = raw[idx.offset[i]: idx.offset[i] + idx.length[i]]   # exactly as received
```

## `_velocity.csv` — velocity-and-transducer reports

One row per report, whether valid or not.

| Column | Unit | Meaning |
|---|---|---|
| `line_no`, `conn`, `rx_utc`, `rx_unix`, `rx_mono_ns` | | see conventions; `line_no` joins to the index |
| `flight_id` | | flight being recorded, or blank |
| `type`, `format` | | `velocity` / `velocity_water`; e.g. `json_v3.3` |
| `time_ms` | ms | the DVL's `time`: since its **previous** velocity report |
| `vx`, `vy`, `vz` | m/s | |
| `fom` | m/s | figure of merit |
| `altitude` | m | |
| `velocity_valid` | 1/0 | the DVL has bottom lock |
| `status` | | the whole status mask |
| `status_high_temperature` | 1/0 | bit 0 of `status` |
| `time_of_validity_us` | µs since 1970, DVL clock | centre of the ping's reflection |
| `time_of_transmission_us` | µs since 1970, DVL clock | just before the report was sent |
| `cov_xx` … `cov_zz` | (m/s)² | covariance, row-major: `cov_xy` is row x, column y |
| `t<id>_velocity` | m/s | along that beam |
| `t<id>_distance` | m | that beam's range to the bottom |
| `t<id>_rssi` | dBm | received signal strength |
| `t<id>_nsd` | dBm | noise spectral density |
| `t<id>_valid` | 1/0 | `beam_valid` |
| `t<id>_snr_db` | dB | **derived**: `rssi − nsd` |
| `n_transducers` | | how many entries `transducers` had |
| `d_tov_ms` | ms | **derived**: `time_of_validity` minus the previous report's — the interval on the DVL's clock |
| `d_rx_ms` | ms | **derived**: `rx_mono_ns` minus the previous report's — the interval on this laptop's clock |
| `dvl_unaccounted_ms` | ms | **derived**: `d_tov_ms − time_ms`. ≈ 0 when nothing came between; ≈ one interval per report made and not received |
| `tx_minus_tov_ms` | ms | **derived**: `time_of_transmission − time_of_validity` — reflection to send: acoustic travel back, decoding, processing |
| `rx_minus_tx_ms` | ms | **derived**: laptop receipt minus DVL send — network latency **plus the offset between the two clocks** |
| `extra_json` | JSON | fields not named above |

Derived columns are plain differences of recorded numbers; none uses a
threshold. The first report of a capture has blank `d_*` columns.

## `_deadreckoning.csv` — dead-reckoning reports

`line_no`, `conn`, `rx_utc`, `rx_unix`, `rx_mono_ns`, `flight_id`, then the
report: `type`, `format`, `ts` (s, DVL clock — see the data-sources note on
what epoch), `x`, `y`, `z` (m), `std` (m), `roll`, `pitch`, `yaw` (°),
`status`; then `d_ts_ms` (derived: `ts` minus the previous report's, in ms),
`d_rx_ms` (derived, laptop clock), `extra_json`.

`ts × 1000` is the `usec` the BlueOS extension writes into
`GLOBAL_VISION_POSITION_ESTIMATE` — the key for matching these rows to an
mcap. See [dvl_drop_diagnosis.md](dvl_drop_diagnosis.md).

## `_commands.csv` — what was asked, and the answers

| Column | Meaning |
|---|---|
| `conn` | the TCP connection it was asked on |
| `command` | one of `get_config`, `get_version_info`, `get_time_status`, `get_time_ntp`; blank for an answer nobody here asked for |
| `sent_utc`, `sent_unix`, `sent_mono_ns` | when it was written to the socket |
| `reply_line_no` | the answer's `line_no` in the index |
| `reply_utc`, `reply_unix` | when the answer arrived |
| `rtt_ms` | `sent` to answer, monotonic clock |
| `outcome` | `answered`, `no answer` (3 s), `connection closed`, `not sent: …`, `unsolicited` |
| `success`, `error_message` | the DVL's |
| `result_json` | the DVL's `result`, whole |

Asked once per connection in the order `get_config`, `get_version_info`,
`get_time_ntp`, `get_time_status`; then `get_time_status` every 30 s and
`get_config` every 60 s.

## `_ws.jsonl` — the WebSocket stream, verbatim

One line per message:

```json
{"seq": 1, "conn": 1, "rx_utc": "…", "rx_unix": 1790902364.537388,
 "rx_mono_ns": 628579187655500, "opcode": 1, "text": "<the message, exactly>"}
```

`text` is the message exactly as sent (a JSON string, so `json.loads` it
again to get the object). A binary message — never seen — would be `b64`
instead.

## `_ws_velocity.csv` and `_ws_motion.csv`

`_ws_velocity.csv`: `seq`, `conn`, `rx_utc`, `rx_unix`, `rx_mono_ns`,
`flight_id`, then the web stream's velocity message: `time_ms`,
`time_of_validity_us` (**joins to `_velocity.csv`**), `vx`, `vy`, `vz`,
`std` (= `fom`), `altitude`, `velocity_valid`,
**`carrying_out_periodic_cycling`**, **`run_config`**,
**`is_watertracking`**, `cov_xx` … `cov_zz`, `t<id>_velocity`,
`t<id>_distance`, `t<id>_rssi`, `t<id>_nsd`, `t<id>_valid`, `t<id>_snr_db`,
`n_transducers`, `extra_json`.

`_ws_motion.csv`: `seq`, `conn`, `rx_utc`, `rx_unix`, `rx_mono_ns`,
`flight_id`, `channel`, `v0`, `v1`, `v2`, `value_json`. A three-vector
channel fills `v0..v2`; a scalar fills `v0`; anything else is `value_json`.

| `channel` | `v0`, `v1`, `v2` |
|---|---|
| `roll_pitch_yaw` | roll, pitch, yaw (°) |
| `position_local` | x, y, z (m) |
| `position_local_std` | std (m) |
| `fusion_velocity` | vx, vy, vz (m/s) |
| `fusion_velocity_std` | std (m/s) |

## `_http.jsonl`, `_echo.jsonl`, `_spectrum.jsonl`, `_vehicle.jsonl` — web reads, verbatim

One line per GET:

| Key | Meaning |
|---|---|
| `seq` | per file |
| `source` | `dvl`, `dvl_extension` |
| `path` | e.g. `/api/v1/about/status`, `/api/graph`, `/get_status` |
| `t0_utc`, `t0_unix`, `t0_mono_ns` | request sent |
| `t1_unix`, `t1_mono_ns` | answer complete |
| `elapsed_ms` | |
| `status` | HTTP status, or null when nothing answered |
| `error` | why it failed |
| `content_type`, `bytes`, `truncated` | (`truncated` past 2 MB — never expected) |
| `body` | the answer exactly, when it is UTF-8 — otherwise `body_b64` |

`_vehicle.jsonl` also holds mavlink2rest message bodies, one line each time
a counter moved: `{"seq", "source": "mavlink2rest", "system", "component",
"message", "rx_utc", "rx_unix", "counter", "frequency_hz", "body": {…}}`.

Reading an acoustic snapshot:

```python
for line in open(stem + "_echo.jsonl"):
    rec = json.loads(line)
    if rec["status"] != 200:
        continue
    snap = json.loads(rec["body"])                  # {"x_scale", "data": [[t1..t4], …]}
    data = np.array(snap["data"], float) * snap.get("y_scale", 1) + snap.get("y_offset", 0)
    rng = snap.get("x_offset", 0) + np.arange(len(data)) * snap["x_scale"]   # m
```

## `_status.csv` — one row per status poll (every 2 s)

| Column | Meaning |
|---|---|
| `rx_utc`, `rx_unix`, `flight_id` | |
| `dvl_address` | the address read |
| `reachable` | 1 if any of the DVL's status reads succeeded |
| `poll_ms` | how long the poll took |
| `temperature_c`, `cpu_load` | the DVL's. It shuts down at 55 °C and restarts below 50 °C |
| `disk_free_root_gb`, `disk_free_data_gb` | the DVL's own partitions |
| `n_warnings`, `warnings` | active warnings, ids joined by `; ` |
| `json_port`, `json_format`, `json_clients` | the TCP JSON output: its port, format and **connected clients — this capture is one** |
| `pd6_clients`, `pd4_clients`, `serial_format` | the other outputs |
| `cfg_speed_of_sound` … `cfg_periodic_cycling_enabled` | the configuration |
| `dvl_clock_utc` | the DVL's clock, as it wrote it |
| `clock_read_utc` | when it was read: the laptop's clock at the request's midpoint |
| `dvl_clock_minus_laptop_ms` | DVL clock minus the laptop's, at that moment |
| `clock_uncertainty_ms` | the offset is known to within this: half the round trip, plus 1000 when the DVL gave whole seconds |
| `clock_source` | `web API /api/v1/time` (read in this poll, nanoseconds) or `TCP get_time_status` (the last answer, whole seconds — see below) |
| `ntp_enabled`, `ntp_server`, `ntp_synchronized`, `ntp_seconds_since_sync` | the DVL's NTP state |

**The DVL's clock.** Software 2.7.2 answers `/api/v1/time` with a 404 (every
poll's `errors` says so), so on it these columns come from the TCP
`get_time_status` answer, asked every 30 s: `system_time` →
`dvl_clock_utc`, `ntp_synced` → `ntp_synchronized`,
`ntp_seconds_since_last_sync` → `ntp_seconds_since_sync`; and
`get_time_ntp`'s `ntp_address` → `ntp_server`. Every row then repeats the
latest answer, which `clock_read_utc` dates; `ntp_enabled` stays blank, as
the TCP answers do not say. The web API's reading is used whenever there is
one. On 15 October 2026 the A50's clock read 2024-05-27, about 871 days
behind, with NTP not synchronized — so its `time_of_validity_us` and
`time_of_transmission_us` are not UTC.
| `ext_reachable`, `ext_status`, `ext_enabled`, `ext_should_send`, `ext_rangefinder`, `ext_orientation`, `ext_hostname` | the BlueOS DVL extension's `/get_status`, as last read |
| `ext_age_s` | how old that read is |
| `errors` | what failed this poll |

## `_mavlink.csv` — what reached MAVLink

One row per read of one message's counter in mavlink2rest, every 2 s:
`rx_utc`, `rx_unix`, `flight_id`, `system`, `component`, `message`,
`counter` (cumulative count mavlink2rest has seen), `frequency_hz`
(mavlink2rest's own estimate), `fresh` (the counter moved since the last
read), `vehicle_last_update_unix` (the Pi's clock), `error` (`never sent by
this vehicle` = mavlink2rest has never seen it: a 404, or, from the
mavlink2rest in BlueOS 1.4.6, HTTP 200 with an empty body).

Read: 255/0 `VISION_POSITION_DELTA`, `GLOBAL_VISION_POSITION_ESTIMATE`,
`VISION_POSITION_ESTIMATE`, `VISION_SPEED_ESTIMATE`, `DISTANCE_SENSOR` (what
the extension sends); 1/1 `RANGEFINDER`, `DISTANCE_SENSOR` (the autopilot's
own). A message never seen is asked about every 30 s instead of every 2 s.
A message read that nothing answers (a timeout) ends that round of reads,
and the DVL tab says mavlink2rest is not answering only if nothing else in
the round answered; a message the service answered badly is that message's
`error`, and the rest are still read. (Until the fix of 15 October 2026 an empty body
was taken for a vehicle not answering, and on the 15 October dive nothing
after `GLOBAL_VISION_POSITION_ESTIMATE` in the list was ever read.)
The difference between two rows' counters is exactly how many arrived
between them; whether mavlink2rest sees messages the extension *sends*, as
opposed to ones it relays, is one of the bench checks.

## `_events.txt`

Plain text, one event per line, UTC:

```
2026-10-02 00:57:57.384  gap: dvl_quiet              DVL produced nothing for 1100 ms (its own interval 1100 ms; usual 100 ms)
```

Connections and disconnections (with reasons, bytes and lines), the DVL's
identity, address and port changes, configuration changes, warnings, NTP
changes, the DVL's clock at its first reading (how far off, and whether NTP is
synchronized), extension status changes, client-count changes, **every gap with
its classification**, the start and end of every invalid stretch, flights
beginning and ending, snapshot-rate changes, disk state, problems and
diagnostic logs.

## `.json` — the capture's record

Rewritten whole (atomically) at start, every 60 s, and at the end.
`state` is `recording` until the capture closes cleanly and `closed` after;
a capture whose record still says `recording` ended without closing.

| Key | Meaning |
|---|---|
| `schema` | `ccr.dvl_capture/1` |
| `capture_id`, `state`, `started_utc`, `ended_utc`, `seconds` | |
| `folder`, `computer` | |
| `program` | name, git commit, Python, platform |
| `laptop_clock` | the wall clock's implementation and **resolution** (µs on Python 3.13 / Windows; coarser on older Pythons) |
| `vehicle_host`, `vehicle_side_read` | |
| `dvl_address`, `dvl_address_source`, `tcp_port` | |
| `extension` | its port, how it was found, its last status and message type |
| `about` | the DVL's identity and software version |
| `config_at_start`, `config_at_end` | |
| `time_at_start` | the first reading of the DVL's clock: `source` (as `clock_source`), `answer` (the DVL's, whole), `dvl_clock_utc`, `clock_read_utc`, `dvl_clock_minus_laptop_ms`, `clock_uncertainty_ms` |
| `snapshot_rates` | every rate set, with when |
| `disk_free_bytes` | at the last check |
| `files` | per file: name, what it holds, records, bytes written, write failures, rows dropped for a full disk |
| `lines_by_kind`, `tcp_bytes` | |
| `tcp_connections`, `ws_connections` | each: opened, closed, bytes or messages, why it closed |
| `commands` | each command's last outcome |
| `cadence` | report count, invalid count, spans and time, gaps by kind, their total time, the longest, the estimate of reports made and not received, the acoustics-off periods, and the thresholds used — below |
| `flights_seen` | flight ids, and when each was first seen |
| `diagnostic_logs` | each collected: file, SHA-256, bytes, request, timing |
| `problem`, `notes`, `stuck_threads` | |
| `read_only_commands` | the commands this capture was able to send |

### `cadence`

| Key | Meaning |
|---|---|
| `reports` | velocity reports |
| `invalid_reports`, `invalid_spans` | reports with `velocity_valid` false, and the stretches of them |
| `invalid_ms`, `longest_invalid_ms` | their total and longest duration, **an open stretch included** |
| `invalid_open_ms` | the stretch still open when the record was written, to its latest report; 0 if none |
| `gaps`, `gap_ms` | count and total time by kind: `dvl_quiet`, `missing_reports`, `delivery_stall`, `clock_step`, `acoustics_off` |
| `longest_gap_ms` | the longest gap that was a fault — `acoustics_off` is not one |
| `missing_reports_estimate` | reports made and never received, summed over `missing_reports` gaps |
| `acoustics_off_periods` | each period a configuration read found `acoustic_enabled` false: `off_utc`, `on_utc` (null while it lasts) — when the reads saw it, so to within a status poll |
| `thresholds` | what the gaps were judged against |

An invalid stretch runs from its first invalid report to the first valid
one, on the DVL's clock — unless that disagrees with this laptop's by more
than a stall's slack, when it is this laptop's measure, which cannot start
before the capture did. One that the acoustics being switched off interrupts
ends at its last invalid report. (Until the fix of 15 October 2026 a stretch could be
backdated by a stale time of validity — capture `2026-10-15_092531` says
`longest_invalid_ms` 3,574,309 in a 2,176 s capture — and an open stretch
was left out of the totals: capture `2026-10-15_103917` says 2,587 invalid
reports in 760 ms.)

**`acoustics_off`.** With the acoustics off the DVL makes no velocity
reports. A gap is `acoustics_off` instead of any other kind when an
acoustics-off period overlaps the time this laptop waited for its report.
Software 2.7.2's first report after the acoustics come back has a stale
`time_of_validity_us` (one interval after the last ping before they went
off, while its `time_ms` is the whole silence), so the interval to the next
report is not judged on the DVL's clock. Capture `2026-10-15_092531`, from
before this was handled, shows the 76 s off at 16:52:25 UTC as a
`delivery_stall` and then ~291 `missing_reports`.

## Diagnostic logs

`dvl_diagnostic_<local time>.<ext>` — what the DVL returned, byte for byte,
named from its `Content-Disposition` or its first bytes — and
`dvl_diagnostic_<local time>.json` beside it: the request, the HTTP status
and headers, bytes, SHA-256, timing and any error. Written into the flight
folder's `logs/dvl`, or `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\dvl_diagnostic`
with no flight folder.
