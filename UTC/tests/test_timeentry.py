"""Tests for the six-keystroke time field.

This needs a real Tk widget: the bug it guards against lived in the interaction
between a variable's write trace and when Tk applies the new text, which no
amount of testing the formatting function in isolation would have caught. The
whole suite skips if Tk cannot open a display.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="module")
def root():
    """One Tk root for the whole module.

    Creating and destroying a root per test was flaky -- Tcl intermittently
    failed to re-initialise ("couldn't read file auto.tcl"), the skip guard
    caught it, and the tests guarding a real reported bug quietly did not run.
    A skipped test that looks like a passing one is worse than no test.
    """
    import customtkinter as ctk
    try:
        r = ctk.CTk()
        r.withdraw()
        r.update()
    except Exception as ex:                       # genuinely no display
        pytest.skip(f"Tk unavailable: {ex}")
    yield r
    try:
        r.destroy()
    except Exception:
        pass


def _type(root, seq: str, start: str = "") -> str:
    """Type one character at a time at the caret, like a person does."""
    from utc.gui.widgets import TimeEntry
    e = TimeEntry(root, width=110)
    e.pack()
    try:
        if start:
            e.set(start)
        root.update()
        for ch in seq:
            e.insert(e.index("insert"), ch)
            root.update()
            root.update_idletasks()           # let the caret fix-up run
        return e.get()
    finally:
        e.destroy()


@pytest.mark.parametrize("typed,expected", [
    ("123456", "12:34:56"),
    ("120000", "12:00:00"),
    ("235959", "23:59:59"),
    ("091503", "09:15:03"),
    ("1234", "12:34"),
    ("12", "12"),
    ("1", "1"),
])
def test_typing_digits_left_to_right(root, typed, expected):
    """The regression: this produced '12:45:63' from '123456'.

    Inserting the colon left the caret to its left, so every later digit landed
    *before* the digit already there and the value came out scrambled. Setting
    the caret inside the write trace does nothing, because Tk has not applied
    the new text to the widget yet.
    """
    assert _type(root, typed) == expected


def test_pasting_a_whole_time_works(root):
    assert _type(root, "", "12:25:45") == "12:25:45"
    assert _type(root, "", "122545") == "12:25:45"
    assert _type(root, "", "12-25-45") == "12:25:45"


def test_extra_digits_are_dropped_not_wrapped(root):
    assert _type(root, "12345678") == "12:34:56"


def test_caret_tracks_digits_not_characters():
    """A character offset shifts when a colon is inserted; a digit count
    does not. This is what the fix relies on."""
    from utc.gui.widgets import TimeEntry
    after = TimeEntry._caret_after_digits
    assert after("12:34:56", 0) == 0
    assert after("12:34:56", 2) == 2      # just past "12"
    assert after("12:34:56", 3) == 4      # past the colon, then "3"
    assert after("12:34:56", 6) == 8
    assert after("12:34:56", 99) == 8     # never past the end


def test_completeness_flag(root):
    from utc.gui.widgets import TimeEntry
    e = TimeEntry(root)
    try:
        e.set("12:34")
        assert not e.complete
        e.set("123456")
        assert e.complete
    finally:
        e.destroy()
