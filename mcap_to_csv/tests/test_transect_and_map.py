"""Cutting the dive into transects, and drawing the result."""

from __future__ import annotations

import json
import os
import re

import numpy as np
import pandas as pd
import pytest
from conftest import BASE_EPOCH, straight_north_dive

from ccr_m2c import mapping
from ccr_m2c.fsutil import _numbered, publish
from ccr_m2c.mcap_read import read_mcaps
from ccr_m2c.pipeline import TransectSpec, run
from ccr_m2c.tide import add_empty_tide
from ccr_m2c.transect import (
    OUTPUT_COLUMNS,
    build_transect_mask,
    export_transect,
    sanitize_filename,
)


@pytest.fixture
def dive(builder):
    """A 60-second northbound dive, tide column present but empty."""
    path = straight_north_dive(builder(), seconds=60).close()
    res = read_mcaps([path])
    return add_empty_tide(res.df), res


#: Every column the CSV has ever carried. The order is free to change, but a
#: column quietly disappearing would break a downstream join without warning.
EXPECTED_COLUMNS = {
    "Date", "Time", "Datetime_UTC", "Site_name", "Transect_number",
    "Transect_ID", "Mode_num", "Mode", "Battery_V", "Battery_A", "Battery_W",
    "Battery_mAh_used", "Battery_Wh_used", "Latitude", "Longitude", "EKFlat",
    "EKFlon", "DVLx", "DVLy", "DVLlat", "DVLlon", "Altitude", "Depth",
    "Depth_std", "Depth_Source", "Heading", "Velocity_mps", "Width", "Area_m2",
    "Distance", "NEDz", "VFR_alt", "Roll", "Pitch", "Water_temp_C",
    "Pressure_abs_hPa", "DVL_confidence", "DVL_source", "Lights_pct",
    "Cam_tilt", "GPS_fix_type", "GPS_satellites", "Relative_alt_m", "Messages",
}


def test_no_column_is_lost_or_invented():
    assert set(OUTPUT_COLUMNS) == EXPECTED_COLUMNS
    assert len(OUTPUT_COLUMNS) == len(set(OUTPUT_COLUMNS)), "a column is repeated"


def test_related_columns_sit_together():
    """The point of the ordering: someone scanning the header should not have to
    hunt for the other half of a pair."""
    at = {c: i for i, c in enumerate(OUTPUT_COLUMNS)}

    # the three coordinate pairs, adjacent and in a comparable block
    for lat, lon in (("Latitude", "Longitude"), ("EKFlat", "EKFlon"),
                     ("DVLlat", "DVLlon")):
        assert at[lon] == at[lat] + 1, f"{lat}/{lon} are not adjacent"
    assert at["DVLlon"] - at["Latitude"] == 5, "the fixes are not one block"

    # fix quality next to the fix it describes
    assert at["GPS_fix_type"] - at["DVLlon"] == 1

    # the depth trio, and the altitude-derived trio
    assert [at["Depth_std"], at["Depth_Source"]] == [at["Depth"] + 1, at["Depth"] + 2]
    assert [at["Width"], at["Area_m2"]] == [at["Altitude"] + 1, at["Altitude"] + 2]

    # raw depth inputs together, after the values derived from them
    for raw in ("Relative_alt_m", "VFR_alt", "NEDz", "Pressure_abs_hPa"):
        assert at[raw] > at["Depth_Source"]


def test_the_written_csv_uses_that_order(dive, tmp_path):
    df, res = dive
    r = export_transect(df, [("10:00:05", "10:00:20")], 1, "T1", "Site",
                        tmp_path, dvl_source=res.dvl_source)
    assert r.path is not None

    written = pd.read_csv(r.path)
    assert list(written.columns) == OUTPUT_COLUMNS
    assert written["Site_name"].eq("Site").all()
    assert written["Transect_ID"].eq("T1").all()
    assert written["Transect_number"].eq(1).all()
    assert written["DVL_source"].eq("VISION_POSITION_DELTA").all()


def test_a_transect_spans_several_windows(dive, tmp_path):
    df, res = dive
    r = export_transect(df, [("10:00:05", "10:00:14"), ("10:00:30", "10:00:39")],
                        2, "T2", "Site", tmp_path)
    written = pd.read_csv(r.path)

    assert len(written) == 20                       # 10 + 10 seconds
    times = set(written["Time"])
    assert "10:00:07" in times and "10:00:33" in times
    assert "10:00:20" not in times                  # the gap really is a gap


