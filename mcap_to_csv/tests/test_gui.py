"""
The window, driven without a user.

These run headless where a display exists and skip where one does not, so they
are worth having in CI but never block it. What they are really guarding is the
threading: every long operation has to leave the main loop responsive, and a
result arriving late must not overwrite a newer one.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from conftest import straight_north_dive
from ccr_m2c.mcap_read import McapInfo
from ccr_m2c.pipeline import TransectSpec, run


@pytest.fixture(scope="module")
def tk_root():
    """One Tk interpreter for the whole file.

    Creating and destroying a root per test breaks partway through on Anaconda's
    Tcl -- the second interpreter comes up without its init script and the first
    ttk widget fails with "invalid command name tcl_findLibrary". Each test gets
    a Toplevel of this root instead.
    """
    try:
        root = tk.Tk()
    except tk.TclError as ex:
        pytest.skip(f"no display available: {ex}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, monkeypatch, tmp_path):
    from ccr_m2c import gui

    # Keep the test off the real user's saved settings.
    monkeypatch.setattr(gui, "_config_path", lambda: tmp_path / "cfg.json")
    a = gui.App(root=tk.Toplevel(tk_root))
    yield a
    a.close()          # cancels the polling loop, then destroys the window


def _settle(app, seconds: float = 3.0, until=None) -> None:
    """Pump the event loop, the way the user's window would be pumping it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.root.update()
        if until is not None and until():
            return
        time.sleep(0.02)


def test_window_builds_with_one_empty_transect(app):
    assert app.root.title().startswith("MCAP to CSV")
    assert len(app.transect_blocks) == 1
    assert app.transect_blocks[0].get_windows() == []


def test_transects_can_be_added_and_removed(app):
    app.add_transect()
    app.add_transect()
    assert [b.index for b in app.transect_blocks] == [1, 2, 3]

    app.transect_blocks[1]._remove_self()
    # the remaining blocks renumber, so the labels stay 1..n
    assert [b.index for b in app.transect_blocks] == [1, 2]


def test_a_transect_keeps_at_least_one_time_window(app):
    block = app.transect_blocks[0]
    assert len(block.window_rows) == 1
    block.window_rows[0]["frame"].winfo_children()[-1].invoke()   # the "x" button
    assert len(block.window_rows) == 1


def test_adding_files_does_not_block_the_main_loop(app, builder):
    """The listbox must fill in immediately, with spans arriving later."""
    path = straight_north_dive(builder(), seconds=5).close()
    app.mcap_paths = [str(path)]
    app.refresh_listbox()

    # names are on screen before any file has been opened
    assert app.listbox.size() == 1
    assert "reading..." in app.listbox.get(0)

    _settle(app, until=lambda: "reading..." not in app.listbox.get(0))
    assert "10:00:00" in app.listbox.get(0)
    assert "Combined local span" in app.span_var.get()
    assert app.date_var.get() == "20260826"      # filled in from the recording


def test_a_stale_probe_does_not_overwrite_a_newer_list(app):
    """A slow probe finishing after the user cleared the list is discarded."""
    app.probe_token = 7
    stale = McapInfo(path=Path("old.mcap"), start=0.0, end=1.0)
    app.listbox.insert(tk.END, "current contents")

    app.events.put(("probe", (6, [stale])))       # token 6 < 7
    _settle(app, seconds=0.5)
    assert app.listbox.get(0) == "current contents"

    app.events.put(("probe", (7, [stale])))       # the current token
    _settle(app, seconds=0.5, until=lambda: app.listbox.get(0) != "current contents")
    assert "old.mcap" in app.listbox.get(0)


def test_run_refuses_without_a_file(app, monkeypatch):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showerror",
                        lambda title, msg: shown.append((title, msg)))
    app.on_run()
    assert shown and "at least one .mcap" in shown[0][1]


def test_run_validates_the_date_and_the_time_windows(app, monkeypatch, builder, tmp_path):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showerror",
                        lambda title, msg: shown.append((title, msg)))

    path = straight_north_dive(builder(), seconds=5).close()
    app.mcap_paths = [str(path)]
    app.site_var.set("Site")
    app.save_var.set(str(tmp_path))
    app.date_var.set("26-08-2026")               # wrong format
    assert app._collect() is None
    assert "YYYYMMDD" in shown[-1][1]

    app.date_var.set("20260826")
    block = app.transect_blocks[0]
    block.transect_id_var.set("T1")
    block.window_rows[0]["start_var"].set("10:05:00")
    block.window_rows[0]["end_var"].set("10:01:00")   # end before start
    assert app._collect() is None
    assert "before end time" in shown[-1][1]

    block.window_rows[0]["end_var"].set("10:09:00")
    args = app._collect()
    assert args is not None
    assert args["transects"][0].transect_id == "T1"
    assert args["transects"][0].windows == [("10:05:00", "10:09:00")]


def test_duplicate_transect_ids_are_rejected(app, monkeypatch, builder, tmp_path):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showerror",
                        lambda title, msg: shown.append((title, msg)))

    path = straight_north_dive(builder(), seconds=5).close()
    app.mcap_paths = [str(path)]
    app.site_var.set("Site")
    app.date_var.set("20260826")
    app.save_var.set(str(tmp_path))
    app.add_transect()
    for block in app.transect_blocks:
        block.transect_id_var.set("SAME")
        block.window_rows[0]["start_var"].set("10:00:00")
        block.window_rows[0]["end_var"].set("10:00:04")

    assert app._collect() is None
    assert "used more than once" in shown[-1][1]


def test_a_finished_run_arrives_through_the_queue(app, monkeypatch, builder, tmp_path):
    """The worker's result must reach the window and light up the buttons.

    The pipeline itself is covered end-to-end in test_transect_and_map.py; what
    is being checked here is only the hand-off, so the result is put on the
    queue directly rather than spending a real run to produce one.
    """
    notes: list[str] = []
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showinfo",
                        lambda title, msg: notes.append(msg))
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showwarning",
                        lambda title, msg: notes.append(msg))

    path = straight_north_dive(builder(), seconds=20).close()
    result = run([path], site_name="Site", survey_date="20260826",
                 station_id=None, save_location=tmp_path,
                 transects=[TransectSpec("T1", [("10:00:05", "10:00:15")])])

    app.events.put(("progress", (0.5, "halfway")))
    app.events.put(("log", "a line of log"))
    app.events.put(("done", result))
    _settle(app, seconds=5, until=lambda: app.result is not None)

    assert app.result is result, app.status_var.get()
    assert str(app.open_btn["state"]) == "normal"
    assert str(app.map_btn["state"]) == "normal"
    assert "a line of log" in app.log_text.get("1.0", "end")
    assert "Done - 1 of 1" in app.status_var.get()
    # no tide station, so the summary flags Depth_std and warns rather than informs
    assert notes and "Saved 1 of 1" in notes[-1]


def test_a_failed_run_is_reported_and_leaves_run_enabled(app, monkeypatch):
    errors: list[str] = []
    monkeypatch.setattr("ccr_m2c.gui.messagebox.showerror",
                        lambda title, msg: errors.append(msg))

    app.run_btn.configure(state="disabled")
    app.events.put(("error", ValueError("no MAVLink telemetry parsed")))
    _settle(app, seconds=5, until=lambda: bool(errors))

    assert "no MAVLink telemetry parsed" in errors[0]
    assert str(app.run_btn["state"]) == "normal"     # the user can try again
    assert app.status_var.get() == "Failed."
