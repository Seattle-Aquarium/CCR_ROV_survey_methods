"""Unit tests for TC-25 parsing and transect resolution.

Runnable directly (``python tests/test_survey.py``) or under pytest.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc.survey import (  # noqa: E402
    Chapter,
    Site,
    SurveyError,
    SurveyPlan,
    Transect,
    format_hhmmss,
    local_midnight_epoch,
    parse_hhmmss,
    resolve_transect,
    utc_offset_hours,
)


def test_parse_hhmmss():
    assert parse_hhmmss("13:37:31") == 13 * 3600 + 37 * 60 + 31
    assert parse_hhmmss(" 09:05:00 ") == 9 * 3600 + 5 * 60
    assert parse_hhmmss("9:5:0") == 9 * 3600 + 5 * 60
    assert parse_hhmmss("13.37.31") == 13 * 3600 + 37 * 60 + 31   # dots from field notes
    assert parse_hhmmss("13:37:31:12") == 13 * 3600 + 37 * 60 + 31  # frames ignored
    for bad in ("", "abc", "25:00:00", "12:60:00", "12:00:61", "12:00"):
        try:
            parse_hhmmss(bad)
        except SurveyError:
            pass
        else:
            raise AssertionError(f"should have rejected {bad!r}")


def test_format_roundtrip():
    for s in (0, 1, 3599, 3600, 49051, 86399):
        assert parse_hhmmss(format_hhmmss(s)) == s


def test_transect_duration_and_midnight_wrap():
    t = Transect("T1", "13:00:00", "13:20:00")
    assert t.duration_s() == 1200
    # a transect running past local midnight
    t2 = Transect("T2", "23:50:00", "00:10:00")
    assert t2.duration_s() == 1200


def test_transect_validation():
    assert Transect("T1", "13:00:00", "13:20:00").validate() == []
    assert Transect("T1", "13:20:00", "13:20:00").validate()      # zero length
    assert Transect("T1", "01:00:00", "23:00:00").validate()      # implausibly long


def test_pacific_offset_dst():
    # August is PDT (-7); January is PST (-8)
    assert utc_offset_hours(date(2026, 8, 24)) == -7.0
    assert utc_offset_hours(date(2026, 1, 24)) == -8.0


def test_local_midnight_epoch():
    m = local_midnight_epoch(date(2026, 8, 24))
    # midnight PDT == 07:00 UTC
    import datetime as dt
    assert dt.datetime.fromtimestamp(m, dt.timezone.utc).hour == 7


def _chapter(tc_start: str, dur: float, name="GX01.MP4") -> Chapter:
    return Chapter(Path(name), dur, 23.976, 3840, 2160, 180, parse_hhmmss(tc_start))


def _site(*transects: Transect) -> Site:
    return Site("Cove", "HSIL", "2026-08-24", list(transects))


def test_resolve_inside_one_chapter():
    ch = _chapter("13:00:00", 1800)                       # 13:00:00 - 13:30:00
    t = Transect("T1", "13:05:00", "13:15:00")
    r = resolve_transect(_site(t), t, [ch])
    assert len(r.segments) == 1
    assert abs(r.segments[0].in_s - 300) < 1e-6
    assert abs(r.segments[0].dur_s - 600) < 1e-6
    assert r.complete
    assert r.output_stem("1080p") == "2026-08-24_HSIL_Cove_T1_1080p"


def test_resolve_spans_two_chapters():
    a = _chapter("13:00:00", 1200, "GX010001.MP4")        # 13:00 - 13:20
    b = _chapter("13:20:00", 1200, "GX020001.MP4")        # 13:20 - 13:40
    t = Transect("T1", "13:15:00", "13:25:00")
    r = resolve_transect(_site(t), t, [a, b])
    assert len(r.segments) == 2
    assert abs(r.segments[0].in_s - 900) < 1e-6 and abs(r.segments[0].dur_s - 300) < 1e-6
    assert abs(r.segments[1].in_s - 0) < 1e-6 and abs(r.segments[1].dur_s - 300) < 1e-6
    assert r.complete
    assert any("chapters" in w for w in r.warnings)


def test_resolve_partial_and_outside():
    ch = _chapter("13:00:00", 600)                        # 13:00 - 13:10
    t = Transect("T1", "13:05:00", "13:20:00")            # runs past the end
    r = resolve_transect(_site(t), t, [ch])
    assert not r.complete and 0 < r.coverage < 1
    assert any("covered" in w for w in r.warnings)

    t2 = Transect("T2", "15:00:00", "15:10:00")           # nowhere near
    r2 = resolve_transect(_site(t2), t2, [ch])
    assert r2.segments == [] and r2.coverage == 0
    assert any("outside the recorded video" in w for w in r2.warnings)


def test_resolve_epochs_use_flight_date_offset():
    ch = _chapter("13:00:00", 1800)
    t = Transect("T1", "13:00:00", "13:10:00")
    r = resolve_transect(_site(t), t, [ch])
    # 13:00 PDT == 20:00 UTC
    import datetime as dt
    assert dt.datetime.fromtimestamp(r.epoch_start, dt.timezone.utc).hour == 20
    assert abs((r.epoch_end - r.epoch_start) - 600) < 1e-6


def test_no_timecode_is_reported():
    ch = Chapter(Path("x.MP4"), 600, 23.976, 3840, 2160, 0, None)
    t = Transect("T1", "13:00:00", "13:10:00")
    r = resolve_transect(_site(t), t, [ch])
    assert r.segments == []
    assert any("precision time" in w for w in r.warnings)


def test_plan_validation_catches_overlap_and_dupes():
    p = SurveyPlan([_site(Transect("T1", "13:00:00", "13:20:00"),
                          Transect("T2", "13:10:00", "13:30:00"))])
    assert any("overlap" in e for e in p.validate())

    p2 = SurveyPlan([_site(Transect("T1", "13:00:00", "13:10:00"),
                           Transect("T1", "13:20:00", "13:30:00"))])
    assert any("two transects called" in e for e in p2.validate())

    assert any("no sites" in e for e in SurveyPlan([]).validate())


def test_two_sites_may_not_share_a_transect_name():
    """The 2026-08-31 bug: two ROVs flown the same day, each with a "T1".

    Imagery is filed by transect name alone, so both sets landed in one folder
    -- 232 frames in a transect that held 191. Nothing complained, because the
    duplicate check only ever looked inside a single site.
    """
    p = SurveyPlan([
        Site("Magnolia_Lutris", "PoS", "2026-08-31",
             [Transect("T1", "10:02:27", "10:12:00")]),
        Site("Magnolia_Nereo", "PoS", "2026-08-31",
             [Transect("T1", "12:54:59", "12:57:01")]),
    ])
    errs = p.validate()
    assert any("more than one site" in e for e in errs), errs
    assert any("Magnolia_Lutris" in e and "Magnolia_Nereo" in e for e in errs), errs

    # renaming the second one clears it -- the times themselves were fine
    p.sites[1].transects[0].name = "T5"
    assert p.validate() == []


def test_one_site_with_several_transects_is_still_fine():
    p = SurveyPlan([_site(Transect("T1", "13:00:00", "13:10:00"),
                          Transect("T2", "13:20:00", "13:30:00"))])
    assert p.validate() == []


def test_plan_roundtrip():
    p = SurveyPlan([_site(Transect("T1", "13:00:00", "13:10:00"))])
    q = SurveyPlan.from_json(p.to_json())
    assert q.sites[0].name == "Cove"
    assert q.sites[0].transects[0].end_tc == "13:10:00"


def test_output_stem_sanitises():
    s = Site("Pier 62 / North", "Port of Seattle", "2026-08-24",
             [Transect("T1", "13:00:00", "13:10:00")])
    r = resolve_transect(s, s.transects[0], [_chapter("13:00:00", 1800)])
    stem = r.output_stem("4K")
    assert stem == "2026-08-24_Port-of-Seattle_Pier-62-North_T1_4K"
    assert not any(c in stem for c in '\\/:*?"<>|')


def test_missing_timezone_database_fails_loudly():
    """A wrong time is worse than no time.

    Windows ships no timezone database, so a fresh laptop (or a CI runner)
    can lack it entirely. The old code fell back to a fixed -8: summer times
    came out an hour wrong, and local_midnight_epoch's version of the same
    fallback double-counted and came out EIGHT hours wrong. That silently
    files imagery into the wrong transect. It must raise instead.
    """
    import datetime as _dt

    from utc import survey as S

    def _raises_no_data(hint: str) -> None:
        for call in (lambda: S.utc_offset_hours(_dt.date(2026, 8, 26)),
                     lambda: S.local_midnight_epoch(_dt.date(2026, 8, 26))):
            try:
                value = call()
            except S.TimezoneDataMissing as ex:
                assert hint in str(ex).lower(), ex
            else:
                raise AssertionError(
                    f"returned {value!r} instead of raising — a wrong time "
                    f"is worse than no time")
        assert S.timezone_data_available() is False

    saved = S.ZoneInfo

    # The realistic case: zoneinfo exists, but Windows ships no database for
    # it to read. The message must name the package that fixes it.
    class _NoData:
        def __init__(self, *_a, **_k):
            raise KeyError("no time zone found with key")

    try:
        S.ZoneInfo = _NoData
        _raises_no_data("tzdata")

        # The other way it can be missing: no zoneinfo module at all.
        S.ZoneInfo = None
        _raises_no_data("zoneinfo")
    finally:
        S.ZoneInfo = saved

    assert S.timezone_data_available() is True


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