def test_windows_outside_the_log_produce_no_file(dive, tmp_path):
    df, _ = dive
    r = export_transect(df, [("23:00:00", "23:30:00")], 1, "T9", "Site", tmp_path)
    assert r.path is None
    assert "no rows" in r.message


def test_dvl_track_is_zeroed_and_georeferenced_per_transect(dive, tmp_path):
    df, _ = dive
    r = export_transect(df, [("10:00:20", "10:00:40")], 1, "T1", "Site", tmp_path)
    w = pd.read_csv(r.path)

    # local frame restarts at the transect, not at the dive
    assert w["DVLx"].iloc[0] == pytest.approx(0.0)
    assert w["DVLy"].iloc[0] == pytest.approx(0.0)

    # heading north: latitude climbs, longitude holds
    assert w["DVLlat"].is_monotonic_increasing
    assert w["DVLlon"].std() == pytest.approx(0.0, abs=1e-9)

    # 0.5 m/s for 20 s, and roughly 1e-5 deg of latitude per metre
    assert r.distance_m == pytest.approx(10.0, rel=0.05)
    span_m = (w["DVLlat"].iloc[-1] - w["DVLlat"].iloc[0]) * 111_320
    assert span_m == pytest.approx(10.0, rel=0.05)


def test_battery_use_is_measured_from_the_transect_start(dive, tmp_path):
    df, _ = dive
    r = export_transect(df, [("10:00:30", "10:00:40")], 1, "T1", "Site", tmp_path)
    w = pd.read_csv(r.path)

    assert w["Battery_mAh_used"].iloc[0] == pytest.approx(0.0)
    assert w["Battery_mAh_used"].iloc[-1] > 0
    assert w["Battery_mAh_used"].is_monotonic_increasing


