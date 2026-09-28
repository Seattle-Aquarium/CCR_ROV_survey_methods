"""
Preparing a site for a day with no signal, and saying whether it is ready.

The operator plans at the dock, where there is a connection, and flies at a
pier where there may not be. So there are two jobs here: fetch a bounded
extent while the network exists, and answer "is this area usable offline?"
*before* leaving — which has to be answerable without asking the network
anything, or it answers itself too late.

**This is not a bulk downloader.** It walks one bounded extent at the tile
cache's own polite interval, one tile at a time, skipping whatever is already
held. No parallelism, no recursion over a region, no speculative pyramid.
That is what keeps it within the terms of the services involved; a layer that
ships with the program is refused outright, because it is already on the disk
and is not ours to re-fetch.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass

from . import geo
from .tiles import MIN_INTERVAL_S, SOURCES, TileCache, is_bundled

log = logging.getLogger(__name__)

#: Zooms a survey site is worth having. 14 frames the approach, 19 is close
#: enough to place a 30 m transect against a pier.
SURVEY_ZOOMS = (14, 15, 16, 17, 18, 19)

#: Measured from this program's own cache: raster basemap tiles run 8-30 KiB.
#: Used only for a labeled estimate shown before a download starts.
TYPICAL_TILE_BYTES = 20 * 1024


def tiles_in_radius(lat: float, lon: float, radius_m: float, zoom: int):
    """Every tile touching a square of `radius_m` either side of a point."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.05, math.cos(math.radians(lat))))
    x0, y0 = geo.latlon_to_tile_xy(lat + dlat, lon - dlon, zoom)
    x1, y1 = geo.latlon_to_tile_xy(lat - dlat, lon + dlon, zoom)
    n = 1 << zoom
    for x in range(int(x0), int(x1) + 1):
        for y in range(max(0, int(y0)), min(n - 1, int(y1)) + 1):
            yield x % n, y


def count_tiles(lat: float, lon: float, radius_m: float, zooms) -> int:
    return sum(1 for z in zooms for _ in tiles_in_radius(lat, lon, radius_m, z))


def estimate_bytes(count: int) -> int:
    return count * TYPICAL_TILE_BYTES


@dataclass
class PrepareJob:
    """One preparation run, as it happens.

    Read by the window on its own timer; nothing here touches a widget.
    """

    key: str
    lat: float
    lon: float
    radius_m: float
    zooms: tuple = ()
    total: int = 0
    done: int = 0
    fetched: int = 0
    already: int = 0
    failed: int = 0
    bytes: int = 0
    canceled: bool = False
    finished: bool = False
    error: str = ""
    started: float = 0.0

    @property
    def fraction(self) -> float:
        return 0.0 if not self.total else min(1.0, self.done / self.total)

    def line(self) -> str:
        label = SOURCES[self.key].label if self.key in SOURCES else self.key
        if self.error:
            return f"{label}: failed — {self.error}"
        if self.canceled:
            return (f"{label}: canceled after {self.done:,} of "
                    f"{self.total:,} tiles ({self.fetched:,} downloaded, "
                    f"{self.bytes / 2 ** 20:.1f} MiB kept)")
        if self.finished:
            bits = [f"{self.fetched:,} downloaded",
                    f"{self.already:,} already held"]
            if self.failed:
                bits.append(f"{self.failed:,} unavailable")
            return (f"{label}: " + ", ".join(bits)
                    + f" · {self.bytes / 2 ** 20:.1f} MiB")
        return (f"{label}: {self.done:,} of {self.total:,} tiles · "
                f"{self.bytes / 2 ** 20:.1f} MiB")


