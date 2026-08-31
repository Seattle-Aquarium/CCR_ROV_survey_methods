"""Fetch the CO-OPS tide-prediction station list and vendor a slim copy.

The full metadata API response is ~3,500 stations with a lot of fields we
never use. This keeps id / name / state / lat / lon / type (R = reference /
harmonic, supports sub-hourly predictions; S = subordinate, high/low only)
so the app can pick the nearest harmonic station offline.

Usage:  python build_station_index.py
Output: flightplan/data/tide_stations.json
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

URL = ("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/"
       "stations.json?type=tidepredictions")
OUT = Path(__file__).resolve().parent.parent / "flightplan" / "data" / "tide_stations.json"


def main() -> int:
    req = urllib.request.Request(URL, headers={"User-Agent": "CCR-ROV-flight-plan build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = json.load(r)

    slim = []
    for s in raw.get("stations", []):
        try:
            slim.append({
                "id": s["id"],
                "name": s["name"],
                "state": s.get("state") or "",
                "lat": round(float(s["lat"]), 5),
                "lon": round(float(s["lng"]), 5),
                "type": s.get("type") or "",          # "R" harmonic / "S" subordinate
                "ref": s.get("reference_id") or "",
            })
        except (KeyError, TypeError, ValueError):
            continue

    slim.sort(key=lambda d: d["id"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(slim, separators=(",", ":")), encoding="utf-8")
    n_r = sum(1 for d in slim if d["type"] == "R")
    print(f"wrote {OUT}  ({len(slim)} stations, {n_r} harmonic, "
          f"{OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
