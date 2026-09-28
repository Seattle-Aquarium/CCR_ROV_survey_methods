"""
Pauses inside a transect.

A pause is a stretch during which the vehicle was down and recording but
nothing was being surveyed -- Cockpit disarmed, the video glitched, a minute
went on getting the ROV back where it was. Two rules follow from that, and
everything here checks one of them:

* the imagery from a pause is not survey imagery, so nothing files it as such;
* the telemetry from a pause is still telemetry, so nothing throws it away.

Runnable directly (``python tests/test_pauses.py``) or under pytest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops.survey import (  # noqa: E402
    Chapter,
    Pause,
    Site,
    SurveyPlan,
    Transect,
    local_midnight_epoch,
    plan_windows,
    resolve_transect,
)

DAY = "2026-09-16"
TZ = "America/Los_Angeles"


def _site(*transects: Transect) -> Site:
    return Site(name="Centennial", project="HSIL", date=DAY,
                transects=list(transects))


def _chapter(tc_start: float, duration: float) -> Chapter:
    return Chapter(path=Path("fake.mp4"), duration=duration, fps=24.0,
                   width=3840, height=2160, rotation=0, tc_start_s=tc_start)


# --------------------------------------------------------------------------
#  the model
# --------------------------------------------------------------------------


def test_a_transect_without_pauses_is_unchanged():
    t = Transect("T1", "13:00:00", "13:20:00")
    assert t.pauses == []
    assert t.pause_spans() == []
    assert t.paused_s() == 0
    assert t.active_s() == t.duration_s() == 1200
    # One span, the whole transect: a caller needs no special case.
    assert t.active_spans() == [(t.start_s(), t.end_s())]


def test_a_pause_splits_the_transect_into_surveying_spans():
    t = Transect("T1", "13:00:00", "13:20:00",
                 [Pause("13:05:00", "13:06:30")])
    assert t.paused_s() == 90
    assert t.active_s() == 1200 - 90
    s = t.start_s()
    assert t.active_spans() == [(s, s + 300), (s + 390, s + 1200)]


def test_several_pauses_come_back_in_order_and_merged():
    t = Transect("T1", "13:00:00", "13:30:00", [
        Pause("13:20:00", "13:21:00"),      # typed out of order
        Pause("13:05:00", "13:06:00"),
        Pause("13:06:00", "13:07:00"),      # touches the one before it
    ])
    s = t.start_s()
    assert t.pause_spans() == [(s + 300, s + 420), (s + 1200, s + 1260)]
    assert t.paused_s() == 180


def test_a_pause_is_read_on_the_transects_own_clock_past_midnight():
    """A transect that runs through midnight carries its pauses with it."""
    t = Transect("T1", "23:55:00", "00:10:00", [Pause("00:01:00", "00:03:00")])
    assert t.duration_s() == 15 * 60
    assert t.paused_s() == 120
    lo, hi = t.pause_spans()[0]
    assert lo > t.start_s() and hi < t.end_s()


def test_a_half_typed_pause_does_not_erase_the_transect():
    """The operator is mid-keystroke; the row must still say something sane."""
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:05", "")])
    assert t.pause_spans() == []
    assert t.active_s() == 1200


# --------------------------------------------------------------------------
#  what the operator is told
# --------------------------------------------------------------------------


def test_a_pause_outside_its_transect_is_an_error():
    t = Transect("T1", "13:00:00", "13:20:00",
                 [Pause("14:00:00", "14:05:00")])
    errs = t.validate()
    assert errs and "not inside" in errs[0]


def test_a_backwards_pause_is_an_error():
    t = Transect("T1", "13:00:00", "13:20:00",
                 [Pause("13:10:00", "13:10:00")])
    assert any("end is not after start" in e for e in t.validate())


def test_overlapping_pauses_are_an_error():
    t = Transect("T1", "13:00:00", "13:30:00", [
        Pause("13:05:00", "13:10:00"),
        Pause("13:08:00", "13:12:00"),
    ])
    assert any("overlap" in e for e in t.validate())


def test_an_empty_pause_row_is_an_error_rather_than_ignored():
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("", "")])
    assert any("no times" in e for e in t.validate())


def test_a_pause_covering_everything_is_an_error():
    t = Transect("T1", "13:00:00", "13:20:00",
                 [Pause("13:00:00", "13:20:00")])
    assert any("whole transect" in e for e in t.validate())


def test_a_good_pause_passes_validation():
    plan = SurveyPlan([_site(Transect("T1", "13:00:00", "13:20:00",
                                      [Pause("13:05:00", "13:06:00")]))])
    assert plan.validate() == []


# --------------------------------------------------------------------------
#  saving and loading
# --------------------------------------------------------------------------


def test_pauses_survive_a_save_and_load(tmp_path=None):
    import tempfile

    folder = Path(tmp_path or tempfile.mkdtemp())
    plan = SurveyPlan([_site(Transect("T1", "13:00:00", "13:20:00",
                                      [Pause("13:05:00", "13:06:30")]))])
    out = folder / "surveys.json"
    plan.save(out)
    back = SurveyPlan.load(out)
    t = back.sites[0].transects[0]
    assert isinstance(t.pauses[0], Pause)
    assert t.pauses[0].start_tc == "13:05:00"
    assert t.paused_s() == 90


def test_a_plan_saved_before_pauses_existed_still_opens():
    old = json.dumps({
        "sites": [{"name": "Centennial", "project": "HSIL", "date": DAY,
                   "transects": [{"name": "T1", "start_tc": "13:00:00",
                                  "end_tc": "13:20:00"}]}],
        "timezone": TZ,
    })
    t = SurveyPlan.from_json(old).sites[0].transects[0]
    assert t.pauses == []
    assert t.active_s() == 1200


def test_a_plan_from_a_newer_version_still_opens():
    """Unknown keys are ignored rather than raising -- the two programs are
    updated separately and one will always see the other's file first."""
    newer = json.dumps({
        "sites": [{"name": "C", "project": "H", "date": DAY, "transects": [
            {"name": "T1", "start_tc": "13:00:00", "end_tc": "13:20:00",
             "pauses": [{"start_tc": "13:05:00", "end_tc": "13:06:00"}],
             "something_new": 7}]}],
    })
    t = SurveyPlan.from_json(newer).sites[0].transects[0]
    assert t.paused_s() == 60


