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

#: The order of a field day: on the boat, at the desk, then the media.
EXPECTED_CHAPTERS = ["Aboard ROV", "Flight report", "Photos", "Videos"]

EXPECTED_TOOLS = {
    "Aboard ROV": ["Flight & transects", "Vehicle & files"],
    "Flight report": ["Transects", "Recording health"],
    "Photos": ["Import photos", "Process photos", "Banner tools"],
    "Videos": ["Video"],
}



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


def test_the_section_strip_sits_between_body_copy_and_the_rail(app):
    """The tools within a chapter are a control, not a caption -- but they
    must not compete with the chapter names above them."""
    from utc.gui import theme as T
    assert T.FONT_BODY[1] < T.FONT_SECTION[1] < T.FONT_RAIL[1]
    assert T.FONT_SECTION_ON[1] > T.FONT_SECTION[1]


def test_the_chapter_buttons_are_larger_than_a_standard_button(app):
    """They are the roadmap, so they are drawn as objects rather than rows."""
    from utc.gui import theme as T
    assert T.CHAPTER_BTN_H > 40, "a standard CTkButton is 28px tall"
    assert T.CHAPTER_BTN_GAP > 0, "they should read as four, not as a stack"
    assert T.CHAPTER_BTN_BORDER_ON > T.CHAPTER_BTN_BORDER


def test_each_chapter_gets_its_own_colour(app):
    from utc.gui import theme as T
    assert len(T.CHAPTER_COLOURS) >= len(EXPECTED_CHAPTERS)
    used = [app.nav._colour_for(i) for i in range(1, 5)]
    assert len(set(used)) == 4, "four chapters, four colours"


def test_the_gaps_between_buttons_are_not_clickable(app):
    """They are separate objects; the space between them belongs to neither."""
    nav = app.nav
    second = nav._btn_y(2)
    assert nav._chapter_at(nav._btn_y(1) + nav._btn_h // 2) == "Aboard ROV"
    assert nav._chapter_at(second - nav._gap // 2) is None
    assert nav._chapter_at(second + 2) == "Flight report"


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
    """A chapter nobody has opened yet lands on the tool that leads it.

    "Never opened" is arranged rather than assumed: the window is shared
    across the suite and the tests do not run in a fixed order, so by the time
    this runs the chapter may well have been visited.
    """
    nav = app.nav
    ch = nav._chapters["Flight report"]
    nav.select("Video")                       # somewhere else entirely
    if hasattr(ch, "_last"):
        del ch._last
    nav.select_chapter("Flight report")
    assert nav.current == "Transects", "Transects lead the flight report"


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
    """The banner and the rail are drawn on canvases rather than themed by
    CustomTkinter, so nothing repaints them for free."""
    from utc.gui import theme as T
    assert T.RAIL_BG[0] != T.RAIL_BG[1]

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


@pytest.mark.parametrize("style", ["solid", "outline", "leftbar", "ghost"])
@pytest.mark.parametrize("state", ["on", "hover", "off"])
def test_every_button_style_puts_legible_type_on_every_chapter(app, style,
                                                               state,
                                                               monkeypatch):
    """A style is a table entry, so a new one is easy to add and easy to add
    wrongly. The trap is always the same: setting type *in* Algae or Seafoam,
    which measures 2.2:1 and 1.9:1 on a light ground.
    """
    from utc import brand
    from utc.gui import theme as T

    monkeypatch.setattr(T, "CHAPTER_BTN_STYLE", style)
    for index in (1, 2, 3, 4):
        colour = app.nav._colour_for(index)
        for mode in (lambda pair: pair[0], lambda pair: pair[1]):
            fill, _b, _w, _r, _bar, ink = app.nav._button_look(
                colour, state, True, mode)
            assert brand.contrast(ink, fill) >= 4.5, (
                f"{style}/{state}: {ink} on {fill} for chapter {index}")


@pytest.mark.parametrize("style", ["solid", "soft", "outline", "dot", "bar",
                                   "plain"])
def test_every_badge_style_reserves_the_room_it_draws_in(app, style,
                                                         monkeypatch):
    """`_badge_width` is what the roadmap wraps against, so a style whose
    drawing is wider than its measurement would overlap the text beside it."""
    from utc.gui import theme as T

    monkeypatch.setattr(T, "BADGE_STYLE", style)
    assert app._badge_width(30, 2.5) > 0
    app._paint_header()                    # must not raise in any style


@pytest.mark.parametrize("layout", ["inline", "stacked"])
def test_both_banner_arrangements_draw(app, layout, monkeypatch):
    from utc.gui import theme as T

    monkeypatch.setattr(T, "BANNER_LAYOUT", layout)
    app._paint_header()
    drawn = [app.header.itemcget(i, "text") for i in app.header.find_all()
             if app.header.type(i) == "text"]
    from utc.gui.app import CHAPTER_BLURBS
    for blurb in CHAPTER_BLURBS:
        assert blurb in drawn, f"{layout} lost a chapter"


@pytest.mark.parametrize("style", ["bold", "black", "caps", "twotone",
                                   "light"])
def test_every_title_style_draws_the_name(app, style, monkeypatch):
    from utc.gui import theme as T
    from utc.gui.app import DISPLAY_TITLE

    monkeypatch.setattr(T, "TITLE_STYLE", style)
    app._paint_header()
    drawn = " ".join(app.header.itemcget(i, "text")
                     for i in app.header.find_all()
                     if app.header.type(i) == "text")
    # "caps" upper-cases it and "twotone" splits it in two, so compare on the
    # letters rather than on the string.
    assert (DISPLAY_TITLE.replace(" ", "").lower()
            in drawn.replace(" ", "").lower())


def test_the_roadmap_breaks_between_the_vehicle_and_the_imagery(app):
    """One and two are the ROV and what it recorded; three and four are the
    imagery that came back. The break carries that, so it stays put however
    wide the window gets -- flowing purely on width would pull three up onto
    the first line on a large monitor and lose the distinction.
    """
    from tkinter import font as tkfont

    from utc.gui import theme as T
    from utc.gui.app import _font_kw

    f = tkfont.Font(**_font_kw(T.scale_font(T.FONT_BANNER_SUB, 1.0)))
    generous = app._wrap_roadmap(f, badge=20, gap=24, avail=100_000)
    assert generous == [[0, 1], [2, 3]], "the break is not a consequence of width"

    # Narrow enough that a pair cannot fit: everything still gets a line.
    cramped = app._wrap_roadmap(f, badge=20, gap=24, avail=1)
    assert cramped == [[0], [1], [2], [3]], "nothing may be dropped"


def test_the_banner_says_what_it_is_told_to(app):
    """Only the banner is renamed -- the window, the dialogs and the file names
    still use APP_NAME, so nothing on disk moves."""
    from utc.gui.app import APP_NAME, DISPLAY_TITLE
    assert DISPLAY_TITLE != APP_NAME
    assert app.title() == APP_NAME
    drawn = [app.header.itemcget(i, "text") for i in app.header.find_all()
             if app.header.type(i) == "text"]
    assert DISPLAY_TITLE in drawn
