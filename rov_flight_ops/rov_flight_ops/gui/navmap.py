"""
The map: tiles when there are any, and a useful picture when there are not.

Drawn on a plain Tk canvas for the same reason the Monitoring charts are --
this has to stay light enough to leave running all day beside Cockpit on a
field laptop. Tiles are pasted as images, tracks are polylines decimated to
the pixel, and the whole thing is one `delete("all")` and a few hundred items.

**It degrades, in this order.** Chart tiles; then whatever tiles are cached;
then a metre grid with a scale bar. The last of those is not a failure mode to
apologise for: a grid, a north arrow, the ROV's track and the distance back to
the start is most of what this map is for, and it works in a tunnel.

**Nothing on it is drawn at a position it does not have.** A stale fix is
drawn hollow and labelled with its age. A position with no geographic
reference at all -- a DVL-only dive before the origin is set -- puts the map
into a local-metre view whose axes are labelled in metres from the start
point, and it says so rather than showing a latitude it cannot justify.

**Tracks break rather than bridge.** An estimator reset, an origin change or a
source change starts a new segment, and segments are never joined: the line
between them would be a movement that did not happen.

The track can be coloured by **seabed depth** -- depth below surface minus
altitude above bottom, which is the seabed under the vehicle, measured by the
vehicle. At the metre scale these surveys work at, that is better bathymetry
than any public source, and it accumulates for free over a survey day.
"""

from __future__ import annotations

import math
import tkinter
from dataclasses import dataclass

import customtkinter as ctk

from ..nav import geo, tiles
from ..nav.model import NO_VALUE, Fix, Quality
from . import theme as T

TILE_PX = 256

#: Track rendering is bounded by pixels, not by samples: two points closer
#: together than this on screen cannot be told apart, so only one is drawn.
#: The *log* keeps every sample -- this is a drawing budget, not a filter.
MIN_SEGMENT_PX = 2.0
#: The most points any one track contributes to the canvas.
MAX_DRAWN_POINTS = 4000

#: Zoom limits. Beyond 19 no source has tiles; below 3 the survey is a dot.
#: The chart pack stops at z19, but `TileCache` enlarges past a layer's top
#: zoom, so the map is no longer limited to where the tiles stop. Two
#: doublings further is 0.05 m per pixel: enough to place a vertex to a tenth
#: of a metre, which is the scale these plans are actually drawn at. The
#: basemap says "enlarged, not sharper" up there, and it means it.
MIN_ZOOM, MAX_ZOOM = 3, 21

#: A colour ramp for seabed depth, shallow to deep. Deliberately not a
#: rainbow: a sequential ramp is read correctly by people who see colour
#: differently, and a rainbow is not.
DEPTH_RAMP = ("#C9E8F2", "#8FCBE0", "#4FA3C7", "#2A72A3", "#1A4A7A", "#122E55")


@dataclass
class Marker:
    """A fixed thing on the chart: a site, a planned start, an origin."""

    lat: float
    lon: float
    label: str
    kind: str = "site"          # site | origin | start | waypoint | transect


