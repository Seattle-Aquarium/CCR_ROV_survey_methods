"""
Guided diagnosis, "what changed?", and the diagnostic snapshot.

The thing being protected here is the line between an observation and a guess.
A program that says "the acoustics failed because the parameters changed" on
the evidence that the two happened eleven seconds apart is worse than one that
says nothing, because an operator will act on it.
"""

from __future__ import annotations

import json

import pytest

from rov_flight_ops.nav import changed as CH
from rov_flight_ops.nav import diagnose as DG
from rov_flight_ops.nav import model as M
from rov_flight_ops.nav import snapshot as SN
from rov_flight_ops.nav import trust as TR
from rov_flight_ops.nav.model import Fix, LinkState, NavSnapshot, Quality

NOW = 10_000.0


def _snap(*, abs_=0.0, rel=1.0, const=0.0, acoustic_age=None,
          acoustic_invalid=False, vessel_age=None, dvl_age=0.2,
          dvl_invalid=False, connected=True, params=None,
          message_type=None) -> NavSnapshot:
    s = NavSnapshot()
    s.link = LinkState(mode="live", connected=connected, host="192.168.2.2")
    s.ekf = {
        "horiz_pos_abs": M.good(abs_, recv_mono=NOW),
        "horiz_pos_rel": M.good(rel, recv_mono=NOW),
        "const_pos_mode": M.good(const, recv_mono=NOW),
    }
    s.rov_fix = Fix(lat=47.6075661, lon=-122.3438752, kind="ekf",
                    quality=Quality.OK, recv_mono=NOW)
    if dvl_invalid:
        s.dvl = {"bottom_lock": M.invalid("no bottom lock", value=0)}
    elif dvl_age is not None:
        s.dvl = {"bottom_lock": M.good(1.0, recv_mono=NOW - dvl_age)}
    if message_type:
        s.dvl["message_type"] = M.good(message_type, recv_mono=NOW)
    if acoustic_invalid:
        s.ugps = {"fix": M.invalid("no acoustic position", value=0)}
    elif acoustic_age is not None:
        s.ugps = {"fix": M.good("fix type 3", recv_mono=NOW - acoustic_age)}
    if vessel_age is not None:
        s.vessel = {"position": M.good((47.6, -122.3),
                                       recv_mono=NOW - vessel_age),
                    "heading": M.good(180.0, recv_mono=NOW - vessel_age)}
    s.params = dict(params or {})
    return s


def _keys(report) -> set[str]:
    return {f.key for f in report.findings}


# --------------------------------------------------------------------------
#  Observation is not cause
# --------------------------------------------------------------------------


def test_the_example_from_the_brief_comes_out_as_three_observations():
    """"Vessel position and heading are fresh; acoustic fixes stopped 12
    seconds ago; DVL measurements remain available." Three facts, and what is
    still working is as important as what is not."""
    s = _snap(abs_=1.0, acoustic_age=12.0, vessel_age=0.5, dvl_age=0.2)
    rep = DG.diagnose(s, NOW, profile_key="acoustic")

    keys = _keys(rep)
    assert "vessel_ok" in keys
    assert "acoustic_stale" in keys
    assert "dvl_ok" in keys

    acoustic = next(f for f in rep.findings if f.key == "acoustic_stale")
    assert "12 s ago" in acoustic.observed
    assert rep.severity == DG.PROBLEM


def test_a_finding_states_what_was_measured_before_anything_else():
    """`observed` is always a measurement. `suspected` is optional and is
    always rendered as a possibility, never as the finding itself."""
    s = _snap(abs_=1.0, acoustic_age=30.0, vessel_age=0.5)
    rep = DG.diagnose(s, NOW, profile_key="acoustic")
    f = next(x for x in rep.findings if x.key == "acoustic_stale")

    assert f.observed.startswith("acoustic fixes stopped")
    assert f.suspected                      # the vessel being fresh supports one
    assert "possibly:" in f.line()
    assert f.line().startswith(f.observed)


def test_no_cause_is_offered_when_nothing_supports_one():
    """With the vessel stale as well, the acoustic gap could be anything in
    the chain, so the finding says only what it saw."""
    s = _snap(abs_=1.0, acoustic_age=30.0, vessel_age=40.0)
    rep = DG.diagnose(s, NOW, profile_key="acoustic")
    f = next(x for x in rep.findings if x.key == "acoustic_stale")
    assert f.suspected == ""
    assert f.line() == f.observed


def test_nothing_offered_is_something_this_program_does():
    """No origin reset, no extension restart, no reboot, no firmware. Every
    action is a place for a person to go and look."""
    banned = ("restart", "reboot", "reset", "upgrade", "repair", "fix it",
              "clear the", "force")
    for action in DG.ALL_ACTIONS:
        text = f"{action.label} {action.where}".lower()
        for word in banned:
            assert word not in text, (action.label, word)


