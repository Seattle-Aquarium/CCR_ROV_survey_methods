"""
Survey plans: measured lines, rotated rectangles, and the lanes that fill them.

What an operator is doing at the dock is laying out where the ROV will fly:
"a 30 m line on 125 true from the pier corner", "a 30 by 20 box, lanes every
2 m". This module is that geometry, with no widget in it, so the arithmetic
can be tested against an independent calculation rather than against a
screenshot.

**Metres are computed in a local frame, never from pixels.** Every feature
carries an anchor, and all of its geometry is held as east/north metres from
that anchor. Converting to and from latitude and longitude happens at the
edges, through the geodesic helpers. Over the few hundred metres a survey
covers, a local tangent plane is accurate to well under a centimetre, and it
makes rotation, rectangles and lane clipping ordinary Cartesian arithmetic
instead of spherical trigonometry. Doing it the other way -- measuring on
screen -- would make a plan's dimensions depend on the zoom it was drawn at,
which is the one thing they must never do.

**A rectangle stays a rectangle.** It is stored as a centre, a length, a
width and a rotation, not as four corner points that a drag can shear. The
corners are derived. The same applies to a grid: it keeps the rectangle and
the lane settings, so an operator can change the spacing afterwards and get
new lanes rather than a frozen set of lines.

**Lanes are not coverage.** Flying the centre line of a lane does not survey
the strip either side of it unless something says how wide the camera sees,
and nothing here knows that. `coverage()` returns "not established" until an
effective swath width is supplied, and says so.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import geo

SCHEMA = "ccr.nav.plan/1"
FILENAME = "survey_plan.json"

#: Two points closer than this are the same point as far as a plan is
#: concerned -- a double-click, or a drag that never moved.
MIN_VERTEX_M = 0.05

#: A rectangle smaller than this in either dimension is a mis-drag, not a
#: survey area. Kept small enough that a 1 m inspection box is still allowed.
MIN_RECT_M = 0.5

#: Lane spacings outside this are refused with a reason. The lower bound is
#: where lane count explodes; the upper is wider than any rectangle this
#: program is used to draw.
MIN_SPACING_M = 0.25
MAX_SPACING_M = 500.0


# --------------------------------------------------------------------------
#  The local frame
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """The point a feature's local metres are measured from.

    Every feature has one, and it is stored with the feature rather than with
    the plan, so moving one feature cannot silently reinterpret another.
    """

    lat: float
    lon: float

    def to_local(self, lat: float, lon: float) -> tuple[float, float]:
        """(east, north) metres from the anchor."""
        m_lat, m_lon = geo.metres_per_degree(self.lat)
        return (geo.wrap180(lon - self.lon) * m_lon, (lat - self.lat) * m_lat)

    def to_geo(self, east: float, north: float) -> tuple[float, float]:
        """Back to (lat, lon).

        Through the geodesic direct solution rather than the linear inverse of
        `to_local`, so a long line does not accumulate the flat-earth error
        that the linear form has at its far end.
        """
        return geo.offset_ned(self.lat, self.lon, north, east)


def _rotate(east: float, north: float, deg: float) -> tuple[float, float]:
    """Rotate a local vector clockwise by `deg`, the way a bearing turns.

    Bearings increase clockwise from north; mathematical angles increase
    anticlockwise from east. This is the one place that difference is handled,
    and getting it backwards mirrors every rotated rectangle.
    """
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return (east * ca + north * sa, -east * sa + north * ca)


def _bearing_of(de: float, dn: float) -> float:
    """The bearing of a local vector, degrees true clockwise from north."""
    return geo.wrap360(math.degrees(math.atan2(de, dn)))


def polygon_area(points) -> float:
    """Area in square metres of a closed local-frame polygon.

    The shoelace formula, absolute value so winding order does not matter.
    In a local tangent frame this is the true ground area to within the
    projection error, which over a survey box is negligible.
    """
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        e1, n1 = points[i]
        e2, n2 = points[(i + 1) % n]
        total += e1 * n2 - e2 * n1
    return abs(total) / 2.0


def path_length(points) -> float:
    return sum(math.dist(points[i], points[i + 1])
               for i in range(len(points) - 1))


# --------------------------------------------------------------------------
#  Features
# --------------------------------------------------------------------------


@dataclass
class Feature:
    """Anything drawn on a plan.

    `revision` counts edits, so a plan flown and then changed can still say
    which version the flight followed.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    kind: str = "line"
    anchor: Anchor = field(default_factory=lambda: Anchor(0.0, 0.0))
    note: str = ""
    locked: bool = False
    hidden: bool = False
    revision: int = 1
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"))

    # -- geometry, in the local frame -------------------------------------

    def local_points(self) -> list:
        raise NotImplementedError

    def geo_points(self) -> list:
        """[(lat, lon), ...] for drawing and export."""
        return [self.anchor.to_geo(e, n) for e, n in self.local_points()]

    def closed(self) -> bool:
        """Is this feature an area rather than a path?

        A grid counts: its `local_points` are the four corners of its
        rectangle, and exporting those as an open LineString would land in
        QGIS as a line rather than the survey area it is. The lanes inside it
        travel in `properties`, not in the boundary's geometry.
        """
        return self.kind in ("rect", "polygon", "circle", "grid")

    # -- what the inspector shows -----------------------------------------

    def measurements(self) -> dict:
        pts = self.local_points()
        out = {"points": len(pts)}
        if self.closed() and len(pts) >= 3:
            out["area_m2"] = polygon_area(pts)
            out["perimeter_m"] = path_length(pts + [pts[0]])
        else:
            out["length_m"] = path_length(pts)
        return out

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "anchor": [self.anchor.lat, self.anchor.lon],
                "note": self.note, "locked": self.locked,
                "hidden": self.hidden, "revision": self.revision,
                "created": self.created}

    def touch(self) -> None:
        self.revision += 1


