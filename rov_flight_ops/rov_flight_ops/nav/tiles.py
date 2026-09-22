"""
The basemap: pluggable XYZ sources, cached on disk, working offline.

Three constraints shaped this, and they pull against each other.

**It has to work with no internet.** A survey vessel in Elliott Bay has a MiFi
that comes and goes, and the map is not allowed to be the reason the operator
cannot see where the ROV is. So tiles are cached to disk permanently, the
cache is consulted first and the network second, and a view with no tiles at
all still draws -- a metre grid, a scale bar and the tracks, which is most of
what the map is for.

**It has to be polite.** These are other people's tile servers. Tiles are
fetched only for the view actually on screen, one at a time, rate-limited,
with a User-Agent that says who we are; there is no prefetching, no
speculative pyramid, and no bulk download. A cached tile is never re-fetched.
That is within OpenStreetMap's tile usage policy for a handful of laptops, and
well within Esri's and NOAA's.

**It must not put imagery in Git.** The cache lives under `%LOCALAPPDATA%`,
beside the program's settings and the telemetry cache, for the same reason
those do: a Dropbox-synced repository is the wrong place for tens of thousands
of small binary files.

On the sources. The default is the nautical one, because this is a marine
survey program and the thing an operator wants behind an ROV track is water
depth, not street names:

* **Esri World Ocean Base** carries bathymetric shading -- a genuine depth
  colour gradient, visibly resolving the shelf break in Puget Sound at zoom
  13 and 14. It is the closest thing to the requested depth gradient that is
  available as plain XYZ tiles worldwide.
* **OpenSeaMap seamarks** overlay buoys, beacons and anchorages, transparently,
  on whatever is underneath.
* **NOAA ENC charts** are the authoritative US nautical chart with real
  soundings and depth contours. They are in the registry and were *not*
  reachable from the machine this was written on, which is exactly why every
  source degrades to the next rather than failing the page.

The site's own bathymetry is better than any of them and this program already
has it: depth below the surface minus altitude above the seabed is the seabed
depth at that position, measured by the vehicle. `MapCanvas` colours the track
by it. Nothing public resolves Elliott Bay at the metre scale these surveys
work at.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Identifies this program to tile servers. OpenStreetMap's usage policy
#: requires a real one, and an unidentified client is the kind that gets a
#: whole institution blocked.
USER_AGENT = ("rov_flight_ops/0.1 (Seattle Aquarium, Coastal Climate "
              "Resilience; https://github.com/Seattle-Aquarium/"
              "CCR_ROV_survey_methods)")

#: The slowest this will ever ask for tiles, per source. Well under any of
#: these services' limits, and enough to fill a screen in a couple of seconds.
MIN_INTERVAL_S = 0.12

#: A tile that failed is not retried for this long. Stops a dead network from
#: turning into a request storm the moment the map is panned.
RETRY_AFTER_S = 60.0

#: How many tiles are held decoded in memory. A 1920x1080 map pane is about
#: forty tiles; this holds several screens' worth of panning.
MEMORY_TILES = 320

#: Tiles are 256 px square in every source here.
TILE_PX = 256

#: How many doublings past a layer's top zoom are still worth showing. Three
#: is an eight-fold enlargement: still recognisably the same shoreline. Beyond
#: that a tile is a handful of coloured squares and a blank grid is honester.
MAX_OVERZOOM = 3


@dataclass(frozen=True)
class TileSource:
    """One basemap layer.

    `url` is an XYZ template. Esri and other ArcGIS services order their path
    `{z}/{y}/{x}` rather than `{z}/{x}/{y}`; that is expressed in the template
    rather than as a flag, so a new source is added by writing its URL and
    nothing else.
    """

    key: str
    label: str
    url: str
    attribution: str
    #: Drawn over a base layer rather than replacing it.
    overlay: bool = False
    min_zoom: int = 0
    max_zoom: int = 19
    #: What the operator is told this layer is for.
    note: str = ""
    ext: str = "png"


#: The layers on offer. Order is the order they appear in the picker.
SOURCES: dict[str, TileSource] = {
    "ocean": TileSource(
        "ocean", "Nautical (depth shaded)",
        "https://services.arcgisonline.com/ArcGIS/rest/services/Ocean/"
        "World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
        "Esri, GEBCO, NOAA, National Geographic, Garmin, HERE, Geonames.org "
        "and other contributors",
        max_zoom=16, ext="jpg",
        note="Bathymetric shading — darker is deeper. Resolves the shelf "
             "break but not metre-scale relief."),
    "noaa_enc": TileSource(
        "noaa_enc", "NOAA ENC chart",
        "https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png",
        "NOAA Office of Coast Survey",
        max_zoom=17,
        note="US nautical charts with soundings and depth contours. Needs a "
             "route to NOAA; unreachable from some networks."),
    "osm": TileSource(
        "osm", "OpenStreetMap",
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "© OpenStreetMap contributors",
        max_zoom=19,
        note="Shoreline, piers and streets. No depth."),
    "imagery": TileSource(
        "imagery", "Aerial imagery",
        "https://services.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Esri, Maxar, Earthstar Geographics and the GIS User Community",
        max_zoom=19, ext="jpg",
        note="Useful for shoreline features and moorings."),
    "seamark": TileSource(
        "seamark", "Seamarks",
        "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
        "© OpenSeaMap contributors",
        overlay=True, max_zoom=18,
        note="Buoys, beacons and anchorages, drawn over the base layer."),
    "ocean_ref": TileSource(
        "ocean_ref", "Ocean labels",
        "https://services.arcgisonline.com/ArcGIS/rest/services/Ocean/"
        "World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}",
        "Esri and other contributors",
        overlay=True, max_zoom=16,
        note="Place names for the nautical layer."),
}

#: The layers that ship with the program, added to SOURCES so the picker and
#: the renderer treat them like any other basemap. They have no URL: they are
#: served from `bundled.BundledMaps` and never fetched.
BUNDLED_SOURCES: dict[str, TileSource] = {
    "pier59_imagery": TileSource(
        "pier59_imagery", "Pier 59 imagery (offline)", "",
        "USGS The National Map — public domain",
        min_zoom=13, max_zoom=16, ext="jpg",
        note="Bundled with the program. Real aerial imagery of Pier 59, "
             "available with no network and no cache. USGS has no tiles "
             "finer than z16 here."),
    "pier59_chart": TileSource(
        "pier59_chart", "Pier 59 chart (offline)", "",
        "© OpenStreetMap contributors (ODbL), rendered locally",
        min_zoom=14, max_zoom=19, ext="png",
        note="Bundled with the program. Coastline, piers, seawall and "
             "buildings, rendered from OpenStreetMap data — the detail USGS "
             "imagery does not reach."),
}
SOURCES.update(BUNDLED_SOURCES)

#: True for a layer that comes out of a packed file rather than the network.
def is_bundled(key: str) -> bool:
    return key in BUNDLED_SOURCES


#: What the map opens on with nothing else chosen. The bundled chart, because
#: it is the only layer guaranteed to be there on a first launch at a dock
#: with no signal -- which is exactly when somebody is trying to plan.
DEFAULT_BASE = "pier59_chart"
DEFAULT_OVERLAYS = ("seamark",)

BASE_KEYS = tuple(k for k, s in SOURCES.items() if not s.overlay)
OVERLAY_KEYS = tuple(k for k, s in SOURCES.items() if s.overlay)


def cache_root() -> Path:
    """Where tiles live. Outside the repository, beside the other caches.

    Shares `utc_cache`'s parent for the same reason `config.default_cache_root`
    does: this is bulk disposable data belonging to this laptop, not to the
    project, and a Dropbox-synced folder is the wrong home for it.
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CCR_ROV" / "map_tiles"


