"""Fetch the NDBC station table and vendor a slim West Coast subset.

NDBC publishes a pipe-delimited station table; the location field looks like
"48.493 N 124.727 W (48&#176;29'34" N 124&#176;43'37" W)". We keep buoys and
fixed platforms in the WA/OR/CA window with a usable lat/lon.

Usage:  python build_buoy_index.py
Output: flightplan/data/ndbc_stations.json
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

URL = "https://www.ndbc.noaa.gov/data/stations/station_table.txt"
OUT = Path(__file__).resolve().parent.parent / "flightplan" / "data" / "ndbc_stations.json"

WEST, EAST, SOUTH, NORTH = -130.0, -116.0, 30.0, 50.0
_LOC = re.compile(r"([-\d.]+)\s*([NS])\s+([-\d.]+)\s*([EW])")


def parse_location(text: str):
    m = _LOC.search(text)
    if not m:
        return None
    lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
    lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
    return lat, lon


def main() -> int:
    req = urllib.request.Request(URL, headers={"User-Agent": "CCR-ROV-flight-plan build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")

    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 7:
            continue
        sid, owner, ttype, hull, name, payload, location = parts[:7]
        loc = parse_location(location)
        if not loc:
            continue
        lat, lon = loc
        if not (SOUTH <= lat <= NORTH and WEST <= lon <= EAST):
            continue
        out.append({
            "id": sid.lower(),
            "name": name,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "type": ttype,
        })

    out.sort(key=lambda d: d["id"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT}  ({len(out)} stations, {OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
