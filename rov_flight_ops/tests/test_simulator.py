"""
The ROV flight simulator.

Three things are checked hardest, because they are what would make it
dangerous or useless:

* **It never touches a vehicle or a real flight folder.** Writes are refused,
  every simulated address is local or cannot resolve, the sandbox is the only
  folder it empties, it will not start with a vehicle connected, and switching
  it off puts everything back.
* **Each fault is one thing, on its own tab.** A Monitoring group's fault
  changes that group and nothing else, and the board is green again before
  the next one.
* **The program's own code judges it.** The Navigation collector, the DVL
  capture and the UGPS reader are the real ones; these tests read what they
  made of the simulated vehicle.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import bands as B  # noqa: E402
from rov_flight_ops import laptop as L  # noqa: E402
from rov_flight_ops.nav import mav2rest as M2R  # noqa: E402
from rov_flight_ops.sim import schedule as S  # noqa: E402
from rov_flight_ops.sim import world as W  # noqa: E402


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


# --------------------------------------------------------------------------
#  the fault clock
# --------------------------------------------------------------------------


def test_thirty_seconds_of_fault_then_fifteen_green_then_the_next():
    sch = S.Schedule(start=100.0)
    assert not sch.phase(50.0).faulty                  # the history is green
    p = sch.phase(110.0)
    assert p.faulty and p.cycle == 0 and p.left == pytest.approx(20.0)
    p = sch.phase(131.0)
    assert not p.faulty and p.cycle == 0 and p.left == pytest.approx(14.0)
    assert sch.phase(145.0).faulty and sch.phase(145.0).cycle == 1
    a, _ = sch.fault("DVL", 110.0)
    b, _ = sch.fault("DVL", 150.0)
    assert a is not None and b is not None and a.key != b.key
    assert sch.fault("DVL", 135.0) == (None, 0.0)


def test_every_tab_and_group_has_its_own_list_of_faults():
    for channel in S.CHANNELS:
        faults = S.CATALOG[channel]
        assert len(faults) >= 3, channel
        assert len({f.key for f in faults}) == len(faults), channel
        for f in faults:
            assert f.channel == channel and f.title and len(f.detail) > 60
    assert set(S.MONITOR_CHANNELS) == set(L.GROUPS)


# --------------------------------------------------------------------------
#  the vehicle
# --------------------------------------------------------------------------


def test_the_vehicle_flies_its_lanes_off_pier_59():
    w = W.SimWorld(mono=1000.0)
    for t in range(int(W.RUN_IN_S) + 5, int(W.RUN_IN_S + W.LOOP_S), 7):
        v = w.vehicle(float(t))
        assert W.LANE_WEST - 3 <= v.east <= W.LANE_EAST + 3, t
        assert W.LANE_NORTH[0] - 2 <= v.north <= W.LANE_NORTH[-1] + 2, t
        assert 12.0 < v.depth < 17.0 and 0.5 < v.altitude < 2.5, t
    lanes = {w.vehicle(float(t)).lane for t in range(0, 1200, 3)}
    assert {0, 1, 2, 3} <= lanes


def test_a_pi_outage_disarms_the_vehicle_and_stops_it_where_it_is():
    w = W.SimWorld(mono=1000.0)
    starts = w.schedule.windows(S.NAV, "pi_outage", 5000.0)
    assert starts
    s0 = starts[0]
    assert w.pi_silent(s0 + 6) and not w.pi_silent(s0 + 13)
    assert w.disarmed(s0 + 10) and not w.disarmed(s0 + 25)
    a, b = w.vehicle(s0 + 9), w.vehicle(s0 + 15)
    assert not a.armed and not b.armed
    assert (a.north, a.east) == pytest.approx((b.north, b.east))
    assert b.depth < a.depth                          # drifting up, disarmed


def test_mavlink_counters_climb_and_hold():
    w = W.SimWorld(mono=1000.0)
    c0 = w.count((1, 1, "ATTITUDE"), 10.0, 100.0)
    c1 = w.count((1, 1, "ATTITUDE"), 10.0, 101.0)
    assert c1 - c0 == 10
    held = w.count((1, 1, "ATTITUDE"), 10.0, 105.0, flowing=False)
    assert held == c1


# --------------------------------------------------------------------------
#  the laptop, one fault per group
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flight():
    from rov_flight_ops.sim.laptop import LaptopModel
    w = W.SimWorld(mono=1000.0)
    m = LaptopModel(w, flight_id="SIM")
    out = [m.row(float(t)) for t in range(0, 600 + int(S.CYCLE_S * 9))]
    return w, out


def test_the_history_before_the_first_fault_is_healthy(flight):
    _w, out = flight
    history = out[:600]
    for column in (c for g in L.GROUPS.values() for c in g):
        flagged = sum(1 for lv in B.levels(column, history) if lv)
        assert flagged <= 6, column


def test_each_groups_fault_shows_in_that_group_and_nowhere_else(flight):
    w, out = flight
    for cycle in range(9):
        s0 = int(600 + cycle * S.CYCLE_S)
        if w.schedule.fault_for(S.NAV, cycle).key == "pi_outage":
            continue                      # that one reaches everything, on purpose
        for group in S.MONITOR_CHANNELS:
            fault = w.schedule.fault_for(group, cycle)
            window = out[:s0 + 30]
            hit = [c for c in L.GROUPS[group]
                   if any(lv for lv in B.levels(c, window)[s0:s0 + 30])]
            assert hit, (cycle, group, fault.key)
        # ...and the board is green again by the end of the green interval.
        tail = out[:s0 + 45]
        late = [c for g in L.GROUPS.values() for c in g
                if any(lv for lv in B.levels(c, tail)[s0 + 40:s0 + 45])]
        assert not late, (cycle, late)


def test_a_group_fault_leaves_the_other_groups_alone(flight):
    w, out = flight
    s0 = 600                                       # cycle 0
    window = out[:s0 + 30]
    for group in S.MONITOR_CHANNELS:
        fault = w.schedule.fault_for(group, 0)
        for other, columns in L.GROUPS.items():
            if other == group:
                continue
            for c in columns:
                if c in L.GROUPS[group]:
                    continue
                flagged = [lv for lv in B.levels(c, window)[s0:s0 + 30] if lv]
                # A reading in another group may be touched only by that
                # group's own fault in the same cycle.
                own = w.schedule.fault_for(other, 0)
                assert not flagged or own is not None, (fault.key, c)


# --------------------------------------------------------------------------
#  MAVLink, answered by the simulator and read by the real code
# --------------------------------------------------------------------------


def test_the_simulated_mavlink2rest_is_read_by_the_real_reader():
    from rov_flight_ops.sim.nav import SimMav
    w = W.SimWorld()
    mav = SimMav(w)
    a = mav.read("ATTITUDE")
    assert a.fresh and a.counter and a.frequency == pytest.approx(10.0)
    time.sleep(0.25)
    b = mav.read("ATTITUDE")
    assert b.fresh and b.counter > a.counter
    never = mav.read("GPS_GLOBAL_ORIGIN")
    assert never.error == M2R.NEVER_SENT and not never.fresh
    assert mav.choose_vehicle() == (1, 1)


def test_the_simulator_sends_nothing_whatever_the_write_lock_says():
    from rov_flight_ops.sim.nav import SIM_HOST, SimMav
    mav = SimMav(W.SimWorld())
    mav.allow_writes = True
    with pytest.raises(M2R.VehicleWriteRefused):
        mav.send({"message": {}}, what="a test write")
    with pytest.raises(M2R.VehicleWriteRefused):
        mav.request_message(49)
    assert SIM_HOST.endswith(".invalid")              # can never resolve


def test_during_a_pi_outage_nothing_answers():
    from rov_flight_ops.sim.nav import SimMav
    w = W.SimWorld()
    s0 = w.schedule.windows(S.NAV, "pi_outage", w.t() + 3000)[0]
    w.t0_mono -= (s0 + 6) - w.t()                     # jump into the outage
    s = SimMav(w).read("HEARTBEAT")
    assert s.error and s.http_status is None


def test_the_navigation_collector_keeps_slow_readings_between_slow_reads():
    """A cycle that did not read the slow group used to drop the estimator
    and acoustic readings, so seven snapshots in eight said "no
    EKF_STATUS_REPORT" on a healthy vehicle."""
    from rov_flight_ops.sim.nav import SimNavCollector
    col = SimNavCollector(W.SimWorld())
    fast = col.mav.read_many(M2R.FAST)
    slow = col.mav.read_many(M2R.SLOW)
    now = time.monotonic()
    col._assemble(fast, slow, now)
    assert col.snapshot().ekf and col.snapshot().ugps
    col._assemble(col.mav.read_many(M2R.FAST), {}, now + 0.25)
    s = col.snapshot()
    assert s.ekf.get("horiz_pos_abs") is not None
    assert s.ugps.get("fix") is not None


