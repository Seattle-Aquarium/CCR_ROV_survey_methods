"""
What one reading is, and the five things it can be.

Every number on the Navigation chapter arrives here first. The type exists
because of one failure mode that field software falls into again and again: a
gauge initialized to zero looks exactly like a gauge reading zero. An altitude
of 0.0 m is either "the ROV is on the bottom" or "nothing has ever answered",
and those must never share a pixel.

So a `Reading` is never bare. It carries **what it is** (unit, frame), **where
it came from** (vehicle, component, sensor, message), **when** (the source's
own timestamp if it has one, when this laptop received it, and a monotonic age
that a clock change cannot corrupt) and **whether it means anything**:

=================  ===========================================================
`UNSUPPORTED`      this vehicle/firmware/extension cannot produce it at all.
                   Shown grayed with a reason, never as a fault.
`NEVER_RECEIVED`   it could exist, and nothing has arrived. The start-up state.
`INVALID`          it arrived and says so -- a sentinel, an out-of-range value,
                   a validity flag that is false, a DVL with no bottom lock.
`STALE`            it arrived, it was good, and it has stopped. The last value
                   is kept and marked; it is not carried forward silently.
`OK`               fresh and valid.
=================  ===========================================================

The distinction between `INVALID` and `STALE` is not pedantry. A DVL that has
lost bottom lock is reporting honestly and its velocity is *unknown*; a DVL
whose packets have stopped may still be locked on. The first must not display
0.0 m/s and the second must not display the last good speed as though it were
current. Both used to, in the pipeline this replaces.

`unknown` is a module-level convenience for "this reading has never arrived",
and it is what every field of a fresh `NavSnapshot` holds. There is deliberately
no constructor that defaults a value to zero.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class Quality(str, Enum):
    """Why a reading may or may not be believed. Ordered worst to best."""

    UNSUPPORTED = "unsupported"
    NEVER_RECEIVED = "never"
    INVALID = "invalid"
    STALE = "stale"
    OK = "ok"

    @property
    def usable(self) -> bool:
        """Is the value safe to steer by, plot, or integrate?"""
        return self is Quality.OK

    @property
    def has_value(self) -> bool:
        """Is there a number to show at all, even if it must be marked?"""
        return self in (Quality.OK, Quality.STALE)

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    Quality.UNSUPPORTED: 0,
    Quality.NEVER_RECEIVED: 1,
    Quality.INVALID: 2,
    Quality.STALE: 3,
    Quality.OK: 4,
}


#: What the pixels are allowed to say for a reading with no number behind it.
#: One string, everywhere, so "--" and "n/a" and "0" cannot drift apart across
#: the page.
NO_VALUE = "—"


@dataclass(frozen=True)
class Source:
    """Where a reading came from, precisely enough to argue about later.

    `system`/`component` are the MAVLink identities: the autopilot is 1/1, the
    DVL extension injects as 255/0, a ground station as 255/240. They are
    recorded because "DISTANCE_SENSOR said 0.8 m" is ambiguous on this fleet --
    the DVL sends its own and the autopilot echoes one back, and the two are
    different observations of different things.
    """

    #: A short stable key, e.g. "mav:RANGEFINDER" or "ext:dvl".
    key: str = ""
    #: The message or endpoint it was read from.
    message: str = ""
    system: int | None = None
    component: int | None = None
    #: A sensor instance where the message carries one (DISTANCE_SENSOR id,
    #: BATTERY_STATUS instance, compass number).
    instance: int | None = None
    #: Free text naming the physical device when it is known.
    device: str = ""

    def label(self) -> str:
        bits = [self.message or self.key]
        if self.system is not None and self.component is not None:
            bits.append(f"{self.system}/{self.component}")
        if self.instance is not None:
            bits.append(f"#{self.instance}")
        if self.device:
            bits.append(self.device)
        return " · ".join(b for b in bits if b)


UNKNOWN_SOURCE = Source()


@dataclass(frozen=True)
class Reading:
    """One measurement, and everything needed to decide whether to trust it.

    Immutable on purpose. A snapshot handed to the window is a value the window
    may hold for as long as it likes; a collector that could mutate it under
    the drawing code would reintroduce exactly the class of bug the one-worker
    rule in `gui/shell.py` exists to prevent.
    """

    value: Any = None
    unit: str = ""
    quality: Quality = Quality.NEVER_RECEIVED
    #: The coordinate frame, where one applies: "ned", "body", "earth",
    #: "wgs84". Empty for scalars that have no frame.
    frame: str = ""
    source: Source = UNKNOWN_SOURCE
    #: The originating system's own timestamp in seconds, when it publishes
    #: one. Not this laptop's clock.
    source_time: float | None = None
    #: This laptop's wall clock when the sample was accepted, for the log.
    recv_time: float | None = None
    #: This laptop's monotonic clock when the sample was accepted. Age is
    #: computed from this so that an NTP step cannot make a reading look fresh.
    recv_mono: float | None = None
    #: Why it is unsupported, invalid or stale -- shown to the operator.
    note: str = ""

    # -- asking about it ------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.quality is Quality.OK

    def age(self, now_mono: float | None = None) -> float | None:
        """Seconds since this sample was accepted, or None if never."""
        if self.recv_mono is None:
            return None
        return max(0.0, (time.monotonic() if now_mono is None else now_mono)
                   - self.recv_mono)

    def number(self) -> float | None:
        """The value as a float when it is usable, else None.

        The gate is `usable`, not "is it a number": a stale 0.8 m is a float
        and must still not be fed to a gauge as a live altitude.
        """
        if not self.quality.usable:
            return None
        return _as_float(self.value)

    def held(self) -> float | None:
        """The last number, usable or merely stale -- for a marked display.

        Never for arithmetic. The gauges draw a stale icon with this; nothing
        integrates it, and the return bearing refuses it.
        """
        if not self.quality.has_value:
            return None
        return _as_float(self.value)

    # -- deriving one from another --------------------------------------

    def staled(self, note: str = "") -> Reading:
        """The same reading, demoted because it has stopped arriving."""
        if self.quality is not Quality.OK:
            return self
        return replace(self, quality=Quality.STALE,
                       note=note or "no new sample")

    def invalid(self, note: str) -> Reading:
        """The same source, reporting that its value means nothing."""
        return replace(self, quality=Quality.INVALID, note=note)

    def text(self, digits: int = 1, *, unit: bool = True) -> str:
        """The value as the operator should read it, or the no-value mark."""
        v = self.held()
        if v is None:
            if isinstance(self.value, str) and self.quality.has_value:
                return self.value
            return NO_VALUE
        if isinstance(self.value, str):
            return self.value
        s = f"{v:.{digits}f}" if digits >= 0 else f"{v:.0f}"
        return f"{s} {self.unit}" if unit and self.unit else s

    def to_json(self) -> dict:
        """The form the session log stores. Small, flat and stable."""
        out: dict[str, Any] = {"q": self.quality.value}
        if self.quality.has_value:
            out["v"] = self.value
        if self.unit:
            out["u"] = self.unit
        if self.frame:
            out["frame"] = self.frame
        if self.source.key:
            out["src"] = self.source.key
        if self.source_time is not None:
            out["t_src"] = round(self.source_time, 6)
        if self.recv_time is not None:
            out["t_recv"] = round(self.recv_time, 3)
        if self.note:
            out["note"] = self.note
        return out


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    return None


# --------------------------------------------------------------------------
#  Making readings
# --------------------------------------------------------------------------

#: The reading every field starts as. Explicitly not zero.
unknown = Reading()


def unsupported(note: str, *, unit: str = "", source: Source = UNKNOWN_SOURCE) -> Reading:
    """A reading this vehicle cannot produce, with the reason on the face.

    Used for the things the September 2026 stack genuinely does not offer --
    vessel GNSS fix quality through the external extension's `/status`, for
    one. An operator who can see *why* a box is empty does not spend a dive
    wondering whether something is broken.
    """
    return Reading(quality=Quality.UNSUPPORTED, unit=unit, note=note, source=source)


def good(value: Any, *, unit: str = "", frame: str = "",
         source: Source = UNKNOWN_SOURCE, source_time: float | None = None,
         recv_time: float | None = None, recv_mono: float | None = None,
         note: str = "") -> Reading:
    """A fresh, valid reading.

    `recv_mono` defaults to now; pass it explicitly when several readings come
    out of one message and should share an age to the microsecond.
    """
    return Reading(
        value=value, unit=unit, frame=frame, quality=Quality.OK, source=source,
        source_time=source_time,
        recv_time=time.time() if recv_time is None else recv_time,
        recv_mono=time.monotonic() if recv_mono is None else recv_mono,
        note=note)


def invalid(note: str, *, value: Any = None, unit: str = "",
            source: Source = UNKNOWN_SOURCE,
            recv_mono: float | None = None) -> Reading:
    """A reading that arrived and declared itself meaningless."""
    return Reading(value=value, unit=unit, quality=Quality.INVALID, note=note,
                   source=source, recv_time=time.time(),
                   recv_mono=time.monotonic() if recv_mono is None else recv_mono)


def age_out(r: Reading, max_age: float, *, now_mono: float | None = None,
            note: str = "") -> Reading:
    """`r`, demoted to stale once it is older than `max_age` seconds.

    Applied at the point of display rather than at the point of reading, so
    that the collector stores what actually arrived and the page decides how
    long each kind of measurement stays believable. Depth from a barometer is
    good for seconds; a flight mode is good for a minute.
    """
    if r.quality is not Quality.OK:
        return r
    a = r.age(now_mono)
    if a is None or a <= max_age:
        return r
    return r.staled(note or f"{a:.0f} s old")


# --------------------------------------------------------------------------
#  Positions
# --------------------------------------------------------------------------

#: Latitude/longitude within a hundredth of a degree of the null island at
#: 0 N 0 E. Every part of this stack initializes coordinates to zero -- the
#: WL UGPS external extension's `/status` returns latitude 0 and longitude 0
#: before a single GGA sentence has arrived -- and a map that plots it puts
#: the vessel in the Gulf of Guinea. Rejected wherever coordinates enter.
NULL_ISLAND_DEG = 0.01


def valid_latlon(lat: Any, lon: Any) -> bool:
    """Is this a coordinate pair worth plotting?

    Range first, because upstream metadata is not to be trusted: the check is
    made here rather than assuming a sensor that says it is valid is. Null
    island second, for the reason above -- a real survey at 0/0 is not a case
    this program needs to serve, and the cost of the false negative is one
    warning against a whole dive plotted in the wrong ocean.
    """
    flat, flon = _as_float(lat), _as_float(lon)
    if flat is None or flon is None:
        return False
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        return False
    return not (abs(flat) < NULL_ISLAND_DEG and abs(flon) < NULL_ISLAND_DEG)


@dataclass(frozen=True)
class Fix:
    """A position, with where it came from and how much to believe it.

    `kind` is the provenance an operator has to be able to see at a glance,
    because the same two numbers mean very different things:

    ``ekf``        the autopilot's own global estimate (`GLOBAL_POSITION_INT`).
    ``dead``       dead-reckoned: local NED projected through a known origin,
                   right relative to itself and free to drift as a whole.
    ``acoustic``   from the Water Linked acoustic solution.
    ``vessel``     the surface vessel, not the ROV.
    ``manual``     typed or picked off the map by the operator.
    """

    lat: float
    lon: float
    kind: str = "ekf"
    quality: Quality = Quality.OK
    source: Source = UNKNOWN_SOURCE
    recv_mono: float | None = None
    recv_time: float | None = None
    #: Horizontal uncertainty in meters, only when something actually
    #: estimated one. Never invented to draw a circle.
    accuracy_m: float | None = None
    note: str = ""
    #: Increments whenever the estimator resets or the origin moves, so the
    #: map can break the track instead of drawing a line across the jump.
    segment: int = 0

    def age(self, now_mono: float | None = None) -> float | None:
        if self.recv_mono is None:
            return None
        return max(0.0, (time.monotonic() if now_mono is None else now_mono)
                   - self.recv_mono)

    def to_json(self) -> dict:
        out = {"lat": round(self.lat, 8), "lon": round(self.lon, 8),
               "kind": self.kind, "q": self.quality.value, "seg": self.segment}
        if self.accuracy_m is not None:
            out["acc_m"] = round(self.accuracy_m, 2)
        if self.source.key:
            out["src"] = self.source.key
        if self.recv_time is not None:
            out["t_recv"] = round(self.recv_time, 3)
        if self.note:
            out["note"] = self.note
        return out


# --------------------------------------------------------------------------
#  A whole page's worth
# --------------------------------------------------------------------------


@dataclass
class LinkState:
    """What the page says about its own connection, above everything else.

    A dashboard whose numbers are all fine but whose link died two minutes ago
    is the most dangerous thing it can be, so this is never inferred from the
    readings: it is the collector's own account of itself.
    """

    #: "live", "replay" or "off".
    mode: str = "off"
    connected: bool = False
    host: str = ""
    #: Identifies one unbroken run of the collector. Changes on reconnect.
    session: str = ""
    #: The vehicle's boot identity, so a vehicle reboot mid-dive is visible.
    boot_id: str = ""
    #: Set when the vehicle appears to have restarted under us.
    restarted: bool = False
    #: Seconds since anything at all was successfully read.
    last_ok_age: float | None = None
    #: The most recent failure, kept until something succeeds.
    problem: str = ""
    #: Samples the collector had to drop because a consumer was slow.
    dropped: int = 0

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def line(self) -> str:
        if self.mode == "off":
            return "Not connected"
        where = self.host or "replay"
        if self.problem:
            return f"{where}: {self.problem}"
        if not self.connected:
            return f"{where}: no answer"
        age = "" if self.last_ok_age is None else f" · {self.last_ok_age:.0f} s ago"
        return f"{where}{age}"


@dataclass
class NavSnapshot:
    """Everything the Navigation chapter draws, as of one instant.

    One object, replaced whole. The window never sees a half-updated set of
    readings, and a consumer that holds one for a second is holding a
    consistent second-old picture rather than a mixture.

    Every field starts `unknown`. There is no zero anywhere in this default.
    """

    link: LinkState = field(default_factory=LinkState)
    #: This laptop's monotonic clock when the snapshot was assembled.
    mono: float = field(default_factory=time.monotonic)
    wall: float = field(default_factory=time.time)

    # -- flight instruments ---------------------------------------------
    mode: Reading = unknown                 # flight mode name
    armed: Reading = unknown
    altitude: Reading = unknown             # m above the bottom
    surftrak_target: Reading = unknown      # m, only when genuinely valid
    depth: Reading = unknown                # m, negative down
    speed: Reading = unknown                # m/s horizontal over ground
    heading: Reading = unknown              # deg true-ish, see note
    roll: Reading = unknown
    pitch: Reading = unknown
    yaw: Reading = unknown
    climb: Reading = unknown

    # -- power ------------------------------------------------------------
    voltage: Reading = unknown              # V
    current: Reading = unknown              # A
    watts: Reading = unknown                # W, computed V*I
    energy_wh: Reading = unknown            # Wh this session
    peak_w: Reading = unknown               # observed peak this session

    # -- position ---------------------------------------------------------
    rov_fix: Fix | None = None
    vessel_fix: Fix | None = None
    vessel_heading: Reading = unknown       # deg true, from HDT only
    local_ned: Reading = unknown            # (n, e, d) meters, as a tuple

    # -- navigation suite ---------------------------------------------------
    dvl: dict[str, Reading] = field(default_factory=dict)
    ugps: dict[str, Reading] = field(default_factory=dict)
    vessel: dict[str, Reading] = field(default_factory=dict)
    ekf: dict[str, Reading] = field(default_factory=dict)
    #: Message name -> how it is arriving. Drives the message-health panel.
    messages: dict[str, MessageHealth] = field(default_factory=dict)
    #: Parameter name -> value, as last read. Empty until something reads them.
    params: dict[str, float] = field(default_factory=dict)
    params_age: float | None = None

    def get(self, group: str, key: str) -> Reading:
        """A reading out of one of the grouped dicts, never KeyError."""
        return getattr(self, group, {}).get(key, unknown)


@dataclass
class MessageHealth:
    """How one MAVLink message is actually arriving.

    mavlink2rest publishes a `status.time` block with a counter, a first and
    last update time and a measured frequency. That is what makes honest
    freshness possible over HTTP at all: a GET that succeeds proves the *server*
    answered, and says nothing about whether the vehicle has sent the message
    since the last GET. The counter is what proves that.
    """

    name: str
    #: mavlink2rest's own count of how many have arrived. Its *change* between
    #: polls is the only evidence the message is still flowing.
    counter: int | None = None
    #: The vehicle-side frequency mavlink2rest measured, Hz.
    frequency: float | None = None
    #: Monotonic clock when this laptop last saw the counter move.
    last_change_mono: float | None = None
    #: Monotonic clock of the last successful read, moved or not.
    last_read_mono: float | None = None
    #: True once a counter has ever moved.
    seen: bool = False
    #: Set when the counter went backwards -- the vehicle or the service
    #: restarted, and anything derived from this message is from before.
    reset: bool = False
    error: str = ""

    def age(self, now_mono: float | None = None) -> float | None:
        """Seconds since a *new* message arrived, not since the last GET."""
        if self.last_change_mono is None:
            return None
        return max(0.0, (time.monotonic() if now_mono is None else now_mono)
                   - self.last_change_mono)

    def quality(self, max_age: float, now_mono: float | None = None) -> Quality:
        if self.error:
            return Quality.INVALID
        if not self.seen:
            return Quality.NEVER_RECEIVED
        a = self.age(now_mono)
        if a is None:
            return Quality.NEVER_RECEIVED
        return Quality.OK if a <= max_age else Quality.STALE