def test_track_without_any_fix_still_writes_a_file(builder, tmp_path):
    """No GPS and no EKF: the CSV is written, with a warning, minus lat/lon."""
    b = builder("nofix.mcap")
    for i in range(10):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [0.5, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        b.add(t, "GLOBAL_POSITION_INT", {"lat": 0, "lon": 0, "relative_alt": -2000})
    df = add_empty_tide(read_mcaps([b.close()]).df)

    r = export_transect(df, [("10:00:00", "10:00:09")], 1, "T1", "Site", tmp_path)
    assert r.path is not None
    assert any("seed" in w for w in r.warnings)
    w = pd.read_csv(r.path)
    assert w["DVLlat"].isna().all()
    assert w["DVLx"].notna().all()          # the local track still exists


def test_locked_output_lands_beside_the_original(dive, tmp_path):
    """Excel holding the last run's CSV must not lose this run's work."""
    df, _ = dive
    first = export_transect(df, [("10:00:05", "10:00:15")], 1, "T1", "Site", tmp_path)
    assert first.path.name == "T1.csv"

    # Genuinely locking a file needs a second process, so the fallback naming is
    # asserted directly: after T1.csv exists, the next free name is T1 (1).csv.
    assert _numbered(tmp_path / "T1.csv").name == "T1 (1).csv"
    (tmp_path / "T1 (1).csv").write_text("taken")
    assert _numbered(tmp_path / "T1.csv").name == "T1 (2).csv"


def test_publish_falls_back_when_the_target_cannot_be_replaced(tmp_path, monkeypatch):
    """A WinError 32 on the destination must divert, not raise."""
    src = tmp_path / "new.csv"
    src.write_text("fresh")
    dst = tmp_path / "held.csv"
    dst.write_text("open in Excel")

    real_replace = os.replace

    def refuse(a, b, *args, **kw):
        if str(b) == str(dst):
            err = PermissionError("in use by another process")
            err.winerror = 32
            raise err
        return real_replace(a, b, *args, **kw)

    monkeypatch.setattr(os, "replace", refuse)
    notes: list[str] = []
    landed = publish(src, dst, timeout=0.0, log=notes.append)

    assert landed.name == "held (1).csv"
    assert landed.read_text() == "fresh"
    assert dst.read_text() == "open in Excel"      # the open file is untouched
    assert any("locked" in n for n in notes)


def test_filenames_are_made_safe():
    assert sanitize_filename("EBM/S24:T4") == "EBM_S24_T4"
    assert sanitize_filename("   ") == "transect"
    assert sanitize_filename("T1") == "T1"


def test_mask_is_inclusive_of_both_ends():
    df = pd.DataFrame({"Time": ["10:00:00", "10:00:01", "10:00:02", "10:00:03"]})
    mask = build_transect_mask(df, [("10:00:01", "10:00:02")])
    assert list(mask) == [False, True, True, False]


# ---- the map --------------------------------------------------------------

def _page_data(html: str) -> dict:
    blob = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    assert blob, "the page has no data blob"
    return json.loads(blob.group(1))


def test_map_carries_every_transect_and_source(dive, tmp_path):
    df, res = dive
    frames = []
    for i, window in enumerate([("10:00:05", "10:00:20"), ("10:00:30", "10:00:45")], 1):
        r = export_transect(df, [window], i, f"T{i}", "Site", tmp_path,
                            dvl_source=res.dvl_source)
        frames.append((f"T{i}", pd.read_csv(r.path)))

    html, warnings = mapping.build_map_html(frames, site_name="Site",
                                            survey_date="20260826")
    assert not warnings
    data = _page_data(html)

    assert [t["name"] for t in data["transects"]] == ["T1", "T2"]
    assert data["site"] == "Site"
    for t in data["transects"]:
        assert len(t["tracks"]["dvl"]) > 5
        # GPS and EKF are both a single held fix here, so each collapses to one
        # vertex once consecutive repeats are dropped
        assert len(t["tracks"]["gps"]) == 1
        assert len(t["tracks"]["ekf"]) == 1
        assert t["stats"]["distance_m"] == pytest.approx(7.5, rel=0.1)
        assert t["stats"]["depth_deep_m"] == pytest.approx(5.0, abs=0.01)


def test_map_ignores_null_island(tmp_path):
    """Lat/lon of exactly zero means "no fix", not a point off West Africa."""
    df = pd.DataFrame({
        "Date": ["2026-08-26"] * 3, "Time": ["10:00:00", "10:00:01", "10:00:02"],
        "DVLlat": [47.61, 47.611, 47.612], "DVLlon": [-122.36, -122.361, -122.362],
        "EKFlat": [0.0, 0.0, 0.0], "EKFlon": [0.0, 0.0, 0.0],
    })
    data = _page_data(mapping.build_map_html([("T1", df)])[0])
    assert data["transects"][0]["tracks"]["ekf"] == []
    assert len(data["transects"][0]["tracks"]["dvl"]) == 3


def test_map_json_has_no_nan(tmp_path):
    """NaN is not JSON; a browser rejects the whole page if one gets through."""
    df = pd.DataFrame({
        "Date": ["2026-08-26"] * 3, "Time": ["10:00:00", "10:00:01", "10:00:02"],
        "DVLlat": [47.61, 47.611, 47.612], "DVLlon": [-122.36] * 3,
        "Depth": [np.nan] * 3, "Distance": [np.nan] * 3,
        "Altitude": [np.nan] * 3, "Width": [np.nan] * 3,
        "Area_m2": [np.nan] * 3, "Velocity_mps": [np.nan] * 3,
        "Water_temp_C": [np.nan] * 3,
    })
    html, _ = mapping.build_map_html([("T1", df)])
    assert "NaN" not in html
    data = _page_data(html)
    assert data["transects"][0]["stats"]["depth_deep_m"] is None


def test_map_needs_at_least_one_coordinate():
    df = pd.DataFrame({"Date": ["2026-08-26"], "Time": ["10:00:00"],
                       "DVLlat": [np.nan], "DVLlon": [np.nan]})
    with pytest.raises(ValueError, match="usable coordinates"):
        mapping.build_map_html([("T1", df)])


def test_map_reads_the_older_tlog_column_spellings(tmp_path):
    df = pd.DataFrame({
        "Date": ["2026-08-26"] * 2, "Time": ["10:00:00", "10:00:01"],
        "EKF.lat": [47.61, 47.611], "EKF.lon": [-122.36, -122.361],
    })
    html, _ = mapping.build_map_html([("old", df)])
    assert len(_page_data(html)["transects"][0]["tracks"]["ekf"]) == 2


# ---- the whole run --------------------------------------------------------

def test_run_writes_csvs_and_a_map_without_the_network(builder, tmp_path):
    path = straight_north_dive(builder(), seconds=60).close()
    result = run(
        [path],
        site_name="Centennial_Park",
        survey_date="20260826",
        station_id=None,                       # skip the NOAA lookup
        save_location=tmp_path,
        transects=[TransectSpec("T1", [("10:00:05", "10:00:20")]),
                   TransectSpec("T2", [("10:00:30", "10:00:45")])],
    )

    assert len(result.saved) == 2
    assert not result.skipped
    assert result.map_path is not None and result.map_path.is_file()
    assert not result.tide_ok
    assert (tmp_path / "transects" / "T1.csv").is_file()

    written = pd.read_csv(tmp_path / "transects" / "T1.csv")
    assert written["Depth_std"].isna().all()       # no tide, so no standardisation
    assert "Saved 2 of 2" in "\n".join(result.summary_lines())


def test_transects_keep_their_true_separation(builder, tmp_path):
    """Two transects from one dive must not land on top of each other.

    The surface fix here never moves, which is the normal case for a USBL that
    has not locked. Seeding each transect at that one fix would stack them; the
    DVL frame runs continuously between them and knows how far apart they are.
    """
    path = straight_north_dive(builder(), seconds=120).close()
    result = run([path], site_name="S", survey_date="20260826", station_id=None,
                 save_location=tmp_path, transects=[
                     TransectSpec("T1", [("10:00:05", "10:00:25")]),
                     TransectSpec("T2", [("10:01:30", "10:01:50")]),
                 ], make_map=False)

    assert len(result.saved) == 2
    t1 = pd.read_csv(tmp_path / "transects" / "T1.csv")
    t2 = pd.read_csv(tmp_path / "transects" / "T2.csv")

    # one static surface fix for the whole dive
    assert t1["Latitude"].nunique() == 1
    assert t1["Latitude"].iloc[0] == t2["Latitude"].iloc[0]

    # ... but the tracks are ~50 m apart, because the dive ran north between them
    gap_m = (t2["DVLlat"].iloc[0] - t1["DVLlat"].iloc[0]) * 111_320
    assert gap_m == pytest.approx(0.5 * 85, rel=0.1)
    assert t2["DVLlat"].iloc[0] > t1["DVLlat"].iloc[-1]

    # the local frame is still zeroed per transect, exactly as the tlog tool did
    assert t1["DVLx"].iloc[0] == pytest.approx(0.0)
    assert t2["DVLx"].iloc[0] == pytest.approx(0.0)
    # and each transect's own distance is unaffected by the gap before it
    assert t2["Distance"].sum() == pytest.approx(10.0, rel=0.05)


def test_without_any_fix_the_dive_track_falls_back_per_transect(builder, tmp_path):
    """No seed anywhere means no coordinates, but the CSVs still get written."""
    b = builder("nofix.mcap")
    for i in range(40):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "LOCAL_POSITION_NED", {"x": float(i), "y": 0.0, "z": 1.0})
        b.add(t, "GLOBAL_POSITION_INT", {"lat": 0, "lon": 0, "relative_alt": -2000})
    result = run([b.close()], site_name="S", survey_date="20260826", station_id=None,
                 save_location=tmp_path,
                 transects=[TransectSpec("T1", [("10:00:05", "10:00:20")])],
                 make_map=False)

    assert len(result.saved) == 1
    w = pd.read_csv(result.saved[0].path)
    assert w["DVLlat"].isna().all()
    assert w["DVLx"].notna().all()
    assert any("seed" in x for x in result.warnings)


