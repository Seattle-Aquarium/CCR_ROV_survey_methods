"""
What the position on the map is resting on, and how the track says so.

The distinctions here are the ones that cost a day of coordinates when they
were conflated: aiding mode is not freshness, relative aiding is not "no
coordinates", and an acoustic fix that arrived is not an acoustic fix the
estimator accepted.
"""

from __future__ import annotations

import time

import pytest

from rov_flight_ops.nav import model as M
from rov_flight_ops.nav import trust as TR
from rov_flight_ops.nav.model import Fix, NavSnapshot, Quality

NOW = 1_000.0


def _snap(*, abs_=0.0, rel=0.0, const=0.0, fix_kind="ekf",
          fix_quality=Quality.OK, acoustic_age=None, acoustic_invalid=False,
          has_fix=True) -> NavSnapshot:
    s = NavSnapshot()
    s.ekf = {
        "horiz_pos_abs": M.good(abs_, recv_mono=NOW),
        "horiz_pos_rel": M.good(rel, recv_mono=NOW),
        "const_pos_mode": M.good(const, recv_mono=NOW),
    }
    if has_fix:
        s.rov_fix = Fix(lat=47.6075661, lon=-122.3438752, kind=fix_kind,
                        quality=fix_quality, recv_mono=NOW)
    if acoustic_invalid:
        s.ugps = {"fix": M.invalid("no acoustic position", value=0)}
    elif acoustic_age is not None:
        s.ugps = {"fix": M.good("fix type 3", recv_mono=NOW - acoustic_age)}
    return s


# --------------------------------------------------------------------------
#  Aiding mode
# --------------------------------------------------------------------------


def test_constant_position_beats_every_other_flag():
    """`EKF_CONST_POS_MODE` means no horizontal aiding at all, whatever else
    the report says, so it is checked first."""
    s = _snap(abs_=1.0, rel=1.0, const=1.0)
    mode, note = TR.aiding_mode(s)
    assert mode == TR.NO_AIDING
    assert "no horizontal aiding" in note


def test_relative_aiding_is_a_working_state_not_a_fault():
    """`getLLH` returns the origin plus the relative offset whenever
    `horiz_pos_rel` is set, so this is a usable dead-reckoned position."""
    mode, note = TR.aiding_mode(_snap(rel=1.0))
    assert mode == TR.RELATIVE
    assert "dead-reckoned" in note
    assert "usable" in note


def test_no_estimator_report_is_unknown_rather_than_bad():
    s = NavSnapshot()
    mode, note = TR.aiding_mode(s)
    assert mode == TR.UNKNOWN
    assert "EKF_STATUS_REPORT" in note


def test_the_matrix_and_the_map_share_one_implementation():
    """Two copies of this judgement drifting apart is the whole reason it
    lives in `nav/` rather than in the widget."""
    from rov_flight_ops.gui import navstatus

    assert navstatus.aiding_mode is TR.aiding_mode


# --------------------------------------------------------------------------
#  Acoustic fixes: received is not accepted
# --------------------------------------------------------------------------


def test_the_acoustic_clock_measures_reception_and_says_so():
    s = _snap(abs_=1.0, acoustic_age=2.0)
    assert TR.acoustic_age_s(s, NOW) == pytest.approx(2.0)
    line = TR.acoustic_line(s, NOW)
    assert "received" in line
    assert "not confirmed accepted" in line, line


def test_an_invalid_acoustic_solution_does_not_reset_the_clock():
    """The extension sets fix_type 0 when the solution is no good. Counting
    that as a fix would show a healthy age for a position nothing is
    correcting."""
    s = _snap(abs_=1.0, acoustic_invalid=True)
    assert TR.acoustic_age_s(s, NOW) is None
    assert "invalid" in TR.acoustic_line(s, NOW)


def test_never_having_had_a_fix_reads_differently_from_having_lost_one():
    never = TR.acoustic_line(_snap(rel=1.0), NOW)
    lost = TR.acoustic_line(_snap(abs_=1.0, acoustic_age=95.0), NOW)
    assert "no acoustic fixes have been received" in never
    assert "2 min" in lost


@pytest.mark.parametrize("age, wanted", [(3.0, "s ago"), (20.0, "20 s"),
                                         (300.0, "5 min")])
def test_the_age_is_phrased_for_what_it_calls_for(age, wanted):
    assert wanted in TR.acoustic_line(_snap(abs_=1.0, acoustic_age=age), NOW)


# --------------------------------------------------------------------------
#  Track state
# --------------------------------------------------------------------------


def test_a_stale_position_is_degraded_however_good_the_aiding_was():
    s = _snap(abs_=1.0, acoustic_age=1.0, fix_quality=Quality.STALE)
    state, note = TR.track_state(s, NOW, profile_key="acoustic")
    assert state == TR.DEGRADED
    assert "stale" in note


