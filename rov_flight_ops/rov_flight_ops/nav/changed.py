"""
"What changed?" — everything the session recorded around one moment.

The whole value of this is that it does **not** answer the question. It puts
what was written down next to when it happened and leaves the operator to
read it, because the alternative is a program that says "the DVL dropped out
because the parameters changed" on the evidence that the two happened eleven
seconds apart.

Two things arriving close together is a fact about their timing. It is
evidence of a shared cause only with an argument this module does not have,
so every rendering here says how far apart they were and nothing else. The
word "because" does not appear in any output, and a test enforces that.

Reuses the navigation session log, which is already being written a line at a
time for exactly this — no second collector, no second file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import session as SESS

#: How far either side of the chosen moment to look, in seconds. Sixty before
#: is enough to catch what was already going wrong; thirty after catches the
#: recovery, which is as informative as the failure.
BEFORE_S = 60.0
AFTER_S = 30.0

#: Never return more than this, however busy the log. A window that scrolls
#: for a minute is a window nobody reads on a deck.
MAX_ROWS = 120

#: Event kinds worth aligning, grouped by what an operator would call them.
GROUPS = {
    "message": ("message_gap", "message_reset", "telemetry_lost",
                "telemetry_restored"),
    "quality": ("dvl_lock", "acoustic_fix", "quality_change", "track_break",
                "position_jump"),
    "configuration": ("param_change", "profile_applied", "profile_selected",
                      "origin_set", "origin_confirmed", "site_selected"),
    "restart": ("vehicle_restart", "service_restart", "collector_started",
                "collector_stopped"),
    "laptop": ("app_started", "link_state", "network", "flight_folder"),
    "plan": ("plan_edit", "waypoint", "lane_state"),
}

#: Which group a kind belongs to, built once.
_GROUP_OF = {k: g for g, kinds in GROUPS.items() for k in kinds}


@dataclass
class Aligned:
    """One recorded event, with its offset from the moment being examined."""

    kind: str
    group: str
    offset_s: float
    row: dict = field(default_factory=dict)

    def line(self) -> str:
        sign = "+" if self.offset_s >= 0 else "−"
        gap = f"{sign}{abs(self.offset_s):.1f} s"
        detail = _detail(self.row)
        return f"{gap:>9}  {self.kind}{('  ' + detail) if detail else ''}"


@dataclass
class Window:
    """Everything recorded around one moment, and what is missing from it."""

    at_mono: float
    rows: list[Aligned] = field(default_factory=list)
    truncated: bool = False
    note: str = ""

    def by_group(self) -> dict[str, list[Aligned]]:
        out: dict[str, list[Aligned]] = {}
        for r in self.rows:
            out.setdefault(r.group, []).append(r)
        return out

    def summary(self) -> str:
        """A count per group. Never a cause, and never an ordering claim.

        Deliberately flat: "3 message, 2 configuration" invites the operator
        to look, where "configuration changed, then the messages stopped"
        would be telling them a story the timestamps do not support.
        """
        groups = self.by_group()
        if not groups:
            return ("nothing else was recorded in this window — which is "
                    "itself worth knowing")
        bits = [f"{len(v)} {k}" for k, v in sorted(groups.items())]
        out = ", ".join(bits)
        if self.truncated:
            out += f" (showing the nearest {MAX_ROWS})"
        return out + ". Proximity in time is not a cause."


def _detail(row: dict) -> str:
    """The interesting fields of an event, short enough for one line."""
    skip = {"t", "mono", "kind"}
    bits = []
    for k, v in row.items():
        if k in skip:
            continue
        text = f"{v}"
        if len(text) > 40:
            text = text[:37] + "…"
        bits.append(f"{k}={text}")
        if len(bits) >= 4:
            break
    return " ".join(bits)


def around(events: list[dict], at_mono: float, *, before: float = BEFORE_S,
           after: float = AFTER_S) -> Window:
    """Everything recorded between `at_mono - before` and `at_mono + after`.

    Monotonic, not wall clock: the wall clock on a laptop that has just
    synchronised can move backwards by seconds, and an alignment built on it
    would silently reorder the very events being examined.
    """
    win = Window(at_mono=at_mono)
    picked: list[Aligned] = []
    for row in events:
        mono = row.get("mono")
        if not isinstance(mono, (int, float)):
            continue
        offset = mono - at_mono
        if offset < -abs(before) or offset > abs(after):
            continue
        kind = str(row.get("kind", ""))
        picked.append(Aligned(kind=kind, group=_GROUP_OF.get(kind, "other"),
                              offset_s=offset, row=row))

    picked.sort(key=lambda a: a.offset_s)
    if len(picked) > MAX_ROWS:
        # Keep the ones nearest the moment, then restore time order.
        picked.sort(key=lambda a: abs(a.offset_s))
        picked = picked[:MAX_ROWS]
        picked.sort(key=lambda a: a.offset_s)
        win.truncated = True
    win.rows = picked

    if not picked:
        win.note = ("nothing was recorded in this window. That rules out "
                    "anything the session log watches; it does not rule out "
                    "anything it does not.")
    return win


def degradations(events: list[dict]) -> list[dict]:
    """The moments worth asking "what changed?" about.

    Only things that were *recorded as* a degradation. Nothing is inferred
    from the shape of the data here, because a moment this module invented
    would send the operator looking for a cause of something that may not
    have happened.
    """
    want = ("track_break", "position_jump", "telemetry_lost",
            "message_gap", "message_reset", "quality_change")
    out = [r for r in events if r.get("kind") in want
           and isinstance(r.get("mono"), (int, float))]
    out.sort(key=lambda r: r["mono"])
    return out


def from_session(path: Path | str, at_mono: float, **kw) -> Window:
    """Read a session log and align around a moment in it."""
    return around(SESS.read_events(Path(path)), at_mono, **kw)
