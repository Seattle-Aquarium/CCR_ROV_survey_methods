"""
One record per flight, instead of six files that disagree.

The first version of this wrote `params_*.json`, `delta_params_*.json`,
`delta_params_*.txt`, `versions_*.json`, `delta_versions_*.json`,
`delta_versions_*.txt` and `laptop_monitor_*.json` -- seven files per flight,
thirty in an afternoon of three. Reading the 11 September logs back showed
four things wrong with that, and none of them were the file count:

**The parameter dump had no provenance.** `{"count": 1020, "parameters": {…}}`
and nothing else: no vehicle, no time, no firmware, no source log. A
parameter set that cannot say which vehicle it came from or when cannot be
compared to another one, which is the only thing anybody wants to do with it.

**A failed read was written as a fact.** One snapshot was taken while the
tether was down. It recorded `blueos: ""` and `containers: []`, the diff
against it reported *twenty extensions and containers removed during the
flight*, and the text file said so in capitals. Nothing was removed. A
reading that did not happen must be absent, never zero -- the same rule the
1 Hz row already follows for a sensor it cannot reach.

**The delta was all noise.** Every change in it was `set_by_autopilot`:
barometer ground pressure, boot count, flight time. The question being asked
is "did somebody turn a knob", and the answer -- none -- was buried under
five entries that move on every flight. Partitioning is not cosmetic here: it
is the difference between a file that answers the question and one that
requires the reader to answer it.

**The numbers were float64 noise.** `0.30000001192092896` for a parameter the
autopilot holds as float32 `0.3`. Unreadable, and worse, two dumps of the
same value can differ in the tail and diff as a change.

So: one `flight_<id>.json`, carrying identity, timing, the full parameter set,
the versions, and the deltas partitioned by who made them -- against arming,
and against the previous flight. Human-readable output is the tear-sheet,
which is better at being read than a text file ever was.

The old files are still *read* by `flightscan`, so an existing folder still
analyses. Nothing here rewrites one.
"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

from . import blueos

#: Bumped when the shape changes in a way a reader must notice. Present from
#: the first version, because the cost of adding it later is going back
#: through every file that does not have it and guessing.
SCHEMA = "utc.flight/1"

FLIGHT_PREFIX = "flight_"


# --------------------------------------------------------------------------
#  numbers
# --------------------------------------------------------------------------


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def clean(value):
    """A parameter value as the shortest decimal that is still the same float32.

    ArduPilot holds parameters as 32-bit floats. Read into Python they become
    doubles, and printing the double writes the binary representation's whole
    tail: `0.30000001192092896` for `0.3`, `0.009999999776482582` for `0.01`.

    That is not only ugly. Two dumps of an unchanged parameter can differ in
    that tail depending on the path they took, and then diff as a change --
    which is a false positive in exactly the file whose job is to have none.

    So: the fewest digits that still round-trip to the same float32. `0.3`
    comes back as `0.3`, and a barometer reading of `101453.421875` keeps
    every digit it needs, because dropping one would land on a different
    float32.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, int):
        return value
    if value != value or value in (float("inf"), float("-inf")):
        return None
    target = _f32(float(value))
    for digits in range(6, 10):
        text = f"{value:.{digits}g}"
        try:
            if _f32(float(text)) == target:
                return float(text)
        except (ValueError, OverflowError):           # pragma: no cover
            break
    return float(value)


def clean_parameters(parameters: dict) -> dict:
    return {name: clean(value) for name, value in sorted(parameters.items())}


def _iso(when: float | None) -> str | None:
    if not when:
        return None
    return datetime.fromtimestamp(when, timezone.utc).isoformat(
        timespec="seconds")


# --------------------------------------------------------------------------
#  did the read happen at all
# --------------------------------------------------------------------------


def versions_were_read(versions: dict | None) -> bool:
    """Is this a reading, or a hole where a reading should be?

    A real answer names BlueOS or lists containers. A request that timed out
    leaves every key present and empty, and the two are indistinguishable
    without somebody deciding -- here -- that a vehicle reporting no BlueOS
    version is a vehicle that did not answer.
    """
    if not versions:
        return False
    return bool(versions.get("blueos")) or bool(versions.get("containers"))


