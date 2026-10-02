# What the DVL A50 gives a topside laptop

Every interface the Water Linked DVL A50 offers over Ethernet, what each one
carries, how fast, and whether the capture records it. Written against Water
Linked's documentation at `waterlinked/docs` commit `8798af4` (1 October
2026; the TCP JSON page last changed in `8d2f75d`, 11 June 2026, describing
`json_v3.3` / software 2.7.2), the web GUI's own JavaScript, and a probe of
Water Linked's public demo DVL (`dvl.demo.waterlinked.com`, software 2.7.1,
`json_v3.2`) on 1 October 2026. See
[dvl_artifacts_and_provenance.md](dvl_artifacts_and_provenance.md) for the
details of each.

**Nothing in this document has yet been seen on our own DVLs.** Their
software version is not recorded anywhere in this repository; the capture
records it (`about` in the capture record) the first time it connects.

## The short answer

| Source | Transport | Rate | Captured? | Into |
|---|---|---|---|---|
| Velocity-and-transducer reports | TCP 16171, JSON lines | 2–15 Hz (range mode) | **yes, every byte** | `_tcp.jsonl`, `_velocity.csv` |
| Dead-reckoning reports | TCP 16171, JSON lines | 5 Hz (doc); 10 Hz on the demo | **yes, every byte** | `_tcp.jsonl`, `_deadreckoning.csv` |
| Answers to commands | TCP 16171 | on request | **yes** (read-only questions only) | `_tcp.jsonl`, `_commands.csv` |
| Web GUI live stream | WebSocket `/ws` | velocity at report rate; others 10 Hz | **yes, every message** | `_ws.jsonl`, `_ws_velocity.csv`, `_ws_motion.csv` |
| Temperature, CPU, disk | HTTP `/api/v1/about/status` | polled every 2 s | **yes** | `_http.jsonl`, `_status.csv` |
| Warnings | HTTP `/api/v1/warnings/` | every 2 s | **yes** | same |
| Output ports and **connected clients** | HTTP `/api/v1/outputs/` | every 2 s | **yes** | same |
| Configuration | HTTP `/api/v1/config` (and TCP `get_config`) | every 2 s (TCP every 60 s) | **yes** | same, and `_commands.csv` |
| The DVL's clock and NTP state | HTTP `/api/v1/time` (and TCP `get_time_status`, 2.7.2+) | every 2 s (TCP every 30 s) | **yes** | same |
| Identity and software version | HTTP `/api/v1/about` (and TCP `get_version_info`, 2.7.2+) | every 60 s | **yes** | same |
| Network configuration | HTTP `/api/v1/ip`, `/api/v1/ip/current` | every 60 s | **yes** | `_http.jsonl` |
| **Echo profile** — signal strength against range, per beam | HTTP `/api/graph` | snapshot; default **5 /s** | **yes, whole** | `_echo.jsonl` |
| **Spectrum** — spectral density against frequency, per beam | HTTP `/api/spectrum` | snapshot; default **5 /s** | **yes, whole** | `_spectrum.jsonl` |
| Diagnostic log (Water Linked support capture) | HTTP `/api/collect` | on request, 15 s – 5 min | **on request only**, disarmed | `dvl_diagnostic_*` |
| PD4 / PD6 | TCP 1038 / 1037 | report rate | no — see below | — |
| IMU orientation | HTTP `/api/v1/imu/` | — | no — the WebSocket carries it faster | — |
| Velocity (GUI copy) | HTTP `/api/v1/velocity` | — | no — the TCP stream has every report | — |
| Serial (UART) protocol | 3.3 V UART | — | no — not reachable over Ethernet | — |

And on the vehicle side, which is where the dropped messages are seen:

| Source | Transport | Captured? | Into |
|---|---|---|---|
| BlueOS DVL extension status (`status`, `should_send`, `hostname`, …) | HTTP `/get_status` on the extension's port | **yes**, every 2 s | `_vehicle.jsonl`, `_status.csv` (`ext_*`) |
| What reached MAVLink from the extension (system 255, component 0) | mavlink2rest counters | **yes**, every 2 s | `_mavlink.csv`, bodies in `_vehicle.jsonl` |
| The autopilot's own rangefinder messages (1/1) | mavlink2rest counters | **yes** | same |

## The TCP JSON stream (port 16171)

The DVL's documented integration interface. A TCP server that sends every
connected client every report, and answers commands from each client.

