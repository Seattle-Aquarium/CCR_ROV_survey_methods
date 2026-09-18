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
    OUTPUT_COLUMNS, make_transect_id, build_transect_mask, export_transect,
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


# ---- transect IDs ---------------------------------------------------------

def test_the_survey_code_is_given_once_and_the_ordinal_filled_in():
    """Typing the whole ID per transect is how EBM_W25_T3 ends up beside
    EMB_W25_T4, with nothing downstream able to tell they are one survey."""
    assert make_transect_id("EBM_W25", 1) == "EBM_W25_T1"
    assert make_transect_id("EBM_W25", 2) == "EBM_W25_T2"


def test_a_name_from_the_plan_keeps_its_own_wording():
    assert make_transect_id("EBM_W25", 3, "T2") == "EBM_W25_T2"
    assert make_transect_id("EBM_W25", 4, "deep_pass") == "EBM_W25_deep_pass"


def test_applying_the_prefix_twice_does_not_double_it():
    """Re-running over a plan whose names are already qualified is normal --
    it happens every time a flight is reprocessed."""
    once = make_transect_id("EBM_W25", 1, "T1")
    assert make_transect_id("EBM_W25", 1, once) == once == "EBM_W25_T1"


def test_no_prefix_leaves_a_plain_ordinal():
    assert make_transect_id("", 4) == "T4"
    assert make_transect_id("   ", 5, "T5") == "T5"


def test_a_trailing_separator_is_not_doubled():
    assert make_transect_id("EBM_W25_", 1) == "EBM_W25_T1"


def test_the_id_reaches_the_csv_and_the_filename(dive, tmp_path):
    df, res = dive
    tid = make_transect_id("EBM_W25", 1)
    r = export_transect(df, [("10:00:05", "10:00:20")], 1, tid, "Site", tmp_path,
                        dvl_source=res.dvl_source)
    assert r.path.name == "EBM_W25_T1.csv"
    assert pd.read_csv(r.path)["Transect_ID"].eq("EBM_W25_T1").all()


# ---- how the DVL track gets its coordinates -------------------------------
#
# Two behaviours that pull in opposite directions, so both are pinned here.
# Anchoring each transect to its own fix keeps the DVL's drift bounded by that
# transect; propagating one track across the dive keeps the transects' true
# separation. Choosing the wrong one is not subtle -- on 2025-08-14 it put the
# later transects 114 m from their GPS.

