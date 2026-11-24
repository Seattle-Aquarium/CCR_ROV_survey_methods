"""
The properties that keep the window responsive while it is being resized.

None of these measure a time -- a test that fails when a laptop is busy is a
test people learn to ignore. What they pin down is the *structure* the
measurements led to, so that a later change that quietly undoes one of them
fails here instead of on a boat:

* only the open tab is in view and follows the window's size; the others are
  parked off-screen at the size they had, so a resize lays out one tab and a
  tab change maps nothing (November 2026, measured on the field laptop);
* a parked tab does not count as shown, so its refresh timers stand down;
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


def _x_of(page) -> int:
    return int(float(page.place_info().get("x", 0)))


def _in_view(app) -> list[str]:
    """The tabs placed where they can be seen."""
    return [name for name, page in app.nav._page_frames.items()
            if page.place_info() and _x_of(page) >= 0]


def test_only_the_open_tab_is_in_view(app):
    """Every page shares one holder; all but the open one are parked off its
    left edge, mapped but out of sight."""
    app.update_idletasks()
    assert _in_view(app) == [app.nav.current]
    for name, page in app.nav._page_frames.items():
        if name != app.nav.current:
            info = page.place_info()
            assert info, f"{name} is not placed at all"
            assert _x_of(page) + int(info["width"]) < 0, f"{name} is not parked"


def test_changing_tabs_swaps_which_one_is_in_view(app):
    was = app.nav.current
    others = [n for n in app.nav.sections if n != was]
    try:
        app.nav.select(others[0])
        app.update_idletasks()
        assert _in_view(app) == [others[0]]
    finally:
        app.nav.select(was)


def test_the_open_tab_fills_the_holder(app):
    was = app.nav.current
    others = [n for n in app.nav.sections if n != was]
    try:
        app.nav.select(others[0])
        app.nav.select(was)
        app.update_idletasks()
        page = app.nav._page_frames[was]
        info = page.place_info()
        w, h = app.nav._holder_px
        assert (int(info["width"]), int(info["height"])) == (w, h)
        assert (_x_of(page), int(float(info["y"]))) == (0, 0)
    finally:
        app.nav.select(was)


def test_a_resize_lays_out_only_the_open_tab(app):
    """What parking is for: a parked page keeps the size it had, so dragging
    a window edge re-lays-out one page, not eight."""
    nav = app.nav
    was = nav.current
    parked = next(n for n in nav.sections if n != was)
    before = dict(nav._sized)
    old = nav._holder_px

    class _Event:
        # Absolute: the test window is withdrawn, so its holder may be 1 px.
        width, height = 903, 611

    try:
        nav._configured_at = 0.0               # a quiet spell: applied at once
        nav._holder_configured(_Event)
        assert nav._sized[was] == (903, 611)
        assert nav._sized[parked] == before[parked]
        info = nav._page_frames[parked].place_info()
        assert (int(info["width"]), int(info["height"])) == before[parked]
    finally:
        class _Back:
            width, height = old
        nav._configured_at = 0.0
        nav._holder_configured(_Back)
        app.update_idletasks()


def test_a_drag_lays_the_page_out_a_few_times_a_second_not_at_every_step(app):
    """A window manager sends a size for every few pixels the pointer moves,
    and each re-layout of a busy page is a few hundred milliseconds on the
    field laptop. A burst is folded into one pass, at the newest size."""
    nav = app.nav
    was = nav.current
    old = nav._holder_px
    shown_at = []
    real_show = nav._show
    nav._show = lambda name: (shown_at.append(nav._holder_px), real_show(name))

    def step(w, h):
        class _E:
            width, height = w, h
        nav._holder_configured(_E)

    try:
        nav._configured_at = 0.0
        step(900, 600)                      # after a quiet spell: at once
        for w in range(901, 921):           # then a drag
            step(w, 600)
        assert shown_at == [(900, 600)], "every step of the drag was laid out"
        assert nav._resize_after is not None, "nothing is waiting for the drag"
        deadline = time.monotonic() + 3.0
        while nav._resize_after is not None and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        assert shown_at[-1] == (920, 600), "the last pass is at the size it ended"
        assert nav._sized[was] == (920, 600)
        assert len(shown_at) == 2
    finally:
        nav._show = real_show
        class _Back:
            width, height = old
        nav._configured_at = 0.0
        nav._holder_configured(_Back)
        app.update_idletasks()


def test_a_parked_tab_is_not_shown_though_tk_says_it_is_mapped(app):
    """Tk calls everything on a parked page mapped and viewable, so the pages'
    refresh timers ask the window instead -- or every tab would be redrawn
    whichever one was open."""
    from rov_flight_ops.gui.widgets import shown

    was = app.nav.current
    try:
        app.nav.select("Monitoring")
        app.update_idletasks()
        nav_page = app.pages["navigation"]
        assert not app.is_shown(nav_page.numbers)
        assert not shown(nav_page.map.canvas)
        assert not nav_page.on_screen()
        app.nav.select("Navigation")
        app.update_idletasks()
        assert nav_page.on_screen()
        assert not shown(app.pages["monitor"].canvas)
    finally:
        app.nav.select(was)


def test_warming_lays_out_every_parked_tab_without_opening_any(app):
    """Each page's first layout is its most expensive. Parked pages are laid
    out where nobody can see them, one per turn of the event loop -- without
    going through `select`, so the open tab never changes and nothing on
    screen flickers past (as the version before parking did)."""
    nav = app.nav
    was = nav.current
    opened: list[str] = []
    real = nav.select

    def watched(name, notify=True, *, by_user=True):
        opened.append(name)
        return real(name, notify, by_user=by_user)

    nav.select = watched                          # type: ignore[method-assign]
    try:
        for name in nav.sections:
            if name != was:
                nav._sized[name] = (1, 1)         # as if never laid out
        nav.warm()
        deadline = time.monotonic() + 10.0
        while nav.warming and time.monotonic() < deadline:
            app.update()
            time.sleep(0.005)
        assert not nav.warming
        assert opened == [], "warming must not open a tab"
        assert nav.current == was
        for name in nav.sections:
            assert nav._sized[name] == nav._holder_px, f"{name} was not laid out"
        assert _in_view(app) == [was]
    finally:
        nav.select = real                         # type: ignore[method-assign]


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


def test_the_scrollable_frame_width_correction_went_on():
    from customtkinter.windows.widgets.ctk_scrollable_frame import (
        CTkScrollableFrame,
    )

    assert ctk_tuning.apply() is True
    assert getattr(CTkScrollableFrame.__init__, "_ccr_tuned", False)
    once = CTkScrollableFrame.__init__
    ctk_tuning._applied = False            # force it to try again
    ctk_tuning.apply()
    assert CTkScrollableFrame.__init__ is once, "the patch must not wrap itself"


def test_a_scrolling_page_wider_than_its_canvas_is_pulled_back(app, monkeypatch):
    """Tk once left the Lights and BlueOS logs pages at their requested width
    inside a narrower canvas, cutting the right-hand cards off. The content
    must be put back to the canvas's width whenever the two disagree."""
    import customtkinter as ctk

    body = ctk.CTkScrollableFrame(app)
    try:
        canvas = body._parent_canvas
        monkeypatch.setattr(canvas, "winfo_width", lambda: 300)
        monkeypatch.setattr(body, "winfo_width", lambda: 500)
        canvas.itemconfigure(body._create_window_id, width=0)
        body._ccr_hold_width()
        assert int(float(canvas.itemcget(body._create_window_id, "width"))) == 300

        # Not before the canvas has a size of its own.
        monkeypatch.setattr(canvas, "winfo_width", lambda: 1)
        canvas.itemconfigure(body._create_window_id, width=0)
        body._ccr_hold_width()
        assert int(float(canvas.itemcget(body._create_window_id, "width"))) == 0
    finally:
        body.destroy()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
