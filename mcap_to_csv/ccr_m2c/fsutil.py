"""
Publishing finished files into the flight folder.

Outputs land in a Dropbox- or OneDrive-synced folder, so another process may
well hold a handle to the file being replaced: the sync client uploading the
previous version, an antivirus scanner, Explorer building a thumbnail, or --
most likely of all for a transect CSV -- Excel, with the last run's file still
open in front of the user.

On Windows that surfaces as WinError 32. A publish therefore waits for the lock
to clear and, if it truly will not, writes alongside under a numbered name
rather than throwing away the work.
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

#: Windows sharing/lock violations. 32 = "in use by another process",
#: 33 = a byte-range lock is held.
_LOCK_WINERR = {32, 33}


def _is_lock_error(exc: OSError) -> bool:
    if getattr(exc, "winerror", None) in _LOCK_WINERR:
        return True
    # POSIX has no equivalent sharing violation, and there PermissionError means
    # read-only or an ACL -- neither of which retrying will fix.
    return os.name == "nt" and isinstance(exc, PermissionError)


def _is_cross_volume(exc: OSError) -> bool:
    return (getattr(exc, "winerror", None) == 17
            or getattr(exc, "errno", None) == errno.EXDEV)


def _numbered(dst: Path) -> Path:
    """``T4.csv`` -> ``T4 (1).csv``, skipping names already taken."""
    for i in range(1, 1000):
        alt = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
        if not alt.exists():
            return alt
    raise OSError(f"could not find a free name beside {dst}")


def _move_onto(src: Path, dst: Path) -> None:
    """Replace dst with src, overwriting, across volumes if need be."""
    try:
        os.replace(src, dst)
    except OSError as exc:
        if not _is_cross_volume(exc):
            raise
        shutil.copy2(src, dst)
        src.unlink(missing_ok=True)


def publish(
    src: Path,
    dst: Path,
    *,
    timeout: float = 10.0,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Move ``src`` onto ``dst``, returning where it actually landed."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + timeout
    delay = 0.2
    while True:
        try:
            _move_onto(src, dst)
            return dst
        except OSError as exc:
            if not _is_lock_error(exc) or time.monotonic() >= deadline:
                break
            if log:
                log(f"{dst.name} is open in another program; waiting...")
            time.sleep(delay)
            delay = min(delay * 1.6, 1.5)

    alt = _numbered(dst)
    _move_onto(src, alt)
    if log:
        log(f"{dst.name} was locked (is it open in Excel?) -- wrote {alt.name} instead")
    return alt


def write_text(dst: Path, text: str, *, log: Callable[[str], None] | None = None) -> Path:
    """Write text through a temporary file, then publish it into place."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    return publish(tmp, dst, log=log)
