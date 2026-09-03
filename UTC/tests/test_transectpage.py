"""
The Transects page: the transect extractor driven from the open flight.

The point of this page is that the survey plan is entered once. These tests
guard that contract -- the page reads the live plan rather than a copy, and it
refuses clearly rather than crashing when the flight is not ready.

Skipped where there is no display, like the other GUI tests.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

ctk = pytest.importorskip("customtkinter")

from utc.gui.app import App  # noqa: E402
from utc.survey import Site, SurveyPlan, Transect  # noqa: E402


@pytest.fixture(scope="module")
def app():
    """One window for the whole file.

    Building and destroying a Tk root per test breaks partway through on
    Anaconda's Tcl: the second interpreter comes up without its init script and
    the first widget fails with "invalid command name tcl_findLibrary". Each
    test sets the state it needs, so sharing one window costs nothing.
    """
    try:
        a = App()
    except Exception as ex:                      # no display on CI
        pytest.skip(f"no display: {ex}")
    a.withdraw()
    yield a
    try:
        a.destroy()
    except Exception:
        pass


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
    monkeypatch.setattr("utc.gui.transectpage._extractor", lambda: stub)
    return stub


def _pump(app, n=20):
    import time
    for _ in range(n):
        app.update()
        time.sleep(0.01)


def test_the_page_is_in_the_rail(app):
    assert "Transects" in app.pages
    # before importing photos: the CSVs need only the plan and the mcaps, and
    # the same windows go on to drive the video overlays
    names = list(app.pages)
    assert names.index("Transects") < names.index("Import photos")


def test_raising_the_page_re_reads_the_plan(app):
    """The plan is edited on another page, so a page built once at startup
    would still be showing the state from then."""
    page = app.pages["Transects"]
    app._apply_plan(SurveyPlan([Site(
        name="Centennial_Park", project="testing", date="2026-08-26",
        transects=[Transect(name="T1", start_tc="12:19:57", end_tc="12:28:42"),
                   Transect(name="T2", start_tc="12:29:55", end_tc="12:35:15")],
    )]))

    app.nav.select("Transects")
    _pump(app)

    summary = page.plan_summary.cget("text")
    assert "Centennial_Park" in summary
    assert "T1" in summary and "T2" in summary


def test_it_refuses_without_a_flight_folder(app, monkeypatch, installed):
    said: list[str] = []
    monkeypatch.setattr("utc.gui.transectpage.messagebox.showinfo",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("utc.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))

    app.flight_dir = None
    app.pages["Transects"]._run()

    assert said and "flight folder" in said[-1].lower()


def test_it_refuses_when_the_flight_has_no_mcaps(app, monkeypatch, tmp_path, installed):
    said: list[str] = []
    monkeypatch.setattr("utc.gui.transectpage.messagebox.showinfo",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("utc.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))

    app.flight_dir = tmp_path
    app.discovery = None
    app.pages["Transects"]._run()

    assert said and "mcap" in said[-1].lower()
    assert "not installed" not in said[-1].lower()


def test_a_missing_extractor_explains_itself(app, monkeypatch, tmp_path):
    """The extractor is a separate install. Without it the page must say what
    to run, not stop the whole application from starting."""
    said: list[str] = []
    monkeypatch.setattr("utc.gui.transectpage.messagebox.showerror",
                        lambda t, m: said.append(m))
    monkeypatch.setattr("utc.gui.transectpage._extractor", lambda: None)

    app.flight_dir = tmp_path
    app.pages["Transects"]._run()

    assert said and "pip install" in said[-1]


def test_several_sites_get_the_site_into_the_filename(app):
    """Transect names repeat across sites, so one flight with two sites would
    otherwise write T1.csv twice and lose the first."""
    page = app.pages["Transects"]
    app._apply_plan(SurveyPlan([
        Site(name="Alki", project="t", date="2026-08-26",
             transects=[Transect(name="T1", start_tc="10:00:00", end_tc="10:10:00")]),
        Site(name="Centennial", project="t", date="2026-08-26",
             transects=[Transect(name="T1", start_tc="11:00:00", end_tc="11:10:00")]),
    ]))
    app.nav.select("Transects")
    _pump(app)

    summary = page.plan_summary.cget("text")
    assert "Alki" in summary and "Centennial" in summary


def test_the_health_check_is_scoped_to_the_transects(app, monkeypatch, tmp_path):
    """The plan is on screen; not passing it made this page report whole-dive
    dropout counts, which on a real flight describe the transit between
    transects rather than the part being analysed."""
    seen: dict = {}

    def fake_read_health(mcaps, transects=(), progress=None):
        seen["transects"] = list(transects)
        return SimpleNamespace(lines=lambda: ["report"], concerns=lambda: [])

    monkeypatch.setitem(sys.modules, "ccr_m2c.health",
                        SimpleNamespace(read_health=fake_read_health))
    monkeypatch.setattr("utc.gui.transectpage._extractor",
                        lambda: (SimpleNamespace(), SimpleNamespace()))

    app.flight_dir = tmp_path
    app.discovery = SimpleNamespace(mcaps=[tmp_path / "a.mcap"])
    app._apply_plan(SurveyPlan([Site(
        name="Jack_Block", project="t", date="2026-09-02",
        transects=[Transect(name="T1", start_tc="09:25:23", end_tc="09:35:37"),
                   Transect(name="T2", start_tc="09:41:15", end_tc="09:50:42")],
    )]))

    app.pages["Transects"]._check_health()
    _pump(app, 40)

    assert "transects" in seen, "read_health was never called"
    names = [t.transect_id for t in seen["transects"]]
    assert names == ["Jack_Block_T1", "Jack_Block_T2"]
    assert seen["transects"][0].windows == [("09:25:23", "09:35:37")]


def test_an_unfinished_plan_still_gets_the_dive_wide_report(app, monkeypatch, tmp_path):
    seen: dict = {}

    def fake_read_health(mcaps, transects=(), progress=None):
        seen["transects"] = list(transects)
        return SimpleNamespace(lines=lambda: ["report"], concerns=lambda: [])

    monkeypatch.setitem(sys.modules, "ccr_m2c.health",
                        SimpleNamespace(read_health=fake_read_health))
    monkeypatch.setattr("utc.gui.transectpage._extractor",
                        lambda: (SimpleNamespace(), SimpleNamespace()))

    app.flight_dir = tmp_path
    app.discovery = SimpleNamespace(mcaps=[tmp_path / "a.mcap"])
    app._apply_plan(SurveyPlan([Site(
        name="S", project="t", date="2026-09-02",
        transects=[Transect(name="T1", start_tc="", end_tc="")],
    )]))

    app.pages["Transects"]._check_health()
    _pump(app, 40)

    assert seen.get("transects") == []      # reported, not crashed
