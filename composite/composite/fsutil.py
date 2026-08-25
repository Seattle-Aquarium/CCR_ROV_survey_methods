"""
Publishing finished files into the flight folder.

Outputs land in a Dropbox-synced folder, so another process may well hold a
handle to the file we are about to replace: Dropbox itself uploading the
previous version, an antivirus scanner, Explorer building a thumbnail, or --
most likely of all for the telemetry CSV -- Excel, with the last run's file
still open.

On Windows that surfaces as WinError 32 and, until this module existed, killed
the run at the final step with every minute of encoding already spent. So a
publish waits for the lock to clear, and if it truly will not, writes alongside
under a numbered name rather than throwing the work away.
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path
from typing import Callable

#: Windows sharing/lock violations. 32 = "in use by another process",
#: 33 = a byte-range lock is held.
_LOCK_WINERR = {32, 33}


def _is_lock_error(exc: OSError) -> bool:
    we = getattr(exc, "winerror", None)
    if we in _LOCK_WINERR:
        return True
    # POSIX has no equivalent sharing violation; PermissionError still covers
    # the read-only and ACL cases, which retrying will not fix, so only treat
    # it as transient on Windows.
    return os.name == "nt" and isinstance(exc, PermissionError)


def _is_cross_volume(exc: OSError) -> bool:
    return (getattr(exc, "winerror", None) == 17
            or getattr(exc, "errno", None) == errno.EXDEV)


def _numbered(dst: Path) -> Path:
    """'clip.mp4' -> 'clip (1).mp4', skipping names already taken."""
    for i in range(1, 1000):
        alt = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
        if not alt.exists():
            return alt
    raise OSError(f"could not find a free name beside {dst}")


def _move_onto(src: Path, dst: Path) -> None:
    """Replace dst with src, overwriting, across volumes if need be."""
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if not _is_cross_volume(exc):
            raise
    # The cache and the flight folder are on different drives. Copy in beside
    # the target first so the swap itself stays atomic and no reader ever sees
    # a half-written file.
    tmp = dst.with_name(dst.name + ".part")
    shutil.copy2(src, tmp)
    try:
        os.replace(tmp, dst)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    Path(src).unlink(missing_ok=True)


def publish(
    src: str | Path,
    dst: str | Path,
    *,
    timeout_s: float = 90.0,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Move a finished file into place, tolerating a transient lock.

    Returns where it actually landed -- normally `dst`, but a numbered sibling
    if the lock never cleared. Callers must use the returned path when they
    report the result, or they will name a file that was not written.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    delay, waited = 0.5, 0.0
    warned = False
    while True:
        try:
            _move_onto(src, dst)
            if warned and log:
                log(f"{dst.name}: lock cleared after {waited:.0f}s")
            return dst
        except OSError as exc:
            if not _is_lock_error(exc) or waited >= timeout_s:
                if not _is_lock_error(exc):
                    raise
                break
            if not warned:
                warned = True
                if log:
                    log(f"{dst.name} is open in another program "
                        f"(Dropbox, Excel or antivirus?); waiting up to "
                        f"{timeout_s:.0f}s for it to be released")
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 5.0)

    alt = _numbered(dst)
    _move_onto(src, alt)
    if log:
        log(f"could not replace {dst.name} -- it is still open in another "
            f"program. Wrote {alt.name} instead; close the other program and "
            f"delete the stale copy.")
    return alt
