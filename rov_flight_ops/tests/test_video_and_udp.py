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


def test_a_held_drop_rate_is_bad_and_none_is_normal():
    def rows(values):
        return [{"elapsed_time_s": float(i), "udp_receive_errors_per_s": v}
                for i, v in enumerate(values)]
    assert set(B.levels("udp_receive_errors_per_s", rows([0.0] * 20))) == {B.NORMAL}
    held = B.levels("udp_receive_errors_per_s", rows([0.0] * 10 + [50.0] * 10))
    assert held[-1] == B.BAD
