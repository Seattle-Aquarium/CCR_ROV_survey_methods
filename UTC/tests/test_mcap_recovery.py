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

from utc import mcap_extract as mx  # noqa: E402

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
