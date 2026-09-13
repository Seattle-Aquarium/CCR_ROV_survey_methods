"""
Two videos side by side in one frame.

Built to answer a question the separate recordings cannot: how much of the
difference between two flights is the lighting rig and how much is the seabed.
Put Lutris next to Nereo over the same site and the comparison is direct.

Either side may be a **video file** or a **folder of mcaps**, and the two need
not match -- a 4K GoPro chapter on the left and an ROV forward camera on the
right is a legitimate pairing.

**Time is the awkward part**, because the sources do not share a clock:

* An **mcap** carries an absolute epoch per frame, so a TC-25 time of day
  places it exactly.
* An **original GoPro chapter** carries a TC-25 timecode track, so it does too.
* A **trim** carries its *source chapter's* timecode -- every trim from one
  recording reports the same start -- and a **composite** carries no timecode
  at all. Neither can be placed on a clock, so both are addressed by offset
  into the file.

So each side gets its own in-point, entered in whichever form suits it, and
the two share one duration. That is not a compromise: comparing Lutris T1
against Nereo T5 means two different absolute times deliberately aligned from
their own starts, which a single shared timeline could not express.

The convention for reading a time follows the rest of UTC: **three
colon-separated fields is a time of day** (``10:02:27``), anything shorter is
an offset into the file (``1:30``, ``90``).
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_tools as ff
from . import layout
from .survey import parse_hhmmss

ProgressCB = Callable[[float, str], None]

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}

#: Pixels of dark ground between the two panes. Two dim underwater scenes run
#: into one another without it, and the seam is exactly what the eye needs to
#: judge them separately.
DIVIDER_PX = 6
DIVIDER_COLOUR = "0x0B1A24"


@dataclass(frozen=True)
class SideBySideFormat:
    key: str
    label: str
    note: str
    #: Height of each pane. The output is twice this tall only in the sense
    #: that both panes share it; the frame is as wide as the two panes plus
    #: the divider.
    height: int
    crf: int = 20
    preset: str = "medium"


SBS_FORMATS: dict[str, SideBySideFormat] = {
    "4K": SideBySideFormat(
        "4K", "4K", "each pane 2160 tall -- only if both sources can supply it",
        2160, crf=20, preset="medium"),
    "1080p": SideBySideFormat(
        "1080p", "1080p", "each pane 1080 tall; the usual choice",
        1080, crf=20, preset="medium"),
    "720p": SideBySideFormat(
        "720p", "720p", "each pane 720 tall, for sharing",
        720, crf=22, preset="medium"),
}


class SideBySideError(RuntimeError):
    """Something the operator has to fix before a run can start."""


# --------------------------------------------------------------------------
#  reading a time
# --------------------------------------------------------------------------


def parse_time(text: str) -> tuple[str, float] | None:
    """``("clock", seconds_of_day)`` or ``("offset", seconds)``, or None.

    Three fields is a time of day, fewer is an offset into the file. That is
    the same split the rest of UTC uses, and it is unambiguous in practice: a
    transect never starts 12 hours into a recording, and a clip offset is
    never written with an hours field.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    fields = raw.replace(".", ":").replace("-", ":").split(":")
    if len(fields) >= 3:
        try:
            return ("clock", float(parse_hhmmss(raw)))
        except Exception:
            return None
    total = 0.0
    for f in fields:
        f = f.strip()
        if not f or not f.replace(".", "", 1).isdigit():
            return None
        total = total * 60 + float(f)
    return ("offset", total)


# --------------------------------------------------------------------------
#  one side
# --------------------------------------------------------------------------


@dataclass
class Side:
    """One pane, resolved far enough to be seekable."""

    path: Path                     # what the operator chose
    kind: str                      # "video" | "mcap"
    playable: Path                 # the file ffmpeg will actually read
    width: int
    height: int
    duration: float
    fps: float
    label: str
    #: Epoch of the playable file's first frame, when it can be known. Only
    #: then does a time of day mean anything for this side.
    epoch_at_zero: float | None = None
    #: Why a timecode was refused, when one was present but not trustworthy.
    clock_note: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_clock(self) -> bool:
        return self.epoch_at_zero is not None

    def in_point(self, text: str) -> float:
        """Seconds into `playable` for what the operator typed."""
        parsed = parse_time(text)
        if parsed is None:
            raise SideBySideError(
                f"{self.label}: could not read {text!r} as a time. Use "
                f"hh:mm:ss for a time of day, or m:ss for an offset.")
        kind, value = parsed
        if kind == "offset":
            return value
        if not self.has_clock:
            raise SideBySideError(
                f"{self.label}: {text} is a time of day, but this source "
                f"carries no usable clock"
                + (f" ({self.clock_note})" if self.clock_note else "")
                + ". Give an offset into the file instead, like 1:30.")
        # `value` is seconds since local midnight; the epoch axis needs the
        # same midnight the rest of the flight was resolved against.
        return value - self._seconds_of_day_at_zero()

    def _seconds_of_day_at_zero(self) -> float:
        import datetime as dt
        t = dt.datetime.fromtimestamp(self.epoch_at_zero or 0.0)
        return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def _is_trustworthy_timecode(path: Path) -> tuple[bool, str]:
    """Whether a video's timecode track can be believed.

    A trim carries the timecode of the chapter it was cut from, so every trim
    of one recording claims the same start. A composite carries none. Only an
    untouched chapter is taken at face value.
    """
    parts = {p.lower() for p in path.parts}
    if layout.TRANSECTS in parts:
        return False, "it is a per-transect trim, which inherits its source's timecode"
    if layout.COMPOSITES in parts or "clips" in parts:
        return False, "it is a generated file"
    if path.stem.endswith("_source"):
        return False, "it is a per-transect trim, which inherits its source's timecode"
    return True, ""


