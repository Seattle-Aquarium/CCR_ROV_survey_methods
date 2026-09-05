"""Two videos side by side.

The hard part is time, because the sources do not share a clock. An mcap and
an untouched GoPro chapter can both be placed on a time of day; a trim carries
its source chapter's timecode and a composite carries none, so both must be
addressed by offset. Getting that wrong would silently cut the wrong ninety
seconds, so the reading of a time and the refusal of an untrustworthy timecode
are what these tests pin down.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import ffmpeg_tools as ff  # noqa: E402
from utc import sidebyside as sbs  # noqa: E402


def _video(path: Path, seconds: int = 8, size: str = "320x180") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ff.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, capture_output=True)
    return path


def _side(label="L", *, w=1920, h=1080, dur=600.0, epoch=None, note="") -> sbs.Side:
    return sbs.Side(path=Path("x"), kind="video", playable=Path("x.mp4"),
                    width=w, height=h, duration=dur, fps=30.0, label=label,
                    epoch_at_zero=epoch, clock_note=note)


# --------------------------------------------------------------------------
#  reading a time
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("10:02:27", ("clock", 10 * 3600 + 2 * 60 + 27)),
    ("9:05:00", ("clock", 9 * 3600 + 5 * 60)),
    ("1:30", ("offset", 90.0)),
    ("90", ("offset", 90.0)),
    ("0", ("offset", 0.0)),
    ("2:00", ("offset", 120.0)),
])
def test_three_fields_is_a_clock_fewer_is_an_offset(text, want):
    """The split has to be unambiguous: a transect never starts twelve hours
    into a recording, and a clip offset is never written with an hours field."""
    assert sbs.parse_time(text) == want


@pytest.mark.parametrize("bad", ["", "   ", "junk", "-5", "1:xx", "::"])
def test_nonsense_is_refused_rather_than_guessed(bad):
    assert sbs.parse_time(bad) is None


def test_a_timecode_carrying_frames_is_still_a_clock():
    """Field notes are sometimes written straight off the camera as
    hh:mm:ss:ff. `parse_hhmmss` accepts that and ignores the frames, and this
    follows it rather than inventing a second rule."""
    assert sbs.parse_time("1:2:3:4") == ("clock", 3723.0)
    assert sbs.parse_time("10:02:27:12") == ("clock", 10 * 3600 + 2 * 60 + 27)


def test_a_clock_time_becomes_an_offset_into_the_source():
    """10:02:27 against a source whose first frame is 10:00:00 is 147 s in."""
    import datetime as dt
    zero = dt.datetime(2026, 8, 31, 10, 0, 0).timestamp()
    side = _side(epoch=zero)
    assert side.in_point("10:02:27") == pytest.approx(147.0)
    # an offset is taken as written, whatever clock the side has
    assert side.in_point("1:30") == pytest.approx(90.0)


def test_a_clock_time_is_refused_when_the_source_has_no_clock():
    """Better to say so than to seek to a time the file cannot mean."""
    side = _side(note="it is a generated file")
    with pytest.raises(sbs.SideBySideError) as ex:
        side.in_point("10:02:27")
    assert "no usable clock" in str(ex.value)
    assert "generated file" in str(ex.value), "should say why"
    # the same source still accepts an offset
    assert side.in_point("0:30") == pytest.approx(30.0)


# --------------------------------------------------------------------------
#  which timecodes may be believed
# --------------------------------------------------------------------------


def test_a_trim_and_a_composite_are_not_trusted_but_a_chapter_is():
    """A trim reports the timecode of the chapter it was cut from -- every
    trim of one recording claims the same start -- so believing it would put
    the cut minutes away from where it was asked for."""
    trim = Path("F/videos/transects/T1/2026-08-31_x_T1_4K_source.mp4")
    comp = Path("F/videos/composites/2026-08-31_x_T1_1080p.mp4")
    chapter = Path("F/videos/downward/GX010001.MP4")

    ok, why = sbs._is_trustworthy_timecode(trim)
    assert not ok and "trim" in why
    ok, why = sbs._is_trustworthy_timecode(comp)
    assert not ok and "generated" in why
    assert sbs._is_trustworthy_timecode(chapter)[0]


def test_two_rovs_on_one_day_get_separate_caches():
    """Lutris and Nereo share a flight folder. Keying the proxy on the flight
    would make one overwrite the other's."""
    root = Path("C:/cache")
    a = sbs._sbs_cache(root, [Path("F/logs/mcap_Lutris/a.mcap")])
    b = sbs._sbs_cache(root, [Path("F/logs/mcap_Nereo/a.mcap")])
    assert a != b
    # and the same set is stable across calls and orderings
    two = [Path("F/logs/m/a.mcap"), Path("F/logs/m/b.mcap")]
    assert sbs._sbs_cache(root, two) == sbs._sbs_cache(root, list(reversed(two)))