def test_run_with_no_windows_takes_the_whole_log(builder, tmp_path):
    path = straight_north_dive(builder(), seconds=30).close()
    result = run([path], site_name="S", survey_date="20260826", station_id=None,
                 save_location=tmp_path, transects=[], make_map=False)

    assert len(result.saved) == 1
    assert len(pd.read_csv(result.saved[0].path)) == 30


def test_a_window_through_midnight_selects_both_sides():
    """A transect from 23:50 to 00:10 is twenty minutes, not an empty set.

    UTC's own `Transect` already reads it that way, so the extractor returning
    nothing was a silent disagreement between the two halves of the toolchain
    -- and an empty result looks exactly like a mistyped time.
    """
    import pandas as pd

    from ccr_m2c.transect import build_transect_mask

    df = pd.DataFrame({"Time": ["23:45:00", "23:55:00", "00:05:00",
                                "00:15:00", "12:00:00"]})

    wrapped = build_transect_mask(df, [("23:50:00", "00:10:00")])
    assert list(df.loc[wrapped, "Time"]) == ["23:55:00", "00:05:00"]

    # the ordinary case must be untouched
    same_day = build_transect_mask(df, [("23:45:00", "23:55:00")])
    assert list(df.loc[same_day, "Time"]) == ["23:45:00", "23:55:00"]
    assert int(build_transect_mask(df, [("11:00:00", "13:00:00")]).sum()) == 1