def _moving_gps_dive(b, *, seconds=60, start=BASE_EPOCH):
    """Like straight_north_dive, but with a surface fix that tracks."""
    for i in range(seconds):
        t = start + i
        for k in range(10):
            b.add(t + k / 10, "ATTITUDE",
                  {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        for k in range(5):
            b.add(t + k / 5, "VISION_POSITION_DELTA",
                  {"time_delta_usec": 200000, "position_delta": [0.1, 0.0, 0.0],
                   "confidence": 99.0}, sysid=255, compid=0)
        # ~1 m of northward movement per second, matching the DVL
        b.add(t, "GPS_RAW_INT",
              {"lat": 476176249 + i * 90, "lon": -1223610207, "alt": 0,
               "fix_type": {"type": "GPS_FIX_TYPE_3D_FIX"},
               "satellites_visible": 12})
        b.add(t, "GLOBAL_POSITION_INT",
              {"lat": 0, "lon": 0, "relative_alt": -5000})
        b.add(t, "RANGEFINDER", {"distance": 2.0, "voltage": 0})
    return b.close()


def test_a_static_fix_is_not_mistaken_for_a_tracking_one(builder):
    """A UGPS with no lock injects one coordinate for the whole recording."""
    from ccr_m2c.transect import has_live_fix
    df = read_mcaps([straight_north_dive(builder(), seconds=30).close()]).df
    assert not has_live_fix(df)


def test_a_tracking_fix_is_recognised(builder):
    from ccr_m2c.transect import has_live_fix
    df = read_mcaps([_moving_gps_dive(builder("m.mcap"), seconds=30)]).df
    assert has_live_fix(df)


def test_with_a_tracking_fix_each_transect_starts_on_its_own(builder, tmp_path):
    """The regression this exists for: a later transect must not carry the
    dive's accumulated dead reckoning."""
    path = _moving_gps_dive(builder("m.mcap"), seconds=120)
    run([path], site_name="S", survey_date="20260826", station_id=None,
        save_location=tmp_path, make_map=False,
        transects=[TransectSpec("T1", [("10:00:05", "10:00:25")]),
                   TransectSpec("T2", [("10:01:30", "10:01:55")])])

    for name in ("T1", "T2"):
        w = pd.read_csv(tmp_path / "transects" / f"{name}.csv")
        first = w.dropna(subset=["Latitude", "DVLlat"]).iloc[0]
        gap_m = abs(first["DVLlat"] - first["Latitude"]) * 111_320
        assert gap_m < 1.0, f"{name} starts {gap_m:.1f} m from its own fix"


def test_without_a_tracking_fix_the_transects_keep_their_separation(builder, tmp_path):
    """The behaviour the above must not undo: seeding per transect on a static
    fix would stack every transect on one coordinate."""
    path = straight_north_dive(builder(), seconds=120).close()
    run([path], site_name="S", survey_date="20260826", station_id=None,
        save_location=tmp_path, make_map=False,
        transects=[TransectSpec("T1", [("10:00:05", "10:00:25")]),
                   TransectSpec("T2", [("10:01:30", "10:01:55")])])

    a = pd.read_csv(tmp_path / "transects" / "T1.csv")["DVLlat"].dropna()
    b = pd.read_csv(tmp_path / "transects" / "T2.csv")["DVLlat"].dropna()
    apart_m = abs(b.iloc[0] - a.iloc[0]) * 111_320
    assert apart_m > 20, f"the transects were stacked ({apart_m:.1f} m apart)"


# ---- a typed origin, for a dive flown without one --------------------------

def test_a_typed_origin_anchors_a_dive_with_no_fix(builder, tmp_path):
    """Forgot to set the origin in BlueOS: the DVL track is intact but has no
    coordinates. The vessel's position, entered afterwards, gives it some."""
    b = builder("nofix.mcap")
    for i in range(30):
        t = BASE_EPOCH + i
        b.add(t, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        b.add(t, "VISION_POSITION_DELTA",
              {"time_delta_usec": 1000000, "position_delta": [0.5, 0.0, 0.0],
               "confidence": 99.0}, sysid=255, compid=0)
        b.add(t, "GLOBAL_POSITION_INT", {"lat": 0, "lon": 0, "relative_alt": -2000})
    path = b.close()

    result = run([path], site_name="S", survey_date="20260826", station_id=None,
                 save_location=tmp_path, make_map=False, origin=(47.60, -122.35),
                 transects=[TransectSpec("T1", [("10:00:05", "10:00:25")])])

    w = pd.read_csv(tmp_path / "transects" / "T1.csv")
    assert w["DVLlat"].notna().all(), "the origin should have anchored every row"
    assert not any("seed" in x for x in result.warnings)
    # walked north from the origin: latitude climbs from it, longitude holds
    assert w["DVLlat"].iloc[0] == pytest.approx(47.60 + 5 * 0.5 / 111_320, abs=2e-5)
    assert w["DVLlon"].std() == pytest.approx(0.0, abs=1e-9)


def test_a_typed_origin_beats_a_static_fix(builder, tmp_path):
    """The only reason to type one is that the recording's own is missing or
    wrong, so a typed origin wins over a fix that never moved."""
    path = straight_north_dive(builder(), seconds=30).close()     # static fix
    run([path], site_name="S", survey_date="20260826", station_id=None,
        save_location=tmp_path, make_map=False, origin=(48.0, -123.0),
        transects=[TransectSpec("T1", [("10:00:00", "10:00:10")])])

    w = pd.read_csv(tmp_path / "transects" / "T1.csv")
    assert abs(w["DVLlat"].iloc[0] - 48.0) < 1e-3
    assert abs(w["Latitude"].iloc[0] - 47.6176249) < 1e-6      # recording's own, untouched


def test_a_typed_origin_is_ignored_when_the_fix_was_tracking(builder, tmp_path):
    """A USBL knows where the vehicle was; a typed origin does not."""
    path = _moving_gps_dive(builder("m.mcap"), seconds=60)
    result = run([path], site_name="S", survey_date="20260826", station_id=None,
                 save_location=tmp_path, make_map=False, origin=(48.0, -123.0),
                 transects=[TransectSpec("T1", [("10:00:05", "10:00:25")])])

    w = pd.read_csv(tmp_path / "transects" / "T1.csv")
    assert abs(w["DVLlat"].iloc[0] - 47.6176) < 1e-3          # its own fix, not 48.0
    assert any("origin was not used" in x for x in result.warnings)


# ---- the per-transect summary ---------------------------------------------

def test_path_length_ignores_jitter_and_null_island():
    from ccr_m2c.transect import path_length_m
    # 1 m north per step, ten steps
    lat = pd.Series([47.0 + i / 111_320 for i in range(11)])
    lon = pd.Series([-122.0] * 11)
    assert path_length_m(lat, lon) == pytest.approx(10.0, rel=0.01)
    # sub-2 cm wobble on a stationary vehicle adds nothing
    lat = pd.Series([47.0 + (i % 2) * 1e-8 for i in range(50)])
    assert path_length_m(lat, pd.Series([-122.0] * 50)) == pytest.approx(0.0)
    # zeros are "no fix", never a position off West Africa
    assert path_length_m(pd.Series([0.0, 0.0]), pd.Series([0.0, 0.0])) is None


def test_the_summary_reports_what_a_survey_lead_asks_for(dive, tmp_path):
    df, res = dive
    r = export_transect(df, [("10:00:05", "10:00:25")], 1, "T1", "Site", tmp_path,
                        dvl_source=res.dvl_source)
    st = r.stats

    assert st["duration_s"] == 21
    assert st["depth_shallow_m"] == pytest.approx(5.0, abs=0.01)   # positive metres
    assert st["depth_deep_m"] == pytest.approx(5.0, abs=0.01)
    assert st["alt_min_m"] == st["alt_max_m"] == st["alt_mean_m"] == pytest.approx(2.0)
    assert st["dist_dvl_m"] == pytest.approx(r.distance_m)
    assert st["dist_gps_m"] == pytest.approx(0.0)          # the fix never moved
    assert st["dist_ekf_m"] is None or st["dist_ekf_m"] == pytest.approx(0.0)


def test_the_summary_table_renders_and_survives_gaps(dive, tmp_path):
    from ccr_m2c.transect import format_stats_table
    df, res = dive
    r = export_transect(df, [("10:00:05", "10:00:25")], 1, "T1", "Site", tmp_path)
    r.stats["dist_ekf_m"] = None                       # a column with nothing in it

    lines = format_stats_table([r])
    assert len(lines) == 3                             # header, units, one row
    assert "T1" in lines[2] and "--" in lines[2]       # missing shows as --, not a crash
    assert format_stats_table([]) == []
