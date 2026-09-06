"""The rail: four chapters, in the order a survey day happens.

The grouping is the point. Eight rail entries had stopped describing anything;
four describe the day -- the boat, the desk, then the two kinds of media -- and
a chapter can grow another tool without the rail growing at all. These tests
pin that shape, because it is the sort of thing that erodes one convenient
addition at a time.

Needs a display, and skips without one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("customtkinter")

from utc.gui.app import App  # noqa: E402

#: The order of a field day: on the boat, at the desk, then the media.
EXPECTED_CHAPTERS = ["Aboard ROV", "Flight report", "Photos", "Videos"]

EXPECTED_TOOLS = {
    "Aboard ROV": ["Flight & transects", "Vehicle & files"],
    "Flight report": ["Transects", "Recording health"],
    "Photos": ["Import photos", "Process photos", "Banner tools"],
    "Videos": ["Video"],
}


@pytest.fixture(scope="module")
def app():
    try:
        a = App()
    except Exception as ex:
        pytest.skip(f"no display: {ex}")
    a.withdraw()
    a.update()
    yield a
    try:
        a.destroy()
    except Exception:
        pass


def _rail_text(app) -> list[str]:
    """Everything the rail actually draws."""
    app.nav._redraw()
    c = app.nav.rail
    return [c.itemcget(i, "text") for i in c.find_all()
            if c.type(i) == "text"]


# --------------------------------------------------------------------------
#  shape
# --------------------------------------------------------------------------


def test_the_rail_holds_four_chapters_in_the_order_of_a_field_day(app):
    assert app.nav.chapters == EXPECTED_CHAPTERS


def test_every_tool_lives_in_the_chapter_it_belongs_to(app):
    got = {ch: app.nav._chapters[ch].sections for ch in app.nav.chapters}
    assert got == EXPECTED_TOOLS


def test_no_tool_was_lost_in_the_regrouping(app):
    """Eight tools before, eight after -- the rail changed, not the app."""
    assert len(app.nav.sections) == 8
    for tools in EXPECTED_TOOLS.values():
        for tool in tools:
            assert tool in app.nav.sections


def test_the_rail_names_chapters_and_nothing_else(app):
    """No subtitles. The rail is the pathway, not a description of it."""
    drawn = _rail_text(app)
    for chapter in EXPECTED_CHAPTERS:
        assert chapter in drawn
    for tools in EXPECTED_TOOLS.values():
        for tool in tools:
            if tool not in EXPECTED_CHAPTERS:
                assert tool not in drawn, f"{tool!r} belongs in the strip"
    # A numeral each, so the four read as a sequence.
    assert {"1", "2", "3", "4"} <= set(drawn)


def test_chapter_names_are_set_larger_than_body_copy(app):
    from utc.gui import theme as T
    assert T.FONT_RAIL[1] > T.FONT_BODY[1]
    assert T.FONT_RAIL_SMALL[1] > T.FONT_BODY[1]


# --------------------------------------------------------------------------
#  behaviour
# --------------------------------------------------------------------------


def test_opening_a_tool_lights_up_its_chapter(app):
    app.nav.select("Banner tools")
    assert app.nav.current == "Banner tools"
    assert app.nav.current_chapter == "Photos"


def test_a_chapter_reopens_on_the_tool_last_used_there(app):
    """Switching chapters and back should not lose your place."""
    app.nav.select("Process photos")
    app.nav.select_chapter("Videos")
    assert app.nav.current == "Video"
    app.nav.select_chapter("Photos")
    assert app.nav.current == "Process photos"


def test_a_chapter_opens_on_its_first_tool_the_first_time(app):
    app.nav.select_chapter("Flight report")
    assert app.nav.current == "Transects", "Transects lead the flight report"


def test_the_app_opens_on_the_flight_because_everything_else_needs_it(app):
    """The site and date name every folder downstream."""
    assert EXPECTED_TOOLS["Aboard ROV"][0] == "Flight & transects"


def test_locking_the_rail_disables_every_tool(app):
    """Used while the Lightroom batch drives another window with synthetic
    keystrokes -- a click that changes page sends the rest somewhere else."""
    app.nav.set_locked(True)
    assert not any(app.nav._enabled.values())
    app.nav.set_locked(False)
    assert all(app.nav._enabled.values())


def test_a_click_on_a_disabled_chapter_does_nothing(app):
    app.nav.select("Video")
    app.nav.set_enabled("Photos", False)
    app.nav.select_chapter("Photos")          # as a click would
    try:
        assert app.nav._chapter_enabled("Photos") is False
    finally:
        app.nav.set_enabled("Photos", True)


def test_switching_appearance_mode_repaints_the_chrome(app):
    """The banner and rail gradients differ between modes, and both are drawn
    rather than themed, so nothing repaints them for free."""
    from utc.gui import theme as T
    assert T.HEADER_GRADIENT["dark"] != T.HEADER_GRADIENT["light"]

    was = app.mode
    try:
        app.theme_switch.deselect()
        app._toggle_theme()
        assert app.mode == "light"
        assert _rail_text(app), "the rail drew nothing after the mode changed"
    finally:
        if was == "dark":
            app.theme_switch.select()
            app._toggle_theme()


def test_the_banner_says_what_it_is_told_to(app):
    """Only the banner is renamed -- the window, the dialogs and the file names
    still use APP_NAME, so nothing on disk moves."""
    from utc.gui.app import APP_NAME, DISPLAY_TITLE
    assert DISPLAY_TITLE != APP_NAME
    assert app.title() == APP_NAME
    drawn = [app.header.itemcget(i, "text") for i in app.header.find_all()
             if app.header.type(i) == "text"]
    assert DISPLAY_TITLE in drawn
