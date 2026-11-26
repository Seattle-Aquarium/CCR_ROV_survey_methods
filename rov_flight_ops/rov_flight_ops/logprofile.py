"""
How much to log: **Standard** for a vehicle that is working, **Intensive** for
chasing a fault.

Most of what this program records was built to find specific problems -- the
DVL "drops", the Pi's Ethernet outages of 15 October 2026, video that reached
the laptop and not Cockpit. Those investigations are done, and logging every
byte of every one of them costs, on every flight:

* **disk** -- a 1.7-hour test on 24 November 2026 wrote 532 MB of DVL capture,
  390 MB of it the acoustic echo snapshots;
* **the tether and the DVL** -- five echo and five spectrum GETs a second, each
  a few kilobytes of JSON, plus the DVL's web-GUI stream;
* **the Pi** -- the DVL extension's status and mavlink2rest's counters every
  two seconds, the Pi's own load every five, the video streams every thirty;
* **the laptop** -- parsing and writing all of it, and a network trace at
  10 Hz (adapter counters) and 5 Hz (pings).

**Standard** keeps everything a flight's record and its report rest on, at
rates that cost next to nothing; **Intensive** is every recorder at full rate,
exactly as before. Switching takes effect at once for the DVL and UGPS
captures (each starts a fresh capture file, named for the profile in its
record) and at the next flight for the flight recorder and network trace.

=================================  ===================  ======================
what                               Standard             Intensive
=================================  ===================  ======================
laptop monitor (Live monitoring)   1 Hz                 1 Hz
flight record, parameters, report  yes                  yes
network: pings / adapter counters  1 Hz / 1 Hz          5 Hz / 10 Hz
DVL: every report, as CSV          yes                  yes
DVL: byte-exact TCP stream + index no                   yes
DVL: web-GUI stream (10 Hz)        no                   yes
DVL: echo + spectrum snapshots     off                  5 a second
DVL: status / extension polls      every 10 s           every 2 s
DVL: raw HTTP replies              no                   yes
UGPS: each receiver, filtered      1 Hz                 4 Hz
UGPS: raw acoustic + raw HTTP      no                   yes
Pi load / video streams polled     15 s / 2 min         5 s / 30 s
Navigation parameter re-read       10 min               2 min
=================================  ===================  ======================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

STANDARD = "standard"
INTENSIVE = "intensive"


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    #: One line for the Monitoring tab.
    blurb: str
    # -- the DVL capture
    dvl_raw_stream: bool          # tcp.jsonl + tcp_index.csv
    dvl_web_stream: bool          # the DVL web GUI's websocket, and its CSVs
    dvl_raw_http: bool            # http.jsonl, vehicle.jsonl
    dvl_snapshot_hz: float | None  # None: the DVL tab's own choice
    #: None: the capture's own full rate (2 s).
    dvl_status_s: float | None
    dvl_vehicle_s: float | None
    # -- the network trace, per flight
    net_counters_s: float
    net_ping_s: float
    # -- the UGPS capture
    ugps_acoustic_s: float
    ugps_raw: bool                # raw acoustic solution, and http.jsonl
    # -- the flight recorder's vehicle polls
    pi_health_s: float
    video_streams_s: float
    # -- the Navigation collector
    nav_params_s: float


PROFILES: dict[str, Profile] = {p.key: p for p in (
    Profile(STANDARD, "Standard",
            "everything a flight's record and report need, at low rates",
            dvl_raw_stream=False, dvl_web_stream=False, dvl_raw_http=False,
            dvl_snapshot_hz=0.0, dvl_status_s=10.0, dvl_vehicle_s=10.0,
            net_counters_s=1.0, net_ping_s=1.0,
            ugps_acoustic_s=1.0, ugps_raw=False,
            pi_health_s=15.0, video_streams_s=120.0,
            nav_params_s=600.0),
    Profile(INTENSIVE, "Intensive",
            "every byte from the DVL, 10 Hz network, raw UGPS — for a fault",
            dvl_raw_stream=True, dvl_web_stream=True, dvl_raw_http=True,
            dvl_snapshot_hz=None, dvl_status_s=None, dvl_vehicle_s=None,
            net_counters_s=0.1, net_ping_s=0.2,
            ugps_acoustic_s=0.25, ugps_raw=True,
            pi_health_s=5.0, video_streams_s=30.0,
            nav_params_s=120.0),
)}

#: Until someone chooses otherwise. The vehicle is working.
DEFAULT = STANDARD


def get(key: str | None) -> Profile:
    return PROFILES.get(str(key or "").lower(), PROFILES[DEFAULT])


def from_settings(settings: Mapping | None) -> Profile:
    return get((settings or {}).get("logging_profile"))