def test_the_sensor_matrix_reads_the_simulated_vehicle():
    from rov_flight_ops.gui.navstatus import matrix_rows
    from rov_flight_ops.nav import trust as TR
    from rov_flight_ops.sim.nav import SimNavCollector
    w = W.SimWorld()
    w.schedule.start = w.t() + 600                    # all green for this test
    col = SimNavCollector(w)
    col.start("test")
    try:
        assert wait_for(lambda: col.snapshot().ekf and col.snapshot().rov_fix, 10)
        s = col.snapshot()
        assert s.link.connected and s.mode.value == "Surftrak"
        assert TR.aiding_mode(s)[0] == TR.ABSOLUTE
        rows = matrix_rows(s, time.monotonic())
        assert [m[0] for m in rows["acoustic"].marks[1:3]] == ["✓", "✓"]
        assert rows["range"].marks[2][0] == "✓"
    finally:
        col.stop(2.0)


# --------------------------------------------------------------------------
#  the DVL and the G2, over localhost
# --------------------------------------------------------------------------


def test_the_dvl_tabs_capture_sees_a_dead_beam_and_three_beam_velocity(tmp_path):
    from rov_flight_ops.sim.dvl import FieldDvl, SimDvlRecorder
    w = W.SimWorld()                     # cycle 0: the DVL tab's first fault
    assert w.fault(S.DVL)[0].key == "beam_dead"
    dev = FieldDvl(w).start()
    rec = SimDvlRecorder(w, dev, snapshot_hz=1.0)
    started = rec.use_folder(tmp_path)    # a folder offered is not used
    try:
        assert started.wait(30)
        assert wait_for(lambda: rec.live.snapshot(history=False).velocity, 15)
        snap = rec.live.snapshot(history=False)
        v = snap.velocity
        assert v["t2_valid"] in (0, False) and v["t2_distance"] == -1
        assert v["velocity_valid"] in (1, True)
        assert not snap.capturing and rec.folder is None
        assert not any(tmp_path.iterdir())
        assert wait_for(lambda: "255/0/VISION_POSITION_DELTA"
                        in rec.live.snapshot(history=False).mavlink, 10)
        hist = rec.live.snapshot().history
        assert len(hist) > 2000                         # the charts open full
        assert all(a.t <= b.t for a, b in zip(hist, hist[1:], strict=False))
    finally:
        rec.shutdown(timeout=8)
        dev.stop()


