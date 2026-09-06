"""
Pytest configuration for the UTC test suite.

Two kinds of file live in this folder:

* **Automated tests** -- hermetic, fast, no flight data and no display. These
  are what CI runs and what a pull request has to keep green.
* **Live scripts** -- ``*_live.py``, ``debug_*.py``, the visual renderers and
  the GUI smoke test. They need a real flight folder on disk, or a screen to
  open a window on, and they do their work at *import* rather than inside test
  functions. Collecting them on a machine without the data costs about ninety
  seconds and then fails for reasons that have nothing to do with the change
  being tested.

So the live scripts are skipped by default and opted into::

    pytest                 # the automated suite, seconds
    pytest --runlive       # everything, needs real data on this machine

Run one directly at any time -- they are still ordinary scripts::

    python tests/test_pipeline_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Files that need real data or a display. Matched against the file name.
LIVE_PATTERNS = ("_live.py", "debug_", "test_render_visual.py",
                 "test_gui_smoke.py", "test_extract_small.py")


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--runlive", action="store_true", default=False,
        help="also run the scripts that need a real flight folder or a display",
    )


def pytest_ignore_collect(collection_path: Path, config) -> bool:
    if config.getoption("--runlive"):
        return False
    name = Path(collection_path).name
    return any(pat in name for pat in LIVE_PATTERNS)


# --------------------------------------------------------------------------
#  The GUI
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """One application, shared by every test that needs a window.

    CustomTkinter and Tk both keep process-wide state -- the default root, the
    appearance mode, the scaling tracker -- and building a second App() after
    the first has been destroyed fails often enough to matter. When each GUI
    module had its own fixture, test ordering decided which of them came up
    and which skipped its entire file, so seventeen tests could quietly not
    run. One session-scoped window removes the question.

    Tests may change the mode, the geometry or the open page; each puts back
    what it changed.
    """
    ctk = pytest.importorskip("customtkinter")
    del ctk
    from utc.gui.app import App

    try:
        a = App()
    except Exception as ex:                      # no display, e.g. on CI
        pytest.skip(f"no display: {ex}")
    a.withdraw()
    a.update()
    yield a
    try:
        a.destroy()
    except Exception:
        pass
