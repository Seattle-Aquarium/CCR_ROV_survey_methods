"""
Cutting the original 4K down to the transects.

Separate from compositing on purpose: this produces the *untouched* footage for
a transect, so it is a stream copy -- no decode, no re-encode, no quality lost,
and roughly as fast as the disk can move the bytes. Compositing is the other
job, and slow for good reasons.

**Cuts land on keyframes.** A stream copy cannot start mid-GOP, so the start of
a clip snaps back to the nearest keyframe before the requested time -- with
GoPro's 1.001 s keyframe interval that is up to a second of extra footage at the
head. Re-encoding would be frame-accurate, and would also throw away the thing
this function exists to preserve.

That head has to be known when the trim is composited, because the composite
places the trim on the clock at its transect's start. Treating the first frame
as the start put the GoPro up to a second behind the telemetry and the ROV
inset. So the cut measures the head and writes it into the trim's metadata
(`HEAD_KEY`), and compositing skips it. A trim cut before that was recorded
has its head estimated from its timecode and keyframe spacing instead.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_tools as ff
from . import layout
from .survey import Chapter, ResolvedTransect, SurveyPlan

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


def find_trims(flight: Path) -> dict[str, Path]:
    """Per-transect trims already on disk, keyed by transect name.

    Keyed by the folder they sit in rather than by anything inside the file.
    A trim is a stream copy, and a stream copy carries the *source* chapter's
    timecode track unchanged -- so every trim from one GoPro recording claims
    the same start time. Trusting that made a transect resolve against all four
    trims at once and produced a 66-minute composite of a 10-minute transect.
    The folder name is the one piece of provenance that survives the copy.
    """
    root = Path(flight) / layout.VIDEOS / layout.TRANSECTS
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        hits = sorted(d.glob("*_source.mp4")) or sorted(d.glob("*.mp4"))
        if hits:
            out[d.name] = hits[0]
    return out


#: The metadata tag a trim's head is written to. ``comment`` because the MP4
#: muxer writes it without extra flags and it survives a stream copy.
HEAD_TAG = "comment"
#: What goes in it: seconds of footage before the transect starts.
HEAD_KEY = "transect_head_s"
_HEAD_RE = re.compile(HEAD_KEY + r"=([0-9]+(?:\.[0-9]+)?)")


def keyframe_at_or_before(path: Path, t: float) -> float | None:
    """Time of the keyframe a stream copy asked to start at `t` starts on.

    Walks packets forward from a backward seek rather than trusting where the
    seek landed, so a demuxer that seeks a keyframe too far back still gives
    the right answer. None when the file cannot be read.
    """
    import av  # imported late: heavy

    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            tb = s.time_base
            start = float((s.start_time or 0) * tb)
            c.seek(int((max(0.0, t) + start) / tb), stream=s,
                   backward=True, any_frame=False)
            best = None
            for pkt in c.demux(s):
                if pkt.pts is None:
                    continue
                pt = float(pkt.pts * tb) - start
                if pt > t + 1e-3:
                    break
                if pkt.is_keyframe:
                    best = pt
            return best
    except Exception:
        return None


def trim_head_s(path: Path) -> float | None:
    """The head a trim recorded when it was cut, or None if it has none."""
    import av

    try:
        with av.open(str(path)) as c:
            m = _HEAD_RE.search(c.metadata.get(HEAD_TAG, "") or "")
    except Exception:
        return None
    return float(m.group(1)) if m else None


def keyframe_interval(path: Path) -> float | None:
    """Seconds between the first two keyframes, or None."""
    import av

    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            keys: list[float] = []
            for pkt in c.demux(s):
                if pkt.pts is not None and pkt.is_keyframe:
                    keys.append(float(pkt.pts * s.time_base))
                    if len(keys) == 2:
                        return keys[1] - keys[0]
    except Exception:
        return None
    return None


def estimate_head(source_tc_s: float | None, transect_start_s: float,
                  gop_s: float | None) -> float | None:
    """A trim's head, for one cut before the head was recorded.

    A trim keeps its source chapter's timecode, so the transect started
    ``transect_start_s - source_tc_s`` into that chapter, and the cut began on
    the last keyframe at or before that. GoPro keyframes fall at fixed
    intervals from the start of a chapter, so the head is the remainder.
    """
    if source_tc_s is None or not gop_s or gop_s <= 0:
        return None
    in_s = transect_start_s - source_tc_s
    if in_s <= 0:
        return 0.0              # the cut began at the chapter's first frame
    k = math.floor(in_s / gop_s + 1e-6)
    return max(0.0, in_s - k * gop_s)


def trim_heads(
    plan: SurveyPlan,
    paths: dict[str, Path],
    trims: dict[str, Chapter],
) -> tuple[dict[str, float], list[str]]:
    """Head of every trim a plan will composite, plus what to tell the operator."""
    heads: dict[str, float] = {}
    notes: list[str] = []
    for site in plan.sites:
        for t in site.transects:
            if t.name not in paths or t.name not in trims:
                continue
            head = trim_head_s(paths[t.name])
            if head is not None:
                heads[t.name] = head
                continue
            ch = trims[t.name]
            est = estimate_head(ch.tc_start_s, t.start_s(),
                                keyframe_interval(paths[t.name]))
            if est is None:
                notes.append(
                    f"{t.name}: the trim does not record where its transect "
                    f"starts and it could not be worked out; the GoPro may run "
                    f"up to a keyframe interval (~1 s) behind the telemetry")
                continue
            heads[t.name] = est
            notes.append(
                f"{t.name}: the trim predates recorded start offsets; its "
                f"first {est:.2f}s precede the transect (estimated from its "
                f"timecode and keyframe spacing) and are skipped")
    return heads, notes


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

    # Only the first part can start mid-GOP: every later one starts at its
    # chapter's first frame, which is a keyframe.
    first = resolved.segments[0]
    kf = (keyframe_at_or_before(first.chapter.path, first.in_s)
          if first.in_s > 0 else 0.0)
    tag = ([] if kf is None else
           ["-metadata", f"{HEAD_TAG}={HEAD_KEY}={max(0.0, first.in_s - kf):.6f}"])

    parts: list[Path] = []
    try:
        for i, seg in enumerate(resolved.segments):
            part = scratch / f"clip{i:02d}.mp4"
            # -ss before -i seeks on keyframes and is what makes this fast.
            ff.run(["-y", "-ss", f"{seg.in_s:.3f}", "-i", str(seg.chapter.path),
                    "-t", f"{seg.dur_s:.3f}", "-c", "copy",
                    "-avoid_negative_ts", "make_zero", *tag,
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
                    "-c", "copy", *tag, "-movflags", "+faststart",
                    str(out_path)],
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
