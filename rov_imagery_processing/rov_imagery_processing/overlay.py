"""
The telemetry panel, footer, and the per-frame overlay sequence.

The panel uses Montserrat (the Seattle Aquarium primary typeface) rather than a
monospace face. Montserrat's digits are not fixed-width, so columns are aligned
by *measuring* each string and right-aligning the value against a common edge,
which keeps the numbers in a clean column while staying on-brand.

Panels are drawn at `Layout.overlay_fps` (default 6 Hz), not at the video frame
rate: the MAVLink streams behind them only update at 2-10 Hz, so drawing every
frame would be roughly four times the work for no visible difference. ffmpeg
holds the most recent panel in between.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import gauges
from .config import PANEL_ROWS, Layout, Row

ProgressCB = Callable[[float, str], None]

#: Below this many frames, rendering in one process is faster than starting
#: several. Process startup in a packaged build is not cheap -- each worker
#: re-launches the executable and re-imports Pillow and NumPy.
_PARALLEL_FLOOR = 400

#: Frames per unit of work handed to a worker. Large enough that the per-task
#: overhead disappears, small enough that progress moves and a cancel is acted
#: on within a second or two.
_CHUNK = 120


def worker_count(requested: int | None = None) -> int:
    """How many processes to render with.

    Leaves two cores for the rest of the machine: this runs for tens of
    minutes and the operator is normally still using their laptop. The cap
    exists because the work is bound by drawing and PNG compression, and past
    a dozen workers the disk and the process startup cost eat the gain.
    """
    if requested and requested > 0:
        return int(requested)
    env = os.environ.get("UTC_OVERLAY_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, min((os.cpu_count() or 2) - 2, 12))


def _font(size: int, weight: str = "medium") -> ImageFont.FreeTypeFont:
    return gauges._font(size, weight)


def _rgba(color: str, alpha: float | None = None):
    return gauges._rgba(color, alpha)


def format_value(row: Row, value) -> str:
    if value is None:
        return "--"
    if isinstance(value, str):
        return value
    if isinstance(value, float) and math.isnan(value):
        return "--"
    if row.digits is None:
        return str(value)
    return f"{value:.{row.digits}f}"


@dataclass
class PanelMetrics:
    """Column geometry, measured once so every frame lays out identically."""

    label_w: int
    value_w: int
    unit_w: int
    line_h: int
    width: int
    height: int


#: Longest mode string, so the box is sized for the widest thing it can show
#: rather than for whatever happens to be on screen at the sample moment.
_WIDEST_MODE = "MOTOR_DETECT"


def measure_panel(cfg: Layout, rows: Sequence[Row] = PANEL_ROWS) -> PanelMetrics:
    f = _font(cfg.text_size, "medium")
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def w(s: str) -> int:
        return int(math.ceil(probe.textlength(s, font=f)))

    label_w = value_w = unit_w = 0
    for r in rows:
        if r.spacer:
            continue
        label_w = max(label_w, w(r.label))
        unit_w = max(unit_w, w(r.unit))
        if r.digits is None:
            sample = _WIDEST_MODE
        else:
            # widest plausible numeric: sign, four integer digits, decimals
            sample = "-8888." + "8" * r.digits if r.digits else "-8888"
        value_w = max(value_w, w(sample))

    asc, desc = f.getmetrics()
    line_h = int(round((asc + desc) * cfg.line_spacing))
    n = len(rows)
    width = cfg.panel_pad_x * 2 + label_w + cfg.value_gap + value_w
    if unit_w:
        width += cfg.unit_gap + unit_w
    if cfg.panel_width:
        width = cfg.panel_width
    height = cfg.panel_pad_y * 2 + line_h * n
    return PanelMetrics(label_w, value_w, unit_w, line_h, int(width), int(height))


def render_panel(vals: dict, cfg: Layout, m: PanelMetrics,
                 rows: Sequence[Row] = PANEL_ROWS) -> Image.Image:
    img = Image.new("RGBA", (m.width, m.height), _rgba(cfg.panel_bg, cfg.panel_bg_alpha))
    d = ImageDraw.Draw(img)
    f = _font(cfg.text_size, "medium")

    x_label = cfg.panel_pad_x
    x_value = x_label + m.label_w + cfg.value_gap + m.value_w      # right edge
    x_unit = x_value + cfg.unit_gap

    y = cfg.panel_pad_y
    for r in rows:
        if r.spacer:
            y += m.line_h
            continue
        d.text((x_label, y), r.label, font=f, fill=_rgba(cfg.panel_fg), anchor="la")
        d.text((x_value, y), format_value(r, vals.get(r.var)), font=f,
               fill=_rgba(cfg.panel_fg), anchor="ra")
        if r.unit:
            d.text((x_unit, y), r.unit, font=f, fill=_rgba(cfg.panel_muted), anchor="la")
        y += m.line_h

    return gauges._with_border(img, cfg)


FOOTER_PAD_X, FOOTER_PAD_Y = 18, 10


def measure_footer(sample: str, cfg: Layout) -> tuple[int, int]:
    """Fixed footer box size for a whole sequence.

    Every frame of an overlay sequence MUST be the same size. Montserrat is
    proportional, so sizing the box to its own text makes the box breathe as the
    clock ticks -- and ffmpeg responds by rebuilding the entire filter graph on
    almost every frame, dropping thousands of frames and losing overlays
    non-deterministically.

    Measuring with every digit replaced by the widest one gives a width that
    fits any time the clock can show.
    """
    f = _font(cfg.footer_size, "medium")
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    widest = max("0123456789", key=lambda d: probe.textlength(d, font=f))
    worst = re.sub(r"\d", widest, sample)
    w = int(math.ceil(probe.textlength(worst, font=f)))
    asc, desc = f.getmetrics()
    return (w + 2 * FOOTER_PAD_X, asc + desc + 2 * FOOTER_PAD_Y)


def render_footer(text: str, cfg: Layout, size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGBA", size, _rgba(cfg.panel_bg, cfg.panel_bg_alpha))
    ImageDraw.Draw(img).text((FOOTER_PAD_X, FOOTER_PAD_Y), text,
                             font=_font(cfg.footer_size, "medium"),
                             fill=_rgba(cfg.panel_fg), anchor="la")
    return img


# --------------------------------------------------------------------------
#  Sequence rendering
# --------------------------------------------------------------------------


@dataclass
class OverlaySequence:
    directory: Path
    frames: int
    fps: float
    panel_size: tuple[int, int]
    gauge_size: tuple[int, int] | None
    footer_size: tuple[int, int] | None


class _Cancelled(Exception):
    """Internal: the operator pressed Stop. Re-raised as CancelledError."""


def _raise_cancelled():
    from .ffmpeg_tools import CancelledError
    raise CancelledError("cancelled")


def _render_serial(out_dir, cfg, m, fsize, n, frame_of, progress, cancel) -> None:
    for k in range(n):
        if cancel is not None and cancel.is_set():
            _raise_cancelled()
        _render_frames((out_dir, cfg, m, fsize, [frame_of(k)]))
        if progress and (k % max(1, n // 40) == 0):
            progress(k / n, f"overlays {k}/{n}")


def _render_parallel(out_dir, cfg, m, fsize, n, frame_of, nw,
                     progress, cancel) -> None:
    """Draw the sequence across `nw` processes.

    Work is handed out in chunks rather than per frame, because a frame takes
    milliseconds and the round trip would dominate. Chunks stay small enough
    that Stop is acted on promptly -- the operator should not have to wait out
    a whole transect.
    """
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

    chunks = [list(range(i, min(i + _CHUNK, n))) for i in range(0, n, _CHUNK)]
    done = 0
    if progress:
        progress(0.0, f"overlays 0/{n} on {nw} cores")

    with ProcessPoolExecutor(max_workers=nw) as ex:
        pending, queued = set(), list(chunks)
        try:
            # Keep roughly two chunks in flight per worker: enough that nobody
            # idles, few enough that a cancel does not have to unwind much.
            while queued or pending:
                while queued and len(pending) < nw * 2:
                    ks = queued.pop(0)
                    pending.add(ex.submit(
                        _render_frames,
                        (out_dir, cfg, m, fsize, [frame_of(k) for k in ks])))
                finished, pending = wait(pending, timeout=0.5,
                                         return_when=FIRST_COMPLETED)
                for fut in finished:
                    done += fut.result()
                    if progress:
                        progress(done / n, f"overlays {done}/{n} on {nw} cores")
                if cancel is not None and cancel.is_set():
                    raise _Cancelled
        except BaseException:
            for fut in pending:
                fut.cancel()
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    if progress:
        progress(1.0, f"overlays {n}/{n}")


def _render_frames(job) -> int:
    """Draw one contiguous run of frames. Runs in a worker process.

    Everything it needs arrives as plain data -- the telemetry samples were
    taken in the parent and the footer strings formatted there. That keeps the
    telemetry store, which is tens of megabytes and holds NumPy arrays, out of
    the pickle entirely, and means the caller's footer callback never has to be
    picklable.
    """
    out_dir, cfg, m, fsize, frames = job
    for k, vals, ftext in frames:
        render_panel(vals, cfg, m).save(out_dir / f"panel_{k:06d}.png")
        if cfg.show_gauges:
            gauges.render_gauges(vals, cfg).save(out_dir / f"gauge_{k:06d}.png")
        if fsize is not None and ftext is not None:
            render_footer(ftext, cfg, fsize).save(out_dir / f"footer_{k:06d}.png")
    return len(frames)


def render_sequence(
    out_dir: Path,
    store,                       # telemetry.TelemetryStore
    epoch_start: float,
    duration: float,
    cfg: Layout,
    *,
    footer_text: Callable[[float], str] | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
    workers: int | None = None,
) -> OverlaySequence:
    """Render panel / gauge / footer PNG sequences for one clip.

    `epoch_start` is absolute, so the overlay content is driven directly by the
    telemetry clock and never by a position within a video file.

    Frames are independent, so they are drawn across several processes. This is
    the pipeline's one genuinely serial stretch -- ffmpeg already uses every
    core when it encodes, but drawing panels is Python holding the GIL, and it
    pegged exactly one core for tens of minutes while the other nineteen idled.
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    n = max(1, int(math.ceil(duration * cfg.overlay_fps)))
    m = measure_panel(cfg)
    gsize = gauges.gauge_size(cfg) if cfg.show_gauges else None

    # Sized once, from the widest text the footer can ever show, so every frame
    # of the sequence is identical in size -- see measure_footer.
    fsize = None
    if cfg.show_footer and footer_text is not None:
        fsize = measure_footer(footer_text(epoch_start), cfg)

    # Sample the telemetry and format the footers up front, in this process.
    # Both are cheap next to drawing, and doing them here is what lets the
    # workers take plain data.
    def _frame(k: int):
        epoch = epoch_start + k / cfg.overlay_fps
        ftext = footer_text(epoch) if (fsize is not None and footer_text) else None
        return (k, store.sample(epoch), ftext)

    nw = worker_count(workers)
    if nw > 1 and n >= _PARALLEL_FLOOR:
        try:
            _render_parallel(out_dir, cfg, m, fsize, n, _frame, nw,
                             progress, cancel)
        except _Cancelled:
            _raise_cancelled()
        except Exception as ex:                      # pragma: no cover
            # A pool that will not start is not a reason to fail the run --
            # fall back to the single-process path and say so.
            if progress:
                progress(0.0, f"parallel render unavailable ({type(ex).__name__}); "
                              f"drawing on one core")
            _render_serial(out_dir, cfg, m, fsize, n, _frame, progress, cancel)
    else:
        _render_serial(out_dir, cfg, m, fsize, n, _frame, progress, cancel)

    panel_px = (m.width + 2 * cfg.border_px, m.height + 2 * cfg.border_px)
    gauge_px = None
    if gsize:
        gauge_px = (gsize[0] + 2 * cfg.border_px, gsize[1] + 2 * cfg.border_px)

    _assert_uniform(out_dir, n)

    if progress:
        progress(1.0, f"{n} overlay frames")
    return OverlaySequence(out_dir, n, cfg.overlay_fps, panel_px, gauge_px, fsize)


def _assert_uniform(out_dir: Path, n: int) -> None:
    """Every frame of a sequence must share one size.

    A sequence whose frames change size makes ffmpeg reconfigure its filter
    graph mid-stream, which drops frames and drops overlays unpredictably. It is
    a silent, non-deterministic failure, so it is worth a cheap explicit check
    rather than trusting that the rendering code never varies.
    """
    from PIL import Image as _Image

    for prefix in ("panel", "gauge", "footer"):
        files = sorted(out_dir.glob(f"{prefix}_*.png"))
        if not files:
            continue
        sizes = set()
        for p in files:
            with _Image.open(p) as im:
                sizes.add(im.size)
            if len(sizes) > 1:
                raise RuntimeError(
                    f"{prefix} overlay frames are not a uniform size "
                    f"({sorted(sizes)}). ffmpeg cannot composite a sequence "
                    f"whose dimensions change; fix the renderer."
                )
        if len(files) != n:
            raise RuntimeError(
                f"expected {n} {prefix} frames, found {len(files)}"
            )
