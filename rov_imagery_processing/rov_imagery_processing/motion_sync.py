"""
Measuring the GoPro's offset from the telemetry by the vehicle's own turns.

The transect times place the GoPro footage on the camera's timecode clock, and
the telemetry and ROV camera are on the vehicle's clock. When the two clocks
disagree the composite is wrong everywhere at once, and nothing about the
picture says so. On the 14 September 2026 OTS flight they were 34.8 s apart:
the telemetry panel and the ROV inset agreed with each other and both showed
what had happened half a minute before the GoPro frame beneath them.

`sync` checks the clocks against the ROV's lights, which needs the lights to be
ramped on camera and the chapter's own timecode to be readable. Neither holds
for a transect trim flown with the lights steady, which is exactly the case
that went unchecked. This check needs neither. Every turn the vehicle makes
rotates the down-facing GoPro's picture about its centre at the rate the
autopilot reports in ``ATTITUDE.yawspeed``. So the picture's rotation is
measured frame to frame, and the lag at which it best matches the yaw rate is
the offset between the clocks.

It is a measurement, so it is only acted on when it is unambiguous: a strong
correlation, a peak that stands well clear of every other lag, and the two
halves of the transect agreeing on the answer independently. A transect flown
dead straight has no turns to measure and is reported as unverified rather
than guessed at.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg_tools as ff
from .survey import Segment

ProgressCB = Callable[[float, str], None]

#: Frames per second the picture is sampled at. Yaw rate reaches the telemetry
#: at 10 Hz; 8 Hz resolves a turn and keeps a 20-minute transect cheap.
RATE = 8.0
#: Size the picture is reduced to. Rotation is a whole-frame property, so fine
#: detail only adds noise.
THUMB_W, THUMB_H = 96, 54
#: Bumped when what is cached changes.
TRACE_VERSION = 1

YAW_FIELD = "ATTITUDE.yawspeed"


@dataclass
class MotionSync:
    """How far the GoPro sits from the telemetry, as measured."""

    checked: bool = False
    #: Seconds to add to a transect's planned epoch to reach the vehicle-clock
    #: time its GoPro footage was actually shot. Positive: the camera's clock
    #: is behind the vehicle's.
    offset_s: float | None = None
    r: float | None = None                 # correlation at the best lag
    peak_ratio: float | None = None        # |r| at the peak / median |r|
    halves_s: tuple[float, float] | None = None
    confident: bool = False
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self, name: str) -> str:
        if not self.checked:
            return f"{name}: motion sync not checked ({self.message})"
        head = "OK" if self.confident else "UNVERIFIED"
        return f"{name}: [{head}] {self.message}"


# --------------------------------------------------------------------------
#  Rotation between frames
# --------------------------------------------------------------------------


def _blur(a: np.ndarray) -> np.ndarray:
    """A 3x3 binomial blur. Enough to steady the gradients on a thumbnail."""
    p = np.pad(a, 1, mode="edge")
    h = p[:, :-2] * 0.25 + p[:, 1:-1] * 0.5 + p[:, 2:] * 0.25
    return h[:-2] * 0.25 + h[1:-1] * 0.5 + h[2:] * 0.25


def _sample(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear lookup, plus a mask of the points that fell inside the image."""
    h, w = img.shape
    inside = (xs >= 0) & (xs <= w - 1) & (ys >= 0) & (ys <= h - 1)
    x = np.clip(xs, 0, w - 1.001)
    y = np.clip(ys, 0, h - 1.001)
    x0, y0 = x.astype(np.int64), y.astype(np.int64)
    fx, fy = x - x0, y - y0
    v = (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
         + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)
    return v, inside


def rotation_between(a: np.ndarray, b: np.ndarray, iters: int = 6) -> float:
    """Degrees `b` is rotated from `a` about the centre (counter-clockwise in
    image coordinates), allowing for translation.

    Gauss-Newton on a rigid warp: a few iterations of a 3-parameter least
    squares (angle, dx, dy) over every pixel, which for the small motion
    between frames 1/8 s apart converges in a handful of steps.
    """
    a = _blur(a.astype(np.float64))
    b = _blur(b.astype(np.float64))
    h, w = a.shape
    cy, cx = (h - 1) / 2, (w - 1) / 2
    Y, X = np.mgrid[0:h, 0:w].astype(np.float64)
    Xc, Yc = X - cx, Y - cy
    th = tx = ty = 0.0
    for _ in range(iters):
        c, s = np.cos(th), np.sin(th)
        # where each pixel of b comes from in a
        xs = c * (Xc - tx) + s * (Yc - ty) + cx
        ys = -s * (Xc - tx) + c * (Yc - ty) + cy
        warped, inside = _sample(a, xs, ys)
        gy, gx = np.gradient(warped)
        e = (warped - b)[inside]
        # a small extra rotation moves a pixel by (-y, x) * dth
        jt = (gx * -Yc + gy * Xc)[inside]
        jx, jy = gx[inside], gy[inside]
        J = np.stack([jt, jx, jy], axis=1)
        A = J.T @ J
        if e.size < 50 or np.linalg.cond(A) > 1e12:
            break
        dth, dx, dy = np.linalg.solve(A, J.T @ e)
        th += dth
        tx += dx
        ty += dy
        if abs(dth) < 1e-5 and abs(dx) < 1e-3 and abs(dy) < 1e-3:
            break
    return float(np.degrees(th))


