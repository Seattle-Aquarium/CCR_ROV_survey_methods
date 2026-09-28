"""
Build the bundled Pier 59 starter map. Run once; the result is committed.

Why this script is in the repository rather than just its output: the pack is a
redistributable asset and somebody, some day, has to be able to answer "where
did these pixels come from and are we allowed to ship them?". Running this
again reproduces the pack from named public sources and rewrites the manifest
that records the answer.

**Two layers, two sources, both redistributable.**

``pier59_imagery``  USGS National Map aerial imagery. A work of the United
                    States Government, so not subject to domestic copyright
                    (17 U.S.C. section 105) and free to redistribute. It is
                    real imagery of the real pier, which is what makes the
                    first launch worth anything. Its cache stops at **zoom
                    16** here -- 1.6 m per pixel -- which is too coarse to
                    draw a 30 m transect against, hence the second layer.

``pier59_chart``    Rendered here from OpenStreetMap data fetched through
                    Overpass. OSM data is ODbL 1.0; a rendering of it is a
                    "Produced Work" under that license and may be distributed
                    provided the data is attributed, which the manifest and
                    the map's own attribution line both do. This gives
                    coastline, the piers themselves, the seawall, buildings
                    and Alaskan Way at zooms 17-19, where the operator is
                    actually laying out a survey.

**What is deliberately not used.** `tile.openstreetmap.org` forbids bulk
downloading in its tile usage policy, so it is never fetched here -- the OSM
*data* is fetched from Overpass and rendered locally instead, which is a
different thing and is permitted. Esri's basemaps are not redistributable and
are runtime-only. NOAA's chart tile service is public domain and would be the
ideal nautical source, but it was unreachable from the machine this was built
on; `tiles.py` still offers it as a live layer.

Output is MBTiles: one SQLite file per layer. SQLite is in the standard
library, MBTiles is a published specification other tools can read, and one
tracked binary per layer is easier to check and to check *in* than a few
hundred loose PNGs.

    python assets/maps/build_pier59.py [--out assets/maps/pier59]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# The launch point, as the operator supplied it. Decimal degrees, WGS-84.
LAT, LON = 47.6075661, -122.3438752
SITE = "Seattle Aquarium — Pier 59 / OTS"

#: "200 m around the launch point", taken as a 400 x 400 m square. Tiles are
#: included whenever they touch it, so the real coverage is a little wider.
RADIUS_M = 200.0

IMAGERY_ZOOMS = range(13, 17)      # what USGS actually has cached here
CHART_ZOOMS = range(14, 20)        # rendered, so we choose

USGS = ("https://basemap.nationalmap.gov/arcgis/rest/services/"
        "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}")
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "rov_flight_ops/0.1 (Seattle Aquarium, Coastal Climate "
                    "Resilience; https://github.com/Seattle-Aquarium/"
                    "CCR_ROV_survey_methods)"}

TILE_PX = 256

ATTRIBUTION = {
    "pier59_imagery": "USGS The National Map — public domain",
    "pier59_chart": "© OpenStreetMap contributors (ODbL), rendered locally",
}
LICENSE = {
    "pier59_imagery": (
        "Public domain. A work of the U.S. Government under 17 U.S.C. 105; "
        "USGS National Map products carry no copyright restriction."),
    "pier59_chart": (
        "Rendered from OpenStreetMap data, © OpenStreetMap contributors, "
        "licensed ODbL 1.0 (https://opendatacommons.org/licenses/odbl/). "
        "This rendering is a Produced Work and is distributed with the "
        "required attribution."),
}


# --------------------------------------------------------------------------
#  tiles and geography
# --------------------------------------------------------------------------


def deg_to_tile(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile_to_deg(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def bounds() -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) of the square around the launch."""
    dlat = RADIUS_M / 111_320.0
    dlon = RADIUS_M / (111_320.0 * math.cos(math.radians(LAT)))
    return LON - dlon, LAT - dlat, LON + dlon, LAT + dlat


def tiles_for(z: int):
    w, s, e, n = bounds()
    x0, y0 = deg_to_tile(n, w, z)
    x1, y1 = deg_to_tile(s, e, z)
    for x in range(int(x0), int(x1) + 1):
        for y in range(int(y0), int(y1) + 1):
            yield x, y


