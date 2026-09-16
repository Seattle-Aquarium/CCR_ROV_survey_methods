"""Reading a recording the vehicle never closed.

The 8/31 Magnolia flight lost power mid-dive. BlueOS had written 4.73 GB of
perfectly good chunks and then stopped: no DATA_END, no summary, no footer, and
a final chunk header still carrying its placeholder length of
0xFFFF_FFFF_FFFF_FFFF. The mcap library refused the whole file
(``RecordLengthLimitExceeded``), so three transects reported no telemetry and
the crew's TC-25 times looked wrong when they were in fact correct.

These tests build small mcaps and break them the same way, so the recovery path
is exercised without a gigabyte of flight data.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_imagery_processing import mcap_extract as mx  # noqa: E402

MAVLINK_SCHEMA = "mavlink.Message"
T0 = 1_788_195_000              # 2026-08-31, roughly


# --------------------------------------------------------------------------
#  building a small recording
# --------------------------------------------------------------------------


def _cdr_video(ts: float, payload: bytes) -> bytes:
    """A foxglove.CompressedVideo as BlueOS encodes it, so
    ``parse_compressed_video`` has something real to read back."""
    sec, nsec = int(ts), int((ts % 1) * 1e9)
    frame_id = b"cam"
    out = bytearray(b"\x00\x01\x00\x00")                    # encapsulation
    out += struct.pack("<iI", sec, nsec)
    out += struct.pack("<I", len(frame_id)) + frame_id
    while len(out) % 4:
        out += b"\x00"
    out += struct.pack("<I", len(payload)) + payload
    return bytes(out)


def write_mcap(path: Path, *, seconds: int = 20, video: bool = True) -> None:
    """A miniature dive: two sysids for one message type, plus a video track."""
    from mcap.writer import Writer

    with open(path, "wb") as f:
        w = Writer(f)
        w.start()
        mav = w.register_schema(MAVLINK_SCHEMA, "jsonschema", b"{}")
        vid = w.register_schema(mx.VIDEO_SCHEMA, "ros2msg", b"")
        hud = w.register_channel("mavlink/1/1/VFR_HUD", "json", mav)
        pos = w.register_channel("mavlink/1/1/GLOBAL_POSITION_INT", "json", mav)
        # the same message type under a second system id; the autopilot (1/1)
        # must win even though this one is not obviously worse
        pos255 = w.register_channel("mavlink/255/0/GLOBAL_POSITION_INT", "json", mav)
        vch = w.register_channel("video/forward", "cdr", vid) if video else None

        for i in range(seconds):
            t = int((T0 + i) * 1e9)
            w.add_message(hud, t, json.dumps(
                {"message": {"alt": 1.0 * i, "groundspeed": 0.4,
                             "heading": 90}}).encode(), t)
            w.add_message(pos, t, json.dumps(
                {"message": {"relative_alt": -1000 * i,
                             "lat": 47, "lon": -122}}).encode(), t)
            w.add_message(pos255, t, json.dumps(
                {"message": {"relative_alt": 0}}).encode(), t)
            if vch is not None:
                w.add_message(vch, t, _cdr_video(T0 + i, b"\x00\x00\x01\x65frame"), t)
        w.finish()


def truncate(src: Path, dst: Path, *, cut: int) -> None:
    """Lose the last `cut` bytes -- power off mid-write."""
    dst.write_bytes(src.read_bytes()[:-cut])


def stub_last_length(src: Path, dst: Path) -> int:
    """Reproduce the real failure exactly: a final chunk header whose length was
    never backfilled. Returns the offset of that header."""
    data = bytearray(src.read_bytes())
    end = mx.scan_health(src).good_end
    pos = last = len(mx.MCAP_MAGIC)
    while pos + 9 <= end:
        ln = int.from_bytes(data[pos + 1:pos + 9], "little")
        last, pos = pos, pos + 9 + ln
    data[last] = mx._OP_CHUNK
    data[last + 1:last + 9] = struct.pack("<Q", 0xFFFF_FFFF_FFFF_FFFF)
    dst.write_bytes(bytes(data[:last + 9 + 40]))    # a stub of a chunk, then nothing
    return last


@pytest.fixture(scope="module")
def good(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("mcap") / "recorder_20260831_165829.mcap"
    write_mcap(p)
    return p


# --------------------------------------------------------------------------
#  scan_health
# --------------------------------------------------------------------------


def test_a_healthy_file_reads_as_complete(good):
    h = mx.scan_health(good)
    assert h.complete and not h.recoverable
    assert h.good_end == h.size == good.stat().st_size
    assert h.lost_bytes == 0
    assert h.chunks >= 1
    assert h.first_time and h.last_time and h.last_time > h.first_time


def test_a_truncated_file_reads_as_recoverable(good, tmp_path):
    bad = tmp_path / "cut.mcap"
    truncate(good, bad, cut=200)
    h = mx.scan_health(bad)
    assert not h.complete
    assert h.recoverable, "chunks survived, so it should be worth reading"
    assert h.good_end < h.size
    assert 0 < h.lost_bytes


def test_a_stubbed_final_chunk_stops_the_scan_cleanly(good, tmp_path):
    """The real 8/31 shape: a placeholder length of 0xFFFF... . The scan must
    stop *before* that header rather than trusting the number."""
    bad = tmp_path / "stub.mcap"
    at = stub_last_length(good, bad)
    h = mx.scan_health(bad)
    assert h.good_end == at, "should stop at the header it cannot trust"
    assert h.recoverable and not h.complete


def test_a_file_with_no_chunks_is_not_called_recoverable(tmp_path):
    """Nothing to recover is not the same as recoverable-but-lossy."""
    p = tmp_path / "empty.mcap"
    p.write_bytes(mx.MCAP_MAGIC)
    h = mx.scan_health(p)
    assert not h.complete and not h.recoverable


def test_a_file_that_is_not_an_mcap_at_all(tmp_path):
    p = tmp_path / "junk.mcap"
    p.write_bytes(b"this is not an mcap" * 100)
    h = mx.scan_health(p)
    assert h.good_end == 0 and not h.recoverable


# --------------------------------------------------------------------------
#  reading through the repair
# --------------------------------------------------------------------------


def test_the_repaired_stream_reads_and_leaves_the_file_alone(good, tmp_path):
    from mcap.reader import NonSeekingReader

    bad = tmp_path / "cut.mcap"
    truncate(good, bad, cut=200)
    before = hashlib.sha256(bad.read_bytes()).hexdigest()
    size_before = bad.stat().st_size

    h = mx.scan_health(bad)
    with mx.open_repaired(bad, h) as f:
        topics = {ch.topic for _s, ch, _m in NonSeekingReader(f).iter_messages()}

    assert "mavlink/1/1/VFR_HUD" in topics
    assert hashlib.sha256(bad.read_bytes()).hexdigest() == before, \
        "recovery must never write to the recording"
    assert bad.stat().st_size == size_before


def test_streaming_selection_agrees_with_the_indexed_one(good, tmp_path):
    """Recovery must not quietly pick a different channel than a healthy read
    would -- that would change whose numbers end up in the CSV."""
    from mcap.reader import make_reader

    with open(good, "rb") as f:
        want_chosen, want_video = mx.select_channels(make_reader(f))

    bad = tmp_path / "cut.mcap"
    truncate(good, bad, cut=200)
    got_chosen, got_video = mx.select_channels_streaming(bad, mx.scan_health(bad))

    assert got_chosen == want_chosen
    assert got_video == want_video
    assert got_chosen["GLOBAL_POSITION_INT"] == "mavlink/1/1/GLOBAL_POSITION_INT", \
        "the autopilot must outrank system 255"


# --------------------------------------------------------------------------
#  the whole pipeline
# --------------------------------------------------------------------------


def test_probe_reports_a_truncated_file_instead_of_dropping_it(good, tmp_path):
    """The bug the crew hit: the file was skipped entirely, so three transects
    looked like they fell outside every recording."""
    bad = tmp_path / "recorder_20260831_165829.mcap"
    truncate(good, bad, cut=200)
    (info,) = mx.probe_mcaps([bad])
    assert info.error is None, info.error
    assert info.truncated and info.health is not None
    assert info.start and info.end and info.end > info.start


def test_extract_reads_a_truncated_recording(good, tmp_path):
    bad = tmp_path / "recorder_20260831_165829.mcap"
    truncate(good, bad, cut=200)
    before = hashlib.sha256(bad.read_bytes()).hexdigest()

    res = mx.extract([bad], tmp_path / "cache", force=True)

    assert res.telemetry_rows > 0, res.warnings
    assert res.video.frames > 0, res.warnings
    assert res.t_start and res.t_end and res.t_end > res.t_start
    assert any("never closed" in w for w in res.warnings), \
        "the operator has to be told the recording is incomplete"
    assert hashlib.sha256(bad.read_bytes()).hexdigest() == before

    fields = {ln.split(",")[1] for ln in
              res.telemetry_csv.read_text().splitlines()[1:]}
    assert "GLOBAL_POSITION_INT.relative_alt" in fields, \
        "the field the depth profile is drawn from"


def test_a_truncated_read_matches_an_intact_one(good, tmp_path):
    """Cut only the tail, and everything before it should come out the same."""
    intact = mx.extract([good], tmp_path / "a", force=True)
    bad = tmp_path / "cut.mcap"
    truncate(good, bad, cut=200)
    recovered = mx.extract([bad], tmp_path / "b", force=True)

    assert recovered.telemetry_rows > 0.5 * intact.telemetry_rows
    assert recovered.telemetry_rows <= intact.telemetry_rows
    assert recovered.video.frames <= intact.video.frames
    assert abs(recovered.t_start - intact.t_start) < 1.0


# --------------------------------------------------------------------------
#  more than one camera
# --------------------------------------------------------------------------

#: Real SPS NAL units from the 14 September 2026 OTS recordings.
SPS_FORWARD = bytes.fromhex("674d4029965403c0113f2a")               # 1920x1080
SPS_COCKPIT = bytes.fromhex(
    "6742c01fda03c045fbc05a83030352800000030080000004478c1950")    # 960x540


def _frame(sps: bytes, tag: bytes) -> bytes:
    """An access unit as BlueOS records it: AUD, SPS, then a slice."""
    return (b"\x00\x00\x00\x01\x09\xf0" + b"\x00\x00\x00\x01" + sps
            + b"\x00\x00\x01\x65" + tag)


def write_two_camera_mcap(path: Path, *, start: float, seconds: int,
                          forward: bool = True, cockpit: bool = True) -> None:
    """The OTS shape: the forward camera at 30 fps, the Madrona cockpit view
    at 3 fps, interleaved in one recording."""
    from mcap.writer import Writer

    with open(path, "wb") as f:
        w = Writer(f)
        w.start()
        mav = w.register_schema(MAVLINK_SCHEMA, "jsonschema", b"{}")
        vid = w.register_schema(mx.VIDEO_SCHEMA, "ros2msg", b"")
        hud = w.register_channel("mavlink/1/1/VFR_HUD", "json", mav)
        fwd = w.register_channel("video/Streamdevvideo2/stream", "cdr", vid)
        cpt = w.register_channel("video/madronacockpit/stream", "cdr", vid)
        for k in range(seconds * 30):
            ts = start + k / 30
            t = int(ts * 1e9)
            if k % 30 == 0:
                w.add_message(hud, t, json.dumps(
                    {"message": {"alt": 1.0, "heading": 90}}).encode(), t)
            if forward:
                w.add_message(fwd, t, _cdr_video(
                    ts, _frame(SPS_FORWARD, b"fwd%05d" % k)), t)
            if cockpit and k % 10 == 5:
                w.add_message(cpt, t, _cdr_video(
                    ts, _frame(SPS_COCKPIT, b"cpt%05d" % k)), t)
        w.finish()


def test_sps_nal_reads_both_real_cameras():
    assert mx.sps_resolution(_frame(SPS_FORWARD, b"x")) == (1920, 1080)
    assert mx.sps_resolution(_frame(SPS_COCKPIT, b"x")) == (960, 540)


def test_only_the_forward_camera_goes_into_the_rov_stream(tmp_path):
    """The OTS regression: the cockpit stream was spliced into the forward
    camera's bitstream, and the proxy built from the mixture ran 1,367 s for
    a 1,166 s window -- an inset minutes away from the GoPro."""
    src = tmp_path / "recorder_20260914_163530.mcap"
    write_two_camera_mcap(src, start=T0, seconds=4)

    res = mx.extract([src], tmp_path / "cache", force=True)

    assert res.video_topic == "video/Streamdevvideo2/stream"
    assert res.video.frames == 4 * 30, "cockpit frames must not be counted"
    assert res.video.resolutions == {"1920x1080"}
    raw = res.h264_path.read_bytes()
    assert b"cpt" not in raw and SPS_COCKPIT not in raw
    assert raw.count(b"fwd") == 4 * 30
    ts = [float(r.split(",")[1]) for r in
          res.frames_csv.read_text().splitlines()[1:]]
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False)), \
        "one camera's timestamps only ever go forward"
    assert any("madronacockpit" in w and "ignored" in w for w in res.warnings)
    assert json.loads((tmp_path / "cache" / "extract.json").read_text())[
        "video_topic"] == "video/Streamdevvideo2/stream"


def test_the_camera_is_chosen_for_the_flight_not_per_recording(tmp_path):
    """A recording whose forward camera dropped out must contribute nothing
    to the ROV stream -- not its cockpit view in the forward camera's place."""
    a = tmp_path / "recorder_20260914_160000.mcap"
    b = tmp_path / "recorder_20260914_161000.mcap"
    write_two_camera_mcap(a, start=T0, seconds=4)
    write_two_camera_mcap(b, start=T0 + 600, seconds=4, forward=False)

    res = mx.extract([a, b], tmp_path / "cache", force=True)

    assert res.video_topic == "video/Streamdevvideo2/stream"
    assert res.video.frames == 4 * 30
    assert b"cpt" not in res.h264_path.read_bytes()
    assert any(b.name in w and "no video/Streamdevvideo2/stream" in w
               for w in res.warnings), res.warnings