# --------------------------------------------------------------------------
#  what can be asked for
# --------------------------------------------------------------------------


def test_a_format_that_would_upscale_the_smaller_source_is_not_offered():
    """1080 blown up to 2160 beside real 2160 makes the ROV camera look like
    the blurry one, when the difference is entirely the scaler."""
    gopro, rov = _side("GoPro", h=2160), _side("ROV", h=1080)
    assert sbs.usable_formats(gopro, rov) == ["1080p", "720p"]
    assert sbs.usable_formats(gopro, _side(h=2160)) == ["4K", "1080p", "720p"]
    assert sbs.usable_formats(_side(h=480), _side(h=480)) == ["720p"]


def test_a_span_past_the_end_is_caught_before_encoding():
    short, long_ = _side("S", dur=100.0), _side("L", dur=600.0)
    errs = sbs.validate(short, long_, in_l=80.0, in_r=0.0, seconds=90.0)
    assert errs and "S:" in errs[0]
    assert sbs.validate(long_, long_, 0.0, 0.0, 90.0) == []
    assert sbs.validate(long_, long_, 0.0, 0.0, 0.0)


def test_the_two_sides_keep_independent_start_points():
    """Comparing Lutris T1 against Nereo T5 means two different absolute
    times deliberately aligned from their own starts."""
    import datetime as dt
    lut = _side("Lutris_T1", epoch=dt.datetime(2026, 8, 31, 9, 58, 29).timestamp())
    ner = _side("Nereo_T5", epoch=dt.datetime(2026, 8, 31, 12, 53, 10).timestamp())
    assert lut.in_point("10:02:27") == pytest.approx(238.0)
    assert ner.in_point("12:54:59") == pytest.approx(109.0)


def test_the_output_name_says_which_is_which():
    n = sbs.output_name(_side("Lutris T1"), _side("Nereo T5"),
                        sbs.SBS_FORMATS["1080p"])
    assert n == "Lutris-T1_vs_Nereo-T5_1080p.mp4"
    assert not any(c in n for c in '\\/:*?"<>|')


def test_output_lands_in_composites():
    assert sbs.output_dir(Path("F")).parts[-2:] == ("videos", "composites")


# --------------------------------------------------------------------------
#  building one
# --------------------------------------------------------------------------


def test_the_panes_end_up_the_same_height_and_the_frame_holds_both():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a = _video(td / "a.mp4", seconds=6, size="640x360")
        b = _video(td / "b.mp4", seconds=6, size="320x180")
        left = sbs.probe_side(a, label="A", cache_root=td / "c")
        right = sbs.probe_side(b, label="B", cache_root=td / "c")

        rep = sbs.make_side_by_side(left, right, 1.0, 1.0, 3.0,
                                    td / "out", "720p", labels=False)
        assert rep.ok, rep.summary()
        # 720 tall, and as wide as two 16:9 panes plus the divider
        assert rep.height == 720
        assert rep.width == 1280 + sbs.DIVIDER_PX + 1280
        assert 2.5 <= rep.seconds <= 3.6


