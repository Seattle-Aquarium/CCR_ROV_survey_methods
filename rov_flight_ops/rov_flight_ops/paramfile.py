"""
A vehicle's reference parameters, as one file a person can review on GitHub.

`vehicle_parameters/<vehicle>.params` is the configuration a vehicle is meant
to have. It is made from a flight this program already recorded -- the
`flight_<id>.json` it writes after every dive carries the whole parameter set,
read from the autopilot's own dataflash log -- or from a dataflash log
directly, and it is what the vehicle is checked against before a dive.

**The format is ArduPilot's own.** One `NAME,VALUE` per line, sorted, which is
what MAVProxy's `param load` and Mission Planner read. That choice is for the
review, not the tools: a reference refreshed after somebody changed the EKF
sources shows up in a pull request as exactly the lines that moved, where a
JSON dump or a spreadsheet would show the whole file. Provenance -- which
vehicle, which firmware, which flight and log it came from -- sits in `#`
lines at the top as `# key: value`, which those tools skip.

**Values are written as the autopilot holds them.** ArduPilot keeps every
parameter as a float32, and `flightfile.clean` gives the fewest digits that are
still that float32: `0.3`, not `0.30000001192092896`. An integral value is
written without its `.0`, as the ground-station tools write it.

**Six parameters are left out on purpose.** The autopilot rewrites barometer
ground pressure, run time, flight time and boot count by itself on every
flight; across the 24 flights recorded between 14 September and 15 October
2026 those were the only ones that moved on every flight. Kept in, every
refresh of the reference would be a diff, and every check against it a false
alarm. The header names them, so their absence reads as a decision and not as
a loss.

Reading is forgiving: the file is edited by hand on GitHub, so a line that
does not parse, or a name given twice, is collected as a problem for the
caller to show rather than raised. A check that refused to run over one typo
would leave the operator with no check at all.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import flightfile

#: Where the reference files live: beside `assets/`, in the repository, so a
#: `git pull` on a field laptop brings the current one with it.
REFERENCE_DIR = Path(__file__).resolve().parent.parent / "vehicle_parameters"

SUFFIX = ".params"

#: Rewritten by the autopilot on every flight. See the module docstring.
OMIT = (
    "BARO1_GND_PRESS", "BARO2_GND_PRESS", "BARO3_GND_PRESS",
    "STAT_BOOTCNT", "STAT_FLTTIME", "STAT_RUNTIME",
)

#: Header keys, in the order they are written.
META_KEYS = ("vehicle", "firmware", "board", "blueos", "source_flight",
             "source_log", "taken", "count", "omitted")

_META_LINE = re.compile(r"^#\s*([a-z_]+):\s*(.*?)\s*$")
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")


def reference_path(vehicle: str) -> Path:
    """`Nereo` -> `.../vehicle_parameters/nereo.params`."""
    return REFERENCE_DIR / f"{vehicle.strip().lower()}{SUFFIX}"


@dataclass
class Reference:
    """A parameter file, read back."""

    params: dict[str, float] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    #: Lines that could not be read, and names given twice, with line numbers.
    problems: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def omitted(self) -> tuple[str, ...]:
        """The parameters the file says it left out deliberately."""
        return tuple(self.meta.get("omitted", "").split())


# --------------------------------------------------------------------------
#  writing
# --------------------------------------------------------------------------


def _text(value) -> str | None:
    v = flightfile.clean(value)
    if v is None:
        return None
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(float(v))


def render(params: dict, meta: dict | None = None) -> str:
    """The file's text. Pure: `params` is a name -> value dump."""
    meta = dict(meta or {})
    lines: list[str] = []
    for name, value in sorted(params.items()):
        if name in OMIT:
            continue
        text = _text(value)
        if text is None:                    # not finite; ArduPilot has none
            continue
        lines.append(f"{name},{text}")

    meta["count"] = str(len(lines))
    meta["omitted"] = " ".join(n for n in OMIT if n in params)
    title = meta.get("vehicle") or "Vehicle"
    head = [f"# {title} -- reference ArduSub parameters",
            "# Made by `python -m rov_flight_ops.paramfile`; see "
            "vehicle_parameters/README.md.",
            "#"]
    head += [f"# {k}: {meta[k]}" for k in META_KEYS if meta.get(k)]
    return "\n".join(head + ["#"] + lines) + "\n"


