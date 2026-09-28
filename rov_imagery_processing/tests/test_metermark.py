"""Tests for the distance marks and the frame each one gets.

The cases that carry weight here are the ones where being wrong is invisible:
a source that measures short because its messages were decimated, two marks
quietly given the same photograph, a pause counted as survey progress, and a
mark placed inside telemetry that was never recorded. All four produce a
plausible-looking CSV.

Runnable directly (``python tests/test_metermark.py``) or under pytest.
"""

from __future__ import annotations

import itertools
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_imagery_processing import layout  # noqa: E402
from rov_imagery_processing import metermark as mm  # noqa: E402
from rov_imagery_processing.telemetry import Series, TelemetryStore  # noqa: E402

T0 = 1_700_000_000.0


# --------------------------------------------------------------------------
#  Fixtures: a vehicle flying due east at a known speed
# --------------------------------------------------------------------------


def straight_store(
    *, seconds: float = 100.0, speed: float = 0.5, rate: float = 3.0,
    with_ned: bool = True, with_dvl: bool = True, with_gpi: bool = False,
) -> TelemetryStore:
    """A clean run east at `speed` m/s, so the true length is speed*seconds."""
    st = TelemetryStore()
    t = np.arange(0.0, seconds, 1.0 / rate) + T0

    def put(name, times, vals):
        st.series[name] = Series(t=np.asarray(times, float),
                                 v=np.asarray(vals, float))

    if with_ned:
        put(mm.F_NED_X, t, np.zeros_like(t))
        put(mm.F_NED_Y, t, (t - T0) * speed)
        put(mm.F_NED_VX, t, np.zeros_like(t))
        put(mm.F_NED_VY, t, np.full_like(t, speed))
    if with_dvl:
        td = np.arange(0.0, seconds, 1.0 / 9.0) + T0
        put(mm.F_DVL_DX, td, np.full_like(td, speed / 9.0))
        put(mm.F_DVL_DY, td, np.zeros_like(td))
        put(mm.F_YAW, td, np.full_like(td, math.pi / 2))     # heading east
    if with_gpi:
        put(mm.F_GPI_VX, t, np.zeros_like(t))
        put(mm.F_GPI_VY, t, np.full_like(t, speed * 100.0))  # cm/s
    st.t_start, st.t_end = float(t[0]), float(t[-1])
    return st


def whole(seconds: float = 100.0):
    """The transect as a single surveying span."""
    return [(T0, T0 + seconds)]


# --------------------------------------------------------------------------
#  Source selection and calibration
# --------------------------------------------------------------------------


def test_prefers_ekf_velocity_when_present():
    store = straight_store()
    assert mm.available_sources(store, whole())[0] == "ekf_velocity"
    assert mm.build_track(store, whole()).source.key == "ekf_velocity"


def test_falls_back_to_dvl_without_local_position():
    store = straight_store(with_ned=False)
    assert "ekf_velocity" not in mm.available_sources(store, whole())
    track = mm.build_track(store, whole())
    assert track.source.key == "dvl"
    assert track.xy_source == "VISION_POSITION_DELTA"


def test_global_position_is_offered_but_carries_no_geometry():
    store = straight_store(with_ned=False, with_dvl=False, with_gpi=True)
    assert "gps_velocity" in mm.available_sources(store, whole())
    # Nothing to place marks on, so there is no track rather than a wrong one.
    assert mm.build_track(store, whole()) is None


def test_calibration_is_applied_and_can_be_switched_off():
    store = straight_store(seconds=100.0, speed=0.5)     # 50 m of true travel
    raw = mm.build_track(store, whole(), calibrate=False)
    cal = mm.build_track(store, whole(), calibrate=True)
    assert abs(raw.length - 50.0) < 0.5
    scale = mm.SOURCES_BY_KEY["ekf_velocity"].scale
    assert abs(cal.length - raw.length * scale) < 1e-6


def test_every_source_carries_a_scale_and_a_reason():
    for src in mm.SOURCES:
        assert 0.9 < src.scale < 1.1, src
        assert src.note and src.label


# --------------------------------------------------------------------------
#  Marks
# --------------------------------------------------------------------------


def test_marks_are_evenly_spaced_at_the_requested_interval():
    store = straight_store(seconds=100.0, speed=0.5)
    track = mm.build_track(store, whole(), calibrate=False)
    for interval in (0.5, 1.0, 2.0, 5.0):
        marks = mm.place_marks(track, interval)
        assert marks, interval
        spacing = {round(b.distance_m - a.distance_m, 6)
                   for a, b in itertools.pairwise(marks)}
        assert spacing == {interval}
        assert marks[0].distance_m == interval
        assert [m.number for m in marks] == list(range(1, len(marks) + 1))


