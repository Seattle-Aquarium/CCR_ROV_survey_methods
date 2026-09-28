"""Tests for the tab strip, and for the start-up warm-up not stealing a click.

The bug these exist for looked like tabs that sometimes did not change. The
click was landing; the warm-up walk was then finishing and returning to the tab
it had started on, undoing the choice a fraction of a second later. Clicking
every tab first "fixed" it only because that outlasted the walk.

Needs a display. Skipped rather than failed where there is none, so a headless
CI run stays green.

Runnable directly (``python tests/test_tabs.py``) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ctk = pytest.importorskip("customtkinter")

from rov_imagery_processing.gui.shell import TabStrip  # noqa: E402

NAMES = ["Flight", "Import", "Process", "Video"]


@pytest.fixture
def strip():
    """A four-tab strip on a hidden window, or a skip if Tk cannot start."""
    try:
        root = ctk.CTk()
        root.withdraw()
    except Exception as ex:                       # no display
        pytest.skip(f"no display: {ex}")
    holder = ctk.CTkFrame(root)
    holder.grid(row=0, column=0, sticky="nsew")
    bar = TabStrip(root, holder, on_select=lambda _n: None)
    pages = {n: bar.add(n) for n in NAMES}
    root.update_idletasks()
    try:
        yield root, bar, pages
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _pump(root, ms: int) -> None:
    """Run the event loop for `ms`, so `after` callbacks actually fire."""
    done = {"v": False}
    root.after(ms, lambda: done.__setitem__("v", True))
    while not done["v"]:
        root.update()


def _mapped(pages) -> list[str]:
    return [n for n, p in pages.items() if p.winfo_manager() == "grid"]


def test_selecting_a_tab_shows_exactly_that_page(strip):
    root, bar, pages = strip
    for name in ["Video", "Flight", "Process", "Import"]:
        bar.select(name)
        root.update_idletasks()
        assert bar.current == name
        assert _mapped(pages) == [name], "one page in the cell at a time"


def test_a_click_during_the_warm_up_is_not_undone(strip):
    """The regression: the walk used to return home over the top of it."""
    root, bar, _pages = strip
    bar.select("Flight", notify=False, by_user=False)
    bar.warm()
    root.update_idletasks()
    root.after(10, lambda: bar.select("Video"))       # the user clicks
    _pump(root, 1200)
    assert bar.current == "Video", "the warm-up walked the user off their tab"


def test_a_click_before_the_warm_up_starts_is_not_undone(strip):
    """The app schedules the walk 400 ms out; a click can beat it."""
    root, bar, _pages = strip
    bar.select("Flight", notify=False, by_user=False)
    bar.select("Process")                             # the user clicks first
    root.after(50, bar.warm)                          # the scheduled walk fires
    _pump(root, 1000)
    assert bar.current == "Process"


def test_the_warm_up_still_opens_every_tab_when_left_alone(strip):
    """It is a latency fix and has to keep working when nobody interferes."""
    root, bar, _pages = strip
    bar.select("Flight", notify=False, by_user=False)
    seen: list[str] = []
    original = bar.select

    def spy(name, notify=True, *, by_user=True):
        seen.append(name)
        return original(name, notify, by_user=by_user)

    bar.select = spy
    bar.warm()
    _pump(root, 1200)
    assert set(seen) == set(NAMES), f"only warmed {sorted(set(seen))}"
    assert bar.current == "Flight", "it must come back to where it began"


def test_theme_refresh_does_not_count_as_choosing_a_tab(strip):
    """Redrawing for a theme change must not abandon the warm-up."""
    root, bar, _pages = strip
    bar.select("Flight", notify=False, by_user=False)
    bar.refresh_theme()
    assert bar._chosen is False
    bar.warm()
    _pump(root, 1200)
    assert bar.current == "Flight"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