def test_the_same_action_from_two_findings_is_one_action():
    s = _snap(abs_=1.0, acoustic_invalid=True, vessel_age=40.0, dvl_age=0.2)
    rep = DG.diagnose(s, NOW, profile_key="acoustic")
    actions = rep.actions()
    assert len(actions) == len(set(actions))


# --------------------------------------------------------------------------
#  Distinct failures read distinctly
# --------------------------------------------------------------------------


def test_lost_gga_lost_hdt_lost_acoustics_and_lost_dvl_are_four_diagnoses():
    """The brief's requirement, and the thing a single "navigation degraded"
    banner cannot do."""
    base = dict(abs_=1.0, acoustic_age=0.5, vessel_age=0.5, dvl_age=0.2)

    lost_acoustic = DG.diagnose(_snap(**{**base, "acoustic_age": 30.0}),
                                NOW, profile_key="acoustic")
    lost_dvl = DG.diagnose(_snap(**{**base, "dvl_invalid": True,
                                    "dvl_age": None}),
                           NOW, profile_key="acoustic")
    lost_vessel = DG.diagnose(_snap(**{**base, "vessel_age": 30.0}),
                              NOW, profile_key="acoustic")
    no_aiding = DG.diagnose(_snap(**{**base, "const": 1.0}),
                            NOW, profile_key="acoustic")

    assert "acoustic_stale" in _keys(lost_acoustic)
    assert "dvl_lock" in _keys(lost_dvl)
    assert "vessel_gga" in _keys(lost_vessel)
    assert "ekf_const" in _keys(no_aiding)

    # And each says something the others do not.
    texts = [" ".join(f.observed for f in r.findings)
             for r in (lost_acoustic, lost_dvl, lost_vessel, no_aiding)]
    assert len(set(texts)) == 4


def test_a_dvl_only_dive_is_not_told_its_acoustics_are_missing():
    s = _snap(rel=1.0, dvl_age=0.2)
    rep = DG.diagnose(s, NOW, profile_key="dvl")
    assert not any(k.startswith("acoustic") for k in _keys(rep))
    assert not any(k.startswith("vessel") for k in _keys(rep))


def test_position_delta_is_an_advisory_with_a_place_to_go():
    s = _snap(rel=1.0, message_type="POSITION_DELTA",
              params={"EK3_SRC1_POSXY": 6.0})
    rep = DG.diagnose(s, NOW, profile_key="dvl")
    f = next(x for x in rep.findings if x.key == "dvl_message")
    assert f.severity == DG.ADVISORY
    assert "usable dead-reckoned position" in f.observed


def test_a_missing_origin_is_a_problem_and_points_at_the_start_dialog():
    rep = DG.diagnose(_snap(rel=1.0), NOW, profile_key="dvl",
                      origin_confirmed=False)
    f = next(x for x in rep.findings if x.key == "origin")
    assert f.severity == DG.PROBLEM
    assert any(a.target == "start" for a in f.actions)


def test_a_healthy_dive_produces_no_problems():
    s = _snap(abs_=1.0, acoustic_age=0.5, vessel_age=0.5, dvl_age=0.2)
    rep = DG.diagnose(s, NOW, profile_key="acoustic", origin_confirmed=True)
    assert rep.severity == DG.INFO
    assert rep.state == TR.ABSOLUTE


# --------------------------------------------------------------------------
#  What changed?
# --------------------------------------------------------------------------


def _events(*pairs) -> list[dict]:
    return [{"mono": mono, "kind": kind, "t": "2026-09-22T12:00:00Z"}
            for mono, kind in pairs]


def test_the_window_aligns_by_offset_and_keeps_time_order():
    rows = _events((NOW - 45, "param_change"), (NOW - 12, "message_gap"),
                   (NOW + 5, "telemetry_restored"), (NOW - 400, "app_started"))
    win = CH.around(rows, NOW)

    assert [r.kind for r in win.rows] == [
        "param_change", "message_gap", "telemetry_restored"]
    assert win.rows[0].offset_s == pytest.approx(-45.0)
    assert win.rows[-1].offset_s == pytest.approx(5.0)


def test_events_are_grouped_by_what_an_operator_would_call_them():
    rows = _events((NOW - 5, "param_change"), (NOW - 3, "message_gap"),
                   (NOW - 1, "plan_edit"), (NOW, "something_new"))
    groups = CH.around(rows, NOW).by_group()
    assert set(groups) == {"configuration", "message", "plan", "other"}


def test_the_summary_never_claims_a_cause():
    """The single most important property of this whole feature."""
    rows = _events((NOW - 11, "param_change"), (NOW, "message_gap"))
    win = CH.around(rows, NOW)
    text = (win.summary() + " " + win.note + " "
            + " ".join(r.line() for r in win.rows)).lower()

    for word in ("because", "caused", "due to", "result of", "led to",
                 "explains"):
        assert word not in text, word
    assert "proximity in time is not a cause" in win.summary().lower()


def test_an_empty_window_says_what_that_does_and_does_not_rule_out():
    win = CH.around(_events((NOW - 500, "app_started")), NOW)
    assert win.rows == []
    assert "does not rule out" in win.note
    assert "itself worth knowing" in win.summary()