def test_a_resolution_change_is_reported_not_just_the_first_sps(tmp_path):
    """Only the first SPS used to be read, so the warning for a camera that
    changed resolution part way through could never fire."""
    import csv as _csv

    from mcap.writer import Writer

    src = tmp_path / "recorder_20260914_170000.mcap"
    with open(src, "wb") as f:
        w = Writer(f)
        w.start()
        vid = w.register_schema(mx.VIDEO_SCHEMA, "ros2msg", b"")
        ch = w.register_channel("video/forward", "cdr", vid)
        for k in range(20):
            sps = SPS_FORWARD if k < 10 else SPS_COCKPIT
            t = int((T0 + k / 30) * 1e9)
            w.add_message(ch, t, _cdr_video(T0 + k / 30, _frame(sps, b"f")), t)
        w.finish()

    res = mx.extract([src], tmp_path / "cache", force=True)
    assert res.video.resolutions == {"1920x1080", "960x540"}
    assert any("resolution changes" in w for w in res.warnings)
    with open(res.frames_csv, newline="") as fh:
        assert len(list(_csv.DictReader(fh))) == 20


def test_choose_video_topic_prefers_the_busiest_and_is_repeatable():
    assert mx.choose_video_topic({}) is None
    assert mx.choose_video_topic(
        {"video/madronacockpit/stream": 5439,
         "video/Streamdevvideo2/stream": 101446}) == "video/Streamdevvideo2/stream"
    assert mx.choose_video_topic({"b": 10, "a": 10}) == "a"