def fetch(url: str, tries: int = 5) -> bytes | None:
    """GET with retries. The network this was built on reset connections
    often enough that one attempt was not enough to finish a pack."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=30) as f:
                return f.read(8_000_000)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None                 # genuinely not in the cache
            time.sleep(1.0 + i)
        except Exception:
            time.sleep(1.0 + i)
    return None


# --------------------------------------------------------------------------
#  MBTiles
# --------------------------------------------------------------------------


def open_mbtiles(path: Path, name: str, fmt: str, zooms) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE metadata (name text, value text);"
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, "
        "tile_row integer, tile_data blob);"
        "CREATE UNIQUE INDEX tile_index ON tiles "
        "(zoom_level, tile_column, tile_row);")
    w, s, e, n = bounds()
    for k, v in (("name", name), ("format", fmt), ("type", "baselayer"),
                 ("version", "1"), ("minzoom", str(min(zooms))),
                 ("maxzoom", str(max(zooms))),
                 ("bounds", f"{w:.7f},{s:.7f},{e:.7f},{n:.7f}"),
                 ("center", f"{LON:.7f},{LAT:.7f},{max(zooms)}"),
                 ("attribution", ATTRIBUTION[name]),
                 ("description", f"{SITE} starter map. {LICENSE[name]}")):
        db.execute("INSERT INTO metadata VALUES (?,?)", (k, v))
    return db


def put(db: sqlite3.Connection, z: int, x: int, y: int, data: bytes) -> None:
    """Store one tile. MBTiles rows are TMS -- y counted from the south --
    so the XYZ y is flipped here and flipped back by the reader."""
    db.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
               (z, x, (2 ** z - 1) - y, data))


# --------------------------------------------------------------------------
#  the imagery layer
# --------------------------------------------------------------------------


def build_imagery(out: Path) -> dict:
    db = open_mbtiles(out / "pier59_imagery.mbtiles", "pier59_imagery",
                      "jpg", IMAGERY_ZOOMS)
    got = missing = 0
    for z in IMAGERY_ZOOMS:
        for x, y in tiles_for(z):
            data = fetch(USGS.format(z=z, x=x, y=y))
            if data and data[:2] == b"\xff\xd8":
                put(db, z, x, y, data)
                got += 1
            else:
                missing += 1
            time.sleep(0.1)                  # be a polite client
        print(f"   imagery z{z}: {got} tiles so far")
    db.commit()
    db.close()
    return {"tiles": got, "missing": missing,
            "zooms": [min(IMAGERY_ZOOMS), max(IMAGERY_ZOOMS)]}


# --------------------------------------------------------------------------
#  the chart layer, rendered from OSM data
# --------------------------------------------------------------------------

#: Muted, so drawn plans and tracks stay the brightest things on the map.
IN_WATER = (96, 132, 163)
LAND = (232, 229, 222)
PIER = (206, 198, 184)
BUILDING = (196, 190, 180)
ROAD = (250, 248, 244)
WALL = (150, 144, 136)


def overpass_data(cache: Path) -> dict:
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    w, s, e, n = bounds()
    pad = 0.0012                      # a little beyond the pack, for clipping
    q = (f"[out:json][timeout:90];"
         f"(way({s - pad},{w - pad},{n + pad},{e + pad});"
         f"relation({s - pad},{w - pad},{n + pad},{e + pad}););out geom;")
    req = urllib.request.Request(
        OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
        headers=UA)
    with urllib.request.urlopen(req, timeout=180) as f:
        raw = f.read()
    cache.write_bytes(raw)
    return json.loads(raw)


def _ways(data: dict):
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom:
            continue
        yield el.get("tags", {}) or {}, [(p["lat"], p["lon"]) for p in geom]


def render_chart_tile(data: dict, z: int, x: int, y: int):
    """One tile, drawn from the OSM ways that fall in it.

    Water first, then land where the coastline says so, then piers, buildings
    and roads. Deliberately plain: this is the ground a survey plan is drawn
    on, not a map to look at for its own sake.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (TILE_PX, TILE_PX), IN_WATER)
    d = ImageDraw.Draw(img)
    n = 2 ** z

    def px(lat: float, lon: float) -> tuple[float, float]:
        tx, ty = deg_to_tile(lat, lon, z)
        return ((tx - x) * TILE_PX, (ty - y) * TILE_PX)

    # Land. OSM coastline ways have the sea on their left, so a filled
    # polygon of the way plus the tile corners is not reliable in general --
    # but over one small harbor tile, filling the landward side of each
    # coastline way against the tile edge is good enough to show where the
    # water stops, which is the only thing this layer has to get right.
    for tags, pts in _ways(data):
        if tags.get("natural") == "coastline" and len(pts) > 1:
            poly = [px(a, b) for a, b in pts]
            # Extend to the east edge: at Pier 59 the land is inshore (east).
            poly = poly + [(TILE_PX + 40, poly[-1][1]),
                           (TILE_PX + 40, poly[0][1])]
            d.polygon(poly, fill=LAND)

    for tags, pts in _ways(data):
        poly = [px(a, b) for a, b in pts]
        if len(poly) < 2:
            continue
        if tags.get("man_made") == "pier":
            if len(poly) > 2:
                d.polygon(poly, fill=PIER, outline=WALL)
            else:
                d.line(poly, fill=PIER, width=max(2, z - 13))
        elif "building" in tags and len(poly) > 2:
            d.polygon(poly, fill=BUILDING, outline=WALL)
        elif tags.get("barrier") in ("wall", "retaining_wall"):
            d.line(poly, fill=WALL, width=2)
        elif tags.get("highway") in ("primary", "secondary", "tertiary",
                                     "residential", "service", "pedestrian"):
            wide = {"primary": 6, "secondary": 5}.get(tags["highway"], 3)
            d.line(poly, fill=ROAD, width=max(1, wide * (z - 15))
                   if z > 15 else 1)
    return img