def _mass_disappearance(changes: dict, limit: int = 4) -> bool:
    """Did most of the vehicle's software appear or vanish at once?

    Extensions are installed one or two at a time, on purpose, by a person.
    A diff in which several components appear or disappear together is a
    snapshot that failed, not an afternoon of uninstalling.
    """
    lopsided = 0
    for value in changes.values():
        if isinstance(value, dict):
            before, after = value.get("before"), value.get("after")
        elif isinstance(value, (list, tuple)):
            before, after = (list(value) + [None, None])[:2]
        else:
            continue
        if bool(before) != bool(after):
            lopsided += 1
    return lopsided >= limit


# --------------------------------------------------------------------------
#  the deltas
# --------------------------------------------------------------------------


def partition_parameters(before: dict, after: dict) -> tuple[dict, dict]:
    """(changed by somebody, changed by the autopilot).

    Two dictionaries rather than one with a flag on each entry. The flag was
    already there and it did not help: a reader still had to filter, and the
    header still said "5 changed" when the answer to the question actually
    being asked was none.
    """
    by_operator: dict[str, dict] = {}
    by_autopilot: dict[str, dict] = {}
    for name in sorted(set(before) | set(after)):
        a, b = clean(before.get(name)), clean(after.get(name))
        if a == b:
            continue
        entry = {"before": a, "after": b}
        if a is None:
            entry["note"] = "not present before"
        elif b is None:
            entry["note"] = "no longer present"
        target = by_autopilot if blueos.is_automatic(name) else by_operator
        target[name] = entry
    return by_operator, by_autopilot


def diff_versions(before: dict, after: dict) -> tuple[dict, str]:
    """(changes, why it is empty when it is).

    Refuses rather than reports when either side did not read. A comparison
    against a hole is not a comparison, and reporting it as one is how the
    11 September log came to claim twenty components were removed mid-dive.
    """
    if not versions_were_read(before):
        return {}, "the earlier snapshot did not read; nothing to compare"
    if not versions_were_read(after):
        return {}, "the later snapshot did not read; nothing to compare"
    found = blueos.diff_versions(before, after)
    if not found:
        return {}, ""
    if _mass_disappearance(found):
        return {}, ("most of the vehicle's software appeared or vanished at "
                    "once, which is a snapshot that failed rather than a "
                    "change; suppressed")
    return ({name: {"before": a, "after": b} for name, (a, b) in found.items()},
            "")


# --------------------------------------------------------------------------
#  writing
# --------------------------------------------------------------------------


def build(*, flight_id: str, started: float, ended: float, reason: str,
          rows: int, computer: str, host: str, interface: str,
          opening, closing, brief_disarms: list,
          capabilities: dict, monitor: dict | None = None,
          network: dict | None = None, site: str = "",
          previous: dict | None = None, note: str = "") -> dict:
    """Assemble one flight's record. Pure: takes readings, returns a dict."""
    params_read = bool(getattr(closing, "parameters", None))
    versions = getattr(closing, "versions", {}) or {}
    versions_read = versions_were_read(versions)

    open_params = getattr(opening, "parameters", {}) or {}
    close_params = getattr(closing, "parameters", {}) or {}
    by_operator, by_autopilot = ({}, {})
    compared = ""
    if open_params and close_params:
        by_operator, by_autopilot = partition_parameters(open_params,
                                                         close_params)
        compared = "arming"

    version_changes, version_note = diff_versions(
        getattr(opening, "versions", {}) or {}, versions)

    record = {
        "schema": SCHEMA,
        "flight_id": flight_id,
        "site": site,
        "started": _iso(started),
        "ended": _iso(ended),
        "seconds": round(max(0.0, ended - started), 1),
        "ended_because": reason,
        "note": note,

        "computer": {"name": computer, "interface_to_vehicle": interface},
        "vehicle": {
            "host": host,
            "board": versions.get("board") or None,
            "ardusub": versions.get("ardusub") or None,
            "ardusub_type": versions.get("ardusub_type") or None,
            "blueos": versions.get("blueos") or None,
        },

        "parameters": {
            "read": params_read,
            "taken": _iso(getattr(closing, "taken", None)),
            "read_from": getattr(closing, "parameters_from", "") or None,
            "source": "ArduPilot dataflash log, read-only",
            "count": len(close_params),
            "values": clean_parameters(close_params) if params_read else None,
        },
        "parameters_at_arming": {
            "read": bool(open_params),
            "taken": _iso(getattr(opening, "taken", None)),
            "read_from": getattr(opening, "parameters_from", "") or None,
            "count": len(open_params),
        },
        "versions": {
            "read": versions_read,
            "taken": _iso(getattr(closing, "taken", None)),
            "values": versions if versions_read else None,
            "why_absent": None if versions_read else (
                "the vehicle did not answer when this was taken; recorded as "
                "absent rather than as a vehicle with nothing installed"),
        },

        "changes": {
            "compared_with": compared,
            "parameters_by_operator": by_operator,
            "parameters_by_autopilot": by_autopilot,
            "versions": version_changes,
            "versions_note": version_note or None,
        },
        "since_previous_flight": previous,
        "brief_disarms": brief_disarms,
        "monitor": monitor or {},
        "network": network or {},
        "readings_available_on_this_machine": capabilities,
    }
    return record