def _sbs_cache(cache_root: Path, sources: Sequence[Path]) -> Path:
    """A cache of its own, keyed by the sources.

    Two ROVs flown the same day share one flight folder, so keying on the
    flight would make Lutris and Nereo overwrite each other's proxy.
    """
    key = "|".join(sorted(str(Path(p).resolve()) for p in sources))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return Path(cache_root) / "sidebyside" / h


def list_mcaps(folder: Path) -> list[Path]:
    folder = Path(folder)
    if folder.is_file() and folder.suffix.lower() == ".mcap":
        return [folder]
    return sorted(p for p in folder.glob("*.mcap") if p.is_file())


def window_for(mcaps: Sequence[Path], when: str,
               seconds: float) -> tuple[float, float] | None:
    """The epoch span a clock time means, judged from the recordings' own date.

    Worked out before anything is extracted, so a folder holding a whole day
    of recordings only has the one covering the window read. Without this,
    asking for ninety seconds of Lutris would decode seven gigabytes.
    """
    import datetime as dt

    from . import mcap_extract

    parsed = parse_time(when)
    if parsed is None or parsed[0] != "clock":
        return None
    infos = [i for i in mcap_extract.probe_mcaps(list(mcaps)) if i.start]
    if not infos:
        return None

    # A time of day says nothing about which day, and a recorder folder often
    # holds strays from other dives -- the Nereo folder carries one from six
    # weeks earlier. Taking the earliest recording's date put the window in
    # July and matched nothing. So try each date present and keep the one a
    # recording actually covers.
    def window_on(day: dt.date) -> tuple[float, float]:
        midnight = dt.datetime.combine(day, dt.time(0, 0)).timestamp()
        start = midnight + parsed[1]
        return (start, start + max(seconds, 1.0))

    days = sorted({dt.datetime.fromtimestamp(i.start).date() for i in infos})
    for day in days:
        lo, hi = window_on(day)
        if any(i.start <= hi and (i.end or i.start) >= lo for i in infos):
            return (lo, hi)
    # Nothing covers it on any date. Return the busiest day's window so the
    # caller reports "no recording covers that time" rather than guessing.
    busiest = max(days, key=lambda d: sum(
        1 for i in infos if dt.datetime.fromtimestamp(i.start).date() == d))
    return window_on(busiest)


def probe_side(
    chosen: Path,
    *,
    label: str,
    cache_root: Path,
    window: tuple[float, float] | None = None,
    when: str = "",
    seconds: float = 0.0,
    progress: ProgressCB | None = None,
    cancel=None,
) -> Side:
    """Turn what the operator picked into something seekable.

    A video is used as it stands. A folder of mcaps is extracted and remuxed
    into the same constant-rate proxy the compositor uses, because the ROV
    stream is too variable-rate to seek directly.
    """
    chosen = Path(chosen)
    if not chosen.exists():
        raise SideBySideError(f"{label}: there is nothing at {chosen}")

    if chosen.is_file() and chosen.suffix.lower() in VIDEO_EXTS:
        from . import discovery
        cloud = discovery.check_local([chosen])
        if cloud:
            raise SideBySideError(f"{label}: " + "\n".join(cloud))
        mi = ff.probe(chosen)
        side = Side(path=chosen, kind="video", playable=chosen,
                    width=mi.width or 0, height=mi.height or 0,
                    duration=mi.duration or 0.0, fps=mi.fps or 30.0,
                    label=label)
        ok, why = _is_trustworthy_timecode(chosen)
        tc = ff.timecode_to_seconds(mi.timecode, mi.fps) if ok else None
        if tc is not None:
            # A chapter's timecode is a time of day, so PTS 0 sits there.
            side.epoch_at_zero = _epoch_from_time_of_day(chosen, tc)
        elif not ok:
            side.clock_note = why
        else:
            side.clock_note = "it has no timecode track"
        return side

    mcaps = list_mcaps(chosen)
    if not mcaps:
        raise SideBySideError(
            f"{label}: {chosen} holds neither a video nor any .mcap files")
    if window is None and when and seconds > 0:
        window = window_for(mcaps, when, seconds)
    return _prepare_mcap_side(mcaps, label=label, cache_root=cache_root,
                              window=window, progress=progress, cancel=cancel)


