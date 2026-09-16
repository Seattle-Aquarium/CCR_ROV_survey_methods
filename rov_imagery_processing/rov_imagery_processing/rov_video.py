"""
Turning the ROV camera stream into something we can seek.

Two steps, both of which exist because of hard-won failures in v1:

1. **Remux with exact timestamps.** The camera arrives over UDP at a strongly
   variable frame rate -- median ~28 fps, mean ~23, with hundreds of gaps.
   Letting a muxer assume a constant rate drifts by over two minutes across a
   36-minute dive. So every packet is stamped with the time the recorder
   captured. This is a remux, not a re-encode: no quality is lost.

2. **Resample to a constant rate.** Honest VFR timestamps make the file
   *unreliable to seek*: asking ffmpeg for 465.259 s can hand back the frame at
   466.708 s, and the error varies from 0.01 s to 1.45 s depending where you
   land. Because ``overlay`` matches frames by timestamp, that silently leaves
   the inset showing the wrong moment -- or nothing at all. Resampling once to
   the GoPro's exact frame rate (the ``fps`` filter uses the true timestamps, so
   nothing drifts) makes every later seek accurate to a single frame.

The proxy is only ever scaled down into a small inset, so its encode settings
are well beyond visible.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import ffmpeg_tools as ff

ProgressCB = Callable[[float, str], None]

TIME_BASE = Fraction(1, 90000)


@dataclass
class RovVideo:
    """A seekable ROV video placed on the absolute epoch timeline."""

    proxy_path: Path
    epoch_at_zero: float          # epoch of PTS 0 in the proxy
    duration: float
    width: int
    height: int
    fps: float
    frames: int
    warnings: list[str]

    def offset_for(self, epoch: float) -> float:
        """Seconds into the proxy corresponding to an absolute epoch."""
        return epoch - self.epoch_at_zero

    def covers(self, epoch: float) -> bool:
        o = self.offset_for(epoch)
        return -0.5 <= o <= self.duration + 0.5


def _stamp(*paths: Path) -> list[list]:
    """Name, size and modification time of each input, for a cache marker.

    A product is only as current as what it was built from. Without this the
    remux was reused whenever ``rov_full.mp4`` existed, so re-extracting a
    flight -- the fix for a bad extraction -- still composited from the video
    built out of the bad one.
    """
    out = []
    for p in paths:
        st = Path(p).stat()
        out.append([Path(p).name, st.st_size, st.st_mtime_ns])
    return out


def _read_frame_index(frames_csv: Path) -> tuple[list[float], list[int]]:
    ts: list[float] = []
    idr: list[int] = []
    with open(frames_csv, newline="") as f:
        for row in csv.DictReader(f):
            ts.append(float(row["ts"]))
            idr.append(int(row["is_idr"]))
    return ts, idr


def remux(
    cache_dir: Path,
    *,
    progress: ProgressCB | None = None,
    force: bool = False,
) -> tuple[Path, float, int]:
    """Wrap the raw H.264 in MP4 with exact per-frame PTS.

    Returns (mp4 path, epoch of PTS 0, frames written).
    """
    import av  # imported late: heavy

    cache_dir = Path(cache_dir)
    out = cache_dir / "rov_full.mp4"
    meta = cache_dir / "rov_full.json"
    raw = cache_dir / "rov_raw.h264"
    frames_csv = cache_dir / "rov_frames.csv"

    if not raw.is_file() or raw.stat().st_size == 0:
        raise FileNotFoundError(f"no ROV bitstream at {raw}")
    source = _stamp(raw, frames_csv)

    if out.is_file() and meta.is_file() and not force:
        try:
            info = json.loads(meta.read_text())
            if info.get("source") == source:
                if progress:
                    progress(1.0, "remux cache hit")
                return out, info["epoch_at_pts0"], info["frames_written"]
        except (OSError, ValueError, KeyError):
            pass                 # unreadable or stale marker: remux again

    meta.unlink(missing_ok=True)

    ts, idr = _read_frame_index(frames_csv)
    if not any(idr):
        raise ValueError(
            "no IDR keyframe in the ROV stream -- cannot start a clean MP4"
        )
    start_i = idr.index(1)
    t0 = ts[start_i]

    inp = av.open(str(raw), format="h264")
    ist = inp.streams.video[0]
    outc = av.open(str(out), "w")
    ost = outc.add_stream_from_template(ist)
    ost.time_base = TIME_BASE

    n = written = 0
    unindexed = 0
    last_pts = -1
    total = len(ts)
    for pkt in inp.demux(ist):
        if pkt.size == 0:
            continue
        if n < start_i:
            n += 1
            continue
        if n >= total:
            # Counted, not written. Timestamps are paired with packets by
            # position, so a bitstream that splits into more packets than the
            # index has rows has put every frame after the split at the wrong
            # time -- which has to be said, not silently truncated.
            unindexed += 1
            continue
        pts = int(round((ts[n] - t0) / TIME_BASE))
        if pts <= last_pts:                      # keep timestamps monotonic
            pts = last_pts + 1
        last_pts = pts
        pkt.stream = ost
        pkt.time_base = TIME_BASE
        pkt.pts = pkt.dts = pts
        pkt.duration = None
        outc.mux(pkt)
        written += 1
        n += 1
        if progress and written % 5000 == 0:
            progress(written / max(1, total - start_i), f"remuxing {written:,} frames")
    outc.close()
    inp.close()

    info = {
        "source": source,
        "epoch_at_pts0": t0,
        "frames_written": written,
        # packets and index rows that found no partner; both 0 when healthy
        "packets_unindexed": unindexed,
        "rows_unmatched": total - min(n, total),
        "span_s": ts[min(n, total) - 1] - t0,
        "width": ist.codec_context.width,
        "height": ist.codec_context.height,
    }
    meta.write_text(json.dumps(info, indent=2))
    if progress:
        progress(1.0, f"remuxed {written:,} frames")
    return out, t0, written


#: Slack either side of the requested window, so small edits to transect times
#: do not force a rebuild.
WINDOW_MARGIN_S = 45.0


def build_proxy(
    cache_dir: Path,
    target_fps: float,
    *,
    window: tuple[float, float] | None = None,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "veryfast",
    use_gpu: bool = True,
    progress: ProgressCB | None = None,
    force: bool = False,
    cancel=None,
) -> tuple[Path, float]:
    """Resample the ROV video to a constant frame rate.

    Returns (path, window_start_s) -- the offset of the proxy's PTS 0 within the
    remuxed source, which the caller folds into the epoch mapping.

    Only the span the transects actually need is encoded. Building the whole
    recording wastes most of the work: a 59-minute dive with four two-minute
    transects reads under 10 minutes of proxy, so 84% of it was never opened.

    NVENC is used when available. The proxy exists solely to be scaled down into
    a ~1100 px inset, so its encoder's quality-per-bit is irrelevant, and NVENC
    is around 2.7x faster on this class of content.
    """
    cache_dir = Path(cache_dir)
    src = cache_dir / "rov_full.mp4"
    out = cache_dir / "rov_cfr.mp4"
    marker = cache_dir / "rov_cfr.json"
    rate = ff.fps_rational(target_fps)

    info = ff.probe(src)
    src_dur = info.duration or 0.0

    if window is None:
        w0, w1 = 0.0, src_dur
    else:
        w0 = max(0.0, window[0] - WINDOW_MARGIN_S)
        w1 = min(src_dur, window[1] + WINDOW_MARGIN_S) if src_dur else window[1]
    w1 = max(w1, w0 + 1.0)

    source = _stamp(src)
    if out.is_file() and marker.is_file() and not force:
        try:
            prev = json.loads(marker.read_text())
            covers = (prev.get("fps") == rate
                      and prev.get("source") == source
                      and prev.get("window_start", 1e18) <= w0 + 0.01
                      and prev.get("window_end", -1e18) >= w1 - 0.01)
            if covers:
                if progress:
                    progress(1.0, "ROV proxy cache hit")
                return out, float(prev["window_start"])
        except Exception:
            pass
    marker.unlink(missing_ok=True)

    if use_gpu and ff.nvenc_available("h264_nvenc"):
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(crf + 2)]
        how = "GPU (h264_nvenc)"
    else:
        enc = ["-c:v", codec, "-crf", str(crf), "-preset", preset]
        how = f"CPU ({codec})"

    dur = w1 - w0
    if progress:
        progress(0.0, f"ROV proxy: {dur/60:.1f} min at {rate} fps on {how}")

    ff.run(
        ["-y", "-ss", f"{w0:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
         "-vf", f"fps={rate}", *enc,
         "-pix_fmt", "yuv420p", "-g", "48", "-an", "-sn", str(out)],
        progress=(lambda f: progress(f, f"ROV proxy on {how}")) if progress else None,
        total_seconds=dur,
        cancel=cancel,
    )
    marker.write_text(json.dumps(
        {"fps": rate, "window_start": w0, "window_end": w1, "encoder": how,
         "source": source}
    ))
    if progress:
        progress(1.0, "ROV proxy ready")
    return out, w0


#: How much longer than its window a proxy may run before its timeline is
#: called broken. A healthy one is within a frame or two.
PROXY_SLACK_S = 2.0


def timeline_warnings(cache_dir: Path, proxy_duration: float | None) -> list[str]:
    """Signs that the ROV video no longer maps linearly onto the clock.

    Both checks are on numbers already written during the build, so they cost
    nothing. Either failing means the inset will show the wrong moment.

    * The remux pairs packets with index rows by position; any unpaired one
      shifts every frame after it.
    * The proxy is one constant-rate pass over a known window, so its length
      is known in advance. On 14 September 2026 a 1,166 s window came out
      1,367 s long, with gaps of up to 1.5 s, because a second camera had been
      spliced into the stream -- and nothing said so.
    """
    cache_dir = Path(cache_dir)
    out: list[str] = []
    try:
        full = json.loads((cache_dir / "rov_full.json").read_text())
        extra = int(full.get("packets_unindexed", 0))
        short = int(full.get("rows_unmatched", 0))
        if extra or short:
            out.append(
                f"the ROV bitstream and its frame index disagree ({extra:,} "
                f"packet(s) with no timestamp, {short:,} timestamp(s) with no "
                f"packet); the ROV inset may be out of step with the GoPro")
    except (OSError, ValueError):
        pass
    try:
        cfr = json.loads((cache_dir / "rov_cfr.json").read_text())
        span = float(cfr["window_end"]) - float(cfr["window_start"])
        if proxy_duration and proxy_duration > span + PROXY_SLACK_S:
            out.append(
                f"the ROV proxy runs {proxy_duration:.0f}s for a {span:.0f}s "
                f"window, so its timeline is broken and the ROV inset may "
                f"show the wrong moment. Re-extract the telemetry (force) and "
                f"check the recording's video streams.")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return out


def prepare(
    cache_dir: Path,
    target_fps: float,
    *,
    needed_epochs: Sequence[tuple[float, float]] = (),
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "veryfast",
    use_gpu: bool = True,
    progress: ProgressCB | None = None,
    force: bool = False,
    cancel=None,
) -> RovVideo:
    """Remux then build the proxy, returning everything the compositor needs.

    The two steps share one caller-visible progress range, split in half. Handing
    the same callback to both would make the fraction jump back to zero when the
    proxy starts, and a progress bar that goes backwards reads as a hang -- which
    is exactly how this looked in the field.
    """
    warnings: list[str] = []

    def stage(lo: float, hi: float):
        if progress is None:
            return None
        return lambda f, m="": progress(lo + max(0.0, min(1.0, f)) * (hi - lo), m)

    _mp4, epoch0, frames = remux(cache_dir, progress=stage(0.0, 0.5), force=force)

    # Encode only the span the transects need, expressed relative to the
    # remuxed source's PTS 0.
    window = None
    if needed_epochs:
        lo = min(a for a, _ in needed_epochs) - epoch0
        hi = max(b for _, b in needed_epochs) - epoch0
        window = (lo, hi)

    proxy, w0 = build_proxy(cache_dir, target_fps, window=window, codec=codec,
                            crf=crf, preset=preset, use_gpu=use_gpu,
                            progress=stage(0.5, 1.0), force=force, cancel=cancel)
    info = ff.probe(proxy)
    if not info.ok:
        raise RuntimeError(f"could not probe the ROV proxy at {proxy}")
    warnings.extend(timeline_warnings(Path(cache_dir), info.duration))

    return RovVideo(
        proxy_path=proxy,
        # the proxy starts w0 seconds into the remuxed source
        epoch_at_zero=epoch0 + w0,
        duration=info.duration or 0.0,
        width=info.width or 1920,
        height=info.height or 1080,
        fps=info.fps or target_fps,
        frames=frames,
        warnings=warnings,
    )