def test_mark_times_track_constant_speed():
    store = straight_store(seconds=100.0, speed=0.5)
    track = mm.build_track(store, whole(), calibrate=False)
    # At 0.5 m/s a 1 m mark falls every 2 s.
    gaps = [b.epoch - a.epoch
            for a, b in itertools.pairwise(mm.place_marks(track, 1.0))]
    assert max(abs(g - 2.0) for g in gaps) < 0.05


def test_interval_must_be_positive():
    track = mm.build_track(straight_store(), whole())
    for bad in (0.0, -1.0):
        try:
            mm.place_marks(track, bad)
        except ValueError:
            continue
        raise AssertionError(f"interval {bad} should be rejected")


# --------------------------------------------------------------------------
#  Pauses
# --------------------------------------------------------------------------


def test_a_pause_adds_no_distance():
    """The ROV keeps moving through a pause; the survey does not."""
    store = straight_store(seconds=100.0, speed=0.5)
    continuous = mm.build_track(store, whole(), calibrate=False).length
    paused = mm.build_track(
        store, [(T0, T0 + 40.0), (T0 + 60.0, T0 + 100.0)], calibrate=False)
    # 20 s of the 100 s is excluded, so ~10 m of the ~50 m goes with it.
    assert abs(continuous - 50.0) < 0.5
    assert abs(paused.length - 40.0) < 0.5
    assert abs(paused.paused_s - 20.0) < 1.0


def test_the_track_stays_whole_across_a_pause():
    """Distance skips the pause, but the drawn path must not tear."""
    store = straight_store(seconds=100.0, speed=0.5)
    paused = mm.build_track(store, [(T0, T0 + 40.0), (T0 + 60.0, T0 + 100.0)])
    assert float(paused.t[0]) <= T0 + 1
    assert float(paused.t[-1]) >= T0 + 99
    # Cumulative distance never decreases, and is flat through the pause.
    assert np.all(np.diff(paused.s) >= -1e-9)
    inside = (paused.t > T0 + 42) & (paused.t < T0 + 58)
    assert np.ptp(paused.s[inside]) < 0.2


def test_grouping_collects_a_paused_transect_into_one():
    windows = [("T1", 1.0, 2.0), ("T2", 5.0, 6.0), ("T1", 3.0, 4.0)]
    grouped = mm.group_windows(windows)
    assert [n for n, _ in grouped] == ["T1", "T2"], "order of first sight"
    assert dict(grouped)["T1"] == [(1.0, 2.0), (3.0, 4.0)]


def test_a_paused_transect_yields_one_reconstruction_not_two():
    store = straight_store(seconds=100.0, speed=0.5)
    with tempfile.TemporaryDirectory() as tmp:
        flight = Path(tmp)
        layout.ensure_transect(flight, "T1")
        rep = mm.run_for_flight(
            flight,
            [("T1", T0, T0 + 40.0), ("T1", T0 + 60.0, T0 + 100.0)],
            store, mm.MarkOptions(enabled=True, write_csv=True))
    assert len(rep.reconstructions) == 1
    assert len(rep.csv_paths) == 1, "two windows must not fight over one file"


# --------------------------------------------------------------------------
#  Handing out frames
# --------------------------------------------------------------------------


def _marks(n: int) -> list[mm.Mark]:
    return [mm.Mark(i + 1, float(i + 1), T0 + (i + 1) * 10.0, 0.0, 0.0)
            for i in range(n)]


def test_each_frame_is_claimed_by_at_most_one_mark():
    # Frames far sparser than the marks: a naive nearest-match would hand the
    # same file to several marks and silently leave the rest with nothing.
    out = mm.choose_frames(_marks(5), [(T0 + 12.0, "a"), (T0 + 41.0, "b")],
                           max_offset=30.0)
    stems = [m.stem for m in out if m.matched]
    assert len(stems) == len(set(stems)) == 2


def test_nearest_frame_wins():
    out = mm.choose_frames(
        _marks(1),
        [(T0 + 5.0, "early"), (T0 + 11.0, "close"), (T0 + 30.0, "late")],
        max_offset=30.0)
    assert out[0].stem == "close"
    assert abs(out[0].offset_s - 1.0) < 1e-9


def test_distant_frames_are_refused_not_stretched_to():
    out = mm.choose_frames(_marks(1), [(T0 + 500.0, "miles away")],
                           max_offset=15.0)
    assert not out[0].matched and out[0].stem is None