class OfflinePrepare:
    """Runs one `PrepareJob` at a time, off the window's thread."""

    def __init__(self, cache: TileCache) -> None:
        self.cache = cache
        self.job: PrepareJob | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, key: str, lat: float, lon: float, radius_m: float,
              zooms=SURVEY_ZOOMS) -> PrepareJob:
        """Begin. Raises with a reason rather than starting something useless."""
        if self.running:
            raise RuntimeError("a map is already being prepared")
        src = SOURCES.get(key)
        if src is None:
            raise ValueError(f"{key} is not a basemap layer")
        if is_bundled(key):
            raise ValueError(
                f"{src.label} ships with the program and is already offline — "
                f"there is nothing to download.")
        usable = tuple(z for z in zooms if src.min_zoom <= z <= src.max_zoom)
        if not usable:
            raise ValueError(
                f"{src.label} has no tiles in the chosen zooms; it covers "
                f"z{src.min_zoom}–{src.max_zoom}.")
        job = PrepareJob(key=key, lat=lat, lon=lon, radius_m=radius_m,
                         zooms=usable)
        job.total = count_tiles(lat, lon, radius_m, usable)
        self.job = job
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(job,),
                                        name="tile-prepare", daemon=True)
        self._thread.start()
        log.info("preparing %s offline: %d tiles, z%s, %.0f m around "
                 "%.5f, %.5f", key, job.total,
                 "/".join(str(z) for z in usable), radius_m, lat, lon)
        return job

    def cancel(self) -> None:
        """Stop between tiles. Whatever has been fetched stays cached."""
        self._stop.set()

    def _run(self, job: PrepareJob) -> None:
        try:
            for z in job.zooms:
                for x, y in tiles_in_radius(job.lat, job.lon, job.radius_m, z):
                    if self._stop.is_set():
                        job.canceled = True
                        return
                    path = self.cache.path_for(job.key, z, x, y)
                    try:
                        if path.is_file() and path.stat().st_size:
                            job.already += 1
                            job.bytes += path.stat().st_size
                            job.done += 1
                            continue
                    except OSError:
                        pass
                    before = self.cache.fetched
                    self.cache._fetch((job.key, z, x, y))
                    try:
                        if self.cache.fetched > before and path.is_file():
                            job.fetched += 1
                            job.bytes += path.stat().st_size
                        else:
                            job.failed += 1
                    except OSError:
                        job.failed += 1
                    job.done += 1
                    self._stop.wait(MIN_INTERVAL_S)
        except Exception as ex:
            job.error = str(ex)
            log.warning("offline prepare failed: %s", ex)
        finally:
            job.finished = True
            log.info("offline prepare finished: %s", job.line())


@dataclass
class Readiness:
    """Whether one layer can be used at one place with no network."""

    key: str
    label: str
    bundled: bool
    ready: bool
    have: int
    want: int
    note: str
    missing: int = 0

    def line(self) -> str:
        if self.bundled:
            return f"{self.label}: {self.note}"
        if self.ready:
            return f"{self.label}: prepared ({self.have:,} tiles)"
        return (f"{self.label}: {self.missing:,} of {self.want:,} tiles "
                f"missing — {self.note}")


def readiness(cache: TileCache, key: str, lat: float, lon: float,
              radius_m: float, zooms=SURVEY_ZOOMS) -> Readiness:
    """Can this layer be used here with no network? Answered from the disk.

    Nothing is asked of the network, deliberately: the question is worth
    asking *while* there is still a connection to act on the answer.
    """
    src = SOURCES.get(key)
    if src is None:
        return Readiness(key, key, False, False, 0, 0,
                         "not a basemap layer this program knows")
    if is_bundled(key):
        layer = cache.bundled.layers.get(key)
        inside = layer is not None and layer.covers(lat, lon)
        ok = bool(layer and layer.available and inside)
        return Readiness(
            key, src.label, True, ok,
            layer.tiles if layer else 0, layer.tiles if layer else 0,
            "ships with the program — always available" if ok else
            ("ships with the program, but this point is outside its extent"
             if layer and layer.available else
             (layer.note if layer else "the pack is not in this checkout")))

    want = have = 0
    for z in zooms:
        if not (src.min_zoom <= z <= src.max_zoom):
            continue
        for x, y in tiles_in_radius(lat, lon, radius_m, z):
            want += 1
            p = cache.path_for(key, z, x, y)
            try:
                if p.is_file() and p.stat().st_size:
                    have += 1
            except OSError:
                pass
    ready = want > 0 and have >= want
    return Readiness(
        key, src.label, False, ready, have, want,
        "prepared" if ready else
        "press Prepare offline map while you still have a connection",
        missing=max(0, want - have))


def all_readiness(cache: TileCache, lat: float, lon: float, radius_m: float,
                  zooms=SURVEY_ZOOMS) -> list[Readiness]:
    """Every base layer, in the order the picker shows them."""
    from .tiles import BASE_KEYS
    return [readiness(cache, k, lat, lon, radius_m, zooms) for k in BASE_KEYS]
