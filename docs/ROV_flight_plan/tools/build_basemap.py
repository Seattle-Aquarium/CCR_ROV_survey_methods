"""Clip Natural Earth land polygons to the US West Coast and write a compact
GeoJSON the wind-map figure can draw with nothing but the standard library.

Source files (download once, they are large and not vendored):

    https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson
    https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_minor_islands.geojson

Usage:

    python build_basemap.py ne_10m_land.geojson ne_10m_minor_islands.geojson

Output: flightplan/figures/assets/land_westcoast.geojson  (MultiPolygon, one feature)

The clip is Sutherland-Hodgman against the bounding rectangle, so rings that
leave the box are cut cleanly at the edge rather than dropped or left whole
(the Natural Earth mainland polygon is otherwise most of a continent).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# West Coast window: a little past the CA/Mexico line up to the Salish Sea,
# and far enough offshore to hold the outer-coast survey sites.
WEST, EAST = -130.0, -116.0
SOUTH, NORTH = 30.0, 50.0

OUT = (Path(__file__).resolve().parent.parent
       / "flightplan" / "figures" / "assets" / "land_westcoast.geojson")


def _clip_edge(pts, inside, intersect):
    out = []
    n = len(pts)
    for i in range(n):
        cur = pts[i]
        prv = pts[i - 1]
        cur_in = inside(cur)
        prv_in = inside(prv)
        if cur_in:
            if not prv_in:
                out.append(intersect(prv, cur))
            out.append(cur)
        elif prv_in:
            out.append(intersect(prv, cur))
    return out


def clip_ring(ring):
    """Sutherland-Hodgman clip of one ring to the WEST/EAST/SOUTH/NORTH box."""
    def isect(a, b, get, val):
        (ax, ay), (bx, by) = a, b
        t = (val - get(a)) / (get(b) - get(a))
        return (ax + t * (bx - ax), ay + t * (by - ay))

    pts = ring
    pts = _clip_edge(pts, lambda p: p[0] >= WEST,
                     lambda a, b: isect(a, b, lambda p: p[0], WEST))
    if not pts:
        return []
    pts = _clip_edge(pts, lambda p: p[0] <= EAST,
                     lambda a, b: isect(a, b, lambda p: p[0], EAST))
    if not pts:
        return []
    pts = _clip_edge(pts, lambda p: p[1] >= SOUTH,
                     lambda a, b: isect(a, b, lambda p: p[1], SOUTH))
    if not pts:
        return []
    pts = _clip_edge(pts, lambda p: p[1] <= NORTH,
                     lambda a, b: isect(a, b, lambda p: p[1], NORTH))
    return [[round(x, 5), round(y, 5)] for x, y in pts]


def iter_polygons(geom):
    t = geom["type"]
    if t == "Polygon":
        yield geom["coordinates"]
    elif t == "MultiPolygon":
        yield from geom["coordinates"]


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    polys_out = []
    for path in argv:
        gj = json.loads(Path(path).read_text(encoding="utf-8"))
        feats = gj["features"] if gj["type"] == "FeatureCollection" else [gj]
        for feat in feats:
            for poly in iter_polygons(feat["geometry"]):
                rings_out = []
                for k, ring in enumerate(poly):
                    clipped = clip_ring(ring)
                    # need >=4 points for a closed ring; drop slivers
                    if len(clipped) >= 4:
                        if clipped[0] != clipped[-1]:
                            clipped.append(clipped[0])
                        rings_out.append(clipped)
                    elif k == 0:
                        break  # outer ring gone -> polygon not in window
                if rings_out:
                    polys_out.append(rings_out)

    out = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "land_westcoast",
                           "bbox": [WEST, SOUTH, EAST, NORTH],
                           "source": "Natural Earth 10m (public domain)"},
            "geometry": {"type": "MultiPolygon", "coordinates": polys_out},
        }],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({len(polys_out)} polygons, {kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
