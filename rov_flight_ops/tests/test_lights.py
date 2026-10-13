"""
The lights: tab 4, the Navigation row, the Light Monitor reader, the light
logs on the Pi, and the light values in an mcap.

No vehicle and no extension: the reader is given a fake fetch, the pages are
fed a state document of the shape the extension serves
(``light_monitor.state/1``), and the mcap is built here. A canvas can be laid
out to the pixel and draw nothing, so the drawing is counted, not just run.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

from rov_flight_ops import mcap_extract as mx
from rov_flight_ops import pifiles as PF
from rov_flight_ops.lights.client import LightsClient, Status


def state_doc(*, l4_answering=True, rail=None) -> dict:
    """A Light Monitor state document: lights 1-3 at 100 %, light 4 as asked."""
    lights = []
    for lid, name, short in ((1, "Front left", "FL"), (2, "Rear left", "RL"),
                             (3, "Rear right", "RR"), (4, "Front right", "FR")):
        ok = lid != 4 or l4_answering
        lights.append({
            "id": lid, "name": name, "short": short,
            "state": "on" if ok else "not_answering",
            "state_text": "Answering" if ok else "Not answering (timeout)",
            "answering": ok, "last_answer_age_s": 0.4 if ok else 6.0,
            "level_pct": 100.0 if ok else None, "commanded_pct": 100.0,
            "power_cycles": 30 if lid == 4 else 112, "restarts_this_session": 1 if lid == 4 else 0,
            "runtime_h": 92.9, "max_temp_c": 87.0, "temp1_c": 0.0, "temp2_c": 24.0,
            "temp3_c": 21.1, "humidity_pct": 0.0, "flags": 0, "flags_hex": "0x0000",
            "info": {"serial": f"12-049-762{lid}", "firmware": "1.1.0"},
            "expected_w": 98.0 if ok else None, "expected_a": 4.08 if ok else None,
        })
    return {
        "schema": "light_monitor.state/1", "now": time.time(),
        "extension": {"version": "0.1.0", "mode": "auto", "can_release": True},
        "link": {"source": "data", "connected": True, "note": "telemetry port",
                 "lines": 1234, "lines_per_s": 9.8, "lost_lines": 0,
                 "last_line_age_s": 0.1, "legacy_firmware": False},
        "controller": {"fw": "lutris-lights 2.0.0-telemetry", "board": "qtpy",
                       "machine": "Adafruit QT Py RP2040 with RP2040",
                       "micropython": "1.27.0", "uptime_s": 600.0, "restarts": 0,
                       "pwm_us": 1896, "signal": True, "commanded_pct": 100,
                       "stats": {"asked": 900, "answered": 897, "failed": 3}},
        "lights": lights,
        "power": {"rail": rail, "vehicle": {"v": 12.9, "a": 41.2, "w": 531.5, "age_s": 0.3},
                  "ardusub": {"lights_pct": 100.0, "pilot_gain_pct": 30.0},
                  "lights_expected_w": 294.0 if not l4_answering else 392.0,
                  "lights_expected_a": 16.3, "vehicle_warn_v": 10.5,
                  "converter_dropout_v": 8.6, "full_w_per_light": 98.0, "monitors": []},
        "mavlink": {"enabled": True, "publishing": True, "system": 1, "component": 191,
                    "sent": 420, "errors": 0},
        "recording": {"dir": "/data", "file": "lights_20261013_200000.jsonl"},
        "events": [{"t": time.time() - 5, "lvl": "warn",
                    "text": "Light 4 (front right) stopped answering (timeout)"}],
    }


class FakeClient:
    """Stands in for the app's LightsClient: no thread, no network."""

    def __init__(self, state):
        self.state = state
        self.base = "http://192.168.2.2:9420"

    def snapshot(self):
        return self.state, Status(url=self.base, connected=self.state is not None)


# --------------------------------------------------------------------------
#  the reader
# --------------------------------------------------------------------------