**Velocity-and-transducer report** (`"type": "velocity"`, or
`"velocity_water"` in water-tracking mode, 2.7+). One per velocity
calculation; 2–15 Hz depending on altitude. Fields (`json_v3.3`):

| Field | Meaning | Unit |
|---|---|---|
| `time` | milliseconds since the DVL's **previous velocity report** | ms |
| `vx`, `vy`, `vz` | velocity in the DVL (or vehicle, with a mounting offset) frame | m/s |
| `fom` | figure of merit — the accuracy of the velocities | m/s |
| `covariance` | 3 × 3 covariance of the velocities | (m/s)² |
| `altitude` | distance to the reflecting surface along z | m |
| `transducers[]` | per beam: `id` (0–3), `velocity` (m/s), `distance` (m), `rssi` (dBm), `nsd` (dBm), `beam_valid` | |
| `velocity_valid` | true when the DVL has a lock on the reflecting surface | bool |
| `status` | bit mask; **bit 0 = high temperature, thermal shutdown soon**; others reserved | |
| `time_of_validity` | the DVL's clock at the **surface reflection — the centre of the ping** | µs since 1970 |
| `time_of_transmission` | the DVL's clock **immediately before the report was sent** | µs since 1970 |
| `format` | protocol version, e.g. `json_v3.3` | |

**Dead-reckoning report** (`"type": "position_local"`). Expected 5 Hz; `ts`
(seconds — Unix time per the document, but the documented example is
`49056.809`, which is time since boot, so do not assume), `x`, `y`, `z`
(m), `std` (m), `roll`, `pitch`, `yaw` (degrees), `status`, `format`.

**Commands.** JSON objects, one per line. The capture sends only four, all
questions: `get_config`, `get_version_info` (2.7.2+), `get_time_status`
(2.7.2+) and `get_time_ntp` (2.7.2+). It never sends `set_config`,
`reset_dead_reckoning`, `calibrate_gyro`, `trigger_ping`, `set_time_ntp`,
`set_time_manual` or `force_sync_ntp` — see the read-only invariants.

**Observed, not documented:**

* Lines end `\r\n`, not `\n` (298 of 298 on the demo).
* The DVL parses everything that arrives in one TCP read as **one** JSON
  document: six commands written together got one answer and
  `"Invalid JSON"`. The capture therefore sends one command and waits for
  its answer (or 3 s) before the next.
* A command the DVL does not know is answered `success: false`,
  `error_message: "Command not recognised"`, with `response_to` set — what
  2.7.1 says to the three 2.7.2 commands. One that does not parse is
  answered `"Invalid JSON"` with `response_to: ""`.
* The output's TCP port is configurable (GUI → outputs). The capture reads
  the port from `/api/v1/outputs/` and follows it.

## The web GUI's API and stream

Not in Water Linked's protocol documentation. These are what the DVL's own
web pages call, read off the GUI's JavaScript bundle (2.7.1) and checked by
GETs against the demo. **They could change in a software update without
notice**, which is one reason the capture keeps every reply verbatim.

### WebSocket `/ws`

Messages `{"channel": …, "payload": …}`. Channels seen on the demo, and
rates there:

| Channel | Payload | Demo rate |
|---|---|---|
| `velocity` | the velocity report in the GUI's own shape — see below | 5 /s (= report rate) |
| `roll_pitch_yaw` | `[roll, pitch, yaw]`, degrees, from the DVL's IMU/AHRS | 10 /s |
| `position_local` | `[x, y, z]`, m, dead reckoning | 10 /s |
| `position_local_std` | m | 10 /s |
| `fusion_velocity` | `[vx, vy, vz]`, m/s, the dead-reckoning filter's | 10 /s |
| `fusion_velocity_std` | m/s | 10 /s |
| `fusion_reset` | dead reckoning was reset | on event |

The `velocity` payload is the same measurement as the TCP report —
`time_of_validity` joins them — but it carries **three fields the TCP
report does not**:

* `carrying_out_periodic_cycling` — the DVL is in the middle of its 10-second
  bottom-lock check, during which "some measurements are lost" (Water
  Linked, configuration.md). You turn periodic cycling off; this field is
  how a capture proves it stayed off.
* `run_config` — the range configuration the DVL is actually running (0–4),
  as opposed to the configured `range_mode` (e.g. `auto`).
* `is_watertracking`.

It lacks the TCP report's `status`, `format`, `type` and
`time_of_transmission`, and names things differently: `std` for `fom`,
`cov` for `covariance`, per-beam `is_valid` (0/1) for `beam_valid`.

