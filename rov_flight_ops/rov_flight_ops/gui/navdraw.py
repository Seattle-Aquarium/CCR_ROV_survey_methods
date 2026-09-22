"""
Drawing and editing a survey plan on the map.

The interaction half of the plan editor. `plan.py` owns the geometry and the
metres; this owns what a press, a drag and a release mean, and what gets drawn
on top of the chart while they happen.

**Pan and draw are separate modes, always.** A tool is armed deliberately and
Escape disarms it. That is the difference between a map you can lean on and
one where a stray click adds a vertex to somebody's survey. The cursor changes
with the mode, and the mode is named on screen rather than inferred from which
modifier is held.

**Dimensions are live and come from the geometry, not the pixels.** While a
rectangle is being dragged its edges carry the length and width in metres and
its middle carries the area, computed by projecting the pointer into the
plan's local frame -- so they are the same numbers the inspector will show and
the same ones that get saved. A label derived from screen distance would
change with the zoom.

**Nothing here writes to a vehicle.** It is a drawing surface.
"""

from __future__ import annotations

import math

from ..nav import plan as P
from . import theme as T

#: Grab radius for a handle, in screen pixels. Generous: this is used on a
#: Rugged laptop's trackpad on a moving boat.
HANDLE_PX = 9
GRAB_PX = 14

#: A drag shorter than this is a click, not a drag.
CLICK_PX = 4

#: An edge shorter than this on screen cannot carry its dimension legibly, so
#: the label is left off rather than stacked on its neighbours. Zooming in
#: brings it back and the inspector has the number either way. Without this a
#: 30 m box seen from 400 m away draws five labels inside seventy pixels, and
#: the smudge that results hides the shape underneath it.
LABEL_MIN_PX = 64

#: Per line of the block in the middle of an area. A rectangle's block is two
#: lines (area, orientation) and a grid's is three (plus the lanes), so the
#: room one needs is not the room the other needs and a single threshold gets
#: one of them wrong. At survey scale this means a 30 by 20 m box shows its
#: edge dimensions on the map and leaves the summary to the inspector, which
#: is beside it, until the operator zooms in far enough for both.
BLOCK_LINE_PX = 34

#: Snap a new vertex to an existing one within this many pixels.
SNAP_PX = 12

TOOLS = ("pan", "line", "polyline", "rect", "grid", "circle", "polygon")

TOOL_LABEL = {
    "pan": "Pan",
    "line": "Line",
    "polyline": "Polyline",
    "rect": "Rectangle",
    "grid": "Survey grid",
    "circle": "Circle",
    "polygon": "Polygon",
}

#: A glyph per feature *kind*, for the feature list. Deliberately separate
#: from the tool palette's glyphs: "polyline" is a tool but the feature it
#: makes is a line, and keying one map by both would mislabel every polyline.
KIND_GLYPH = {"line": "╱", "rect": "▭", "grid": "▦",
              "circle": "◯", "polygon": "⬠"}


TOOL_HINT = {
    "pan": "Drag to move the map.",
    "line": "Click the start, then click the end. Escape cancels.",
    "polyline": "Click each point; double-click or Enter to finish.",
    "rect": "Drag a box. Hold Shift while dragging a corner handle to rotate.",
    "grid": "Drag a box; lanes fill it. Spacing and direction are on the right.",
    "circle": "Click the centre, then drag out the radius.",
    "polygon": "Click each corner; double-click or Enter to close.",
}