@dataclass
class Line(Feature):
    """A measured line or polyline.

    Two points is the common case -- "30 m on 125 true" -- and the same type
    carries a many-vertex polyline, because an operator who adds a vertex to a
    line should not find it has become a different kind of thing.
    """

    kind: str = "line"
    #: [(east, north), ...] metres from the anchor.
    points: list = field(default_factory=list)

    def local_points(self) -> list:
        return list(self.points)

    @classmethod
    def from_bearing(cls, lat: float, lon: float, bearing_deg: float,
                     length_m: float, **kw) -> Line:
        """The way an operator states a line: start here, go that way, so far."""
        a = Anchor(lat, lon)
        e = length_m * math.sin(math.radians(bearing_deg))
        n = length_m * math.cos(math.radians(bearing_deg))
        return cls(anchor=a, points=[(0.0, 0.0), (e, n)], **kw)

    # -- measurements ------------------------------------------------------

    @property
    def length_m(self) -> float:
        return path_length(self.points)

    @property
    def bearing_deg(self) -> float | None:
        """The bearing of the whole line, start to end. None if degenerate."""
        if len(self.points) < 2:
            return None
        de = self.points[-1][0] - self.points[0][0]
        dn = self.points[-1][1] - self.points[0][1]
        if math.hypot(de, dn) < MIN_VERTEX_M:
            return None
        return _bearing_of(de, dn)

    def segments(self) -> list:
        """[(length_m, bearing_deg), ...] for each leg of a polyline."""
        out = []
        for i in range(len(self.points) - 1):
            de = self.points[i + 1][0] - self.points[i][0]
            dn = self.points[i + 1][1] - self.points[i][1]
            out.append((math.hypot(de, dn), _bearing_of(de, dn)))
        return out

    def measurements(self) -> dict:
        segs = self.segments()
        return {"points": len(self.points), "length_m": self.length_m,
                "bearing_deg": self.bearing_deg, "segments": len(segs),
                "segment_lengths_m": [s[0] for s in segs]}

    # -- editing -----------------------------------------------------------

    def set_length_bearing(self, length_m: float, bearing_deg: float,
                           *, fix: str = "start") -> None:
        """Retype a two-point line exactly, holding one end still.

        `fix` is "start" or "end": which endpoint stays where it is. An
        operator adjusting a line off a pier corner needs the corner end
        pinned; one adjusting the far end needs the opposite.
        """
        if len(self.points) != 2:
            raise ValueError("only a two-point line has a single length and "
                             "bearing; edit a polyline vertex by vertex")
        if length_m <= 0:
            raise ValueError("a line needs a positive length")
        e = length_m * math.sin(math.radians(bearing_deg))
        n = length_m * math.cos(math.radians(bearing_deg))
        if fix == "start":
            s = self.points[0]
            self.points = [s, (s[0] + e, s[1] + n)]
        else:
            t = self.points[1]
            self.points = [(t[0] - e, t[1] - n), t]
        self.touch()

    def reverse(self) -> None:
        self.points = list(reversed(self.points))
        self.touch()

    def parallels(self, count: int, spacing_m: float, *,
                  side: str = "right") -> list:
        """`count` copies offset perpendicular to this line.

        Offsets are perpendicular to the line's overall direction, so a
        polyline's copies stay parallel to its chord rather than following
        each wiggle -- which is what "run three more lines 2 m apart" means.
        """
        brg = self.bearing_deg
        if brg is None:
            raise ValueError("a line with no direction has no parallels")
        if count < 1:
            raise ValueError("count must be at least 1")
        if not (MIN_SPACING_M <= spacing_m <= MAX_SPACING_M):
            raise ValueError(f"spacing must be between {MIN_SPACING_M} and "
                             f"{MAX_SPACING_M} m")
        sign = 1.0 if side == "right" else -1.0
        # Perpendicular to the line, in the local frame.
        pe = math.sin(math.radians(brg + 90.0)) * sign
        pn = math.cos(math.radians(brg + 90.0)) * sign
        out = []
        for i in range(1, count + 1):
            d = spacing_m * i
            out.append(Line(
                name=f"{self.name or 'line'} +{i}", anchor=self.anchor,
                points=[(e + pe * d, n + pn * d) for e, n in self.points]))
        return out

    def to_json(self) -> dict:
        return {**super().to_json(),
                "points": [[round(e, 4), round(n, 4)] for e, n in self.points]}


