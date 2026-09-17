"""
The properties that keep the window responsive while it is being resized.

None of these measure a time -- a test that fails when a laptop is busy is a
test people learn to ignore. What they pin down is the *structure* the
measurements led to, so that a later change that quietly undoes one of them
fails here instead of on a boat:

* only the open tab is in the grid, so a resize lays out one tab and not five;
* repaints driven by ``<Configure>`` are coalesced rather than run per event;
* the banner moves what a width change moves instead of redrawing itself;
* a report box works its scrollbars out from its text, not from what is on
  screen, so the answer cannot oscillate.

Measured on the station this was written on: 113-187 ms to service one resize
step before these, 33-55 ms after.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops.gui import ctk_tuning  # noqa: E402
from rov_flight_ops.gui.widgets import OutputBox, Repaint  # noqa: E402

# --------------------------------------------------------------------------
#  one tab at a time
# --------------------------------------------------------------------------


def test_only_the_open_tab_is_in_the_grid(app):
    """Five tabs share one cell. Laying out all five to show one was where
    most of the resize cost went."""
    app.update_idletasks()
    gridded = [name for name, page in app.nav._page_frames.items()
               if page.grid_info()]
    assert gridded == [app.nav.current]


def test_changing_tabs_swaps_which_one_is_laid_out(app):
    was = app.nav.current
    others = [n for n in app.nav.sections if n != was]
    try:
        app.nav.select(others[0])
        app.update_idletasks()
        gridded = [name for name, page in app.nav._page_frames.items()
                   if page.grid_info()]
        assert gridded == [others[0]]
    finally:
        app.nav.select(was)


def test_a_tab_keeps_its_place_when_it_comes_back(app):
    """grid_remove, not grid_forget: the options have to survive."""
    was = app.nav.current
    page = app.nav._page_frames[was]
    before = dict(page.grid_info())
    others = [n for n in app.nav.sections if n != was]
    try:
        app.nav.select(others[0])
        app.nav.select(was)
        app.update_idletasks()
        after = dict(page.grid_info())
        for key in ("row", "column", "sticky"):
            assert str(after[key]) == str(before[key])
    finally:
        app.nav.select(was)


def _run_warm(app, seconds: float = 4.0) -> list[str]:
    """Run a warm-up to completion, returning the tabs it opened, in order."""
    opened: list[str] = []
    real = app.nav.select

    def watched(name, notify=True):
        opened.append(name)
        return real(name, notify)

    app.nav.select = watched                      # type: ignore[method-assign]
    try:
        # Let any warm-up already under way -- the one start-up begins --
        # finish, so two are never going round at once.
        deadline = time.monotonic() + seconds
        while app.nav._warm_home is not None and time.monotonic() < deadline:
            app.update()
            time.sleep(0.005)
        opened.clear()
        app.nav.warm()
        want = set(app.nav.sections)
        deadline = time.monotonic() + seconds
        while not want <= set(opened) and time.monotonic() < deadline:
            app.update()
            time.sleep(0.005)
        # ... and the pass back to where it started.
        deadline = time.monotonic() + seconds
        while app.nav._warm_home is not None and time.monotonic() < deadline:
            app.update()
            time.sleep(0.005)
        app.update()
    finally:
        app.nav.select = real                     # type: ignore[method-assign]
    return opened


def test_warming_opens_every_tab_and_comes_back(app):
    """Otherwise the first visit to each tab pays for its whole layout at the
    moment somebody clicks it -- 242 ms against 43 ms, measured."""
    was = app.nav.current
    try:
        app.nav.select(app.nav.sections[0])
        home = app.nav.current
        opened = _run_warm(app)
        assert set(opened) >= set(app.nav.sections), "every tab has to be warmed"
        assert app.nav.current == home, "and it has to put the tab back"
        gridded = [name for name, page in app.nav._page_frames.items()
                   if page.grid_info()]
        assert gridded == [home]
    finally:
        app.nav.select(was)


def test_warming_goes_through_select_rather_than_gridding_by_hand(app):
    """The lesson from the version of this that shipped blank tabs.

    Gridding a page, laying it out and taking it straight back out never lets
    it finish *mapping*, and every widget inside a canvas-embedded frame --
    which is every card on every tab, because each tab scrolls -- is then
    left believing it was never shown. The tab opened, its geometry was right
    to the pixel, `winfo_ismapped` and `winfo_viewable` both said yes, and it
    drew nothing at all. No property of the widget tree gave it away; only a
    screenshot did.

    So the rule is that warming uses the same path a click uses, and this is
    what holds it there. Every tab it warms has to arrive through `select`.
    """
    was = app.nav.current
    try:
        opened = _run_warm(app)
        assert set(opened) >= set(app.nav.sections)
    finally:
        app.nav.select(was)


# --------------------------------------------------------------------------
#  coalescing
# --------------------------------------------------------------------------


def test_a_burst_of_asks_draws_once_and_then_once_more(app):
    """Once on the frame boundary for the drag, once after it for the size it
    actually ended at."""
    import time

    runs = []
    r = Repaint(app, lambda: runs.append(1), frame_ms=5, settle_ms=25)
    try:
        for _ in range(50):
            r.ask()
        assert runs == []                 # fifty events, nothing drawn yet
        deadline = time.monotonic() + 3.0
        while len(runs) < 2 and time.monotonic() < deadline:
            app.update()
            time.sleep(0.002)
        assert runs == [1, 1], (
            f"expected a frame pass and a settle pass, got {runs}")
    finally:
        r.cancel()


def test_repaint_says_when_it_is_still_moving(app):
    """What lets a chart draw coarsely now and accurately at the end."""
    r = Repaint(app, lambda: None, frame_ms=5, settle_ms=25)
    assert not r.busy
    r.ask()
    assert r.busy
    r.now()
    assert not r.busy
    r.cancel()


def test_the_banner_only_repaints_when_something_other_than_width_changed(app):
    """A width change moves the ground, the rule and the controls. Nothing
    else in the banner depends on it."""
    app.update_idletasks()
    app._paint_header()
    shape = app._painted
    assert shape is not None

    items_before = len(app.header.find_all())
    app._paint_header()                   # nothing changed at all
    assert app._painted == shape
    assert len(app.header.find_all()) == items_before

    # Folding it is a different shape, and does rebuild.
    was_open = app.banner_open
    try:
        app.toggle_banner()
        app.update_idletasks()
        assert app._painted != shape
    finally:
        if app.banner_open != was_open:
            app.toggle_banner()


# --------------------------------------------------------------------------
#  the report boxes
# --------------------------------------------------------------------------


def test_a_report_box_decides_its_scrollbars_from_its_text(app):
    """Not from the lines on screen: showing the horizontal bar costs a line
    of height, which changes which lines are on screen, which changes the
    answer. That loop is what made the window twitch at 5 Hz."""
    box = OutputBox(app, height=40, wrap="none", font=("Consolas", 11))
    try:
        box.insert("1.0", "short\n" * 40)
        assert box._widest() < 400        # measured, whatever is displayed

        box.delete("1.0", "end")
        box.insert("1.0", "x" * 4000)
        assert box._widest() > 4000
    finally:
        box.destroy()


def test_measuring_the_text_is_not_redone_until_the_text_changes(app):
    box = OutputBox(app, height=40, wrap="none", font=("Consolas", 11))
    try:
        box.insert("1.0", "hello")
        first = box._widest()
        assert box._widest_px is not None          # remembered
        box.insert("end", " and more")
        assert box._widest_px is None              # and dropped when it changed
        assert box._widest() > first
    finally:
        box.destroy()


def test_a_wrapped_box_never_asks_for_a_sideways_scrollbar(app):
    box = OutputBox(app, height=40, wrap="word", font=("Consolas", 11))
    try:
        box.insert("1.0", "x" * 10_000)
        assert box._wants_x() is False
    finally:
        box.destroy()


def test_the_settle_is_coalesced_to_one_per_idle_cycle(app):
    box = OutputBox(app, height=40, wrap="none", font=("Consolas", 11))
    try:
        calls = []
        box._settle_now = lambda: calls.append(1)   # type: ignore[method-assign]
        for _ in range(20):
            box.settle()
        assert calls == []
        app.update()
        assert len(calls) <= 1
    finally:
        box.destroy()


# --------------------------------------------------------------------------
#  the library patch
# --------------------------------------------------------------------------


def test_the_scrollbar_tuning_went_on():
    from customtkinter.windows.widgets.ctk_scrollbar import CTkScrollbar

    assert ctk_tuning.apply() is True
    assert getattr(CTkScrollbar._draw, "_ccr_tuned", False)
    assert getattr(CTkScrollbar.set, "_ccr_tuned", False)


def test_applying_the_tuning_twice_is_harmless():
    from customtkinter.windows.widgets.ctk_scrollbar import CTkScrollbar

    once = CTkScrollbar._draw
    ctk_tuning.apply()
    ctk_tuning._applied = False            # force it to try again
    ctk_tuning.apply()
    assert CTkScrollbar._draw is once, "the patch must not wrap itself"


def test_a_scrollbar_still_draws_and_still_reports_its_position(app):
    import customtkinter as ctk

    sb = ctk.CTkScrollbar(app, orientation="vertical")
    try:
        sb.set(0.25, 0.75)
        assert sb.get() == (0.25, 0.75)
        sb.set(0.0, 1.0)
        assert sb.get() == (0.0, 1.0)
    finally:
        sb.destroy()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
