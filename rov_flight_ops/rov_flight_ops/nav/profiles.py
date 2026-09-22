"""
The two navigation profiles, what each needs, and what the vehicle has.

A profile is data, not code: a name, a firmware it was written against, and a
list of parameter requirements each carrying its own reason. That shape is
deliberate. The values below were derived by reading ArduSub 4.5.7 at commit
b09fafe2 and the Water Linked DVL extension at v1.0.10 against a real
parameter dump from this fleet -- and the next firmware will move some of
them. A table can be corrected by someone who is not a programmer; a nest of
`if` statements cannot.

Four findings from that reading are built in, because each one would otherwise
produce a confident, wrong dashboard:

**`EK3_GPS_TYPE` does not exist on ArduSub 4.5.7.** It was removed from EKF3
(`AP_NavEKF3.cpp` carries only the comment ``// 1 was GPS_TYPE`` and a one-time
conversion) and is absent from all 1,020 parameters in this fleet's 18
September dump. The DVL extension still writes it in both of its presets, so
that write lands nowhere. A profile check that demanded it would raise a
permanent false alarm; one that silently ignored it would hide that the
extension's own preset is partly inert on this firmware. It is listed as
`ABSENT_OK` and explained.

**`VISION_POSITION_DELTA` cannot produce a geographic position.** ArduPilot
routes it to `writeBodyFrameOdom`, so EKF3 enters *relative* aiding
(`readyToUseBodyOdm`), never absolute -- `readyToUseExtNav` requires
`extNavDataToFuse`, which only `writeExtNavData` fills, and only
`VISION_POSITION_ESTIMATE`/`GLOBAL_VISION_POSITION_ESTIMATE`/`ODOMETRY` reach
that. With a valid origin, relative aiding still yields a dead-reckoned
lat/lon through `getLLH`; with no origin it yields lat 0, lon 0. That pairing
is why this fleet's 18 September dive produced no coordinates, and it is why
the DVL message type is a checked requirement rather than a detail.

**Source-set switching works but the sets are empty.** ArduSub 4.5.7 does
handle `MAV_CMD_SET_EKF_SOURCE_SET` for sets 1-3. This fleet's `EK3_SRC2_*`
and `EK3_SRC3_*` are all zero, so switching to set 2 today would select *no*
horizontal position, *no* horizontal velocity and *no* yaw source. The
capability is therefore offered only when the destination set is actually
configured, and refused with that specific reason when it is not.

**The command is acknowledged, not confirmed.** `handle_command_set_ekf_source_set`
calls `set_posvelyaw_source_set` and returns `MAV_RESULT_ACCEPTED`. Nothing in
MAVLink then reports which set is live. A profile is "requested and
acknowledged"; it is "active" only when the measurements say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)

#: Bumped when a profile's contents change, so a session log says which
#: definition it was validated against.
PROFILES_VERSION = "2026-09-21"

#: The firmware these were read from. A vehicle reporting something else still
#: gets checked, with the mismatch shown -- refusing to run against an
#: unrecognised build would be worse than checking and saying so.
WRITTEN_FOR = "ArduSub 4.5.7"


# --------------------------------------------------------------------------
#  EKF source enumerations, from AP_NavEKF_Source.h at b09fafe2
# --------------------------------------------------------------------------

SOURCE_XY = {0: "None", 3: "GPS", 4: "Beacon", 5: "OpticalFlow",
             6: "ExternalNav", 7: "WheelEncoder"}
SOURCE_Z = {0: "None", 1: "Baro", 2: "RangeFinder", 3: "GPS", 4: "Beacon",
            6: "ExternalNav"}
SOURCE_YAW = {0: "None", 1: "Compass", 2: "GPS", 3: "GPS with compass fallback",
              6: "ExternalNav", 8: "GSF"}


def source_name(param: str, value: float | None) -> str:
    """`6` -> `ExternalNav`, for a parameter whose number means nothing alone."""
    if value is None:
        return "—"
    v = int(value)
    table = (SOURCE_YAW if param.endswith("YAW") else
             SOURCE_Z if param.endswith(("POSZ", "VELZ")) else SOURCE_XY)
    return f"{v} ({table.get(v, 'unknown')})"


class Severity(str, Enum):
    """How much a mismatch matters.

    The split exists because an operator on a deck needs to know which of six
    red rows actually stops the dive.
    """

    #: The profile cannot work at all until this is right.
    BLOCKER = "blocker"
    #: It will work, worse or less safely, and should be looked at.
    ADVISORY = "advisory"
    #: Recorded for the log; nothing to do.
    INFO = "info"


@dataclass(frozen=True)
class Requirement:
    """One thing a profile needs, and why.

    `why` is not decoration. Every mismatch this program shows an operator
    carries the sentence that explains it, because a parameter name and two
    numbers is not something anyone can act on at 0700 in the rain.
    """

    param: str
    want: float | None
    why: str
    severity: Severity = Severity.BLOCKER
    #: True when the parameter legitimately may not exist on this firmware.
    absent_ok: bool = False
    #: Shown instead of the raw number where the number is an enumeration.
    enum: bool = False
    #: Any of these values is acceptable; `want` is the one that would be set.
    also_ok: tuple[float, ...] = ()

    def accepts(self, value: float | None) -> bool:
        if value is None:
            return self.absent_ok
        if self.want is None:
            return True
        return (abs(value - self.want) < 1e-6
                or any(abs(value - v) < 1e-6 for v in self.also_ok))


@dataclass(frozen=True)
class Profile:
    """A named navigation configuration."""

    key: str
    label: str
    summary: str
    requirements: tuple[Requirement, ...]
    #: What the DVL extension's `should_send` must be for this profile.
    dvl_message_type: str = "POSITION_ESTIMATE"
    #: Which preconfigured EKF source set this corresponds to, when the
    #: vehicle has one set up. None means "there is no configured set for
    #: this; changing profile means changing SRC1 parameters".
    source_set: int | None = None
    needs_vessel: bool = False
    version: str = PROFILES_VERSION
    written_for: str = WRITTEN_FOR


# --------------------------------------------------------------------------
#  The profiles
# --------------------------------------------------------------------------

_COMMON = (
    Requirement("AHRS_EKF_TYPE", 3.0,
                "EKF3 is the estimator both profiles are defined against; the "
                "DVL extension sets this itself at startup.",
                Severity.BLOCKER),
    Requirement("EK3_ENABLE", 1.0,
                "EKF3 must be running.", Severity.BLOCKER),
    Requirement("VISO_TYPE", 1.0,
                "Accept vision/odometry messages over MAVLink. Without it the "
                "DVL's messages are parsed and discarded.",
                Severity.BLOCKER),
    Requirement("EK3_SRC1_POSZ", 1.0,
                "Depth comes from the barometer. The DVL's vertical range is "
                "height above the seabed, which is not depth below the "
                "surface and must not be fused as one.",
                Severity.BLOCKER, enum=True),
    Requirement("EK3_SRC1_YAW", 1.0,
                "Heading from the vehicle's own compass. The vessel's "
                "satellite compass orients the acoustic solution and is not "
                "the ROV's yaw.",
                Severity.BLOCKER, enum=True),
    Requirement("EK3_GPS_TYPE", None,
                "Removed from EKF3 after ArduPilot 4.1 — EK3_SRC1_* replaced "
                "it. The Water Linked DVL extension still writes it in both "
                "of its presets, so that one write lands nowhere on this "
                "firmware. Nothing to fix.",
                Severity.INFO, absent_ok=True),
    Requirement("SCR_ENABLE", 1.0,
                "Lua scripting, needed only if the EKF origin is set by the "
                "ahrs-set-origin applet rather than from this laptop.",
                Severity.ADVISORY),
    Requirement("RNGFND1_ORIENT", 25.0,
                "Rangefinder pointing down (25 = MAV_SENSOR_ROTATION_PITCH_270). "
                "Worth checking explicitly: Surftrak Fixit v1.0.0-beta.2 has a "
                "confirmed bug that writes 5000 to this parameter when asked "
                "to fix the range maximum.",
                Severity.ADVISORY, enum=False),
)

DVL_ONLY = Profile(
    key="dvl",
    label="DVL dead reckoning",
    summary=(
        "Position and horizontal velocity from the DVL, depth from the "
        "barometer, heading from the compass. No acoustic correction, so the "
        "whole track drifts as one and rotates with any compass error."),
    dvl_message_type="POSITION_ESTIMATE",
    source_set=1,
    needs_vessel=False,
    requirements=_COMMON + (
        Requirement("EK3_SRC1_POSXY", 6.0,
                    "Horizontal position from external navigation — the DVL. "
                    "Nothing else is available underwater.",
                    Severity.BLOCKER, enum=True),
        Requirement("EK3_SRC1_VELXY", 6.0,
                    "Horizontal velocity from the DVL, which is what it "
                    "measures directly and best.",
                    Severity.BLOCKER, enum=True),
    ),
)

ACOUSTIC_DVL = Profile(
    key="acoustic",
    label="Acoustic position + DVL",
    summary=(
        "Horizontal position from the Water Linked acoustic solution injected "
        "as GPS_INPUT, horizontal velocity from the DVL, depth from the "
        "barometer, heading from the compass. Needs the vessel's satellite "
        "compass feeding the topside."),
    dvl_message_type="POSITION_ESTIMATE",
    source_set=None,
    needs_vessel=True,
    requirements=_COMMON + (
        Requirement("EK3_SRC1_POSXY", 3.0,
                    "Horizontal position from 'GPS' — which here means the "
                    "acoustic position the Water Linked UGPS extension "
                    "injects as GPS_INPUT. The ROV has no satellite "
                    "reception.",
                    Severity.BLOCKER, enum=True),
        Requirement("EK3_SRC1_VELXY", 6.0,
                    "Horizontal velocity stays with the DVL even when "
                    "position comes from the acoustics: the DVL measures "
                    "velocity far better than a position stream differentiates "
                    "into one.",
                    Severity.BLOCKER, enum=True),
        Requirement("GPS_TYPE", 14.0,
                    "GPS driver 14 (MAV) accepts GPS_INPUT over MAVLink. "
                    "Without it the injected acoustic position is ignored.",
                    Severity.BLOCKER, enum=False),
    ),
)

PROFILES = {p.key: p for p in (DVL_ONLY, ACOUSTIC_DVL)}
#: The order they are offered in. DVL-only first: it is the one that works
#: without a vessel, and it is what this fleet flies most.
PROFILE_ORDER = ("dvl", "acoustic")


# --------------------------------------------------------------------------
#  Checking
# --------------------------------------------------------------------------


@dataclass
class Finding:
    """One requirement, checked against what the vehicle actually has."""

    requirement: Requirement
    current: float | None
    ok: bool
    #: True when the parameter is not in the dump at all.
    absent: bool = False

    @property
    def param(self) -> str:
        return self.requirement.param

    @property
    def severity(self) -> Severity:
        return self.requirement.severity

    def want_text(self) -> str:
        r = self.requirement
        if r.want is None:
            return "—"
        return (source_name(r.param, r.want) if r.enum
                else _num(r.want))

    def current_text(self) -> str:
        if self.absent:
            return "not on this firmware"
        return (source_name(self.param, self.current) if self.requirement.enum
                else _num(self.current))

    def line(self) -> str:
        mark = "OK" if self.ok else self.severity.value.upper()
        return (f"[{mark}] {self.param}: has {self.current_text()}, "
                f"wants {self.want_text()}")


def _num(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:g}"


@dataclass
class CheckResult:
    """A whole profile, checked. What the validation panel draws."""

    profile: Profile
    findings: list[Finding] = field(default_factory=list)
    #: Things that are not parameters: the DVL's message type, the origin,
    #: whether the vessel is feeding the topside.
    notes: list[tuple[Severity, str]] = field(default_factory=list)
    #: True when the parameter dump was too old or absent to judge.
    unknown: bool = False
    params_age: float | None = None

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings
                if not f.ok and f.severity is Severity.BLOCKER]

    @property
    def advisories(self) -> list[Finding]:
        return [f for f in self.findings
                if not f.ok and f.severity is Severity.ADVISORY]

    @property
    def note_blockers(self) -> list[str]:
        return [t for s, t in self.notes if s is Severity.BLOCKER]

    @property
    def matches(self) -> bool:
        """Every blocker satisfied. Not a claim that the EKF is using it."""
        return (not self.unknown and not self.blockers
                and not self.note_blockers)

    def changes(self) -> list[Finding]:
        """The parameter writes that would make this profile hold.

        Only findings whose parameter actually exists on the vehicle: writing
        a parameter this firmware does not have is a no-op that would appear
        in the review dialog as a change the operator is asked to approve.
        """
        return [f for f in self.findings
                if not f.ok and not f.absent and f.requirement.want is not None]

    def summary(self) -> str:
        if self.unknown:
            return "Parameters have not been read from the vehicle"
        n_b, n_a = len(self.blockers) + len(self.note_blockers), len(self.advisories)
        if not n_b and not n_a:
            return "Configured as this profile requires"
        bits = []
        if n_b:
            bits.append(f"{n_b} blocking")
        if n_a:
            bits.append(f"{n_a} advisory")
        return " · ".join(bits) + " mismatch" + ("es" if n_b + n_a > 1 else "")


def check(profile: Profile, params: dict[str, float] | None, *,
          params_age: float | None = None,
          dvl_message_type: str = "",
          dvl_reachable: bool = False,
          origin_set: bool | None = None,
          vessel_feeding: bool | None = None) -> CheckResult:
    """Compare a profile with what the vehicle has, and say what is wrong.

    `params` is a parameter dump — from the dataflash log, which is how
    `blueos.read_parameters_now` reads them without sending the vehicle
    anything. A `None` dump gives an explicitly unknown result rather than a
    row of green ticks, because "we have not looked" and "it is fine" are
    different answers.
    """
    res = CheckResult(profile=profile, params_age=params_age)
    if not params:
        res.unknown = True
        return res

    for req in profile.requirements:
        present = req.param in params
        cur = params.get(req.param)
        ok = req.accepts(cur if present else None)
        res.findings.append(Finding(requirement=req, current=cur, ok=ok,
                                    absent=not present))

    # -- the DVL's message type, which no parameter records ----------------
    if not dvl_reachable:
        res.notes.append((
            Severity.ADVISORY,
            "The Water Linked DVL extension did not answer, so its message "
            "type could not be checked. That setting decides whether the "
            "vehicle can have a geographic position at all."))
    elif dvl_message_type != profile.dvl_message_type:
        if dvl_message_type == "POSITION_DELTA":
            res.notes.append((
                Severity.BLOCKER,
                "The DVL is sending POSITION_DELTA. ArduPilot treats that as "
                "body-frame odometry, which puts EKF3 into relative aiding "
                "only — EK3_SRC1_POSXY=ExternalNav has no external position "
                "to consume. Set the extension's message type to "
                "POSITION_ESTIMATE for an absolute position."))
        elif dvl_message_type == "SPEED_ESTIMATE":
            res.notes.append((
                Severity.BLOCKER,
                "The DVL is sending SPEED_ESTIMATE, which carries velocity "
                "only. There is no position in it for EK3_SRC1_POSXY to use."))
        elif dvl_message_type:
            res.notes.append((
                Severity.BLOCKER,
                f"The DVL is sending {dvl_message_type}; this profile needs "
                f"{profile.dvl_message_type}."))

    # -- the origin ---------------------------------------------------------
    if origin_set is False:
        res.notes.append((
            Severity.BLOCKER,
            "The EKF has no origin, so there is no geographic position even "
            "when the estimator is otherwise healthy — GLOBAL_POSITION_INT "
            "reports latitude and longitude 0. Set an origin before the dive."))
    elif origin_set is None:
        res.notes.append((
            Severity.ADVISORY,
            "Whether the EKF origin is set has not been confirmed. ArduPilot "
            "does not stream GPS_GLOBAL_ORIGIN; it has to be asked for."))

    # -- the vessel ---------------------------------------------------------
    if profile.needs_vessel:
        if vessel_feeding is False:
            res.notes.append((
                Severity.BLOCKER,
                "The vessel's satellite compass is not reaching the acoustic "
                "topside, so there is no reference to place the ROV against."))
        elif vessel_feeding is None:
            res.notes.append((
                Severity.ADVISORY,
                "The vessel feed could not be checked — the WL UGPS External "
                "extension did not answer."))

    return res


# --------------------------------------------------------------------------
#  Source-set switching
# --------------------------------------------------------------------------


@dataclass
class SourceSetOption:
    """Whether the deliberate source-set switch is available, and why not."""

    available: bool
    set_number: int | None
    reason: str


def source_set_available(profile: Profile,
                         params: dict[str, float] | None) -> SourceSetOption:
    """Can this profile be selected with `MAV_CMD_SET_EKF_SOURCE_SET`?

    ArduSub 4.5.7 supports the command. Whether *using* it is a good idea
    depends entirely on whether the destination set has been configured, and
    on this fleet it has not: `EK3_SRC2_*` and `EK3_SRC3_*` are all zero,
    which selects no position, no velocity and no yaw source.

    Switching into that would be worse than any mismatch it was meant to fix,
    so the action is offered only when the destination set genuinely holds the
    profile's sources -- and the refusal says which parameter is wrong.
    """
    n = profile.source_set
    if n is None:
        return SourceSetOption(
            False, None,
            "This profile has no preconfigured EKF source set; selecting it "
            "means changing the EK3_SRC1_* parameters.")
    if not params:
        return SourceSetOption(
            False, n,
            "Parameters have not been read, so the destination source set "
            "cannot be checked.")

    wants = {r.param.replace("SRC1", f"SRC{n}"): r.want
             for r in profile.requirements
             if r.param.startswith("EK3_SRC1_") and r.want is not None}
    wrong = []
    for name, want in sorted(wants.items()):
        cur = params.get(name)
        if cur is None:
            wrong.append(f"{name} is not present")
        elif abs(cur - want) > 1e-6:
            wrong.append(f"{name} is {source_name(name, cur)}, "
                         f"needs {source_name(name, want)}")
    if wrong:
        return SourceSetOption(
            False, n,
            f"EKF source set {n} is not configured for this profile: "
            + "; ".join(wrong) + ".")
    return SourceSetOption(
        True, n,
        f"EKF source set {n} matches this profile and can be selected with "
        f"MAV_CMD_SET_EKF_SOURCE_SET. The command is acknowledged by the "
        f"autopilot; nothing in MAVLink reports which set is live, so the "
        f"result stays 'requested' until the measurements confirm it.")


# --------------------------------------------------------------------------
#  Which profile is the vehicle actually in?
# --------------------------------------------------------------------------


def detect(params: dict[str, float] | None) -> tuple[str | None, str]:
    """(profile key, how it was decided) from the parameters alone.

    "Configured as", never "running as". The parameters say what the EKF has
    been told to prefer; only the estimator's own behaviour says what it is
    doing, and that is the `used` column of the sensor matrix.
    """
    if not params:
        return None, "parameters have not been read"
    posxy = params.get("EK3_SRC1_POSXY")
    if posxy is None:
        return None, "EK3_SRC1_POSXY is not in the parameter set"
    if abs(posxy - 6.0) < 1e-6:
        return "dvl", "EK3_SRC1_POSXY is ExternalNav"
    if abs(posxy - 3.0) < 1e-6:
        return "acoustic", "EK3_SRC1_POSXY is GPS"
    return None, (f"EK3_SRC1_POSXY is {source_name('EK3_SRC1_POSXY', posxy)}, "
                  f"which is neither profile")
