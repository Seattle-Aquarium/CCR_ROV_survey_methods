"""Compositing from per-transect trims rather than full-length footage.

This exists because of a specific, expensive failure. Trimming is an ffmpeg
stream copy, and a stream copy carries the *source* chapter's timecode track
unchanged — so all four trims from one GoPro recording reported the same start
time of 09:22:05. Resolving a transect against them by timecode matched every
one of them, and a 10.2-minute transect came out as a single 66.6-minute
composite containing all four transects, after nearly three hours of encoding.

Two independent defects had to line up, so both are pinned here: resolving
trims by name instead of timecode, and bounding each overlay input so a part
cannot outrun its own segment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import videoclip  # noqa: E402
from utc.survey import (  # noqa: E402
    Chapter,
    Site,
    SurveyPlan,
    Transect,
    resolve_from_trims,
)

#: The timecode every trim from one recording wrongly shares.
_SHARED_TC = 9 * 3600 + 22 * 60 + 5


def _site(*transects: Transect) -> Site:
    return Site("Jack_Block_Park", "Port_of_Seattle", "2026-09-02",
                list(transects))


def _trim(seconds: float, name: str = "x.mp4") -> Chapter:
    """A trim: real duration, but the source recording's timecode."""
    return Chapter(Path(name), seconds, 23.976, 3840, 2160, 180, _SHARED_TC)


PLAN_TRANSECTS = (
    Transect("T1", "09:25:23", "09:35:37"),      # 614 s
    Transect("T2", "09:41:15", "09:50:42"),      # 567 s
    Transect("T3", "09:57:56", "10:09:17"),      # 681 s
    Transect("T4", "10:33:04", "10:42:08"),      # 544 s
)


# --------------------------------------------------------------------------
#  resolving by name
# --------------------------------------------------------------------------


def test_each_trim_becomes_exactly_one_segment_of_its_own_transect():
    """The regression: T1 resolved against all four trims and concatenated
    them into one 3994 s file. Each trim is one transect, whole."""
    plan = SurveyPlan([_site(*PLAN_TRANSECTS)])
    trims = {"T1": _trim(614.4), "T2": _trim(567.5),
             "T3": _trim(681.5), "T4": _trim(544.4)}
    out = resolve_from_trims(plan, trims)

    assert [r.transect.name for r in out] == ["T1", "T2", "T3", "T4"]
    for r in out:
        assert len(r.segments) == 1, \
            f"{r.transect.name} got {len(r.segments)} segments"
        assert r.segments[0].in_s == 0.0
        assert r.complete
    total = sum(s.dur_s for r in out for s in r.segments)
    assert 2400 < total < 2420, f"{total:.0f}s -- the four transects, once each"


def test_the_shared_timecode_is_never_consulted():
    """Every trim claims the same start; pairing must not depend on it."""
    plan = SurveyPlan([_site(Transect("T3", "09:57:56", "10:09:17"))])
    out = resolve_from_trims(plan, {"T3": _trim(681.5)})
    assert len(out[0].segments) == 1
    # A wildly wrong timecode changes nothing, because it is not read.
    out2 = resolve_from_trims(
        plan, {"T3": Chapter(Path("x.mp4"), 681.5, 23.976, 3840, 2160, 180, 0)})
    assert out2[0].segments[0].dur_s == out[0].segments[0].dur_s


def test_a_transect_with_no_trim_is_reported_not_guessed():
    plan = SurveyPlan([_site(*PLAN_TRANSECTS)])
    out = resolve_from_trims(plan, {"T1": _trim(614.4), "T3": _trim(681.5)})
    missing = [r for r in out if not r.segments]
    assert [r.transect.name for r in missing] == ["T2", "T4"]
    assert all(any("no trimmed video" in w for w in r.warnings)
               for r in missing)


def test_a_short_trim_is_used_as_far_as_it_goes_and_flagged():
    """Footage that ran out is not the same as times being wrong."""
    plan = SurveyPlan([_site(Transect("T1", "09:25:23", "09:35:37"))])
    out = resolve_from_trims(plan, {"T1": _trim(400.0)})
    (r,) = out
    assert r.segments[0].dur_s == pytest.approx(400.0)
    assert not r.complete
    assert any("only the footage that exists" in w for w in r.warnings)