def rotation_rate(frames: np.ndarray, rate: float = RATE, cancel=None) -> np.ndarray:
    """Picture rotation rate (deg/s) at each frame; the first is 0."""
    out = np.zeros(len(frames))
    for i in range(1, len(frames)):
        if cancel is not None and i % 500 == 0 and cancel.is_set():
            raise ff.CancelledError("cancelled")
        out[i] = rotation_between(frames[i - 1], frames[i]) * rate
    return out


# --------------------------------------------------------------------------
#  The trace for one transect
# --------------------------------------------------------------------------


def _segment_key(seg: Segment) -> dict:
    st = Path(seg.chapter.path).stat()
    return {"path": str(seg.chapter.path), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "in_s": round(seg.in_s, 4),
            "dur_s": round(seg.dur_s, 4), "rate": RATE,
            "thumb": [THUMB_W, THUMB_H], "version": TRACE_VERSION}


def rotation_trace(
    segments: Sequence[Segment],
    cache_dir: Path,
    *,
    ffmpeg: str | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> tuple[np.ndarray, np.ndarray]:
    """(seconds into the transect, rotation rate in deg/s) for its footage.

    Decoding 4K is the slow part -- minutes for a long transect -- so each
    segment's trace is cached against the file it came from and the slice.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    total = sum(s.dur_s for s in segments) or 1.0
    taus: list[np.ndarray] = []
    rates: list[np.ndarray] = []
    start = 0.0
    for i, seg in enumerate(segments):
        key = _segment_key(seg)
        stem = f"motion_{Path(seg.chapter.path).stem}_{int(seg.in_s * 1000)}"
        npz, meta = cache_dir / f"{stem}.npz", cache_dir / f"{stem}.json"
        rate = None
        if npz.is_file() and meta.is_file():
            try:
                if json.loads(meta.read_text()) == key:
                    rate = np.load(npz)["rate"]
            except (OSError, ValueError, KeyError):
                rate = None
        if rate is None:
            raw = cache_dir / f"{stem}.gray"
            base = start

            def dp(f: float, _b=base, _d=seg.dur_s, _i=i) -> None:
                if progress:
                    progress((_b + f * _d * 0.8) / total,
                             f"reading GoPro motion ({_i + 1}/{len(segments)})")

            try:
                ff.run(["-y", "-ss", f"{seg.in_s:.3f}", "-t", f"{seg.dur_s:.3f}",
                        "-i", str(seg.chapter.path), "-an", "-sn", "-dn",
                        "-vf", f"fps={RATE:g},scale={THUMB_W}:{THUMB_H}:flags=area,"
                               f"format=gray",
                        "-f", "rawvideo", str(raw)],
                       ffmpeg=ffmpeg, progress=dp, total_seconds=seg.dur_s,
                       cancel=cancel)
                frames = np.fromfile(raw, np.uint8)
            finally:
                raw.unlink(missing_ok=True)
            frames = frames[: frames.size - frames.size % (THUMB_W * THUMB_H)]
            frames = frames.reshape(-1, THUMB_H, THUMB_W)
            rate = rotation_rate(frames, cancel=cancel)
            np.savez_compressed(npz, rate=rate)
            meta.write_text(json.dumps(key))
        taus.append(start + np.arange(len(rate)) / RATE)
        rates.append(np.asarray(rate, float))
        start += seg.dur_s
        if progress:
            progress(start / total, "GoPro motion read")
    if not taus:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(taus), np.concatenate(rates)


# --------------------------------------------------------------------------
#  The offset
# --------------------------------------------------------------------------


def _robust(x: np.ndarray) -> np.ndarray:
    """Clip outliers -- a frame of silt or a fish reads as a huge rotation."""
    x = np.nan_to_num(np.asarray(x, float))
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826 + 1e-9
    return np.clip(x, med - 8 * mad, med + 8 * mad)


def _corr_at(tau, a, yaw_t, yaw_v, epoch_start, lags) -> np.ndarray:
    a = a - a.mean()
    na = np.sqrt((a * a).sum())
    out = np.full(len(lags), np.nan)
    for k, d in enumerate(lags):
        when = epoch_start + tau + d
        ok = (when >= yaw_t[0]) & (when <= yaw_t[-1])
        if ok.sum() < 0.8 * len(tau):
            continue
        b = np.interp(when, yaw_t, yaw_v)
        b = b - b.mean()
        nb = np.sqrt((b * b).sum())
        if na > 0 and nb > 0:
            out[k] = float((a * b).sum() / (na * nb))
    return out


def _peak(lags: np.ndarray, c: np.ndarray) -> tuple[float, float]:
    """Lag and signed correlation of the strongest |r|, refined parabolically."""
    ac = np.abs(np.nan_to_num(c))
    i = int(np.argmax(ac))
    d = float(lags[i])
    if 0 < i < len(c) - 1:
        y0, y1, y2 = ac[i - 1], ac[i], ac[i + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            d += 0.5 * (y0 - y2) / den * float(lags[1] - lags[0])
    return d, float(c[i])


def measure(
    tau: np.ndarray,
    rot_rate: np.ndarray,
    yaw_t: np.ndarray,
    yaw_rate_deg_s: np.ndarray,
    epoch_start: float,
    *,
    search_s: float = 180.0,
    min_r: float = 0.4,
    min_peak_ratio: float = 6.0,
    halves_agree_s: float = 1.0,
) -> MotionSync:
    """The lag between the picture's rotation and the telemetry's yaw rate.

    The sign of the correlation is not used: whether the picture turns with
    the vehicle or against it depends on how the camera is mounted.
    """
    ms = MotionSync()
    if len(tau) < 20 * RATE or len(yaw_t) < 20:
        ms.message = "too little footage or yaw telemetry to compare"
        return ms
    a = _robust(rot_rate)
    yv = np.asarray(yaw_rate_deg_s, float)
    ok = np.isfinite(yv)
    yaw_t, yv = np.asarray(yaw_t, float)[ok], yv[ok]

    coarse = np.arange(-search_s, search_s + 1e-9, 0.25)
    c = _corr_at(tau, a, yaw_t, yv, epoch_start, coarse)
    if np.all(np.isnan(c)):
        ms.message = "the telemetry does not cover this footage at any lag"
        return ms
    d0, _ = _peak(coarse, c)
    fine = np.arange(d0 - 1.0, d0 + 1.0 + 1e-9, 1 / 32)
    d, r = _peak(fine, _corr_at(tau, a, yaw_t, yv, epoch_start, fine))

    ac = np.abs(np.nan_to_num(c))
    ratio = float(abs(r) / (np.median(ac[ac > 0]) + 1e-9)) if np.any(ac > 0) else 0.0

    half = len(tau) // 2
    hs = []
    for sl in (slice(0, half), slice(half, None)):
        ch = _corr_at(tau[sl], a[sl], yaw_t, yv, epoch_start, coarse)
        hs.append(_peak(coarse, ch)[0] if not np.all(np.isnan(ch)) else np.nan)

    ms.checked = True
    ms.offset_s, ms.r, ms.peak_ratio = float(d), float(r), float(ratio)
    ms.halves_s = (float(hs[0]), float(hs[1]))
    agree = all(np.isfinite(h) and abs(h - d) <= halves_agree_s for h in hs)
    ms.confident = abs(r) >= min_r and ratio >= min_peak_ratio and agree
    detail = (f"r={abs(r):.2f}, peak {ratio:.0f}x background, halves "
              f"{hs[0]:+.1f}s / {hs[1]:+.1f}s")
    if ms.confident:
        ms.message = f"GoPro is {d:+.2f}s from the telemetry ({detail})"
    else:
        ms.message = (f"no unambiguous match between the GoPro's rotation and "
                      f"the yaw rate (best {d:+.1f}s, {detail})")
    return ms


def check_transect(
    segments: Sequence[Segment],
    store,
    epoch_start: float,
    cache_dir: Path,
    *,
    search_s: float = 180.0,
    min_r: float = 0.4,
    min_peak_ratio: float = 6.0,
    ffmpeg: str | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> MotionSync:
    """Measure one transect's footage against the flight's yaw rate."""
    yaw = store.series.get(YAW_FIELD)
    if yaw is None or yaw.v is None or len(yaw.t) < 20:
        ms = MotionSync(message=f"no {YAW_FIELD} in the telemetry")
        return ms
    tau, rot = rotation_trace(segments, cache_dir, ffmpeg=ffmpeg,
                              progress=progress, cancel=cancel)
    return measure(tau, rot, yaw.t, np.degrees(yaw.v), epoch_start,
                   search_s=search_s, min_r=min_r, min_peak_ratio=min_peak_ratio)
