"""
The basemap that ships with the program, for the first launch on a boat.

A fresh clone, no internet, no cache, no ROV: the map still shows the real
Pier 59. That is the whole point of this module, and it is the one case the
runtime tile cache cannot serve, because a cache that has never been filled is
empty.

**Two layers, both genuinely redistributable**, built by
`assets/maps/build_pier59.py` which records where every pixel came from:

``pier59_imagery``  USGS National Map aerial imagery, zooms 13-16. A work of
                    the United States Government, so not under domestic
                    copyright. Real imagery of the real pier.
``pier59_chart``    Rendered locally from OpenStreetMap data (ODbL, attributed)
                    at zooms 14-19 -- coastline, the piers, the seawall,
                    buildings and Alaskan Way. It exists because the USGS
                    cache stops at zoom 16, which is 1.6 m per pixel and far
                    too coarse to lay out a 30 m transect on.

**Read-only, always.** `open()` opens the SQLite files with an immutable URI,
so a bug here cannot write to a tracked asset, and `TileCache` is told to
leave them alone: clearing the runtime cache must never take the bundled map
with it.

MBTiles rather than loose PNGs: SQLite is in the standard library, the format
is published and readable by other tools, and one tracked binary per layer is
far easier to review and checksum than 134 small files. Its rows are TMS --
y counted from the south -- and `_tms` does the one flip that needs doing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the packs live, relative to the package. Inside the repository on
#: purpose: this is a maintained asset, not runtime data.
PACK_DIR = Path(__file__).resolve().parents[1].parent / "assets" / "maps" / "pier59"

#: The site the pack covers, and where the map opens when nothing else has
#: been chosen.
PIER59 = {
    "key": "pier59",
    "name": "Seattle Aquarium — Pier 59 / OTS",
    "short": "Pier 59 / OTS",
    "lat": 47.6075661,
    "lon": -122.3438752,
    #: A survey-planning zoom: about 200 m across a 500 px pane.
    "zoom": 18,
}


@dataclass
class BundledLayer:
    """One packed layer, and what is known about where it came from."""

    key: str
    path: Path
    name: str = ""
    attribution: str = ""
    licence: str = ""
    source: str = ""
    fmt: str = "png"
    min_zoom: int = 0
    max_zoom: int = 19
    #: (west, south, east, north)
    bounds: tuple[float, float, float, float] | None = None
    tiles: int = 0
    bytes: int = 0
    available: bool = False
    note: str = ""
    _db: sqlite3.Connection | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- reading ---------------------------------------------------------

    def open(self) -> bool:
        """Open the pack read-only. False if it is missing or unreadable."""
        if self._db is not None:
            return True
        if not self.path.is_file():
            self.note = f"{self.path.name} is not in this checkout"
            return False
        try:
            # immutable=1: SQLite will not create a journal, will not write,
            # and is safe to open from several threads. A tracked asset must
            # not be modified by running the program.
            uri = f"file:{self.path.as_posix()}?immutable=1"
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._db.execute("SELECT count(*) FROM tiles").fetchone()
            self.available = True
            return True
        except Exception as ex:
            self.note = f"{self.path.name} could not be opened: {ex}"
            log.warning("bundled map %s: %s", self.key, ex)
            self._db = None
            return False

    @staticmethod
    def _tms(z: int, y: int) -> int:
        """XYZ y to MBTiles (TMS) row, which counts from the south."""
        return (1 << z) - 1 - y

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """The raw bytes of one tile, or None if the pack has no such tile."""
        if self._db is None and not self.open():
            return None
        if not (self.min_zoom <= z <= self.max_zoom):
            return None
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? "
                    "AND tile_column=? AND tile_row=?",
                    (z, x, self._tms(z, y))).fetchone()
        except Exception as ex:
            log.debug("bundled tile %s/%s/%s/%s: %s", self.key, z, x, y, ex)
            return None
        return bytes(row[0]) if row else None

    def covers(self, lat: float, lon: float) -> bool:
        if self.bounds is None:
            return False
        w, s, e, n = self.bounds
        return w <= lon <= e and s <= lat <= n

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None


class BundledMaps:
    """Every pack that shipped with this build, and what they cover.

    Loaded once and held. Opening a SQLite file is cheap and the packs are
    small, so there is nothing to defer; what matters is that a missing or
    corrupt pack degrades to "no bundled map" with a reason, rather than
    stopping the page.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = Path(directory) if directory else PACK_DIR
        self.layers: dict[str, BundledLayer] = {}
        self.manifest: dict = {}
        self.problem = ""
        self._load()

    def _load(self) -> None:
        mf = self.dir / "manifest.json"
        try:
            self.manifest = json.loads(mf.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.problem = (f"no bundled map: {mf} is missing. The metric grid "
                            f"is still available for planning.")
            log.info("%s", self.problem)
            return
        except Exception as ex:
            self.problem = f"the bundled map manifest could not be read: {ex}"
            log.warning("%s", self.problem)
            return

        b = self.manifest.get("bounds") or {}
        bounds = ((b.get("west"), b.get("south"), b.get("east"), b.get("north"))
                  if b else None)
        for key, info in (self.manifest.get("layers") or {}).items():
            zooms = info.get("zooms") or [0, 19]
            layer = BundledLayer(
                key=key, path=self.dir / f"{key}.mbtiles",
                name=info.get("source", key),
                attribution=info.get("attribution", ""),
                licence=info.get("licence", ""),
                source=info.get("source", ""),
                fmt=info.get("format", "png"),
                min_zoom=int(zooms[0]), max_zoom=int(zooms[1]),
                bounds=bounds if bounds and all(v is not None for v in bounds)
                else None,
                tiles=int(info.get("tiles") or 0),
                bytes=int(info.get("bytes") or 0))
            layer.open()
            self.layers[key] = layer

    # -- what the page asks ----------------------------------------------

    @property
    def any_available(self) -> bool:
        return any(x.available for x in self.layers.values())

    def tile(self, key: str, z: int, x: int, y: int) -> bytes | None:
        layer = self.layers.get(key)
        return layer.tile(z, x, y) if layer is not None else None

    def coverage(self, key: str, lat: float, lon: float, zoom: int) -> str:
        """Why a bundled tile is or is not there, in words for the operator.

        Four different answers, because they call for four different things
        and "no map" tells the operator none of them.
        """
        layer = self.layers.get(key)
        if layer is None:
            return "not a bundled layer"
        if not layer.available:
            return layer.note or "the pack is not readable"
        if not layer.covers(lat, lon):
            return "outside the bundled extent"
        if zoom > layer.max_zoom:
            return (f"beyond the bundled detail (z{layer.max_zoom}) — the view "
                    f"is enlarged, not sharper")
        if zoom < layer.min_zoom:
            return f"below the bundled zoom range (z{layer.min_zoom})"
        return "bundled"

    def summary(self) -> str:
        if self.problem:
            return self.problem
        parts = []
        for layer in self.layers.values():
            if layer.available:
                parts.append(f"{layer.key} z{layer.min_zoom}–{layer.max_zoom} "
                             f"({layer.bytes / 1024:.0f} KiB)")
        if not parts:
            return "no bundled map is available"
        return "offline: " + ", ".join(parts)

    def close(self) -> None:
        for layer in self.layers.values():
            layer.close()