def _epoch_from_time_of_day(path: Path, seconds_of_day: float) -> float | None:
    """Place a timecode on the epoch axis using the file's own date.

    A timecode says what time of day it is but not which day, so the file's
    modification date supplies that. Good enough to seek within one recording,
    which is all this is used for.
    """
    import datetime as dt
    try:
        day = dt.datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None
    midnight = dt.datetime.combine(day, dt.time(0, 0)).timestamp()
    return midnight + seconds_of_day


def _prepare_mcap_side(
    mcaps: Sequence[Path],
    *,
    label: str,
    cache_root: Path,
    window: tuple[float, float] | None,
    progress: ProgressCB | None,
    cancel,
) -> Side:
    from . import mcap_extract, rov_video

    cache = _sbs_cache(cache_root, mcaps)
    cache.mkdir(parents=True, exist_ok=True)

    chosen = list(mcaps)
    warnings: list[str] = []
    # A file Dropbox has not finished fetching reads as a short prefix rather
    # than as an error: the extraction runs for minutes and reports no frames,
    # which looks like a broken recording. The rest of UTC checks this before
    # starting, and so must this.
    from . import discovery
    cloud = discovery.check_local(list(mcaps))
    if cloud:
        raise SideBySideError(f"{label}: " + "\n".join(cloud))
    if window:
        chosen, _skipped, warns = mcap_extract.select_for_windows(
            list(mcaps), [window])
        warnings.extend(warns)
        if not chosen:
            raise SideBySideError(
                f"{label}: none of the recordings cover that time")

    def stage(lo: float, hi: float):
        if progress is None:
            return None
        return lambda f, m="": progress(lo + (hi - lo) * f, f"{label}: {m}")

    ex = mcap_extract.extract(chosen, cache, progress=stage(0.0, 0.6))
    warnings.extend(ex.warnings)
    if ex.video.frames == 0:
        raise SideBySideError(
            f"{label}: these recordings contain no video stream")

    # The proxy is constant-rate, so it needs a rate. Take the recording's
    # own: the ROV stream is strongly variable, and a wrong nominal rate makes
    # the proxy longer or shorter than the footage it came from.
    span = (ex.video.last_ts or 0.0) - (ex.video.first_ts or 0.0)
    fps = ((ex.video.frames - 1) / span) if span > 0 and ex.video.frames > 1 else 30.0
    rov = rov_video.prepare(
        cache, fps,
        needed_epochs=[window] if window else (),
        progress=stage(0.6, 1.0), cancel=cancel)
    warnings.extend(rov.warnings)

    return Side(path=Path(mcaps[0]).parent, kind="mcap", playable=rov.proxy_path,
                width=rov.width, height=rov.height, duration=rov.duration,
                fps=rov.fps, label=label, epoch_at_zero=rov.epoch_at_zero,
                warnings=warnings)


# --------------------------------------------------------------------------
#  building it
# --------------------------------------------------------------------------


@dataclass
class SideBySideReport:
    output: Path | None = None
    width: int = 0
    height: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None and not self.errors

    def summary(self) -> str:
        if not self.ok:
            return "; ".join(self.errors) or "nothing written"
        return (f"{self.output.name}  {self.width}x{self.height}  "
                f"{self.seconds:.0f}s")


def output_dir(flight: Path) -> Path:
    """``videos/composites/`` -- these sit beside the transect composites."""
    return Path(flight) / layout.VIDEOS / layout.COMPOSITES


def output_name(left: Side, right: Side, fmt: SideBySideFormat) -> str:
    return f"{_safe(left.label)}_vs_{_safe(right.label)}_{fmt.key}.mp4"


def _safe(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (text or "").strip()]
    out = "".join(keep)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "side"


def usable_formats(left: Side, right: Side) -> list[str]:
    """Formats neither side has to be upscaled for.

    Blowing 1080 up to 2160 next to real 2160 would look like the ROV camera
    is the blurry one, when the difference is entirely the scaler.
    """
    limit = min(left.height or 0, right.height or 0)
    return [k for k, f in SBS_FORMATS.items() if f.height <= limit] or ["720p"]