@dataclass
class Rect(Feature):
    """A rotated rectangle, stored so it cannot stop being one.

    Centre, length, width and rotation -- not four corners. A handle drag
    recomputes those four numbers, so no sequence of edits can shear it into a
    parallelogram, which is exactly what storing corners allows.

    `rotation_deg` is the bearing of the **length** axis, degrees true.
    """

    kind: str = "rect"
    centre: tuple = (0.0, 0.0)          # local (east, north)
    length_m: float = 0.0               # along the rotation axis
    width_m: float = 0.0                # across it
    rotation_deg: float = 0.0

    @classmethod
    def from_corners(cls, anchor: Anchor, a: tuple, b: tuple,
                     rotation_deg: float = 0.0, **kw) -> Rect:
        """An axis-aligned drag from `a` to `b`, then rotated about its centre."""
        ce = (a[0] + b[0]) / 2.0
        cn = (a[1] + b[1]) / 2.0
        return cls(anchor=anchor, centre=(ce, cn),
                   width_m=abs(b[0] - a[0]), length_m=abs(b[1] - a[1]),
                   rotation_deg=rotation_deg, **kw)

    def corners(self) -> list:
        """The four corners, local frame, clockwise from the near-left."""
        hl, hw = self.length_m / 2.0, self.width_m / 2.0
        base = [(-hw, -hl), (-hw, hl), (hw, hl), (hw, -hl)]
        out = []
        for e, n in base:
            re, rn = _rotate(e, n, self.rotation_deg)
            out.append((self.centre[0] + re, self.centre[1] + rn))
        return out

    def local_points(self) -> list:
        return self.corners()

    @property
    def area_m2(self) -> float:
        return self.length_m * self.width_m

    def measurements(self) -> dict:
        return {"length_m": self.length_m, "width_m": self.width_m,
                "area_m2": self.area_m2, "rotation_deg": self.rotation_deg,
                "perimeter_m": 2 * (self.length_m + self.width_m)}

    def set_size(self, length_m: float, width_m: float, *,
                 anchor_corner: int | None = None) -> None:
        """Retype the dimensions exactly.

        `anchor_corner` is an index into `corners()` that stays put; None
        keeps the centre still. Pinning a corner is what an operator wants
        when the box is registered to a pier edge.
        """
        if length_m < MIN_RECT_M or width_m < MIN_RECT_M:
            raise ValueError(f"a rectangle must be at least {MIN_RECT_M} m "
                             f"in each direction")
        if anchor_corner is None:
            self.length_m, self.width_m = length_m, width_m
            self.touch()
            return
        keep = self.corners()[anchor_corner % 4]
        self.length_m, self.width_m = length_m, width_m
        moved = self.corners()[anchor_corner % 4]
        self.centre = (self.centre[0] + keep[0] - moved[0],
                       self.centre[1] + keep[1] - moved[1])
        self.touch()

    def to_json(self) -> dict:
        return {**super().to_json(),
                "centre": [round(self.centre[0], 4), round(self.centre[1], 4)],
                "length_m": round(self.length_m, 4),
                "width_m": round(self.width_m, 4),
                "rotation_deg": round(self.rotation_deg, 4)}