# --------------------------------------------------------------------------
#  windows -- what imagery is filed by, and what is not
# --------------------------------------------------------------------------


def test_plan_windows_keeps_the_whole_span_by_default():
    plan = SurveyPlan([_site(Transect("T1", "13:00:00", "13:20:00",
                                      [Pause("13:05:00", "13:06:00")]))], TZ)
    windows = plan_windows(plan)
    assert len(windows) == 1
    name, lo, hi = windows[0]
    assert name == "T1" and hi - lo == 1200


def test_plan_windows_can_leave_the_pauses_out():
    plan = SurveyPlan([_site(Transect("T1", "13:00:00", "13:20:00",
                                      [Pause("13:05:00", "13:06:00")]))], TZ)
    windows = plan_windows(plan, exclude_pauses=True)
    assert [n for n, _a, _b in windows] == ["T1", "T1"]
    assert sum(b - a for _n, a, b in windows) == 1200 - 60

    # A frame taken during the pause falls inside no window at all, which is
    # what makes the sorter treat it as off-transect.
    midnight = local_midnight_epoch(plan.sites[0].date_obj(), TZ)
    during = midnight + 13 * 3600 + 5 * 60 + 30
    assert not any(a <= during <= b for _n, a, b in windows)
    assert any(a <= during <= b for _n, a, b in plan_windows(plan))


# --------------------------------------------------------------------------
#  resolving against the footage
# --------------------------------------------------------------------------


def test_resolving_cuts_the_pause_out_of_the_footage():
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:05:00", "13:06:00")])
    site = _site(t)
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(site, t, [ch], timezone=TZ)

    assert len(r.segments) == 2
    assert sum(s.dur_s for s in r.segments) == 1200 - 60
    assert r.requested_s == 1140
    assert r.complete                       # the surveying time is all covered
    # The second piece starts after the pause, in the chapter and on the clock.
    assert r.segments[1].in_s == r.segments[0].in_s + 360
    assert r.segment_epoch(1) - r.segment_epoch(0) == 360
    assert any("pause" in w for w in r.warnings)


def test_a_transect_with_no_pauses_resolves_exactly_as_before():
    t = Transect("T1", "13:00:00", "13:20:00")
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(_site(t), t, [ch], timezone=TZ)
    assert len(r.segments) == 1
    assert r.segments[0].dur_s == 1200
    assert r.requested_s == 1200
    assert r.runs() == [(0, 1)]


def test_the_segments_carry_the_clock_they_belong_to():
    """The compositor places the telemetry by these, so a pause must move the
    later pieces along rather than leaving them running early."""
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:05:00", "13:09:00")])
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(_site(t), t, [ch], timezone=TZ)

    midnight = local_midnight_epoch(_site(t).date_obj(), TZ)
    assert r.segment_epoch(0) == midnight + 13 * 3600
    assert r.segment_epoch(1) == midnight + 13 * 3600 + 9 * 60
    # Laid end to end -- the old behavior -- the second piece would have
    # started four minutes early.
    assert r.segment_epoch(1) != r.epoch_start + r.segments[0].dur_s


def test_a_pause_makes_two_runs_and_the_longer_one_is_offered():
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:02:00", "13:03:00")])
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(_site(t), t, [ch], timezone=TZ)
    assert r.runs() == [(0, 1), (1, 2)]

    segments, epoch = r.longest_run()
    assert len(segments) == 1 and segments[0].dur_s == 17 * 60
    assert epoch == r.segment_epoch(1)

    # spans is what the overlay walks: one per run, each with its own clock.
    assert r.spans == [(r.segment_epoch(0), 120.0), (r.segment_epoch(1), 1020.0)]


def test_shifting_a_transect_moves_every_segment():
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:05:00", "13:06:00")])
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(_site(t), t, [ch], timezone=TZ)
    before = [r.segment_epoch(i) for i in range(len(r.segments))]

    r.shift(1.5)
    after = [r.segment_epoch(i) for i in range(len(r.segments))]
    assert [b + 1.5 for b in before] == after
    # The pauses move with it, so a paused second is still known as one.
    assert r.is_paused(r.epoch_start + 5 * 60 + 30)


def test_is_paused_answers_on_the_telemetry_clock():
    t = Transect("T1", "13:00:00", "13:20:00", [Pause("13:05:00", "13:06:00")])
    ch = _chapter(tc_start=12 * 3600 + 50 * 60, duration=3600)
    r = resolve_transect(_site(t), t, [ch], timezone=TZ)
    assert r.is_paused(r.epoch_start + 330)
    assert not r.is_paused(r.epoch_start + 30)
    assert not r.is_paused(r.epoch_start + 700)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as ex:
                failed += 1
                print(f"  FAIL  {name}: {ex}")
    print(f"\n{'all passed' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