def write(path: Path | str, params: dict, meta: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Newline pinned: the file is compared byte-for-byte across machines.
    path.write_text(render(params, meta), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
#  reading
# --------------------------------------------------------------------------


def parse(text: str) -> Reference:
    """A parameter file's text, as a `Reference`. Never raises.

    Takes ArduPilot's `NAME,VALUE` and `NAME VALUE`, and QGroundControl's
    tab-separated `vehicle component NAME VALUE type`, so a file saved from
    either ground station can stand in as a reference.
    """
    ref = Reference()
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _META_LINE.match(line)
            if m and m.group(1) in META_KEYS:
                ref.meta[m.group(1)] = m.group(2)
            continue
        parts = [p for p in re.split(r"[,\s]+", line) if p]
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            parts = parts[2:]                  # QGroundControl's layout
        if len(parts) < 2 or not _NAME.match(parts[0]):
            ref.problems.append(f"line {n}: not a parameter: {raw.strip()!r}")
            continue
        name = parts[0]
        try:
            value = float(parts[1])
        except ValueError:
            ref.problems.append(f"line {n}: {name} has no number: {parts[1]!r}")
            continue
        if name in ref.params:
            ref.problems.append(f"line {n}: {name} is given more than once; "
                                f"the later value is used")
        ref.params[name] = value
    return ref


def read(path: Path | str) -> Reference:
    """A parameter file from disk. A missing file is a problem, not a raise."""
    path = Path(path)
    try:
        ref = parse(path.read_text(encoding="utf-8-sig"))
    except OSError as ex:
        ref = Reference(problems=[f"{path.name} could not be read: {ex}"])
    ref.path = path
    return ref


# --------------------------------------------------------------------------
#  where a reference comes from
# --------------------------------------------------------------------------


def from_flight_record(path: Path | str, vehicle: str = "") -> tuple[dict, dict]:
    """(parameters, header) from a `flight_<id>.json` this program wrote."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    block = record.get("parameters") or {}
    values = block.get("values") or {}
    if not values:
        raise ValueError(
            f"{Path(path).name} has no parameter values: the closing read did "
            f"not happen on that flight, so there is nothing to take")
    v = record.get("vehicle") or {}
    firmware = " ".join(x for x in ("ArduSub" if v.get("ardusub") else "",
                                    v.get("ardusub") or "",
                                    v.get("ardusub_type") or "") if x)
    meta = {
        "vehicle": vehicle,
        "firmware": firmware,
        "board": v.get("board") or "",
        "blueos": v.get("blueos") or "",
        "source_flight": record.get("flight_id") or "",
        "source_log": block.get("read_from") or "",
        "taken": block.get("taken") or "",
    }
    return dict(values), meta


def from_dataflash(path: Path | str, vehicle: str = "") -> tuple[dict, dict]:
    """(parameters, header) from an ArduPilot `.BIN`, each at its last value.

    The log opens with every parameter and records each later change, so the
    last occurrence of a name is the set as it stood when the log closed.
    """
    from pymavlink import mavutil

    path = Path(path)
    params: dict = {}
    firmware = ""
    conn = None
    try:
        # pymavlink prints a line for every unparseable byte, and a log the
        # vehicle was still writing ends mid-record.
        with contextlib.redirect_stdout(io.StringIO()):
            conn = mavutil.mavlink_connection(str(path))
            while True:
                msg = conn.recv_match(type=["PARM", "MSG"])
                if msg is None:
                    break
                if msg.get_type() == "PARM":
                    params[msg.Name] = msg.Value
                elif not firmware and str(msg.Message).startswith("ArduSub V"):
                    # "ArduSub V4.5.7 (b09fafe2)"
                    firmware = str(msg.Message).split(" (")[0].replace(" V", " ")
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    if not params:
        raise ValueError(f"{path.name} has no PARM records")
    return params, {"vehicle": vehicle, "firmware": firmware,
                    "source_log": path.name}


def diff(old: dict, new: dict) -> dict[str, tuple]:
    """name -> (old, new), compared as the float32 the autopilot holds."""
    out = {}
    for name in sorted(set(old) | set(new)):
        a, b = flightfile.clean(old.get(name)), flightfile.clean(new.get(name))
        if a != b:
            out[name] = (a, b)
    return out


# --------------------------------------------------------------------------
#  command line
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m rov_flight_ops.paramfile",
        description="Make a vehicle's reference parameter file from a flight "
                    "record (flight_<id>.json) or an ArduPilot dataflash log "
                    "(.BIN), and say what it changes.")
    ap.add_argument("source", type=Path,
                    help="flight_<id>.json from a flight's logs folder, or a .BIN")
    ap.add_argument("--vehicle", default="Nereo",
                    help="vehicle name; also names the file (default: Nereo)")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="where to write (default: vehicle_parameters/<vehicle>.params)")
    args = ap.parse_args(argv)

    try:
        if args.source.suffix.lower() == ".bin":
            params, meta = from_dataflash(args.source, args.vehicle)
        else:
            params, meta = from_flight_record(args.source, args.vehicle)
    except (OSError, ValueError) as ex:
        print(f"nothing written: {ex}", file=sys.stderr)
        return 1

    out = args.out or reference_path(args.vehicle)
    before = read(out) if out.exists() else None
    write(out, params, meta)
    after = read(out)

    print(f"wrote {out}: {len(after.params)} parameters"
          + (f" from flight {meta['source_flight']}" if meta.get("source_flight") else "")
          + (f" ({meta['source_log']})" if meta.get("source_log") else ""))
    if after.omitted:
        print("left out, rewritten by the autopilot every flight: "
              + ", ".join(after.omitted))
    if before is None:
        print("no earlier reference to compare with")
        return 0
    changes = diff(before.params, after.params)
    if not changes:
        print("identical to the reference it replaces")
        return 0
    print(f"{len(changes)} change(s) from the reference it replaces:")
    for name, (a, b) in changes.items():
        print(f"  {name:<16} {'(absent)' if a is None else a}  ->  "
              f"{'(absent)' if b is None else b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