def test_the_reader_keeps_the_latest_state():
    c = LightsClient("rov", fetch=lambda url: state_doc())
    c.poll_state()
    state, st = c.snapshot()
    assert st.connected and state["lights"][3]["name"] == "Front right"


def test_a_failed_read_is_a_reason_not_an_exception():
    def boom(url):
        raise OSError("connection refused")
    c = LightsClient("rov", fetch=boom)
    c.poll_state()
    state, st = c.snapshot()
    assert state is None and not st.connected and "refused" in st.error


def test_the_recording_is_copied_topside_with_gaps_marked(tmp_path):
    pages = [{"records": [{"id": 1, "x": "session"}, {"id": 2, "line": "a"}], "newest": 2},
             {"records": [{"id": 9, "line": "b"}], "newest": 9}]
    c = LightsClient("rov", fetch=lambda url: pages.pop(0))
    c.use_folder(tmp_path / "logs" / "lights")
    c.poll_records()
    c.poll_records()
    c.shutdown()
    copy = next((tmp_path / "logs" / "lights").glob("lights_topside_*.jsonl"))
    rows = [json.loads(x) for x in copy.read_text().splitlines()]
    assert [r.get("x") for r in rows] == ["topside", "session", None, "gap", None]
    assert rows[3]["after"] == 2 and rows[3]["before"] == 9


def test_an_extension_restart_starts_the_ids_again(tmp_path):
    pages = [{"records": [{"id": 50, "line": "a"}], "newest": 50},
             {"records": [], "newest": 3}]
    c = LightsClient("rov", fetch=lambda url: pages.pop(0))
    c.use_folder(tmp_path)
    c.poll_records()
    c.poll_records()
    assert c._last_id == 0 and "restarted" in c.snapshot()[1].notes[-1]
    c.shutdown()


# --------------------------------------------------------------------------
#  tab 4 and the Navigation row
# --------------------------------------------------------------------------


def test_lights_is_the_fourth_tab(app):
    assert app.nav.sections[3] == "Lights"
    assert app.nav.key_of("Lights") == "lights"
    assert "lights" in app.pages


@pytest.fixture
def shown(app, monkeypatch):
    """The Lights tab on screen, fed a state with light 4 not answering."""
    fake = FakeClient(state_doc(l4_answering=False))
    monkeypatch.setattr(app, "lights_client", lambda: fake)
    before = app.nav.current
    app.deiconify()
    app.geometry("1400x950")
    app.nav.select("Lights")
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        app.update()
        time.sleep(0.02)
    try:
        yield app.pages["lights"], fake
    finally:
        if before:
            app.nav.select(before)
        app.update()


def test_the_lights_tab_shows_each_light_and_the_fault(shown):
    page, _fake = shown
    page._update()
    one, four = page.cards[1], page.cards[4]
    assert one.level.cget("text") == "100%"
    assert one.values["expected"].cget("text").startswith("≈ 98 W")
    assert one.values["temps"].cget("text") == "24.0 · 21.1 °C"
    assert four.state.cget("text").startswith("Not answering")
    assert "(+1 this session)" in four.values["power_ons"].cget("text")
    assert page.power["veh_v"].cget("text") == "12.90 V"
    assert page.power["rail"].cget("text") == "no power monitor fitted"


def test_the_drawing_draws_the_vehicle_and_four_lights(shown):
    page, _fake = shown
    page._update()
    page.update()
    cv = page.schematic
    assert cv.winfo_width() > 150
    items = cv.find_all()
    texts = [cv.itemcget(i, "text") for i in items if cv.type(i) == "text"]
    assert {"Front left", "Rear left", "Rear right", "Front right"} <= set(texts)
    assert len([i for i in items if cv.type(i) == "arc"]) == 3   # light 4 has none
    assert "Not answering" in texts


