"""
The bundled Pier 59 pack, over-zoom, and offline readiness.

These cover the promise the Navigation chapter makes when it opens on a pier
with no network: that there is a map, that it says honestly what it is showing,
and that nothing the application does to its cache can take it away.
"""

from __future__ import annotations

import sqlite3
import tempfile

import pytest

from rov_flight_ops.nav import bundled as B
from rov_flight_ops.nav import geo
from rov_flight_ops.nav import offline as O
from rov_flight_ops.nav import tiles as TL

#: Pier 59, the default site. WGS84.
LAT, LON = 47.6075661, -122.3438752


@pytest.fixture
def cache():
    c = TL.TileCache(root=tempfile.mkdtemp(prefix="tiletest_"), online=False)
    yield c
    try:
        c.stop()
    except Exception:
        pass


def _tile_at(key: str, cache, lat: float, lon: float, z: int):
    x, y = geo.latlon_to_tile_xy(lat, lon, z)
    return cache.get(key, z, int(x), int(y))


# --------------------------------------------------------------------------
#  The pack itself
# --------------------------------------------------------------------------


def test_the_bundled_pack_is_present_and_readable():
    """It is committed to the repository, so a laptop that has only ever
    pulled from Git has a map."""
    maps = B.BundledMaps()
    assert maps.any_available, maps.problem or maps.summary()
    for key in ("pier59_chart", "pier59_imagery"):
        layer = maps.layers[key]
        assert layer.available, layer.note
        assert layer.bounds is not None
        assert layer.covers(LAT, LON), f"{key} does not cover Pier 59"
    maps.close()


def test_the_pack_lives_outside_the_runtime_cache():
    """The strongest form of "clearing the cache must not remove it": the two
    are not in the same tree, so no sweep of the cache can reach the pack
    however it is written."""
    cache_root = TL.TileCache(root=tempfile.mkdtemp()).root.resolve()
    pack = B.PACK_DIR.resolve()
    assert not str(pack).startswith(str(cache_root))


def test_a_bundled_tile_is_never_queued_for_fetching(cache):
    """Offline means offline. A missing bundled tile is missing from the
    pack, and asking the network for it would be both futile and a surprise
    to an operator who chose an offline layer."""
    cache.online = True
    try:
        # Far outside the 200 m pack, at a zoom the pack carries.
        x, y = geo.latlon_to_tile_xy(0.0, 0.0, 17)
        assert cache.get("pier59_chart", 17, int(x), int(y)) is None
        assert not cache._queue, cache._queue
    finally:
        cache.online = False


def test_coverage_gives_four_distinct_answers():
    """"No map" tells the operator nothing about what to do next; each of
    these calls for something different."""
    maps = B.BundledMaps()
    if not maps.any_available:
        pytest.skip(maps.problem or "no pack")
    try:
        chart = maps.layers["pier59_chart"]
        assert maps.coverage("pier59_chart", LAT, LON, 17) == "bundled"
        assert "outside" in maps.coverage("pier59_chart", 0.0, 0.0, 17)
        assert "beyond" in maps.coverage("pier59_chart", LAT, LON,
                                         chart.max_zoom + 1)
        assert "below" in maps.coverage("pier59_chart", LAT, LON,
                                        chart.min_zoom - 1)
    finally:
        maps.close()


# --------------------------------------------------------------------------
#  Over-zoom
# --------------------------------------------------------------------------


def test_beyond_the_pack_the_view_really_is_enlarged(cache):
    """`coverage` says "enlarged, not sharper" past a layer's top zoom. This
    is what makes that sentence true instead of a caption on a blank grid."""
    src = TL.SOURCES["pier59_imagery"]
    assert _tile_at("pier59_imagery", cache, LAT, LON, src.max_zoom) is not None
    for z in range(src.max_zoom + 1, src.max_zoom + TL.MAX_OVERZOOM + 1):
        im = _tile_at("pier59_imagery", cache, LAT, LON, z)
        assert im is not None, f"nothing at z{z}"
        assert im.size == (TL.TILE_PX, TL.TILE_PX)


def test_enlargement_stops_before_it_becomes_a_colour_wash(cache):
    """Eight-fold is still recognisably the same shoreline. Past that a tile
    is a handful of coloured squares, and an honest blank grid beats a
    confident-looking smear."""
    src = TL.SOURCES["pier59_imagery"]
    too_far = src.max_zoom + TL.MAX_OVERZOOM + 1
    assert _tile_at("pier59_imagery", cache, LAT, LON, too_far) is None


def test_the_children_of_a_tile_reassemble_into_their_parent(cache):
    """The part that a size check cannot catch: that each enlarged tile is
    the *right* quarter of its ancestor. Four children put back together,
    each shrunk by half, must reproduce the parent exactly -- nearest
    neighbour up and back down is lossless."""
    from PIL import Image

    src = TL.SOURCES["pier59_imagery"]
    z = src.max_zoom
    px, py = (int(v) for v in geo.latlon_to_tile_xy(LAT, LON, z))
    parent = cache.get("pier59_imagery", z, px, py)
    assert parent is not None

    half = TL.TILE_PX // 2
    rebuilt = Image.new("RGBA", (TL.TILE_PX, TL.TILE_PX))
    for dx in (0, 1):
        for dy in (0, 1):
            child = cache.get("pier59_imagery", z + 1, px * 2 + dx,
                              py * 2 + dy)
            assert child is not None, (dx, dy)
            rebuilt.paste(child.resize((half, half), Image.NEAREST),
                          (dx * half, dy * half))
    assert rebuilt.tobytes() == parent.tobytes()