def test_epochs_come_from_the_plan_not_the_file():
    """The trim carries no usable clock, so the transect time is the truth.

    Read back in the *flight's* timezone, not the machine's. Bare
    `fromtimestamp` uses whatever zone the runner happens to sit in, so this
    passed in Seattle and failed in CI, which runs on UTC -- 09:25:23 PDT read
    back as 16:25:23. The product code was right; the assertion was parochial.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    plan = SurveyPlan([_site(Transect("T1", "09:25:23", "09:35:37"))])
    (r,) = resolve_from_trims(plan, {"T1": _trim(614.4)})
    local = dt.datetime.fromtimestamp(r.epoch_start,
                                      ZoneInfo(plan.timezone))
    assert local.strftime("%H:%M:%S") == "09:25:23"
    assert r.epoch_end - r.epoch_start == pytest.approx(614.0)


# --------------------------------------------------------------------------
#  finding them on disk
# --------------------------------------------------------------------------


def test_find_trims_keys_by_folder_name(tmp_path):
    for name in ("T1", "T2", "T10"):
        d = tmp_path / "videos" / "transects" / name
        d.mkdir(parents=True)
        (d / f"2026-09-02_Port_of_Seattle_Site_{name}_4K_source.mp4").write_bytes(b"x")
    found = videoclip.find_trims(tmp_path)
    assert set(found) == {"T1", "T2", "T10"}
    assert found["T10"].parent.name == "T10"


def test_find_trims_is_empty_without_the_folder(tmp_path):
    assert videoclip.find_trims(tmp_path) == {}
    (tmp_path / "videos").mkdir()
    assert videoclip.find_trims(tmp_path) == {}


def test_find_trims_prefers_the_source_cut(tmp_path):
    """A transect folder may also hold other renders; the untouched cut wins."""
    d = tmp_path / "videos" / "transects" / "T1"
    d.mkdir(parents=True)
    (d / "something_else.mp4").write_bytes(b"x")
    (d / "a_4K_source.mp4").write_bytes(b"x")
    assert videoclip.find_trims(tmp_path)["T1"].name == "a_4K_source.mp4"


# --------------------------------------------------------------------------
#  the second defect
# --------------------------------------------------------------------------


def test_overlay_inputs_are_bounded_to_the_segment():
    """Each PNG input needs its own -t. Without one the image demuxer reads
    every remaining frame in the sequence, so a part runs to the end of the
    flight: part00 came out 1617 s for a 614 s transect."""
    src = Path("utc/compose.py").read_text(encoding="utf-8")
    i = src.index('"-start_number", str(start_frame),')
    window = src[i:i + 240]
    assert '"-t", f"{segment.dur_s:.3f}"' in window, \
        "the PNG inputs must be time-limited to their segment"


def test_discovery_does_not_treat_trims_as_chapters():
    from utc import discovery
    assert "transects" in discovery._EXCLUDE_PARTS, \
        "trims must not be picked up as full-length footage"


# --------------------------------------------------------------------------
#  what the operator is told
# --------------------------------------------------------------------------


def test_the_scan_describes_trims_without_claiming_a_timecode(tmp_path):
    """Pointing the Video page at videos/transects/ reported "No video files
    found here", because the scan excluded trims as if they were our own
    output. They are a valid source, and for this flight the only one.
    """
    from utc.gui.videopage import VideoPage

    trims = {}
    for name in ("T2", "T10", "T1"):
        f = tmp_path / f"{name}_4K_source.mp4"
        f.write_bytes(b"x" * 2048)
        trims[name] = f

    text = VideoPage._describe_trims(trims)
    assert "3 per-transect trim(s)" in text
    assert "folder name, not by timecode" in text, \
        "must not imply the trim's timecode was used"
    # natural transect order, not lexicographic
    assert text.index("T1 ") < text.index("T2 ") < text.index("T10")
