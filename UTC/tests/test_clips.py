"""Tests for cutting a short shareable clip out of one video.

The offsets here are *into a file*, not TC-25 clock times, and getting that
wrong would silently cut the wrong fifteen seconds — so the parser gets the
most attention.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import clips  # noqa: E402
from utc import ffmpeg_tools as ff  # noqa: E402


def _video(path: Path, seconds: int = 12, size: str = "320x180") -> None:
    exe = ff.find_ffmpeg()
    subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, capture_output=True)


# --------------------------------------------------------------------------
#  offsets
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,seconds", [
    ("6:40", 400.0),
    ("06:40", 400.0),
    ("0:06:40", 400.0),
    ("400", 400.0),
    ("90", 90.0),          # a bare number is seconds...
    ("1:30", 90.0),        # ...and 1:30 is the same thing said differently
    ("1:02:03", 3723.0),
    ("0", 0.0),
    ("12.5", 12.5),
])
def test_parse_offset(text, seconds):
    assert clips.parse_offset(text) == seconds


@pytest.mark.parametrize("bad", ["", "junk", "6:", ":40", "1:2:3:4", "-5", "6,40"])
def test_parse_offset_rejects_nonsense(bad):
    assert clips.parse_offset(bad) is None


def test_offset_round_trips():
    for s in (0, 59, 60, 400, 3599, 3600, 3723):
        assert clips.parse_offset(clips.format_offset(s)) == float(s)


def test_format_offset_only_shows_hours_when_there_are_some():
    assert clips.format_offset(400) == "6:40"
    assert clips.format_offset(3723) == "1:02:03"


# --------------------------------------------------------------------------
#  validation
# --------------------------------------------------------------------------


def _src(dur=600.0):
    return clips.SourceVideo(Path("x.mp4"), dur, 1920, 1080)


def test_validate_catches_a_backwards_span():
    assert clips.validate(_src(), 400, 300)
    assert clips.validate(_src(), 400, 400)


def test_validate_catches_a_span_past_the_end():
    errs = clips.validate(_src(dur=300), 400, 415)
    assert errs and "past the end" in errs[0]
    assert clips.validate(_src(dur=300), 290, 400)


def test_validate_accepts_a_sane_span():
    assert clips.validate(_src(dur=600), 400, 415) == []


# --------------------------------------------------------------------------
#  naming and location
# --------------------------------------------------------------------------


def test_clip_name_carries_the_span_and_format():
    fmt = clips.CLIP_FORMATS["social"]
    n = clips.clip_name(Path("T1_1080p.mp4"), 400, 415, fmt, label="lingcod")
    assert n == "lingcod_6m40s-6m55s_social.mp4"
    # without a label it falls back to the source stem
    assert clips.clip_name(Path("T1_1080p.mp4"), 400, 415, fmt).startswith("T1_1080p_")


def test_clip_name_sanitises_a_typed_label():
    fmt = clips.CLIP_FORMATS["1080p"]
    n = clips.clip_name(Path("a.mp4"), 0, 5, fmt, label="ling cod / big!")
    assert "/" not in n and " " not in n


def test_clips_dir_lands_in_videos_clips():
    root = Path("F")
    assert clips.clips_dir(root / "videos").parts[-2:] == ("videos", "clips")
    assert clips.clips_dir(root / "videos" / "composites").parts[-2:] == \
        ("videos", "clips")
    assert clips.clips_dir(root / "videos" / "transects" / "T1").parts[-2:] == \
        ("videos", "clips")


def test_gif_format_is_a_gif_everything_else_is_mp4():
    assert clips.CLIP_FORMATS["gif"].suffix == ".gif"
    for k, f in clips.CLIP_FORMATS.items():
        if k != "gif":
            assert f.suffix == ".mp4", k


# --------------------------------------------------------------------------
#  cutting
# --------------------------------------------------------------------------


def test_make_clip_cuts_the_requested_span_and_keeps_the_source():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src_path = td / "source.mp4"
        _video(src_path, seconds=12)
        before = hashlib.sha256(src_path.read_bytes()).hexdigest()
        src = clips.list_videos(src_path)[0]

        rep = clips.make_clip(src, 3.0, 8.0, td / "out", ["720p"])
        assert len(rep.written) == 1, rep.summary()
        info = ff.probe(rep.written[0])
        assert 4.5 <= (info.duration or 0) <= 5.6, info.duration
        assert hashlib.sha256(src_path.read_bytes()).hexdigest() == before, \
            "the source must never be modified"


def test_make_clip_writes_one_file_per_format():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "s.mp4", seconds=8)
        src = clips.list_videos(td / "s.mp4")[0]
        rep = clips.make_clip(src, 1.0, 4.0, td / "out", ["1080p", "720p"])
        assert len(rep.written) == 2, rep.summary()
        assert {p.suffix for p in rep.written} == {".mp4"}


def test_a_clip_is_not_upscaled_past_the_source():
    """Asking for 1080p from a 320x180 source should not blow it up."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "s.mp4", seconds=6, size="320x180")
        src = clips.list_videos(td / "s.mp4")[0]
        rep = clips.make_clip(src, 1.0, 3.0, td / "out", ["1080p"])
        info = ff.probe(rep.written[0])
        assert info.height == 180, f"upscaled to {info.height}"


def test_gif_output_is_a_real_gif():
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "s.mp4", seconds=5)
        src = clips.list_videos(td / "s.mp4")[0]
        rep = clips.make_clip(src, 0.5, 2.5, td / "out", ["gif"])
        assert len(rep.written) == 1, rep.summary()
        out = rep.written[0]
        assert out.suffix == ".gif"
        with Image.open(out) as im:
            assert im.format == "GIF"
            assert getattr(im, "n_frames", 1) > 1, "should be animated"


def test_make_clip_refuses_an_invalid_span_before_encoding():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "s.mp4", seconds=5)
        src = clips.list_videos(td / "s.mp4")[0]
        rep = clips.make_clip(src, 4.0, 2.0, td / "out", ["720p"])
        assert rep.errors and not rep.written
        assert not (td / "out").exists() or not any((td / "out").iterdir())


def test_existing_output_is_kept_not_silently_replaced():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "s.mp4", seconds=6)
        src = clips.list_videos(td / "s.mp4")[0]
        first = clips.make_clip(src, 1.0, 3.0, td / "out", ["720p"])
        stamp = first.written[0].stat().st_mtime_ns
        again = clips.make_clip(src, 1.0, 3.0, td / "out", ["720p"])
        assert again.written[0].stat().st_mtime_ns == stamp
        assert any("already existed" in w for w in again.warnings)


def test_list_videos_finds_files_and_ignores_other_types():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _video(td / "a.mp4", seconds=3)
        (td / "notes.txt").write_text("hi", encoding="utf-8")
        found = clips.list_videos(td)
        assert [v.path.name for v in found] == ["a.mp4"]
        assert found[0].width == 320 and found[0].duration > 2