def test_dead_reckoning_is_relative_and_labelled_as_such():
    s = _snap(rel=1.0, fix_kind="dead")
    state, note = TR.track_state(s, NOW)
    assert state == TR.RELATIVE
    assert "dead-reckoned from the confirmed origin" in note


def test_absolute_aiding_without_a_recent_acoustic_fix_is_degraded():
    """In the acoustic profile the estimator can hold an absolute position
    long after the corrections stopped. The flag is true; the position is
    drifting. Colouring that as healthy is how an operator keeps trusting a
    fix that stopped being corrected minutes ago."""
    fresh = _snap(abs_=1.0, acoustic_age=1.0)
    stale = _snap(abs_=1.0, acoustic_age=30.0)
    assert TR.track_state(fresh, NOW, profile_key="acoustic")[0] == TR.ABSOLUTE
    state, note = TR.track_state(stale, NOW, profile_key="acoustic")
    assert state == TR.DEGRADED
    assert "30 s ago" in note


def test_the_acoustic_test_is_not_applied_to_a_dvl_only_dive():
    """There are no acoustics to be stale in the DVL profile, and marking
    every DVL dive degraded for missing them would make the colour useless."""
    s = _snap(abs_=1.0)
    assert TR.track_state(s, NOW, profile_key="dvl")[0] == TR.ABSOLUTE


def test_no_position_at_all_is_unknown_not_degraded():
    state, note = TR.track_state(_snap(rel=1.0, has_fix=False), NOW)
    assert state == TR.UNKNOWN and note == "no position"


def test_the_worst_state_in_a_run_is_the_one_reported():
    assert TR.summarise([TR.ABSOLUTE, TR.RELATIVE]) == TR.RELATIVE
    assert TR.summarise([TR.ABSOLUTE, TR.DEGRADED, TR.RELATIVE]) == TR.DEGRADED
    assert TR.summarise([TR.ABSOLUTE]) == TR.ABSOLUTE
    assert TR.summarise([]) == TR.UNKNOWN


# --------------------------------------------------------------------------
#  The track that gets drawn
# --------------------------------------------------------------------------


def test_a_track_point_keeps_the_state_it_was_recorded_with():
    """Recoloured from the present state, a track would quietly relabel
    history: an hour of good acoustic work would turn amber the moment the
    acoustics dropped out at the end of the dive."""
    from rov_flight_ops.nav.collector import TrackPoint

    p = TrackPoint(47.6, -122.3, 1.0, 2.0, 0, "ekf", TR.ABSOLUTE)
    assert p.trust == TR.ABSOLUTE
    # And it survives having no state given, rather than failing.
    assert TrackPoint(47.6, -122.3, 1.0, 2.0, 0).trust == TR.UNKNOWN


def test_a_jump_records_what_the_position_rested_on_either_side():
    from rov_flight_ops.nav.collector import Jump

    j = Jump(mono=1.0, wall=time.time(), metres=42.0, seconds=0.5, segment=3,
             from_trust=TR.ABSOLUTE, to_trust=TR.DEGRADED)
    line = j.line()
    assert "42 m in 0.5 s" in line
    assert "absolute to degraded" in line

    same = Jump(mono=1.0, wall=time.time(), metres=9.0, seconds=0.2, segment=1,
                from_trust=TR.RELATIVE, to_trust=TR.RELATIVE)
    assert "relative to relative" not in same.line()
    assert "(relative)" in same.line()


def test_every_state_has_a_colour_and_a_word():
    navmap = pytest.importorskip("rov_flight_ops.gui.navmap")

    for state in TR.STATES:
        assert state in navmap.TRUST_COLOURS, state
        assert navmap.TRUST_WORDS.get(state), state
    # Every state must be distinguishable from every other **in the mode the
    # operator is actually looking at**. Comparing the (light, dark) pairs is
    # not enough and was the bug: `ok` and `accent` differ as pairs but are
    # both Algae in dark mode, so "acoustically aided" and "dead-reckoned"
    # drew as the same green -- the one distinction the colouring is for.
    for index, mode in ((0, "light"), (1, "dark")):
        styles = {}
        for s in TR.STATES:
            colour = navmap.TRUST_COLOURS[s]
            if isinstance(colour, (list, tuple)):
                colour = colour[index]
            styles[s] = (str(colour).upper(), navmap.TRUST_DASH.get(s))
        assert len(set(styles.values())) == len(TR.STATES), (mode, styles)


def test_a_run_of_track_takes_the_style_of_what_it_rested_on():
    """The whole point: a dive that was acoustically aided and then was not
    must not draw as one confident line."""
    navmap = pytest.importorskip("rov_flight_ops.gui.navmap")

    assert navmap.trust_colour(TR.ABSOLUTE) != navmap.trust_colour(TR.DEGRADED)
    assert navmap.trust_dash(TR.ABSOLUTE) is None
    assert navmap.trust_dash(TR.NO_AIDING) is not None
    # An unrecognised state falls back to muted rather than raising in a draw.
    assert navmap.trust_colour("nonsense") == navmap.trust_colour(TR.UNKNOWN)
