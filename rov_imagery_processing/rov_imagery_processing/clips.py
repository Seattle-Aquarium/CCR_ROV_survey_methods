"""
Short shareable clips cut out of one video.

Different job from the transect trim, and deliberately a different module:

* A transect clip is *evidence*. It is a stream copy of the original footage,
  cut on TC-25, and it must not be re-encoded.
* A moment clip is *communication* — a lingcod for a talk or a post. Its times
  are offsets **into one file** ("6:40"), not clock times, and it is re-encoded
  so the cut lands on the frame asked for rather than the nearest keyframe.

Everything here writes into ``videos/clips/`` and never touches the source.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_tools as ff
from . import layout

ProgressCB = Callable[[float, str], None]

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
CLIPS_DIR = "clips"


# --------------------------------------------------------------------------
#  Time offsets into a file
# --------------------------------------------------------------------------

_OFFSET = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)\s*$")


def parse_offset(text: str) -> float | None:
    """``6:40`` -> 400.0 seconds. Also ``400``, ``0:06:40``, ``1:02:03.5``.

    Read the way people say it: the rightmost field is always seconds, the one
    before it minutes, the one before that hours. So a bare ``90`` is ninety
    seconds and ``1:30`` is a minute and a half -- never the other way round.
    """
    m = _OFFSET.match(str(text))
    if not m:
        return None
    a, b, c = m.groups()
    parts = [p for p in (a, b) if p is not None]
    secs = float(c)
    if len(parts) == 2:
        secs += int(parts[0]) * 3600 + int(parts[1]) * 60
    elif len(parts) == 1:
        secs += int(parts[0]) * 60
    return secs


def format_offset(seconds: float) -> str:
    """400 -> ``6:40``; over an hour -> ``1:06:40``."""
    s = max(0.0, float(seconds))
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --------------------------------------------------------------------------
#  Output formats
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipFormat:
    key: str
    label: str
    note: str
    height: int | None          # None = keep the source height
    animated_gif: bool = False
    crf: int = 20
    preset: str = "medium"
    fps: int | None = None
    #: GIF only. Fewer colours shrink the file a lot and cost little on
    #: underwater footage, which is mostly greens and blues anyway.
    max_colors: int = 128

    @property
    def suffix(self) -> str:
        return ".gif" if self.animated_gif else ".mp4"


#: Everything here is 8-bit H.264 (or GIF). The analysis renditions elsewhere
#: use 10-bit HEVC, which is right for archival and wrong for anything anyone
#: is going to open on a phone or drop into a slide.
CLIP_FORMATS: dict[str, ClipFormat] = {
    "1080p": ClipFormat(
        "1080p", "1080p", "full quality for a talk or a slide", 1080, crf=19),
    "720p": ClipFormat(
        "720p", "720p", "smaller, still sharp", 720, crf=21),
    "social": ClipFormat(
        "social", "Social", "1080p, web-optimised — safe on any platform",
        1080, crf=23, preset="slow"),
    # 480x270 at 10 fps. Measured on a 15 s underwater clip: 640px/12fps came
    # out at 31 MB, this at 12 MB. GIF stores every frame whole, so size grows
    # linearly with duration -- roughly 0.8 MB per second at these settings.
    "gif": ClipFormat(
        "gif", "GIF", "480x270, 10 fps — autoplays anywhere, but ~0.8 MB/second",
        270, animated_gif=True, fps=10, max_colors=128),
}

DEFAULT_FORMATS = ("1080p",)


# --------------------------------------------------------------------------
#  Finding source video
# --------------------------------------------------------------------------


@dataclass
class SourceVideo:
    path: Path
    duration: float
    width: int
    height: int

    @property
    def caption(self) -> str:
        return (f"{self.path.name}   {format_offset(self.duration)}   "
                f"{self.width}x{self.height}")


def list_videos(root: Path, recursive: bool = True) -> list[SourceVideo]:
    """Every playable video under `root`, probed for duration and size."""
    root = Path(root)
    if root.is_file():
        paths = [root] if root.suffix.lower() in VIDEO_EXTS else []
    elif root.is_dir():
        it = root.rglob("*") if recursive else root.iterdir()
        paths = sorted(p for p in it
                       if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    else:
        return []

    out: list[SourceVideo] = []
    for p in paths:
        info = ff.probe(p)
        if not info.ok:
            continue
        out.append(SourceVideo(p, info.duration or 0.0,
                               info.width or 0, info.height or 0))
    return out


def clips_dir(flight_or_videos: Path) -> Path:
    """``videos/clips/`` for a flight, or ``clips/`` beside a videos folder."""
    p = Path(flight_or_videos)
    if p.name == layout.VIDEOS:
        return p / CLIPS_DIR
    if (p / layout.VIDEOS).is_dir():
        return p / layout.VIDEOS / CLIPS_DIR
    # pointed somewhere inside videos/ (composites/, transects/T1/, ...)
    for parent in p.parents:
        if parent.name == layout.VIDEOS:
            return parent / CLIPS_DIR
    return p / CLIPS_DIR


def clip_name(source: Path, start_s: float, end_s: float, fmt: ClipFormat,
              label: str = "") -> str:
    """``<source>_6m40s-6m55s_social.mp4``, or a name the user typed."""
    def stamp(x: float) -> str:
        m, s = divmod(int(x), 60)
        return f"{m}m{s:02d}s"
    stem = _safe(label) if label else source.stem
    return f"{stem}_{stamp(start_s)}-{stamp(end_s)}_{fmt.key}{fmt.suffix}"


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(text: str) -> str:
    return _UNSAFE.sub("_", str(text).strip()).strip("_") or "clip"


# --------------------------------------------------------------------------
#  Cutting
# --------------------------------------------------------------------------


@dataclass
class ClipOutput:
    fmt: str
    path: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error is None


@dataclass
class ClipJobReport:
    source: Path
    start_s: float = 0.0
    end_s: float = 0.0
    outputs: list[ClipOutput] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> list[Path]:
        return [o.path for o in self.outputs if o.ok and o.path]

    def summary(self) -> str:
        span = f"{format_offset(self.start_s)}–{format_offset(self.end_s)}"
        lines = [f"Clip {span} from {self.source.name}: "
                 f"{len(self.written)} file(s)"]
        for o in self.outputs:
            if o.ok and o.path:
                try:
                    mb = o.path.stat().st_size / 1e6
                    lines.append(f"   {o.path.name}  ({mb:.1f} MB)")
                except OSError:
                    lines.append(f"   {o.path.name}")
            else:
                lines.append(f"   {o.fmt}: FAILED — {o.error}")
        return "\n".join(lines)


def validate(source: SourceVideo, start_s: float, end_s: float) -> list[str]:
    """Problems with a requested clip, worst first."""
    errs: list[str] = []
    if end_s <= start_s:
        errs.append("The end time is not after the start time.")
    if start_s < 0:
        errs.append("The start time is before the beginning of the file.")
    if source.duration and start_s >= source.duration:
        errs.append(f"The start time is past the end of the file "
                    f"({format_offset(source.duration)} long).")
    elif source.duration and end_s > source.duration + 0.5:
        errs.append(f"The end time is past the end of the file "
                    f"({format_offset(source.duration)} long).")
    return errs


def _gif(src: Path, start: float, dur: float, out: Path, fmt: ClipFormat,
         scratch: Path, cancel=None) -> None:
    """Two passes: build a palette from this clip, then map onto it.

    A single pass uses a fixed 256-colour web palette and underwater footage --
    all greens and blues -- bands horribly under it.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    palette = scratch / "palette.png"
    vf = f"fps={fmt.fps},scale=-2:{fmt.height}:flags=lanczos"
    ff.run(["-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-vf", f"{vf},palettegen=max_colors={fmt.max_colors}:stats_mode=diff", str(palette)],
           cancel=cancel)
    ff.run(["-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
            "-i", str(palette),
            "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
            "-loop", "0", str(out)],
           cancel=cancel)
    palette.unlink(missing_ok=True)


