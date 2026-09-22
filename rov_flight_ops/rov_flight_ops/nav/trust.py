"""
What the position on the map is actually resting on.

One place, because two implementations of "what is the aiding mode" is exactly
the divergence that made the 18 September dive hard to diagnose: the matrix
said one thing, the map said another, and neither was wrong on its own terms.
`gui/navstatus.py` imports `aiding_mode` from here rather than having its own.

Four ideas are kept apart on purpose, and conflating any two of them produces
a confident wrong answer:

``aiding mode``       what the estimator says it has: absolute, relative, none.
``geographic``        whether that can be turned into a latitude and longitude,
                      which for relative aiding means having a confirmed origin.
``freshness``         whether the number arrived recently.
``quality``           whether the measurement claims to mean anything.

**Relative aiding with a confirmed origin is a working state**, not a fault:
`getLLH` returns the origin plus the relative offset whenever `horiz_pos_rel`
is set. It drifts as a whole, and the track's shape is still right.
"""

from __future__ import annotations

from .model import Quality

#: Track states, worst last. The order is the precedence used when a run of
#: track points is summarised.
ABSOLUTE = "absolute"
RELATIVE = "relative"
DEGRADED = "degraded"
NO_AIDING = "none"
UNKNOWN = "unknown"

STATES = (ABSOLUTE, RELATIVE, DEGRADED, NO_AIDING, UNKNOWN)

#: How old an acoustic fix may be before the position stops counting as
#: acoustically aided. Water Linked's topside publishes at about 1 Hz, so four
#: seconds is several missed updates rather than one late one.
ACOUSTIC_FRESH_S = 4.0

#: Beyond this, say how long it has been rather than just "stale" -- "12 s ago"
#: and "four minutes ago" call for different actions.
ACOUSTIC_REPORT_S = 60.0


def aiding_mode(s) -> tuple[str, str]:
    """(mode, sentence) from the estimator's own flags.

    Three states worth telling apart, and the middle one is the one that was
    being misreported:

    ``absolute``  the estimator has an absolute horizontal position.
    ``relative``  it has a relative one. With a confirmed origin that still
                  yields a usable dead-reckoned latitude and longitude, so
                  this is a working state, not a fault.
    ``none``      constant-position mode: no horizontal aiding at all.
    """
    abs_ = s.ekf.get("horiz_pos_abs")
    rel = s.ekf.get("horiz_pos_rel")
    const = s.ekf.get("const_pos_mode")
    if const is not None and const.number():
        return NO_AIDING, "constant position mode — no horizontal aiding at all"
    if abs_ is not None and abs_.number():
        return ABSOLUTE, "absolute horizontal position"
    if rel is not None and rel.number():
        return RELATIVE, ("relative horizontal position — dead-reckoned, "
                          "and a usable fix when the origin is confirmed")
    return UNKNOWN, "no EKF_STATUS_REPORT to judge from"


def acoustic_age_s(s, now: float) -> float | None:
    """Seconds since an acoustic fix was **received**, or None if never.

    Received, not accepted. Nothing in MAVLink says the estimator took a
    particular `GPS_INPUT` and used it, so this is deliberately the weaker
    claim — the stronger one would need evidence that does not exist on this
    stack, and inventing it is how an operator ends up trusting a position
    that stopped being corrected minutes ago.
    """
    fix = s.ugps.get("fix")
    if fix is None or fix.recv_mono is None:
        return None
    if fix.quality is Quality.INVALID:
        # It arrived, but the extension said the solution was no good, so it
        # is not a fix and does not reset the clock.
        return None
    return max(0.0, now - fix.recv_mono)


def acoustic_line(s, now: float) -> str:
    """One sentence about the acoustic correction, for the operator."""
    age = acoustic_age_s(s, now)
    if age is None:
        fix = s.ugps.get("fix")
        if fix is None:
            return "no acoustic fixes have been received"
        return "the acoustic solution is being reported invalid"
    if age <= ACOUSTIC_FRESH_S:
        return (f"acoustic fix received {age:.0f} s ago — received, not "
                f"confirmed accepted by the estimator")
    if age < ACOUSTIC_REPORT_S:
        return f"no acoustic fix for {age:.0f} s"
    return f"no acoustic fix for {age / 60.0:.0f} min"


def track_state(s, now: float, *, profile_key: str = "") -> tuple[str, str]:
    """(state, sentence) for the position being plotted right now.

    This is what colours the track. It is a judgement about *this* position,
    not about the health of any one sensor, which is why a fresh DVL and a
    stale estimator can still come out degraded.
    """
    fix = s.rov_fix
    if fix is None:
        return UNKNOWN, "no position"
    if fix.quality is Quality.STALE:
        return DEGRADED, "the last position is stale"
    if fix.quality is not Quality.OK:
        return UNKNOWN, "no usable position"

    mode, note = aiding_mode(s)
    if mode == NO_AIDING:
        return NO_AIDING, note
    if mode == UNKNOWN:
        return UNKNOWN, note

    if mode == ABSOLUTE:
        if profile_key == "acoustic":
            age = acoustic_age_s(s, now)
            if age is None:
                return DEGRADED, ("absolute aiding, but no acoustic fix has "
                                  "been received to support it")
            if age > ACOUSTIC_FRESH_S:
                return DEGRADED, (f"absolute aiding, but the last acoustic "
                                  f"fix was {age:.0f} s ago")
            return ABSOLUTE, f"acoustically aided, last fix {age:.0f} s ago"
        return ABSOLUTE, "absolute horizontal position"

    # Relative: usable, and honest about what it is.
    if fix.kind == "dead":
        return RELATIVE, "dead-reckoned from the confirmed origin"
    return RELATIVE, note


def summarise(states) -> str:
    """The worst state in a run, for labelling a stretch of track."""
    rank = {ABSOLUTE: 0, RELATIVE: 1, DEGRADED: 2, NO_AIDING: 3, UNKNOWN: 4}
    worst = None
    for st in states:
        if worst is None or rank.get(st, 4) > rank.get(worst, 4):
            worst = st
    return worst or UNKNOWN
