"""
Building the composite video with ffmpeg.

One pass per segment: 4K GoPro base, ROV camera inset, stacked gauges, telemetry
panel, footer. Left to right the strip reads inset / gauges / panel, leaving the
right half of the frame clear.

Two things here are not obvious and were both bugs in v1:

* **Rotation.** The GoPro is mounted inverted and carries a -180 degree display
  matrix. Relying on ffmpeg's autorotate is a trap: it rotates the frames fed to
  the filter graph *and* copies the matrix onto the output, so a player rotates
  the finished composite a second time and the whole picture -- overlays
  included -- comes out upside down. We neutralise the input matrix with
  ``-display_rotation 0`` and apply the rotation ourselves.

* **setpts.** Seeking leaves each input with its own small timestamp offset, and
  ``overlay`` matches frames by PTS, so without ``setpts=PTS-STARTPTS`` on both
  video inputs the first frames of a clip find no inset frame to composite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import ffmpeg_tools as ff
from .config import AppConfig, Layout, Rendition
from .fsutil import publish
from .overlay import OverlaySequence
from .rov_video import RovVideo
from .survey import ResolvedTransect, Segment

ProgressCB = Callable[[float, str], None]


def rotation_filter(rot: int) -> str | None:
    return {0: None, 180: "hflip,vflip", 90: "transpose=1", 270: "transpose=2"}[rot % 360]


@dataclass
class OverlayInput:
    """One PNG sequence feeding the filter graph."""

    prefix: str
    index: int
    x: str
    y: str
    start_number: int


def build_filter(
    cfg: Layout,
    base_rot: int,
    overlays: Sequence[OverlayInput],
    out_pix_fmt: str,
    out_height: int | None,
) -> str:
    parts: list[str] = []
    b = cfg.border_px
    parts.append(
        f"[1:v]setpts=PTS-STARTPTS,scale={cfg.inset_width}:-2:flags=lanczos,"
        f"format=yuv420p10le,pad=iw+{2*b}:ih+{2*b}:{b}:{b}:color={cfg.border_color}[rov]"
    )
    rf = rotation_filter(base_rot)
    parts.append(
        f"[0:v]setpts=PTS-STARTPTS,{rf + ',' if rf else ''}format=yuv420p10le[base]"
    )
    parts.append(
        f"[base][rov]overlay=x={cfg.margin}:y={cfg.margin}:eof_action=repeat[v0]"
    )
    last = "v0"
    for k, o in enumerate(overlays, start=1):
        lab = f"o{k}"
        parts.append(f"[{o.index}:v]format=yuva420p10le[{lab}]")
        parts.append(
            f"[{last}][{lab}]overlay=x={o.x}:y={o.y}:eof_action=repeat[v{k}]"
        )
        last = f"v{k}"
    if out_height:
        parts.append(f"[{last}]scale=-2:{out_height}:flags=lanczos[vs]")
        last = "vs"
    parts.append(f"[{last}]format={out_pix_fmt}[out]")
    return ";".join(parts)


def _overlay_layout(cfg: Layout, seq: OverlaySequence) -> list[tuple[str, str, str]]:
    """(prefix, x, y) for each overlay, packed left after the inset."""
    inset_total = cfg.inset_width + 2 * cfg.border_px
    x = cfg.margin + inset_total + cfg.inset_gap
    out: list[tuple[str, str, str]] = []
    if seq.gauge_size:
        out.append(("gauge", str(x), str(cfg.margin)))
        x += seq.gauge_size[0] + cfg.gauge_gap
    out.append(("panel", str(x), str(cfg.margin)))
    if seq.footer_size:
        out.append(("footer", str(cfg.margin), f"H-h-{cfg.margin}"))
    return out


def compose_segment(
    *,
    segment: Segment,
    seq: OverlaySequence,
    rov: RovVideo,
    epoch_start: float,
    out_path: Path,
    app: AppConfig,
    rendition: Rendition,
    overlay_offset_s: float,
    scratch: Path,
    progress: ProgressCB | None = None,
    cancel=None,
) -> Path:
    """Render one contiguous slice of one GoPro chapter."""
    cfg = app.layout
    rov_in = rov.offset_for(epoch_start)
    if rov_in < -1.0:
        raise ValueError(
            f"the ROV video starts after this transect "
            f"({-rov_in:.1f}s late); check the sync"
        )

    args: list[str] = [
        "-y",
        "-display_rotation", "0",
        "-ss", f"{segment.in_s:.3f}", "-t", f"{segment.dur_s:.3f}",
        "-i", str(segment.chapter.path),
        "-ss", f"{max(0.0, rov_in):.3f}", "-t", f"{segment.dur_s:.3f}",
        "-i", str(rov.proxy_path),
    ]

    overlays: list[OverlayInput] = []
    idx = 1
    start_frame = int(round(overlay_offset_s * seq.fps))
    for prefix, x, y in _overlay_layout(cfg, seq):
        idx += 1
        args += [
            "-framerate", f"{seq.fps:g}",
            "-start_number", str(start_frame),
            "-i", str(seq.directory / f"{prefix}_%06d.png"),
        ]
        overlays.append(OverlayInput(prefix, idx, x, y, start_frame))

    filt = build_filter(cfg, segment.chapter.rotation, overlays,
                        rendition.pix_fmt, rendition.height)
    # Scratch lives in the cache, not next to the output: flight folders sit deep
    # inside Dropbox and a descriptive filename plus a long path trips Windows'
    # 260-character MAX_PATH limit. It also keeps the composites folder clean.
    scratch.mkdir(parents=True, exist_ok=True)
    script = scratch / "filter.txt"
    script.write_text(filt, encoding="utf-8")

    args += ["-filter_complex_script", str(script), "-map", "[out]"]
    args += ["-an"] if not app.keep_audio else ["-map", "0:a?", "-c:a", "copy"]
    args += [
        "-c:v", rendition.codec, "-crf", str(rendition.crf),
        "-preset", rendition.preset, "-pix_fmt", rendition.pix_fmt,
        *rendition.extra, "-movflags", "+faststart", str(out_path),
    ]

    try:
        ff.run(
            args,
            progress=(lambda f: progress(f, f"encoding {out_path.name}")) if progress else None,
            total_seconds=segment.dur_s,
            cancel=cancel,
        )
    finally:
        script.unlink(missing_ok=True)
    return out_path


def concat(parts: Sequence[Path], out_path: Path, scratch: Path,
           cancel=None, log: Callable[[str], None] | None = None) -> Path:
    """Join segments. Identical codec parameters, so this is a stream copy.

    The join always happens in scratch and the result is published in one move,
    so the flight folder never holds a partial file and a locked destination is
    survivable. Returns where the file actually landed.
    """
    parts = [p for p in parts if p.is_file()]
    if not parts:
        raise ValueError("nothing to concatenate")
    if len(parts) == 1:
        return publish(parts[0], out_path, log=log)

    scratch.mkdir(parents=True, exist_ok=True)
    lst = scratch / "concat.txt"
    joined = scratch / "joined.mp4"
    lst.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    try:
        ff.run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c", "copy", "-movflags", "+faststart", str(joined)],
               cancel=cancel)
    finally:
        lst.unlink(missing_ok=True)
        for p in parts:
            p.unlink(missing_ok=True)
    return publish(joined, out_path, log=log)


def compose_transect(
    *,
    resolved: ResolvedTransect,
    seq: OverlaySequence,
    rov: RovVideo,
    out_dir: Path,
    scratch: Path,
    app: AppConfig,
    rendition: Rendition,
    progress: ProgressCB | None = None,
    cancel=None,
) -> Path:
    """Render one transect at one resolution, joining chapters if it spans them."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{resolved.output_stem(rendition.label)}.mp4"
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    total = sum(s.dur_s for s in resolved.segments) or 1.0
    done = 0.0
    parts: list[Path] = []

    for i, seg in enumerate(resolved.segments):
        part = scratch / f"part{i:02d}.mp4"
        # overlays are indexed from the start of the transect, so a later
        # segment must start reading partway into the sequence
        offset = sum(s.dur_s for s in resolved.segments[:i])

        def seg_progress(f: float, msg: str, _o=done, _d=seg.dur_s) -> None:
            if progress:
                progress((_o + f * _d) / total, msg)

        compose_segment(
            segment=seg, seq=seq, rov=rov,
            epoch_start=resolved.epoch_start + offset,
            out_path=part, app=app, rendition=rendition,
            overlay_offset_s=offset, scratch=scratch,
            progress=seg_progress if progress else None,
            cancel=cancel,
        )
        parts.append(part)
        done += seg.dur_s

    # All the encoding is done by this point, so publishing reports at 1.0;
    # any wait for a locked destination surfaces as a message, not a stall.
    out_path = concat(parts, out_path, scratch, cancel=cancel,
                      log=(lambda m: progress(1.0, m)) if progress else None)
    if progress:
        progress(1.0, f"{out_path.name} ({out_path.stat().st_size / 1e6:.0f} MB)")
    return out_path
