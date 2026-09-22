"""
Points the operator marks, captured at the instant the button goes down.

The one requirement that shapes everything here: **the coordinate is taken
when the button is pressed, not when the name dialog is answered.** An
operator marking a wolf eel den is looking at the wolf eel, not the screen;
by the time they have typed "wolf eel den" the ROV has moved five metres, and
a waypoint that records where the vehicle was when the typing finished is
worse than useless -- it is confidently wrong.

So `capture` takes the position and returns a saved waypoint immediately. The
rename that follows is an edit of something that already exists on disk, and
cancelling it keeps the point.

Everything about the fix's provenance is saved with it, because a coordinate
alone cannot be judged later. In DVL-only mode a position is dead-reckoned
from an origin and drifts as a whole; the same numbers from an acoustic fix
mean something different. `dead_reckoned` and the origin reference are part of
the record, not a display detail.

**These are survey annotations, not autopilot missions.** Nothing here is ever
uploaded to the vehicle. The distinction matters: a mission item is an
instruction and these are observations, and this program does not fly the ROV.
"""

from __future__ import annotations

import csv
import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import model as M
from .model import Fix

log = logging.getLogger(__name__)

FILENAME = "waypoints.json"
SCHEMA = "ccr.nav.waypoints/1"


class CaptureRefused(RuntimeError):
    """No position worth saving, with the reason to show the operator."""


@dataclass
class Waypoint:
    """One marked point, with everything needed to judge it later."""

    id: str
    name: str
    lat: float
    lon: float
    #: UTC ISO-8601, from this laptop's clock.
    created: str
    #: "rov" for the vehicle's own position, "map" for a point picked off the
    #: chart. They are different kinds of claim and are never merged.
    origin_of: str = "rov"
    #: "ekf", "dead", "acoustic", "manual" -- the fix's provenance.
    fix_kind: str = ""
    #: The quality the fix had at the moment of capture, verbatim.
    fix_quality: str = ""
    #: Seconds between the sample arriving and the button being pressed.
    fix_age_s: float | None = None
    source: str = ""
    #: True when the position was dead-reckoned. Drifts as a whole.
    dead_reckoned: bool = False
    #: The EKF origin the dead reckoning was referenced to, when there was one.
    origin: list | None = None
    #: The navigation profile in force.
    profile: str = ""
    session: str = ""
    depth_m: float | None = None
    altitude_m: float | None = None
    note: str = ""
    #: Set when the point was captured from an explicitly stale position.
    stale: bool = False

    def to_json(self) -> dict:
        from dataclasses import asdict
        return {k: v for k, v in asdict(self).items() if v is not None}


