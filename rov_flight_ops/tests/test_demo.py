"""
The web demo: the program running its simulator for visitors.

What it has to guarantee is what the simulator guarantees, made permanent:
it is on from the first frame and stays on, no vehicle is ever asked for, and
nothing is written outside the simulator's own folder -- including across the
restart that keeps a long-running demo fresh.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import demo, desktop  # noqa: E402

# --------------------------------------------------------------------------
#  switching it on
# --------------------------------------------------------------------------


def test_the_flag_or_the_environment_turns_it_on():
    assert demo.requested(["launch.py", "--demo"], {})
    assert demo.requested([], {demo.ENV: "1"})
    assert demo.requested([], {demo.ENV: "yes"})
    assert not demo.requested([], {demo.ENV: "0"})
    assert not demo.requested(["launch.py"], {})


def test_the_restart_interval_is_minutes_and_zero_means_never():
    assert demo.restart_every_s({}) == demo.DEFAULT_RESTART_MIN * 60
    assert demo.restart_every_s({demo.RESTART_ENV: "30"}) == 1800
    assert demo.restart_every_s({demo.RESTART_ENV: "0"}) == 0
    assert demo.restart_every_s({demo.RESTART_ENV: "soon"}) == demo.DEFAULT_RESTART_MIN * 60


def test_file_dialogs_open_in_the_sandbox_and_refuse_anything_outside(tmp_path,
                                                                       monkeypatch):
    from tkinter import filedialog
    root = tmp_path / "simulator"
    inside = root / "flights" / "survey_plan.json"
    outside = tmp_path / "elsewhere" / "survey_plan.json"
    asked = []
    answer = {"path": str(inside)}

    def fake(**kw):
        asked.append(kw)
        return answer["path"]

    for name in ("askdirectory", "askopenfilename", "asksaveasfilename"):
        monkeypatch.setattr(filedialog, name, fake)
    refused = []
    demo.confine_file_dialogs(root, refused.append)

    assert filedialog.asksaveasfilename(initialdir="C:/flights") == str(inside)
    assert asked[-1]["initialdir"] == str(root)
    answer["path"] = str(outside)
    assert filedialog.askopenfilename() == ""
    assert refused == [str(outside)]
    answer["path"] = ""
    assert filedialog.askdirectory() == ""          # cancelled is not refused
    assert len(refused) == 1


def test_opening_a_folder_with_nothing_to_open_it_says_so(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.open_path(Path.cwd()) is False


# --------------------------------------------------------------------------
#  the window
# --------------------------------------------------------------------------


def test_the_demo_starts_simulating_and_stays_on_through_its_restart(app, monkeypatch):
    from tkinter import filedialog

    from rov_flight_ops.sim import controller
    from rov_flight_ops.sim.nav import SimNavCollector

    for name in ("askdirectory", "askopenfilename", "asksaveasfilename"):
        monkeypatch.setattr(filedialog, name, getattr(filedialog, name))
    asked = []
    monkeypatch.setattr(controller, "vehicle_answers",
                        lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(app, "_fill_screen", lambda: None)
    monkeypatch.setattr(app, "demo", True)
    nav = app.pages["navigation"]
    live = []
    monkeypatch.setattr(nav, "_start_live", lambda: live.append(True))
    sim, switch = app.simulator, app.sim_controls.switch
    before = app.flight_dir
    try:
        app._start_demo()
        assert app.simulating and "simulator" in str(app.flight_dir)
        assert switch.get() and switch.cget("state") == "disabled"
        assert app.browse_btn.cget("state") == "disabled"

        # It does not look for a vehicle.
        sim._probe_at = 0.0
        assert sim.tick() == ""
        time.sleep(0.1)
        assert not asked and not sim._probing

        # Nor can a folder be chosen out from under it.
        app.use_flight(Path.cwd())
        assert app.simulating and "simulator" in str(app.flight_dir)

        # The restart: a new world, and the real vehicle never read between.
        world = sim.world
        sim.restart_every_s = 60.0
        sim.started_at = time.monotonic() - 61.0
        assert sim.tick() == ""
        assert sim.world is not world and sim.active
        assert isinstance(nav.collector, SimNavCollector)
        assert not live
        assert switch.get()
    finally:
        sim.restart_every_s = 0.0
        monkeypatch.setattr(app, "demo", False)
        sim.disengage("test")
        switch.configure(state="normal", text="ROV flight simulator")
        app.browse_btn.configure(state="normal")
    assert not app.simulating and app.flight_dir == before


def test_a_restart_is_put_off_while_a_job_is_running(app, monkeypatch):
    monkeypatch.setattr("rov_flight_ops.sim.controller.vehicle_answers",
                        lambda *a, **k: False)
    # Switching the simulator off starts the Navigation tab reading the real
    # vehicle, on threads that outlive this test and would answer to the next
    # file's fake mavlink2rest (see the `app` fixture).
    monkeypatch.setattr(app.pages["navigation"], "_start_live", lambda: None)
    sim = app.simulator
    sim.engage()
    try:
        world = sim.world
        sim.restart_every_s = 60.0
        sim.started_at = time.monotonic() - 61.0
        monkeypatch.setattr(type(app), "busy", property(lambda self: True))
        sim.tick()
        assert sim.world is world
        monkeypatch.setattr(type(app), "busy", property(lambda self: False))
        sim.tick()
        assert sim.world is not world
    finally:
        sim.restart_every_s = 0.0
        sim.disengage("test")
