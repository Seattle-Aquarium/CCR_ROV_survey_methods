"""
Opening a file or folder the way the desktop would.

Explorer on Windows, which is where the program lives. Elsewhere whatever
``xdg-open`` (or macOS's ``open``) hands it to -- and in the web demo's
container, which has no file manager at all, nothing: the caller is told so
by a False rather than an AttributeError it never expected from
``os.startfile``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

#: What a page says when `open_path` had nothing to open it with.
NO_OPENER = "This computer has no program to open it with."


def open_path(path: Path | str) -> bool:
    """Open `path` in its usual program.

    False when this computer has nothing to open it with. Raises OSError, as
    ``os.startfile`` does, when opening it failed.
    """
    if sys.platform == "win32":
        os.startfile(str(path))                           # noqa: S606
        return True
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if opener is None:
        return False
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True