def test_a_zoom_below_the_pack_is_not_invented(cache):
    """Enlarging works one way only. Zoomed out past the pack there is no
    ancestor to stretch, and making one up would mean drawing a coastline
    from data that is not there."""
    src = TL.SOURCES["pier59_chart"]
    assert _tile_at("pier59_chart", cache, LAT, LON, src.min_zoom - 1) is None


# --------------------------------------------------------------------------
#  Offline readiness
# --------------------------------------------------------------------------


def test_a_bundled_layer_reports_ready_without_any_preparation(cache):
    """The point of committing the pack: an operator who has just cloned the
    repository does not have to prepare anything before leaving the dock."""
    r = O.readiness(cache, "pier59_chart", LAT, LON, 200.0, O.SURVEY_ZOOMS)
    assert r.ready, r.line()
    assert r.bundled and r.missing == 0
    assert r.have == r.want > 0


def test_an_unprepared_online_layer_is_honest_about_being_empty(cache):
    """Never let a blank cache read as ready."""
    key = next(k for k, s in TL.SOURCES.items()
               if not TL.is_bundled(k) and not s.overlay)
    r = O.readiness(cache, key, LAT, LON, 200.0, (16, 17))
    assert not r.ready
    assert r.have == 0 and r.want > 0


def test_preparing_a_bundled_layer_is_refused(cache):
    """There is nothing to fetch and the source would not serve it. Better to
    say so than to run a progress bar that can only reach zero."""
    job = O.OfflinePrepare(cache)
    with pytest.raises(ValueError):
        job.start("pier59_chart", LAT, LON, 200.0, O.SURVEY_ZOOMS)


def test_the_tile_estimate_rises_with_zoom_and_with_radius(cache):
    """A sanity check on the numbers the Offline dialog shows before it spends
    an operator's tethering allowance.

    Not a fourfold rule: over 200 m the tile grid is three across at z17 and
    five at z18, because a span that does not line up with the tile boundaries
    is dominated by its rounding, not by the area.
    """
    del cache
    counts = [O.count_tiles(LAT, LON, 200.0, (z,)) for z in O.SURVEY_ZOOMS]
    assert all(b >= a for a, b in zip(counts, counts[1:], strict=False)), counts
    assert counts[-1] > counts[0]
    wide = O.count_tiles(LAT, LON, 600.0, (18,))
    assert wide > O.count_tiles(LAT, LON, 200.0, (18,))
    assert O.estimate_bytes(wide) > O.estimate_bytes(counts[0])


# --------------------------------------------------------------------------
#  The manifest
# --------------------------------------------------------------------------


def test_the_manifest_records_where_every_pixel_came_from():
    """A redistributable pack has to be able to say what it is: a licence
    that permits the redistribution, and a checksum that proves the file in
    Git is the file that was built."""
    import hashlib
    import json

    path = B.PACK_DIR / "manifest.json"
    assert path.is_file(), "the pack has no manifest"
    man = json.loads(path.read_text(encoding="utf-8"))
    for field in ("site", "centre", "radius_m", "bounds", "built", "built_by"):
        assert man.get(field), f"the manifest has no {field}"

    assert set(man["layers"]) == {"pier59_chart", "pier59_imagery"}
    for key, layer in man["layers"].items():
        for field in ("source", "url", "licence", "attribution", "zooms",
                      "tiles", "bytes", "sha256"):
            assert layer.get(field), f"{key} has no {field}"
        f = B.PACK_DIR / f"{key}.mbtiles"
        assert f.is_file(), f
        assert f.stat().st_size == layer["bytes"]
        assert hashlib.sha256(f.read_bytes()).hexdigest() == layer["sha256"], (
            f"{f.name} is not the file the manifest describes")
        # Neither layer may be built by prefetching the standard OSM tile
        # service, whose terms do not allow it.
        assert "tile.openstreetmap.org" not in layer["url"]
        assert "tile.openstreetmap.org" not in layer["source"]


def test_the_packs_are_mbtiles_and_open_read_only():
    """Opened with `immutable=1`, so a pack on a read-only checkout or a
    synchronised drive cannot be journalled into a modified state."""
    for key in ("pier59_chart", "pier59_imagery"):
        layer = B.BundledMaps().layers[key]
        if not layer.available:
            pytest.skip(layer.note or "pack not available")
        with sqlite3.connect(f"file:{layer.path.as_posix()}?immutable=1",
                             uri=True) as db:
            (n,) = db.execute("SELECT count(*) FROM tiles").fetchone()
        assert n > 0, key