def build_chart(out: Path, work: Path) -> dict:
    data = overpass_data(work / "osm_pier59.json")
    db = open_mbtiles(out / "pier59_chart.mbtiles", "pier59_chart",
                      "png", CHART_ZOOMS)
    got = 0
    for z in CHART_ZOOMS:
        for x, y in tiles_for(z):
            img = render_chart_tile(data, z, x, y)
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            put(db, z, x, y, buf.getvalue())
            got += 1
        print(f"   chart z{z}: {got} tiles so far")
    db.commit()
    db.close()
    return {"tiles": got, "zooms": [min(CHART_ZOOMS), max(CHART_ZOOMS)],
            "osm_elements": len(data.get("elements", []))}


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(Path(__file__).parent / "pier59"))
    ap.add_argument("--skip-imagery", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = out.parent / "_work"
    work.mkdir(exist_ok=True)

    w, s, e, n = bounds()
    manifest = {
        "site": SITE,
        "center": {"lat": LAT, "lon": LON},
        "radius_m": RADIUS_M,
        "bounds": {"west": round(w, 7), "south": round(s, 7),
                   "east": round(e, 7), "north": round(n, 7)},
        "built": date.today().isoformat(),
        "built_by": "assets/maps/build_pier59.py",
        "layers": {},
    }

    if not args.skip_imagery:
        print("USGS National Map imagery…")
        info = build_imagery(out)
        info.update(source="USGS The National Map (USGSImageryOnly)",
                    url=USGS, license=LICENSE["pier59_imagery"],
                    attribution=ATTRIBUTION["pier59_imagery"], format="jpg")
        manifest["layers"]["pier59_imagery"] = info

    print("OpenStreetMap-derived chart…")
    info = build_chart(out, work)
    info.update(source="OpenStreetMap via Overpass API, rendered locally",
                url=OVERPASS, license=LICENSE["pier59_chart"],
                attribution=ATTRIBUTION["pier59_chart"], format="png")
    manifest["layers"]["pier59_chart"] = info

    total = 0
    for name in list(manifest["layers"]):
        p = out / f"{name}.mbtiles"
        if not p.is_file():
            continue
        raw = p.read_bytes()
        total += len(raw)
        manifest["layers"][name]["bytes"] = len(raw)
        manifest["layers"][name]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest["total_bytes"] = total
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1),
                                       encoding="utf-8")
    print(f"\n{total / 2**20:.2f} MiB total in {out}")
    for name, info in manifest["layers"].items():
        print(f"   {name}: {info.get('tiles', 0)} tiles, "
              f"{info.get('bytes', 0) / 1024:.0f} KiB, "
              f"z{info['zooms'][0]}-{info['zooms'][1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