@dataclass
class Circle(Feature):
    kind: str = "circle"
    centre: tuple = (0.0, 0.0)
    radius_m: float = 0.0

    def local_points(self, steps: int = 72) -> list:
        return [(self.centre[0] + self.radius_m * math.sin(2 * math.pi * i / steps),
                 self.centre[1] + self.radius_m * math.cos(2 * math.pi * i / steps))
                for i in range(steps)]

    def measurements(self) -> dict:
        return {"radius_m": self.radius_m, "diameter_m": self.radius_m * 2,
                "area_m2": math.pi * self.radius_m ** 2,
                "perimeter_m": 2 * math.pi * self.radius_m}

    def to_json(self) -> dict:
        return {**super().to_json(),
                "centre": [round(self.centre[0], 4), round(self.centre[1], 4)],
                "radius_m": round(self.radius_m, 4)}


@dataclass
class Polygon(Feature):
    kind: str = "polygon"
    points: list = field(default_factory=list)

    def local_points(self) -> list:
        return list(self.points)

    def measurements(self) -> dict:
        pts = self.points
        return {"points": len(pts), "area_m2": polygon_area(pts),
                "perimeter_m": path_length(pts + pts[:1])}

    def to_json(self) -> dict:
        return {**super().to_json(),
                "points": [[round(e, 4), round(n, 4)] for e, n in self.points]}


# --------------------------------------------------------------------------
#  Survey grids
# --------------------------------------------------------------------------


