"""Telling a broken file apart from a broken dive.

Both show up as "no depth data" on a transect, and the right response is
opposite in each case, so these tests build recordings with each fault and
check the diagnosis names the right one.

The 2026-09-01 case is the one to keep in mind: structurally perfect file,
57 minutes of video after the MAVLink router died, three transects with no
telemetry and nothing to recover.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_mcap_recovery import T0, _cdr_video, truncate  # noqa: E402

from utc import mcap_extract as mx  # noqa: E402
from utc import mcap_health as H  # noqa: E402


def write_flight(
    path: Path,
    *,
    seconds: int = 60,
    telemetry_until: int | None = None,
) -> None:
    """A recording with video throughout and telemetry up to a chosen second.

    `telemetry_until=None` means telemetry runs the whole way, which is what a
    healthy dive looks like.
    """
    from mcap.writer import Writer

    cut = seconds if telemetry_until is None else telemetry_until
    with open(path, "wb") as f:
        w = Writer(f)
        w.start()
        mav = w.register_schema("mavlink.Message", "jsonschema", b"{}")
        vid = w.register_schema(mx.VIDEO_SCHEMA, "ros2msg", b"")
        svc = w.register_schema("blueos.Log", "jsonschema", b"{}")
        pos = w.register_channel("mavlink/1/1/GLOBAL_POSITION_INT", "json", mav)
        hud = w.register_channel("mavlink/1/1/VFR_HUD", "json", mav)
        cam = w.register_channel("video/UDPStream0/stream", "cdr", vid)
        log = w.register_channel("services/cable-guy/log", "json", svc)

        for i in range(seconds):
            t = int((T0 + i) * 1e9)
            w.add_message(cam, t, _cdr_video(T0 + i, b"\x00\x00\x01\x65f"), t)
            w.add_message(log, t, json.dumps({"message": "tick"}).encode(), t)
            if i < cut:
                w.add_message(pos, t, json.dumps(
                    {"message": {"relative_alt": -1000 * i}}).encode(), t)
                w.add_message(hud, t, json.dumps(
                    {"message": {"alt": 1.0, "groundspeed": 0.3}}).encode(), t)
        w.finish()


@pytest.fixture(scope="module")
def healthy(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("h") / "recorder_20260901_161800.mcap"
    write_flight(p, seconds=60)
    return p


@pytest.fixture(scope="module")
def link_died(tmp_path_factory) -> Path:
    """Structurally perfect, but MAVLink stops a third of the way in."""
    p = tmp_path_factory.mktemp("h") / "recorder_20260901_163046.mcap"
    write_flight(p, seconds=60, telemetry_until=20)
    return p


# --------------------------------------------------------------------------
#  grouping
# --------------------------------------------------------------------------


def test_topics_are_grouped_by_what_losing_them_costs():
    assert H.topic_group("mavlink/1/1/VFR_HUD") == H.TELEMETRY
    assert H.topic_group("video/UDPStream0/stream") == H.VIDEO
    assert H.topic_group("x", mx.VIDEO_SCHEMA) == H.VIDEO
    assert H.topic_group("services/cable-guy/log") == H.SERVICES
    assert H.topic_group("extensions/logs/waterlinked.ugps") == H.SERVICES


def test_span_coverage_is_a_fraction_of_the_window():
    s = H.Span(first=100.0, last=200.0)
    assert s.covers(120.0, 180.0) == 1.0
    assert s.covers(150.0, 250.0) == pytest.approx(0.5)
    assert s.covers(300.0, 400.0) == 0.0
    assert H.Span().covers(0.0, 10.0) == 0.0


# --------------------------------------------------------------------------
#  quick scan
# --------------------------------------------------------------------------


def test_quick_scan_separates_ok_truncated_and_junk(healthy, tmp_path):
    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    junk = tmp_path / "junk.mcap"
    junk.write_bytes(b"not an mcap" * 50)

    a, b, c = H.quick_scan([healthy, cut, junk])
    assert (a.status, a.repairable) == ("ok", False)
    assert (b.status, b.repairable) == ("truncated", True)
    assert (c.status, c.repairable) == ("unreadable", False)
    assert "not an mcap" in (c.error or "")
    assert a.readable and b.readable and not c.readable


def test_quick_scan_is_structural_only(healthy):
    (rep,) = H.quick_scan([healthy])
    assert not rep.deep and rep.groups == {}


def test_a_missing_file_is_reported_not_raised(tmp_path):
    (rep,) = H.quick_scan([tmp_path / "nope.mcap"])
    assert rep.error and not rep.readable


# --------------------------------------------------------------------------
#  deep scan -- the 2026-09-01 signature
# --------------------------------------------------------------------------


def test_deep_scan_sees_telemetry_stop_while_video_continues(link_died):
    (rep,) = H.quick_scan([link_died])
    H.deep_scan(rep)
    assert rep.deep and rep.status == "ok", "the file itself is fine"
    tel, vid = rep.groups[H.TELEMETRY], rep.groups[H.VIDEO]
    assert tel.last < vid.last
    assert rep.telemetry_ended_early_by() == pytest.approx(39.0, abs=1.5)


def test_a_healthy_recording_reports_no_early_ending(healthy):
    (rep,) = H.quick_scan([healthy])
    H.deep_scan(rep)
    assert rep.telemetry_ended_early_by() == pytest.approx(0.0, abs=1.5)
    assert rep.groups[H.TELEMETRY].count > 0
    assert rep.groups[H.SERVICES].count > 0


def test_deep_scan_works_through_a_repair(healthy, tmp_path):
    """A truncated file must still be diagnosable -- that is the case where
    knowing what is inside matters most."""
    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    (rep,) = H.quick_scan([cut])
    H.deep_scan(rep)
    assert rep.truncated and rep.deep
    assert rep.groups[H.TELEMETRY].count > 0


# --------------------------------------------------------------------------
#  window verdicts
# --------------------------------------------------------------------------


def test_windows_after_the_link_died_are_called_out(link_died):
    (rep,) = H.quick_scan([link_died])
    H.deep_scan(rep)
    early = (T0 + 2, T0 + 15)           # inside the telemetry
    late = (T0 + 35, T0 + 55)           # video only
    good, bad = H.judge_windows([rep], [("T1", *early), ("T3", *late)])

    assert good.ok and "telemetry present" in good.explain()
    assert not bad.ok
    assert bad.telemetry == 0.0 and bad.video > 0.0
    assert "stopped reporting" in bad.explain(), bad.explain()
    assert bad.recordings == [rep.path.name]


def test_a_window_clipping_the_last_second_reads_as_no_telemetry(link_died):
    """2026-09-01's T2 began at the exact second MAVLink stopped.

    Reporting that as "1% covered" invites someone to go looking for the other
    99%. There is no other 99% -- the link was already gone.
    """
    (rep,) = H.quick_scan([link_died])
    H.deep_scan(rep)
    # Telemetry ends at T0+19; a ten-minute window starting one second
    # earlier catches exactly that second, as the real T2 did.
    (v,) = H.judge_windows([rep], [("T2", T0 + 18, T0 + 618)])
    assert 0.0 < v.telemetry < 0.01
    assert "stopped reporting" in v.explain(), v.explain()
    assert "%" not in v.explain()


def test_a_window_with_no_recording_at_all_says_so(healthy):
    (rep,) = H.quick_scan([healthy])
    H.deep_scan(rep)
    (v,) = H.judge_windows([rep], [("T9", T0 + 9000, T0 + 9100)])
    assert not v.ok and v.recordings == []
    assert "no recording covers" in v.explain()


def test_verdicts_need_a_deep_scan(healthy):
    """A structural scan knows the span but not what is inside it, and must
    not pretend otherwise."""
    (rep,) = H.quick_scan([healthy])
    (v,) = H.judge_windows([rep], [("T1", T0 + 2, T0 + 15)])
    assert not v.ok and v.recordings == []


# --------------------------------------------------------------------------
#  repair
# --------------------------------------------------------------------------


def test_repair_writes_a_valid_copy_and_never_touches_the_original(healthy, tmp_path):
    cut = tmp_path / "recorder_20260901_163046.mcap"
    truncate(healthy, cut, cut=200)
    before = hashlib.sha256(cut.read_bytes()).hexdigest()
    size_before = cut.stat().st_size

    out = H.repair_copy(H.quick_scan([cut])[0])

    assert out.name == "recorder_20260901_163046_repaired.mcap"
    assert out.parent == cut.parent
    assert H.scan_health(out).complete, "the copy must be a valid mcap"
    assert hashlib.sha256(cut.read_bytes()).hexdigest() == before
    assert cut.stat().st_size == size_before


def test_the_repaired_copy_opens_with_a_stock_reader(healthy, tmp_path):
    """The whole point: other tools (Foxglove) must accept it."""
    from mcap.reader import make_reader

    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    out = H.repair_copy(H.quick_scan([cut])[0])
    with open(out, "rb") as f:
        n = sum(1 for _ in make_reader(f).iter_messages())
    assert n > 0


def test_repair_refuses_a_healthy_file(healthy):
    with pytest.raises(ValueError):
        H.repair_copy(H.quick_scan([healthy])[0])


def test_repair_will_not_overwrite_an_existing_file(healthy, tmp_path):
    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    rep = H.quick_scan([cut])[0]
    first = H.repair_copy(rep)
    with pytest.raises(FileExistsError):
        H.repair_copy(rep, first)


def test_repair_refuses_to_write_over_the_original(healthy, tmp_path):
    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    with pytest.raises(ValueError):
        H.repair_copy(H.quick_scan([cut])[0], cut)


def test_an_interrupted_repair_leaves_no_finished_looking_file(healthy, tmp_path):
    class Stop:
        """Cancelled before the first block is written.

        These fixtures copy in a single read, so a later trigger would never
        be reached -- and a copy that short is not meaningfully cancellable.
        """

        def is_set(self):
            return True

    cut = tmp_path / "cut.mcap"
    truncate(healthy, cut, cut=200)
    from utc.ffmpeg_tools import CancelledError
    with pytest.raises(CancelledError):
        H.repair_copy(H.quick_scan([cut])[0], cancel=Stop())
    assert list(tmp_path.glob("*_repaired.mcap")) == []
    assert list(tmp_path.glob("*.part")) == []