def test_a_cache_from_before_one_camera_per_flight_is_rebuilt(tmp_path):
    """A schema-2 cache may hold two cameras in one stream, with nothing to
    say so. It must not be taken as a hit."""
    src = tmp_path / "recorder_20260914_163530.mcap"
    write_two_camera_mcap(src, start=T0, seconds=2)
    cache = tmp_path / "cache"
    mx.extract([src], cache, force=True)
    marker = cache / "extract.json"
    old = json.loads(marker.read_text())
    old["schema"] = 2
    marker.write_text(json.dumps(old))

    said: list[str] = []
    mx.extract([src], cache, progress=lambda f, m="": said.append(m))
    assert "telemetry cache hit" not in said
    assert json.loads(marker.read_text())["schema"] == mx.CACHE_SCHEMA


def test_a_zero_rangefinder_reading_is_dropped():
    """MAVLink's RANGEFINDER has no status field, so a lost bottom lock
    arrives as distance 0.0. The dataflash log, which does carry a status,
    shows these are NoData -- and 0.00 m stamped on a photo is a false
    measurement, not a missing one.
    """
    import csv
    import io

    def write(payload):
        buf = io.StringIO()
        rows = mx._write_telemetry(csv.writer(buf), "RANGEFINDER", 1.0,
                                   json.dumps({"message": payload}).encode())
        return rows, buf.getvalue()

    n, text = write({"distance": 0.0, "voltage": 0.0})
    assert n == 0 and text == "", text

    n, text = write({"distance": 0.87, "voltage": 0.0})
    assert n > 0 and "RANGEFINDER.distance" in text