def make_clip(
    source: SourceVideo,
    start_s: float,
    end_s: float,
    out_dir: Path,
    formats: Sequence[str] = DEFAULT_FORMATS,
    *,
    label: str = "",
    scratch: Path | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
    overwrite: bool = False,
) -> ClipJobReport:
    """Cut one span out of one video, in each requested format.

    Re-encodes rather than stream-copying: a fifteen-second moment should start
    on the frame asked for, and at this length the encode costs seconds.
    """
    rep = ClipJobReport(source=source.path, start_s=start_s, end_s=end_s)
    problems = validate(source, start_s, end_s)
    if problems:
        rep.errors.extend(problems)
        return rep

    chosen = [CLIP_FORMATS[k] for k in formats if k in CLIP_FORMATS]
    if not chosen:
        rep.errors.append("No output format selected.")
        return rep

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch) if scratch else out_dir / ".scratch"
    dur = end_s - start_s

    for i, fmt in enumerate(chosen):
        if cancel is not None and cancel.is_set():
            raise ff.CancelledError("cancelled")
        out = out_dir / clip_name(source.path, start_s, end_s, fmt, label)
        if out.exists() and not overwrite:
            rep.outputs.append(ClipOutput(fmt.key, out))
            rep.warnings.append(f"{out.name} already existed and was kept")
            continue

        # Bind i and fmt: this closure runs inside the iteration today, but an
        # unbound one would report the last format's name if that changed.
        step = (lambda f, m="", i=i, _f=fmt: progress(
            (i + f) / len(chosen), m or f"{_f.label}…")) if progress else None
        try:
            if fmt.animated_gif:
                if progress:
                    progress(i / len(chosen), f"{fmt.label} (two passes)…")
                _gif(source.path, start_s, dur, out, fmt, scratch, cancel)
            else:
                # -ss before -i seeks fast; -ss again after would be frame
                # accurate but re-encoding already gives that, and the second
                # form costs a full decode from the start of the file.
                scale = (f"scale=-2:{fmt.height}" if fmt.height
                         and fmt.height < source.height else None)
                args = ["-y", "-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(source.path)]
                if scale:
                    args += ["-vf", scale]
                args += ["-c:v", "libx264", "-crf", str(fmt.crf),
                         "-preset", fmt.preset, "-pix_fmt", "yuv420p",
                         "-profile:v", "high", "-movflags", "+faststart",
                         "-c:a", "aac", "-b:a", "128k", str(out)]
                ff.run(args, progress=step,
                       total_seconds=dur, cancel=cancel)
            rep.outputs.append(ClipOutput(fmt.key, out))
        except ff.CancelledError:
            raise
        except Exception as ex:
            msg = f"{type(ex).__name__}: {ex}".split("\n")[0][:160]
            rep.outputs.append(ClipOutput(fmt.key, None, msg))
            rep.errors.append(f"{fmt.label}: {msg}")

    try:
        if scratch.is_dir() and not any(scratch.iterdir()):
            scratch.rmdir()
    except OSError:
        pass

    gif = next((o for o in rep.outputs if o.fmt == "gif" and o.ok), None)
    if gif and gif.path:
        try:
            mb = gif.path.stat().st_size / 1e6
            secs = max(0.1, end_s - start_s)
            if mb > 8:
                fits = 8.0 / (mb / secs)
                rep.warnings.append(
                    f"the GIF is {mb:.0f} MB. GIF stores every frame whole, so "
                    f"size grows with duration — about {int(fits)}s would fit "
                    f"in 8 MB. The social MP4 is smaller and better quality; "
                    f"most platforms loop it like a GIF.")
        except OSError:
            pass

    if progress:
        progress(1.0, f"{len(rep.written)} clip(s) written")
    return rep
