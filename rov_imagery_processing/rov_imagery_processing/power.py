"""
Keeping the machine awake for the length of a run.

A production run is tens of minutes to hours of encoding with no keyboard or
mouse activity, and Windows does not count a working process as user activity.
On this laptop that meant Modern Standby suspended a run mid-encode for 55
minutes; from the outside it looked exactly like the tool had hung, and it cost
more wall-clock time than the encode itself.

``SetThreadExecutionState`` asks Windows not to idle-sleep while we work. It is
per-thread and only holds while the thread lives, so it must be entered on the
same thread that does the work -- which is why this wraps the pipeline rather
than being set once at startup. The display is deliberately left alone: there is
no reason to burn the screen for a batch job, and blocking display sleep annoys
people more than it helps.

The lid is a different matter. Closing the lid is an explicit instruction to
sleep and no process can veto it, so that caveat belongs in the user-facing
documentation, not in code.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake(log: Callable[[str], None] | None = None) -> Iterator[bool]:
    """Block idle sleep for the duration of the block.

    Yields whether the request was actually granted. Never raises: failing to
    hold off sleep is a degraded run, not a failed one, so a machine or platform
    that refuses simply proceeds without it.
    """
    if os.name != "nt":
        yield False
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Returns the previous state, or 0 on failure.
        ok = kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ) != 0
    except Exception:
        ok = False

    if not ok and log:
        log("could not ask Windows to stay awake; if the machine sleeps "
            "mid-run the encode pauses until it wakes")

    try:
        yield ok
    finally:
        if ok:
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