def validate(left: Side, right: Side, in_l: float, in_r: float,
             seconds: float) -> list[str]:
    errs: list[str] = []
    if seconds <= 0:
        errs.append("the duration has to be more than zero")
    for side, start in ((left, in_l), (right, in_r)):
        if start < 0:
            errs.append(f"{side.label}: the start is before the file begins")
        elif side.duration and start >= side.duration:
            errs.append(
                f"{side.label}: {start:.0f}s is past the end of a "
                f"{side.duration:.0f}s source")
        elif side.duration and start + seconds > side.duration + 0.5:
            errs.append(
                f"{side.label}: only {side.duration - start:.0f}s remain after "
                f"that start, but {seconds:.0f}s were asked for")
    return errs


def _filter(left: Side, right: Side, fmt: SideBySideFormat,
            labels: bool) -> str:
    h = fmt.height
    parts = [
        f"[0:v]scale=-2:{h}:flags=lanczos,setsar=1[l0]",
        f"[1:v]scale=-2:{h}:flags=lanczos,setsar=1[r0]",
    ]
    lab_l, lab_r = "l0", "r0"
    if labels:
        from . import brand
        font = brand.font_path("semibold") or brand.font_path("regular")
        if font:
            fp = str(font).replace("\\", "/").replace(":", "\\:")
            size = max(18, h // 30)
            pad = max(10, h // 60)
            common = (f"fontfile='{fp}':fontsize={size}:fontcolor=white:"
                      f"box=1:boxcolor=0x0B1A24@0.72:boxborderw={pad // 2}:"
                      f"x={pad}:y={pad}")
            parts.append(f"[l0]drawtext={common}:text='{_dt(left.label)}'[l1]")
            parts.append(f"[r0]drawtext={common}:text='{_dt(right.label)}'[r1]")
            lab_l, lab_r = "l1", "r1"
    # The divider is padding on the left pane, so it cannot be mistaken for
    # part of either image.
    parts.append(
        f"[{lab_l}]pad=iw+{DIVIDER_PX}:ih:0:0:color={DIVIDER_COLOUR}[lp]")
    parts.append(f"[lp][{lab_r}]hstack=inputs=2[out]")
    return ";".join(parts)


def _dt(text: str) -> str:
    """Escape a label for drawtext, which has its own quoting rules."""
    out = (text or "").replace("\\", "\\\\").replace("'", "")
    for ch in (":", "%", "[", "]", ","):
        out = out.replace(ch, "\\" + ch)
    return out


def make_side_by_side(
    left: Side,
    right: Side,
    in_l: float,
    in_r: float,
    seconds: float,
    out_dir: Path,
    fmt_key: str = "1080p",
    *,
    labels: bool = True,
    progress: ProgressCB | None = None,
    cancel=None,
    overwrite: bool = False,
) -> SideBySideReport:
    """Cut the same length from each source and stand them next to each other."""
    rep = SideBySideReport()
    fmt = SBS_FORMATS.get(fmt_key)
    if fmt is None:
        rep.errors.append(f"unknown format {fmt_key!r}")
        return rep

    allowed = usable_formats(left, right)
    if fmt_key not in allowed:
        rep.warnings.append(
            f"{fmt_key} would upscale the smaller source; using "
            f"{allowed[-1]} instead")
        fmt = SBS_FORMATS[allowed[-1]]

    errs = validate(left, right, in_l, in_r, seconds)
    if errs:
        rep.errors.extend(errs)
        return rep

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / output_name(left, right, fmt)
    if out.exists() and not overwrite:
        rep.warnings.append(f"{out.name} already existed and was kept")
        rep.output = out
        return rep

    scratch = out_dir / f".{out.stem}.part.mp4"
    args = [
        "-y",
        "-ss", f"{in_l:.3f}", "-t", f"{seconds:.3f}", "-i", str(left.playable),
        "-ss", f"{in_r:.3f}", "-t", f"{seconds:.3f}", "-i", str(right.playable),
        "-filter_complex", _filter(left, right, fmt, labels),
        "-map", "[out]", "-an",
        "-c:v", "libx264", "-crf", str(fmt.crf), "-preset", fmt.preset,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(scratch),
    ]
    try:
        ff.run(args, progress=(lambda f: progress(f, f"building {out.name}"))
               if progress else None,
               total_seconds=seconds, cancel=cancel)
    except Exception as ex:
        scratch.unlink(missing_ok=True)
        rep.errors.append(f"{type(ex).__name__}: {str(ex).splitlines()[0][:160]}")
        return rep

    shutil.move(str(scratch), str(out))
    mi = ff.probe(out)
    rep.output = out
    rep.width, rep.height = mi.width or 0, mi.height or 0
    rep.seconds = mi.duration or seconds
    rep.warnings.extend(left.warnings + right.warnings)
    return rep
