"""
The Water Linked Underwater GPS G2 topside's web API, as this program reads it.

The G2 serves a documented REST API (``/swagger/swagger.json`` on the unit,
and on Water Linked's public demo at https://demo.waterlinked.com). Two of its
answers carry what the per-receiver logging is for: the acoustic position,
filtered and raw, each with **one value per receiver** for distance, received
signal strength (RSSI), noise spectral density (NSD) and a valid flag::

    {"position_valid": true, "x": -12.5, "y": -11.65, "z": 7.5, "std": 0.37,
     "receiver_distance": [20.03, 18.43, 15.52, 17.99],
     "receiver_rssi": [-45.0, -55.0, -55.0, -65.0],
     "receiver_nsd": [-101.7, -86.7, -61.7, -51.7],
     "receiver_valid": [1, 1, 1, 0]}

That shape was read off the demo unit on 15 October 2026. The G2's own web
page gets the same numbers over a websocket in a different shape -- a
``receivers`` list of ``{id, recv_distance, recv_rssi, recv_nsd_db,
recv_valid}`` and ``xPos``/``yPos``/``zPos`` -- which is what its diagnostic
logs (.wdat) record. Both are understood here, so a firmware that changes
from one to the other still logs.

Receivers are named by their index in those arrays, ``r0`` to ``r3``. The G2's
page may number them from 1.

**GETs only.** The API also accepts the vessel position, depth, IMU
calibration and configuration changes; none of those paths is used here.
"""

from __future__ import annotations

import json
import math

DEFAULT_HOST = "192.168.2.94"

ACOUSTIC_PATHS = {
    "filtered": "/api/v1/position/acoustic/filtered",
    "raw": "/api/v1/position/acoustic/raw",
}
POSITION_PATHS = {
    "global": "/api/v1/position/global",      # the locator, as latitude/longitude
    "master": "/api/v1/position/master",      # the topside (vessel) reference
}
STATUS_PATHS = {
    "status": "/api/v1/about/status",         # battery, gps and imu state
    "temperature": "/api/v1/about/temperature",
    "status_report": "/api/v1/status_report/",
}
SLOW_PATHS = (
    "/api/v1/about",                          # product, chip id, software version
    "/api/v1/config/generic",                 # gps/compass sources, channel, ...
    "/api/v1/config/receivers/",              # where each receiver is mounted
    "/api/v1/config/antenna",
)

#: Receivers given their own columns. A G2 antenna has four; more would be
#: kept whole in `extra_json` rather than dropped.
N_RECEIVERS = 4
RECEIVER_FIELDS = ("distance", "rssi", "nsd", "valid", "snr_db")

ACOUSTIC_COLUMNS = (
    "position_valid", "x", "y", "z", "std",
    *(f"r{i}_{f}" for i in range(N_RECEIVERS) for f in RECEIVER_FIELDS),
    "n_receivers", "extra_json",
)
POSITION_COLUMNS = ("lat", "lon", "orientation", "cog", "sog", "hdop",
                    "numsats", "fix_quality", "extra_json")
STATUS_COLUMNS = ("battery", "gps", "imu", "board_temp_c", "water_temp_c",
                  "status_groups", "groups_not_ok", "status_messages")


def num(value):
    """A finite number from the data, or None. Booleans become 1/0."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _extras(obj: dict, known) -> str:
    rest = {k: v for k, v in obj.items() if k not in known}
    return json.dumps(rest, separators=(",", ":"), sort_keys=True) if rest else ""


def receivers_of(obj: dict) -> list[dict]:
    """Each receiver as {distance, rssi, nsd, valid}, in index order."""
    if isinstance(obj.get("receivers"), list):              # websocket shape
        out = []
        for r in obj["receivers"]:
            if not isinstance(r, dict):
                continue
            out.append({"id": r.get("id"),
                        "distance": num(r.get("recv_distance")),
                        "rssi": num(r.get("recv_rssi")),
                        "nsd": num(r.get("recv_nsd_db", r.get("recv_nsd"))),
                        "valid": num(r.get("recv_valid"))})
        if all(isinstance(r["id"], int) for r in out):
            out.sort(key=lambda r: r["id"])
        return out
    arrays = {f: obj.get(f"receiver_{f}") for f in ("distance", "rssi", "nsd", "valid")}
    n = max((len(a) for a in arrays.values() if isinstance(a, list)), default=0)
    out = []
    for i in range(n):
        r = {"id": i}
        for f, a in arrays.items():
            r[f] = num(a[i]) if isinstance(a, list) and i < len(a) else None
        out.append(r)
    return out


_ACOUSTIC_KNOWN = {"position_valid", "x", "y", "z", "std", "xPos", "yPos", "zPos",
                   "pos", "receivers", "receiver_distance", "receiver_rssi",
                   "receiver_nsd", "receiver_valid"}


def parse_acoustic(obj) -> dict:
    """One acoustic answer as a row: {column: value}. Unknown fields kept whole."""
    if not isinstance(obj, dict):
        return {"extra_json": json.dumps(obj)}
    row = {
        "position_valid": num(obj.get("position_valid")),
        "x": num(obj.get("x", obj.get("xPos"))),
        "y": num(obj.get("y", obj.get("yPos"))),
        "z": num(obj.get("z", obj.get("zPos"))),
        "std": num(obj.get("std")),
    }
    recs = receivers_of(obj)
    for i in range(N_RECEIVERS):
        r = recs[i] if i < len(recs) else {}
        for f in ("distance", "rssi", "nsd", "valid"):
            row[f"r{i}_{f}"] = r.get(f)
        rssi, nsd = r.get("rssi"), r.get("nsd")
        row[f"r{i}_snr_db"] = (rssi - nsd) if rssi is not None and nsd is not None else None
    row["n_receivers"] = len(recs)
    extra = json.loads(_extras(obj, _ACOUSTIC_KNOWN) or "{}")
    if len(recs) > N_RECEIVERS:
        extra["receivers_beyond_columns"] = recs[N_RECEIVERS:]
    row["extra_json"] = json.dumps(extra, separators=(",", ":"), sort_keys=True) if extra else ""
    return row


def parse_position(obj) -> dict:
    if not isinstance(obj, dict):
        return {"extra_json": json.dumps(obj)}
    row = {c: num(obj.get(c)) for c in POSITION_COLUMNS if c != "extra_json"}
    row["extra_json"] = _extras(obj, POSITION_COLUMNS)
    return row


def parse_status(status, temperature, report) -> dict:
    """The three status answers as one row. Any of them may be None."""
    status = status if isinstance(status, dict) else {}
    temperature = temperature if isinstance(temperature, dict) else {}
    groups = report if isinstance(report, list) else []
    states, not_ok, messages = [], [], []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("status_group") or g.get("status_group_id") or "?")
        state = str(g.get("status") or "?")
        states.append(f"{name}={state}")
        if state != "ok":
            not_ok.append(name)
        msg = (g.get("message") or "").strip()
        ids = g.get("error_ids") or []
        if msg or ids:
            messages.append(f"{name}: {msg}" + (f" [{', '.join(map(str, ids))}]" if ids else ""))
    return {
        "battery": num(status.get("battery")),
        "gps": num(status.get("gps")),
        "imu": num(status.get("imu")),
        "board_temp_c": num(temperature.get("board")),
        "water_temp_c": num(temperature.get("water")),
        "status_groups": "; ".join(states),
        "groups_not_ok": "; ".join(not_ok),
        "status_messages": " | ".join(messages),
    }
