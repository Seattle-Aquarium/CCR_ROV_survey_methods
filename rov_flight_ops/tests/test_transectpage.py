"""
The Transects page: the transect extractor driven from the open flight.

The point of this page is that the survey plan is entered once. These tests
guard that contract -- the page reads the live plan rather than a copy, and it
refuses clearly rather than crashing when the flight is not ready.

Skipped where there is no display, like the other GUI tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

ctk = pytest.importorskip("customtkinter")

from rov_flight_ops.survey import Site, SurveyPlan, Transect  # noqa: E402

# The window comes from the session fixture in conftest. This file used to
# build its own, which was a second Tk root -- a second Tcl interpreter, whose
# widgets cannot see images created by the first. Once the banner and the rail
# started drawing themselves as images, that surfaced as "image pyimage258
# doesn't exist" and skipped this whole file behind a "no display" guard.


@pytest.fixture
def installed(monkeypatch):
    """Pretend the extractor is installed, whatever this environment has.

    _run() checks for the extractor before anything else, so without this a
    guard test silently exercises the "not installed" branch wherever
    ccr_m2c is absent -- which is what CI is. One of these failed loudly there;
    the other passed for the wrong reason, because the not-installed message
    names ``mcap_to_csv`` and so matched a check for "mcap".
    """
    stub = (SimpleNamespace(), SimpleNamespace(STATIONS=[("Test", "9447130")]))
    monkeypatch.setattr("rov_flight_ops.gui.transectpage._extractor", lambda: stub)
    return stub


@pytest.fixture(scope="module")
def analyze(app):
    """The Analyze transects page, built off the shelf.

    It is not in the window any more (shelf.py, SHELF.md) -- the 1 Hz CSVs
    come with a download on BlueOS logs -- but it is kept working, so it is
    built here in a hidden window of its own and tested as before.
    """
    from rov_flight_ops.gui.transectpage import TransectPage

    top = ctk.CTkToplevel(app)
    top.withdraw()
    page = TransectPage(top, app)
    page.grid(row=0, column=0, sticky="nsew")
    app.update()
    yield page
    top.destroy()


def _pump(app, n=20):
    import time
    for _ in range(n):
        app.update()
        time.sleep(0.01)


def test_the_tabs_run_in_the_order_of_a_survey_day(app):
    """Set up, fly, then read back -- and no imagery, which is its own program.

    Navigation went in at position 2 in September 2026 and pushed Transects to
    3. It sits there because that is the order of the day: the folder is
    chosen on Monitoring before the ROV is wet, Navigation is where the
    operator sits for the whole dive, and the transect times are typed
    afterwards with the vehicle disarmed on deck. DVL went in at 3 in October
    2026, beside Navigation, because it is read during the dive too, and
    Parameters at 2, because it is checked on deck before the dive.
    """
    # Flight summary and Analyze transects followed BlueOS logs until November
    # 2026, when they went on the shelf (shelf.py): the report and the 1 Hz
    # CSVs are made by BlueOS logs after a download now.
    assert app.nav.sections == ["Monitoring", "Parameters", "Navigation", "DVL",
                                "Transects", "BlueOS logs"]
    assert "analyze" not in app.pages and "summary" not in app.pages
    assert not any("photo" in k.lower() or "video" in k.lower() for k in app.pages)


def test_a_tab_is_identified_by_a_key_rather_than_its_position(app):
    """Inserting a chapter must not silently reopen a different one.

    Nothing persists the open tab today. This is the guard for when something
    does: a saved "2" from before Navigation existed would now be Navigation
    rather than Transects, so what gets saved has to be the key.
    """
    assert app.nav.key_of("Transects") == "transects"
    assert app.nav.key_of("Navigation") == "navigation"
    before = app.nav.current
    try:
        assert app.nav.select_key("transects")
        assert app.nav.current == "Transects"
        assert not app.nav.select_key("a_chapter_that_was_removed")
        assert app.nav.current == "Transects"      # unchanged, not guessed at
    finally:
        if before:
            app.nav.select(before)


def test_raising_the_page_re_reads_the_plan(app, analyze):
    """The plan is edited on another page, so a page built once at startup
    would still be showing the state from then."""
    page = analyze
    app._apply_plan(SurveyPlan([Site(
        name="Centennial_Park", project="testing", date="2026-08-26",
        transects=[Transect(name="T1", start_tc="12:19:57", end_tc="12:28:42"),
                   Transect(name="T2", start_tc="12:29:55", end_tc="12:35:15")],
    )]))

    page.refresh()                     # what raising its tab does
    _pump(app)

    summary = page.plan_summary.cget("text")
    assert "Centennial_Park" in summary
    assert "T1" in summary and "T2" in summary


def test_it_refuses_without_a_flight_folder(app, analyze, monkeypatch, installed):
    said: list[str] = []
    monkeypatch.setattr("rov_flight_ops.gui.transectpage.messagebox.showinfo",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("rov_flight_ops.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))

    app.flight_dir = None
    analyze._run()

    assert said and "flight folder" in said[-1].lower()


def test_it_refuses_when_the_flight_has_no_mcaps(app, analyze, monkeypatch, tmp_path,
                                                installed):
    said: list[str] = []
    monkeypatch.setattr("rov_flight_ops.gui.transectpage.messagebox.showinfo",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("rov_flight_ops.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))

    app.flight_dir = tmp_path
    app.discovery = None
    try:
        analyze._run()
    finally:
        app.flight_dir = None

    assert said and "mcap" in said[-1].lower()
    assert "not installed" not in said[-1].lower()


def test_a_missing_extractor_explains_itself(app, analyze, monkeypatch, tmp_path):
    """The extractor is a separate install. Without it the page must say what
    to run, not stop the whole application from starting."""
    said: list[str] = []
    monkeypatch.setattr("rov_flight_ops.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("rov_flight_ops.gui.transectpage._extractor", lambda: None)

    app.flight_dir = tmp_path
    try:
        analyze._run()
    finally:
        app.flight_dir = None

    assert said and "pip install" in said[-1]


def test_several_sites_get_the_site_into_the_filename(app, analyze):
    """Transect names repeat across sites, so one flight with two sites would
    otherwise write T1.csv twice and lose the first."""
    page = analyze
    app._apply_plan(SurveyPlan([
        Site(name="Alki", project="t", date="2026-08-26",
             transects=[Transect(name="T1", start_tc="10:00:00", end_tc="10:10:00")]),
        Site(name="Centennial", project="t", date="2026-08-26",
             transects=[Transect(name="T1", start_tc="11:00:00", end_tc="11:10:00")]),
    ]))
    page.refresh()
    _pump(app)

    summary = page.plan_summary.cget("text")
    assert "Alki" in summary and "Centennial" in summary


# --------------------------------------------------------------------------
#  what replaced the tab: transectcsv, run after a download
# --------------------------------------------------------------------------


def _plan_one():
    return SurveyPlan([Site(name="Alki", project="t", date="2026-08-26",
                            transects=[Transect("T1", "10:00:00", "10:10:00")])])


def test_the_csvs_say_why_when_the_extractor_is_missing(tmp_path, monkeypatch):
    from rov_flight_ops import transectcsv
    monkeypatch.setattr(transectcsv, "extractor", lambda: None)
    with pytest.raises(RuntimeError, match="pip install"):
        transectcsv.make(tmp_path, _plan_one())


def test_the_csvs_say_why_when_there_is_nothing_to_read(tmp_path, monkeypatch):
    from rov_flight_ops import transectcsv
    monkeypatch.setattr(transectcsv, "extractor",
                        lambda: (SimpleNamespace(), SimpleNamespace(STATIONS=[])))
    (tmp_path / "logs" / "BIN").mkdir(parents=True)
    (tmp_path / "logs" / "BIN" / "00000001.BIN").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="no mcap or tlog") as got:
        transectcsv.make(tmp_path, _plan_one())
    assert "BIN" in str(got.value)
