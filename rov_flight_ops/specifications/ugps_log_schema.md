# UGPS capture: the files and their columns

Schema `ccr.ugps_capture/1`. What `rov_flight_ops/ugps/` reads from the
Water Linked Underwater GPS G2 topside, and every file and column it writes.

## Where it reads from, and why there

The G2 topside sits on the laptop's side of the tether, plugged into the
laptop's own network port, so the capture reads it **directly from the
laptop** at `192.168.2.94` (the settings file's `ugps_host` changes that).
No Pi, no tether: the capture keeps logging when the Pi drops off the
network, which is what grounded the flight of 15 October 2026.

It reads the G2's documented REST API (`/swagger/swagger.json` on the unit;
Water Linked's public demo is https://demo.waterlinked.com). **GETs only.**

| Path | Every | What it gives |
|---|---|---|
| `/api/v1/position/acoustic/filtered` | 0.25 s | the Kalman-filtered acoustic position, and **per receiver** its distance, signal (RSSI), noise (NSD) and validity |
| `/api/v1/position/acoustic/raw` | 0.25 s | the same, unfiltered (deprecated by Water Linked; asked every 60 s instead if it answers 404) |
| `/api/v1/position/global` | 1 s | the locator as latitude and longitude |
| `/api/v1/position/master` | 1 s | the topside (vessel) reference |
| `/api/v1/about/status`, `/about/temperature`, `/status_report/` | 5 s | battery/gps/imu state, board and water temperature, status groups |
| `/api/v1/about`, `/config/generic`, `/config/receivers/`, `/config/antenna` | 60 s | identity and version, configuration, where each receiver is mounted |

The G2 makes a new acoustic solution about twice a second, so reading at
four catches each one. Repeats are kept and marked `fresh = 0`.

When the G2 stops answering, the capture says so in the events file and asks
`/about/status` every 3 s until it answers again.

## Where, and what a capture is

A capture runs from the moment a flight folder is chosen until the program
closes or another folder is chosen, armed or not. Its files go in

```
<flight folder>/logs/ugps/ugps_<capture id>_<part>.<ext>
```

`<capture id>` is the local time it started, to the second, with `-2`, `-3`, …
added if needed. Files are created exclusively and never replaced. Each row
carries `flight_id`: the flight being recorded when it was written, blank
between flights.

The G2 is read whether or not a folder is chosen, so the Navigation tab's
receiver row works on the bench; nothing is written until a folder is.

| File | One row / line per |
|---|---|
| `_acoustic.csv` | acoustic answer (filtered and raw) |
| `_position.csv` | latitude/longitude answer (global and master) |
| `_status.csv` | status poll |
| `_http.jsonl` | every GET, verbatim |
| `_events.txt` | event |
| `.json` | the capture's record |

About 30 MB an hour, most of it `_http.jsonl`.

## Conventions

As the DVL capture's ([dvl_log_schema.md](dvl_log_schema.md)): `rx_unix` is
the laptop's wall clock in seconds since 1970 UTC, `rx_utc` the same instant
in ISO 8601, `rx_mono_ns` its monotonic clock; blank means unknown, never
zero; booleans are `1`/`0`; floats are not rounded; fields this schema does
not name are kept whole as JSON in `extra_json`. CSVs are UTF-8 with a header
row.

**Receivers are named by their index in the G2's arrays, `r0` to `r3`.** The
G2's own web page may number them from 1. The Navigation tab calls them
`Rx 0` to `Rx 3`, the same as these columns.

## `_acoustic.csv`

| Column | Unit | Meaning |
|---|---|---|
| `seq` | | 1, 2, 3, … across both sources |
| `rx_utc`, `rx_unix`, `rx_mono_ns`, `flight_id` | | see conventions |
| `source` | | `filtered` or `raw` |
| `poll_ms` | ms | how long the GET took |
| `fresh` | 1/0 | the answer differs from this source's previous one (a new solution) |
| `position_valid` | 1/0 | the G2's own judgement |
| `x`, `y`, `z` | m | the locator relative to the topside's acoustic reference |
| `std` | m | the G2's accuracy estimate |
| `r<i>_distance` | m | that receiver's range to the locator; `-1` when it has none |
| `r<i>_rssi` | dB | received signal strength |
| `r<i>_nsd` | dB | noise spectral density |
| `r<i>_valid` | 1/0 | the G2 used that receiver |
| `r<i>_snr_db` | dB | **derived**: `rssi − nsd` |
| `n_receivers` | | how many receivers the answer had; beyond four they go in `extra_json` under `receivers_beyond_columns` |
| `extra_json` | JSON | fields not named above |

To compare receivers, keep `source == "filtered"` and `fresh == 1`.

An acoustic answer that is not 200 (the G2 answers 500 with no locator in
the water) writes no row here. It is in `_http.jsonl` and counted in the
record.

## `_position.csv`

`seq`, `rx_utc`, `rx_unix`, `rx_mono_ns`, `flight_id`, `source` (`global` =
the locator, `master` = the topside), `poll_ms`, `fresh`, then `lat`, `lon`
(degrees), `orientation` (degrees, −1 when unknown), `cog`, `sog`, `hdop`,
`numsats`, `fix_quality`, `extra_json`.

## `_status.csv`

`rx_utc`, `rx_unix`, `flight_id`, `g2_address`, `reachable`, `poll_ms`,
`battery`, `gps`, `imu` (from `/about/status`), `board_temp_c`,
`water_temp_c`, `status_groups` (every group as `name=state`, `; `-joined),
`groups_not_ok`, `status_messages` (each group's message and error ids),
`errors` (paths that did not answer 200 this poll).

## `_http.jsonl`

One line per GET, the same record the DVL capture writes: `seq`, `source`
(`acoustic`, `position`, `status`, `config`), `path`, `t0_utc`, `t0_unix`,
`t1_unix`, `t0_mono_ns`, `t1_mono_ns`, `elapsed_ms`, `status` (null when
nothing answered), `error`, `content_type`, `bytes`, `truncated`, and `body`
exactly as received (or `body_b64`).

## `_events.txt`

The capture starting and stopping, the G2's address and identity, the G2
answering and not answering, configuration changes (each key, before and
after), status groups changing state, a path the firmware does not serve,
and flights beginning and ending.

## `.json`: the record

Rewritten whole every 60 s and at the end. `state` is `recording` until the
capture closes cleanly and `closed` after.

`schema`, `capture_id`, `state`, `started_utc`, `ended_utc`, `seconds`,
`folder`, `computer`, `program` (name, git commit, Python, platform),
`laptop_clock` (its resolution), `g2_address`, `about`, `config_at_start`,
`config_at_end`, `receivers_config`, `periods_s`, `polls` (per path: answered
200, other HTTP answers, no answer), `files` (per file: records, write
failures), `flights_seen`, `problem`, and **`receivers`**: per receiver over
the capture's fresh filtered solutions, `fresh_solutions`, `valid_fraction`,
`snr_db_mean_when_valid`, `snr_db_min_when_valid`, `rssi_dbm_mean_when_valid`,
`nsd_db_mean`, `nsd_db_max`. That summary is the quickest way to see whether
one receiver was weaker or noisier than the rest over a whole day.

## The Navigation tab's receiver row

The row under the map shows, per receiver, the median signal-to-noise and
noise floor over the last minute and a sparkline of the signal-to-noise (0-60
dB, dashed line at 30 dB). The text at its left is `ugps/health.py`
comparing each receiver with the median of the other three:

| Flag | When |
|---|---|
| low SNR | its signal-to-noise is 6 dB or more below theirs |
| noisy | its noise floor is 6 dB or more above theirs |
| dropping out | it is valid in under half the solutions while they manage 80% |

Nothing is judged with fewer than ten solutions in the minute. The
thresholds are first guesses, to be tuned against real dives. It is a
screening aid, not a diagnosis: a receiver shadowed by the hull or a pier pile
will flag too.

## The extension's own logs, on the Pi

The WL UGPS External extension (`clydemcqueen/wl_ugps_external_extension`)
already polls the G2 and writes `/data/logs/<date>/<date>_<time>_g2_acoustic.csv`
inside its container. On the Pi that is
`/usr/blueos/extensions/wl_ugps_external/logs/`. Its columns are `timestamp`,
`position_valid`, `x`, `y`, `z`, `std`, `distance_r0..3`, `nsd_r0..3`,
`rssi_r0..3` and `valid_r0..3`, about five times a second. Beside it are its
`g2_position`, `g2_orientation`, `GGA`, `HDT` and `PASHR` logs.

**BlueOS logs → UGPS logs** downloads all of them into
`<flight folder>/logs/ugps/pi/<date>/`. Those files exist from the day the
extension was installed, so they cover dives flown before this capture
existed. Their names carry the UTC time the extension started, which is how
the downloader places them in time.