def test_the_navigation_row_is_compact_and_says_the_same(app):
    nav = app.pages["navigation"]
    strip = nav.lights_strip
    strip.update_from(state_doc(l4_answering=False), Status(url="u", connected=True))
    one = strip.cells["1"][1].cget("text")
    four = strip.cells["4"][1].cget("text")
    assert one.startswith("100%") and "≈98 W" in one and "≈4.1 A" in one
    assert four == "Not answering"
    assert strip.cells["power"][1].cget("text") == "12.9 V · 41 A · 532 W"
    strip.update_from(None, Status(url="http://rov:9420", error="refused"))
    assert "not answering at http://rov:9420" in strip.message.cget("text")
    strip.update_from(state_doc(), Status(url="u", connected=True))
    assert all(f.winfo_manager() == "grid" for f in strip._frames)


# --------------------------------------------------------------------------
#  the light logs on the Pi
# --------------------------------------------------------------------------


def test_light_logs_are_a_download_type_beside_the_topside_copy():
    assert PF.BY_KEY["lights"].label == "light logs"
    assert PF.BY_KEY["lights"].dest == "logs/lights/pi"
    for name in ("lights_20261013_200000.jsonl", "lights_20261013_200000_1hz.csv"):
        assert PF.LIGHTS_NAME.match(name)
    assert not PF.LIGHTS_NAME.match("lights_topside_20261013_200000.jsonl")


def test_a_light_log_spans_from_its_name_to_its_last_write():
    inv = PF.Inventory()
    f = PF.PiFile("lights", PF.LIGHTS_FB_PATH + "/lights_20261013_200000.jsonl",
                  "lights_20261013_200000.jsonl", size=10, modified=1_760_390_000.0)
    inv.files["lights"] = [f]
    PF.fill_spans(inv)
    assert f.start == PF.stamp_time("lights_20261013_200000") and f.end == f.modified


# --------------------------------------------------------------------------
#  the light values in an mcap
# --------------------------------------------------------------------------


def test_named_values_from_the_light_monitor_survive_extraction(tmp_path):
    """The autopilot's and the extension's NAMED_VALUE_FLOATs both come out;
    a measured type still comes from the autopilot alone."""
    from mcap.writer import Writer
    src = tmp_path / "flight.mcap"
    t0 = 1_791_000_000
    with open(src, "wb") as f:
        w = Writer(f)
        w.start()
        mav = w.register_schema("mavlink.Message", "jsonschema", b"{}")
        ap_nvf = w.register_channel("mavlink/1/1/NAMED_VALUE_FLOAT", "json", mav)
        lm_nvf = w.register_channel("mavlink/1/191/NAMED_VALUE_FLOAT", "json", mav)
        lm_txt = w.register_channel("mavlink/1/191/STATUSTEXT", "json", mav)
        hud = w.register_channel("mavlink/1/1/VFR_HUD", "json", mav)
        hud255 = w.register_channel("mavlink/255/0/VFR_HUD", "json", mav)
        for i in range(5):
            t = int((t0 + i) * 1e9)
            w.add_message(ap_nvf, t, json.dumps({"message": {"name": "Lights1",
                                                              "value": 1.0}}).encode(), t)
            w.add_message(lm_nvf, t, json.dumps({"message": {"name": "LT4_LVL",
                                                              "value": 100.0}}).encode(), t)
            w.add_message(hud, t, json.dumps({"message": {"alt": 1.0}}).encode(), t)
            w.add_message(hud255, t, json.dumps({"message": {"alt": 99.0}}).encode(), t)
        t = int((t0 + 2) * 1e9)
        w.add_message(lm_txt, t, json.dumps({"message": {
            "text": "Lights: light 4 FR not answering"}}).encode(), t)
        w.finish()
    res = mx.extract([src], tmp_path / "cache", force=True)
    with open(res.telemetry_csv, newline="") as fh:
        rows = list(csv.reader(fh))
    keys = {r[1] for r in rows[1:]}
    assert {"NVF.Lights1", "NVF.LT4_LVL"} <= keys
    assert any(r[1] == "STATUSTEXT" and "light 4" in r[3] for r in rows[1:])
    alts = {r[2] for r in rows[1:] if r[1] == "VFR_HUD.alt"}
    assert alts == {"1.0"}                       # 255/0's copy still not read
