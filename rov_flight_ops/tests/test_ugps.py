"""
The UGPS capture: reading the G2's per-receiver acoustics, logging them, and
judging whether one receiver stands out -- against a simulated G2.

Waits are on conditions, never fixed sleeps, so a slow CI runner only makes
these slower, not wrong.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

from rov_flight_ops.ugps import capture as CAP
from rov_flight_ops.ugps import g2, health
from rov_flight_ops.ugps.simulator import G2Simulator

#: What Water Linked's public demo G2 answered on 15 October 2026.
DEMO = {"position_valid": True,
        "receiver_distance": [20.03, 18.43, 15.52, 17.99],
        "receiver_nsd": [-101.73, -86.73, -61.73, -51.73],
        "receiver_rssi": [-45.0, -55.0, -55.0, -65.0],
        "receiver_valid": [1, 1, 1, 0], "std": 0.375, "x": -12.5, "y": -11.65, "z": 7.5}

#: The same solution as the G2's own page receives it (from a .wdat log).
WEBSOCKET = {"xPos": -6.05, "yPos": 12.97, "zPos": 2.04, "std": 3.58,
             "pos": [-6.05, 12.97, 2.04], "dt": 1.0, "position_valid": True,
             "receivers": [{"id": i, "recv_distance": 9.1 + i, "recv_rssi": -60.0 - i,
                            "recv_nsd_db": -100.0, "recv_valid": 1.0} for i in range(4)]}


def wait_for(cond, timeout=15.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except (AssertionError, OSError, ValueError):
            pass                    # not written yet, or written half-way
        time.sleep(step)
    return False


# --------------------------------------------------------------------------
#  parsing
# --------------------------------------------------------------------------

def test_the_rest_shape_gives_each_receiver_its_columns():
    row = g2.parse_acoustic(DEMO)
    assert row["x"] == -12.5 and row["position_valid"] == 1 and row["n_receivers"] == 4
    assert row["r0_rssi"] == -45.0 and row["r0_nsd"] == -101.73
    assert row["r0_snr_db"] == pytest.approx(56.73)
    assert row["r3_valid"] == 0 and row["r3_snr_db"] == pytest.approx(-13.27)
    assert row["extra_json"] == ""


def test_the_websocket_shape_reads_the_same():
    row = g2.parse_acoustic(WEBSOCKET)
    assert row["x"] == -6.05 and row["r2_rssi"] == -62.0 and row["r2_snr_db"] == 38.0
    assert json.loads(row["extra_json"]) == {"dt": 1.0}


def test_fields_nobody_named_are_kept_not_dropped():
    row = g2.parse_acoustic({**DEMO, "new_thing": [1, 2]})
    assert json.loads(row["extra_json"]) == {"new_thing": [1, 2]}
    five = {**DEMO, **{k: v + [v[0]] for k, v in DEMO.items()
                       if k.startswith("receiver_")}}
    row = g2.parse_acoustic(five)
    assert row["n_receivers"] == 5
    assert json.loads(row["extra_json"])["receivers_beyond_columns"][0]["rssi"] == -45.0


def test_status_groups_are_summarised():
    row = g2.parse_status({"battery": 80, "gps": 3, "imu": 2}, {"board": 54, "water": 13},
                          [{"status_group": "receivers", "status": "warning",
                            "message": "receiver 2 weak", "error_ids": ["rx2"]},
                           {"status_group": "gnss", "status": "ok", "message": ""}])
    assert row["groups_not_ok"] == "receivers"
    assert row["status_groups"] == "receivers=warning; gnss=ok"
    assert "receiver 2 weak [rx2]" in row["status_messages"]
    assert row["board_temp_c"] == 54 and row["gps"] == 3


# --------------------------------------------------------------------------
#  judging the receivers
# --------------------------------------------------------------------------

def history(n=60, snr=(40, 40, 40, 40), nsd=(-100, -100, -100, -100),
            valid=(1, 1, 1, 1), now=1000.0):
    out = []
    for k in range(n):
        out.append((now - n * 0.5 + k * 0.5, 1,
                    tuple((valid[i], snr[i] + (k % 3 - 1) * 0.5, nsd[i], nsd[i] + snr[i])
                          for i in range(4))))
    return out


def test_four_alike_receivers_are_not_flagged():
    h = health.judge(history(), now=1000.0)
    assert not h.flagged and h.verdict.startswith("all 4 alike")


def test_a_weak_receiver_is_flagged():
    h = health.judge(history(snr=(40, 40, 28, 40)), now=1000.0)
    assert h.flagged and h.receivers[2].flags == ["SNR 12 dB below the others"]
    assert not any(r.flags for r in h.receivers if r.index != 2)


def test_a_noisy_receiver_is_flagged():
    h = health.judge(history(nsd=(-100, -90, -100, -100)), now=1000.0)
    assert "noise 10 dB above the others" in h.receivers[1].flags


def test_a_receiver_that_keeps_dropping_out_is_flagged():
    hist = history()
    hist = [(t, v, tuple((0 if (i == 3 and k % 3) else r[0], *r[1:]) for i, r in enumerate(rx)))
            for k, (t, v, rx) in enumerate(hist)]
    h = health.judge(hist, now=1000.0)
    assert any(f.startswith("valid in") for f in h.receivers[3].flags)


def test_too_little_history_is_not_judged():
    h = health.judge(history(n=4), now=1000.0)
    assert not h.flagged and "only 4" in h.verdict
    h = health.judge(history(now=500.0), now=1000.0)          # all of it stale
    assert "no acoustic solutions" in h.verdict


# --------------------------------------------------------------------------
#  the capture, against a simulated G2
# --------------------------------------------------------------------------

@pytest.fixture()
def sim():
    s = G2Simulator().start()
    yield s
    s.stop()


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(CAP, "STATUS_PERIOD_S", 0.5)
    monkeypatch.setattr(CAP, "RETRY_S", 0.2)
    monkeypatch.setattr(CAP, "RECORD_EVERY_S", 1.0)


def rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def one(folder: Path, suffix: str) -> Path:
    found = list(folder.glob(f"ugps_*{suffix}"))
    assert len(found) == 1, found
    return found[0]


def test_a_capture_logs_every_receiver_and_only_reads(sim, fast, tmp_path):
    sim.weak = {2: 10.0}
    cap = CAP.UgpsCapture(sim.host, flight_id=lambda: "2026-10-15_103918", timeout=0.5)
    cap.use_folder(tmp_path)
    cap.ensure_running()
    try:
        assert wait_for(lambda: cap.snapshot()["receivers"][2]["fresh_solutions"] >= 12)
        assert wait_for(lambda: one(tmp_path, "_status.csv").stat().st_size > 400)
    finally:
        assert cap.shutdown()
    ac = rows(one(tmp_path, "_acoustic.csv"))
    assert {r["source"] for r in ac} == {"filtered", "raw"}
    assert all(r["flight_id"] == "2026-10-15_103918" for r in ac)
    fresh = [r for r in ac if r["source"] == "filtered" and r["fresh"] == "1"]
    assert len(fresh) >= 12
    r = fresh[-1]
    assert float(r["r2_snr_db"]) == pytest.approx(float(r["r2_rssi"]) - float(r["r2_nsd"]))
    rec = json.loads(one(tmp_path, ".json").read_text())
    assert rec["state"] == "closed" and rec["schema"] == CAP.CAPTURE_SCHEMA
    snr = {k: v["snr_db_mean_when_valid"] for k, v in rec["receivers"].items()}
    assert snr["r0"] - snr["r2"] > 7
    assert rec["about"]["version"] == "3.3.1"
    assert rows(one(tmp_path, "_status.csv"))[0]["status_groups"].startswith("receivers=ok")
    assert sum(1 for _ in open(one(tmp_path, "_http.jsonl"))) > len(ac)
    assert {m for m, _ in sim.requests} == {"GET"}
    verdict = health.judge(cap.snapshot()["history"], time.monotonic())
    assert verdict.flagged and verdict.receivers[2].flags


def test_a_g2_that_stops_answering_is_noted_and_picked_up_again(sim, fast, tmp_path):
    cap = CAP.UgpsCapture(sim.host, timeout=0.4)
    cap.use_folder(tmp_path)
    cap.ensure_running()
    try:
        assert wait_for(lambda: cap.snapshot()["connected"])
        sim.down = True
        assert wait_for(lambda: not cap.snapshot()["connected"])
        sim.down = False
        assert wait_for(lambda: cap.snapshot()["connected"])
        before = cap.snapshot()["receivers"][0]["fresh_solutions"]
        assert wait_for(lambda: cap.snapshot()["receivers"][0]["fresh_solutions"] > before)
    finally:
        assert cap.shutdown()
    events = one(tmp_path, "_events.txt").read_text(encoding="utf-8")
    assert "G2 not answering" in events and "G2 answering" in events


def test_a_path_the_firmware_does_not_serve_is_asked_rarely(sim, fast, tmp_path):
    sim.raw_404 = True
    cap = CAP.UgpsCapture(sim.host, timeout=0.5)
    cap.use_folder(tmp_path)
    cap.ensure_running()

    def filtered():
        return [p for _m, p in sim.requests if p.endswith("/acoustic/filtered")]

    try:
        # Waited on the count being checked, not on a proxy for it: a slow
        # CI runner once reached six fresh solutions on eleven requests.
        assert wait_for(lambda: len(filtered()) >= 12)
        assert wait_for(lambda: cap.snapshot()["receivers"][0]["fresh_solutions"] >= 6)
    finally:
        assert cap.shutdown()
    raw = [p for _m, p in sim.requests if p.endswith("/acoustic/raw")]
    assert len(raw) == 1 and len(filtered()) >= 12
    assert "not served" in one(tmp_path, "_events.txt").read_text(encoding="utf-8")


def test_no_fix_writes_no_rows_but_is_counted(sim, fast, tmp_path):
    sim.no_fix = True
    cap = CAP.UgpsCapture(sim.host, timeout=0.5)
    cap.use_folder(tmp_path)
    cap.ensure_running()
    try:
        assert wait_for(lambda: sum(1 for _m, p in sim.requests if "acoustic" in p) >= 10)
    finally:
        assert cap.shutdown()
    assert rows(one(tmp_path, "_acoustic.csv")) == []
    rec = json.loads(one(tmp_path, ".json").read_text())
    assert rec["polls"]["/api/v1/position/acoustic/filtered"]["http_errors"] >= 4


def test_choosing_another_folder_closes_one_capture_and_opens_the_next(sim, fast, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    cap = CAP.UgpsCapture(sim.host, timeout=0.5)
    cap.ensure_running()
    try:
        cap.use_folder(a)
        assert wait_for(lambda: any(a.glob("ugps_*_acoustic.csv"))
                        and one(a, "_acoustic.csv").stat().st_size > 600)
        cap.use_folder(b)
        assert wait_for(lambda: any(b.glob("ugps_*_acoustic.csv")))
        assert wait_for(lambda: json.loads(one(a, ".json").read_text())["state"] == "closed")
        cap.use_folder(None)
        assert wait_for(lambda: json.loads(one(b, ".json").read_text())["state"] == "closed")
    finally:
        assert cap.shutdown()
