"""
Reading what the light controller sends.

Two dialects arrive on the controller's serial ports, and both are read:

**ccr.lights/1** -- one JSON object per line, from the telemetry firmware in
`lighting/code/src`. On the second USB port every line is one of these; on
the console they carry an "@L " prefix to set them apart from anything else
printed there. The firmware on Lutris (`lutris-lights 2.0.1-telemetry`,
CCR_ROV_survey_methods#54, November 2026) names a line's kind in ``"type"``
and sends each light's answer as a ``"query"`` line with the question in
``"cmd"``::

    {"id": 2, "cmd": "lout", "ok": 1, "rtt": 31, "raw": "87", "type": "query", "n": 812, "ms": 40211}

The format's first draft (`lighting/code/TELEMETRY.md`) used ``"k"``,
``"q"`` and ``"c"`` for the same three things. Both are read, and every
record is turned into the draft's names here -- ``k``, ``q``, ``c`` -- so
the rest of the extension, and recordings made in either dialect, read the
same. (``"cmd"`` keeps its own meaning, the commanded level, on ``ctl``
lines; only a query's ``cmd`` is the question.)

**Plain console text** -- what the controller prints for people. The Lutris
firmware prints a status line every two seconds::

    PWM: 1523 us | Scaled PWM: 0.54 | Lout: 87
    PWM: NO SIGNAL | Lout: 0

and Aaron Marburg's debug build printed (CCR_development#50, 13 October 2026)::

    PWM: 1109 us | Level: 0%
    1609790283245552000   1: Stat:     93.7hr  112  87.0C  0.0C  24.0C  20.6C  0%  0x0000
    1609790283268117000   1: Level:    0

These are translated into the same records, marked ``legacy``, so the
extension still says something useful when it can only read the console of a
controller without telemetry.

Anything else is kept verbatim in the recording and otherwise ignored.

**STAT? is parsed by its header when every name in it is known.** DeepSea's
specification lists five tab-separated columns (RunTime, PrCycle, MaxTemp,
Humid, FLAGS); Lutris's LSL-2075s answer with eight -- run time, power-on
count, maximum temperature, three temperatures, humidity and flags, per
Aaron, which the units in each value confirm (hr, a count, four in C, %, a
hex code). A header whose names are all recognised names the columns; any
other header is kept for display, and the columns are read by position,
eight or five.
"""

from __future__ import annotations

import json
import re

PROTOCOL = "ccr.lights/1"
CONSOLE_PREFIX = "@L "

#: The status line, in both the Lutris firmware's form ("| Scaled PWM: 0.54 |
#: Lout: 87") and the debug build's ("| Level: 0%").
_PWM = re.compile(r"^PWM:\s*(?:(\d+)\s*us|NO SIGNAL)\s*\|"
                  r"(?:\s*Scaled PWM:\s*[-+0-9.eE]+\s*\|)?"
                  r"\s*(?:Level|Lout):\s*(-?\d+)\s*%?")
_LEGACY = re.compile(r"^\s*(?:\d+\s+)?(\d+):\s*(Stat|Level|Temp|Humidity|Info|Standby):\s*(.*)$",
                     re.IGNORECASE)
_LEGACY_CMD = {"stat": "stat", "level": "lout", "temp": "temp", "humidity": "rhum",
               "info": "info", "standby": "stby"}

#: The firmware's names for a kind of line -> the names used in here.
_KINDS = {"query": "q"}


def _normalise(rec: dict) -> dict | None:
    """A ccr.lights/1 record in either dialect, with the draft's names (k, q, c)."""
    kind = rec.get("type", rec.get("k"))
    if not isinstance(kind, str) or not kind:
        return None
    kind = _KINDS.get(kind, kind)
    out = dict(rec)
    out["k"] = kind
    if kind == "q" and "c" not in out and "cmd" in out:
        out["c"] = out["cmd"]
    return out


def parse_line(text: str) -> dict | None:
    """One line from the controller, as a record, or None if it is not one."""
    if text is None:
        return None
    line = text.strip("\r\n\x00")
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(CONSOLE_PREFIX.strip()):
        stripped = stripped[len(CONSOLE_PREFIX.strip()):].strip()
    if stripped.startswith("{"):
        try:
            rec = json.loads(stripped)
        except ValueError:
            return None
        return _normalise(rec) if isinstance(rec, dict) else None
    m = _PWM.match(stripped)
    if m:
        pwm = int(m.group(1)) if m.group(1) else 0
        return {"k": "ctl", "pwm": pwm, "sig": 1 if m.group(1) else 0,
                "cmd": int(m.group(2)), "legacy": 1}
    m = _LEGACY.match(line)
    if m:
        cmd = _LEGACY_CMD[m.group(2).lower()]
        body = m.group(3).strip()
        if cmd == "stat":
            body = "\t".join(body.split())
        return {"k": "q", "id": int(m.group(1)), "c": cmd, "ok": 1, "raw": body,
                "legacy": 1}
    return None