def test_the_g2_shows_a_weak_receiver_when_the_navigation_fault_says_so():
    from rov_flight_ops.sim.g2 import FieldG2
    w = W.SimWorld()
    weak = [i for i, f in enumerate(S.CATALOG[S.NAV]) if f.key == "ugps_weak_receiver"][0]
    w.schedule.start = w.t() - weak * S.CYCLE_S - 1
    g2 = FieldG2(w)
    try:
        code, body = g2.answer("/api/v1/position/acoustic/filtered")
        assert code == 200
        rssi = body["receiver_rssi"]
        others = [r for i, r in enumerate(rssi) if i != 2]
        assert rssi[2] < min(others) - 8
    finally:
        g2.server.server_close()


# --------------------------------------------------------------------------
#  the Pi, the sandbox, and starting at all
# --------------------------------------------------------------------------


def test_the_simulated_pi_lists_every_type_and_the_newest_files_are_growing():
    from rov_flight_ops import pifiles as PF
    from rov_flight_ops.sim import pi
    inv = pi.inventory(W.SimWorld(), [c.key for c in PF.CATEGORIES])
    for c in PF.CATEGORIES:
        assert inv.files.get(c.key), c.key
    growing = [f for f in inv.all_files() if f.modified and time.time() - f.modified < 60]
    assert growing, "the files of the flight in progress are still being written"
    assert "armed" in pi.REFUSED_ARMED


def test_the_sandbox_is_the_only_folder_it_writes_or_empties(tmp_path, monkeypatch):
    from rov_flight_ops.nav import plan as P
    from rov_flight_ops.sim import sandbox
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    w = W.SimWorld()
    folder = sandbox.prepare(w)
    assert folder.is_relative_to(tmp_path / "CCR_ROV" / "rov_flight_ops" / "simulator")
    assert (folder / P.FILENAME).is_file()
    lanes = sandbox.grid().lanes()
    assert [round(ln.start[1], 3) for ln in lanes] == list(W.LANE_NORTH)
    assert lanes[0].start == pytest.approx((W.LANE_EAST, W.LANE_NORTH[0]))
    (folder / "stray.txt").write_text("x")
    again = sandbox.prepare(w)
    assert not (again / "stray.txt").exists()


