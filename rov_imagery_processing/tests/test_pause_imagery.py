"""
What a pause does to the imagery: the stills, the clock under the composite,
and the 1 Hz CSV.

The rule being checked throughout is the one the operator asked for. A pause is
time the vehicle was down and recording but nothing was being surveyed, so its
GoPro imagery is not filed as survey imagery -- and its telemetry is kept and
marked rather than thrown away, because a hole in a transect's telemetry looks
exactly like a recording that failed.

Runnable directly (``python tests/test_pause_imagery.py``) or under pytest.
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_imagery_processing import csv_export, overlay, sorting  # noqa: E402
from rov_imagery_processing.pipeline import plan_windows  # noqa: E402
from rov_imagery_processing.survey import (  # noqa: E402
    Chapter,
    Pause,
    Site,
    SurveyPlan,
    Transect,
    local_midnight_epoch,
    resolve_transect,
)

DAY = "2026-09-16"
TZ = "America/Los_Angeles"


def _plan(*pauses: Pause) -> SurveyPlan:
    t = Transect("T1", "13:00:00", "13:20:00", list(pauses))
    return SurveyPlan([Site(name="Centennial", project="HSIL", date=DAY,
                            transects=[t])], TZ)


def _resolved(*pauses: Pause):
    plan = _plan(*pauses)
    site = plan.sites[0]
    ch = Chapter(path=Path("fake.mp4"), duration=3600.0, fps=24.0, width=3840,
                 height=2160, rotation=0, tc_start_s=12 * 3600 + 50 * 60)
    return resolve_transect(site, site.transects[0], [ch], timezone=TZ)


def _epoch(hhmmss: str) -> float:
    h, m, s = (int(p) for p in hhmmss.split(":"))
    return (local_midnight_epoch(_plan().sites[0].date_obj(), TZ)
            + h * 3600 + m * 60 + s)


# --------------------------------------------------------------------------
#  the stills
# --------------------------------------------------------------------------


def test_a_frame_taken_during_a_pause_lands_off_transect():
    """`plan_sort` is what decides a frame's transect, so this is the whole
    behavior for stills: no window contains the frame, so it has no transect
    and the off-transect policy takes over."""
    windows = plan_windows(_plan(Pause("13:05:00", "13:06:00")),
                           exclude_pauses=True)

    def transect_for(t: str):
        for name, lo, hi in windows:
            if lo <= _epoch(t) <= hi:
                return name
        return None

    assert transect_for("13:02:00") == "T1"
    assert transect_for("13:05:30") is None          # inside the pause
    assert transect_for("13:10:00") == "T1"


def test_the_sorter_leaves_a_paused_frame_out_of_the_transect(tmp_path=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_photos import _make_jpeg

    folder = Path(tmp_path or tempfile.mkdtemp())
    jpg_dir = folder / "photos" / "JPG"
    jpg_dir.mkdir(parents=True)
    for name, when in (("surveying", "2026:09:16 13:02:00"),
                       ("paused", "2026:09:16 13:05:30")):
        _make_jpeg(jpg_dir / f"{name}.JPG", when=when)

    windows = plan_windows(_plan(Pause("13:05:00", "13:06:00")),
                           exclude_pauses=True)
    items, _warnings = sorting.plan_sort(folder, windows)
    by_stem = {i.stem: i for i in items}

    assert by_stem["surveying"].transect == "T1"
    assert by_stem["paused"].transect is None


# --------------------------------------------------------------------------
#  the clock under the composite
# --------------------------------------------------------------------------


def test_the_overlay_clock_skips_the_pause():
    """Frame 130 of a clip whose first piece ran 120 seconds belongs ten
    seconds into the second piece, not to 02:10 of an unbroken transect."""
    r = _resolved(Pause("13:02:00", "13:05:00"))
    clock = overlay._Clock(r.epoch_start, r.spans, fps=1.0)

    assert clock.at(0) == _epoch("13:00:00")
    assert clock.at(119) == _epoch("13:01:59")
    assert clock.at(120) == _epoch("13:05:00")       # straight over the pause
    assert clock.at(130) == _epoch("13:05:10")


def test_the_overlay_clock_is_unchanged_without_spans():
    clock = overlay._Clock(1000.0, None, fps=2.0)
    assert clock.at(0) == 1000.0
    assert clock.at(4) == 1002.0


def test_the_overlay_clock_ignores_a_zero_length_span():
    clock = overlay._Clock(500.0, [(500.0, 0.0), (600.0, 10.0)], fps=1.0)
    assert clock.at(0) == 600.0


# --------------------------------------------------------------------------
#  the 1 Hz CSV
# --------------------------------------------------------------------------


class _FlatStore:
    """Telemetry that reads the same at every instant, for column checks."""

    t_start = 0.0
    t_end = 0.0
    series: dict = {}

    def __init__(self, t0: float, t1: float):
        self.t_start, self.t_end = t0, t1

    def fields(self):
        return []

    def num(self, _field, _epoch):
        return None

    def get(self, _field, _epoch):
        return None


def test_the_1hz_csv_marks_paused_seconds(tmp_path=None):
    folder = Path(tmp_path or tempfile.mkdtemp())
    r = _resolved(Pause("13:05:00", "13:06:00"))
    store = _FlatStore(_epoch("13:00:00"), _epoch("13:10:00"))

    out = csv_export.export_1hz(store, folder / "telemetry.csv",
                                plan=_plan(), resolved=[r])
    rows = list(csv.DictReader(out.path.open(encoding="utf-8")))
    by_time = {row["tc25_local"]: row for row in rows}

    assert by_time["13:02:00"]["survey_state"] == "transect"
    assert by_time["13:05:30"]["survey_state"] == "pause"
    assert by_time["13:07:00"]["survey_state"] == "transect"
    # A paused second is still inside its transect and still has its rows.
    assert by_time["13:05:30"]["transect"] == "T1"
    assert "survey_state" in out.columns


def test_the_1hz_csv_is_unchanged_without_pauses(tmp_path=None):
    folder = Path(tmp_path or tempfile.mkdtemp())
    r = _resolved()
    store = _FlatStore(_epoch("13:00:00"), _epoch("13:05:00"))
    out = csv_export.export_1hz(store, folder / "telemetry.csv",
                                plan=_plan(), resolved=[r])
    rows = list(csv.DictReader(out.path.open(encoding="utf-8")))
    assert {row["survey_state"] for row in rows} == {"transect"}


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