class PlanEditor:
    """Tool state and pointer handling for one `MapCanvas`.

    Holds no geometry of its own beyond the shape being drawn right now:
    everything committed goes straight into the `Plan`, so undo, autosave and
    export all see the same thing.
    """

    def __init__(self, canvas_owner, on_change=None, on_select=None) -> None:
        #: The MapCanvas. Used for its projection and its canvas.
        self.map = canvas_owner
        self.on_change = on_change           # (label) -> None, for undo/save
        self.on_select = on_select           # (feature or None) -> None

        self.plan: P.Plan | None = None
        self.tool = "pan"
        self.selected_id: str | None = None

        #: While a shape is being drawn: local-frame points collected so far.
        self._draft: list = []
        self._draft_anchor: P.Anchor | None = None
        #: Live pointer position in the draft's local frame.
        self._cursor: tuple | None = None
        #: Which handle of the selected feature is being dragged.
        self._grab: tuple | None = None
        self._grab_start: tuple | None = None
        self._rotating = False

        #: Default settings a new grid is created with; the inspector edits
        #: them afterwards.
        self.grid_spacing_m = 2.0
        self.grid_axis = "length"

        self.status = ""

    # ------------------------------------------------------------------
    #  tools
    # ------------------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        if tool not in TOOLS:
            return
        self.cancel_draft()
        self.tool = tool
        self.status = TOOL_HINT.get(tool, "")
        self._set_cursor()
        self.map.draw()

    def _set_cursor(self) -> None:
        try:
            self.map.canvas.configure(
                cursor="fleur" if self.tool == "pan" else "crosshair")
        except Exception:
            pass

    @property
    def drawing(self) -> bool:
        return self.tool != "pan"

    def cancel_draft(self) -> None:
        """Escape: abandon what is being drawn, keep everything saved."""
        had = bool(self._draft)
        self._draft = []
        self._draft_anchor = None
        self._cursor = None
        self._grab = None
        self._rotating = False
        if had:
            self.status = "Cancelled."
            self.map.draw()

    def select(self, feature_id: str | None) -> None:
        self.selected_id = feature_id
        if self.on_select is not None:
            self.on_select(self.selected() if feature_id else None)
        self.map.draw()

    def selected(self):
        return self.plan.get(self.selected_id) if (
            self.plan and self.selected_id) else None

    # ------------------------------------------------------------------
    #  geometry <-> screen
    # ------------------------------------------------------------------

    def _anchor(self) -> P.Anchor | None:
        """The local frame a new feature is drawn in: the map's centre.

        One anchor per feature, taken where it was drawn, so a plan spread
        over a site does not accumulate projection error from a single distant
        origin.
        """
        c = self.map.centre
        return P.Anchor(c[0], c[1]) if c else None

    def _to_local(self, anchor: P.Anchor, x: float, y: float):
        at = self.map.latlon_at(int(x), int(y))
        return anchor.to_local(*at) if at else None

    def _to_screen(self, anchor: P.Anchor, east: float, north: float):
        return self.map.xy(*anchor.to_geo(east, north))

    # ------------------------------------------------------------------
    #  pointer
    # ------------------------------------------------------------------

    def press(self, event) -> bool:
        """True if the editor consumed the press (so the map must not pan)."""
        if self.plan is None:
            return False
        if self.tool == "pan":
            return self._press_select(event)

        anchor = self._draft_anchor or self._anchor()
        if anchor is None:
            return False
        pt = self._to_local(anchor, event.x, event.y)
        if pt is None:
            return False
        self._draft_anchor = anchor
        pt = self._snap(anchor, pt, event)

        if self.tool in ("line", "circle"):
            self._draft.append(pt)
            if len(self._draft) >= 2:
                self._commit()
        elif self.tool in ("polyline", "polygon"):
            self._draft.append(pt)
            self.status = (f"{len(self._draft)} point(s) — double-click or "
                           f"Enter to finish")
        elif self.tool in ("rect", "grid"):
            self._draft = [pt]
        self.map.draw()
        return True

    def _press_select(self, event) -> bool:
        """In pan mode a press may still grab a handle of the selection."""
        f = self.selected()
        if f is not None and not f.locked:
            grab = self._hit_handle(f, event.x, event.y)
            if grab is not None:
                self._grab = grab
                self._grab_start = (event.x, event.y)
                self._rotating = bool(event.state & 0x0001)   # Shift
                return True
        hit = self.hit_test(event.x, event.y)
        if hit is not None:
            self.select(hit.id)
            return True
        if self.selected_id is not None:
            self.select(None)
        return False

    def motion(self, event) -> bool:
        """Pointer moved with no button down, or with one during a drag."""
        if self.plan is None:
            return False
        if self._grab is not None:
            self._drag_handle(event)
            return True
        if not self._draft_anchor:
            return False
        pt = self._to_local(self._draft_anchor, event.x, event.y)
        if pt is None:
            return False
        self._cursor = pt
        self.map.draw()
        return True

    def release(self, event) -> bool:
        if self._grab is not None:
            self._grab = None
            self._rotating = False
            self._changed("edit")
            return True
        if self.tool in ("rect", "grid") and len(self._draft) == 1:
            moved = self._cursor
            if moved is None:
                return True
            a, b = self._draft[0], moved
            if abs(a[0] - b[0]) < P.MIN_RECT_M or abs(a[1] - b[1]) < P.MIN_RECT_M:
                self.status = (f"Too small — a survey box is at least "
                               f"{P.MIN_RECT_M} m each way.")
                self.cancel_draft()
                return True
            self._commit()
            return True
        return False

    def finish(self) -> bool:
        """Enter or a double-click: close a polyline or polygon."""
        if self.tool in ("polyline", "polygon") and len(self._draft) >= 2:
            self._commit()
            return True
        return False

    def _snap(self, anchor: P.Anchor, pt, event):
        """Snap to a nearby existing vertex, so plans join up cleanly."""
        if self.plan is None:
            return pt
        best, best_d = None, SNAP_PX
        for f in self.plan.features:
            if f.hidden:
                continue
            for la, lo in f.geo_points():
                at = self.map.xy(la, lo)
                if at is None:
                    continue
                d = math.dist(at, (event.x, event.y))
                if d < best_d:
                    best, best_d = (la, lo), d
        return anchor.to_local(*best) if best else pt

    # ------------------------------------------------------------------
    #  committing
    # ------------------------------------------------------------------

    def _commit(self) -> None:
        a = self._draft_anchor
        if a is None or self.plan is None:
            self.cancel_draft()
            return
        pts = list(self._draft)
        if self.tool in ("rect", "grid") and self._cursor is not None:
            pts = [pts[0], self._cursor]
        feature = None

        if self.tool == "line" and len(pts) >= 2:
            feature = P.Line(anchor=a, points=[pts[0], pts[1]])
        elif self.tool == "polyline" and len(pts) >= 2:
            feature = P.Line(anchor=a, points=pts)
        elif self.tool == "polygon" and len(pts) >= 3:
            feature = P.Polygon(anchor=a, points=pts)
        elif self.tool == "circle" and len(pts) >= 2:
            feature = P.Circle(anchor=a, centre=pts[0],
                               radius_m=math.dist(pts[0], pts[1]))
        elif self.tool in ("rect", "grid") and len(pts) >= 2:
            ae, an = pts[0]
            be, bn = pts[1]
            centre = ((ae + be) / 2.0, (an + bn) / 2.0)
            width = abs(be - ae)
            length = abs(bn - an)
            if self.tool == "rect":
                feature = P.Rect(anchor=a, centre=centre, length_m=length,
                                 width_m=width, rotation_deg=0.0)
            else:
                feature = P.Grid(anchor=a, centre=centre, length_m=length,
                                 width_m=width, rotation_deg=0.0,
                                 spacing_m=self.grid_spacing_m,
                                 lane_axis=self.grid_axis)

        self._draft = []
        self._draft_anchor = None
        self._cursor = None
        if feature is None:
            self.status = "Not enough points."
            self.map.draw()
            return
        self.plan.add(feature)
        self.select(feature.id)
        m = feature.measurements()
        self.status = _describe(feature, m)
        self._changed(f"add {feature.kind}")
        # One shape per arming, except the free-hand tools, so a stray click
        # after finishing cannot start another box nobody wanted.
        if self.tool in ("line", "rect", "grid", "circle"):
            self.set_tool("pan")

    def _changed(self, label: str) -> None:
        if self.on_change is not None:
            self.on_change(label)
        self.map.draw()

    # ------------------------------------------------------------------
    #  handles
    # ------------------------------------------------------------------

    def handles(self, f) -> list:
        """[(screen_x, screen_y, role, index), ...] for the selected feature."""
        out = []
        if isinstance(f, (P.Rect, P.Grid)):
            for i, (e, n) in enumerate(f.corners()):
                at = self._to_screen(f.anchor, e, n)
                if at:
                    out.append((at[0], at[1], "corner", i))
            at = self._to_screen(f.anchor, *f.centre)
            if at:
                out.append((at[0], at[1], "centre", -1))
        elif isinstance(f, P.Circle):
            at = self._to_screen(f.anchor, *f.centre)
            if at:
                out.append((at[0], at[1], "centre", -1))
            edge = self._to_screen(f.anchor, f.centre[0] + f.radius_m,
                                   f.centre[1])
            if edge:
                out.append((edge[0], edge[1], "radius", 0))
        elif isinstance(f, (P.Line, P.Polygon)):
            for i, (e, n) in enumerate(f.points):
                at = self._to_screen(f.anchor, e, n)
                if at:
                    out.append((at[0], at[1], "vertex", i))
        return out

    def _hit_handle(self, f, x: float, y: float):
        for hx, hy, role, idx in self.handles(f):
            if math.dist((hx, hy), (x, y)) <= GRAB_PX:
                return (role, idx)
        return None

    def _drag_handle(self, event) -> None:
        f = self.selected()
        if f is None or f.locked or self._grab is None:
            return
        role, idx = self._grab
        pt = self._to_local(f.anchor, event.x, event.y)
        if pt is None:
            return

        if isinstance(f, (P.Rect, P.Grid)):
            if role == "centre":
                f.centre = pt
            elif role == "corner" and self._rotating:
                # Shift-drag a corner spins the box about its centre, keeping
                # its dimensions -- which is what "arbitrary rotation" means.
                de = pt[0] - f.centre[0]
                dn = pt[1] - f.centre[1]
                # The corner's own angle within the unrotated box, so the box
                # does not jump to put the corner under the pointer.
                base = math.degrees(math.atan2(f.width_m / 2, f.length_m / 2))
                offset = {0: -base, 1: -180 + base, 2: 180 - base,
                          3: base}.get(idx, 0.0)
                f.rotation_deg = (math.degrees(math.atan2(de, dn))
                                  - offset) % 360.0
            elif role == "corner":
                # Resize about the opposite corner, keeping it a rectangle.
                opp = f.corners()[(idx + 2) % 4]
                # Work in the box's own frame so a rotated box resizes along
                # its own axes rather than along north and east.
                ce, cn = (pt[0] - opp[0], pt[1] - opp[1])
                a = math.radians(-f.rotation_deg)
                le = ce * math.cos(a) + cn * math.sin(a)
                ln = -ce * math.sin(a) + cn * math.cos(a)
                width = max(P.MIN_RECT_M, abs(le))
                length = max(P.MIN_RECT_M, abs(ln))
                f.length_m, f.width_m = length, width
                mid_local = (le / 2.0, ln / 2.0)
                a2 = math.radians(f.rotation_deg)
                f.centre = (opp[0] + mid_local[0] * math.cos(a2)
                            + mid_local[1] * math.sin(a2),
                            opp[1] - mid_local[0] * math.sin(a2)
                            + mid_local[1] * math.cos(a2))
        elif isinstance(f, P.Circle):
            if role == "centre":
                f.centre = pt
            else:
                f.radius_m = max(0.25, math.dist(f.centre, pt))
        elif isinstance(f, (P.Line, P.Polygon)) and role == "vertex":
            if 0 <= idx < len(f.points):
                f.points[idx] = pt
        f.touch()
        self.status = _describe(f, f.measurements())
        self.map.draw()

    # ------------------------------------------------------------------
    #  hit testing
    # ------------------------------------------------------------------

    def hit_test(self, x: float, y: float):
        """The topmost feature under the pointer, or None."""
        if self.plan is None:
            return None
        for f in reversed(self.plan.features):
            if f.hidden:
                continue
            pts = [self.map.xy(la, lo) for la, lo in f.geo_points()]
            pts = [p for p in pts if p]
            if len(pts) < 2:
                continue
            if f.closed() and _inside(pts, (x, y)):
                return f
            for i in range(len(pts) - 1):
                if _near_segment(pts[i], pts[i + 1], (x, y), GRAB_PX):
                    return f
            if f.closed() and _near_segment(pts[-1], pts[0], (x, y), GRAB_PX):
                return f
        return None

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def draw(self) -> None:
        """Everything the editor adds on top of the map.

        Called from `MapCanvas.draw` while its projection cache is warm, so
        the many `xy` calls here cost one canvas measurement between them
        rather than one each.
        """
        if self.plan is None:
            return
        for f in self.plan.features:
            if not f.hidden:
                self._draw_feature(f, selected=f.id == self.selected_id)
        self._draw_draft()

    def _draw_feature(self, f, *, selected: bool) -> None:
        c = self.map.canvas
        pts = [self.map.xy(la, lo) for la, lo in f.geo_points()]
        pts = [p for p in pts if p]
        if len(pts) < 2:
            return
        colour = _hex(T.ACCENT if selected else T.HEADING)
        width = 3 if selected else 2

        if isinstance(f, P.Grid):
            self._draw_grid(f, pts, colour, selected)
        elif f.closed():
            flat = [v for p in pts for v in p]
            c.create_polygon(*flat, outline=colour, fill="", width=width)
        else:
            flat = [v for p in pts for v in p]
            c.create_line(*flat, fill=colour, width=width, capstyle="round")
            self._draw_direction(pts, colour)

        label = f.name or f.kind
        m = f.measurements()
        if "area_m2" in m:
            label += f"  {m['area_m2']:,.0f} m²"
        elif m.get("length_m"):
            label += f"  {m['length_m']:.1f} m"
        c.create_text(pts[0][0] + 8, pts[0][1] - 10, text=label, anchor="w",
                      fill=colour, font=T.FONT_SMALL)

        if selected:
            self._draw_dimensions(f, pts)
            for hx, hy, role, _i in self.handles(f):
                fill = _hex(T.OK) if role == "centre" else _hex(T.ACCENT)
                c.create_rectangle(hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                                   hx + HANDLE_PX / 2, hy + HANDLE_PX / 2,
                                   fill=fill, outline=_hex(T.BG))
        if f.locked:
            c.create_text(pts[0][0] + 8, pts[0][1] + 6, text="locked",
                          anchor="w", fill=_hex(T.TEXT_MUTED),
                          font=T.FONT_SMALL)

    def _draw_grid(self, g, corner_pts, colour: str, selected: bool) -> None:
        c = self.map.canvas
        flat = [v for p in corner_pts for v in p]
        c.create_polygon(*flat, outline=colour, fill="", width=3 if selected else 2,
                         dash=(6, 3))
        lanes = g.lanes() + g.second_pass_lanes()
        for lane in lanes:
            a = self._to_screen(g.anchor, *lane.start)
            b = self._to_screen(g.anchor, *lane.end)
            if not a or not b:
                continue
            done = lane.state == "done"
            skipped = lane.state == "skipped"
            lane_colour = (_hex(T.OK) if done else
                           _hex(T.TEXT_MUTED) if skipped else colour)
            c.create_line(a[0], a[1], b[0], b[1], fill=lane_colour,
                          width=2, dash=(2, 4) if skipped else None)
            # Direction arrow at the far end, so the travel order is visible.
            self._arrow(a, b, lane_colour)
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            c.create_text(mid[0], mid[1] - 7, text=str(lane.index),
                          fill=lane_colour, font=T.FONT_SMALL)
        # Turn legs, dotted and visibly not survey lines.
        for pa, pb in g.transits():
            a = self._to_screen(g.anchor, *pa)
            b = self._to_screen(g.anchor, *pb)
            if a and b:
                c.create_line(a[0], a[1], b[0], b[1], fill=_hex(T.TEXT_MUTED),
                              width=1, dash=(1, 4))

    def _arrow(self, a, b, colour: str) -> None:
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        for s in (-1, 1):
            self.map.canvas.create_line(
                b[0], b[1],
                b[0] - 9 * math.cos(ang + s * 0.5),
                b[1] - 9 * math.sin(ang + s * 0.5), fill=colour, width=2)

    def _draw_direction(self, pts, colour: str) -> None:
        if len(pts) >= 2:
            self._arrow(pts[-2], pts[-1], colour)

    def _draw_dimensions(self, f, pts) -> None:
        """Numbers on the edges, so the shape says what it is while it is
        being shaped rather than only in the inspector.

        Each label is drawn only where there is room for it. The numbers come
        from the geometry, but whether they *fit* is a question about pixels,
        and answering it wrongly costs more than the labels are worth: five of
        them inside seventy pixels is not a small diagram, it is an
        unreadable one that hides the shape it is describing.
        """
        c = self.map.canvas
        m = f.measurements()
        if isinstance(f, (P.Rect, P.Grid)):
            corners = pts[:4]
            edges = [_px(corners[i], corners[(i + 1) % 4]) for i in range(4)]
            for i in range(4):
                if edges[i] < LABEL_MIN_PX:
                    continue
                a, b = corners[i], corners[(i + 1) % 4]
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                # `corners()` runs (-hw,-hl), (-hw,hl), (hw,hl),
                # (hw,-hl), so the even edges span the length and the
                # odd ones the width. These were the other way round,
                # which put "20 m" along a 30 m edge -- invisible
                # while both labels were drawn and the box was square
                # on screen, obvious the moment only one of them fits.
                metres = f.length_m if i % 2 == 0 else f.width_m
                c.create_text(mid[0], mid[1], text=f"{metres:.1f} m",
                              fill=_hex(T.TEXT), font=T.FONT_SMALL)
            lines = 3 if isinstance(f, P.Grid) else 2
            if min(edges) < BLOCK_LINE_PX * lines:
                return
            cx = sum(p[0] for p in corners) / 4
            cy = sum(p[1] for p in corners) / 4
            extra = ""
            if isinstance(f, P.Grid):
                extra = f"\n{m['lane_count']} lanes @ {_sp(m)}"
            c.create_text(cx, cy,
                          text=f"{m['area_m2']:,.0f} m²\n"
                               f"{m['rotation_deg']:.0f}°T{extra}",
                          fill=_hex(T.TEXT), font=T.FONT_SMALL,
                          justify="center")
        elif isinstance(f, P.Line):
            for i, (length, brg) in enumerate(f.segments()):
                a, b = pts[i], pts[i + 1]
                if _px(a, b) < LABEL_MIN_PX:
                    continue
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                c.create_text(mid[0], mid[1] - 9,
                              text=f"{length:.1f} m  {brg:.0f}°T",
                              fill=_hex(T.TEXT), font=T.FONT_SMALL)
        elif isinstance(f, P.Circle):
            at = self._to_screen(f.anchor, *f.centre)
            if at and pts and _px(at, pts[0]) >= LABEL_MIN_PX / 2:
                c.create_text(at[0], at[1] - 10,
                              text=f"r {f.radius_m:.1f} m · "
                                   f"{m['area_m2']:,.0f} m²",
                              fill=_hex(T.TEXT), font=T.FONT_SMALL)

    def _draw_draft(self) -> None:
        """The shape being drawn right now, with its live dimensions."""
        if not self._draft or self._draft_anchor is None:
            return
        c = self.map.canvas
        a = self._draft_anchor
        colour = _hex(T.ACCENT)
        pts = [self._to_screen(a, e, n) for e, n in self._draft]
        pts = [p for p in pts if p]
        cur = (self._to_screen(a, *self._cursor) if self._cursor else None)

        if self.tool in ("rect", "grid") and pts and cur:
            e0, n0 = self._draft[0]
            e1, n1 = self._cursor
            box = [self._to_screen(a, e0, n0), self._to_screen(a, e1, n0),
                   self._to_screen(a, e1, n1), self._to_screen(a, e0, n1)]
            if all(box):
                flat = [v for p in box for v in p]
                c.create_polygon(*flat, outline=colour, fill="", width=2,
                                 dash=(5, 3))
                width = abs(e1 - e0)
                length = abs(n1 - n0)
                for i in range(4):
                    p, q = box[i], box[(i + 1) % 4]
                    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                    c.create_text(mid[0], mid[1],
                                  text=f"{(width if i % 2 == 0 else length):.1f} m",
                                  fill=colour, font=T.FONT_SMALL)
                cx = sum(p[0] for p in box) / 4
                cy = sum(p[1] for p in box) / 4
                note = f"{width * length:,.0f} m²"
                if self.tool == "grid" and self.grid_spacing_m > 0:
                    across = width if self.grid_axis == "length" else length
                    n = max(1, math.ceil(across / self.grid_spacing_m - 1e-9))
                    note += f"\n{n} lanes @ {self.grid_spacing_m:g} m"
                c.create_text(cx, cy, text=note, fill=colour,
                              font=T.FONT_SMALL, justify="center")
            return

        if self.tool == "circle" and pts and cur:
            r = math.dist(pts[0], cur)
            c.create_oval(pts[0][0] - r, pts[0][1] - r, pts[0][0] + r,
                          pts[0][1] + r, outline=colour, width=2, dash=(5, 3))
            metres = math.dist(self._draft[0], self._cursor)
            c.create_text(pts[0][0], pts[0][1] - 12,
                          text=f"r {metres:.1f} m · {math.pi * metres ** 2:,.0f} m²",
                          fill=colour, font=T.FONT_SMALL)
            return

        chain = pts + ([cur] if cur else [])
        if len(chain) >= 2:
            flat = [v for p in chain for v in p]
            c.create_line(*flat, fill=colour, width=2, dash=(5, 3))
            if self._cursor is not None:
                last = self._draft[-1]
                d = math.dist(last, self._cursor)
                brg = math.degrees(math.atan2(self._cursor[0] - last[0],
                                              self._cursor[1] - last[1])) % 360
                total = P.path_length(self._draft + [self._cursor])
                text = f"{d:.1f} m  {brg:.0f}°T"
                if len(self._draft) > 1:
                    text += f"   total {total:.1f} m"
                c.create_text(chain[-1][0] + 10, chain[-1][1] - 10, text=text,
                              anchor="w", fill=colour, font=T.FONT_SMALL)
        for p in pts:
            c.create_oval(p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3,
                          fill=colour, outline="")