def test_a_vehicle_answering_keeps_it_off(monkeypatch):
    from rov_flight_ops import blueos
    from rov_flight_ops.sim import controller
    monkeypatch.setattr(blueos, "read_arm_state", lambda host, timeout=6.0: (False, 12.0))
    assert controller.vehicle_answers("192.0.2.1")
    monkeypatch.setattr(blueos, "read_arm_state", lambda host, timeout=6.0: (None, None))
    assert not controller.vehicle_answers("192.0.2.1")


# --------------------------------------------------------------------------
#  the window
# --------------------------------------------------------------------------


def _pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(0.02)


def test_the_switch_sits_left_of_diagnostics_open_or_folded(app):
    box, line = app.sim_controls.pairs()[0]
    diag = app.diag_btn
    if not app.banner_open:
        app.toggle_banner()
    assert int(box.grid_info()["column"]) < int(diag.grid_info()["column"])
    assert int(line.grid_info()["row"]) == 1
    app.toggle_banner()
    try:
        info = box.grid_info()
        assert int(info["row"]) == 0
        assert int(app.lamps["logging"].grid_info()["column"]) < int(info["column"])
        assert int(line.grid_info()["column"]) < int(diag.grid_info()["column"])
    finally:
        app.toggle_banner()


def test_it_will_not_start_while_a_vehicle_answers(app, monkeypatch):
    from rov_flight_ops.flightlog import Status

    class Answering:
        status = Status(state="idle")
        watching = True
        host = "192.168.2.2"
    Answering.status.last_seen = time.time()
    said = []
    monkeypatch.setattr("rov_flight_ops.gui.simbanner.messagebox.showinfo",
                        lambda *a, **k: said.append(a))
    saved = app.recorder
    app.recorder = Answering()
    try:
        app.sim_controls.switch.select()
        app.sim_controls._switched()
        assert not app.simulating and said
        assert not app.sim_controls.switch.get()
    finally:
        app.recorder = saved


def test_switching_it_on_and_off_puts_everything_back(app, monkeypatch):
    from rov_flight_ops.sim.nav import SimNavCollector
    from rov_flight_ops.sim.recorder import SimRecorder
    monkeypatch.setattr("rov_flight_ops.sim.controller.vehicle_answers",
                        lambda *a, **k: False)
    before = (app.flight_dir, app.recorder, dict(app._versions))
    nav = app.pages["navigation"]
    nav_plan = nav.plan
    app.simulator.engage()
    try:
        assert app.simulating
        assert isinstance(app.recorder, SimRecorder)
        assert app.vehicle_status() == (True, "on")
        assert "simulator" in str(app.flight_dir)
        assert app.dvl_recorder() is app.simulator.dvl
        assert app.ugps_capture() is app.simulator.ugps
        assert isinstance(nav.collector, SimNavCollector)
        assert app._versions["blueos"] == "1.4.6"
        assert [t.name for s in app._plan().sites for t in s.transects][:1] == ["T1"]
        text, tone, button = app.simulator.banner("DVL")
        assert text and tone in ("fault", "green")
        _pump(app, 1.5)
    finally:
        app.simulator.disengage("test")
    assert not app.simulating
    assert (app.flight_dir, app.recorder) == before[:2]
    assert dict(app._versions) == before[2]
    assert not isinstance(nav.collector, SimNavCollector)
    assert nav.plan is nav_plan


def test_live_monitoring_is_coloured_by_level_and_drawn_at_display_scale(app):
    from rov_flight_ops.gui import monitorpage as MP
    app.simulator.engage()
    try:
        app.deiconify()
        app.geometry("1400x900")
        app.nav.select("Monitoring")
        page = app.pages["monitor"]
        page.group_pick.set("CPU")
        page._pick_group("CPU")
        _pump(app, 2.0)
        page._redraw()
        cv = page.canvas
        s = page._geom.get("s", 1.0)
        n = len(L.GROUPS["CPU"])
        assert int(cv.cget("height")) == int((MP.ROW_H * n + MP.TIME_H) * s)
        fills = {cv.itemcget(i, "fill") for i in cv.find_all()
                 if cv.type(i) in ("line", "rectangle")}
        green = page._apply_appearance_mode(B.LEVEL_COLORS[B.NORMAL])
        assert green in fills
        texts = [cv.itemcget(i, "text") for i in cv.find_all() if cv.type(i) == "text"]
        assert "now" in texts and any("normal" in t or "unusual" in t or "bad" in t
                                      for t in texts)
        assert any("simulated" in t for t in texts)
    finally:
        app.simulator.disengage("test")
        app.withdraw()


