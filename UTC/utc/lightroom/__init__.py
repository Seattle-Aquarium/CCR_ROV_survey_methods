"""
Batch-developing a folder of GoPro GPR raws through Lightroom Classic.

Crop to a fixed pixel size, remove chromatic aberration, run AI Denoise, and
export 16-bit ProPhoto TIFs into a sibling TIF folder. Lightroom does the
imaging; this package decides what it is asked to do and watches it happen.

The parts, in the order a run touches them:

* `gpr`       -- read frame sizes out of the raws without decoding them
* `spec`      -- what a batch is, and the crop arithmetic
* `preflight` -- everything that must be true before it is worth starting
* `install`   -- find Lightroom, install the plugin, mint a scratch catalog
* `runner`    -- drive one run and report progress
* `catalog`   -- read the scratch catalog while Lightroom is using it
* `denoise_ui`-- the one unsupported step, isolated
* `plugin/`   -- the Lightroom SDK plugin that does the work in-application
"""

from __future__ import annotations

from .preflight import Preflight, check, tif_dir_for  # noqa: F401
from .spec import (  # noqa: F401
    CROP_H,
    CROP_W,
    DENOISE_AMOUNT,
    CropImpossible,
    CropRect,
    RawDevelopOptions,
    RawReport,
    crop_fractions,
)


def run_batch(*args, **kw):
    """Process one folder. Imported lazily -- the runner pulls in Lightroom
    plumbing that a GUI which never opens this tab should not pay for."""
    from .runner import run_batch as _run
    return _run(*args, **kw)
