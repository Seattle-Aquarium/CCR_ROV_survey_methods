"""
What a RAW batch is, and the crop arithmetic that makes it exact.

Everything in here is pure: no Lightroom, no filesystem, no GUI. That is
deliberate -- the one number the whole feature turns on is the crop rectangle,
and a rectangle that is one pixel out is indistinguishable from a correct one
until 179 TIFs have been written. So the arithmetic is testable on its own.

Lightroom stores a crop as four fractions of the *uncropped* frame and rounds
the resulting pixel size to nearest. Six decimal places is what it writes back
to the catalog, so six is what we plan against: a rectangle that only survives
at full float precision is a rectangle that will drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

#: The delivered TIF size. Fixed by the survey protocol, not by the camera.
CROP_W = 4606
CROP_H = 4030

#: Lightroom writes crop fractions with this many decimals.
_PLACES = 6
_STEP = 10 ** -_PLACES

#: AI Denoise default, and the only value the protocol uses.
DENOISE_AMOUNT = 50

# A develop preset carrying an Enhance/Denoise filter looks like a way to
# script Denoise, and is not: applying one sets the flag without computing
# anything, and the export is bit-identical to un-denoised. See the note in
# the plugin's Job.lua. Denoise is applied to one photo in the Develop
# Detail panel and carried to the rest with Synchronize Settings.


def _round_half_up(x: float) -> int:
    """Lightroom rounds .5 away from zero; Python's round() does not."""
    return int(math.floor(x + 0.5))


@dataclass(frozen=True)
class CropRect:
    """A Lightroom crop, as the four fractions it stores."""

    left: float
    top: float
    right: float
    bottom: float

    def size_in(self, src_w: int, src_h: int) -> tuple[int, int]:
        """The pixel size Lightroom will report for this rectangle."""
        return (_round_half_up((self.right - self.left) * src_w),
                _round_half_up((self.bottom - self.top) * src_h))

    def as_settings(self) -> dict:
        """The develop-settings keys, ready to hand to the Lua side."""
        return {
            "CropLeft": self.left,
            "CropTop": self.top,
            "CropRight": self.right,
            "CropBottom": self.bottom,
            "CropAngle": 0.0,
            "CropConstrainToWarp": 0,
            "HasCrop": True,
        }


class CropImpossible(ValueError):
    """The requested output cannot be cut from this source."""


def crop_fractions(src_w: int, src_h: int,
                   out_w: int = CROP_W, out_h: int = CROP_H) -> CropRect:
    """A centred crop that Lightroom will round to exactly `out_w` x `out_h`.

    Centred rather than corner-anchored because the ROV camera points straight
    down: the subject is the middle of the frame, and trimming evenly keeps the
    optical centre in the centre of the delivered TIF.

    The naive fractions are quantised to the six decimals Lightroom keeps, which
    can move the rounded size by a pixel; when it does, the right and bottom
    edges are nudged one unit at a time until the prediction lands. Raises
    `CropImpossible` if the source is too small, or if no representable
    rectangle produces the requested size.
    """
    if out_w > src_w or out_h > src_h:
        raise CropImpossible(
            f"cannot cut {out_w}x{out_h} from a {src_w}x{src_h} frame")

    def quant(v: float) -> float:
        return round(round(v / _STEP) * _STEP, _PLACES)

    left = quant((1.0 - out_w / src_w) / 2.0)
    top = quant((1.0 - out_h / src_h) / 2.0)
    right = quant(1.0 - left)
    bottom = quant(1.0 - top)

    rect = CropRect(left, top, right, bottom)
    got_w, got_h = rect.size_in(src_w, src_h)
    if (got_w, got_h) == (out_w, out_h):
        return rect

    # Quantisation cost us a pixel. Walk the far edge -- never the near one, so
    # the crop stays anchored where the centring put it -- until it lands.
    right = _settle(left, right, src_w, out_w, "width")
    bottom = _settle(top, bottom, src_h, out_h, "height")
    rect = CropRect(left, top, right, bottom)
    got_w, got_h = rect.size_in(src_w, src_h)
    if (got_w, got_h) != (out_w, out_h):
        raise CropImpossible(
            f"no six-decimal crop of a {src_w}x{src_h} frame yields "
            f"{out_w}x{out_h} (closest {got_w}x{got_h})")
    return rect


def _settle(near: float, far: float, src: int, want: int, what: str) -> float:
    """Nudge `far` in single quantisation steps until the size is `want`."""
    for i in range(0, 2000):
        for cand in ({far} if i == 0 else
                     {round(far + i * _STEP, _PLACES),
                      round(far - i * _STEP, _PLACES)}):
            if not (near < cand <= 1.0):
                continue
            if _round_half_up((cand - near) * src) == want:
                return cand
    return far


@dataclass
class RawDevelopOptions:
    """One batch: a folder of GPRs, and what to do to every one of them."""

    crop_w: int = CROP_W
    crop_h: int = CROP_H
    remove_ca: bool = True
    denoise: bool = True
    denoise_amount: int = DENOISE_AMOUNT
    #: 16-bit ProPhoto is the delivery format; these are not offered in the GUI
    #: because the protocol fixes them, but the plumbing carries them so a
    #: future change is one edit rather than a search.
    bit_depth: int = 16
    color_space: str = "ProPhotoRGB"
    tiff_compression: str = "zip"      # 'zip' | 'none' | 'lzw'
    overwrite: bool = False

    @property
    def crop_label(self) -> str:
        return f"{self.crop_w}x{self.crop_h}"


@dataclass
class RawReport:
    """What a batch did. Shaped like ingest.ImportReport so the GUI's one
    reporting path in App._finish serves this tab too."""

    source: Path | None = None
    #: `App._finish` opens this in Explorer when the run ends.
    root: Path | None = None
    found: int = 0
    imported: int = 0
    cropped: int = 0
    denoised: int = 0
    exported: int = 0
    skipped: int = 0
    cancelled: bool = False
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"RAW develop: {self.source}" if self.source else "RAW develop"]
        lines.append(f"  GPR found: {self.found}   imported: {self.imported}")
        lines.append(f"  cropped: {self.cropped}   denoised: {self.denoised}")
        lines.append(f"  TIF exported: {self.exported} -> {self.root}")
        if self.skipped:
            lines.append(f"  already present, skipped: {self.skipped}")
        if self.seconds:
            lines.append(f"  took {self.seconds / 60:.1f} min")
        if self.cancelled:
            lines.append("  STOPPED before finishing")
        return "\n".join(lines)