def test_the_window_is_bounded_and_keeps_the_nearest_events():
    rows = _events(*[(NOW - 60 + i * 0.1, "message_gap")
                     for i in range(900)])
    win = CH.around(rows, NOW)
    assert len(win.rows) == CH.MAX_ROWS
    assert win.truncated
    # Still in time order after the nearest-first selection.
    offsets = [r.offset_s for r in win.rows]
    assert offsets == sorted(offsets)


def test_alignment_uses_the_monotonic_clock():
    """A laptop that has just synchronised can move its wall clock backwards
    by seconds, which would reorder the very events being examined."""
    rows = [{"mono": NOW - 5, "kind": "message_gap", "t": "2026-01-01T00:00:00Z"},
            {"mono": NOW + 1, "kind": "param_change", "t": "1999-01-01T00:00:00Z"}]
    win = CH.around(rows, NOW)
    assert [r.kind for r in win.rows] == ["message_gap", "param_change"]


def test_only_recorded_degradations_are_offered_to_ask_about():
    rows = _events((1.0, "track_break"), (2.0, "plan_edit"),
                   (3.0, "position_jump"))
    got = CH.degradations(rows)
    assert [r["kind"] for r in got] == ["track_break", "position_jump"]


# --------------------------------------------------------------------------
#  The snapshot
# --------------------------------------------------------------------------


def test_the_snapshot_carries_the_parameters_with_their_age_and_provenance():
    """A value read twenty minutes ago is evidence about twenty minutes ago.
    Presenting it as the vehicle's current settings would be worse than
    leaving it out."""
    s = _snap(rel=1.0, params={"EK3_SRC1_POSXY": 6.0, "VISO_TYPE": 1.0})
    bundle = SN.build(s, NOW, profile_key="dvl", params_age_s=1230.0)

    cfg = bundle["configuration"]
    assert cfg["parameters"]["EK3_SRC1_POSXY"] == 6.0
    assert cfg["parameters_age_s"] == 1230.0
    assert "not a measurement of the vehicle's current settings" \
        in cfg["provenance"]
    assert "nothing was sent to the vehicle" in cfg["provenance"]


def test_the_snapshot_separates_observations_from_possibilities():
    s = _snap(abs_=1.0, acoustic_age=30.0, vessel_age=0.5)
    bundle = SN.build(s, NOW, profile_key="acoustic")
    v = bundle["verdict"]
    assert "not a diagnosis" in v["caveat"]
    for f in v["findings"]:
        assert f["observed"]
        assert "suspected" in f


def test_the_snapshot_carries_a_bounded_window_and_a_manifest():
    rows = _events(*[(NOW - 50 + i * 0.05, "message_gap") for i in range(600)])
    s = _snap(rel=1.0)
    bundle = SN.build(s, NOW, events=rows, profile_key="dvl")

    assert len(bundle["window"]["events"]) <= CH.MAX_ROWS
    assert bundle["window"]["truncated"] is True
    assert bundle["manifest"]["program"] == "rov_flight_ops"
    assert bundle["schema"] == SN.VERSION


def test_the_snapshot_is_json_and_survives_a_round_trip(tmp_path):
    s = _snap(abs_=1.0, acoustic_age=3.0, vessel_age=0.5,
              params={"EK3_SRC1_POSXY": 6.0})
    s.messages = {"ATTITUDE": M.MessageHealth(name="ATTITUDE", counter=12,
                                              frequency=10.0, seen=True)}
    bundle = SN.build(s, NOW, profile_key="acoustic", origin_confirmed=True)

    path = SN.save(bundle, tmp_path)
    assert path is not None and path.is_file()
    back = json.loads(path.read_text(encoding="utf-8"))
    assert back["profile"] == "acoustic"
    assert back["position"]["aiding_mode"] == TR.ABSOLUTE
    assert "ATTITUDE" in back["streams"]


def test_saving_never_raises_even_when_it_cannot_write(tmp_path):
    """An operator reaches for this when something has already gone wrong.
    A second failure at that moment helps nobody."""
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x", encoding="utf-8")
    assert SN.save({"a": 1}, blocker / "under") is None


def test_the_snapshot_does_not_start_a_collector_of_its_own(tmp_path):
    """It reads what is already being written. A snapshot that collected its
    own data would change the thing it was measuring, and would be slowest on
    the laptop that was already struggling."""
    import inspect

    src = inspect.getsource(SN)
    for forbidden in ("NavCollector", "Mavlink2Rest", "requests.",
                      "urllib.request", "Thread("):
        assert forbidden not in src, forbidden
    del tmp_path


def test_a_snapshot_with_no_session_log_still_says_something(tmp_path):
    s = _snap(rel=1.0)
    bundle = SN.from_session_file(s, NOW, tmp_path / "missing.jsonl",
                                  profile_key="dvl")
    assert bundle["window"]["events"] == []
    assert "does not rule out" in bundle["window"]["note"]
