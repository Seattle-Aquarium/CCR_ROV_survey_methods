"""
The window and the BlueOS logs tab, driven the way an operator would.

The shell tests hold the layout promises: the banner folds but the rule and
its two controls stay, and every output box opens one line tall and cannot be
dragged smaller than that. The logs tests run the tab against the fake vehicle
from test_pifiles -- search, select, download, and a delete refused while armed.

Skipped where there is no display, like the other GUI tests.
"""

from __future__ import annotations

import time

import pytest

ctk = pytest.importorskip("customtkinter")

from test_pifiles import BIN_DIR, vehicle  # noqa: E402,F401  (fixture)

from rov_flight_ops import pifiles as PF  # noqa: E402
from rov_flight_ops.gui.widgets import one_line_height  # noqa: E402


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.01)


def wait_idle(app, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if not app.busy and app._queue.empty():
            pump(app, 0.2)
            return
        time.sleep(0.02)
    raise AssertionError("the job never finished")


def boxes(widget, found=None):
    found = [] if found is None else found
    if isinstance(widget, ctk.CTkTextbox) and hasattr(widget, "min_height"):
        found.append(widget)
    for child in widget.winfo_children():
        boxes(child, found)
    return found


# --------------------------------------------------------------------------
#  the shell
# --------------------------------------------------------------------------


def test_folding_the_banner_keeps_the_rule_and_both_controls(app):
    app.deiconify()
    app.geometry("1300x900")
    pump(app)
    tall = int(app.header.cget("height"))
    app.toggle_banner()
    pump(app)
    short = int(app.header.cget("height"))
    try:
        assert short < tall / 2, (tall, short)
        assert app.theme_switch.winfo_ismapped() and app.fold_btn.winfo_ismapped()
        assert app._rule_photo is not None
    finally:
        app.toggle_banner()
        pump(app)
        app.withdraw()
    assert int(app.header.cget("height")) == tall


def test_every_output_box_opens_one_line_tall(app):
    found = boxes(app)
    assert len(found) >= 3, "expected the report boxes on Monitoring at least"
    for box in found:
        assert int(box.cget("height")) == box.min_height
    assert int(app.log.cget("height")) == one_line_height("word")


def test_an_output_box_cannot_be_dragged_below_one_line(app):
    grip = app.log_grip
    assert grip.clamp(1) == grip.minimum
    grip._set(grip.clamp(300))
    assert int(app.log.cget("height")) >= 300 or grip.maximum is not None
    grip._set(grip.clamp(-50))
    assert int(app.log.cget("height")) == grip.minimum


def test_the_last_line_of_the_log_is_the_last_message(app):
    """A log dragged down to one line shows its last line, so that line must be
    the newest message rather than the empty line after it."""
    app._log("first message")
    app._log("newest message")
    text = app.log.get("1.0", "end-1c")
    assert text.endswith("newest message")


def test_switching_flights_resets_the_plan_and_the_analyze_folder(app, tmp_path,
                                                                 monkeypatch):
    """Review finding F05: the previous flight's transects and output folder
    used to survive a switch to a folder with none of its own."""
    from rov_flight_ops.survey import Site, SurveyPlan, Transect

    monkeypatch.setattr(app, "_arm_monitor", lambda: None)
    monkeypatch.setattr("rov_flight_ops.gui.app.messagebox.askyesno",
                        lambda *a, **k: True)
    a, b = tmp_path / "2026_09_13_Alki", tmp_path / "2026_09_14_Centennial"
    a.mkdir()
    b.mkdir()
    try:
        app.use_flight(a)
        app._apply_plan(SurveyPlan([Site("Alki", "t", "2026-09-13",
                                         [Transect("T1", "10:00:00", "10:10:00")])]))
        analyze = app.pages["analyze"]
        analyze.refresh()
        assert analyze._out_dir() == a

        app.use_flight(b)
        typed = [t for s in app._plan().sites for t in s.transects if t.start_tc]
        assert typed == [], "Alki's transects were carried into Centennial"
        assert analyze._out_dir() == b
    finally:
        app.flight_dir = None


def test_committing_an_address_redirects_the_running_recorder(app, monkeypatch):
    """Review finding F07: the box and the watcher used to disagree."""
    from rov_flight_ops import flightlog

    monkeypatch.setattr(app, "save_settings", lambda: None)
    before_rec, before_host = app.recorder, app.settings.get("vehicle_host", "")
    app.recorder = flightlog.FlightRecorder(host="192.168.2.2")
    monitor = app.pages["monitor"]
    try:
        monitor.host_entry.delete(0, "end")
        monitor.host_entry.insert(0, "10.0.0.9")
        monitor._remember_host()
        assert app.recorder.host == "10.0.0.9"
        assert app.vehicle_host() == "10.0.0.9"

        app.recorder.status.state = "recording"      # now mid-flight
        monitor.host_entry.delete(0, "end")
        monitor.host_entry.insert(0, "10.0.0.10")
        monitor._remember_host()
        assert app.recorder.host == "10.0.0.9", "a flight must keep its vehicle"
        assert app.recorder.status.pending_host == "10.0.0.10"
    finally:
        app.recorder.status.state = "idle"
        app.recorder = before_rec
        monitor.host_entry.delete(0, "end")
        monitor.host_entry.insert(0, before_host)
        app.settings["vehicle_host"] = before_host


def test_rates_are_shown_beside_each_reading():
    from rov_flight_ops.gui import monitorpage as M
    assert M.hz_text(M.refresh_hz("cpu_usage_pct")) == "1 Hz"
    assert M.hz_text(M.refresh_hz("rov_armed")) == "0.5 Hz"
    assert M.hz_text(M.refresh_hz("pi_soc_temp_c")) == "0.2 Hz"
    assert "10 Hz" in M.FAST_TRACE_NOTE and "5 Hz" in M.FAST_TRACE_NOTE


# --------------------------------------------------------------------------
#  the BlueOS logs tab
# --------------------------------------------------------------------------


@pytest.fixture()
def logs(app, vehicle, tmp_path, monkeypatch):     # noqa: F811
    # A dialog nobody answers blocks the test run forever, so every one is
    # stubbed; a test that cares about an answer patches that one itself.
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(f"rov_flight_ops.gui.logspage.messagebox.{name}",
                            lambda *a, **k: None)
    monitor = app.pages["monitor"]
    monitor.host_entry.delete(0, "end")
    monitor.host_entry.insert(0, vehicle.host)
    monkeypatch.setattr(app, "save_settings", lambda: None)
    app.flight_dir = tmp_path
    page = app.pages["logs"]
    page.inv = None
    page.c3_entry.delete(0, "end")
    for key, v in page.v_kind.items():
        v.set(key in ("mcap", "bin"))
    yield page
    monitor.host_entry.delete(0, "end")
    app.flight_dir = None


def test_a_search_shows_totals_and_a_type_can_be_selected(app, logs):
    logs._search()
    wait_idle(app)
    assert logs.tree.exists("cat:mcap") and logs.tree.exists("cat:bin")
    files, size, *_ = logs.tree.item("cat:bin", "values")
    assert files == "2"
    logs.tree.selection_set(("cat:bin",))
    pump(app)
    assert sorted(f.rel for f in logs.picked()) == ["00000081.BIN", "00000082.BIN"]
    assert "2 file(s)" in logs.sel_note.cget("text")


def test_copy_checks_run_off_the_window_and_a_stale_answer_is_dropped(
        app, logs, tmp_path, monkeypatch):
    """Review finding R6: every visit to this tab stat-ed every listed file on
    the window's thread. Now the tree says "checking…" until the answer comes
    back, and an answer for a flight folder already left is not shown."""
    import threading

    logs._search()
    wait_idle(app)
    release = threading.Event()
    real = PF.copy_state

    def slow(f, flight, manifest=None):
        release.wait(10)
        return real(f, flight, manifest)

    monkeypatch.setattr(PF, "copy_state", slow)
    logs._states_for = None
    began = time.monotonic()
    logs._rebuild()
    assert time.monotonic() - began < 0.5, "the rebuild waited on the disk"
    assert logs.tree.item("cat:bin", "values")[4] == "checking…"
    pump(app, 0.2)
    assert logs.tree.item("cat:bin", "values")[4] == "checking…"
    release.set()
    assert_eventually = time.monotonic() + 10
    while (logs.tree.item("cat:bin", "values")[4] == "checking…"
           and time.monotonic() < assert_eventually):
        pump(app, 0.05)
    assert logs.tree.item("cat:bin", "values")[4].endswith("of 2")

    # Started for one folder, answered after the operator moved to another.
    release.clear()
    logs._states_for = None
    logs._rebuild()
    other = tmp_path / "other"
    other.mkdir()
    app.flight_dir = other
    release.set()
    pump(app, 0.5)
    assert logs._states_for is None or logs._states_for[0] == other


def test_a_large_folder_opens_in_batches(app, logs, monkeypatch):
    logs._search()
    wait_idle(app)
    monkeypatch.setattr(type(logs), "LEAF_BATCH", 1)
    logs._toggle_individual()
    try:
        assert len(logs.tree.get_children("cat:bin")) == 1
        pump(app, 0.2)
        assert len(logs.tree.get_children("cat:bin")) == 2
    finally:
        logs._toggle_individual()


def test_individual_files_open_up_and_can_be_picked_one_by_one(app, logs):
    logs._search()
    wait_idle(app)
    logs._toggle_individual()
    try:
        kids = logs.tree.get_children("cat:bin")
        assert len(kids) == 2
        logs.tree.selection_set((kids[0],))
        assert [f.rel for f in logs.picked()] == ["00000081.BIN"]
    finally:
        logs._toggle_individual()


def test_download_uses_the_selection_when_no_period_is_chosen(app, logs, tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr("rov_flight_ops.gui.logspage.messagebox.askyesno",
                        lambda *a, **k: True)
    logs._search()
    wait_idle(app)
    logs.tree.selection_set(("cat:mcap",))
    logs._download()
    wait_idle(app)
    got = sorted(p.name for p in (tmp_path / "logs" / "mcap").iterdir())
    assert got == ["recorder_20260801_120000.mcap", "recorder_20260911_184953.mcap"]
    assert not (tmp_path / "logs" / "BIN").exists()


def test_types_not_yet_searched_are_listed_before_downloading(app, logs, tmp_path,
                                                              monkeypatch, vehicle):  # noqa: F811
    monkeypatch.setattr("rov_flight_ops.gui.logspage.messagebox.askyesno",
                        lambda *a, **k: True)
    for key, v in logs.download.v_kind.items():
        v.set(key == "video")
    logs.download.v_period.set(PF.PERIOD_ALL)
    logs._download()                  # lists mcap video first, then comes back
    wait_idle(app)
    pump(app, 0.3)
    wait_idle(app)
    assert (tmp_path / "logs/mcap_video/recorder_20260911_184953/stream_0.mp4").is_file()
    logs.download.v_period.set("")
    for v in logs.download.v_kind.values():
        v.set(False)


def test_cleaning_is_refused_while_armed(app, logs, vehicle, monkeypatch):  # noqa: F811
    said = []
    monkeypatch.setattr("rov_flight_ops.gui.logspage.messagebox.askyesno",
                        lambda *a, **k: True)
    monkeypatch.setattr("rov_flight_ops.gui.logspage.messagebox.showerror",
                        lambda t, m: said.append(m))
    logs._search()
    wait_idle(app)
    vehicle.armed = True
    before = dict(vehicle.fs)
    logs.tree.selection_set(("cat:bin",))
    logs._delete()
    wait_idle(app)
    assert said and "armed" in said[-1].lower()
    assert vehicle.fs == before


def test_cleaning_removes_the_chosen_files_and_updates_the_list(app, logs, vehicle,  # noqa: F811
                                                                tmp_path, monkeypatch):
    monkeypatch.setattr("rov_flight_ops.gui.logspage.messagebox.askyesno",
                        lambda *a, **k: True)
    logs._search()
    wait_idle(app)
    for key, v in logs.clean.v_kind.items():
        v.set(key == "bin")
    logs.clean.v_period.set(PF.PERIOD_ALL)
    logs.tree.selection_set(())
    logs._delete()
    wait_idle(app)
    assert f"{BIN_DIR}/00000081.BIN" not in vehicle.fs
    assert f"{BIN_DIR}/00000082.BIN" in vehicle.fs, "still being written"
    assert logs.tree.item("cat:bin", "values")[0] == "1"
    assert list((tmp_path / "logs").glob("pi_cleanup_*.txt"))
    logs.clean.v_period.set("")
    for v in logs.clean.v_kind.values():
        v.set(False)