def test_no_frames_at_all_is_not_an_error():
    out = mm.choose_frames(_marks(3), [], max_offset=15.0)
    assert len(out) == 3 and not any(m.matched for m in out)


# --------------------------------------------------------------------------
#  The things that go wrong quietly
# --------------------------------------------------------------------------


def test_decimating_a_differential_channel_measures_short():
    """Why VISION_POSITION_DELTA is exempt from MIN_INTERVAL in mcap_extract.

    position_delta is travel since the last message, not a reading. Dropping
    every third message deletes a third of the distance -- it does not sample
    it more coarsely.
    """
    whole_len = mm.build_track(straight_store(with_ned=False), whole(),
                               calibrate=False).length

    thin = straight_store(with_ned=False)
    for f in (mm.F_DVL_DX, mm.F_DVL_DY, mm.F_YAW):
        s = thin.series[f]
        thin.series[f] = Series(t=s.t[::3], v=s.v[::3])
    thinned = mm.build_track(thin, whole(), calibrate=False).length

    assert thinned < whole_len * 0.5, (
        f"decimation should lose distance: {whole_len:.1f} -> {thinned:.1f}")


def test_a_filter_reset_is_not_counted_as_travel():
    store = straight_store(seconds=100.0, speed=0.5)
    s = store.series[mm.F_NED_Y]
    v = s.v.copy()
    v[len(v) // 2:] += 8.0                    # GPS correction snaps position
    store.series[mm.F_NED_Y] = Series(t=s.t, v=v)

    clean = mm.build_track(store, whole(), source="ekf_position",
                           calibrate=False)
    assert clean.length < 55.0, "the 8 m jump should not be counted"
    assert clean.repairs >= 1
    assert clean.phantom_m > 5.0


def test_a_mark_inside_a_telemetry_gap_is_flagged():
    store = straight_store(seconds=100.0, speed=0.5)
    for f in (mm.F_NED_X, mm.F_NED_Y, mm.F_NED_VX, mm.F_NED_VY):
        s = store.series[f]
        keep = (s.t < T0 + 30.0) | (s.t > T0 + 70.0)      # 40 s hole
        store.series[f] = Series(t=s.t[keep], v=s.v[keep])

    rec = mm.reconstruct(store, "T1", whole(), interval=1.0)
    assert rec.track.gaps_s > 30.0
    assert any("missing" in w or "covers only" in w for w in rec.warnings)


def test_sources_that_disagree_raise_a_warning():
    store = straight_store(seconds=100.0, speed=0.5)
    s = store.series[mm.F_DVL_DX]              # DVL claims twice the travel
    store.series[mm.F_DVL_DX] = Series(t=s.t, v=s.v * 2.0)
    rec = mm.reconstruct(store, "T1", whole(), interval=1.0)
    assert mm.spread(rec.lengths) > mm.SPREAD_WARN
    assert any("disagree" in w for w in rec.warnings)


def test_a_window_that_doubles_back_is_flagged_as_unstraight():
    store = straight_store(seconds=100.0, speed=0.5)
    s = store.series[mm.F_NED_Y]
    half = len(s.v) // 2
    v = s.v.copy()
    v[half:] = v[half] - (v[half:] - v[half])          # turn around
    store.series[mm.F_NED_Y] = Series(t=s.t, v=v)
    vy = store.series[mm.F_NED_VY]
    store.series[mm.F_NED_VY] = Series(
        t=vy.t, v=np.where(vy.t < vy.t[half], vy.v, -vy.v))

    rec = mm.reconstruct(store, "T1", whole(), interval=1.0)
    assert rec.track.straightness < mm.STRAIGHTNESS_WARN
    assert any("straightness" in w for w in rec.warnings)


def test_empty_window_reports_rather_than_raises():
    rec = mm.reconstruct(straight_store(), "T9",
                         [(T0 + 5000, T0 + 5100)])
    assert not rec.ok and rec.track is None and rec.warnings


# --------------------------------------------------------------------------
#  Outputs
# --------------------------------------------------------------------------


def test_csv_has_the_documented_columns_and_one_row_per_mark():
    import csv as _csv

    store = straight_store(seconds=60.0, speed=0.5)
    rec = mm.reconstruct(store, "T1", whole(60.0), interval=1.0)
    with tempfile.TemporaryDirectory() as tmp:
        out = mm.write_marks_csv(rec, Path(tmp) / "T1_meter_marks.csv")
        rows = list(_csv.reader(out.read_text().splitlines()))
    assert tuple(rows[0]) == mm.CSV_COLUMNS
    assert len(rows) - 1 == len(rec.marks)
    assert rows[1][0] == "T1"
    assert rows[1][10] == "ekf_velocity"


def test_png_is_written_and_is_a_png():
    from rov_imagery_processing.trackplot import save_track_png

    store = straight_store(seconds=60.0, speed=0.5)
    rec = mm.reconstruct(store, "T1", whole(60.0), interval=1.0)
    with tempfile.TemporaryDirectory() as tmp:
        out = save_track_png(rec, Path(tmp) / "T1_track.png")
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_png_survives_a_window_with_no_telemetry():
    from rov_imagery_processing.trackplot import save_track_png

    rec = mm.reconstruct(straight_store(), "T9", [(T0 + 5000, T0 + 5100)])
    with tempfile.TemporaryDirectory() as tmp:
        assert save_track_png(rec, Path(tmp) / "T9_track.png").is_file()


def test_png_draws_a_paused_transect():
    from rov_imagery_processing.trackplot import save_track_png

    store = straight_store(seconds=100.0, speed=0.5)
    rec = mm.reconstruct(store, "T1",
                         [(T0, T0 + 40.0), (T0 + 60.0, T0 + 100.0)])
    with tempfile.TemporaryDirectory() as tmp:
        assert save_track_png(rec, Path(tmp) / "T1_track.png").is_file()


# --------------------------------------------------------------------------
#  Over a whole flight
# --------------------------------------------------------------------------


def test_run_for_flight_is_off_unless_asked():
    with tempfile.TemporaryDirectory() as tmp:
        rep = mm.run_for_flight(Path(tmp), [("T1", *whole()[0])],
                                straight_store(),
                                mm.MarkOptions(enabled=False))
    assert not rep.reconstructions
    assert not rep.csv_paths and not rep.png_paths


def test_run_for_flight_writes_beside_the_imagery():
    store = straight_store(seconds=60.0, speed=0.5)
    with tempfile.TemporaryDirectory() as tmp:
        flight = Path(tmp)
        layout.ensure_transect(flight, "T1")
        rep = mm.run_for_flight(
            flight, [("T1", *whole(60.0)[0])], store,
            mm.MarkOptions(enabled=True, write_csv=True, write_png=True))
        tdir = layout.transect_dir(flight, "T1")
        assert (tdir / f"T1{mm.CSV_SUFFIX}").is_file()
        assert (tdir / f"T1{mm.PNG_SUFFIX}").is_file()
    assert rep.marks > 0 and len(rep.reconstructions) == 1


def test_frames_are_read_from_disk_not_assumed():
    from datetime import datetime, timezone

    with tempfile.TemporaryDirectory() as tmp:
        flight = Path(tmp)
        tdir = layout.ensure_transect(flight, "T1")
        # Two frames really present, named the way sorting writes them.
        for offset in (10.0, 20.0):
            when = datetime.fromtimestamp(T0 + offset, timezone.utc)
            (tdir / layout.JPG_PREVIEW /
             f"{when.strftime('%Y_%m_%d_%H-%M-%S')}.JPG").write_bytes(b"x")
        found = mm.frames_in_transect(flight, "T1", tz_name="UTC")
    assert len(found) == 2
    assert all(isinstance(e, float) and isinstance(s, str) for e, s in found)


# --------------------------------------------------------------------------
#  Filing the matched frames into meters/
# --------------------------------------------------------------------------


def _flight_with_frames(tmp: Path, *, edited: bool = False):
    """A transect folder holding GPR (and optionally edited JPG) frames."""
    from datetime import datetime, timezone

    flight = Path(tmp)
    tdir = layout.ensure_transect(flight, "T1")
    stems = []
    for offset in range(0, 60, 3):
        when = datetime.fromtimestamp(T0 + offset, timezone.utc)
        stem = when.strftime("%Y_%m_%d_%H-%M-%S")
        stems.append(stem)
        (tdir / layout.GPR / f"{stem}.GPR").write_bytes(b"raw")
        if edited:
            (tdir / layout.JPG_EDITED / f"{stem}.JPG").write_bytes(b"edited")
    return flight, tdir, stems


def _run(flight, **kw):
    store = straight_store(seconds=60.0, speed=0.5)
    opts = mm.MarkOptions(enabled=True, **kw)
    return mm.run_for_flight(flight, [("T1", *whole(60.0)[0])], store, opts,
                             tz_name="UTC")


def test_matched_gpr_frames_are_filed_into_meters():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, stems = _flight_with_frames(tmp)
        before = len(list((tdir / layout.GPR).glob("*.GPR")))
        rep = _run(flight)
        meters = tdir / layout.GPR / layout.METERS
        filed = sorted(p.name for p in meters.glob("*.GPR"))
        left = sorted(p.name for p in (tdir / layout.GPR).glob("*.GPR"))

    assert rep.filed == len(filed) > 0
    assert len(filed) + len(left) == before, "nothing may be lost or duplicated"
    matched = {m.stem for m in rep.reconstructions[0].matches if m.matched}
    assert {Path(n).stem for n in filed} == matched


def test_unmatched_frames_stay_where_they_were():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, stems = _flight_with_frames(tmp)
        rep = _run(flight)
        left = {p.stem for p in (tdir / layout.GPR).glob("*.GPR")}
    matched = {m.stem for m in rep.reconstructions[0].matches if m.matched}
    assert left and not (left & matched), "only extraneous frames remain"


def test_edited_jpgs_are_left_alone_unless_asked():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp, edited=True)
        _run(flight)
        assert not (tdir / layout.JPG_EDITED / layout.METERS).exists()
        assert list((tdir / layout.JPG_EDITED).glob("*.JPG"))