def test_a_source_is_never_modified():
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a = _video(td / "a.mp4", seconds=5)
        b = _video(td / "b.mp4", seconds=5)
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)]
        left = sbs.probe_side(a, label="A", cache_root=td / "c")
        right = sbs.probe_side(b, label="B", cache_root=td / "c")
        sbs.make_side_by_side(left, right, 0.0, 0.0, 2.0, td / "out",
                              "720p", labels=False)
        after = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)]
        assert before == after


def test_an_existing_output_is_kept_not_silently_replaced():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a, b = _video(td / "a.mp4", 5), _video(td / "b.mp4", 5)
        left = sbs.probe_side(a, label="A", cache_root=td / "c")
        right = sbs.probe_side(b, label="B", cache_root=td / "c")
        first = sbs.make_side_by_side(left, right, 0.0, 0.0, 2.0, td / "o",
                                      "720p", labels=False)
        stamp = first.output.stat().st_mtime_ns
        again = sbs.make_side_by_side(left, right, 0.0, 0.0, 2.0, td / "o",
                                      "720p", labels=False)
        assert again.output.stat().st_mtime_ns == stamp
        assert any("already existed" in w for w in again.warnings)


def test_no_partial_file_is_left_behind_when_the_encode_fails():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a, b = _video(td / "a.mp4", 5), _video(td / "b.mp4", 5)
        left = sbs.probe_side(a, label="A", cache_root=td / "c")
        right = sbs.probe_side(b, label="B", cache_root=td / "c")
        left.playable = td / "missing.mp4"          # make ffmpeg fail
        rep = sbs.make_side_by_side(left, right, 0.0, 0.0, 2.0, td / "o",
                                    "720p", labels=False)
        assert not rep.ok and rep.errors
        assert not list((td / "o").glob("*.part.mp4"))
        assert not list((td / "o").glob("*.mp4"))


def test_labels_are_escaped_for_drawtext():
    """A site name with a colon in it would otherwise end the filter option
    and ffmpeg would reject the whole graph."""
    assert sbs._dt("Pier 62: north") == "Pier 62\\: north"
    assert "[" not in sbs._dt("a[b]").replace("\\[", "").replace("\\]", "")


def test_a_folder_with_neither_video_nor_mcap_says_so():
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty"
        empty.mkdir()
        with pytest.raises(sbs.SideBySideError) as ex:
            sbs.probe_side(empty, label="L", cache_root=Path(td) / "c")
        assert "neither a video nor any .mcap" in str(ex.value)


def test_a_stray_recording_from_another_day_does_not_move_the_window(monkeypatch):
    """A time of day says nothing about which day. The Nereo folder carries a
    recording from six weeks before the dive, and taking the earliest file's
    date put the window in July, where nothing matched."""
    import datetime as dt

    from utc import mcap_extract

    def at(y, mo, d, h, mi, s):
        return dt.datetime(y, mo, d, h, mi, s).timestamp()

    class _Info:
        def __init__(self, start, end):
            self.start, self.end = start, end

    infos = [
        _Info(at(2026, 7, 22, 13, 59, 39), at(2026, 7, 22, 14, 22, 24)),
        _Info(at(2026, 8, 31, 12, 37, 30), at(2026, 8, 31, 12, 40, 39)),
        _Info(at(2026, 8, 31, 12, 53, 10), at(2026, 8, 31, 13, 0, 31)),
    ]
    monkeypatch.setattr(mcap_extract, "probe_mcaps", lambda _p: infos)

    win = sbs.window_for([Path("a.mcap")], "12:54:59", 90.0)
    assert win is not None
    started = dt.datetime.fromtimestamp(win[0])
    assert started.date() == dt.date(2026, 8, 31), "should not land in July"
    assert started.strftime("%H:%M:%S") == "12:54:59"
    assert win[1] - win[0] == pytest.approx(90.0)


def test_an_offset_needs_no_window_at_all():
    """Only a clock time has to be placed on a date."""
    assert sbs.window_for([Path("a.mcap")], "1:30", 90.0) is None
    assert sbs.window_for([Path("a.mcap")], "", 90.0) is None
