"""
Giving dead reckoning a place on the Earth: the EKF origin.

Without an origin a DVL-only ROV navigates perfectly and has no coordinates.
EKF3 tracks position in a local NED frame; `GLOBAL_POSITION_INT` is that frame
projected through `EKF_origin`, and `NavEKF3_core::getLLH` returns nothing at
all unless `getOriginLLH` succeeds. Latitude 0, longitude 0, forever — which
is exactly what this fleet recorded on 18 September 2026 with
`ORIGIN_LAT = 47.62691` and `ORIGIN_LON = -122.39018` sitting right there in
the parameters.

**That pairing is the thing to understand.** Those parameters existed because
a Lua applet had once been installed and created them; `param:add_table`
entries survive in storage after the script that made them is gone. No script
was running to consume them. The coordinates were set and nobody had told the
autopilot.

So this module's first job is not to write an origin. It is to tell the
operator, before the dive, which of these is true:

* something on the vehicle is capable of applying an origin, and what;
* whether the EKF actually has one right now;
* whether the saved coordinates are today's or last week's.

Three mechanisms can own the origin, and they are not synonyms:

===================  ========================================================
`ORIGIN_LAT/LON/ALT`  created by a Lua applet whose `PARAM_TABLE_PREFIX` is
                      ``ORIGIN_``. This fleet's variant, in the repository at
                      ``lua_scripts/ahrs-set-origin-ORIGIN_.lua``.
`AHRS_ORIG_LAT/...`   created by ArduPilot's published applet
                      ``ahrs-set-origin.lua``. The short ``ORIG`` is
                      significant and is not a typo for either of the others.
`AHRS_ORIGIN_LAT/...` native firmware persistence, ArduPilot 4.7 and later,
                      with `AHRS_OPTIONS` bit 3 to record and bit 4 to
                      restore. **Not present on 4.5.7** and not a reason to
                      upgrade a working vehicle.
===================  ========================================================

A leftover set from a prefix nobody is using any more is worse than none: it
reads like configuration and does nothing. `detect` finds every family that is
present and says which, if any, has a script behind it.

**On the published applet.** ArduPilot's own copy contains a bug that this
repository already carries a one-line fix for. Its "nothing has been set yet"
guard reads ``if AHRS_ORIG_LAT == 0 and ...`` — comparing the `Parameter`
*object* to a number, which Lua never finds equal, so the guard never fires.
On a fresh install with the parameters still at their defaults the script
therefore goes straight on and calls `ahrs:set_origin()` with 0, 0, 0, and
then returns for good without retrying. Install the fixed copy from
``lua_scripts/``, not master.

**On ordering.** The applet is not transactional: its guard passes as soon as
*any one* of the three parameters is non-zero, so there is no write order that
is safe against a copy sitting in its five-second retry loop. There is,
however, a safe *sequence*, and it is the one this module implements:

    1. Set the origin from this laptop with `SET_GPS_GLOBAL_ORIGIN`, and read
       `GPS_GLOBAL_ORIGIN` back to confirm it.
    2. Only then write the applet's parameters, for the next boot.

After step 1 the applet's first check — ``if ahrs:get_origin() then ... return``
— is true, so it stops before it can ever look at a half-written coordinate
pair. The race is removed by making it impossible for the applet to act, not
by writing quickly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import model as M

log = logging.getLogger(__name__)

#: MAVLink message id for GPS_GLOBAL_ORIGIN. ArduPilot does not stream it; it
#: answers a MAV_CMD_REQUEST_MESSAGE for it.
GPS_GLOBAL_ORIGIN_ID = 49

#: The three parameter families, newest-documented first. The tuple order is
#: the order `detect` reports them in, not a preference.
FAMILIES = {
    "ORIGIN_": ("ORIGIN_LAT", "ORIGIN_LON", "ORIGIN_ALT"),
    "AHRS_ORIG_": ("AHRS_ORIG_LAT", "AHRS_ORIG_LON", "AHRS_ORIG_ALT"),
    "AHRS_ORIGIN_": ("AHRS_ORIGIN_LAT", "AHRS_ORIGIN_LON", "AHRS_ORIGIN_ALT"),
}

FAMILY_NOTE = {
    "ORIGIN_": ("created by a Lua applet using the ORIGIN_ prefix "
                "(lua_scripts/ahrs-set-origin-ORIGIN_.lua in this repository)"),
    "AHRS_ORIG_": ("created by ArduPilot's published ahrs-set-origin.lua "
                   "applet — install this repository's fixed copy, not master"),
    "AHRS_ORIGIN_": ("native firmware origin persistence, ArduPilot 4.7 and "
                     "later, governed by AHRS_OPTIONS bits 3 and 4"),
}

#: `AHRS_OPTIONS` bits that matter to the native mechanism, so that writing
#: one never clears an unrelated option the operator set for another reason.
AHRS_OPTION_RECORD_ORIGIN = 1 << 3
AHRS_OPTION_RESTORE_ORIGIN = 1 << 4


class OriginError(RuntimeError):
    """A refusal with a reason the operator can read."""


# --------------------------------------------------------------------------
#  What the coordinates are allowed to be
# --------------------------------------------------------------------------


def validate(lat, lon, alt=0.0) -> tuple[float, float, float]:
    """Parse and range-check a typed coordinate, or raise with why.

    Range-checked here regardless of what any upstream claims, because this is
    the last point before a number becomes the frame every position on the
    page is expressed in. A transposed pair puts the whole dive in the wrong
    hemisphere and nothing downstream can tell.
    """
    try:
        flat = float(str(lat).strip())
        flon = float(str(lon).strip())
        falt = float(str(alt).strip() or 0.0)
    except (TypeError, ValueError):
        raise OriginError(
            "Latitude and longitude must be decimal degrees, e.g. "
            "47.62691 and -122.39018.") from None
    if not -90.0 <= flat <= 90.0:
        raise OriginError(f"Latitude {flat} is outside −90 to 90.")
    if not -180.0 <= flon <= 180.0:
        raise OriginError(f"Longitude {flon} is outside −180 to 180.")
    if abs(flat) < M.NULL_ISLAND_DEG and abs(flon) < M.NULL_ISLAND_DEG:
        raise OriginError(
            "0, 0 is the null island in the Gulf of Guinea — it is what every "
            "uninitialised coordinate in this stack reads, so it is refused "
            "as an origin.")
    if not -500.0 <= falt <= 9000.0:
        raise OriginError(f"Altitude {falt} m above sea level is implausible.")
    return flat, flon, falt


#: Parameters are float32 in ArduPilot's storage. At Seattle's latitude a
#: float32 degree holds about 1 m of longitude — fine for an origin, and not
#: fine to describe as centimeter accurate. Reported, not hidden.
FLOAT32_NOTE = (
    "Origin parameters are stored as 32-bit floats, so a typed coordinate "
    "comes back rounded — about a meter at this latitude. That offsets the "
    "whole track equally and does not affect its shape.")


def float32(value: float) -> float:
    """What a parameter will actually hold, so the readback can be compared.

    Comparing a typed 47.62691 with a read-back 47.626911 and calling it a
    mismatch would have the operator chasing a rounding they cannot fix.
    """
    import struct
    return struct.unpack("<f", struct.pack("<f", value))[0]


# --------------------------------------------------------------------------
#  What the vehicle has
# --------------------------------------------------------------------------


@dataclass
class Family:
    """One origin parameter family, as found on this vehicle."""

    prefix: str
    present: bool
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None

    @property
    def names(self) -> tuple[str, str, str]:
        return FAMILIES[self.prefix]

    @property
    def configured(self) -> bool:
        """Does it hold a coordinate, as opposed to existing at its defaults?"""
        return M.valid_latlon(self.lat, self.lon)

    def note(self) -> str:
        return FAMILY_NOTE.get(self.prefix, "")

    def text(self) -> str:
        if not self.configured:
            return "present, not set"
        return f"{self.lat:.6f}, {self.lon:.6f} @ {self.alt or 0:.1f} m"


@dataclass
class OriginState:
    """Everything known about this vehicle's origin, and how sure we are.

    The four questions the under-map strip answers, kept apart on purpose:
    what mechanism *could* set it, what is *saved*, what the EKF *actually
    has*, and whether anyone has *checked*.
    """

    #: Every family found in the parameter dump.
    families: list[Family] = field(default_factory=list)
    #: Which one this program will write, chosen by `authority`.
    authority: str = ""
    authority_reason: str = ""
    #: True/False/None for "does the EKF have an origin". None means nobody
    #: has asked — ArduPilot does not stream GPS_GLOBAL_ORIGIN.
    active: bool | None = None
    active_lat: float | None = None
    active_lon: float | None = None
    active_alt_m: float | None = None
    #: Monotonic clock when the active origin was last confirmed.
    confirmed_mono: float | None = None
    #: What this laptop proposes to set, before it has been applied.
    proposed: tuple[float, float, float] | None = None
    proposed_label: str = ""
    #: Warnings the operator has to see before flying.
    warnings: list[str] = field(default_factory=list)
    scripting_enabled: bool | None = None
    firmware: str = ""

    @property
    def configured_families(self) -> list[Family]:
        return [f for f in self.families if f.configured]

    def summary(self) -> str:
        """The one line the under-map strip shows."""
        if self.active is True and self.active_lat is not None:
            age = ""
            if self.confirmed_mono is not None:
                age = f" ({time.monotonic() - self.confirmed_mono:.0f} s ago)"
            return (f"Origin confirmed {self.active_lat:.5f}, "
                    f"{self.active_lon:.5f}{age}")
        if self.active is False:
            return "No EKF origin — position will be local only"
        saved = self.configured_families
        if saved:
            return (f"Origin not confirmed; {saved[0].prefix}* holds "
                    f"{saved[0].lat:.5f}, {saved[0].lon:.5f}")
        return "Origin not confirmed and none saved"


def detect(params: dict[str, float] | None, *, firmware: str = "",
           active: bool | None = None) -> OriginState:
    """Work out which mechanism owns the origin on this vehicle.

    Looks at every family, because the interesting case is more than one being
    present: a fleet that has changed applets leaves the old prefix behind
    holding real-looking coordinates that nothing reads.
    """
    st = OriginState(active=active, firmware=firmware)
    if params is None:
        st.warnings.append(
            "Parameters have not been read, so the origin mechanism is unknown.")
        return st

    for prefix, (lat_n, lon_n, alt_n) in FAMILIES.items():
        present = lat_n in params and lon_n in params
        if not present:
            continue
        st.families.append(Family(
            prefix=prefix, present=True,
            lat=params.get(lat_n), lon=params.get(lon_n),
            alt=params.get(alt_n)))

    scr = params.get("SCR_ENABLE")
    st.scripting_enabled = None if scr is None else bool(scr)

    if not st.families:
        st.authority = "gcs"
        st.authority_reason = (
            "No origin parameter family exists on this vehicle, so the origin "
            "can only be set from this laptop over MAVLink. That takes effect "
            "immediately and does not survive a reboot.")
    elif len(st.families) == 1:
        fam = st.families[0]
        st.authority = fam.prefix
        st.authority_reason = (
            f"{fam.prefix}* is the only origin family on this vehicle — "
            f"{fam.note()}.")
    else:
        # Prefer a family that already holds a coordinate; if several do, that
        # itself is the warning.
        configured = st.configured_families
        chosen = configured[0] if configured else st.families[0]
        st.authority = chosen.prefix
        others = ", ".join(f.prefix + "*" for f in st.families
                           if f.prefix != chosen.prefix)
        st.authority_reason = (
            f"{chosen.prefix}* chosen as the origin authority; {others} also "
            f"exists on this vehicle.")
        st.warnings.append(
            f"More than one origin parameter family is present ({others} "
            f"besides {chosen.prefix}*). Only the one a running script reads "
            f"has any effect; the rest look like configuration and do nothing. "
            f"Remove the unused ones.")

    if st.scripting_enabled is False and st.authority not in ("gcs", "AHRS_ORIGIN_"):
        st.warnings.append(
            "SCR_ENABLE is 0, so no Lua applet is running. Whatever "
            f"{st.authority}* holds cannot reach the autopilot.")

    if st.authority == "AHRS_ORIGIN_":
        opts = params.get("AHRS_OPTIONS")
        if opts is not None and not (int(opts) & AHRS_OPTION_RESTORE_ORIGIN):
            st.warnings.append(
                "AHRS_ORIGIN_* is present but AHRS_OPTIONS bit 4 (restore "
                "origin without GPS) is clear, so the saved origin is not "
                "applied at boot.")

    # The case that cost this fleet a dive.
    saved = st.configured_families
    if active is False and saved:
        st.warnings.append(
            f"{saved[0].prefix}* holds {saved[0].lat:.5f}, {saved[0].lon:.5f} "
            f"and the EKF has no origin. Parameters created by a Lua applet "
            f"stay in storage after the applet is removed, so a saved "
            f"coordinate is not evidence that anything is reading it — this "
            f"is the state that produced a whole dive with no coordinates on "
            f"18 September 2026.")
    return st


def read_active(mav, *, request: bool = False) -> tuple[bool | None, dict]:
    """Ask whether the EKF has an origin. (is_set, detail).

    ArduPilot does not stream `GPS_GLOBAL_ORIGIN`. Two ways to find out, and
    the difference matters:

    * Passively — mavlink2rest may still hold one from when something else
      asked. Free, and possibly minutes old, so its age is reported.
    * Actively, with `request=True` — sends `MAV_CMD_REQUEST_MESSAGE`. That is
      traffic to the vehicle, so it happens on an operator's button and never
      in the poll loop.

    A `None` return is "nobody has asked", which is not "there is no origin".
    """
    detail: dict = {"requested": bool(request)}
    if request:
        ans = mav.request_message(GPS_GLOBAL_ORIGIN_ID,
                                  reason="confirm the EKF origin")
        detail["request_ok"] = ans.ok
        if not ans.ok:
            detail["error"] = ans.error
        else:
            # The autopilot answers on its own schedule; a short settle keeps
            # the immediately-following read from seeing the previous value.
            time.sleep(0.5)

    s = mav.read("GPS_GLOBAL_ORIGIN")
    detail["fresh"] = s.fresh
    detail["counter"] = s.counter
    if not s.message:
        return None, detail

    lat = s.num("latitude")
    lon = s.num("longitude")
    alt = s.num("altitude")
    if lat is None or lon is None:
        return None, detail
    lat, lon = lat * 1e-7, lon * 1e-7
    detail.update({"lat": lat, "lon": lon,
                   "alt_m": None if alt is None else alt / 1000.0})
    # ArduPilot only ever sends this message when it has an origin, so a
    # message at all is the positive answer -- but a 0/0 body is the
    # uninitialised one, and must not be taken as a real origin off West
    # Africa.
    if not M.valid_latlon(lat, lon):
        detail["note"] = "GPS_GLOBAL_ORIGIN reports 0, 0 — not a real origin"
        return False, detail
    return True, detail


# --------------------------------------------------------------------------
#  Setting it
# --------------------------------------------------------------------------


@dataclass
class ApplyStep:
    """One step of an origin operation, as it turned out."""

    what: str
    ok: bool | None = None
    detail: str = ""

    def line(self) -> str:
        mark = "…" if self.ok is None else ("✓" if self.ok else "✗")
        return f"{mark} {self.what}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class ApplyResult:
    """What an origin operation did, step by step. Never just True/False."""

    steps: list[ApplyStep] = field(default_factory=list)
    #: "succeeded", "failed", "partial" or "unconfirmed".
    outcome: str = "unconfirmed"
    message: str = ""

    def add(self, what: str, ok: bool | None = None, detail: str = "") -> ApplyStep:
        s = ApplyStep(what, ok, detail)
        self.steps.append(s)
        return s

    def report(self) -> str:
        return "\n".join(s.line() for s in self.steps)


def set_origin_now(mav, lat: float, lon: float, alt_m: float = 0.0,
                   *, confirm: bool = True) -> ApplyResult:
    """Set the EKF origin from this laptop, and confirm it took.

    `SET_GPS_GLOBAL_ORIGIN` is the supported GCS operation and takes effect
    immediately. It does **not** persist: a reboot loses it, which is what the
    applet parameters are for.

    Altitude goes in as millimeters above the WGS-84 ellipsoid, and is the one
    field worth being careful about. The DVL extension's own origin helper
    hard-codes zero here; for a dive that is harmless, because the vertical
    channel comes from the barometer and never from the origin, but it is a
    datum and it is recorded rather than assumed.
    """
    res = ApplyResult()
    lat, lon, alt_m = validate(lat, lon, alt_m)

    tpl = mav.helper_template("SET_GPS_GLOBAL_ORIGIN")
    if tpl is None:
        res.add("ask mavlink2rest for a SET_GPS_GLOBAL_ORIGIN template", False,
                "the service did not answer")
        res.outcome = "failed"
        res.message = ("mavlink2rest would not supply a message template, so "
                       "nothing was sent.")
        return res
    res.add("ask mavlink2rest for a SET_GPS_GLOBAL_ORIGIN template", True)

    msg = tpl.setdefault("message", {})
    msg["latitude"] = int(round(lat * 1e7))
    msg["longitude"] = int(round(lon * 1e7))
    msg["altitude"] = int(round(alt_m * 1000.0))
    msg["target_system"] = mav.system
    msg["time_usec"] = 0

    ans = mav.send(tpl, what=f"SET_GPS_GLOBAL_ORIGIN {lat:.6f},{lon:.6f}")
    res.add(f"send origin {lat:.6f}, {lon:.6f} @ {alt_m:.1f} m", ans.ok,
            ans.error)
    if not ans.ok:
        res.outcome = "failed"
        res.message = f"The origin was not sent: {ans.error}"
        return res

    if not confirm:
        res.outcome = "unconfirmed"
        res.message = "Sent. Not read back."
        return res

    # ArduPilot applies it on its next AHRS update; a moment's settle before
    # asking, then the request-and-read that actually proves it.
    time.sleep(0.6)
    is_set, detail = read_active(mav, request=True)
    if is_set is True:
        got_lat, got_lon = detail.get("lat"), detail.get("lon")
        close = (got_lat is not None and got_lon is not None
                 and abs(got_lat - lat) < 1e-5 and abs(got_lon - lon) < 1e-5)
        res.add("read GPS_GLOBAL_ORIGIN back", close,
                f"{got_lat:.6f}, {got_lon:.6f}" if got_lat is not None else "")
        if close:
            res.outcome = "succeeded"
            res.message = (f"The EKF origin is {got_lat:.6f}, {got_lon:.6f}. "
                           f"It is not stored on the vehicle and will be lost "
                           f"at the next reboot.")
        else:
            res.outcome = "partial"
            res.message = (
                "The vehicle reports a different origin from the one just "
                "sent. An origin already set cannot be moved by "
                "SET_GPS_GLOBAL_ORIGIN — EKF3 refuses to move one it already "
                "has. Reboot the vehicle to set a new one.")
        return res

    if is_set is False:
        res.add("read GPS_GLOBAL_ORIGIN back", False,
                detail.get("note", "the vehicle reports no origin"))
        res.outcome = "failed"
        res.message = (
            "The origin was sent and the vehicle still reports none. On a "
            "DVL-only vehicle this usually means EKF3 was not yet ready to "
            "accept one — it needs the tilt alignment to have completed. Wait "
            "for the vehicle to settle and try again.")
        return res

    res.add("read GPS_GLOBAL_ORIGIN back", None, "no answer")
    res.outcome = "unconfirmed"
    res.message = (
        "The origin was sent and the vehicle did not answer the read-back. "
        "It may have been applied. Do not send it again without checking: a "
        "second attempt against an origin that did take is refused by the "
        "firmware and proves nothing.")
    return res


def save_for_next_boot(setter, family: str, lat: float, lon: float,
                       alt_m: float = 0.0, *, origin_is_set: bool) -> ApplyResult:
    """Write the applet's parameters, so the origin survives a reboot.

    `setter(name, value) -> (ok, detail)` writes one parameter and reads it
    back; it is passed in rather than imported so that this stays testable
    without a vehicle.

    **Refuses to run while the EKF has no origin.** That is the whole safety
    property. The applet's guard passes as soon as any one of its three
    parameters is non-zero, so a copy sitting in its five-second retry loop
    can act on a latitude that has been written and a longitude that has not,
    and lock in an origin on the prime meridian. Once `ahrs:get_origin()` is
    true the applet returns before it looks at the parameters at all, and no
    ordering can hurt. So: set the origin first, then save it.
    """
    res = ApplyResult()
    lat, lon, alt_m = validate(lat, lon, alt_m)

    if family == "gcs":
        res.outcome = "failed"
        res.message = ("This vehicle has no origin parameter family, so there "
                       "is nothing to save into. The origin has to be set "
                       "from the laptop after each reboot.")
        return res
    if family not in FAMILIES:
        res.outcome = "failed"
        res.message = f"{family} is not an origin parameter family."
        return res

    if not origin_is_set:
        res.outcome = "failed"
        res.message = (
            "Set the origin on the vehicle first, then save it.\n\n"
            "While the EKF has no origin, an ahrs-set-origin applet may be "
            "running its five-second retry loop. Its check passes as soon as "
            "any one of the three parameters is non-zero, so writing them one "
            "at a time can hand it half a coordinate and lock in an origin "
            "somewhere in the Atlantic. Once the origin is set the applet "
            "stops before it reads them, and the order stops mattering.")
        return res

    lat_n, lon_n, alt_n = FAMILIES[family]
    # Altitude first, longitude next, latitude last: this is belt and braces
    # behind the guard above, on the reasoning that latitude alone is the
    # least useful partial tuple for anything to act on.
    plan = ((alt_n, alt_m), (lon_n, lon), (lat_n, lat))
    wrote: list[str] = []
    for name, value in plan:
        ok, detail = setter(name, value)
        res.add(f"set {name} = {value:g}", ok, detail)
        if not ok:
            res.outcome = "partial" if wrote else "failed"
            res.message = (
                f"{name} could not be written"
                + (f" after {', '.join(wrote)} had been" if wrote else "")
                + f". {detail}\n\nThe saved origin is now inconsistent; "
                  f"correct it before the next reboot.")
            return res
        wrote.append(name)

    res.outcome = "succeeded"
    res.message = (
        f"{family}LAT/LON/ALT saved. They take effect at the next reboot, if "
        f"an ahrs-set-origin applet is installed and running to read them — "
        f"saving them is not the same as an origin being set.\n\n{FLOAT32_NOTE}")
    return res
