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


#: Saved transect times live beside the flight's data under this name. The
#: file holds the sites and their transects, which is what a reader opening
#: the logs folder is actually looking for -- the older `utc_plan.json` named
#: the program rather than the contents.
PLAN_FILENAME = "surveys.json"

#: Names written by earlier versions, newest first. Read, never written, so
#: flight folders prepared before a rename keep opening without anyone
#: re-typing a dozen transect times. Every flight this program has ever
#: written is still openable: nothing on disk has to move.
LEGACY_PLAN_FILENAMES = ("utc_plan.json", "composite_plan.json")


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
class Pause:
    """A stretch inside a transect during which nothing was being surveyed.

    The vehicle is still down, the recordings are still running and the
    telemetry is still being written -- what has happened is that Cockpit
    disarmed, or the video glitched, or a minute went on getting the ROV back
    where it was. None of the GoPro imagery from that minute is survey
    imagery, so nothing downstream should treat it as such.

    Times are TC-25, on exactly the same clock as the transect's own, and are
    read in the transect's frame: a pause typed as 00:03:10 inside a transect
    that started at 23:58:00 is after midnight, not twenty-four hours early.
    """

    start_tc: str
    end_tc: str


@dataclass
class Transect:
    name: str                     # "T1", "T2", ...
    start_tc: str                 # hh:mm:ss, TC-25 local
    end_tc: str
    #: Stretches inside this transect that were not surveying. A transect
    #: with none behaves exactly as it always did.
    pauses: list[Pause] = field(default_factory=list)

    def start_s(self) -> float:
        return parse_hhmmss(self.start_tc)

    def end_s(self) -> float:
        s, e = parse_hhmmss(self.start_tc), parse_hhmmss(self.end_tc)
        # a transect that runs past local midnight reads as end < start
        return e + SECONDS_PER_DAY if e < s else e

    def duration_s(self) -> float:
        return self.end_s() - self.start_s()

    # ---- pauses --------------------------------------------------------

    def _in_frame(self, tc: str) -> float:
        """A time-of-day put on this transect's clock rather than the day's."""
        s = parse_hhmmss(self.start_tc)
        v = parse_hhmmss(tc)
        return v + SECONDS_PER_DAY if v < s else v

    def pause_spans(self) -> list[tuple[float, float]]:
        """Each pause as (start, end) seconds, in order, merged and clipped.

        Clipped to the transect because a pause reaching outside it describes
        time this transect does not own; merged because two pauses that touch
        are one stretch, and every consumer downstream wants stretches rather
        than entries. Anything unparseable is left out here and reported by
        `validate` -- half-typed times must not make the whole transect vanish
        while somebody is still typing.
        """
        try:
            s, e = self.start_s(), self.end_s()
        except SurveyError:
            return []
        spans: list[tuple[float, float]] = []
        for p in self.pauses:
            try:
                a, b = self._in_frame(p.start_tc), self._in_frame(p.end_tc)
            except SurveyError:
                continue
            if b < a:                         # a pause running past midnight
                b += SECONDS_PER_DAY
            a, b = max(a, s), min(b, e)
            if b > a:
                spans.append((a, b))
        spans.sort()
        merged: list[tuple[float, float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def active_spans(self) -> list[tuple[float, float]]:
        """The transect with its pauses taken out: what was surveyed.

        One span, the whole transect, when there are no pauses -- so a caller
        written against this needs no special case for the ordinary transect.
        """
        try:
            s, e = self.start_s(), self.end_s()
        except SurveyError:
            return []
        out: list[tuple[float, float]] = []
        cursor = s
        for a, b in self.pause_spans():
            if a > cursor:
                out.append((cursor, a))
            cursor = max(cursor, b)
        if e > cursor:
            out.append((cursor, e))
        return out

    def paused_s(self) -> float:
        return sum(b - a for a, b in self.pause_spans())

    def active_s(self) -> float:
        """Seconds actually surveyed -- the duration minus the pauses."""
        return self.duration_s() - self.paused_s()

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
            return errs
        if d > 4 * 3600:
            errs.append(f"{self.name}: {d/3600:.1f} h long -- check the times")
        errs += self._validate_pauses(s, e)
        return errs

    def _validate_pauses(self, s: float, e: float) -> list[str]:
        """Each pause parses, runs forwards, and sits inside the transect.

        A pause outside its transect is almost always a time typed against the
        wrong row, and silently clipping it to nothing would hide that -- the
        operator would see the imagery they meant to drop arrive anyway.
        """
        errs: list[str] = []
        ranges: list[tuple[float, float, int]] = []
        for i, p in enumerate(self.pauses, start=1):
            where = f"{self.name} pause {i}"
            if not str(p.start_tc).strip() and not str(p.end_tc).strip():
                errs.append(f"{where}: no times -- fill it in or remove it")
                continue
            try:
                a = self._in_frame(p.start_tc)
            except SurveyError as ex:
                errs.append(f"{where} start: {ex}")
                continue
            try:
                b = self._in_frame(p.end_tc)
            except SurveyError as ex:
                errs.append(f"{where} end: {ex}")
                continue
            if b < a:
                b += SECONDS_PER_DAY
            if b <= a:
                errs.append(f"{where}: end is not after start")
                continue
            if a < s or b > e:
                errs.append(
                    f"{where} ({p.start_tc}-{p.end_tc}) is not inside "
                    f"{self.name} ({self.start_tc}-{self.end_tc})")
                continue
            ranges.append((a, b, i))
        ranges.sort()
        for (_a1, b1, i1), (a2, _b2, i2) in zip(ranges, ranges[1:], strict=False):
            if a2 < b1:
                errs.append(f"{self.name}: pauses {i1} and {i2} overlap")
        if ranges and not errs and self.active_s() <= 0:
            errs.append(f"{self.name}: the pauses cover the whole transect")
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


def _transect_from(raw: dict) -> Transect:
    """One transect out of a saved plan, pauses and all.

    Written by hand rather than with ``Transect(**raw)`` for two reasons: a
    plan saved before pauses existed has no ``pauses`` key at all, and one
    saved after has a list of dictionaries that would otherwise be handed
    straight through as dictionaries. Unknown keys are ignored, so a file
    written by a newer version still opens here.
    """
    pauses = [Pause(start_tc=str(p.get("start_tc", "")),
                    end_tc=str(p.get("end_tc", "")))
              for p in (raw.get("pauses") or []) if isinstance(p, dict)]
    return Transect(name=raw.get("name", "T?"),
                    start_tc=raw.get("start_tc", ""),
                    end_tc=raw.get("end_tc", ""),
                    pauses=pauses)


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
                transects=[_transect_from(t) for t in s.get("transects", [])],
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
    """A slice of one chapter contributing to a transect.

    `epoch` is when this slice's first frame happened, on the telemetry clock.
    It is carried rather than worked out from the segment's position in the
    list because the segments of one transect are not always shoulder to
    shoulder in time: a pause is cut out between them, and so is a gap between
    two GoPro chapters. Adding up the durations before it -- which is what the
    compositor used to do -- puts the telemetry ahead of the picture by the
    length of every gap that came earlier.
    """

    chapter: Chapter
    in_s: float            # offset into the chapter
    dur_s: float
    epoch: float | None = None

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

    # ---- placing the segments on the telemetry clock --------------------

    def segment_epoch(self, index: int) -> float:
        """When segment `index` starts, on the telemetry clock.

        Falls back to laying the segments end to end from the transect's own
        start, which is what a segment carrying no epoch of its own means.
        """
        seg = self.segments[index]
        if seg.epoch is not None:
            return seg.epoch
        return self.epoch_start + sum(s.dur_s for s in self.segments[:index])

    def shift(self, seconds: float) -> None:
        """Move the whole transect along the telemetry clock.

        Used when the GoPro is measured against the vehicle's own turns and
        found to be running early or late. Every segment moves with it, or the
        later ones would keep the offset the measurement just removed.
        """
        self.epoch_start += seconds
        self.epoch_end += seconds
        for seg in self.segments:
            if seg.epoch is not None:
                seg.epoch += seconds

    def runs(self) -> list[tuple[int, int]]:
        """Segment index ranges that are continuous in time, as [start, stop).

        A transect with no pauses and no chapter gaps is one run. Anything
        that needs a single unbroken stretch of wall clock -- measuring the
        picture against the yaw rate, for one -- asks for these rather than
        assuming the transect is one.
        """
        out: list[tuple[int, int]] = []
        start = 0
        for i in range(1, len(self.segments)):
            expected = self.segment_epoch(i - 1) + self.segments[i - 1].dur_s
            if abs(self.segment_epoch(i) - expected) > 0.25:
                out.append((start, i))
                start = i
        if self.segments:
            out.append((start, len(self.segments)))
        return out

    def longest_run(self) -> tuple[list[Segment], float]:
        """The longest unbroken stretch of this transect, and when it starts."""
        best: tuple[int, int] | None = None
        best_s = -1.0
        for lo, hi in self.runs():
            span = sum(s.dur_s for s in self.segments[lo:hi])
            if span > best_s:
                best, best_s = (lo, hi), span
        if best is None:
            return [], self.epoch_start
        lo, hi = best
        return self.segments[lo:hi], self.segment_epoch(lo)

    def paused_epochs(self) -> list[tuple[float, float]]:
        """This transect's pauses, on the telemetry clock.

        Derived from `epoch_start` rather than from the date, so a transect
        moved by the motion check carries its pauses along with it.
        """
        base = self.epoch_start - _safe(self.transect.start_s)
        return [(base + lo, base + hi)
                for lo, hi in self.transect.pause_spans()]

    def is_paused(self, epoch: float) -> bool:
        return any(lo <= epoch < hi for lo, hi in self.paused_epochs())

    @property
    def spans(self) -> list[tuple[float, float]]:
        """(epoch, duration) for each run, for anything drawn per second.

        The overlay sequence is one frame per second of *output*, so it walks
        these rather than a single start and duration.
        """
        return [(self.segment_epoch(lo),
                 sum(s.dur_s for s in self.segments[lo:hi]))
                for lo, hi in self.runs()]

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
    """Map one transect onto the available video and the mcap clock.

    Only the stretches that were surveying are resolved: a pause is never
    given a segment, so the footage inside it is neither trimmed out of the
    original nor composited. A transect with no pauses resolves to exactly
    what it always did.
    """
    start_s, end_s = transect.start_s(), transect.end_s()
    wanted = transect.active_spans()
    requested = sum(hi - lo for lo, hi in wanted)
    warnings: list[str] = []
    midnight = local_midnight_epoch(site.date_obj(), timezone)

    usable = [c for c in chapters if c.tc_start_s is not None]
    if not usable and chapters:
        warnings.append(
            "no GoPro timecode track found -- the camera was probably not synced "
            "with GoPro Labs precision time, so transect times cannot be located"
        )

    # Every (chapter, surveying span) overlap, in time order. Sorted by when
    # they happened rather than by chapter, so a transect cut into pieces by a
    # pause still comes out in the order it was flown.
    cuts: list[tuple[float, Segment]] = []
    for ch in usable:
        assert ch.tc_start_s is not None
        for want_lo, want_hi in wanted:
            lo = max(want_lo, ch.tc_start_s)
            hi = min(want_hi, ch.tc_start_s + ch.duration)
            if hi - lo > 0.05:
                cuts.append((lo, Segment(ch, lo - ch.tc_start_s, hi - lo,
                                         epoch=midnight + lo)))
    cuts.sort(key=lambda c: c[0])
    segments = [seg for _lo, seg in cuts]

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
    paused = transect.paused_s()
    if paused > 0:
        warnings.append(
            f"{len(transect.pause_spans())} pause(s) totalling {paused:.0f}s are "
            f"cut out; {requested:.0f}s of surveying remains")
    chapters_used = len({id(s.chapter) for s in segments})
    if chapters_used > 1:
        warnings.append(f"spans {chapters_used} GoPro chapters; they will be joined")

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


def resolve_from_trims(
    plan: SurveyPlan,
    trims: dict[str, Chapter],
) -> list[ResolvedTransect]:
    """Resolve a plan against per-transect trims instead of GoPro chapters.

    A trim already *is* one transect, so there is nothing to search for: it
    contributes its own footage from its own first frame. That matters because
    a trim cannot be placed on the TC-25 clock by its timecode track -- a
    stream copy keeps the source chapter's timecode, so every trim from one
    recording reports the same start.

    A transect with pauses was trimmed with the paused footage already cut
    out, so the trim's own seconds run continuously while the clock they
    belong to does not. The trim is therefore laid back over the transect's
    surveying spans in order, which puts each part of it back on the telemetry
    clock where it was actually recorded.

    The pairing is by transect name, which is how the trim folders are laid
    out. A transect with no trim resolves to nothing and is reported, exactly
    as an uncovered transect would be.
    """
    out: list[ResolvedTransect] = []
    for site in plan.sites:
        midnight = local_midnight_epoch(site.date_obj(), plan.timezone)
        for t in site.transects:
            want = t.active_s()
            ch = trims.get(t.name)
            warnings: list[str] = []
            segments: list[Segment] = []
            covered = 0.0
            if ch is None:
                warnings.append(
                    f"no trimmed video found for {t.name}; expected one in "
                    f"videos/transects/{t.name}/")
            else:
                have = ch.duration or 0.0
                dur = min(have, want) if have else want
                segments = _lay_over_spans(ch, 0.0, dur, t, midnight)
                covered = dur
                # A trim shorter than its transect means the trim was cut from
                # footage that ran out, not that the times are wrong.
                if have and have + 1.0 < want:
                    warnings.append(
                        f"the trim is {have:.0f}s but {t.name} is {want:.0f}s; "
                        f"only the footage that exists will be composited")
                if t.pauses:
                    warnings.append(
                        f"{t.name} has {len(t.pause_spans())} pause(s); the trim "
                        f"is taken to have been cut with them already removed. "
                        f"Re-cut it if the pause times have changed since.")
            out.append(ResolvedTransect(
                site=site, transect=t, segments=segments,
                epoch_start=midnight + t.start_s(),
                epoch_end=midnight + t.end_s(),
                covered_s=covered, requested_s=want, warnings=warnings,
            ))
    return out


def _lay_over_spans(ch: Chapter, head: float, dur: float, transect: Transect,
                    midnight: float) -> list[Segment]:
    """`dur` seconds of `ch`, starting `head` in, placed on a transect's spans.

    One segment per surveying span, each carrying the epoch it belongs to. A
    transect with no pauses has one span, so this returns the single segment
    it always did.
    """
    segments: list[Segment] = []
    at = head
    left = dur
    for lo, hi in transect.active_spans():
        if left <= 0:
            break
        take = min(hi - lo, left)
        if take > 0.05:
            segments.append(Segment(chapter=ch, in_s=at, dur_s=take,
                                    epoch=midnight + lo))
        at += take
        left -= take
    return segments


def resolve_plan(
    plan: SurveyPlan, chapters: Sequence[Chapter]
) -> list[ResolvedTransect]:
    out: list[ResolvedTransect] = []
    for site in plan.sites:
        for t in site.transects:
            out.append(resolve_transect(site, t, chapters, timezone=plan.timezone))
    return out


def plan_windows(plan: SurveyPlan, *, exclude_pauses: bool = False
                 ) -> list[tuple[str, float, float]]:
    """(name, epoch_start, epoch_end) for every transect in a plan.

    Both programs turn a plan into windows, and both now have to be able to
    ask for it two ways, so the one implementation lives here.

    ``exclude_pauses`` splits a paused transect into one window per surveying
    stretch, every one of them under the transect's own name. That is what
    imagery is filed by: a frame taken during a pause then falls inside no
    window at all and is handled as off-transect, which is the whole reason
    for typing the pause in the first place.

    Everything that is about *when the flight happened* rather than about what
    was surveyed -- which recordings to read, which files on the Pi cover a
    transect, where the dive profile's bands go -- wants the whole span, and
    leaves this alone.
    """
    out: list[tuple[str, float, float]] = []
    for site in plan.sites:
        midnight = local_midnight_epoch(site.date_obj(), plan.timezone)
        for t in site.transects:
            spans = (t.active_spans() if exclude_pauses
                     else [(t.start_s(), t.end_s())])
            for lo, hi in spans:
                out.append((t.name, midnight + lo, midnight + hi))
    return out