class MapCanvas(ctk.CTkFrame):
    """Live chart: tiles, tracks, the vehicle, the vessel and the marks."""

    def __init__(self, master, *, cache: tiles.TileCache | None = None,
                 on_status=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tkinter.Canvas(self, highlightthickness=0, bd=0,
                                     bg=_hex(T.FIELD_BG))
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.cache = cache or tiles.TileCache(on_ready=self._tile_arrived)
        self.on_status = on_status
        self.base = tiles.DEFAULT_BASE
        self.overlays = list(tiles.DEFAULT_OVERLAYS)

        self.zoom = 16
        self.centre: tuple[float, float] | None = None
        self.follow = True
        self.show_tiles = True
        self.colour_by_depth = False

        #: What is drawn. Set by the page from the collector's state.
        self.rov_track: list = []
        self.vessel_track: list = []
        self.rov_fix: Fix | None = None
        self.vessel_fix: Fix | None = None
        self.vessel_heading: float | None = None
        self.rov_heading: float | None = None
        self.markers: list[Marker] = []
        #: Planned sites and transects, imported from GeoJSON. Drawn
        #: under everything live, so a plan can never be mistaken for
        #: a measurement.
        self.planned: list = []
        #: (lat, lon, seabed_depth_m) accumulated for the depth colouring.
        self.depth_points: list[tuple[float, float, float]] = []
        #: When there is no geographic reference, positions are (north, east)
        #: metres from the start and the map says so.
        self.local_only = False
        self.local_track: list[tuple[float, float]] = []
        self.status_note = ""

        #: Tk images must outlive the draw that used them, and building one
        #: from a decoded tile is not free -- a profile found `_draw_tiles`
        #: taking 23 ms of a 38 ms map redraw, almost all of it converting
        #: the same forty tiles into PhotoImages two and a half times a
        #: second. They are keyed by tile identity and reused; only panning
        #: or zooming brings in new ones.
        self._photos: dict[tuple, object] = {}
        self._images: list = []          # what this draw is showing
        #: The plan editor. Set by the page; None means no plan is loaded.
        self.editor = None
        #: The site marker -- a fixed launch reference, never a vehicle and
        #: never a vessel.
        self.site = None
        self._drag_from: tuple[int, int] | None = None
        self._drag_centre: tuple[float, float] | None = None
        self._pixel_bounds: tuple[float, float] | None = None
        #: Set for the length of one draw; see `_origin_px`.
        self._origin_cache: tuple[float, float, int, int] | None = None

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<Motion>", self._hover)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._zoom_by(1, e))
        c.bind("<Button-5>", lambda e: self._zoom_by(-1, e))
        c.bind("<Double-Button-1>", self._double)

    # ------------------------------------------------------------------
    #  view
    # ------------------------------------------------------------------

    def set_base(self, key: str) -> None:
        if key in tiles.SOURCES and not tiles.SOURCES[key].overlay:
            self.base = key
            self.draw()

    def toggle_overlay(self, key: str, on: bool) -> None:
        if on and key not in self.overlays:
            self.overlays.append(key)
        elif not on and key in self.overlays:
            self.overlays.remove(key)
        self.draw()

    def fit_track(self) -> None:
        """Frame everything drawn. Turns following off -- an operator who
        asked to see the whole track did not ask to be dragged away from it."""
        pts = [(p.lat, p.lon) for p in self.rov_track]
        pts += [(p.lat, p.lon) for p in self.vessel_track]
        pts += [(m.lat, m.lon) for m in self.markers]
        pts += [pair for item in self.planned for pair in item.points]
        if self.rov_fix is not None:
            pts.append((self.rov_fix.lat, self.rov_fix.lon))
        if not pts:
            return
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        self.centre = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
        self.follow = False
        w = max(200, self.canvas.winfo_width())
        h = max(200, self.canvas.winfo_height())
        span_m = max(
            geo.distance_m(min(lats), self.centre[1], max(lats), self.centre[1]),
            geo.distance_m(self.centre[0], min(lons), self.centre[0], max(lons)),
            30.0) * 1.25
        self.zoom = max(MIN_ZOOM, min(
            MAX_ZOOM, geo.zoom_for_span(self.centre[0], span_m, min(w, h))))
        self.draw()

    def centre_on_vehicle(self) -> None:
        if self.rov_fix is not None:
            self.centre = (self.rov_fix.lat, self.rov_fix.lon)
        self.follow = True
        self.draw()

    def _press(self, e) -> None:
        # The editor gets first refusal: an armed tool, or a grab on a handle
        # of the selection, must not also pan the map underneath it.
        if self.editor is not None and self.editor.press(e):
            self._drag_from = None
            return
        self._drag_from = (e.x, e.y)
        self._drag_centre = self.centre

    def _hover(self, e) -> None:
        """Pointer moved with no button down: live dimensions while drawing."""
        if self.editor is not None and (self.editor.drawing
                                        or self.editor._grab is not None):
            self.editor.motion(e)

    def _drag(self, e) -> None:
        if self.editor is not None and (self.editor._grab is not None
                                        or self.editor.drawing):
            self.editor.motion(e)
            return
        if self._drag_from is None or self._drag_centre is None:
            return
        # Panning is an explicit instruction to look somewhere: following is
        # released so the map does not snap back on the next fix.
        self.follow = False
        dx, dy = e.x - self._drag_from[0], e.y - self._drag_from[1]
        cx, cy = geo.latlon_to_tile_xy(*self._drag_centre, self.zoom)
        cx -= dx / TILE_PX
        cy -= dy / TILE_PX
        n = 2.0 ** self.zoom
        cy = max(0.0, min(n, cy))
        self.centre = geo.tile_xy_to_latlon(cx % n, cy, self.zoom)
        self.draw()

    def _release(self, e) -> None:
        if self.editor is not None and self.editor.release(e):
            self._drag_from = None
            return
        self._drag_from = None

    def _double(self, e) -> None:
        """Finish an open polyline, or zoom in when nothing is being drawn."""
        if self.editor is not None and self.editor.finish():
            return
        self._zoom_by(1, e)

    def _wheel(self, e) -> None:
        self._zoom_by(1 if e.delta > 0 else -1, e)

    def _zoom_by(self, step: int, event=None) -> None:
        new = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + step))
        if new == self.zoom:
            return
        # Zoom about the pointer, not the centre: the thing under the cursor
        # is the thing the operator is looking at.
        if event is not None and self.centre is not None:
            try:
                at = self.latlon_at(event.x, event.y)
            except Exception:
                at = None
            if at is not None:
                old = self.centre
                self.zoom = new
                after = self.latlon_at(event.x, event.y)
                if after is not None:
                    self.centre = (old[0] + (at[0] - after[0]),
                                   old[1] + (at[1] - after[1]))
                self.draw()
                return
        self.zoom = new
        self.draw()

    # ------------------------------------------------------------------
    #  projection
    # ------------------------------------------------------------------

    def _origin_px(self) -> tuple[float, float, int, int] | None:
        """The pixel origin of the current view, cached for the whole draw.

        `xy` calls this once per plotted point, and it used to ask Tk for the
        canvas size every time -- two round-trips into the interpreter per
        track point. A profile found it at 1,262 calls per redraw and the
        single largest cost on the page, which is an absurd price for a
        number that cannot change in the middle of a draw.

        `draw` fills the cache at the top and clears it at the end, so
        anything called outside a draw (a click, a hit test) still measures
        the canvas properly.
        """
        if self._origin_cache is not None:
            return self._origin_cache
        try:
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tkinter.TclError:
            return None
        if w < 10 or h < 10 or self.centre is None:
            return None
        cx, cy = geo.latlon_to_tile_xy(*self.centre, self.zoom)
        return cx * TILE_PX - w / 2, cy * TILE_PX - h / 2, w, h

    def xy(self, lat: float, lon: float) -> tuple[float, float] | None:
        o = self._origin_px()
        if o is None:
            return None
        ox, oy, _w, _h = o
        tx, ty = geo.latlon_to_tile_xy(lat, lon, self.zoom)
        return tx * TILE_PX - ox, ty * TILE_PX - oy

    def latlon_at(self, x: int, y: int) -> tuple[float, float] | None:
        o = self._origin_px()
        if o is None:
            return None
        ox, oy, _w, _h = o
        return geo.tile_xy_to_latlon((ox + x) / TILE_PX, (oy + y) / TILE_PX,
                                     self.zoom)

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def _tile_arrived(self, _ident) -> None:
        """A tile finished downloading, on the fetcher's thread.

        Marshalled onto the window's thread with `after`, because Tk is not
        thread-safe and a canvas touched from a worker is the classic way to
        produce an unexplained freeze.
        """
        try:
            self.after(0, self.draw)
        except Exception:
            pass

    def draw(self) -> None:
        c = self.canvas
        try:
            w, h = c.winfo_width(), c.winfo_height()
        except tkinter.TclError:
            return
        if w < 20 or h < 20:
            return
        c.delete("all")
        self._images = []
        c.configure(bg=_hex(T.FIELD_BG))

        if self.follow and self.rov_fix is not None:
            self.centre = (self.rov_fix.lat, self.rov_fix.lon)

        if self.local_only or self.centre is None:
            self._draw_local(w, h)
            return

        self._origin_cache = None
        self._origin_cache = self._origin_px()
        try:
            drew_tiles = self._draw_tiles(w, h) if self.show_tiles else 0
            self._draw_grid(w, h, faint=drew_tiles > 0)
            self._draw_planned()
            self._draw_track(self.vessel_track, _hex(T.WARN), width=2,
                             dash=(4, 3))
            self._draw_track(self.rov_track, _hex(T.ACCENT), width=3,
                             depth_coloured=self.colour_by_depth)
            self._draw_markers()
            self._draw_site()
            if self.editor is not None:
                self.editor.draw()
            self._draw_vessel()
            self._draw_rov()
            self._draw_scale(w, h)
            self._draw_attribution(w, h, drew_tiles)
        finally:
            self._origin_cache = None

    # -- layers -------------------------------------------------------------

    def _draw_tiles(self, w: int, h: int) -> int:
        o = self._origin_px()
        if o is None:
            return 0
        ox, oy, _w, _h = o
        n = 1 << self.zoom
        x0 = int(math.floor(ox / TILE_PX))
        y0 = int(math.floor(oy / TILE_PX))
        x1 = int(math.floor((ox + w) / TILE_PX))
        y1 = int(math.floor((oy + h) / TILE_PX))
        drawn = 0
        from PIL import ImageTk
        for key in [self.base, *self.overlays]:
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    if not (0 <= ty < n):
                        continue
                    ident = (key, self.zoom, tx % n, ty)
                    photo = self._photos.get(ident)
                    if photo is None:
                        img = self.cache.get(key, self.zoom, tx, ty)
                        if img is None:
                            continue
                        try:
                            photo = ImageTk.PhotoImage(img)
                        except Exception:
                            continue
                        self._photos[ident] = photo
                    self._images.append(photo)
                    self.canvas.create_image(
                        tx * TILE_PX - ox, ty * TILE_PX - oy,
                        image=photo, anchor="nw")
                    drawn += 1
        self._trim_photos()
        return drawn

    #: Built PhotoImages kept between draws. A 1920-wide map pane is about
    #: forty tiles per layer; this holds several screens of panning before the
    #: oldest go.
    PHOTO_MAX = 240

    def _trim_photos(self) -> None:
        """Drop the least recently drawn images once there are too many.

        Bounded because a survey day of panning would otherwise accumulate
        every tile ever shown as a live Tk image, which is memory Tk does not
        give back until the image is deleted.
        """
        if len(self._photos) <= self.PHOTO_MAX:
            return
        showing = set(id(x) for x in self._images)
        for ident in list(self._photos):
            if len(self._photos) <= self.PHOTO_MAX:
                break
            if id(self._photos[ident]) not in showing:
                del self._photos[ident]

    def _draw_grid(self, w: int, h: int, *, faint: bool) -> None:
        """A metre grid with a round spacing, over or instead of the tiles."""
        if self.centre is None:
            return
        colour = _hex(T.BORDER) if faint else _hex(T.TEXT_MUTED)
        m_lat, m_lon = geo.metres_per_degree(self.centre[0])
        # Metres per pixel at this latitude and zoom.
        mpp = (156543.03392 * math.cos(math.radians(self.centre[0]))
               / (2 ** self.zoom))
        if mpp <= 0:
            return
        for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
            if step / mpp >= 55:
                break
        else:
            step = 5000
        px = step / mpp
        o = self._origin_px()
        if o is None:
            return
        ox, oy, _w, _h = o
        # Anchor the grid to the centre so it does not crawl while panning.
        cx, cy = w / 2, h / 2
        k = int(cx / px) + 2
        for i in range(-k, k + 1):
            x = cx + i * px
            if 0 <= x <= w:
                self.canvas.create_line(x, 0, x, h, fill=colour,
                                        dash=(1, 6) if faint else (2, 4))
        k = int(cy / px) + 2
        for i in range(-k, k + 1):
            y = cy + i * px
            if 0 <= y <= h:
                self.canvas.create_line(0, y, w, y, fill=colour,
                                        dash=(1, 6) if faint else (2, 4))
        self.canvas.create_text(6, 14, text=f"grid {step} m", anchor="w",
                                fill=_hex(T.TEXT_MUTED), font=T.FONT_SMALL)

    def _draw_track(self, points, colour: str, *, width: int = 2,
                    dash=None, depth_coloured: bool = False) -> None:
        """One track, per segment, decimated to the pixel.

        Segments are drawn as separate polylines and never joined. The
        decimation is by screen distance so that an hour of a hovering vehicle
        costs the same as a minute of a moving one.
        """
        if not points:
            return
        by_segment: dict[int, list] = {}
        for p in points:
            by_segment.setdefault(getattr(p, "segment", 0), []).append(p)

        for seg in sorted(by_segment):
            pts = by_segment[seg]
            coords: list[float] = []
            last: tuple[float, float] | None = None
            for p in pts:
                at = self.xy(p.lat, p.lon)
                if at is None:
                    continue
                if last is not None and math.dist(at, last) < MIN_SEGMENT_PX:
                    continue
                coords.extend(at)
                last = at
                if len(coords) >= MAX_DRAWN_POINTS * 2:
                    break
            if len(coords) >= 4:
                kw = {"fill": colour, "width": width, "capstyle": "round",
                      "joinstyle": "round"}
                if dash:
                    kw["dash"] = dash
                self.canvas.create_line(*coords, **kw)

        if depth_coloured and self.depth_points:
            self._draw_depth_dots()

    def _draw_depth_dots(self) -> None:
        """The seabed depth the vehicle measured, as coloured dots.

        Depth below the surface minus altitude above the bottom is the seabed
        depth under the vehicle. Nothing public resolves Elliott Bay at this
        scale; this is the survey's own bathymetry, and it costs nothing
        because both numbers are already on the page.
        """
        depths = [d for _la, _lo, d in self.depth_points]
        if not depths:
            return
        lo, hi = min(depths), max(depths)
        span = max(0.5, hi - lo)
        step = max(1, len(self.depth_points) // 1500)
        for la, lo_, d in self.depth_points[::step]:
            at = self.xy(la, lo_)
            if at is None:
                continue
            i = int((d - lo) / span * (len(DEPTH_RAMP) - 1))
            colour = DEPTH_RAMP[max(0, min(len(DEPTH_RAMP) - 1, i))]
            self.canvas.create_oval(at[0] - 3, at[1] - 3, at[0] + 3, at[1] + 3,
                                    fill=colour, outline="")
        self.canvas.create_text(
            6, 30, anchor="w", font=T.FONT_SMALL, fill=_hex(T.TEXT_MUTED),
            text=f"seabed {lo:.1f}–{hi:.1f} m (measured: depth − altitude)")

    def _draw_planned(self) -> None:
        """The survey plan: dashed, muted, and clearly not a measurement.

        Drawn first so everything live sits on top of it, and in a dashed
        outline so that a planned transect and a flown one cannot be confused
        at a glance -- which is the whole risk of putting them on one chart.
        """
        for item in self.planned:
            pts: list[float] = []
            for lat, lon in item.points:
                at = self.xy(lat, lon)
                if at is not None:
                    pts.extend(at)
            if not pts:
                continue
            if item.shape == "line" and len(pts) >= 4:
                self.canvas.create_line(*pts, fill=_hex(T.TEXT_MUTED), width=2,
                                        dash=(6, 4))
                self.canvas.create_text(pts[0] + 8, pts[1] - 8, text=item.name,
                                        anchor="w", fill=_hex(T.TEXT_MUTED),
                                        font=T.FONT_SMALL)
            else:
                x, y = pts[0], pts[1]
                self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7,
                                        outline=_hex(T.TEXT_MUTED), width=2,
                                        dash=(3, 3))
                self.canvas.create_text(x + 10, y, text=item.name, anchor="w",
                                        fill=_hex(T.TEXT_MUTED),
                                        font=T.FONT_SMALL)

    def _draw_markers(self) -> None:
        for m in self.markers:
            at = self.xy(m.lat, m.lon)
            if at is None:
                continue
            x, y = at
            if m.kind == "origin":
                # A fixed reference, deliberately not a boat: it does not move
                # and nothing about it follows the vessel.
                self.canvas.create_line(x - 9, y, x + 9, y, fill=_hex(T.OK),
                                        width=2)
                self.canvas.create_line(x, y - 9, x, y + 9, fill=_hex(T.OK),
                                        width=2)
                self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                                        outline=_hex(T.OK), width=2)
            elif m.kind == "waypoint":
                self.canvas.create_polygon(x, y - 8, x - 6, y + 5, x + 6, y + 5,
                                           fill=_hex(T.HEADING), outline="")
            else:
                self.canvas.create_rectangle(x - 5, y - 5, x + 5, y + 5,
                                             outline=_hex(T.TEXT),
                                             fill=_hex(T.SURFACE), width=2)
            self.canvas.create_text(x + 11, y, text=m.label, anchor="w",
                                    fill=_hex(T.TEXT), font=T.FONT_SMALL)

    def _draw_site(self) -> None:
        """The launch site: a fixed reference, deliberately unlike everything
        that moves.

        Not a boat, not the vehicle, not the EKF origin. An operator glancing
        at the map must be able to tell "where we go in" from "where the ROV
        is" without reading a label.
        """
        if not self.site:
            return
        at = self.xy(self.site["lat"], self.site["lon"])
        if at is None:
            return
        x, y = at
        c = self.canvas
        col = _hex(T.HEADING)
        c.create_oval(x - 11, y - 11, x + 11, y + 11, outline=col, width=2)
        c.create_line(x - 5, y, x + 5, y, fill=col, width=2)
        c.create_line(x, y - 5, x, y + 5, fill=col, width=2)
        c.create_text(x + 15, y, text=self.site.get("short", "site"),
                      anchor="w", fill=col, font=T.FONT_SMALL)

    def _draw_vessel(self) -> None:
        f = self.vessel_fix
        if f is None:
            return
        at = self.xy(f.lat, f.lon)
        if at is None:
            return
        x, y = at
        stale = f.quality is not Quality.OK
        colour = _hex(T.TEXT_MUTED) if stale else _hex(T.WARN)
        # The bow points along HDT true heading. Not course over ground, not
        # the ROV's yaw, not the direction of travel -- and when HDT is stale
        # the hull is drawn without a bow at all rather than pointing a way
        # nobody has confirmed.
        hdg = self.vessel_heading
        if hdg is None:
            self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7,
                                    outline=colour, width=2)
            self.canvas.create_text(x + 10, y - 10, text="vessel · no HDT",
                                    anchor="w", fill=_hex(T.WARN),
                                    font=T.FONT_SMALL)
        else:
            self._hull(x, y, hdg, colour, filled=not stale)
            self.canvas.create_text(x + 12, y - 12,
                                    text=f"vessel {hdg:.0f}°T"
                                         + (" · stale" if stale else ""),
                                    anchor="w", fill=colour, font=T.FONT_SMALL)

    def _hull(self, x, y, heading_deg, colour, *, filled=True) -> None:
        a = math.radians(heading_deg)
        pts = []
        for lx, ly in ((0, -11), (6, 4), (0, 1), (-6, 4)):
            rx = lx * math.cos(a) - ly * math.sin(a)
            ry = lx * math.sin(a) + ly * math.cos(a)
            pts += [x + rx, y + ry]
        self.canvas.create_polygon(*pts, fill=colour if filled else "",
                                   outline=colour, width=2)

    def _draw_rov(self) -> None:
        f = self.rov_fix
        if f is None:
            return
        at = self.xy(f.lat, f.lon)
        if at is None:
            return
        x, y = at
        stale = f.quality is not Quality.OK
        colour = _hex(T.TEXT_MUTED) if stale else _hex(T.ACCENT)
        hdg = self.rov_heading
        if hdg is not None:
            a = math.radians(hdg)
            pts = []
            for lx, ly in ((0, -12), (8, 7), (0, 3), (-8, 7)):
                rx = lx * math.cos(a) - ly * math.sin(a)
                ry = lx * math.sin(a) + ly * math.cos(a)
                pts += [x + rx, y + ry]
            self.canvas.create_polygon(*pts, fill="" if stale else colour,
                                       outline=colour, width=2)
        else:
            self.canvas.create_oval(x - 8, y - 8, x + 8, y + 8,
                                    fill="" if stale else colour,
                                    outline=colour, width=2)
        if stale:
            age = f.age()
            self.canvas.create_text(
                x + 13, y + 12, anchor="w", fill=_hex(T.WARN),
                font=T.FONT_SMALL,
                text="last known" + (f" · {age:.0f} s" if age else ""))
        if f.kind == "dead":
            self.canvas.create_text(x + 13, y - 12, anchor="w",
                                    fill=_hex(T.TEXT_MUTED),
                                    font=T.FONT_SMALL, text="dead-reckoned")

    def _draw_scale(self, w: int, h: int) -> None:
        if self.centre is None:
            return
        mpp = (156543.03392 * math.cos(math.radians(self.centre[0]))
               / (2 ** self.zoom))
        for metres in (5, 10, 20, 50, 100, 200, 500, 1000, 2000):
            px = metres / mpp
            if px >= 70:
                break
        else:
            metres, px = 5000, 5000 / mpp
        x0, y0 = 12, h - 16
        ink = _hex(T.TEXT)
        self.canvas.create_line(x0, y0, x0 + px, y0, fill=ink, width=3)
        self.canvas.create_line(x0, y0 - 4, x0, y0 + 4, fill=ink, width=2)
        self.canvas.create_line(x0 + px, y0 - 4, x0 + px, y0 + 4, fill=ink,
                                width=2)
        self.canvas.create_text(x0 + px / 2, y0 - 9,
                                text=f"{metres:,} m" if metres < 1000
                                else f"{metres / 1000:g} km",
                                fill=ink, font=T.FONT_SMALL)
        # North arrow. The map is never rotated, so north is always up -- and
        # saying so beats leaving an operator to assume it.
        nx, ny = w - 22, 30
        self.canvas.create_line(nx, ny + 12, nx, ny - 12, fill=ink, width=2,
                                arrow="first")
        self.canvas.create_text(nx, ny - 20, text="N", fill=ink,
                                font=T.FONT_SMALL)

    def _draw_attribution(self, w: int, h: int, drew: int) -> None:
        text = tiles.attribution(self.base, self.overlays)
        if not self.show_tiles or drew == 0:
            missing = "no basemap — grid only"
            if not self.cache.online:
                missing += " (offline)"
            self.canvas.create_text(w - 8, h - 6, text=missing, anchor="e",
                                    fill=_hex(T.WARN), font=T.FONT_SMALL)
            return
        self.canvas.create_text(w - 8, h - 6, text=text[:110], anchor="e",
                                fill=_hex(T.TEXT_MUTED), font=T.FONT_SMALL)

    # -- the no-geography case ---------------------------------------------

    def _draw_local(self, w: int, h: int) -> None:
        """Metres from the start, with no claim to a geographic position.

        This is what a DVL-only dive looks like before the EKF origin is set,
        which on this fleet is the normal state of affairs until somebody
        fixes it. The shape of the track is entirely real and its place on the
        Earth is unknown; drawing it against a chart would imply the second.
        """
        c = self.canvas
        c.create_rectangle(0, 0, w, h, fill=_hex(T.FIELD_BG), outline="")
        pts = self.local_track
        cx, cy = w / 2, h / 2
        if pts:
            xs = [p[1] for p in pts]
            ys = [p[0] for p in pts]
            span = max(max(xs) - min(xs), max(ys) - min(ys), 20.0) * 1.3
            ppm = min(w, h) / span
            mid_n = (max(ys) + min(ys)) / 2
            mid_e = (max(xs) + min(xs)) / 2
        else:
            ppm, mid_n, mid_e = min(w, h) / 40.0, 0.0, 0.0

        step = 5
        while step * ppm < 45:
            step *= 2
        k = int(max(w, h) / (step * ppm)) + 2
        for i in range(-k, k + 1):
            x = cx + (i * step - (mid_e % step)) * ppm
            y = cy - (i * step - (mid_n % step)) * ppm
            c.create_line(x, 0, x, h, fill=_hex(T.BORDER), dash=(2, 5))
            c.create_line(0, y, w, y, fill=_hex(T.BORDER), dash=(2, 5))

        to_px = lambda n, e: (cx + (e - mid_e) * ppm, cy - (n - mid_n) * ppm)  # noqa: E731
        coords: list[float] = []
        last = None
        for n, e in pts:
            at = to_px(n, e)
            if last is not None and math.dist(at, last) < MIN_SEGMENT_PX:
                continue
            coords.extend(at)
            last = at
        if len(coords) >= 4:
            c.create_line(*coords, fill=_hex(T.ACCENT), width=3,
                          capstyle="round", joinstyle="round")
        if pts:
            sx, sy = to_px(0.0, 0.0)
            c.create_line(sx - 9, sy, sx + 9, sy, fill=_hex(T.OK), width=2)
            c.create_line(sx, sy - 9, sx, sy + 9, fill=_hex(T.OK), width=2)
            c.create_text(sx + 11, sy, text="start", anchor="w",
                          fill=_hex(T.OK), font=T.FONT_SMALL)
            nx, ny = to_px(*pts[-1])
            hdg = self.rov_heading
            if hdg is not None:
                a = math.radians(hdg)
                poly = []
                for lx, ly in ((0, -12), (8, 7), (0, 3), (-8, 7)):
                    poly += [nx + lx * math.cos(a) - ly * math.sin(a),
                             ny + lx * math.sin(a) + ly * math.cos(a)]
                c.create_polygon(*poly, fill=_hex(T.ACCENT), outline="")
            else:
                c.create_oval(nx - 8, ny - 8, nx + 8, ny + 8,
                              fill=_hex(T.ACCENT), outline="")

        c.create_text(
            w / 2, 18, anchor="c", fill=_hex(T.WARN), font=T.FONT_H2,
            text="LOCAL VIEW — metres from the start, not a geographic position")
        c.create_text(
            w / 2, 36, anchor="c", fill=_hex(T.TEXT_MUTED), font=T.FONT_SMALL,
            text=self.status_note
                 or "The EKF has no origin, so there is no latitude or "
                    "longitude to plot. Set one under the map.")
        c.create_text(12, h - 14, anchor="w", fill=_hex(T.TEXT),
                      font=T.FONT_SMALL, text=f"grid {step} m")

    def refresh_theme(self) -> None:
        self.draw()

    def forget_images(self) -> None:
        """Drop the cached Tk images. For a theme change or a shutdown."""
        self._photos.clear()
        self._images = []


def format_position(fix: Fix | None, *, now: float | None = None) -> str:
    """The lat/lon readout, with its source and its age. Never bare numbers."""
    if fix is None:
        return f"{NO_VALUE}  no position"
    age = fix.age(now)
    bits = [f"{fix.lat:.6f}, {fix.lon:.6f}"]
    kind = {"ekf": "EKF", "dead": "dead-reckoned", "acoustic": "acoustic",
            "vessel": "vessel", "manual": "manual"}.get(fix.kind, fix.kind)
    bits.append(kind)
    if fix.quality is not Quality.OK:
        bits.append(fix.quality.value.upper())
    if age is not None:
        bits.append(f"{age:.0f} s")
    return "  ·  ".join(bits)


def _hex(colour) -> str:
    if isinstance(colour, (tuple, list)):
        mode = ctk.get_appearance_mode()
        return colour[1] if str(mode).lower() == "dark" else colour[0]
    return colour