@dataclass
class Lane:
    """One run of a survey grid, with where it is and whether it is done."""

    index: int
    #: Local-frame endpoints, in the order they are to be flown.
    start: tuple
    end: tuple
    #: "done", "skipped", "" -- the operator's own record.
    state: str = ""

    @property
    def length_m(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def bearing_deg(self) -> float:
        return _bearing_of(self.end[0] - self.start[0],
                           self.end[1] - self.start[1])


@dataclass
class Grid(Feature):
    """A rectangle plus the back-and-forth lanes that fill it.

    The rectangle is kept, not flattened into lines, so spacing can be changed
    afterwards and new lanes generated. Lane state -- done, skipped -- lives
    here too, because resuming an interrupted grid is the normal case, not the
    exception.

    **The edge-offset rule**, which has to be stated because every choice of
    it is defensible and they give different answers:

        Lanes are inset half a spacing from each edge, and the remainder is
        distributed evenly between them. So for a width W and requested
        spacing S the lane count is `max(1, ceil(W / S))` and the *effective*
        spacing is `(W - S) / (count - 1)`, which is never more than the
        requested spacing.

    That means a width not divisible by the spacing gets slightly tighter
    lanes rather than a bare strip along one edge, and `effective_spacing_m`
    reports what was actually used. Asking for 2 m in a 5 m width gives three
    lanes at 1.5 m, not two at 2 m with a metre unswept.
    """

    kind: str = "grid"
    centre: tuple = (0.0, 0.0)
    length_m: float = 0.0
    width_m: float = 0.0
    rotation_deg: float = 0.0
    #: Requested lane spacing, across the width.
    spacing_m: float = 2.0
    #: "length" runs lanes along the length axis; "width" across it.
    lane_axis: str = "length"
    #: Which corner the first lane starts at: 0-3 into `corners()`.
    start_corner: int = 0
    #: Reverse alternate lanes, which is what an ROV actually flies.
    boustrophedon: bool = True
    #: A second pass at right angles to the first.
    second_pass: bool = False
    #: index -> "done" / "skipped".
    lane_state: dict = field(default_factory=dict)

    # -- the rectangle -----------------------------------------------------

    def as_rect(self) -> Rect:
        return Rect(anchor=self.anchor, centre=self.centre,
                    length_m=self.length_m, width_m=self.width_m,
                    rotation_deg=self.rotation_deg)

    def corners(self) -> list:
        return self.as_rect().corners()

    def local_points(self) -> list:
        return self.corners()

    @property
    def area_m2(self) -> float:
        return self.length_m * self.width_m

    # -- the lanes ---------------------------------------------------------

    def lane_count(self) -> int:
        """How many lanes, chosen so the spacing is never *exceeded*.

        With a half-spacing inset at each edge, the centres span
        ``across - S`` and the gap between adjacent lanes is
        ``(across - S) / (n - 1)``. Requiring that to be at most the requested
        ``S`` gives ``n >= across / S``, so the count is the **ceiling**.

        It was `round` first, and that was wrong twice over: Python rounds
        half to even, so a 5 m width at 2 m spacing gave `round(2.5) = 2`
        lanes at 3 m apart -- both fewer lanes than asked for and a spacing
        wider than requested, silently. A survey planned at 2 m that flies at
        3 m has a strip nobody looked at.
        """
        across = self.width_m if self.lane_axis == "length" else self.length_m
        if across <= 0 or self.spacing_m <= 0:
            return 0
        return max(1, math.ceil(across / self.spacing_m - 1e-9))

    def effective_spacing_m(self) -> float | None:
        """The gap actually used, which is never more than requested.

        None for a single lane, where there is no gap between lanes to state.
        """
        n = self.lane_count()
        across = self.width_m if self.lane_axis == "length" else self.length_m
        if n <= 1:
            return None
        return (across - min(self.spacing_m, across)) / (n - 1)

    def lanes(self) -> list:
        """Every lane, clipped to the rectangle, in the order to be flown."""
        n = self.lane_count()
        if n <= 0:
            return []
        along = self.length_m if self.lane_axis == "length" else self.width_m
        across = self.width_m if self.lane_axis == "length" else self.length_m
        half_along, half_across = along / 2.0, across / 2.0

        inset = min(self.spacing_m / 2.0, across / 2.0)
        if n == 1:
            offsets = [0.0]
        else:
            step = (across - 2 * inset) / (n - 1)
            offsets = [-half_across + inset + step * i for i in range(n)]

        # The rotation that takes local "along/across" into the plan frame.
        # When lanes run across the width the axes swap, which is one extra
        # right angle rather than a second code path.
        rot = self.rotation_deg + (0.0 if self.lane_axis == "length" else 90.0)

        # start_corner selects which end the first lane begins at, and which
        # side the offsets start from.
        flip_along = self.start_corner in (1, 2)
        flip_across = self.start_corner in (2, 3)
        if flip_across:
            offsets = list(reversed(offsets))

        out: list[Lane] = []
        for i, off in enumerate(offsets):
            a_along = -half_along
            b_along = half_along
            if flip_along:
                a_along, b_along = b_along, a_along
            if self.boustrophedon and i % 2 == 1:
                a_along, b_along = b_along, a_along
            pts = []
            for al in (a_along, b_along):
                e, nn = _rotate(off, al, rot)
                pts.append((self.centre[0] + e, self.centre[1] + nn))
            out.append(Lane(index=i + 1, start=pts[0], end=pts[1],
                            state=self.lane_state.get(str(i + 1), "")))
        return out

    def second_pass_lanes(self) -> list:
        if not self.second_pass:
            return []
        other = replace(self, lane_axis=("width" if self.lane_axis == "length"
                                         else "length"),
                        second_pass=False, lane_state={})
        lanes = other.lanes()
        for lane in lanes:
            lane.index += len(self.lanes())
        return lanes

    def transits(self) -> list:
        """The turn legs between lanes, as (start, end) local pairs.

        Suggestions, not routes: nothing here knows about the tether, the
        bottom or anything in the way. `transits_outside()` says which of them
        leave the rectangle, because those are the ones worth looking at.
        """
        lanes = self.lanes() + self.second_pass_lanes()
        return [(lanes[i].end, lanes[i + 1].start)
                for i in range(len(lanes) - 1)]

    def transits_outside(self) -> int:
        """How many turn legs leave the boundary. Disclosed, not hidden.

        A back-and-forth turn runs exactly *along* the end edge, so its
        midpoint sits on the boundary. Tested against the rectangle grown by
        a millimetre, so those count as inside -- a point-on-edge test that
        called every ordinary turn an excursion would make the number
        meaningless.
        """
        grown = replace(self, length_m=self.length_m + 0.002,
                        width_m=self.width_m + 0.002).corners()
        n = 0
        for a, b in self.transits():
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if not _point_in_polygon(mid, grown):
                n += 1
        return n

    # -- what the inspector shows ------------------------------------------

    def measurements(self) -> dict:
        lanes = self.lanes()
        extra = self.second_pass_lanes()
        survey = sum(x.length_m for x in lanes + extra)
        transit = sum(math.dist(a, b) for a, b in self.transits())
        return {
            "length_m": self.length_m, "width_m": self.width_m,
            "area_m2": self.area_m2, "rotation_deg": self.rotation_deg,
            "lane_count": len(lanes) + len(extra),
            "requested_spacing_m": self.spacing_m,
            "effective_spacing_m": self.effective_spacing_m(),
            "edge_offset_m": min(self.spacing_m / 2.0,
                                 (self.width_m if self.lane_axis == "length"
                                  else self.length_m) / 2.0),
            "survey_line_m": survey,
            "transit_m": transit,
            "total_path_m": survey + transit,
            "lane_lengths_m": [x.length_m for x in lanes + extra],
            "transits_outside_boundary": self.transits_outside(),
            "done": sum(1 for v in self.lane_state.values() if v == "done"),
            "skipped": sum(1 for v in self.lane_state.values()
                           if v == "skipped"),
        }

    def coverage(self, swath_m: float | None = None) -> dict:
        """What fraction of the box the lanes actually see.

        **Returns "not established" without a swath width**, because flying
        the centre line of a lane surveys nothing either side of it unless
        something says how wide the camera sees, and this module does not
        know. Supplying a swath is an explicit claim by the operator or by a
        camera-footprint model, and it is labelled as one.
        """
        if swath_m is None or swath_m <= 0:
            return {"established": False,
                    "note": "coverage not established — no effective swath "
                            "width has been given, and flying a lane's centre "
                            "line does not survey the strip either side of it"}
        eff = self.effective_spacing_m()
        if eff is None:
            eff = self.spacing_m
        overlap = swath_m - eff
        across = self.width_m if self.lane_axis == "length" else self.length_m
        inset = min(self.spacing_m / 2.0, across / 2.0)
        # The outermost lanes see half a swath beyond themselves; whatever is
        # left between that and the edge is not covered.
        edge_gap = max(0.0, inset - swath_m / 2.0)
        return {
            "established": True,
            "swath_m": swath_m,
            "effective_spacing_m": eff,
            "overlap_m": overlap,
            "overlap_fraction": (overlap / swath_m) if swath_m else 0.0,
            "gap_between_lanes_m": max(0.0, -overlap),
            "uncovered_edge_m": edge_gap,
            "note": ("planned coverage from a stated swath width; it is not "
                     "observed coverage and does not account for altitude, "
                     "attitude, visibility or what was actually flown"),
        }

    def set_lane_state(self, index: int, state: str) -> None:
        if state:
            self.lane_state[str(index)] = state
        else:
            self.lane_state.pop(str(index), None)
        self.touch()

    def to_json(self) -> dict:
        return {**super().to_json(),
                "centre": [round(self.centre[0], 4), round(self.centre[1], 4)],
                "length_m": round(self.length_m, 4),
                "width_m": round(self.width_m, 4),
                "rotation_deg": round(self.rotation_deg, 4),
                "spacing_m": round(self.spacing_m, 4),
                "lane_axis": self.lane_axis,
                "start_corner": self.start_corner,
                "boustrophedon": self.boustrophedon,
                "second_pass": self.second_pass,
                "lane_state": dict(self.lane_state)}


def _point_in_polygon(pt, poly) -> bool:
    """Ray casting. Used only to report transits that leave the boundary."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


# --------------------------------------------------------------------------
#  A plan
# --------------------------------------------------------------------------

_KINDS = {"line": Line, "rect": Rect, "circle": Circle, "polygon": Polygon,
          "grid": Grid}


@dataclass
class Plan:
    """A named, versioned set of features for one site.

    Saved next to the flight folder's other records. Autosaved on every edit,
    because an operator drawing a grid on a moving boat should not have to
    remember to.
    """

    name: str = "Survey plan"
    site: str = ""
    #: Where the plan's coordinates mean something. "wgs84" for a real site;
    #: "local" for one drawn against a dead-reckoned frame with no confirmed
    #: origin, which must not be exported as geography.
    crs: str = "wgs84"
    features: list = field(default_factory=list)
    revision: int = 1
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"))
    modified: str = ""
    schema: str = SCHEMA

    # -- editing ----------------------------------------------------------

    def add(self, feature: Feature) -> Feature:
        if not feature.name:
            feature.name = self._auto_name(feature.kind)
        self.features.append(feature)
        self.touch()
        return feature

    def remove(self, feature_id: str) -> Feature | None:
        for i, f in enumerate(self.features):
            if f.id == feature_id:
                self.touch()
                return self.features.pop(i)
        return None

    def get(self, feature_id: str) -> Feature | None:
        for f in self.features:
            if f.id == feature_id:
                return f
        return None

    def _auto_name(self, kind: str) -> str:
        stem = {"line": "Line", "rect": "Box", "grid": "Grid",
                "circle": "Circle", "polygon": "Area"}.get(kind, "Feature")
        n = sum(1 for f in self.features if f.kind == kind) + 1
        while any(f.name == f"{stem} {n}" for f in self.features):
            n += 1
        return f"{stem} {n}"

    def touch(self) -> None:
        self.revision += 1
        self.modified = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")

    # -- persistence -------------------------------------------------------

    def to_json(self) -> dict:
        return {"schema": self.schema, "name": self.name, "site": self.site,
                "crs": self.crs, "revision": self.revision,
                "created": self.created, "modified": self.modified,
                "features": [f.to_json() for f in self.features]}

    def save(self, path: Path) -> bool:
        """Write atomically. Returns whether it landed."""
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.to_json(), indent=1),
                           encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False

    @classmethod
    def load(cls, path: Path) -> Plan:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_json(data)

    @classmethod
    def from_json(cls, data: dict) -> Plan:
        plan = cls(name=data.get("name", "Survey plan"),
                   site=data.get("site", ""), crs=data.get("crs", "wgs84"),
                   revision=int(data.get("revision", 1)),
                   created=data.get("created", ""),
                   modified=data.get("modified", ""),
                   schema=data.get("schema", SCHEMA))
        for row in data.get("features", []):
            f = _feature_from_json(row)
            if f is not None:
                plan.features.append(f)
        return plan

    # -- export ------------------------------------------------------------

    def to_geojson(self) -> dict:
        """Standards-compliant GeoJSON, with the plan's own semantics kept
        in `properties` so a round trip does not flatten a grid into lines.

        Refuses a local-only plan: a set of metres from an unreferenced
        vehicle frame is not geography, and writing it out as latitude and
        longitude would invent a position it never had.
        """
        if self.crs != "wgs84":
            raise ValueError(
                "this plan is in a session-local frame with no confirmed "
                "transform to latitude and longitude. Confirm an origin "
                "before exporting it as geography.")
        feats = []
        for f in self.features:
            pts = f.geo_points()
            if not pts:
                continue
            props = {"name": f.name, "kind": f.kind, "id": f.id,
                     "note": f.note, "revision": f.revision,
                     **{k: v for k, v in f.measurements().items()
                        if isinstance(v, (int, float, str))}}
            if isinstance(f, Grid):
                props["plan"] = f.to_json()
                lanes = []
                for lane in f.lanes() + f.second_pass_lanes():
                    lanes.append({
                        "index": lane.index, "state": lane.state,
                        "coordinates": [
                            [round(lo, 8), round(la, 8)] for la, lo in
                            (f.anchor.to_geo(*lane.start),
                             f.anchor.to_geo(*lane.end))]})
                props["lanes"] = lanes
            elif isinstance(f, (Rect, Circle)):
                props["plan"] = f.to_json()
            if f.closed():
                ring = [[round(lo, 8), round(la, 8)] for la, lo in pts]
                ring.append(ring[0])
                geom = {"type": "Polygon", "coordinates": [ring]}
            else:
                geom = {"type": "LineString",
                        "coordinates": [[round(lo, 8), round(la, 8)]
                                        for la, lo in pts]}
            feats.append({"type": "Feature", "geometry": geom,
                          "properties": props})
        return {"type": "FeatureCollection",
                "properties": {"name": self.name, "site": self.site,
                               "schema": self.schema,
                               "revision": self.revision},
                "features": feats}


def _feature_from_json(row: dict) -> Feature | None:
    kind = row.get("kind")
    cls = _KINDS.get(kind)
    if cls is None:
        return None
    a = row.get("anchor") or [0.0, 0.0]
    common = dict(id=row.get("id") or uuid.uuid4().hex[:12],
                  name=row.get("name", ""), kind=kind,
                  anchor=Anchor(float(a[0]), float(a[1])),
                  note=row.get("note", ""), locked=bool(row.get("locked")),
                  hidden=bool(row.get("hidden")),
                  revision=int(row.get("revision", 1)),
                  created=row.get("created", ""))
    if cls is Line:
        return Line(**common, points=[tuple(p) for p in row.get("points", [])])
    if cls is Polygon:
        return Polygon(**common,
                       points=[tuple(p) for p in row.get("points", [])])
    if cls is Circle:
        return Circle(**common, centre=tuple(row.get("centre", (0, 0))),
                      radius_m=float(row.get("radius_m", 0)))
    if cls is Rect:
        return Rect(**common, centre=tuple(row.get("centre", (0, 0))),
                    length_m=float(row.get("length_m", 0)),
                    width_m=float(row.get("width_m", 0)),
                    rotation_deg=float(row.get("rotation_deg", 0)))
    if cls is Grid:
        return Grid(**common, centre=tuple(row.get("centre", (0, 0))),
                    length_m=float(row.get("length_m", 0)),
                    width_m=float(row.get("width_m", 0)),
                    rotation_deg=float(row.get("rotation_deg", 0)),
                    spacing_m=float(row.get("spacing_m", 2.0)),
                    lane_axis=row.get("lane_axis", "length"),
                    start_corner=int(row.get("start_corner", 0)),
                    boustrophedon=bool(row.get("boustrophedon", True)),
                    second_pass=bool(row.get("second_pass", False)),
                    lane_state=dict(row.get("lane_state") or {}))
    return None


# --------------------------------------------------------------------------
#  Undo
# --------------------------------------------------------------------------


class History:
    """Undo and redo for a plan, by keeping whole snapshots.

    Snapshots rather than inverse operations: a plan is a few kilobytes of
    JSON and an operator on a boat needs undo to be reliable far more than it
    needs it to be clever. Bounded, so a long editing session cannot grow
    without limit.
    """

    LIMIT = 60

    def __init__(self, plan: Plan) -> None:
        self._undo: list[dict] = [plan.to_json()]
        self._redo: list[dict] = []
        self.last_label = ""

    def record(self, plan: Plan, label: str = "") -> None:
        self._undo.append(plan.to_json())
        self.last_label = label
        if len(self._undo) > self.LIMIT:
            del self._undo[0]
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return len(self._undo) > 1

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> Plan | None:
        if not self.can_undo:
            return None
        self._redo.append(self._undo.pop())
        return Plan.from_json(self._undo[-1])

    def redo(self) -> Plan | None:
        if not self._redo:
            return None
        state = self._redo.pop()
        self._undo.append(state)
        return Plan.from_json(state)
