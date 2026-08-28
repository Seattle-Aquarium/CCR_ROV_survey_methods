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

from pathlib import Path

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
