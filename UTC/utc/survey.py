"""
Sites, transects, and the TC-25 timecode that ties them to the recordings.

Field workflow: the GoPro is synced with GoPro Labs precision time before a
dive, which sets the camera clock and stamps a timecode track into every MP4.
Transect start and end times are then written down by hand off the camera's
TC-25 display, as local wall-clock ``hh:mm:ss``.

Two independent mappings fall out of that:

  * **TC-25 -> video.** Each MP4 carries the timecode of its first frame, so a
    transect time maps to a position inside a chapter by simple subtraction.
    Exact, and needs no timezone at all.
  * **TC-25 -> mcap.** The mcap timestamps are UTC epoch, so this one needs the
    UTC offset that was in force locally. We derive it from the flight date via
    the IANA zone rather than asking the user, then check it against the ROV's
    own lights (see sync.py). Deriving-and-verifying beats asking, because a
    mistyped offset would look exactly like a good run until someone watched
    the video.

Transects may span a chapter boundary -- GoPro splits at ~11 GB, which is well
inside a long transect -- so video resolution returns a list of segments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None                                   # type: ignore

SECONDS_PER_DAY = 86400

_HHMMSS = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{1,2})\s*[:.]\s*(\d{1,2})(?:[:.](\d{1,3}))?\s*$")


#: Saved transect times live beside the flight's data under this name.
PLAN_FILENAME = "utc_plan.json"

#: Names written by earlier versions. Read, never written, so flight folders
#: prepared before the tool was renamed keep opening without anyone re-typing
#: a dozen transect times.
LEGACY_PLAN_FILENAMES = ("composite_plan.json",)


def plan_path(flight_dir, *, for_writing: bool = False):
    """Where a flight's saved plan lives.

    Writing always uses the current name; reading falls back to a legacy one if
    that is what is actually on disk.
    """
    from pathlib import Path as _Path

    d = _Path(flight_dir)
    current = d / PLAN_FILENAME
    if for_writing or current.is_file():
        return current
    for name in LEGACY_PLAN_FILENAMES:
        legacy = d / name
        if legacy.is_file():
            return legacy
    return current


class SurveyError(ValueError):
    """Bad user input -- surfaced in the GUI, not a crash."""


def parse_hhmmss(text: str) -> float:
    """'13:37:31' -> seconds since local midnight.

    Accepts ``.`` as a separator and an optional frames/fraction field, because
    field notes are handwritten and get transcribed inconsistently.
    """
    if text is None:
        raise SurveyError("missing time")
    m = _HHMMSS.match(str(text))
    if not m:
        raise SurveyError(f"expected hh:mm:ss, got {text!r}")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if h > 23 or mi > 59 or s > 59:
        raise SurveyError(f"not a valid clock time: {text!r}")
    return h * 3600 + mi * 60 + s


def format_hhmmss(seconds: float) -> str:
    seconds = int(round(seconds)) % SECONDS_PER_DAY
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


class TimezoneDataMissing(RuntimeError):
    """No IANA timezone database is available to this Python."""


def _zone(tz_name: str):
    """The tzinfo for a zone, or a loud failure.

    There used to be a fallback here that returned a fixed -8 (PST) when the
    database was missing. It was worse than useless: it made every summer
    transect an hour out, and the midnight helper's version of the same
    fallback double-counted the offset and landed **eight** hours out. Times
    that are quietly wrong send imagery into the wrong transect and cut the
    wrong footage, and nothing downstream can tell.

    Windows ships no timezone database at all, so this is a real possibility
    on a fresh laptop rather than a theoretical one. Stopping with an
    actionable message is the only safe answer.
    """
    if ZoneInfo is None:
        raise TimezoneDataMissing(
            "This Python has no zoneinfo module, so local times cannot be "
            "resolved. Python 3.9 or newer is required."
        )
    try:
        return ZoneInfo(tz_name)
    except Exception as ex:
        raise TimezoneDataMissing(
            f"No timezone database entry for {tz_name!r}. Windows does not "
            f"ship one, so Python needs the 'tzdata' package:\n"
            f"    python -m pip install tzdata\n"
            f"Without it every transect time would resolve to the wrong "
            f"instant, and the error would not be visible in the output."
        ) from ex


def timezone_data_available(tz_name: str = "America/Los_Angeles") -> bool:
    """Cheap check for startup diagnostics."""
    try:
        _zone(tz_name)
        return True
    except TimezoneDataMissing:
        return False


def utc_offset_hours(on: _date, tz_name: str = "America/Los_Angeles") -> float:
    """Local UTC offset in force on a given date (handles PST/PDT)."""
    dt = datetime.combine(on, _time(12, 0), tzinfo=_zone(tz_name))  # midday: never ambiguous
    off = dt.utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


def local_midnight_epoch(on: _date, tz_name: str = "America/Los_Angeles") -> float:
    """Epoch seconds at local midnight on `on`."""
    return datetime.combine(on, _time(0, 0), tzinfo=_zone(tz_name)).timestamp()


# --------------------------------------------------------------------------
#  Model
# --------------------------------------------------------------------------


@dataclass
class Transect:
    name: str                     # "T1", "T2", ...
    start_tc: str                 # hh:mm:ss, TC-25 local
    end_tc: str

    def start_s(self) -> float:
        return parse_hhmmss(self.start_tc)

    def end_s(self) -> float:
        s, e = parse_hhmmss(self.start_tc), parse_hhmmss(self.end_tc)
        # a transect that runs past local midnight reads as end < start
        return e + SECONDS_PER_DAY if e < s else e

    def duration_s(self) -> float:
        return self.end_s() - self.start_s()

    def validate(self) -> list[str]:
        errs: list[str] = []
        try:
            s = self.start_s()
        except SurveyError as ex:
            errs.append(f"{self.name} start: {ex}")
            return errs
        try:
            e = self.end_s()
        except SurveyError as ex:
            errs.append(f"{self.name} end: {ex}")
            return errs
        d = e - s
        if d <= 0:
            errs.append(f"{self.name}: end is not after start")
        elif d > 4 * 3600:
            errs.append(f"{self.name}: {d/3600:.1f} h long -- check the times")
        return errs


@dataclass
class Site:
    name: str
    project: str
    date: str                     # ISO yyyy-mm-dd
    transects: list[Transect] = field(default_factory=list)

    def date_obj(self) -> _date:
        try:
            return _date.fromisoformat(self.date)
        except ValueError as ex:
            raise SurveyError(f"site {self.name!r}: bad date {self.date!r}") from ex

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.name.strip():
            errs.append("a site is missing its name")
        if not self.project.strip():
            errs.append(f"site {self.name!r} is missing a project")
        try:
            self.date_obj()
        except SurveyError as ex:
            errs.append(str(ex))
        if not self.transects:
            errs.append(f"site {self.name!r} has no transects")
        seen: set[str] = set()
        for t in self.transects:
            errs += t.validate()
            if t.name in seen:
                errs.append(f"site {self.name!r} has two transects called {t.name}")
            seen.add(t.name)
        # overlapping transects are almost always a transcription slip
        ordered = sorted((t for t in self.transects), key=lambda t: _safe(t.start_s))
        for a, b in zip(ordered, ordered[1:], strict=False):   # pairwise
            try:
                if b.start_s() < a.end_s():
                    errs.append(
                        f"site {self.name!r}: {a.name} and {b.name} overlap in time"
                    )
            except SurveyError:
                pass
        return errs


def _safe(fn) -> float:
    try:
        return fn()
    except Exception:
        return 0.0


@dataclass
class SurveyPlan:
    sites: list[Site] = field(default_factory=list)
    timezone: str = "America/Los_Angeles"

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.sites:
            errs.append("no sites added")
        for s in self.sites:
            errs += s.validate()
        errs += self._duplicate_names()
        return errs

    def _duplicate_names(self) -> list[str]:
        """A transect name reused across sites is an error, not a warning.

        Imagery is filed by transect name alone -- two ROVs flown on the same
        day, each with a transect called T1, land in one folder and cannot be
        told apart afterwards except by reading timestamps out of filenames.
        That happened on 2026-08-31 and was only noticed because the folder
        held more frames than the transect could account for. Catch it while
        it is still a typing mistake.
        """
        where: dict[str, list[str]] = {}
        for site in self.sites:
            for t in site.transects:
                where.setdefault(t.name, []).append(site.name)
        return [
            f"{name!r} is used by more than one site "
            f"({', '.join(sites)}); imagery is filed by transect name, so "
            f"give each one its own name"
            for name, sites in where.items() if len(sites) > 1
        ]

    # ---- persistence ---------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> SurveyPlan:
        raw = json.loads(text)
        sites = [
            Site(
                name=s["name"], project=s["project"], date=s["date"],
                transects=[Transect(**t) for t in s.get("transects", [])],
            )
            for s in raw.get("sites", [])
        ]
        return cls(sites=sites, timezone=raw.get("timezone", "America/Los_Angeles"))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> SurveyPlan:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
#  Resolution against the recordings
# --------------------------------------------------------------------------


@dataclass
class Chapter:
    """One GoPro MP4, placed on the TC-25 clock by its timecode track."""

    path: Path
    duration: float
    fps: float
    width: int
    height: int
    rotation: int
    tc_start_s: float | None          # seconds since local midnight

    @property
    def tc_end_s(self) -> float | None:
        return None if self.tc_start_s is None else self.tc_start_s + self.duration

    def contains(self, tc_s: float) -> bool:
        if self.tc_start_s is None:
            return False
        return self.tc_start_s <= tc_s < self.tc_start_s + self.duration


@dataclass
class Segment:
    """A slice of one chapter contributing to a transect."""

    chapter: Chapter
    in_s: float            # offset into the chapter
    dur_s: float

    @property
    def out_s(self) -> float:
        return self.in_s + self.dur_s


@dataclass
class ResolvedTransect:
    site: Site
    transect: Transect
    segments: list[Segment]
    epoch_start: float
    epoch_end: float
    covered_s: float
    requested_s: float
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return 0.0 if self.requested_s <= 0 else self.covered_s / self.requested_s

    @property
    def complete(self) -> bool:
        return self.coverage > 0.999

    def output_stem(self, resolution: str) -> str:
        """YYYY-MM-DD_project_site_transect_resolution."""
        return "_".join((
            self.site.date,
            _slug(self.site.project),
            _slug(self.site.name),
            _slug(self.transect.name),
            resolution,
        ))


def _slug(s: str) -> str:
    """Filesystem-safe, but readable -- spaces to hyphens, drop the rest."""
    s = re.sub(r"[\\/:*?\"<>|]+", "", str(s)).strip()
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-{2,}", "-", s) or "unnamed"


def resolve_transect(
    site: Site,
    transect: Transect,
    chapters: Sequence[Chapter],
    *,
    timezone: str = "America/Los_Angeles",
) -> ResolvedTransect:
    """Map one transect onto the available video and the mcap clock."""
    start_s, end_s = transect.start_s(), transect.end_s()
    requested = end_s - start_s
    warnings: list[str] = []

    usable = [c for c in chapters if c.tc_start_s is not None]
    if not usable and chapters:
        warnings.append(
            "no GoPro timecode track found -- the camera was probably not synced "
            "with GoPro Labs precision time, so transect times cannot be located"
        )

    segments: list[Segment] = []
    for ch in sorted(usable, key=lambda c: c.tc_start_s or 0.0):
        assert ch.tc_start_s is not None
        # intersect [start_s, end_s) with this chapter's timecode span
        lo = max(start_s, ch.tc_start_s)
        hi = min(end_s, ch.tc_start_s + ch.duration)
        if hi - lo > 0.05:
            segments.append(Segment(ch, lo - ch.tc_start_s, hi - lo))

    covered = sum(s.dur_s for s in segments)
    if segments and covered < requested - 0.5:
        warnings.append(
            f"only {covered:.1f}s of the requested {requested:.1f}s is covered by "
            "the video files"
        )
    if not segments and usable:
        span = (min(c.tc_start_s for c in usable),               # type: ignore[arg-type]
                max(c.tc_start_s + c.duration for c in usable))  # type: ignore[operator]
        warnings.append(
            f"{transect.name} ({transect.start_tc}-{transect.end_tc}) falls outside "
            f"the recorded video, which spans {format_hhmmss(span[0])}-"
            f"{format_hhmmss(span[1])}"
        )
    if len(segments) > 1:
        warnings.append(f"spans {len(segments)} GoPro chapters; they will be joined")

    midnight = local_midnight_epoch(site.date_obj(), timezone)
    return ResolvedTransect(
        site=site,
        transect=transect,
        segments=segments,
        epoch_start=midnight + start_s,
        epoch_end=midnight + end_s,
        covered_s=covered,
        requested_s=requested,
        warnings=warnings,
    )


def resolve_plan(
    plan: SurveyPlan, chapters: Sequence[Chapter]
) -> list[ResolvedTransect]:
    out: list[ResolvedTransect] = []
    for site in plan.sites:
        for t in site.transects:
            out.append(resolve_transect(site, t, chapters, timezone=plan.timezone))
    return out
