"""
Guided diagnosis: what is observably true, and what to do about it.

There is no "repair navigation" button here and there will not be one. The
useful thing to hand an operator on a moving deck is not a verdict, it is the
set of observations that led to one and a short list of the next things worth
looking at.

**Observation and suspected cause are kept apart, in the type system.** A
`Finding` always carries what was measured. It carries a cause only when
something actually implies one, and the cause is rendered differently so that
"acoustic fixes stopped 12 s ago" is never read as "the acoustic topside has
failed". Those call for different actions, and on a boat the wrong one costs
the dive.

**Nothing here acts.** Every action is a thing for a person to do -- open a
page, look at an extension, save a bundle. No origin reset, no extension
restart, no reboot, no firmware anything, and nothing that touches the vehicle
at all. A warning that a program can clear by itself is a warning the program
should not have raised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import trust as TR
from .model import Quality

#: How stale a stream may be before it is worth mentioning, per kind. These
#: are "several missed updates", not "one late one": a threshold of one period
#: turns a healthy link into a wall of amber.
FRESH_S = {
    "acoustic": 4.0,
    "vessel": 5.0,
    "dvl": 2.0,
    "estimator": 3.0,
    "attitude": 1.5,
}

#: Severity, worst last.
INFO = "info"
ADVISORY = "advisory"
PROBLEM = "problem"
SEVERITY_ORDER = (INFO, ADVISORY, PROBLEM)


@dataclass(frozen=True)
class Action:
    """Something for a person to do. Never something this program does.

    `where` names the place to go, because "check the DVL" and "check the DVL
    in the Water Linked extension's own BlueOS page" are different amounts of
    help at 0800 on a deck.
    """

    label: str
    where: str = ""
    #: A key the UI can bind to a button it already has, or "" for advice
    #: with nowhere in particular to go.
    target: str = ""


@dataclass
class Finding:
    """One observation, and optionally what it might mean.

    `observed` is always filled and is always a statement of fact about a
    measurement. `suspected` is filled only when an inference is actually
    supported, and is always phrased as a possibility.
    """

    key: str
    observed: str
    severity: str = ADVISORY
    suspected: str = ""
    actions: tuple[Action, ...] = ()

    def line(self) -> str:
        if not self.suspected:
            return self.observed
        return f"{self.observed}  —  possibly: {self.suspected}"


@dataclass
class Report:
    """What the dive looks like right now."""

    headline: str = ""
    state: str = TR.UNKNOWN
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> str:
        worst = INFO
        for f in self.findings:
            if SEVERITY_ORDER.index(f.severity) > SEVERITY_ORDER.index(worst):
                worst = f.severity
        return worst

    def actions(self) -> list[Action]:
        """Every suggested action, in order, without duplicates."""
        out: list[Action] = []
        for f in self.findings:
            for a in f.actions:
                if a not in out:
                    out.append(a)
        return out


# --------------------------------------------------------------------------
#  Reading the snapshot
# --------------------------------------------------------------------------


def _age(reading, now: float) -> float | None:
    if reading is None or reading.recv_mono is None:
        return None
    return max(0.0, now - reading.recv_mono)


def _fresh(reading, now: float, kind: str) -> bool:
    age = _age(reading, now)
    return (age is not None and age <= FRESH_S.get(kind, 5.0)
            and reading.quality is Quality.OK)


def _secs(age: float | None) -> str:
    if age is None:
        return "never"
    if age < 90:
        return f"{age:.0f} s ago"
    return f"{age / 60:.0f} min ago"


def diagnose(s, now: float, *, profile_key: str = "",
             origin_confirmed: bool | None = None) -> Report:
    """The whole picture, as a short list of observations.

    Deliberately ordered by what an operator can act on, not by severity: the
    thing that is broken is usually obvious, and the thing that is *still
    working* is what decides whether the dive continues.
    """
    rep = Report()
    state, note = TR.track_state(s, now, profile_key=profile_key)
    rep.state = state
    rep.headline = note

    acoustic = profile_key == "acoustic"

    # -- what is still working -------------------------------------------
    dvl_lock = s.dvl.get("bottom_lock")
    dvl_ok = _fresh(dvl_lock, now, "dvl")
    if dvl_ok:
        rep.findings.append(Finding(
            "dvl_ok", "DVL measurements remain available", INFO))

    if acoustic:
        v_pos = s.vessel.get("position")
        v_hdg = s.vessel.get("heading")
        if _fresh(v_pos, now, "vessel") and _fresh(v_hdg, now, "vessel"):
            rep.findings.append(Finding(
                "vessel_ok", "vessel position and heading are fresh", INFO))

    # -- the acoustic chain ------------------------------------------------
    if acoustic:
        age = TR.acoustic_age_s(s, now)
        fix = s.ugps.get("fix")
        if fix is None:
            rep.findings.append(Finding(
                "acoustic_none",
                "no acoustic fixes have been received at all", PROBLEM,
                actions=(_A_ACOUSTIC, _A_DVL_ONLY, _A_SNAPSHOT)))
        elif fix.quality is Quality.INVALID:
            rep.findings.append(Finding(
                "acoustic_invalid",
                "the acoustic solution is being reported invalid "
                "(fix type 0)", PROBLEM,
                suspected="the ROV locator is out of the receivers' "
                          "coverage, or the topside has lost its own "
                          "position",
                actions=(_A_ACOUSTIC, _A_VESSEL, _A_DVL_ONLY)))
        elif age is not None and age > FRESH_S["acoustic"]:
            f = Finding("acoustic_stale",
                        f"acoustic fixes stopped {_secs(age)}", PROBLEM,
                        actions=(_A_ACOUSTIC, _A_DVL_ONLY, _A_SNAPSHOT))
            # Only infer a cause when the rest of the chain rules things out.
            v_pos = s.vessel.get("position")
            if _fresh(v_pos, now, "vessel"):
                f.suspected = ("something between the topside and the "
                               "estimator, rather than the vessel's own "
                               "GNSS, which is still fresh")
            rep.findings.append(f)

        v_pos = s.vessel.get("position")
        v_hdg = s.vessel.get("heading")
        if v_pos is not None and not _fresh(v_pos, now, "vessel"):
            rep.findings.append(Finding(
                "vessel_gga",
                f"the vessel's GNSS position was last fresh "
                f"{_secs(_age(v_pos, now))}", PROBLEM,
                actions=(_A_VESSEL, _A_ACOUSTIC)))
        if v_hdg is not None and not _fresh(v_hdg, now, "vessel"):
            rep.findings.append(Finding(
                "vessel_hdt",
                f"the vessel's heading (HDT) was last fresh "
                f"{_secs(_age(v_hdg, now))} — the acoustic solution's "
                f"rotation depends on it", ADVISORY,
                actions=(_A_VESSEL,)))

    # -- the DVL chain -----------------------------------------------------
    if dvl_lock is not None and not dvl_ok:
        if dvl_lock.quality is Quality.INVALID:
            rep.findings.append(Finding(
                "dvl_lock", "the DVL reports no bottom lock", PROBLEM,
                suspected="the vehicle is too high, too low, or over ground "
                          "the beams cannot resolve",
                actions=(_A_DVL, _A_SNAPSHOT)))
        else:
            rep.findings.append(Finding(
                "dvl_stale",
                f"DVL measurements were last fresh "
                f"{_secs(_age(dvl_lock, now))}", PROBLEM,
                actions=(_A_DVL, _A_TELEMETRY)))

    mt = s.dvl.get("message_type")
    if mt is not None and isinstance(mt.value, str):
        posxy = (s.params or {}).get("EK3_SRC1_POSXY")
        if posxy == 6.0 and mt.value == "POSITION_DELTA":
            rep.findings.append(Finding(
                "dvl_message",
                "the DVL is sending POSITION_DELTA, which reaches relative "
                "aiding only; with a confirmed origin that is still a usable "
                "dead-reckoned position", ADVISORY,
                actions=(_A_DVL,)))

    # -- the estimator -----------------------------------------------------
    mode, mode_note = TR.aiding_mode(s)
    if mode == TR.NO_AIDING:
        rep.findings.append(Finding(
            "ekf_const", "the estimator is in constant-position mode — no "
                         "horizontal aiding at all", PROBLEM,
            actions=(_A_READINESS, _A_SNAPSHOT)))
    elif mode == TR.UNKNOWN:
        rep.findings.append(Finding(
            "ekf_quiet", "no EKF_STATUS_REPORT has arrived, so the "
                         "estimator's aiding mode is not observable",
            ADVISORY, actions=(_A_TELEMETRY,)))

    if origin_confirmed is False:
        rep.findings.append(Finding(
            "origin", "the EKF origin is not confirmed, so a relative "
                      "position cannot be turned into a latitude and "
                      "longitude", PROBLEM,
            actions=(_A_START, _A_READINESS)))

    # -- the link ----------------------------------------------------------
    if not s.link.connected:
        rep.findings.append(Finding(
            "link", f"not connected: {s.link.problem or 'no answer'}",
            PROBLEM, actions=(_A_TELEMETRY, _A_SNAPSHOT)))

    return rep


# The actions, defined once so the same one from two findings is one button.
_A_ACOUSTIC = Action("Inspect the acoustic status",
                     "Water Linked UGPS, in its own BlueOS page", "ugps")
_A_VESSEL = Action("Check the vessel's GNSS and heading",
                   "the External UGPS extension", "vessel")
_A_DVL = Action("Open the DVL extension",
                "Water Linked DVL, in its own BlueOS page", "dvl")
_A_DVL_ONLY = Action("Review DVL-only readiness",
                     "the profile selector on this page", "profile")
_A_TELEMETRY = Action("Recheck the required telemetry",
                      "Health — message health", "health")
_A_READINESS = Action("Review the readiness matrix", "this page", "health")
_A_START = Action("Set the origin", "Start a dive — step 3", "start")
_A_SNAPSHOT = Action("Save a diagnostic snapshot", "", "snapshot")

ALL_ACTIONS = (_A_ACOUSTIC, _A_VESSEL, _A_DVL, _A_DVL_ONLY, _A_TELEMETRY,
               _A_READINESS, _A_START, _A_SNAPSHOT)
