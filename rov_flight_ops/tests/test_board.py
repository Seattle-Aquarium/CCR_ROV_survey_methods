"""
The Monitoring tab's go / no-go board.

Each row is judged from readings built by hand, so these need no vehicle, no
network and no display -- except the last, which checks that the board, once
on screen, starts the readers it judges from.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import board as B  # noqa: E402
from rov_flight_ops.nav import model as M  # noqa: E402
from rov_flight_ops.nav.model import LinkState, NavSnapshot  # noqa: E402


def _live(**kw) -> NavSnapshot:
    return NavSnapshot(link=LinkState(mode="live", connected=True, host="rov"),
                       armed=M.good(True), mode=M.good("Surftrak"), **kw)


def _row(board: B.Board, key: str) -> B.Row:
    return next(r for r in board.rows if r.key == key)


def test_nothing_connected_is_no_verdict_not_a_go():
    b = B.judge(B.Inputs())
    assert b.verdict is None
    assert b.headline.startswith("NO VEHICLE")
    assert _row(b, "link").level is None
    assert b.volts is b.amps is b.watts is None


def test_every_component_the_operator_named_has_a_row():
    names = " | ".join(r.name for r in B.judge(B.Inputs()).rows)
    for part in ("Navigator", "DVL", "Depth sensor", "GNSS compass", "G2",
                 "receivers ×4", "U1", "Leak sensors"):
        assert part in names, part


def test_a_wet_leak_sensor_is_no_go_and_says_so():
    s = _live(leak=M.good(True), statustext=M.good("Leak Detected"))
    b = B.judge(B.Inputs(snapshot=s))
    assert _row(b, "leak").level == B.NOGO
    assert "Leak Detected" in _row(b, "leak").detail
    assert b.verdict == B.NOGO and "Leak sensors" in b.headline


def test_a_dry_vehicle_with_the_leak_failsafe_off_is_a_check():
    s = _live(leak=M.good(False))
    s.params = {"FS_LEAK_ENABLE": 0.0}
    assert _row(B.judge(B.Inputs(snapshot=s)), "leak").level == B.CHECK


def test_a_heartbeat_that_stopped_is_no_go():
    s = _live()
    s.armed = M.good(True, recv_mono=time.monotonic() - 30)
    assert _row(B.judge(B.Inputs(snapshot=s)), "navigator").level == B.NOGO


def test_power_shows_watts_between_battery_messages():
    """V × I goes stale between BATTERY_STATUS messages. Stale is not wrong:
    a blank watts cell next to live volts and amps read as a fault."""
    s = _live(voltage=M.good(15.8), current=M.good(11.7),
              watts=M.good(185.0).staled("no new BATTERY_STATUS"))
    b = B.judge(B.Inputs(snapshot=s))
    assert (b.volts, b.amps, b.watts) == (15.8, 11.7, 185.0)


def test_board_readers_start_once_the_board_is_on_screen(app, monkeypatch):
    """The board judges the Navigation collector, the DVL view and the G2.
    Opened on Monitoring, as the program is, it starts them itself."""
    from rov_flight_ops.gui.navpage import NavigationPage
    calls = []
    monkeypatch.setattr(NavigationPage, "ensure_live",
                        lambda self: calls.append("nav"))
    monkeypatch.setattr(type(app), "ugps_capture",
                        lambda self: calls.append("ugps"))
    rec = app.dvl_recorder()
    monkeypatch.setattr(type(rec), "ensure_running",
                        lambda self, folder: calls.append("dvl"))
    monkeypatch.setattr(app, "_dvl_view_started", False, raising=False)
    # The fixture's guard, lifted for this one test.
    type(app).ensure_vehicle_readers(app)
    type(app).ensure_vehicle_readers(app)
    assert calls.count("nav") == 2 and calls.count("ugps") == 2
    assert calls.count("dvl") == 1, "a DVL capture stopped by hand stays stopped"