### Status and configuration

| Path | Returns |
|---|---|
| `/api/v1/about/status` | `temperature` (°C), `cpu_load`, `disk_free` {partition: GB} |
| `/api/v1/warnings/` | list of active warnings, e.g. `warnings:temperature_high` |
| `/api/v1/outputs/` | each output: `id` (`tcp_water_linked`, `tcp_pd6`, `tcp_pd4`, `serial`), `port`, `format`, **`clients`** (connected client count), `error` |
| `/api/v1/config` | `speed_of_sound`, `mounting_rotation_offset`, `acoustic_enabled`, `dark_mode_enabled`, `range_mode`, `periodic_cycling_enabled` |
| `/api/v1/time` | `time` (RFC 3339 to the nanosecond), `ntp_enabled`, `ntp_server`, `ntp_synchronized` |
| `/api/v1/about` | `chipid`, `hardware_revision`, `product_id`, `product_name`, `variant`, `version`, `version_short`, `is_ready` |
| `/api/v1/ip`, `/api/v1/ip/current` | configured and current address, DHCP, prefix |

`clients` on the JSON output is worth having on every row: the capture is
one client, so `1` means **the BlueOS extension is not connected to the
DVL**, and `2` means it is.

### The acoustic views

The DVL GUI's diagnostics page polls these ten times a second.

**`/api/graph`** — "Acoustic signal view". `{"x_scale", "y_scale",
"x_offset"?, "y_offset"?, "data": [[t1, t2, t3, t4], …]}`: 1,024 samples per
beam in bottom-tracking mode, **signal strength (dBm) against distance (m)**
— the echo envelope of the latest ping. Distance of sample *k* is
`x_offset + k × x_scale`; value is `v × y_scale + y_offset`. The GUI treats
values below −10⁹ as no data. In water-tracking mode the GUI reads the same
endpoint as **relative power (dBm) against time (s)** with eight series
(`t1..t4 strength`, `t1..t4 noise`) — the capture stores whatever comes, so
both shapes are kept. Because range and two-way travel time are tied by the
speed of sound, *t = 2 d / c* with `c` = `speed_of_sound` from the
configuration, this is the nearest the DVL comes to per-beam receive times
(see "What the DVL does not expose").

**`/api/spectrum`** — "Spectrum". Same shape, 128 bins per beam,
**spectral density (dBm) against frequency (kHz)**, `x_offset` 968.75 kHz,
`x_scale` 0.48828125 kHz on the demo: about 969–1031 kHz around the
A50's 1 MHz carrier.

Each is a snapshot of the latest ping, about 18 KB and 3 KB of JSON. At the
default 5 snapshots a second that is roughly **375 MB an hour**. The rate is
set on the DVL tab (Off, 1, 2, 5, 10 /s); the GUI's own diagnostics page
polls at 10 /s. They pause automatically when the flight folder's drive has
less than 2 GiB free.

### The diagnostic log

`GET /api/collect?desc=<description>[&t=<seconds>]` — the DVL's form sends
it as a plain HTML GET, `t` omitted at the default 15 s; the form offers 15,
30, 60 and 300 s. The DVL records its own logs and measurements for that
long and returns a file for Water Linked support. **The format has not been
seen**: the demo answers `406 - Sorry not simulated`. The DVL tab collects
one only when asked, only with the vehicle confirmed disarmed, and saves it
as it arrives with its SHA-256.

## Deliberately not captured

* **PD4 / PD6 (TCP 1038 / 1037).** Re-encodings of the same velocity report
  for legacy integrations, at lower precision (mm/s integers, cm ranges) and
  with fewer fields; Water Linked's own table marks PD4's temperature and
  built-in-test fields "not used". Nothing in them is not already in the
  JSON report. Each would be one more client on the DVL. Adding them later
  is a few lines in `capture.py` if a use appears.
* **`/api/v1/imu/`, `/api/v1/velocity`.** The GUI's polled copies of what
  the WebSocket and TCP stream already carry, at lower rates.
* **Anything the GUI can change** — configuration, network, reboots, factory
  reset, gyro calibration, dead-reckoning reset, output settings. Read-only.

## What the DVL does not expose at all

Asked for: "acoustic beam times of transmission, times they are received,
times they are analysed, the clock the DVL has, spectral density, spectral
noise." Against what exists:

| Wanted | What exists | Where in the capture |
|---|---|---|
| Time of transmission (ping) | **Not exposed.** `time_of_validity` is the centre of the ping's *reflection*, not the transmit time | `time_of_validity_us` |
| Time received, per beam | **Not exposed as a time.** The echo profile gives received strength against *range* per beam; *t = 2d/c* turns it into a receive-time profile | `_echo.jsonl` |
| Time analysed | **Not exposed per beam.** `time_of_transmission` is when the finished report was sent; `tx_minus_tov_ms` is reflection-to-send, which Water Linked describe as acoustic travel back plus decoding and processing | `time_of_transmission_us`, `tx_minus_tov_ms` |
| The DVL's clock | **Yes**: every report's two timestamps, `/api/v1/time` every 2 s with NTP state, and `get_time_status` | `_velocity.csv`, `_status.csv` (`dvl_clock_minus_laptop_ms`) |
| Spectral density | **Yes**: per-beam spectrum, ~969–1031 kHz | `_spectrum.jsonl` |
| Spectral noise | **Yes**: `nsd` per beam per report (noise spectral density, dBm), and the noise floor in the spectrum | `t*_nsd`, `_spectrum.jsonl` |
| Received signal strength | **Yes**: `rssi` per beam per report, and the echo profile | `t*_rssi`, `_echo.jsonl` |
| Raw samples, correlation, Doppler spectra | **Not exposed** over any documented or GUI interface. The diagnostic log may hold more; its format is unknown until one is collected | `dvl_diagnostic_*` |
| Raw IMU (rates, accelerations) | **Not exposed** — only the AHRS's roll, pitch, yaw | `_ws_motion.csv` |

## The BlueOS extension, and why it matters here

The stock `BlueOS-Water-Linked-DVL` extension (v1.0.10 on the vehicles as of
18 September 2026; master `cf0224f` inspected) is what turns DVL reports into
MAVLink. Read from its source, four behaviours bear directly on "messages
dropped in the mcap, Cockpit and QGC but not on the DVL's own page":

1. **Invalid reports are discarded.** `handle_velocity` returns early when
   `velocity_valid` is false: no `DISTANCE_SENSOR`, no
   `VISION_POSITION_DELTA`. Every stretch of lost bottom lock is a gap
   downstream, and the DVL's own page shows the same stretch continuously
   (as invalid).
2. **One line per loop.** Each pass of its loop takes at most one line from
   its buffer, then sleeps 3 ms; each velocity report costs up to two
   blocking HTTP POSTs to mavlink2rest (rangefinder, then vision), each
   position report one (`GLOBAL_VISION_POSITION_ESTIMATE` under
   `POSITION_ESTIMATE`). A slow mavlink2rest makes it fall behind.
3. **Its buffer is thrown away on any hiccup.** Three seconds with nothing
   received, or a socket error, and it empties its buffer and reconnects —
   whatever was buffered is gone.
4. **It sends as system 255, component 0**, with `time_usec` 0 in
   `VISION_POSITION_DELTA` (the delta's length is the DVL's `time`, in µs)
   and `usec` = the DVL's dead-reckoning `ts` × 1000 in
   `GLOBAL_VISION_POSITION_ESTIMATE`. That last one is a join key between a
   capture and an mcap.

See [dvl_drop_diagnosis.md](dvl_drop_diagnosis.md) for how to use these.

## The beams

From Water Linked's transducer-numbering drawing of the A50 face: the cable
— the DVL's aft, −x — is at the top of the picture, which is the face seen
from below, so the picture's right is starboard. With a mounting rotation
offset of 0:

| Transducer (drawing) | `id` (protocol) | Points |
|---|---|---|
| 1 | 0 | aft-starboard |
| 2 | 1 | aft-port |
| 3 | 2 | forward-port |
| 4 | 3 | forward-starboard |

A non-zero `mounting_rotation_offset` rotates these clockwise by the offset,
and the DVL tab does so. Half-power beam width 4.4°. This agrees with Keenan
Johnson's beam-splitter code and not with his `technical_details.md`, which
disagree with each other. **It is derived from a photograph and the bench
checklist asks for it to be confirmed.**

Water Linked's A50 range modes (range-mode.md): 0 = 0.05–0.6 m at 15 Hz,
1 = 0.3–3 m at 10 Hz, 2 = 1.5–14 m at 5–6 Hz, 3 = 7.7–36 m at 7–8 Hz,
4 = 15 m+ at 2–4 Hz. At a 1 m survey altitude expect mode 1, about 10 Hz.
