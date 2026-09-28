"""
Typing a pause on the first screen, and getting it back out.

The plan is entered once and everything else reads it, so the editor has to
round-trip a pause exactly: a saved plan with pauses must open with its pause
cells filled, and the plan the app hands downstream must carry what the cells
say. Skipped where there is no display, like the other GUI tests.
"""

from __future__ import annotations

import pytest

ctk = pytest.importorskip("customtkinter")

from utc.survey import Pause, Site, SurveyPlan, Transect  # noqa: E402

# The window comes from the session fixture in conftest -- see test_transectpage
# for why a second Tk root of this file's own is what to avoid.


def _pump(app, n=10):
    import time
    for _ in range(n):
        app.update()
        time.sleep(0.01)


def _plan_with_pauses() -> SurveyPlan:
    return SurveyPlan([Site(
        name="Centennial", project="HSIL", date="2026-09-16",
        transects=[
            Transect("T1", "13:00:00", "13:20:00",
                     [Pause("13:05:00", "13:06:30")]),
            Transect("T2", "13:30:00", "13:45:00"),
        ])])


def test_a_saved_plans_pauses_come_back_out_of_the_editor(app):
    app._apply_plan(_plan_with_pauses())
    _pump(app)

    plan = app._plan()
    t1, t2 = plan.sites[0].transects
    assert [(p.start_tc, p.end_tc) for p in t1.pauses] == [("13:05:00", "13:06:30")]
    assert t2.pauses == []
    assert plan.validate() == []


def test_the_row_says_how_much_was_surveying(app):
    app._apply_plan(_plan_with_pauses())
    _pump(app)
    row_with, row_without = app._sites[0]._rows
    assert row_with.status.cget("text") == "18.5 min surveying  ·  1.5 min paused"
    assert row_without.status.cget("text") == "15.0 min"


def test_add_pause_makes_a_cell_and_remove_takes_it_away(app):
    app._apply_plan(_plan_with_pauses())
    _pump(app)
    row = app._sites[0]._rows[1]                       # T2, no pauses yet
    assert not row.pauses_frame.winfo_manager()        # hidden while empty

    row.add_pause("13:35:00", "13:36:00")
    _pump(app)
    assert row.pauses_frame.winfo_manager()
    assert len(row._pauses) == 1
    assert app._plan().sites[0].transects[1].paused_s() == 60

    row._remove_pause(row._pauses[0])
    _pump(app)
    assert row._pauses == []
    assert not row.pauses_frame.winfo_manager()
    assert app._plan().sites[0].transects[1].pauses == []


def test_a_pause_typed_against_the_wrong_row_is_flagged_on_that_row(app):
    """The likeliest mistake: the pause is real, the row is not. The row
    turns it into words rather than clipping it to nothing."""
    app._apply_plan(_plan_with_pauses())
    _pump(app)
    row = app._sites[0]._rows[1]                       # T2 runs 13:30-13:45
    row.add_pause("13:05:00", "13:06:00")              # T1's pause
    _pump(app)
    assert "not inside" in row.status.cget("text")
    assert any("not inside" in e for e in app._plan().validate())


def test_pause_cells_are_numbered_and_renumbered(app):
    app._apply_plan(_plan_with_pauses())
    _pump(app)
    row = app._sites[0]._rows[0]
    row.add_pause("13:10:00", "13:11:00")
    _pump(app)
    assert [c.index_label.cget("text") for c in row._pauses] == ["pause 1", "pause 2"]

    row._remove_pause(row._pauses[0])
    _pump(app)
    assert [c.index_label.cget("text") for c in row._pauses] == ["pause 1"]
    assert app._plan().sites[0].transects[0].pauses == [Pause("13:10:00", "13:11:00")]