def _sp(m: dict) -> str:
    eff = m.get("effective_spacing_m")
    want = m.get("requested_spacing_m")
    if eff is None:
        return f"{want:g} m"
    if abs(eff - (want or 0)) < 0.005:
        return f"{want:g} m"
    return f"{eff:.2f} m (asked {want:g})"


def _px(a, b) -> float:
    """Screen distance between two canvas points, in pixels."""
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _describe(f, m: dict) -> str:
    if isinstance(f, P.Grid):
        return (f"{f.name}: {m['length_m']:.1f} × {m['width_m']:.1f} m, "
                f"{m['area_m2']:,.0f} m², {m['lane_count']} lanes at "
                f"{_sp(m)}")
    if isinstance(f, P.Rect):
        return (f"{f.name}: {m['length_m']:.1f} × {m['width_m']:.1f} m, "
                f"{m['area_m2']:,.0f} m², {m['rotation_deg']:.0f}°T")
    if isinstance(f, P.Circle):
        return f"{f.name}: r {m['radius_m']:.1f} m, {m['area_m2']:,.0f} m²"
    if "area_m2" in m:
        return f"{f.name}: {m['area_m2']:,.0f} m²"
    brg = m.get("bearing_deg")
    return (f"{f.name}: {m.get('length_m', 0):.1f} m"
            + (f" at {brg:.0f}°T" if brg is not None else ""))


def _inside(pts, at) -> bool:
    x, y = at
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def _near_segment(a, b, at, tol: float) -> bool:
    ax, ay = a
    bx, by = b
    px, py = at
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.dist(a, at) <= tol
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.dist((ax + t * dx, ay + t * dy), at) <= tol


def _hex(colour) -> str:
    import customtkinter as ctk
    if isinstance(colour, (tuple, list)):
        mode = ctk.get_appearance_mode()
        return colour[1] if str(mode).lower() == "dark" else colour[0]
    return colour