def test_the_mounting_offset_line_clears_the_beam_tiles(app):
    app.simulator.engage()
    try:
        app.deiconify()
        app.geometry("1300x900")
        app.nav.select("DVL")
        _pump(app, 2.0)
        page = app.pages["dvl"]
        page._draw_beams()
        cv = page.beams
        note = [i for i in cv.find_all() if cv.type(i) == "text"
                and "mounting offset" in cv.itemcget(i, "text")]
        assert note
        nx0, ny0, nx1, ny1 = cv.bbox(note[0])
        for i in cv.find_all():
            if cv.type(i) != "rectangle":
                continue
            x0, y0, x1, y1 = cv.coords(i)
            overlaps = nx0 < x1 and x0 < nx1 and ny0 < y1 and y0 < ny1
            assert not overlaps, "the note runs into a tile"
    finally:
        app.simulator.disengage("test")
        app.withdraw()


# --------------------------------------------------------------------------
#  what the Monitoring board, Navigation numbers and Parameters tab read
# --------------------------------------------------------------------------


def test_a_leak_reaches_the_snapshot_and_the_gain_and_forward_speed_are_read():
    from rov_flight_ops.sim.nav import SimNavCollector
    w = W.SimWorld()
    s0 = w.schedule.windows(S.NAV, "leak_detected", w.t() + 3000)[0]
    w.t0_mono -= (s0 + 2) - w.t()                     # jump into the leak
    col = SimNavCollector(w)
    col.start("test")
    try:
        assert wait_for(lambda: col.snapshot().leak.value is True, 15), \
            "the leak text was not read"
        s = col.snapshot()
        assert "Leak" in s.statustext.value
        assert wait_for(lambda: col.snapshot().pilot_gain.number() is not None, 10)
        assert col.snapshot().pilot_gain.number() == pytest.approx(0.5)
        fwd, spd = col.snapshot().forward.held(), col.snapshot().speed.held()
        assert fwd is not None and spd is not None and abs(fwd) <= spd + 0.05
    finally:
        col.stop(2.0)


def test_the_simulated_vehicle_differs_from_its_reference_on_purpose():
    from rov_flight_ops import paramcheck as PC
    from rov_flight_ops import paramfile
    from rov_flight_ops.sim import nav as SN
    cmp = PC.compare(SN.vehicle_params(),
                     paramfile.read(paramfile.reference_path("Nereo")).params)
    assert {r.name for r in cmp.key_differences} == {"JS_GAIN_DEFAULT",
                                                     "JS_LIGHTS_STEPS"}
    assert {r.name for r in cmp.others} == {"PSC_VELXY_P", "NTF_LED_BRIGHT"}
    assert {"STAT_RUNTIME", "COMPASS_DEC", "SR0_POSITION"} <= set(cmp.ignored)


def test_the_new_panels_fill_from_the_simulated_vehicle(app):
    app.simulator.engage()
    try:
        app.deiconify()
        app.geometry("1500x950")
        app.nav.select("Monitoring")
        _pump(app, 4.0)
        board = app.pages["monitor"].board
        assert {"link", "navigator", "dvl", "leak", "rx", "u1"} <= set(board._rows)
        assert board.headline.cget("text")
        assert " V " in board.power_label.cget("text") + " "
        assert not board.power_label.cget("text").startswith("—")

        app.nav.select("Parameters")
        _pump(app, 3.0)
        params = app.pages["parameters"]
        params._update()
        assert params.comparison.headline()[0] == "DIFFERS"
        ok, why = params._writable(params._snapshot())
        assert not ok and "armed" in why            # the simulated ROV is flying
        assert params.key_all.cget("state") == "disabled"

        app.nav.select("Navigation")
        _pump(app, 2.0)
        value, _sub = app.pages["navigation"].numbers.cells["depth"]
        assert value.cget("text").endswith(" m")

        app.nav.select("DVL")
        _pump(app, 3.0)
        dvl = app.pages["dvl"]
        # The four beams and the echo start level with each other.
        beams_card = dvl.beams.master.master
        echo_card = dvl.acoustic.master.master
        assert abs(beams_card.winfo_rooty() - echo_card.winfo_rooty()) <= 2
        assert dvl.charts.winfo_width() > dvl.beams.winfo_width() * 1.6,             "the live charts take the full width"
    finally:
        app.simulator.disengage("test")
        app.withdraw()
