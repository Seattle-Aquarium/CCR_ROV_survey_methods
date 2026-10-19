"""
The reference parameter file: written as ArduPilot writes one, read back
unchanged, forgiving of hand edits, and the shipped one intact.

The shipped file is checked here as well as the code because it is edited on
GitHub by hand. A typo there should fail a pull request, not surface at the
pier as a check that quietly compared against fewer parameters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import paramfile as P  # noqa: E402
from rov_flight_ops.nav import profiles  # noqa: E402

META = {"vehicle": "Nereo", "firmware": "ArduSub 4.5.7 STABLE",
        "source_flight": "2026-10-15_103918", "source_log": "00000025.BIN"}


def test_written_values_are_the_float32_the_autopilot_holds():
    text = P.render({"ACRO_EXPO": 0.30000001192092896, "ARMING_CHECK": 448.0,
                     "ATC_RAT_PIT_D": 0.0035999999381601810,
                     "AHRS_ORIG_LON": -122.34387207031250}, META)
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body == ["ACRO_EXPO,0.3", "AHRS_ORIG_LON,-122.34387",
                    "ARMING_CHECK,448", "ATC_RAT_PIT_D,0.0036"]


def test_round_trip_keeps_every_value_and_the_header(tmp_path):
    params = {"EK3_SRC1_POSXY": 3.0, "EK3_SRC1_VELXY": 6.0,
              "COMPASS_DEC": 0.26483968, "GPS_TYPE": 14.0}
    path = P.write(tmp_path / "nereo.params", params, META)
    ref = P.read(path)
    assert ref.problems == []
    assert P.diff(params, ref.params) == {}
    for key, value in META.items():
        assert ref.meta[key] == value
    assert ref.meta["count"] == "4"
    assert ref.path == path


def test_volatile_parameters_are_left_out_and_named():
    params = {"STAT_RUNTIME": 163819.0, "BARO1_GND_PRESS": 103821.62,
              "STAT_RESET": 1.0, "EK3_ENABLE": 1.0}
    ref = P.parse(P.render(params, META))
    assert set(ref.params) == {"STAT_RESET", "EK3_ENABLE"}
    # Only the ones this vehicle actually has are listed as left out.
    assert ref.omitted == ("BARO1_GND_PRESS", "STAT_RUNTIME")
    assert ref.meta["count"] == "2"


def test_reads_the_other_layouts_ground_stations_save():
    ref = P.parse("EK3_SRC1_POSXY 6\n"
                  "1\t1\tGPS_TYPE\t14.000000000000000000\t2\n"
                  "VISO_TYPE,1\n")
    assert ref.problems == []
    assert ref.params == {"EK3_SRC1_POSXY": 6.0, "GPS_TYPE": 14.0,
                          "VISO_TYPE": 1.0}


def test_hand_edit_mistakes_are_reported_not_raised():
    ref = P.parse("# vehicle: Nereo\n"
                  "EK3_SRC1_POSXY,6\n"
                  "EK3_SRC1_POSXY,3\n"
                  "VISO_TYPE,one\n"
                  "this is not a parameter\n"
                  "GPS_TYPE,14\n")
    assert ref.params == {"EK3_SRC1_POSXY": 3.0, "GPS_TYPE": 14.0}
    assert len(ref.problems) == 3
    assert any("more than once" in p for p in ref.problems)
    assert any("VISO_TYPE" in p for p in ref.problems)
    assert ref.meta == {"vehicle": "Nereo"}


def test_a_missing_file_is_a_problem_not_an_exception(tmp_path):
    ref = P.read(tmp_path / "nobody.params")
    assert ref.params == {}
    assert ref.problems


def _record(tmp_path, values):
    record = {"flight_id": "2026-10-15_103918",
              "vehicle": {"ardusub": "4.5.7", "ardusub_type": "STABLE",
                          "blueos": "1.4.6", "board": "Navigator"},
              "parameters": {"read": bool(values), "values": values,
                             "read_from": "00000025.BIN",
                             "taken": "2026-10-15T17:47:27+00:00"}}
    path = tmp_path / "flight_2026-10-15_103918.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_flight_record_gives_values_and_provenance(tmp_path):
    params, meta = P.from_flight_record(
        _record(tmp_path, {"EK3_SRC1_POSXY": 3.0}), "Nereo")
    assert params == {"EK3_SRC1_POSXY": 3.0}
    assert meta["firmware"] == "ArduSub 4.5.7 STABLE"
    assert meta["source_flight"] == "2026-10-15_103918"
    assert meta["source_log"] == "00000025.BIN"


def test_a_flight_whose_read_failed_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no parameter values"):
        P.from_flight_record(_record(tmp_path, None), "Nereo")


def test_the_command_reports_what_a_refresh_changes(tmp_path, capsys):
    out = tmp_path / "nereo.params"
    P.write(out, {"EK3_SRC1_POSXY": 6.0, "GPS_TYPE": 14.0}, META)
    source = _record(tmp_path, {"EK3_SRC1_POSXY": 3.0, "GPS_TYPE": 14.0,
                                "STAT_RUNTIME": 5.0})
    assert P.main([str(source), "-o", str(out)]) == 0
    said = capsys.readouterr().out
    assert "1 change(s)" in said
    assert "EK3_SRC1_POSXY" in said
    assert "STAT_RUNTIME" in said                       # named as left out
    assert P.read(out).params == {"EK3_SRC1_POSXY": 3.0, "GPS_TYPE": 14.0}


# --------------------------------------------------------------------------
#  the file that ships
# --------------------------------------------------------------------------


def test_the_shipped_nereo_reference_is_intact():
    ref = P.read(P.reference_path("Nereo"))
    assert ref.problems == []
    assert ref.meta.get("vehicle") == "Nereo"
    assert ref.meta.get("firmware", "").startswith("ArduSub ")
    assert ref.meta.get("source_log")
    assert int(ref.meta["count"]) == len(ref.params)
    assert not set(ref.params) & set(P.OMIT)


def test_the_shipped_reference_has_every_parameter_the_profiles_check():
    ref = P.read(P.reference_path("Nereo"))
    for profile in profiles.PROFILES.values():
        for req in profile.requirements:
            if not req.absent_ok:
                assert req.param in ref.params, (profile.key, req.param)
