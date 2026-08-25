"""
Checking the TC-25 alignment against the ROV's own lights.

With GoPro Labs precision time the camera's timecode track *is* the answer, so
this is a verification rather than a solve. It matters because the failure mode
is silent: a wrong UTC offset, an unsynced camera, or a mistyped date all
produce a video that looks fine until someone notices the depth readout does not
match the picture.

The check uses a signal both recorders see. The ROV's lights are ramped to full
at the start of a dive and back to zero before ascending, and the down-facing
GoPro goes from near-black to lit when that happens.

Two traps this deliberately avoids:

* **Brightness is not linear in light power.** Altitude above the seabed and
  scene albedo move it too. So the score is agreement between "GoPro is dark"
  and "lights are off", not a correlation of the raw values.
* **Ambient light inverts the relationship near the surface.** On a shallow
  descent the GoPro can be bright *while the lights are off*, which is why a
  naive correlation locks onto the wrong offset. Restricting the comparison to
  samples where the lights actually change avoids this.

The ROV's own forward camera is useless for this -- its auto-gain is aggressive
enough that whole-frame luma barely moves across a full lights-off transition.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from . import ffmpeg_tools as ff
from .config import SyncConfig
from .survey import Chapter

ProgressCB = Callable[[float, str], None]

_PTS = re.compile(r"pts_time:([0-9.]+)")
_MEAN = re.compile(r"mean:\[([0-9.]+)")


@dataclass
class SyncReport:
    checked: bool = False
    ok: bool = False
    residual_s: float | None = None      # how far off the timecode mapping looks
    agreement: float | None = None       # 0..1, best dark/off agreement
    n_samples: int = 0
    n_transitions: int = 0
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.checked:
            return f"Sync check skipped: {self.message}"
        head = "PASS" if self.ok else "CHECK"
        bits = [f"[{head}] {self.message}"]
        if self.residual_s is not None:
            bits.append(f"residual {self.residual_s:+.2f}s")
        if self.agreement is not None:
            bits.append(f"agreement {self.agreement:.2f}")
        if self.n_samples:
            bits.append(f"{self.n_samples} samples")
        return "  |  ".join(bits)


def brightness_trace(
    video: Path,
    cache_dir: Path,
    *,
    ffmpeg: str | None = None,
    progress: ProgressCB | None = None,
    force: bool = False,
    cancel=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Whole-frame luma at ~1 Hz, from keyframes only.

    Decoding only keyframes avoids a full 4K HEVC decode -- roughly 6x realtime
    instead of far slower -- and 1 Hz is ample for locating a light ramp.
    ffmpeg's showinfo filter already reports per-frame mean luma, so no pixels
    need to come back to Python.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    npz = cache_dir / f"luma_{video.stem}.npz"
    if npz.is_file() and not force:
        d = np.load(npz)
        return d["pts"], d["luma"]

    if progress:
        progress(0.0, f"scanning {video.name} brightness")

    log = ff.run(
        ["-skip_frame", "nokey", "-i", str(video), "-an", "-sn", "-dn",
         "-vf", "scale=32:32:flags=area,format=gray,showinfo",
         "-fps_mode", "passthrough", "-f", "null", "-"],
        ffmpeg=ffmpeg, cancel=cancel, tail_lines=10_000_000,
    )

    pts: list[float] = []
    luma: list[float] = []
    for line in log.splitlines():
        if "pts_time:" not in line or "mean:[" not in line:
            continue
        mp, mm = _PTS.search(line), _MEAN.search(line)
        if mp and mm:
            pts.append(float(mp.group(1)))
            luma.append(float(mm.group(1)))

    arr_p, arr_l = np.asarray(pts, float), np.asarray(luma, float)
    np.savez_compressed(npz, pts=arr_p, luma=arr_l)
    if progress:
        progress(1.0, f"{len(arr_p)} brightness samples from {video.name}")
    return arr_p, arr_l


def _lights_at(lt: np.ndarray, lv: np.ndarray, when: np.ndarray) -> np.ndarray:
    """Zero-order hold, NaN outside the recorded span."""
    idx = np.searchsorted(lt, when, side="right") - 1
    out = np.full(when.shape, np.nan)
    ok = (idx >= 0) & (when <= lt[-1])
    out[ok] = lv[idx[ok]]
    return out


def validate(
    chapters: Sequence[Chapter],
    lights: tuple[np.ndarray, np.ndarray] | None,
    midnight_epoch: float,
    cache_dir: Path,
    cfg: SyncConfig,
    *,
    ffmpeg: str | None = None,
    progress: ProgressCB | None = None,
    search_s: float = 45.0,
    cancel=None,
) -> SyncReport:
    """Verify that TC-25 + the derived UTC offset lands the GoPro on the mcap."""
    rep = SyncReport()
    if not cfg.validate_with_lights:
        rep.message = "disabled in settings"
        return rep
    if lights is None:
        rep.message = "no Lights1 telemetry in this flight"
        rep.warnings.append(
            "Without light power the sync cannot be verified; the timecode is "
            "being trusted as-is."
        )
        return rep

    lt, lv = lights
    if lt.size < 2 or float(np.nanmax(lv)) - float(np.nanmin(lv)) < 0.5:
        rep.message = "the lights never changed during this flight"
        rep.warnings.append(
            "The lights were not ramped, so there is nothing to verify against."
        )
        return rep

    usable = [c for c in chapters if c.tc_start_s is not None]
    if not usable:
        rep.message = "no GoPro timecode track"
        rep.warnings.append(
            "The camera does not appear to have been synced with GoPro Labs "
            "precision time; transect times cannot be verified."
        )
        return rep

    all_pts: list[np.ndarray] = []
    all_lum: list[np.ndarray] = []
    for i, ch in enumerate(usable):
        if progress:
            progress(i / len(usable), f"brightness {i+1}/{len(usable)}")
        p, l = brightness_trace(ch.path, cache_dir, ffmpeg=ffmpeg,
                                progress=None, cancel=cancel)
        if p.size:
            # chapter position -> absolute epoch, via the timecode track
            all_pts.append(midnight_epoch + ch.tc_start_s + p)
            all_lum.append(l)

    if not all_pts:
        rep.message = "no brightness samples could be read"
        return rep

    epochs = np.concatenate(all_pts)
    luma = np.concatenate(all_lum)
    dark = luma < cfg.dark_luma

    offsets = np.arange(-search_s, search_s + 0.5, 0.5)
    best_off, best_agree, best_n = 0.0, -1.0, 0
    for off in offsets:
        lvals = _lights_at(lt, lv, epochs + off)
        ok = ~np.isnan(lvals)
        if ok.sum() < max(30, cfg.min_overlap_frac * epochs.size):
            continue
        agree = float(np.mean(dark[ok] == (lvals[ok] < cfg.lights_off)))
        if agree > best_agree:
            best_agree, best_off, best_n = agree, float(off), int(ok.sum())

    rep.checked = True
    rep.n_samples = best_n
    rep.agreement = best_agree if best_agree >= 0 else None

    if best_agree < 0:
        rep.message = "the video and telemetry do not overlap in time"
        rep.warnings.append(
            "No offset put the GoPro inside the telemetry window. Check the "
            "flight date and that these files belong to the same dive."
        )
        return rep

    # count the light transitions available, so a high score on a flat signal
    # is not mistaken for a confident result
    on = lv >= 0.5
    rep.n_transitions = int(np.count_nonzero(np.diff(on.astype(int)) != 0))

    rep.residual_s = best_off
    if rep.n_transitions == 0:
        rep.ok = False
        rep.message = "no full light transition to verify against"
        rep.warnings.append(
            "The lights never crossed 50% power, so the check is inconclusive."
        )
    elif abs(best_off) <= cfg.max_residual_s and best_agree >= 0.75:
        rep.ok = True
        rep.message = (
            f"timecode agrees with the lights to {abs(best_off):.1f}s "
            f"across {rep.n_transitions} transition(s)"
        )
    else:
        rep.ok = False
        rep.message = (
            f"timecode looks {best_off:+.1f}s away from the lights "
            f"(agreement {best_agree:.2f})"
        )
        rep.warnings.append(
            f"The light-based check suggests the GoPro and telemetry are "
            f"{best_off:+.1f}s apart. Composites will still be produced, but "
            f"verify the flight date and that the camera was synced with GoPro "
            f"Labs precision time before trusting the overlays."
        )
    return rep
