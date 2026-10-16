# Finding where DVL messages are lost

The symptom, as reported on 1 October 2026: DVL data drops out in the
mcaps, in Cockpit and in QGC, but not when the DVL's own web page is
watched at its address. That points away from the DVL and towards the
path from the DVL to the recording — but it does not say where. A capture
records what the DVL actually said, independently of that path, so every
gap in an mcap can be looked up in it.

## The path, and where each link can lose a message

```
DVL ──TCP 16171──▶ BlueOS DVL extension ──HTTP POST──▶ mavlink2rest / router ──▶ ArduSub
 │                       (on the Pi)                         │
 │                                                           ├──▶ BlueOS recorder ──▶ mcap
 │                                                           └──▶ tether ──▶ Cockpit / QGC
 └──TCP 16171 / HTTP / WebSocket ──tether──▶ this capture (topside)
```

| Where | How a message is lost there | What the capture shows |
|---|---|---|
| The DVL | no bottom lock: reports keep coming, marked invalid | `velocity_valid = 0` rows; "velocity became INVALID" events |
| | no report at all: range search, periodic cycling, thermal trouble, power | `dvl_quiet` gaps; `carrying_out_periodic_cycling`; temperature, `status_high_temperature` |
| The DVL's TCP server | a report made but not sent to a client | `missing_reports` gaps (`dvl_unaccounted_ms` ≈ one interval per lost report) |
| The extension | **drops every invalid report by design** | same rows as "no bottom lock" above |
| | falls behind (one line per loop, blocking POSTs) | nothing on the topside; latency grows in the mcap (Recipe A's offset drifts up) |
| | empties its buffer and reconnects after 3 s of silence or a socket error | `ext_status` changes ("timeout, restarting"); `json_clients` drops to 1 |
| mavlink2rest / router / recorder | a POST that fails or a message not recorded | the capture has the report; the mcap does not (Recipe A) |
| The tether | live displays starve | `delivery_stall` gaps at the topside; the mcap (recorded on the Pi) is unaffected |

Two rows deserve emphasis. **The stock extension forwards only valid
reports**, so every loss of bottom lock is a hole in the mcap's
`DISTANCE_SENSOR` and `VISION_POSITION_*` while the DVL's page shows the
DVL running throughout — exactly the reported symptom. And **Cockpit and
QGC are on the far side of the tether** from the recording: a tether stall
blanks them without touching the mcap. If they blank where the mcap is
complete, the tether is the place to look.

## Before a dive

1. Choose the flight folder. The capture starts; the DVL tab says what it
   is writing.
2. On the DVL tab, section 2: `clients on the DVL's JSON output` should read
   **2** — this program and the BlueOS extension. **1** means the extension
   is not connected to the DVL at all.
3. Periodic cycling should read **off**.
4. If the vehicle's mavlink2rest shows the extension's messages, section 2's
   `→ DISTANCE_SENSOR` rate should track the DVL's valid velocity rate when
   the rangefinder is enabled in the extension.

## After a dive: recipes

Assume `stem = ".../logs/dvl/dvl_2026-10-08_101500"` and an mcap from the
same flight. Python with `pandas` and `mcap` (both already installed with
this program).

```python
import json
import numpy as np
import pandas as pd
from mcap.reader import make_reader

stem = r"...\logs\dvl\dvl_2026-10-08_101500"
vel = pd.read_csv(stem + "_velocity.csv")
dr = pd.read_csv(stem + "_deadreckoning.csv")
status = pd.read_csv(stem + "_status.csv")
record = json.load(open(stem + ".json"))

def mcap_messages(path, topics):
    """(topic, message, Pi-clock seconds) for the given topics."""
    with open(path, "rb") as fh:
        for _schema, ch, msg in make_reader(fh).iter_messages(topics=topics):
            yield ch.topic, json.loads(msg.data)["message"], msg.log_time / 1e9
```

### Recipe 0 — what the capture alone says

```python
print(record["cadence"])                 # gaps by kind, invalid time, estimate of lost reports
print(open(stem + "_events.txt").read()) # every gap and transition, in order
vel["t"] = pd.to_datetime(vel.rx_unix, unit="s")
vel.set_index("t").velocity_valid.resample("10s").mean().plot()   # share valid
```

If `record["cadence"]["gaps"]` is all zeros and `invalid_ms` is small, the
DVL delivered everything to the topside, and any hole in the mcap was made
after the DVL.

### Recipe A — which dead-reckoning reports reached the mcap (exact)

Under the `POSITION_ESTIMATE` message type, the extension sends one
`GLOBAL_VISION_POSITION_ESTIMATE` per DVL dead-reckoning report and sets its
`usec` to `int(ts * 1e3)` — the DVL's own timestamp. That is an exact key,
independent of any clock:

```python
dr["usec"] = [int(ts * 1e3) for ts in dr.ts]        # the extension's own arithmetic
got = pd.DataFrame(
    [(m["usec"], t) for _top, m, t in mcap_messages(
        mcap_path, ["mavlink/255/0/GLOBAL_VISION_POSITION_ESTIMATE"])],
    columns=["usec", "mcap_t"])
m = dr.merge(got.drop_duplicates("usec"), on="usec", how="left")
lost = m[m.mcap_t.isna()]
print(f"{len(lost)} of {len(m)} DVL dead-reckoning reports never reached the mcap")
# When they were lost, and in runs:
lost_runs = (m.mcap_t.isna() != m.mcap_t.isna().shift()).cumsum()[m.mcap_t.isna()]
print(lost.groupby(lost_runs).agg(start=("rx_utc", "first"), n=("line_no", "size")))
# Pi clock minus laptop clock, plus the extension's latency, report by report:
m["pi_minus_rx_s"] = m.mcap_t - m.rx_unix
m.pi_minus_rx_s.describe()      # a value that climbs = the extension falling behind
```

Under `POSITION_DELTA` the extension sends `VISION_POSITION_DELTA` instead,
with `time_usec` 0 and `time_delta_usec` = the DVL's `time` × 1000, **only
for valid reports**. Match it to `_velocity.csv` by the sequence of
`time_delta_usec` against `time_ms × 1000`.

### Recipe B — the altitude that Cockpit and QGC show

The extension sends `DISTANCE_SENSOR` (255/0, `current_distance` =
`int(altitude × 100)` cm) once per **valid** velocity report with altitude
above 0.05 m, if its rangefinder setting is on. It carries no DVL timestamp,
so match on time after aligning the clocks with Recipe A's offset:

```python
offset = m.pi_minus_rx_s.median()
ds = pd.DataFrame([(t - offset, m_["current_distance"]) for _top, m_, t in
                   mcap_messages(mcap_path, ["mavlink/255/0/DISTANCE_SENSOR"])],
                  columns=["rx_equiv", "cm"])
expect = vel[(vel.velocity_valid == 1) & (vel.altitude > 0.05)]
bins = np.arange(vel.rx_unix.min(), vel.rx_unix.max(), 1.0)
per_s = pd.DataFrame({
    "dvl_valid": np.histogram(expect.rx_unix, bins)[0],
    "dvl_all": np.histogram(vel.rx_unix, bins)[0],
    "in_mcap": np.histogram(ds.rx_equiv, bins)[0]}, index=bins[:-1])
per_s["short"] = per_s.dvl_valid - per_s.in_mcap
per_s[per_s.short > 0]          # seconds where valid reports did not reach the mcap
per_s[per_s.dvl_valid < per_s.dvl_all]   # seconds where the DVL itself was invalid
```

Seconds where `dvl_valid < dvl_all` and `in_mcap` follows `dvl_valid` are
holes made by the extension's design, not by a fault.

### Recipe C — the extension's own state

```python
status[["rx_utc", "ext_status", "ext_reachable", "json_clients"]] \
    .loc[lambda d: d.ext_status.ne(d.ext_status.shift()) | d.json_clients.ne(d.json_clients.shift())]
```

`json_clients` falling from 2 to 1 is the extension disconnecting from the
DVL; its `ext_status` will say why. Pair it with the BlueOS extension's own
log if it is still on the Pi.

### Recipe D — the tether, against the flight recorder's network trace

`delivery_stall` events mean the DVL reported on time and this laptop
received the reports late. The flight recorder's `logs/network_pings_*.csv`
(5 Hz) and `network_fast_*.csv` (10 Hz) cover the same seconds:

```python
pings = pd.read_csv(glob.glob(r"...\logs\network_pings_*.csv")[0])
stalls = [l for l in open(stem + "_events.txt") if "delivery_stall" in l]
```

Stalls that line up with lost pings are the tether; stalls with clean pings
are this laptop (its CPU, its adapter's power saving — see Monitoring,
section 2).

### Recipe E — the DVL itself

```python
vel[["rx_utc", "status_high_temperature"]].query("status_high_temperature == 1")
status[["rx_utc", "temperature_c", "cpu_load", "warnings"]].describe()
pd.read_csv(stem + "_ws_velocity.csv").carrying_out_periodic_cycling.sum()   # should be 0
for b in range(4):
    print(b, vel[f"t{b}_valid"].mean(), vel[f"t{b}_snr_db"].median())        # a weak beam?
```

A beam that is invalid far more often than the others, or whose SNR sits
well below theirs, is pointing at something — a skid, a cable, a wall —
or is damaged. Its acoustic snapshots (`_echo.jsonl`) show whether its echo
is there and weak, or absent.

## Bench experiments worth running once

1. **Snapshots and the DVL's CPU.** Record five minutes at each snapshot
   rate (off, 5 /s, 10 /s) with the DVL in a tank. Compare `cpu_load` and
   the report cadence. If the DVL's own rate suffers, lower the default.
2. **Can a topside client hold the DVL back?** With the extension and this
   capture both connected, pull the tether for 60 s and plug it back in.
   The mcap's `DISTANCE_SENSOR` (recorded on the Pi) must have no gap
   during the outage. If it does, a topside client stalls the DVL's server
   when the tether drops, and the capture should run on the Pi instead.
3. **Periodic cycling.** Turn it on for two minutes: the capture should
   report `periodic cycling is ON`, `carrying_out_periodic_cycling` spans
   every ~10 s, and `dvl_quiet` gaps on the same cadence. This proves the
   detection end to end on our DVL.
4. **Bottom lock.** Lift the vehicle out of range: invalid stretches begin
   and end in the events file, and the extension's `DISTANCE_SENSOR` stops
   for exactly those stretches (Recipe B).