class WaypointStore:
    """Waypoints for one flight folder. Saved before success is reported.

    "Persist, then confirm" is the rule: a dialog that says Saved before the
    write has happened is a promise the program has not kept, and on a field
    laptop with a synchronised folder the write is exactly the thing that can
    fail.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.points: list[Waypoint] = []
        self.last_error = ""
        self.load()

    @property
    def path(self) -> Path:
        return self.folder / FILENAME

    # -- durability --------------------------------------------------------

    def load(self) -> None:
        self.points = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as ex:
            self.last_error = str(ex)
            log.warning("waypoints at %s could not be read: %s", self.path, ex)
            return
        rows = data.get("waypoints", data) if isinstance(data, dict) else data
        fields = set(Waypoint.__dataclass_fields__)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                self.points.append(
                    Waypoint(**{k: v for k, v in row.items() if k in fields}))
            except TypeError:
                continue
        log.info("loaded %d waypoint(s) from %s", len(self.points), self.path)

    def save(self) -> bool:
        """Write the whole set atomically. Returns whether it landed."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"schema": SCHEMA,
                 "waypoints": [w.to_json() for w in self.points]}, indent=1),
                encoding="utf-8")
            tmp.replace(self.path)
            self.last_error = ""
            return True
        except Exception as ex:
            self.last_error = str(ex)
            log.warning("waypoints could not be saved to %s: %s", self.path, ex)
            return False

    # -- capture -----------------------------------------------------------

    def capture(self, fix: Fix | None, *, name: str = "",
                allow_stale: bool = False, profile: str = "",
                session: str = "", origin: tuple[float, float] | None = None,
                depth_m: float | None = None, altitude_m: float | None = None,
                now_mono: float | None = None) -> Waypoint:
        """Save the ROV's position **as it is at this instant**.

        Raises `CaptureRefused` rather than saving a point nobody can trust.
        A stale position can still be captured deliberately -- sometimes the
        last known position is exactly what wants marking -- and is flagged as
        such rather than passed off as current.
        """
        if fix is None:
            raise CaptureRefused(
                "There is no ROV position to capture. The vehicle has not "
                "reported one, or the EKF has no origin.")
        if not M.valid_latlon(fix.lat, fix.lon):
            raise CaptureRefused(
                f"The ROV position ({fix.lat}, {fix.lon}) is not a usable "
                f"coordinate.")
        age = fix.age(now_mono)
        stale = fix.quality is not M.Quality.OK
        if stale and not allow_stale:
            raise CaptureRefused(
                f"The ROV position is {fix.quality.value}"
                + (f", {age:.0f} s old" if age is not None else "")
                + (f" — {fix.note}" if fix.note else "")
                + ". Capture the last known position deliberately if that is "
                  "what you want.")

        wp = Waypoint(
            id=uuid.uuid4().hex[:12],
            name=name or self._auto_name(),
            lat=round(float(fix.lat), 8), lon=round(float(fix.lon), 8),
            created=datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            origin_of="rov", fix_kind=fix.kind,
            fix_quality=fix.quality.value,
            fix_age_s=None if age is None else round(age, 2),
            source=fix.source.label(),
            dead_reckoned=fix.kind == "dead",
            origin=[origin[0], origin[1]] if origin else None,
            profile=profile, session=session,
            depth_m=None if depth_m is None else round(depth_m, 2),
            altitude_m=None if altitude_m is None else round(altitude_m, 2),
            stale=stale)
        self.points.append(wp)
        if not self.save():
            # It is in memory but not on disk, and the caller has to be able
            # to say so rather than show a tick.
            raise CaptureRefused(
                f"The waypoint was captured but could not be saved: "
                f"{self.last_error}")
        log.info("waypoint %s captured at %.6f, %.6f (%s)", wp.name, wp.lat,
                 wp.lon, wp.fix_kind)
        return wp

    def capture_map_point(self, lat: float, lon: float, *, name: str = "",
                          session: str = "") -> Waypoint:
        """A point picked off the chart. A different claim from the ROV's own.

        Kept distinct in `origin_of` because it is not a measurement of where
        anything was -- it is somewhere on a map that an operator pointed at.
        """
        if not M.valid_latlon(lat, lon):
            raise CaptureRefused(f"({lat}, {lon}) is not a usable coordinate.")
        wp = Waypoint(
            id=uuid.uuid4().hex[:12], name=name or self._auto_name("Map"),
            lat=round(float(lat), 8), lon=round(float(lon), 8),
            created=datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            origin_of="map", fix_kind="manual", fix_quality="ok",
            source="picked on the map", session=session)
        self.points.append(wp)
        if not self.save():
            raise CaptureRefused(f"Could not save: {self.last_error}")
        return wp

    def _auto_name(self, stem: str = "WP") -> str:
        n = sum(1 for w in self.points if w.name.startswith(stem)) + 1
        while any(w.name == f"{stem} {n:03d}" for w in self.points):
            n += 1
        return f"{stem} {n:03d}"

    # -- editing -----------------------------------------------------------

    def rename(self, wp_id: str, name: str) -> bool:
        for i, w in enumerate(self.points):
            if w.id == wp_id:
                self.points[i] = replace(w, name=name.strip() or w.name)
                return self.save()
        return False

    def annotate(self, wp_id: str, note: str) -> bool:
        for i, w in enumerate(self.points):
            if w.id == wp_id:
                self.points[i] = replace(w, note=note)
                return self.save()
        return False

    def delete(self, wp_id: str) -> bool:
        before = len(self.points)
        self.points = [w for w in self.points if w.id != wp_id]
        return self.save() if len(self.points) != before else False

    # -- export ------------------------------------------------------------

    def to_geojson(self) -> dict:
        """GeoJSON, which QGIS, ArcGIS and R all read without a converter.

        Coordinates are `[lon, lat]` -- the GeoJSON order, which is the
        opposite of how everything else in this program writes them, and is
        the single most common way an export ends up in the wrong hemisphere.
        """
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [w.lon, w.lat]},
                "properties": {k: v for k, v in w.to_json().items()
                               if k not in ("lat", "lon")},
            } for w in self.points],
        }

    def export_geojson(self, path: Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_geojson(), indent=1),
                        encoding="utf-8")
        return path

    def export_csv(self, path: Path) -> Path:
        path = Path(path)
        cols = ["name", "lat", "lon", "created", "origin_of", "fix_kind",
                "fix_quality", "fix_age_s", "dead_reckoned", "profile",
                "depth_m", "altitude_m", "source", "note", "id"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for p in self.points:
                w.writerow(p.to_json())
        return path


def track_geojson(segments, name: str = "ROV track") -> dict:
    """A segmented track as a GeoJSON MultiLineString.

    Segments stay separate features so that a break -- an estimator reset, an
    origin change -- survives the export instead of being welded shut by the
    next program to open the file.
    """
    lines = [[[lon, lat] for lat, lon in seg] for seg in segments if len(seg) > 1]
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {"name": name, "segment": i},
        } for i, line in enumerate(lines)],
    }


