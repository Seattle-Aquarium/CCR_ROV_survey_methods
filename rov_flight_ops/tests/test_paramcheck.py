"""
The Parameters tab's comparison of the vehicle with its reference file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import paramcheck as PC  # noqa: E402
from rov_flight_ops import paramfile  # noqa: E402

REF = paramfile.read(paramfile.reference_path("Nereo")).params


def _row(cmp: PC.Comparison, name: str) -> PC.Row:
    return next(r for r in cmp.key_rows if r.name == name)


def test_the_reference_matches_itself():
    cmp = PC.compare(dict(REF), REF)
    assert cmp.headline()[0] == "MATCHES"
    assert not cmp.key_differences and not cmp.others
    assert cmp.read == len(REF)


def test_nothing_read_is_no_verdict():
    assert PC.compare({}, REF).headline()[0] == "—"


def test_a_changed_key_parameter_is_counted_and_can_be_set():
    v = dict(REF, JS_GAIN_DEFAULT=0.5)
    cmp = PC.compare(v, REF)
    row = _row(cmp, "JS_GAIN_DEFAULT")
    assert row.state == PC.DIFFERS and row.settable
    assert [r.name for r in cmp.key_differences] == ["JS_GAIN_DEFAULT"]
    assert cmp.headline() == ("DIFFERS", "1 key parameter differs from the reference")


def test_the_origin_belongs_to_the_site_and_is_never_set_from_the_file():
    v = dict(REF, ORIGIN_LAT=47.0, ORIGIN_LON=-122.0)
    cmp = PC.compare(v, REF)
    row = _row(cmp, "ORIGIN_LAT")
    assert row.state == PC.SITE and not row.settable
    assert not cmp.key_differences and not cmp.others


def test_what_the_autopilot_rewrites_by_itself_is_not_a_difference():
    v = dict(REF, STAT_RUNTIME=1e6, STAT_BOOTCNT=999, SR0_EXTRA1=4,
             COMPASS_DEC=0.3, BARO1_GND_PRESS=101_000)
    cmp = PC.compare(v, REF)
    assert not cmp.others
    for name in ("STAT_RUNTIME", "SR0_EXTRA1", "COMPASS_DEC", "BARO1_GND_PRESS"):
        assert name in cmp.ignored


def test_a_serial_port_other_than_the_gcs_ones_is_compared():
    v = dict(REF, SR1_EXTRA1=REF.get("SR1_EXTRA1", 0) + 2)
    assert [r.name for r in PC.compare(v, REF).others] == ["SR1_EXTRA1"]


def test_values_are_compared_as_the_autopilot_holds_them():
    v = dict(REF, EK3_POSNE_M_NSE=0.5000000001, VISO_VEL_M_NSE=0.10000000149011612)
    assert PC.compare(v, REF).headline()[0] == "MATCHES"


def test_a_light_step_follows_the_steps_and_is_not_counted_twice():
    v = dict(REF, JS_LIGHTS_STEPS=4)
    cmp = PC.compare(v, REF)
    lights = next(g for g in cmp.groups if g.name == "Lights")
    steps = [r for r in lights.rows if "step" in r.name]
    assert len(steps) == 2                               # Lights 1 and Lights 2
    assert all(r.derived_differs and r.state == PC.DERIVED for r in steps)
    assert (steps[0].vehicle, steps[0].reference) == (200.0, 100.0)
    assert [r.name for r in cmp.key_differences] == ["JS_LIGHTS_STEPS"]


def test_the_gain_step_is_worked_out_as_ardusub_does():
    cmp = PC.compare(dict(REF), REF)
    gain = next(r for r in cmp.key_rows if r.name == "↳ gain step")
    lo, hi, n = REF["JS_GAIN_MIN"], REF["JS_GAIN_MAX"], REF["JS_GAIN_STEPS"]
    assert gain.vehicle == (hi - lo) / (n - 1) * 100


def test_parameters_on_one_side_only_are_listed_apart():
    v = dict(REF, NEW_PARAM_X=1.0)
    del v["PSC_VELXY_P"]
    cmp = PC.compare(v, REF)
    assert cmp.only_vehicle == ["NEW_PARAM_X"]
    assert cmp.only_reference == ["PSC_VELXY_P"]
    assert not cmp.others


def test_fresh_water_density_is_flagged_whatever_the_file_says():
    assert "Puget Sound" in _row(PC.compare(dict(REF, BARO_SPEC_GRAV=1.0), REF),
                                 "BARO_SPEC_GRAV").advice
    assert _row(PC.compare(dict(REF, BARO_SPEC_GRAV=1.024), REF),
                "BARO_SPEC_GRAV").advice == ""


def test_enumerations_read_as_words():
    cmp = PC.compare(dict(REF), REF)
    assert "EKF3" in _row(cmp, "AHRS_EKF_TYPE").vehicle_text
    assert _row(cmp, "EK3_SRC1_VELXY").vehicle_text.endswith("ExternalNav")
