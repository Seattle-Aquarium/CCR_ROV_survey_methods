"""What BlueOS was streaming, and what Windows dropped after it arrived.

Both added after the 4 November 2026 OTS test. Steady video reached the
laptop's adapter, Cockpit still could not show it, and two things nobody had
recorded would have told the cases apart: UDP datagrams Windows threw away
after the adapter counted them, and the list of streams BlueOS was sending.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import bands as B  # noqa: E402
from rov_flight_ops import blueos, flightfile  # noqa: E402
from rov_flight_ops import laptop as L  # noqa: E402

#: One stream as the camera manager lists it: a UVC camera sent to QGC's UDP
#: port and offered over RTSP.
STREAM = {
    "id": "a1",
    "running": True,
    "error": None,
    "video_and_stream": {
        "name": "UVC Camera",
        "stream_information": {
            "endpoints": ["udp://192.168.2.1:5600", "rtsp://192.168.2.2:8554/video_stream__dev_video2"],
            "configuration": {"type": "video", "encode": "H264", "width": 1920,
                              "height": 1080,
                              "frame_interval": {"numerator": 1, "denominator": 30}},
        },
        "video_source": {"Local": {"name": "UVC Camera (046d:0825)",
                                   "device_path": "/dev/video2"}},
    },
}


def _answer(url, body, ok=True):
    return blueos.Answer(url=url, ok=ok, status=200 if ok else None,
                         body=body, error="" if ok else "refused")


# ---- the stream list ------------------------------------------------------


def test_the_stream_list_is_summarised_and_kept_whole(monkeypatch):
    import json
    monkeypatch.setattr(blueos, "_get",
                        lambda url, **kw: _answer(url, json.dumps([STREAM, STREAM])))
    got = blueos.read_video_streams("192.168.2.2")
    assert got["read"] is True and got["count"] == 2
    s = got["streams"][0]
    assert (s["name"], s["encode"], s["width"], s["height"], s["fps"]) == (
        "UVC Camera", "H264", 1920, 1080, 30.0)
    assert s["endpoints"][0] == "udp://192.168.2.1:5600"
    assert s["source"] == "UVC Camera (046d:0825)"
    assert s["running"] is True
    assert got["raw"][1]["id"] == "a1"


def test_the_proxy_is_tried_when_the_port_does_not_answer(monkeypatch):
    import json
    seen = []

    def fake(url, **kw):
        seen.append(url)
        if ":6020" in url:
            return _answer(url, "", ok=False)
        return _answer(url, json.dumps([STREAM]))

    monkeypatch.setattr(blueos, "_get", fake)
    got = blueos.read_video_streams("192.168.2.2")
    assert got["read"] and got["source"].endswith("/mavlink-camera-manager/streams")
    assert len(seen) == 2


def test_no_answer_is_not_an_empty_stream_list(monkeypatch):
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _answer(url, "", ok=False))
    got = blueos.read_video_streams("192.168.2.2")
    assert got["read"] is False and got["count"] is None


def test_an_unfamiliar_shape_is_kept_rather_than_rejected(monkeypatch):
    import json
    monkeypatch.setattr(blueos, "_get",
                        lambda url, **kw: _answer(url, json.dumps([{"name": "x"}, 7])))
    got = blueos.read_video_streams("192.168.2.2")
    assert got["read"] and got["count"] == 2
    assert got["streams"][0]["name"] == "x" and got["streams"][1] == {}


class _Snap:
    def __init__(self, video_streams=None):
        self.parameters = {}
        self.versions = {}
        self.taken = 0.0
        self.parameters_from = ""
        if video_streams is not None:
            self.video_streams = video_streams


def _record(opening, closing):
    return flightfile.build(
        flight_id="2026-11-04_153520", started=0.0, ended=60.0,
        reason="disarmed", rows=60, computer="L342-D", host="192.168.2.2",
        interface="Network Bridge", opening=opening, closing=closing,
        brief_disarms=[], capabilities={})


def test_the_flight_record_carries_the_streams_at_both_ends():
    start = {"read": True, "count": 1, "streams": [{"name": "UVC Camera"}]}
    end = {"read": False, "count": None, "streams": []}
    record = _record(_Snap(start), _Snap(end))
    assert record["video_streams"]["at_start"]["count"] == 1
    assert record["video_streams"]["at_end"]["read"] is False


def test_a_snapshot_from_before_this_existed_records_no_streams():
    record = _record(_Snap(), _Snap())
    assert record["video_streams"] == {"at_start": None, "at_end": None}


# ---- UDP dropped inside Windows -------------------------------------------


def _rate(sampler, total, now):
    return L.Sampler._udp_error_rate(sampler, total, now)


def test_udp_errors_are_a_rate_from_the_running_total():
    s = SimpleNamespace(_last_udp_errors=None)
    assert _rate(s, 100.0, 10.0) is None          # first reading: nothing to compare
    assert _rate(s, 100.0, 11.0) == 0.0
    assert _rate(s, 140.0, 13.0) == 20.0          # 40 errors over 2 s


def test_a_reset_or_an_unreadable_counter_is_blank_not_negative():
    s = SimpleNamespace(_last_udp_errors=None)
    _rate(s, 500.0, 1.0)
    assert _rate(s, 3.0, 2.0) is None             # counter went backwards
    assert _rate(s, 9.0, 3.0) == 6.0              # and counts again from there
    assert _rate(s, None, 4.0) is None
    assert _rate(s, 12.0, 5.0) is None            # starts over after a gap


def test_the_columns_are_in_the_row_and_on_the_page():
    for c in ("udp_received_per_s", "udp_no_port_per_s", "udp_receive_errors_per_s"):
        assert c in L.COLUMNS and L.UNITS[c] == "datagrams/s"
    assert "udp_receive_errors_per_s" in L.GROUPS["Network"]


def test_the_pi_cpu_is_the_mean_of_its_cores(monkeypatch):
    import json
    body = [{"name": "cpu0", "usage": 90.0}, {"name": "cpu1", "usage": 70.0},
            {"name": "x"}]
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _answer(url, json.dumps(body)))
    assert blueos.read_cpu_usage("192.168.2.2") == 80.0
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _answer(url, "", ok=False))
    assert blueos.read_cpu_usage("192.168.2.2") is None


def test_throttling_now_is_true_false_or_unknown(monkeypatch):
    import json

    def with_occurring(occurring):
        body = {"raspberry": {"model": "Pi 4",
                              "events": {"list": [], "occurring": occurring}}}
        return lambda url, **kw: _answer(url, json.dumps(body))

    monkeypatch.setattr(blueos, "_get", with_occurring(["FrequencyCapping"]))
    assert blueos.read_throttling("h") is True
    monkeypatch.setattr(blueos, "_get", with_occurring([]))
    assert blueos.read_throttling("h") is False
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _answer(url, "", ok=False))
    assert blueos.read_throttling("h") is None


def test_stream_flags_say_count_udp_and_failures():
    streams = {"read": True, "streams": [
        {"endpoints": ["udp://192.168.2.1:5600"], "running": True},
        {"endpoints": ["rtsp://192.168.2.2:8554/x"], "running": False}]}
    assert blueos.stream_flags(streams) == (2, True, 1)
    assert blueos.stream_flags({"read": False}) == (None, None, None)


def test_a_mark_lands_in_the_next_row_only():
    import threading
    s = SimpleNamespace(_mark=None, _mark_lock=threading.Lock())
    L.Sampler.mark(s, "video froze\n")
    row = {}
    L.Sampler._mark_column(s, row)
    assert row["operator_mark"] == "video froze"
    L.Sampler._mark_column(s, row)
    assert row["operator_mark"] is None


def test_a_mark_is_written_beside_the_logs_between_flights(tmp_path):
    from rov_flight_ops import flightlog
    rec = flightlog.FlightRecorder(flight_dir=tmp_path)
    path = rec.mark("restarted BlueOS")
    assert path == tmp_path / "logs" / "operator_marks.txt"
    line = path.read_text(encoding="utf-8").splitlines()[-1]
    assert line.endswith("\t-\trestarted BlueOS")
    assert flightlog.FlightRecorder().mark("x") is None   # no folder, no file


def test_the_new_readings_are_columns_and_the_charted_ones_have_bands():
    for c in ("qgc_running", "qgc_cpu_pct", "pi_cpu_pct", "pi_disk_free_gb",
              "video_stream_count", "video_stream_udp", "video_stream_errors",
              "logger_cpu_pct", "logger_ram_mb", "operator_mark"):
        assert c in L.COLUMNS, c
    for c in ("pi_cpu_pct", "logger_cpu_pct"):
        assert B.bands_for(c), c


def test_a_held_drop_rate_is_bad_and_none_is_normal():
    def rows(values):
        return [{"elapsed_time_s": float(i), "udp_receive_errors_per_s": v}
                for i, v in enumerate(values)]
    assert set(B.levels("udp_receive_errors_per_s", rows([0.0] * 20))) == {B.NORMAL}
    held = B.levels("udp_receive_errors_per_s", rows([0.0] * 10 + [50.0] * 10))
    assert held[-1] == B.BAD
