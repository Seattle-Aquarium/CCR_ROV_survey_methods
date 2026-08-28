"""Tests for trimming the original 4K down to the transects, and for the
dive-profile preview.

The trim is a stream copy, so the properties worth pinning are that the source
is never modified, that a transect spanning two chapters is joined rather than
truncated, and that the "footage is missing" warning fires only when footage is
actually missing.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import depthplot, videoclip  # noqa: E402
from utc import ffmpeg_tools as ff
from utc.survey import (  # noqa: E402
    Site,
    SurveyPlan,
    Transect,
    format_hhmmss,
    resolve_plan,
)
from utc.telemetry import Series, TelemetryStore  # noqa: E402

PDT = timezone(timedelta(hours=-7))


def _make_video(path: Path, seconds: int, timecode: str) -> None:
    """A tiny clip carrying a timecode track, so it can be placed on TC-25."""
    exe = ff.find_ffmpeg()
    subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=30:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
         "-pix_fmt", "yuv420p", "-timecode", timecode, str(path)],
        check=True, capture_output=True,
    )


def _plan(transects) -> SurveyPlan:
    return SurveyPlan(sites=[Site(name="Site", project="proj",
                                  date="2026-08-25", transects=transects)])


# --------------------------------------------------------------------------
#  trimming
# --------------------------------------------------------------------------


def test_trim_produces_a_clip_and_leaves_the_source_alone():
    from utc.pipeline import describe_chapters
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "GX010001.MP4"
        _make_video(src, 30, "12:00:00:00")
        before = hashlib.sha256(src.read_bytes()).hexdigest()

        ch = describe_chapters([src])
        assert ch[0].tc_start_s is not None, "test needs a readable timecode"
        plan = _plan([Transect("T1", "12:00:05", "12:00:15")])
        res = [r for r in resolve_plan(plan, ch) if r.segments]
        assert res, "the transect should land inside the clip"

        rep = videoclip.trim_flight(td / "flight", res, td / "scratch")
        assert len(rep.written) == 1, rep.summary()
        out = rep.written[0]
        assert out.is_file() and out.stat().st_size > 0
        info = ff.probe(out)
        assert 9.0 <= (info.duration or 0) <= 13.0, info.duration

        assert hashlib.sha256(src.read_bytes()).hexdigest() == before, \
            "the source footage must never be modified"


def test_a_transect_spanning_two_chapters_is_joined():
    from utc.pipeline import describe_chapters
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        a, b = td / "GX010001.MP4", td / "GX020001.MP4"
        _make_video(a, 20, "12:00:00:00")
        _make_video(b, 20, "12:00:20:00")
        ch = describe_chapters([a, b])
        plan = _plan([Transect("T1", "12:00:15", "12:00:25")])
        res = [r for r in resolve_plan(plan, ch) if r.segments]
        assert len(res[0].segments) == 2, "should need both chapters"

        rep = videoclip.trim_flight(td / "flight", res, td / "scratch")
        assert len(rep.written) == 1, rep.summary()
        assert rep.clips[0].parts == 2
        assert not rep.warnings, rep.warnings


def test_no_warning_when_a_transect_is_fully_covered():
    """A warning that fires at 100% teaches people to ignore warnings."""
    from utc.pipeline import describe_chapters
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "GX010001.MP4"
        _make_video(src, 30, "12:00:00:00")
        ch = describe_chapters([src])
        plan = _plan([Transect("T1", "12:00:05", "12:00:15")])
        res = [r for r in resolve_plan(plan, ch) if r.segments]
        rep = videoclip.trim_flight(td / "flight", res, td / "scratch")
        assert rep.warnings == [], rep.warnings


def test_warning_when_footage_really_is_missing():
    from utc.pipeline import describe_chapters
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "GX010001.MP4"
        _make_video(src, 20, "12:00:00:00")
        ch = describe_chapters([src])
        plan = _plan([Transect("T1", "12:00:15", "12:01:15")])   # runs off the end
        res = [r for r in resolve_plan(plan, ch) if r.segments]
        rep = videoclip.trim_flight(td / "flight", res, td / "scratch")
        assert any("no footage" in w for w in rep.warnings), rep.warnings


def test_a_transect_with_no_footage_is_skipped_not_failed():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        from utc.survey import ResolvedTransect
        r = ResolvedTransect(site=Site("S", "p", "2026-08-25", []),
                             transect=Transect("T9", "12:00:00", "12:01:00"),
                             segments=[], epoch_start=0.0, epoch_end=60.0,
                             covered_s=0.0, requested_s=60.0)
        out = videoclip.trim_transect(r, td / "x.mp4", td / "s")
        assert out.skipped and out.output is None and out.error is None


def test_rerun_skips_an_existing_clip_unless_forced():
    from utc.pipeline import describe_chapters
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "GX010001.MP4"
        _make_video(src, 20, "12:00:00:00")
        ch = describe_chapters([src])
        plan = _plan([Transect("T1", "12:00:05", "12:00:12")])
        res = [r for r in resolve_plan(plan, ch) if r.segments]
        first = videoclip.trim_flight(td / "f", res, td / "s")
        again = videoclip.trim_flight(td / "f", res, td / "s")
        assert first.clips[0].skipped is None
        assert again.clips[0].skipped == "already present"


def test_clip_dir_mirrors_the_photo_side():
    d = videoclip.clip_dir(Path("F"), "T3")
    assert d.parts[-3:] == ("videos", "transects", "T3")


# --------------------------------------------------------------------------
#  the dive profile
# --------------------------------------------------------------------------


def _store(depths_m, t0=1_756_000_000.0, step=1.0) -> TelemetryStore:
    st = TelemetryStore()
    t = np.arange(len(depths_m), dtype=np.float64) * step + t0
    # the field is height above surface in mm, negative once submerged
    v = np.asarray([-d * 1000.0 for d in depths_m], dtype=np.float64)
    st.series[depthplot.DEPTH_FIELD] = Series(t=t, v=v)
    st.t_start, st.t_end = float(t[0]), float(t[-1])
    return st


def test_depth_series_converts_to_metres_below_surface():
    st = _store([0.0, 5.0, 10.0])
    t, d = depthplot.depth_series(st)
    assert list(np.round(d, 3)) == [0.0, 5.0, 10.0]
    assert len(t) == 3


def test_depth_series_is_none_without_the_field():
    assert depthplot.depth_series(TelemetryStore()) is None


def test_profile_renders_at_the_requested_size_in_both_themes():
    st = _store([0, 3, 8, 12, 12, 9, 4, 0])
    w0 = st.t_start
    windows = [("T1", w0 + 2, w0 + 5)]
    for style in (depthplot.PlotStyle(), depthplot.PlotStyle.light()):
        img = depthplot.render_profile(st, windows, width=420, height=180,
                                       style=style)
        assert img.size == (420, 180)


def test_profile_says_so_when_there_is_no_depth():
    img = depthplot.render_profile(TelemetryStore(), [], width=300, height=120)
    assert img.size == (300, 120)          # renders a message, does not crash


def test_transect_band_is_actually_drawn():
    """The band is the whole point of the figure, so prove it reaches pixels."""
    st = _store([0, 3, 8, 12, 12, 9, 4, 0])
    w0 = st.t_start
    style = depthplot.PlotStyle()
    plain = np.asarray(depthplot.render_profile(st, [], width=400, height=160,
                                                style=style).convert("RGB"))
    banded = np.asarray(depthplot.render_profile(
        st, [("T1", w0 + 2, w0 + 5)], width=400, height=160,
        style=style).convert("RGB"))
    assert not np.array_equal(plain, banded), "the band changed nothing"


def test_transect_stats_report_duration_and_depth_range():
    st = _store([0, 3, 8, 12, 12, 9, 4, 0])
    w0 = st.t_start
    rows = depthplot.transect_stats(st, [("T1", w0 + 2, w0 + 5)])
    assert rows[0]["name"] == "T1"
    assert rows[0]["seconds"] == 3.0
    assert rows[0]["depth_max"] == 12.0
    assert rows[0]["depth_min"] == 8.0


def test_transect_stats_survive_a_window_outside_the_data():
    st = _store([0, 3, 8])
    rows = depthplot.transect_stats(st, [("T1", 0.0, 1.0)])
    assert rows[0]["seconds"] == 1.0
    assert "depth_min" not in rows[0]