def write(folder: Path, record: dict) -> Path:
    path = Path(folder) / f"{FLIGHT_PREFIX}{record['flight_id']}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True,
                               default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
#  comparing with the flight before
# --------------------------------------------------------------------------


def load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_previous(logs_dir: Path, flight_id: str,
                  search_root: Path | None = None) -> Path | None:
    """The most recent flight record before this one.

    Looks in this flight's own logs folder first -- several flights in one
    afternoon is the common case -- and then across sibling flight folders,
    which is how "what changed since last week" gets answered. Flight ids
    sort chronologically because they are `YYYY-MM-DD_HHMM`, which is the
    whole reason they are shaped that way.
    """
    logs_dir = Path(logs_dir)
    found: list[Path] = []
    for path in logs_dir.glob(f"{FLIGHT_PREFIX}*.json"):
        if path.stem[len(FLIGHT_PREFIX):] < flight_id:
            found.append(path)
    if not found and search_root is not None:
        root = Path(search_root)
        for sibling in root.iterdir() if root.is_dir() else []:
            logs = sibling / "logs"
            if not logs.is_dir() or logs == logs_dir:
                continue
            for path in logs.glob(f"{FLIGHT_PREFIX}*.json"):
                if path.stem[len(FLIGHT_PREFIX):] < flight_id:
                    found.append(path)
    if not found:
        return None
    return max(found, key=lambda p: p.stem)


def compare_with_previous(previous_path: Path, parameters: dict,
                          versions: dict) -> dict | None:
    """What has moved since the last flight this vehicle recorded.

    The comparison a survey lead actually wants before a dive: not "did
    anything change while I was down", which is almost always no, but "is
    this vehicle configured the way it was the last time it worked".
    """
    earlier = load(previous_path)
    if not earlier:
        return None
    earlier_params = ((earlier.get("parameters") or {}).get("values")) or {}
    if not earlier_params or not parameters:
        return None
    by_operator, by_autopilot = partition_parameters(earlier_params, parameters)
    earlier_versions = ((earlier.get("versions") or {}).get("values")) or {}
    version_changes, version_note = diff_versions(earlier_versions, versions)
    return {
        "flight_id": earlier.get("flight_id"),
        "flight_ended": earlier.get("ended"),
        "file": previous_path.name,
        "parameters_by_operator": by_operator,
        "parameters_by_autopilot_count": len(by_autopilot),
        "versions": version_changes,
        "versions_note": version_note or None,
    }


def summarise(record: dict) -> list[str]:
    """The flight record as the handful of lines worth saying out loud."""
    out: list[str] = []
    changes = record.get("changes") or {}
    operator = changes.get("parameters_by_operator") or {}
    if operator:
        out.append(f"{len(operator)} parameter(s) changed by hand during the "
                   f"flight: " + ", ".join(sorted(operator)[:6]))
    else:
        out.append("No parameter was changed by hand during the flight.")
    if not (record.get("versions") or {}).get("read"):
        out.append("The closing software snapshot did not read — recorded as "
                   "absent, not as an empty vehicle.")
    note = changes.get("versions_note")
    if note:
        out.append(f"Software comparison: {note}.")
    elif changes.get("versions"):
        out.append(f"{len(changes['versions'])} software component(s) changed "
                   f"during the flight.")
    previous = record.get("since_previous_flight") or {}
    if previous:
        changed = previous.get("parameters_by_operator") or {}
        if changed:
            out.append(f"Since {previous.get('flight_id')}: "
                       f"{len(changed)} parameter(s) differ — "
                       + ", ".join(sorted(changed)[:6]))
        else:
            out.append(f"Identical configuration to "
                       f"{previous.get('flight_id')}.")
    return out
