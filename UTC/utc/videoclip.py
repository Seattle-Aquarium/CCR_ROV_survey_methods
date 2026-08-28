"""
Cutting the original 4K down to the transects.

Separate from compositing on purpose: this produces the *untouched* footage for
a transect, so it is a stream copy -- no decode, no re-encode, no quality lost,
and roughly as fast as the disk can move the bytes. Compositing is the other
job, and slow for good reasons.

**Cuts land on keyframes.** A stream copy cannot start mid-GOP, so the start of
a clip snaps back to the nearest keyframe before the requested time -- with
GoPro's ~0.5 s keyframe interval that is under a second of extra footage at the
head. Re-encoding would be frame-accurate, and would also throw away the thing
this function exists to preserve. The slop is reported rather than hidden.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_tools as ff
from . import layout
from .survey import ResolvedTransect

ProgressCB = Callable[[float, str], None]


@dataclass
class ClipResult:
    transect: str
    output: Path | None = None
    seconds: float = 0.0
    parts: int = 0
    skipped: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.output is not None and self.error is None


@dataclass
class ClipReport:
    clips: list[ClipResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> list[Path]:
        return [c.output for c in self.clips if c.ok and c.output]

    def summary(self) -> str:
        good = [c for c in self.clips if c.ok]
        lines = [f"{len(good)} transect clip(s) written"]
        for c in self.clips:
            if c.ok and c.output:
                try:
                    mb = c.output.stat().st_size / 1e6
                    lines.append(f"   {c.transect}: {c.output.name}  "
                                 f"{c.seconds/60:.1f} min, {mb:.0f} MB"
                                 + (f", {c.parts} chapters joined"
                                    if c.parts > 1 else ""))
                except OSError:
                    lines.append(f"   {c.transect}: {c.output.name}")
            elif c.skipped:
                lines.append(f"   {c.transect}: skipped ({c.skipped})")
            elif c.error:
                lines.append(f"   {c.transect}: FAILED ({c.error})")
        return "\n".join(lines)


def clip_dir(flight: Path, transect: str) -> Path:
    """``videos/transects/T1/`` -- mirrors the photo side."""
    return Path(flight) / layout.VIDEOS / layout.TRANSECTS / transect


def clip_name(resolved: ResolvedTransect) -> str:
    """Same stem the composite uses, marked as the untouched source."""
    return f"{resolved.output_stem('4K')}_source.mp4"


def trim_transect(
    resolved: ResolvedTransect,
    out_path: Path,
    scratch: Path,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
    force: bool = False,
) -> ClipResult:
    """Stream-copy a transect's footage out of its chapter(s)."""
    res = ClipResult(transect=resolved.transect.name)
    if not resolved.segments:
        res.skipped = "no footage covers these times"
        return res
    out_path = Path(out_path)
    if out_path.is_file() and not force:
        res.output, res.parts = out_path, len(resolved.segments)
        res.seconds = sum(s.dur_s for s in resolved.segments)
        res.skipped = "already present"
        return res

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res.parts = len(resolved.segments)
    res.seconds = sum(s.dur_s for s in resolved.segments)

    parts: list[Path] = []
    try:
        for i, seg in enumerate(resolved.segments):
            part = scratch / f"clip{i:02d}.mp4"
            # -ss before -i seeks on keyframes and is what makes this fast.
            ff.run(["-y", "-ss", f"{seg.in_s:.3f}", "-i", str(seg.chapter.path),
                    "-t", f"{seg.dur_s:.3f}", "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", str(part)],
                   cancel=cancel)
            parts.append(part)
            if progress:
                progress((i + 1) / len(resolved.segments),
                         f"{resolved.transect.name} part {i+1}")

        if len(parts) == 1:
            if out_path.exists():
                out_path.unlink()
            parts[0].replace(out_path)
        else:
            lst = scratch / "join.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                           encoding="utf-8")
            ff.run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(out_path)],
                   cancel=cancel)
            lst.unlink(missing_ok=True)
        res.output = out_path
    except ff.CancelledError:
        raise
    except Exception as ex:
        res.error = f"{type(ex).__name__}: {ex}".split("\n")[0][:160]
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
    return res


def trim_flight(
    flight: Path,
    resolved: Sequence[ResolvedTransect],
    scratch: Path,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
    force: bool = False,
) -> ClipReport:
    """Trim every resolved transect into ``videos/transects/T*/``."""
    rep = ClipReport()
    todo = [r for r in resolved if r.segments]
    if not todo:
        rep.warnings.append(
            "No transect has footage covering it. Check the GoPro timecode "
            "and the transect times."
        )
        return rep

    for i, r in enumerate(todo):
        if cancel is not None and cancel.is_set():
            raise ff.CancelledError("cancelled")
        d = clip_dir(flight, r.transect.name)
        out = d / clip_name(r)
        sub = (lambda f, m="", i=i: progress((i + f) / len(todo), m)) \
            if progress else None
        c = trim_transect(r, out, Path(scratch) / f"trim_{r.transect.name}",
                          progress=sub, cancel=cancel, force=force)
        rep.clips.append(c)
        if c.error:
            rep.errors.append(f"{c.transect}: {c.error}")

    # Judge coverage in seconds, not as a ratio. Floating point leaves a fully
    # covered transect a hair under 1.0, and a warning that fires at 100% is
    # how people learn to stop reading warnings.
    for r in todo:
        want = r.transect.duration_s()
        missing = want - sum(s.dur_s for s in r.segments)
        if missing > 1.0:
            rep.warnings.append(
                f"{r.transect.name}: {missing:.0f}s of the requested "
                f"{want:.0f}s has no footage ({r.coverage*100:.0f}% covered)"
            )
    if progress:
        progress(1.0, f"{len(rep.written)} clip(s) written")
    return rep
