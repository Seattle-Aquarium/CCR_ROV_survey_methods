"""Reading the survey plan JSON that UTC also uses."""

from __future__ import annotations

import json

import pytest

from ccr_m2c.survey import DEFAULT_TIMEZONE, load_plan, transect_ids

PLAN = {
    "sites": [
        {
            "name": "Centennial_Park",
            "project": "testing",
            "date": "2026-08-26",
            "transects": [
                {"name": "T1", "start_tc": "12:19:57", "end_tc": "12:28:42"},
                {"name": "T2", "start_tc": "12:29:55", "end_tc": "12:35:15"},
                {"name": "T3", "start_tc": "12:43:37", "end_tc": "12:49:26"},
            ],
        }
    ],
    "timezone": "America/Los_Angeles",
}


def write(tmp_path, data, name="plan.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_reads_the_real_plan(tmp_path):
    plan = load_plan(write(tmp_path, PLAN))

    assert plan.timezone == DEFAULT_TIMEZONE
    assert not plan.warnings
    assert len(plan.sites) == 1

    site = plan.sites[0]
    assert site.name == "Centennial_Park"
    assert site.project == "testing"
    assert site.survey_date == "20260826"        # YYYYMMDD, for the tide lookup
    assert [t.transect_id for t in site.transects] == ["T1", "T2", "T3"]
    assert site.transects[0].windows == [("12:19:57", "12:28:42")]


def test_site_can_prefix_the_transect_names(tmp_path):
    site = load_plan(write(tmp_path, PLAN)).sites[0]
    assert transect_ids(site) == ["T1", "T2", "T3"]
    assert transect_ids(site, prefix_site=True) == [
        "Centennial_Park_T1", "Centennial_Park_T2", "Centennial_Park_T3"]


def test_several_sites_are_kept_separate(tmp_path):
    data = {"sites": [
        dict(PLAN["sites"][0]),
        {"name": "Alki", "project": "testing", "date": "2026-08-27",
         "transects": [{"name": "T1", "start_tc": "09:00:00", "end_tc": "09:30:00"}]},
    ]}
    plan = load_plan(write(tmp_path, data))
    assert [s.name for s in plan.sites] == ["Centennial_Park", "Alki"]
    assert plan.sites[1].survey_date == "20260827"


def test_a_foreign_timezone_is_flagged_not_silently_shifted(tmp_path):
    data = dict(PLAN, timezone="America/New_York")
    plan = load_plan(write(tmp_path, data))
    assert plan.sites                                  # still usable
    assert any("US/Pacific" in w for w in plan.warnings)


def test_a_site_without_transects_is_skipped_with_a_note(tmp_path):
    data = {"sites": [
        {"name": "Empty", "project": "testing", "date": "2026-08-26", "transects": []},
        PLAN["sites"][0],
    ]}
    plan = load_plan(write(tmp_path, data))
    assert [s.name for s in plan.sites] == ["Centennial_Park"]
    assert any("no transects" in w for w in plan.warnings)


@pytest.mark.parametrize("data, message", [
    ({"sites": []}, "no 'sites'"),
    ({"nope": 1}, "no 'sites'"),
    ({"sites": [{"project": "t", "date": "2026-08-26", "transects": []}]}, "no name"),
    ({"sites": [{"name": "S", "date": "26-08-2026",
                 "transects": [{"name": "T1", "start_tc": "1:00:00",
                                "end_tc": "2:00:00"}]}]}, "YYYY-MM-DD"),
    ({"sites": [{"name": "S", "date": "2026-08-26",
                 "transects": [{"name": "T1", "start_tc": "nope",
                                "end_tc": "12:00:00"}]}]}, "HH:MM:SS"),
    ({"sites": [{"name": "S", "date": "2026-08-26",
                 "transects": [{"name": "T1", "start_tc": "13:00:00",
                                "end_tc": "12:00:00"}]}]}, "starts at"),
])
def test_a_broken_plan_says_what_is_wrong(tmp_path, data, message):
    with pytest.raises(ValueError, match=message):
        load_plan(write(tmp_path, data))


def test_malformed_json_is_reported_as_such(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_plan(p)