class TileCache:
    """Disk-backed, memory-fronted, offline-first tile store.

    The fetcher runs on its own thread and hands finished tiles back through a
    callback the caller marshals onto the window's thread. Nothing here
    touches Tk, and `get` never blocks: it answers from memory or disk, or
    returns None and queues a fetch.
    """

    def __init__(self, root: Path | None = None, *, online: bool = True,
                 on_ready=None, bundled=None) -> None:
        self.root = Path(root) if root else cache_root()
        #: The packs that shipped with the program. Read-only, never fetched,
        #: and never removed by a cache clear -- they are tracked assets.
        if bundled is None:
            from .bundled import BundledMaps
            bundled = BundledMaps()
        self.bundled = bundled
        #: Turned off by the operator, or by a network that is not there.
        #: When off, only cached tiles are used and nothing is requested.
        self.online = online
        self.on_ready = on_ready

        self._mem: dict[tuple, object] = {}
        self._mem_order: list[tuple] = []
        self._lock = threading.Lock()
        self._queue: list[tuple] = []
        self._queued: set[tuple] = set()
        self._failed: dict[tuple, float] = {}
        #: Tiles known not to be on disk, so the disk is not asked
        #: again every frame. Cleared when one arrives.
        self._missing: set[tuple] = set()
        self._last_fetch = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.fetched = 0
        self.from_disk = 0
        self.failures = 0
        self.last_error = ""

    # -- addresses ---------------------------------------------------------

    def path_for(self, key: str, z: int, x: int, y: int) -> Path:
        src = SOURCES[key]
        return self.root / key / str(z) / str(x) / f"{y}.{src.ext}"

    @staticmethod
    def url_for(key: str, z: int, x: int, y: int) -> str:
        return SOURCES[key].url.format(z=z, x=x, y=y)

    # -- reading ------------------------------------------------------------

    def get(self, key: str, z: int, x: int, y: int):
        """A decoded tile, or None. Never blocks and never raises.

        A None answer means "not here yet"; the map draws its grid underneath
        and repaints when `on_ready` fires. That is what makes the map usable
        the instant it opens rather than after a round of HTTP.
        """
        src = SOURCES.get(key)
        if src is None or z < src.min_zoom:
            return None
        if z > src.max_zoom:
            return self._overzoom(key, src, z, x, y)
        n = 1 << z
        if not (0 <= y < n):
            return None
        x %= n                                   # the world wraps in x

        ident = (key, z, x, y)
        with self._lock:
            hit = self._mem.get(ident)
        if hit is not None:
            return hit

        # A bundled layer is served from its pack and is never fetched or
        # written to the runtime cache: it is already on the disk, in Git,
        # and clearing the cache must not take it away.
        if is_bundled(key):
            img = self._from_bundle(ident)
            if img is not None:
                self._remember(ident, img)
            return img

        # Only go to the disk when it is worth it. A tile that is not there
        # stays not there, and stat-ing forty absent files on every redraw --
        # which is what a first version did, 7,900 times across two minutes of
        # profiling -- is pure waste on a laptop with a synchronised drive.
        if ident not in self._missing:
            img = self._load_disk(ident)
            if img is not None:
                self._remember(ident, img)
                self.from_disk += 1
                return img
            self._missing.add(ident)

        if self.online:
            self._enqueue(ident)
        return None

    def _overzoom(self, key: str, src, z: int, x: int, y: int):
        """A tile past a layer's top zoom, made by enlarging its ancestor.

        `bundled.coverage` tells the operator the view is "enlarged, not
        sharper" beyond the pack's detail, and this is what makes that true
        rather than a caption on a blank grid. The imagery layer stops at
        z16; a 30 m survey box is 75 pixels across at z16 and 600 at z19, so
        without this the operator has to choose between seeing the site and
        seeing the plan.

        Nearest-neighbour on purpose. Smooth interpolation would invent edges
        that look like resolution the pack does not have, and on a chart that
        is the difference between a blurred label and a plausible wrong one.
        Blocky pixels say "enlarged" without anyone having to read the note.
        """
        depth = z - src.max_zoom
        if depth > MAX_OVERZOOM:
            return None                      # past this it is a colour wash
        n = 1 << z
        if not (0 <= y < n):
            return None
        x %= n

        ident = (key, z, x, y)
        with self._lock:
            hit = self._mem.get(ident)
        if hit is not None:
            return hit

        parent = self.get(key, src.max_zoom, x >> depth, y >> depth)
        if parent is None:
            return None
        try:
            from PIL import Image

            step = 1 << depth
            sx, sy = x - ((x >> depth) << depth), y - ((y >> depth) << depth)
            side = TILE_PX / step
            crop = parent.crop((round(sx * side), round(sy * side),
                                round((sx + 1) * side), round((sy + 1) * side)))
            img = crop.resize((TILE_PX, TILE_PX), Image.NEAREST)
        except Exception as ex:
            log.debug("over-zoom %s failed: %s", ident, ex)
            return None
        self._remember(ident, img)
        return img

    def _from_bundle(self, ident: tuple):
        key, z, x, y = ident
        raw = self.bundled.tile(key, z, x, y)
        if raw is None:
            return None
        try:
            import io

            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                return im.convert("RGBA")
        except Exception as ex:
            log.debug("bundled tile %s did not decode: %s", ident, ex)
            return None

    def _load_disk(self, ident: tuple):
        key, z, x, y = ident
        p = self.path_for(key, z, x, y)
        try:
            if not p.is_file() or p.stat().st_size == 0:
                return None
            from PIL import Image
            with Image.open(p) as im:
                return im.convert("RGBA")
        except Exception as ex:
            log.debug("cached tile %s unreadable: %s", p, ex)
            try:
                p.unlink()          # a torn write, so it is fetched again
            except Exception:
                pass
            return None

    def _remember(self, ident: tuple, img) -> None:
        with self._lock:
            if ident in self._mem:
                return
            self._mem[ident] = img
            self._mem_order.append(ident)
            while len(self._mem_order) > MEMORY_TILES:
                self._mem.pop(self._mem_order.pop(0), None)

    # -- fetching -----------------------------------------------------------

    def _enqueue(self, ident: tuple) -> None:
        if is_bundled(ident[0]):
            return            # it is packed or it is nowhere; never fetched
        now = time.monotonic()
        with self._lock:
            if ident in self._queued:
                return
            failed_at = self._failed.get(ident)
            if failed_at is not None and (now - failed_at) < RETRY_AFTER_S:
                return
            # Newest request first: the operator has just panned, and the
            # tiles for where they are looking now matter more than the ones
            # for where they were looking a second ago.
            self._queue.insert(0, ident)
            self._queued.add(ident)
            # A queue that has run away is a symptom of a map being dragged
            # faster than the network; keep the newest screenful.
            if len(self._queue) > 200:
                for dead in self._queue[200:]:
                    self._queued.discard(dead)
                del self._queue[200:]
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tile-fetch",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        idle_since = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                ident = self._queue.pop(0) if self._queue else None
            if ident is None:
                # Let the thread retire when nothing has been asked for in a
                # while, rather than spinning for a survey day.
                if time.monotonic() - idle_since > 20.0:
                    return
                self._stop.wait(0.15)
                continue
            idle_since = time.monotonic()

            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_fetch)
            if wait > 0:
                self._stop.wait(wait)
            self._last_fetch = time.monotonic()

            try:
                self._fetch(ident)
            except Exception as ex:
                log.debug("tile fetch %s failed: %s", ident, ex)
            finally:
                with self._lock:
                    self._queued.discard(ident)

    def _fetch(self, ident: tuple) -> None:
        key, z, x, y = ident
        url = self.url_for(key, z, x, y)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as ex:
            # 404 means this source genuinely has no tile there -- open ocean
            # off the edge of a chart. Remembered so it is never asked again.
            with self._lock:
                self._failed[ident] = time.monotonic() + (
                    3600.0 if ex.code == 404 else 0.0)
            if ex.code != 404:
                self.failures += 1
                self.last_error = f"{SOURCES[key].label}: HTTP {ex.code}"
            return
        except Exception as ex:
            with self._lock:
                self._failed[ident] = time.monotonic()
            self.failures += 1
            self.last_error = f"{SOURCES[key].label}: {ex}"
            return

        if not data:
            return
        p = self.path_for(key, z, x, y)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(p)
        except Exception as ex:
            # A cache that cannot be written still gives a working map this
            # session; it simply refetches next time.
            log.debug("tile could not be cached at %s: %s", p, ex)

        try:
            import io

            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                img = im.convert("RGBA")
        except Exception as ex:
            log.debug("tile from %s did not decode: %s", url, ex)
            with self._lock:
                self._failed[ident] = time.monotonic()
            return

        self._remember(ident, img)
        self._missing.discard(ident)
        self.fetched += 1
        if self.on_ready is not None:
            try:
                self.on_ready(ident)
            except Exception:
                log.debug("tile-ready callback raised", exc_info=True)

    # -- housekeeping -------------------------------------------------------

    def stop(self, timeout: float = 0.0) -> None:
        """Signal the fetcher to stop. Does not join unless asked to.

        It may be inside a twelve-second tile request; joining on the window's
        thread while a tile server is slow would hang the close.
        """
        self._stop.set()
        with self._lock:
            self._queue.clear()
            self._queued.clear()
        t = self._thread
        if t is not None and timeout > 0:
            t.join(timeout)
            if not t.is_alive():
                self._thread = None
        elif t is None:
            self._thread = None

    def set_online(self, online: bool) -> None:
        self.online = online
        if not online:
            with self._lock:
                self._queue.clear()
                self._queued.clear()

    def cached_count(self, key: str | None = None) -> int:
        """How many tiles are on disk. Shown so an operator can tell whether
        a site has been prepared for an offline day."""
        root = self.root / key if key else self.root
        try:
            return sum(1 for p in root.rglob("*")
                       if p.is_file() and p.suffix != ".part")
        except Exception:
            return 0

    def cache_bytes(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.root.rglob("*")
                       if p.is_file())
        except Exception:
            return 0

    def status(self) -> str:
        bits = [f"{self.fetched:,} fetched", f"{self.from_disk:,} from cache"]
        if not self.online:
            bits.append("offline")
        if self.failures:
            bits.append(f"{self.failures:,} failed — {self.last_error}")
        return " · ".join(bits)


def attribution(base: str, overlays=()) -> str:
    """The credit line the map must carry. Never optional."""
    parts = []
    src = SOURCES.get(base)
    if src:
        parts.append(src.attribution)
    for key in overlays:
        o = SOURCES.get(key)
        if o and o.attribution not in parts:
            parts.append(o.attribution)
    return " · ".join(parts)
