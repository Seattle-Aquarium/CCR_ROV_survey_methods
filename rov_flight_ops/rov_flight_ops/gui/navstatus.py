"""
Turning a snapshot into the sensor matrix, honestly.

Kept out of the widget so the judgements can be tested without a screen, and
because the judgements *are* the panel -- the widget only paints them.

**Four columns, not three.** An earlier version had "configured, available,
used", and the third was the problem: it inferred use from general estimator
flags, from which parameters were selected, and from a fresh derived heading.
None of those is evidence that a particular sensor is being fused. So:

``Conf``   the parameters say this source should be used.
``Recv``   samples are arriving at all -- from the message counter, so a
           cached HTTP reply is not mistaken for a new measurement.
``Valid``  the measurement itself says it means something: a bottom lock, a
           fix type above zero, a range greater than nought.
``Fused``  the estimator's own behaviour supports it being used, with the
           basis shown. **`?` when it cannot be established**, which is most
           of the time, because ArduPilot publishes aiding mode rather than
           per-instance fusion.

**Aiding mode is not freshness and not geographic validity.** Relative aiding
with a confirmed origin produces a perfectly good dead-reckoned latitude and
longitude; it is a different thing from stale, and from having no coordinates
at all. Those three used to be conflated into one amber row.

**DVL position and DVL velocity are separate rows.** In the acoustic profile
the DVL supplies velocity while position comes from the acoustics, and a
single DVL row cannot say that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..nav.model import Quality
from . import theme as T

#: The glyph as well as the colour, always. A daylight-washed Rugged screen
#: eats the colour first, and so does colour-vision deficiency.
YES = ("✓", T.OK)
NO = ("✗", T.ERROR)
PART = ("◐", T.WARN)
UNKNOWN = ("?", T.WARN)
NA = ("–", T.TEXT_MUTED)


@dataclass
class Row:
    """One line of the matrix: four marks and a sentence."""

    marks: list = field(default_factory=lambda: [NA, NA, NA, NA])
    detail: str = ""
    #: What the fusion verdict rests on, for the drawer.
    basis: str = ""


def _recv(r) -> tuple:
    """Is anything arriving? Freshness only -- not whether it is any good."""
    if r is None:
        return NA
    if r.quality in (Quality.OK, Quality.INVALID):
        return YES          # it arrived; whether it is valid is the next column
    if r.quality is Quality.STALE:
        return PART
    if r.quality is Quality.UNSUPPORTED:
        return NA
    return UNKNOWN


def _valid(r) -> tuple:
    """Does the measurement say it means something?"""
    if r is None:
        return NA
    if r.quality is Quality.OK:
        return YES
    if r.quality is Quality.INVALID:
        return NO
    if r.quality is Quality.STALE:
        return PART
    if r.quality is Quality.UNSUPPORTED:
        return NA
    return UNKNOWN


def _cfg(params: dict, name: str, want: float) -> tuple:
    if not params:
        return UNKNOWN
    v = params.get(name)
    if v is None:
        return UNKNOWN
    return YES if abs(v - want) < 1e-6 else NO


def aiding_mode(s) -> tuple[str, str]:
    """(mode, sentence) from the estimator's own flags.

    Three states worth telling apart, and the middle one is the one that was
    being misreported:

    ``absolute``  the estimator has an absolute horizontal position.
    ``relative``  it has a relative one. With a confirmed origin that still
                  yields a usable dead-reckoned latitude and longitude --
                  `getLLH` returns origin + offset when `horiz_pos_rel` is
                  set -- so this is a working state, not a fault.
    ``none``      constant-position mode: no horizontal aiding at all.
    """
    abs_ = s.ekf.get("horiz_pos_abs")
    rel = s.ekf.get("horiz_pos_rel")
    const = s.ekf.get("const_pos_mode")
    if const is not None and const.number():
        return "none", "constant position mode — no horizontal aiding at all"
    if abs_ is not None and abs_.number():
        return "absolute", "absolute horizontal position"
    if rel is not None and rel.number():
        return "relative", ("relative horizontal position — dead-reckoned, "
                            "and a usable fix when the origin is confirmed")
    return "unknown", "no EKF_STATUS_REPORT to judge from"


def matrix_rows(s, now: float) -> dict[str, Row]:
    """Every row of the sensor matrix, from one snapshot."""
    params = s.params or {}
    out: dict[str, Row] = {}

    posxy = params.get("EK3_SRC1_POSXY")
    velxy = params.get("EK3_SRC1_VELXY")
    mode, mode_note = aiding_mode(s)
    has_geo = s.rov_fix is not None and s.rov_fix.quality is Quality.OK

    # -- acoustic position ---------------------------------------------------
    acoustic = s.ugps.get("fix")
    std = s.ugps.get("std_m")
    acoustic_configured = posxy == 3.0
    if not acoustic_configured:
        fused = NO if posxy is not None else UNKNOWN
        basis = ("EK3_SRC1_POSXY is not GPS, so the acoustic position is not "
                 "the estimator's horizontal source")
    elif mode == "absolute" and acoustic is not None \
            and acoustic.quality is Quality.OK:
        fused, basis = YES, ("configured as the horizontal source, valid "
                             "fixes arriving, and the estimator has an "
                             "absolute position")
    else:
        fused, basis = UNKNOWN, ("configured, but nothing observable confirms "
                                 "the estimator is using these fixes")
    out["acoustic"] = Row(
        marks=[_cfg(params, "EK3_SRC1_POSXY", 3.0) if params else UNKNOWN,
               _recv(acoustic), _valid(acoustic), fused],
        detail=(f"σ {std.number():.1f} m (GPS_INPUT.vdop)"
                if std is not None and std.number() is not None
                else (acoustic.note if acoustic is not None else "no GPS_INPUT")),
        basis=basis)

    # -- the vessel: it orients the acoustics, it is never the ROV ----------
    gga = s.vessel.get("position")
    hdt = s.vessel.get("heading")
    inject = s.vessel.get("inject")
    injecting = bool(inject is not None and inject.number())
    out["vessel_gga"] = Row(
        marks=[YES if inject is not None else UNKNOWN, _recv(gga), _valid(gga),
               NA],
        detail="orients the acoustic topside; never fused as ROV position",
        basis="vessel position is not a vehicle position source at all")
    out["vessel_hdt"] = Row(
        marks=[YES if inject is not None else UNKNOWN, _recv(hdt), _valid(hdt),
               NA],
        detail=("bow direction only — not the ROV's yaw"
                if hdt is not None and hdt.quality is Quality.OK
                else (hdt.note if hdt is not None else "")),
        basis="the vessel's satellite compass rotates the acoustic solution")
    if not injecting and inject is not None:
        out["vessel_gga"].detail = ("not reaching the acoustic topside — "
                                    + out["vessel_gga"].detail)

    # -- the DVL, split into the two jobs it does ---------------------------
    msg = s.dvl.get("message_type")
    lock = s.dvl.get("bottom_lock")
    mt = msg.value if msg is not None and isinstance(msg.value, str) else ""
    # Position: only when the DVL is the configured horizontal source *and*
    # it is sending a message that carries a position at all.
    carries_position = mt in ("POSITION_ESTIMATE", "POSITION_DELTA")
    absolute_capable = mt == "POSITION_ESTIMATE"
    if posxy != 6.0:
        pos_cfg = NO if posxy is not None else UNKNOWN
        pos_fused = NA if posxy == 3.0 else UNKNOWN
        pos_detail = ("not the horizontal source in this profile"
                      if posxy == 3.0 else "EK3_SRC1_POSXY is not ExternalNav")
        pos_basis = pos_detail
    elif not carries_position:
        pos_cfg, pos_fused = NO, NO
        pos_detail = (f"{mt or 'message type unknown'} carries no position "
                      f"for EK3_SRC1_POSXY = ExternalNav")
        pos_basis = pos_detail
    elif mode == "absolute" and absolute_capable:
        pos_cfg, pos_fused = YES, YES
        pos_detail = "external navigation accepted as absolute position"
        pos_basis = ("POSITION_ESTIMATE reaches writeExtNavData, and the "
                     "estimator reports an absolute horizontal position")
    elif mode == "relative":
        pos_cfg = YES if absolute_capable else PART
        pos_fused = PART
        pos_detail = ("relative aiding" + ("" if has_geo else ", and no "
                                           "confirmed origin to reference it")
                      + (" — dead-reckoned position is usable" if has_geo
                         else ""))
        pos_basis = (
            "POSITION_DELTA reaches writeBodyFrameOdom, which is body-frame "
            "odometry and gives relative aiding only; with a confirmed origin "
            "that still yields a dead-reckoned latitude and longitude"
            if mt == "POSITION_DELTA" else
            "the estimator reports relative rather than absolute position")
    else:
        pos_cfg = YES if absolute_capable else PART
        pos_fused, pos_detail = UNKNOWN, (mt or "message type unknown")
        pos_basis = "aiding mode is not observable"
    out["dvl_position"] = Row(
        marks=[pos_cfg, _recv(lock), _valid(lock), pos_fused],
        detail=pos_detail, basis=pos_basis)

    # Velocity is a separate job, and in the acoustic profile it is the DVL's
    # only one. A single DVL row could not say that.
    vel_cfg = _cfg(params, "EK3_SRC1_VELXY", 6.0) if params else UNKNOWN
    vel_fused = (YES if (velxy == 6.0 and mode in ("absolute", "relative")
                         and lock is not None and lock.quality is Quality.OK)
                 else NO if velxy not in (6.0, None) else UNKNOWN)
    out["dvl_velocity"] = Row(
        marks=[vel_cfg, _recv(lock), _valid(lock), vel_fused],
        detail=("horizontal velocity from the DVL"
                if velxy == 6.0 else
                "EK3_SRC1_VELXY is not ExternalNav"),
        basis=("the DVL supplies velocity in both profiles; in the acoustic "
               "profile it is the only thing it supplies to the estimator"))

    # -- heading, attitude, depth, range -------------------------------------
    hdg = s.heading
    out["compass"] = Row(
        marks=[_cfg(params, "EK3_SRC1_YAW", 1.0) if params else UNKNOWN,
               _recv(hdg), _valid(hdg),
               # A fresh heading is the *output* of the estimator, not proof
               # that any particular compass went into it.
               UNKNOWN],
        detail="vehicle compass; the vessel's satellite compass is separate",
        basis=("the heading on the wire is the fused yaw. ArduPilot does not "
               "publish which compass instance produced it, so per-instance "
               "fusion cannot be confirmed from here"))

    roll = s.roll
    out["imu"] = Row(
        marks=[YES if params else UNKNOWN, _recv(roll), _valid(roll),
               YES if (roll is not None and roll.quality is Quality.OK
                       and mode != "unknown") else UNKNOWN],
        detail=(f"roll {roll.number():+.1f}°  pitch {s.pitch.number():+.1f}°"
                if roll.number() is not None and s.pitch.number() is not None
                else "no attitude"),
        basis="attitude is always fused; the estimator cannot run without it")

    depth = s.depth
    out["depth"] = Row(
        marks=[_cfg(params, "EK3_SRC1_POSZ", 1.0) if params else UNKNOWN,
               _recv(depth), _valid(depth),
               YES if (depth is not None and depth.quality is Quality.OK
                       and params.get("EK3_SRC1_POSZ") == 1.0) else UNKNOWN],
        detail="barometer, negative down; not the DVL's vertical range",
        basis="EK3_SRC1_POSZ selects the barometer for vertical position")

    alt = s.altitude
    out["range"] = Row(
        marks=[_cfg(params, "RNGFND1_TYPE", 10.0) if params else UNKNOWN,
               _recv(alt), _valid(alt), NA],
        detail=_range_detail(params, alt),
        basis=("the rangefinder is an instrument and a Surftrak input, not an "
               "estimator source: EK3_SRC1_POSZ is the barometer"))

    # -- the estimator --------------------------------------------------------
    flags = s.ekf.get("flags")
    geo_note = ""
    if mode in ("absolute", "relative"):
        geo_note = (" · geographic fix available" if has_geo
                    else " · no geographic fix (origin not confirmed)")
    out["ekf"] = Row(
        marks=[_cfg(params, "AHRS_EKF_TYPE", 3.0) if params else UNKNOWN,
               _recv(flags), _valid(flags),
               {"absolute": YES, "relative": PART, "none": NO}.get(mode,
                                                                   UNKNOWN)],
        detail=(mode_note + geo_note),
        basis=(flags.value if flags is not None and isinstance(flags.value, str)
               else "no EKF_STATUS_REPORT"))
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
    moved in a minute is a minute stale however many times it has been
    fetched -- and saying "network loss" about that would be a guess: the
    request succeeded, so nothing was lost on the wire.
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