# --------------------------------------------------------------------------
#  answers
# --------------------------------------------------------------------------

_NUM = re.compile(r"^[-+]?\d+(?:\.\d+)?")


def number(token) -> float | None:
    """'93.7hr' -> 93.7, '87.0C' -> 87.0, '44%' -> 44.0, '0x0001' -> 1."""
    if token is None:
        return None
    t = str(token).strip()
    if t.lower().startswith("0x"):
        try:
            return float(int(t, 16))
        except ValueError:
            return None
    m = _NUM.match(t)
    return float(m.group(0)) if m else None


def first_number(raw: str | None) -> float | None:
    """The first number in a single-line answer ('100', ' 27 ', '0.0')."""
    if not raw:
        return None
    for tok in str(raw).replace(",", " ").split():
        n = number(tok)
        if n is not None:
            return n
    return None


#: Header names (lower case, punctuation stripped) -> our field.
_STAT_NAMES = {
    "runtime": "runtime_h", "prcycle": "power_cycles", "maxtemp": "max_temp_c",
    "humid": "humidity_pct", "humidity": "humidity_pct", "flags": "flags",
    "temp1": "temp1_c", "temp2": "temp2_c", "temp3": "temp3_c", "temp": "temp1_c",
}
_STAT_8 = ("runtime_h", "power_cycles", "max_temp_c", "temp1_c", "temp2_c",
           "temp3_c", "humidity_pct", "flags")
_STAT_5 = ("runtime_h", "power_cycles", "max_temp_c", "humidity_pct", "flags")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def parse_stat(raw: str | None) -> dict:
    """STAT?'s answer as named values, plus the columns verbatim.

    Returns {} for nothing parseable. Keys: runtime_h, power_cycles,
    max_temp_c, temp1_c..temp3_c, humidity_pct, flags (int), flags_hex,
    columns ([name, text] pairs), by ("header" or "position").
    """
    if not raw:
        return {}
    rows = [r for r in str(raw).replace("\r", "").split("\n") if r.strip()]
    if not rows:
        return {}
    header = None
    values = rows[-1].split()
    if len(rows) >= 2 and number(rows[-2].split()[0]) is None:
        header = rows[-2].split()
    if number(values[0]) is None:
        return {}                                   # a header with no values
    if header is not None and len(header) != len(values):
        header = None                               # not this row's header
    names = None
    by = "position"
    if header:
        mapped = [_STAT_NAMES.get(_key(h)) for h in header]
        if all(mapped) and len(set(mapped)) == len(mapped):
            names, by = mapped, "header"
    if names is None:
        if len(values) >= 8:
            names = list(_STAT_8) + [f"col{i}" for i in range(8, len(values))]
        elif len(values) == 5:
            names = list(_STAT_5)
        else:
            names = [f"col{i}" for i in range(len(values))]
    labels = header or names
    out: dict = {"columns": [[h, v] for h, v in zip(labels, values, strict=True)], "by": by}
    for name, text in zip(names, values, strict=True):
        if name == "flags":
            n = number(text)
            out["flags"] = int(n) if n is not None else None
            out["flags_hex"] = text if text.lower().startswith("0x") else (
                f"0x{int(n):04X}" if n is not None else text)
        else:
            out[name] = number(text)
    if isinstance(out.get("power_cycles"), float):
        out["power_cycles"] = int(out["power_cycles"])
    temps = [out.get(k) for k in ("temp1_c", "temp2_c", "temp3_c") if k in out]
    if temps:
        out["temps_c"] = temps
    return out


def parse_info(raw: str | None) -> dict:
    """INFO?: '010,LSL-1000,12-049-000101,0A,1.1.1686,DW00,FLD,SER'."""
    if not raw:
        return {}
    parts = [p.strip() for p in str(raw).strip().split(",")]
    out = {"raw": str(raw).strip()}
    if len(parts) >= 5:
        out.update(address=parts[0], model=parts[1], serial=parts[2],
                   pcb=parts[3], firmware=parts[4], options=parts[5:])
    return out