def test_edited_jpgs_are_filed_when_asked():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp, edited=True)
        rep = _run(flight, move_jpg=True)
        filed = sorted(p.stem for p in
                       (tdir / layout.JPG_EDITED / layout.METERS).glob("*.JPG"))
    matched = {m.stem for m in rep.reconstructions[0].matches if m.matched}
    assert set(filed) == matched


def test_filing_is_a_move_not_a_copy():
    """A re-encode would cost a JPEG generation; a move costs nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp, edited=True)
        _run(flight, move_jpg=True)
        meters = tdir / layout.JPG_EDITED / layout.METERS
        one = next(iter(meters.glob("*.JPG")))
        assert one.read_bytes() == b"edited", "content must be untouched"
        assert not (tdir / layout.JPG_EDITED / one.name).exists()


def test_re_running_finds_the_filed_frames_and_does_not_double_file():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp)
        first = _run(flight)
        second = _run(flight)
        meters = tdir / layout.GPR / layout.METERS
        assert len(list(meters.glob("*.GPR"))) == first.filed
    # The second pass still sees the frames, so the marks are unchanged.
    assert second.matched == first.matched
    assert second.filed == 0, "already filed, nothing left to move"


def test_filing_can_be_switched_off_entirely():
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp)
        rep = _run(flight, move_gpr=False)
        assert not (tdir / layout.GPR / layout.METERS).exists()
    assert rep.filed == 0
    assert rep.matched > 0, "marks still get their frames"


# --------------------------------------------------------------------------
#  A flight that was sorted on an earlier day
# --------------------------------------------------------------------------


def test_marks_run_when_the_sort_has_nothing_left_to_move():
    """The regression: a reopened flight has empty offload folders.

    `sort_flight` finds nothing to move and reports no transects. Marks used
    to be filtered to what that run had just moved, so they silently did
    nothing on every flight that was already sorted -- which is most of them.
    """
    from rov_imagery_processing import sorting

    store = straight_store(seconds=60.0, speed=0.5)
    with tempfile.TemporaryDirectory() as tmp:
        flight, tdir, _ = _flight_with_frames(tmp)     # already in T1/GPR
        rep = sorting.sort_flight(
            flight, [("T1", *whole(60.0)[0])], store=store,
            options=sorting.SortOptions(
                banner_previews=False, meter_marks=True, marks_csv=True))

        assert rep.gpr_moved == 0 and rep.jpg_moved == 0, "nothing to sort"
        assert rep.transects == [], "the sort itself found no transects"
        assert rep.marks is not None, "marks must still have run"
        assert rep.marks.marks > 0
        assert (tdir / f"T1{mm.CSV_SUFFIX}").is_file()


def test_a_transect_never_imported_is_skipped():
    """A window with no folder on disk must not produce an empty CSV."""
    store = straight_store(seconds=60.0, speed=0.5)
    with tempfile.TemporaryDirectory() as tmp:
        flight, _tdir, _ = _flight_with_frames(tmp)    # only T1 exists
        rep = mm.run_for_flight(
            flight,
            [("T1", *whole(60.0)[0]), ("T7", *whole(60.0)[0])],
            store, mm.MarkOptions(enabled=True, write_csv=True))
        names = [r.name for r in rep.reconstructions]
        assert names == ["T1"], names
        assert not (layout.transect_dir(flight, "T7")).exists()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
