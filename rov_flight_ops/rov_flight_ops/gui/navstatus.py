"""
Turning a snapshot into the sensor matrix's three columns.

Kept out of the widget so it can be tested without a screen, and because the
judgements here are the substance of the panel -- the widget only paints them.

**Configured, available and used are three different questions**, and the
whole reason this matrix exists is that collapsing them into one status light
hides the state that actually strands a dive:

``Configured``  the parameters and integration settings say this source should
                be used. Read from the parameter dump.
``Available``   fresh, valid measurements are arriving. Read from the
                messages, with freshness from the counters rather than from a
                successful HTTP request.
``Used``        the estimator's own behaviour shows it is being fused. This is
                the one that usually cannot be established, and when it cannot
                it says **?** rather than a tick.

The gap between the second and the third is where this fleet lost a whole
day's coordinates on 18 September 2026: the DVL was configured correctly,
delivering beautifully, and feeding `VISION_POSITION_DELTA`, which ArduPilot
routes to body-frame odometry. EKF3 was in relative aiding the entire dive, so
`EK3_SRC1_POSXY = ExternalNav` had no external position to consume and
`GLOBAL_POSITION_INT` read 0, 0 from start to finish. Every "available" light
was green. The row that would have shown it is *used*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..nav.model import Quality
from . import theme as T

#: The glyph as well as the colour, always. A daylight-washed Rugged screen
#: eats the colour first, and so does colour-vision deficiency.
YES = ("✓", T.OK)
NO = ("✗", T.ERROR)
STALE = ("◐", T.WARN)
UNKNOWN = ("?", T.WARN)
NA = ("–", T.TEXT_MUTED)


@dataclass
class Row:
    """One line of the matrix."""

    marks: list = field(default_factory=lambda: [NA, NA, NA])
    detail: str = ""


def _avail(r) -> tuple:
    """The Available mark for one reading."""
    if r is None:
        return NA
    if r.quality is Quality.OK:
        return YES
    if r.quality is Quality.STALE:
        return STALE
    if r.quality is Quality.UNSUPPORTED:
        return NA
    if r.quality is Quality.NEVER_RECEIVED:
        return UNKNOWN
    return NO


def _cfg(params: dict, name: str, want: float) -> tuple:
    if not params:
        return UNKNOWN
    v = params.get(name)
    if v is None:
        return UNKNOWN
    return YES if abs(v - want) < 1e-6 else NO


def matrix_rows(s, now: float) -> dict[str, Row]:
    """Every row of the sensor matrix, from one snapshot."""
    params = s.params or {}
    out: dict[str, Row] = {}

    posxy = params.get("EK3_SRC1_POSXY")
    ekf_abs = s.ekf.get("horiz_pos_abs")
    ekf_rel = s.ekf.get("horiz_pos_rel")
    ekf_const = s.ekf.get("const_pos_mode")
    has_abs = bool(ekf_abs is not None and ekf_abs.number())
    has_rel = bool(ekf_rel is not None and ekf_rel.number())
    is_const = bool(ekf_const is not None and ekf_const.number())

    # -- acoustic position ---------------------------------------------------
    acoustic = s.ugps.get("fix")
    std = s.ugps.get("std_m")
    out["acoustic"] = Row(
        marks=[
            _cfg(params, "EK3_SRC1_POSXY", 3.0) if params else UNKNOWN,
            _avail(acoustic),
            # Only "used" if the EKF has an absolute position *and* the
            # configured horizontal source is GPS. Either alone proves
            # nothing.
            (YES if (has_abs and posxy == 3.0) else
             NO if posxy != 3.0 else UNKNOWN),
        ],
        detail=(f"σ {std.number():.1f} m (GPS_INPUT.vdop)"
                if std is not None and std.number() is not None
                else (acoustic.note if acoustic is not None else
                      "no GPS_INPUT")))

    # -- the vessel, which feeds the acoustic solution, not the EKF ----------
    gga = s.vessel.get("position")
    hdt = s.vessel.get("heading")
    inject = s.vessel.get("inject")
    injecting = bool(inject is not None and inject.number())
    out["vessel_gga"] = Row(
        marks=[YES if inject is not None else UNKNOWN, _avail(gga),
               # Never a tick: vessel position is not fused as vehicle
               # position, it orients the acoustic solution.
               (YES if injecting else NO) if gga is not None else NA],
        detail="orients the acoustic topside; never fused as ROV position")
    out["vessel_hdt"] = Row(
        marks=[YES if inject is not None else UNKNOWN, _avail(hdt),
               (YES if injecting else NO) if hdt is not None else NA],
        detail=("bow direction only — not the ROV's yaw"
                if hdt is not None and hdt.quality is Quality.OK
                else (hdt.note if hdt is not None else "")))

    # -- DVL -----------------------------------------------------------------
    msg_type = s.dvl.get("message_type")
    lock = s.dvl.get("bottom_lock")
    mt = msg_type.value if msg_type is not None and isinstance(
        msg_type.value, str) else ""
    dvl_cfg = (YES if (posxy == 6.0 and mt == "POSITION_ESTIMATE")
               else NO if posxy == 6.0 and mt else UNKNOWN)
    if posxy == 6.0 and mt == "POSITION_DELTA":
        dvl_used = NO
        dvl_detail = ("POSITION_DELTA is body-frame odometry — relative "
                      "aiding only, no absolute position")
    elif posxy == 6.0 and has_abs:
        dvl_used, dvl_detail = YES, "external nav accepted for position"
    elif posxy == 6.0 and has_rel:
        dvl_used = STALE
        dvl_detail = "relative aiding only — no absolute position"
    else:
        dvl_used = UNKNOWN
        dvl_detail = mt or "message type unknown"
    out["dvl"] = Row(marks=[dvl_cfg, _avail(lock), dvl_used],
                     detail=dvl_detail)

    # -- heading, attitude, depth, range -------------------------------------
    hdg = s.heading
    out["compass"] = Row(
        marks=[_cfg(params, "EK3_SRC1_YAW", 1.0) if params else UNKNOWN,
               _avail(hdg),
               YES if (hdg is not None and hdg.quality is Quality.OK
                       and not is_const) else UNKNOWN],
        detail="vehicle compass; the vessel's satellite compass is separate")

    roll = s.roll
    out["imu"] = Row(
        marks=[YES if params else UNKNOWN, _avail(roll),
               YES if (roll is not None and roll.quality is Quality.OK)
               else UNKNOWN],
        detail=(f"roll {roll.number():+.1f}°  pitch {s.pitch.number():+.1f}°"
                if roll.number() is not None and s.pitch.number() is not None
                else "no attitude"))

    depth = s.depth
    out["depth"] = Row(
        marks=[_cfg(params, "EK3_SRC1_POSZ", 1.0) if params else UNKNOWN,
               _avail(depth),
               YES if (depth is not None and depth.quality is Quality.OK)
               else UNKNOWN],
        detail="barometer, negative down; not the DVL's vertical range")

    alt = s.altitude
    out["range"] = Row(
        marks=[_cfg(params, "RNGFND1_TYPE", 10.0) if params else UNKNOWN,
               _avail(alt),
               # The rangefinder is an instrument here, not an EKF source:
               # EK3_SRC1_POSZ is the barometer.
               NA],
        detail=_range_detail(params, alt))

    # -- the estimator --------------------------------------------------------
    flags = s.ekf.get("flags")
    if is_const:
        ekf_used, ekf_detail = NO, "constant position mode — no aiding at all"
    elif has_abs:
        ekf_used, ekf_detail = YES, "absolute horizontal position"
    elif has_rel:
        ekf_used = STALE
        ekf_detail = ("relative horizontal position only — position is "
                      "dead-reckoned, there is no geographic fix")
    else:
        ekf_used, ekf_detail = UNKNOWN, (
            flags.value if flags is not None and isinstance(flags.value, str)
            else "no EKF_STATUS_REPORT")
    out["ekf"] = Row(
        marks=[_cfg(params, "AHRS_EKF_TYPE", 3.0) if params else UNKNOWN,
               _avail(flags), ekf_used],
        detail=ekf_detail)
    return out


def _range_detail(params: dict, alt) -> str:
    """The rangefinder row's detail, including the Surftrak Fixit signature.

    `RNGFND1_ORIENT` outside the MAV_SENSOR_ORIENTATION range is the
    fingerprint of a known bug in Surftrak Fixit v1.0.0-beta.2, whose
    `prb_bad_max` branch logs "setting RNGFND1_MAX_CM to 5000" and actually
    calls `set_param('RNGFND1_ORIENT', 5000)`. This program never calls that
    endpoint; it checks for the damage and names it.
    """
    orient = params.get("RNGFND1_ORIENT") if params else None
    if orient is not None and not (0 <= orient <= 45):
        return (f"⚠ RNGFND1_ORIENT is {orient:g} — not an orientation. "
                f"Surftrak Fixit's range-maximum repair writes 5000 here.")
    if alt is not None and alt.quality is Quality.OK:
        v = alt.number()
        return f"{v:.2f} m above the bottom" if v is not None else ""
    return (alt.note if alt is not None else "")


def message_health_rows(s, now: float) -> list[tuple[str, str, str, str]]:
    """(message, rate, age, note) for the message-health drawer.

    The age is time since a *new* message, taken from mavlink2rest's counter,
    not since the last successful request. A message whose counter has not
    moved in a minute is a minute stale however many times it has been fetched.
    """
    rows = []
    for name in sorted(s.messages):
        h = s.messages[name]
        age = h.age(now)
        rate = f"{h.frequency:.1f} Hz" if h.frequency else "—"
        if h.error:
            note, age_s = h.error, "—"
        elif not h.seen:
            note, age_s = "never received", "—"
        else:
            age_s = f"{age:.0f} s" if age is not None else "—"
            note = "restarted — counter went backwards" if h.reset else ""
        rows.append((name, rate, age_s, note))
    return rows
