"""
Card subtitles have to fit the card.

A CTkLabel does not wrap unless it is given a width, so a subtitle longer than
the window used to run off the right edge and lose the end of its own sentence.
Nothing about that is visible in a screenshot of a wide window, which is why it
survived several passes: it only shows up on a narrow one, or on a subtitle
longer than any that existed at the time.

Geometry answers it without reading the screen, and covers every card in the app
rather than the one that happened to be looked at.
"""

from __future__ import annotations

import time

import pytest

ctk = pytest.importorskip("customtkinter")

from utc.gui.app import App  # noqa: E402
from utc.gui.widgets import Card  # noqa: E402

#: CustomTkinter scales wraplength on the way to the underlying label, and the
#: round trip through int() leaves a few pixels of slack either way.
TOLERANCE_PX = 16


@pytest.fixture(scope="module")
def app():
    try:
        a = App()
    except Exception as ex:
        pytest.skip(f"no display: {ex}")
    a.withdraw()
    yield a
    try:
        a.destroy()
    except Exception:
        pass


def _cards(widget, found=None):
    found = [] if found is None else found
    if isinstance(widget, Card):
        found.append(widget)
    for child in widget.winfo_children():
        _cards(child, found)
    return found


def _settle(app, ticks=25):
    for _ in range(ticks):
        app.update()
        time.sleep(0.02)


@pytest.mark.parametrize("size", ["1180x900", "980x700"])
def test_no_subtitle_is_wider_than_its_card(app, size):
    """Checked at the default size and at the window's own minimum."""
    app.geometry(size)
    _settle(app)

    too_wide = []
    for page in app.nav.sections:
        app.nav.select(page)
        _settle(app, 12)
        for card in _cards(app):
            subtitle = getattr(card, "_subtitle", None)
            if subtitle is None or not card.winfo_ismapped():
                continue
            available = subtitle.master.winfo_width()
            needed = subtitle.winfo_reqwidth()
            if needed > available + TOLERANCE_PX:
                too_wide.append(
                    f"{page}: needs {needed}px in {available}px — "
                    f"{subtitle.cget('text')[:50]}..."
                )

    assert not too_wide, "subtitle(s) overflow their card:\n  " + "\n  ".join(too_wide)


def test_a_long_subtitle_actually_wraps(app):
    """The regression itself: a subtitle far longer than the card must end up
    on more than one line, not one clipped line."""
    app.geometry("1180x900")
    _settle(app)

    # Its own window, so the card gets a width of its own rather than whatever
    # is left over in a cell of the real layout.
    top = ctk.CTkToplevel(app)
    top.geometry("600x300")
    top.grid_columnconfigure(0, weight=1)

    long_text = ("A subtitle long enough that it cannot possibly fit on one "
                 "line of this card, which is exactly the case that used to "
                 "run off the right edge of the window and lose its own ending.")
    card = Card(top, "Test", long_text)
    card.grid(row=0, column=0, sticky="ew")
    short = Card(top, "Test", "Short.")
    short.grid(row=1, column=0, sticky="ew")
    _settle(app, 25)

    assert card.winfo_width() > 200, "the test card never got laid out"
    assert card._subtitle.winfo_reqheight() > short._subtitle.winfo_reqheight(), \
        "the long subtitle did not wrap onto extra lines"
    assert card._subtitle.winfo_reqwidth() <= card.winfo_width() + TOLERANCE_PX

    top.destroy()
