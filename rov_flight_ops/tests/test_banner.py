"""
The title banner: the three software versions, and the two lamps.

These are the things an operator glances at while flying, so what is checked
here is mostly that they cannot lie -- a lamp that stays green after the
tether is pulled, or a version that disappears the moment the vehicle does,
would each be worse than not being there at all.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import blueos  # noqa: E402
from rov_flight_ops.flightlog import Status  # noqa: E402
from rov_flight_ops.gui.shell import UNKNOWN_VERSION, Lamp  # noqa: E402


class _Recorder:
    """Just enough recorder for `App.vehicle_status` to read."""

    def __init__(self, *, state="idle", watching=False, seen_ago=None):
        self.status = Status(state=state)
        self.watching = watching
        if seen_ago is not None:
            self.status.last_seen = time.time() - seen_ago


# --------------------------------------------------------------------------
#  "is the vehicle there"
# --------------------------------------------------------------------------


def test_a_vehicle_that_answered_just_now_is_connected():
    st = Status()
    st.last_seen = time.time()
    assert st.reachable


def test_a_vehicle_that_has_stopped_answering_is_not():
    """`armed` holds its last value forever, so it cannot stand in for this:
    a vehicle disarmed and then unplugged still reads 'disarmed'."""
    st = Status(armed=False)
    st.last_seen = time.time() - 30
    assert not st.reachable


def test_a_vehicle_never_heard_from_is_not_connected():
    assert not Status().reachable


# --------------------------------------------------------------------------
#  the lamps, through the application
# --------------------------------------------------------------------------


def test_no_recorder_means_both_lamps_are_dark(app):
    saved = app.recorder
    app.recorder = None
    try:
        assert app.vehicle_status() == (False, "off")
    finally:
        app.recorder = saved


def test_watching_but_not_recording_is_the_middle_state(app):
    """Between two transects the monitoring is up and nothing is being
    written, and that difference is the reason there are three states."""
    saved = app.recorder
    app.recorder = _Recorder(watching=True, seen_ago=0.0)
    try:
        assert app.vehicle_status() == (True, "waiting")
    finally:
        app.recorder = saved


def test_recording_lights_the_logging_lamp(app):
    saved = app.recorder
    app.recorder = _Recorder(state="recording", watching=True, seen_ago=0.0)
    try:
        assert app.vehicle_status() == (True, "on")
    finally:
        app.recorder = saved


def test_losing_the_tether_darkens_the_vehicle_lamp_but_not_the_logging_one(app):
    """The recorder goes on writing the laptop's own columns through a tether
    dropout, and saying otherwise would send somebody chasing the wrong fault."""
    saved = app.recorder
    app.recorder = _Recorder(state="recording", watching=True, seen_ago=30.0)
    try:
        assert app.vehicle_status() == (False, "on")
    finally:
        app.recorder = saved


def test_a_lamp_has_three_distinguishable_states():
    glyphs = {Lamp.OFF[0], Lamp.WAITING[0], Lamp.ON[0]}
    colours = {Lamp.OFF[1], Lamp.WAITING[1], Lamp.ON[1]}
    # Not by colour alone: a laptop in daylight, and being colour-blind, both
    # have to survive this.
    assert len(glyphs) == 2 and len(colours) == 3
    assert Lamp.OFF[0] != Lamp.ON[0]


# --------------------------------------------------------------------------
#  the versions
# --------------------------------------------------------------------------


def test_versions_read_briefly_never_raise_when_nothing_answers():
    """Most of a survey day has no vehicle plugged in. That is not an error."""
    got = blueos.read_versions_brief("127.0.0.1:1", timeout=0.2)
    assert set(got) >= {"blueos", "ardusub", "cockpit"}
    assert got["blueos"] == "" and got["ardusub"] == ""


def test_the_banner_shows_a_dash_until_something_answers(app):
    saved = dict(app._versions)
    try:
        app._versions = dict.fromkeys(("blueos", "ardusub", "cockpit"), "")
        assert app.version_lines() == [f"BlueOS {UNKNOWN_VERSION}",
                                       f"ArduSub {UNKNOWN_VERSION}",
                                       f"Cockpit {UNKNOWN_VERSION}"]
    finally:
        app._versions = saved


def test_a_version_already_read_is_not_erased_by_a_dropout(app):
    """The tether comes and goes; the vehicle has not changed underneath it."""
    saved = dict(app._versions)
    try:
        app.merge_versions({"blueos": "1.4.0", "ardusub": "4.5.1",
                            "cockpit": "1.19.2"})
        assert app.version_lines() == ["BlueOS 1.4.0", "ArduSub 4.5.1",
                                       "Cockpit 1.19.2"]

        # The next poll finds nothing, because the tether is out.
        app.merge_versions({"blueos": "", "ardusub": "", "cockpit": ""})
        assert app.version_lines() == ["BlueOS 1.4.0", "ArduSub 4.5.1",
                                       "Cockpit 1.19.2"]

        # A read that failed outright is handled the same way.
        app.merge_versions(RuntimeError("no route to host"))
        assert app.version_lines()[0] == "BlueOS 1.4.0"

        # A vehicle that has actually been updated does change it.
        app.merge_versions({"blueos": "1.5.0"})
        assert app.version_lines()[0] == "BlueOS 1.5.0"
    finally:
        app._versions = saved
        app._show_versions()


def test_the_version_host_does_not_commit_the_address_box(app):
    """It runs once a second. A poll must not be able to change anything."""
    saved = app.settings.get("vehicle_host", "")
    try:
        app.settings["vehicle_host"] = "10.0.0.9"
        assert app.version_host() == "10.0.0.9"
        app.settings["vehicle_host"] = ""
        assert app.version_host() == "192.168.2.2"
    finally:
        app.settings["vehicle_host"] = saved


def test_cockpit_is_looked_for_on_this_laptop_too():
    """Cockpit is normally flown from the topside laptop, where the vehicle
    cannot see it. A laptop without it installed returns nothing, quietly."""
    from rov_flight_ops import laptop

    got = laptop.cockpit_version()
    assert isinstance(got, str)


# --------------------------------------------------------------------------
#  the two layouts
# --------------------------------------------------------------------------


def test_folding_moves_the_versions_and_lamps_into_the_control_row(app):
    was_open = app.banner_open
    try:
        if not app.banner_open:
            app.toggle_banner()
        app.update_idletasks()
        # Open: the versions are drawn beside the title, so the widget that
        # carries them when folded is not in the control row at all.
        assert not app.versions_row.grid_info()
        assert int(app.lamps["vehicle"].grid_info()["row"]) == 1
        assert int(app.lamps["logging"].grid_info()["row"]) == 1
        # ... aligned under the two controls they belong to.
        assert (int(app.lamps["vehicle"].grid_info()["column"])
                == int(app.diag_btn.grid_info()["column"]))
        assert (int(app.lamps["logging"].grid_info()["column"])
                == int(app.theme_switch.grid_info()["column"]))

        app.toggle_banner()
        app.update_idletasks()
        assert app.versions_row.grid_info()
        assert int(app.lamps["vehicle"].grid_info()["row"]) == 0
        # Reading left to right: versions, lamps, then the three controls.
        order = [app.versions_row, app.lamps["vehicle"], app.lamps["logging"],
                 app.diag_btn, app.theme_switch, app.fold_btn]
        columns = [int(w.grid_info()["column"]) for w in order]
        assert columns == sorted(columns) and len(set(columns)) == len(columns)
    finally:
        if app.banner_open != was_open:
            app.toggle_banner()


def test_the_fold_button_stays_put(app):
    """The one thing about this banner that has always been deliberate: the
    control that brings it back does not move out from under the pointer."""
    was_open = app.banner_open
    try:
        if not app.banner_open:
            app.toggle_banner()
        app.update_idletasks()
        app.update()
        open_x = app.fold_btn.winfo_rootx()
        app.toggle_banner()
        app.update_idletasks()
        app.update()
        assert abs(app.fold_btn.winfo_rootx() - open_x) <= 2
    finally:
        if app.banner_open != was_open:
            app.toggle_banner()


def test_the_open_banner_is_tall_enough_for_two_rows_of_controls(app):
    was_open = app.banner_open
    try:
        if not app.banner_open:
            app.toggle_banner()
        app.update_idletasks()
        app.update()
        assert app.header.winfo_height() >= app.controls.winfo_reqheight()
    finally:
        if app.banner_open != was_open:
            app.toggle_banner()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