# --------------------------------------------------------------------------
#  Planned features
# --------------------------------------------------------------------------
#
# Survey sites and planned transects come in as GeoJSON, because this
# repository's own `survey.Site` has no coordinates in it at all -- it is
# names, dates and transect *times*, which is what the rest of the programme
# needs and is no use to a map. Rather than bend that format into carrying
# geometry it was not designed for, planned features are imported from the
# lightweight standard every GIS tool this team uses can already write.

PLANNED_FILENAME = "planned.geojson"


@dataclass
class Planned:
    """A site marker or a planned transect line, read from GeoJSON."""

    name: str
    #: "point" or "line".
    shape: str
    #: [(lat, lon), ...]. One pair for a point.
    points: list
    note: str = ""


def read_planned(path: Path) -> tuple[list[Planned], list[str]]:
    """(features, problems) from a GeoJSON file.

    Tolerant on purpose: a file that is half usable gives the usable half and
    says what it could not read, because a survey plan exported from somebody
    else's software is not going to be exactly what this expects and a map
    that refuses the whole file helps nobody on a boat.

    GeoJSON coordinates are **[lon, lat]** -- the opposite order to everywhere
    else in this program, and the single most common way an imported plan ends
    up in the wrong hemisphere.
    """
    problems: list[str] = []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], [f"{path} does not exist"]
    except Exception as ex:
        return [], [f"{path} could not be read: {ex}"]

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        # A bare geometry or a bare list is common enough to accept.
        features = data if isinstance(data, list) else [data]

    out: list[Planned] = []
    for i, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry", feat)
        props = feat.get("properties") or {}
        if not isinstance(geom, dict):
            problems.append(f"feature {i} has no geometry")
            continue
        kind = str(geom.get("type") or "")
        coords = geom.get("coordinates")
        name = str(props.get("name") or props.get("Name")
                   or props.get("site") or f"plan {i + 1}")
        note = str(props.get("note") or props.get("description") or "")

        pairs: list[tuple[float, float]] = []
        if kind == "Point" and isinstance(coords, (list, tuple)):
            coords = [coords]
        elif kind in ("LineString", "MultiPoint"):
            pass
        elif kind in ("MultiLineString", "Polygon"):
            # Take the first ring or line; a survey plan rarely needs more and
            # guessing at the rest would be worse than saying so.
            coords = coords[0] if isinstance(coords, list) and coords else []
            if kind == "Polygon":
                problems.append(f"{name}: polygon imported as its outer ring")
        else:
            problems.append(f"{name}: {kind or 'unknown geometry'} not imported")
            continue

        for c in coords if isinstance(coords, list) else []:
            if not (isinstance(c, (list, tuple)) and len(c) >= 2):
                continue
            lon, lat = c[0], c[1]          # GeoJSON order
            if M.valid_latlon(lat, lon):
                pairs.append((float(lat), float(lon)))
            else:
                problems.append(f"{name}: ({lat}, {lon}) is not a usable "
                                f"coordinate — check the lon/lat order")
        if not pairs:
            problems.append(f"{name}: no usable coordinates")
            continue
        out.append(Planned(name=name,
                           shape="point" if len(pairs) == 1 else "line",
                           points=pairs, note=note))
    log.info("read %d planned feature(s) from %s (%d problem(s))",
             len(out), path, len(problems))
    return out, problems
