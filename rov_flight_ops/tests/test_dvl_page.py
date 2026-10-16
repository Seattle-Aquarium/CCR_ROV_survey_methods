"""
The DVL tab: where it sits, and that it draws what the capture holds.

The page is fed a live state built by hand rather than a running capture, so
these need a display but no DVL and no network. What a running capture puts
into that state is `test_dvl_capture`'s business.

A canvas can be laid out to the pixel and draw nothing at all -- this program
has been caught by exactly that -- so these count what was drawn, not just
that nothing raised.
"""

from __future__ import annotations

import time

import pytest

from rov_flight_ops.dvl import protocol as P
from rov_flight_ops.dvl.cadence import Gap
from rov_flight_ops.dvl.live import ChartPoint
from rov_flight_ops.dvl.recorder import DvlRecorder


def test_the_dvl_tab_is_third_and_keyed(app):
    assert app.nav.sections[2] == "DVL"
    assert app.nav.key_of("DVL") == "dvl"
    assert "dvl" in app.pages


@pytest.fixture
def fed(app, monkeypatch):
    """The DVL page, its recorder's live state filled in, no capture running."""
    monkeypatch.setattr(DvlRecorder, "ensure_running", lambda self, folder: None)
    page = app.pages["dvl"]
    rec = app.dvl_recorder()
    # A capture another test started would write into the same live state.
    assert rec.stop().wait(15)
    live = rec.live
    now = time.time()
    velocity = {**P.flatten_velocity({
        "type": "velocity", "time": 100.0, "vx": 0.25, "vy": -0.01, "vz": 0.0,
        "fom": 0.002, "altitude": 1.04, "velocity_valid": True, "status": 0,
        "time_of_validity": 1, "time_of_transmission": 2,
        "transducers": [{"id": i, "velocity": 0.2, "distance": 1.1 + i / 100,
                         "rssi": -35.0 - i, "nsd": -95.0, "beam_valid": i != 2}
                        for i in range(4)]}),
        "d_tov_ms": 100.0, "tx_minus_tov_ms": 2.5, "rx_minus_tx_ms": 1.0}
    live.update(velocity=velocity, address="192.168.2.95",
                address_source="typed on the DVL tab", tcp_connected=True,
                tcp_connections=1, tcp_port=16171, ws_connected=True,
                capturing=False, median_ms=100.0,
                cadence={"gaps": {"dvl_quiet": 1, "missing_reports": 2,
                                  "delivery_stall": 0, "clock_step": 0},
                         "gap_ms": {"dvl_quiet": 1200.0, "missing_reports": 400.0},
                         "missing_reports_estimate": 6, "invalid_reports": 9,
                         "invalid_spans": 1, "invalid_ms": 900.0},
                ws_velocity={"run_config": 1},
                echo={"x_scale": 0.01, "y_scale": 1, "t": now,
                      "data": [[-80 + (k == 100) * 50] * 4 for k in range(1024)]},
                spectrum={"x_offset": 968.75, "x_scale": 0.48828125, "y_scale": 1,
                          "t": now, "data": [[-126 + (60 < k < 68) * 30] * 4
                                             for k in range(128)]})
    live.merge("status", {"reachable": True, "temperature_c": 31.5, "cpu_load": 0.3,
                          "cfg_periodic_cycling_enabled": False,
                          "cfg_mounting_rotation_offset": 0, "json_clients": 2,
                          "ext_reachable": True, "ext_status": "Running",
                          "ext_should_send": "POSITION_ESTIMATE",
                          "ext_rangefinder": True})
    live.merge("mavlink", {"255/0/DISTANCE_SENSOR": {"counter": 1200, "rate": 9.6,
                                                     "frequency": 9.6, "error": ""}})
    for k in range(120):
        t = now - 60 + k * 0.5
        live.add_point(ChartPoint(t=t, altitude=1.0 + 0.01 * (k % 7),
                                  distance=(1.1, 1.12, 1.08, 1.1),
                                  rssi=(-35, -36, -37, -38), nsd=(-95,) * 4,
                                  valid=not (40 <= k < 48), fom=0.002,
                                  d_tov_ms=100.0, d_rx_ms=101.0))
        live.add_temperature(t, 31.0 + k / 200)
    live.add_gap(Gap("missing_reports", now - 20, 400.0, 100.0, 401.0, 100.0, 3))
    live.add_event(now, "gap: missing_reports — ~3 report(s) made but not received")
    before = app.nav.current
    # Shown, as test_shell_and_logs does: a withdrawn window lays nothing out,
    # and a canvas one pixel wide draws nothing.
    app.deiconify()
    app.geometry("1300x900")
    app.nav.select("DVL")
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        app.update()
        time.sleep(0.02)
    try:
        yield page
    finally:
        if before:
            app.nav.select(before)
        app.withdraw()
        live.reset_streams()


def test_the_beams_are_drawn_where_they_point(fed, app):
    page = fed
    page._draw_beams()
    texts = [page.beams.itemcget(i, "text") for i in page.beams.find_all()
             if page.beams.type(i) == "text"]
    joined = " | ".join(texts)
    for beam in P.BEAMS:
        assert f"T{beam.number} · id {beam.id} · {P.beam_position(beam, 0)}" in joined
    assert "1.04 m" in joined and "VALID" in joined
    assert "1.120 m" in joined                         # id 2's distance


def test_the_charts_draw_lines_bands_and_gaps(fed, app):
    page = fed
    page._draw_charts()
    kinds = [page.charts.type(i) for i in page.charts.find_all()]
    assert kinds.count("line") > 15, "series, separators and gap marks"
    assert kinds.count("rectangle") >= 6, "the invalid stretch, under each strip"


def test_the_echo_and_spectrum_are_drawn(fed, app):
    page = fed
    page._draw_acoustic()
    lines = [i for i in page.acoustic.find_all() if page.acoustic.type(i) == "line"]
    assert len(lines) >= 8, "four beams in each panel"


def test_the_text_says_what_reached_the_autopilot(fed, app):
    page = fed
    page._update_text()
    assert "9.6/s" in page.delivery["ext_dist"].cget("text")
    assert "Running" in page.delivery["ext"].cget("text")
    assert page.delivery["clients"].cget("text").startswith("2")
    gaps = page.health["gaps"].cget("text")
    assert "missing reports 2" in gaps and "~6" in gaps
    assert "100 ms" in page.health["cadence"].cget("text")
    assert "Live view only" in page.capture_label.cget("text")
