"""
Reading what the light controller sends.

Two dialects arrive on the controller's serial ports, and both are read:

**ccr.lights/1** -- one JSON object per line, from the telemetry firmware in
`lighting/code/src` (documented in `lighting/code/TELEMETRY.md`). On the
second USB port every line is one of these; on the console they carry an
"@L " prefix to set them apart from anything else printed there.

**Aaron Marburg's debug build** -- the text the controller printed while the
telemetry was being worked out (CCR_development#50, 13 October 2026)::

    PWM: 1109 us | Level: 0%
    1609790283245552000   1: Stat:     93.7hr  112  87.0C  0.0C  24.0C  20.6C  0%  0x0000
    1609790283268117000   1: Level:    0
    1609790283293502000   1: Temp:     0.0
    1609790283315646000   1: Humidity: 0.0

It is translated into the same records, marked ``legacy``, so the extension
is useful against the controller as it is today -- reading the console,
read-only -- before the telemetry firmware is installed.

Anything else is kept verbatim in the recording and otherwise ignored.

**STAT? is parsed by its own header.** DeepSea's specification lists five
columns (RunTime, PrCycle, MaxTemp, Humid, FLAGS); Lutris's LSL-2075s answer
with eight -- run time, power-on count, maximum temperature, three
temperatures, humidity and flags, per Aaron. When the header row is present
its names are used; when it is not (the debug build drops it) the position
is used, eight columns or five.
"""

from __future__ import annotations

import json
import re

PROTOCOL = "ccr.lights/1"
CONSOLE_PREFIX = "@L "

_PWM = re.compile(r"^PWM:\s*(?:(\d+)\s*us|NO SIGNAL)\s*\|\s*Level:\s*(-?\d+)\s*%")
_LEGACY = re.compile(r"^\s*(?:\d+\s+)?(\d+):\s*(Stat|Level|Temp|Humidity|Info|Standby):\s*(.*)$",
                     re.IGNORECASE)
_LEGACY_CMD = {"stat": "stat", "level": "lout", "temp": "temp", "humidity": "rhum",
               "info": "info", "standby": "stby"}


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
        return rec if isinstance(rec, dict) and "k" in rec else None
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
    if len(rows) >= 2 and not number(rows[-2].split()[0]) is not None:
        header = rows[-2].split()
    if header is None and number(values[0]) is None:
        return {}                                   # a header with no values
    out: dict = {"columns": [], "by": "header" if header else "position"}
    if header and len(header) == len(values):
        names = [_STAT_NAMES.get(_key(h), _key(h) or f"col{i}") for i, h in enumerate(header)]
        out["columns"] = [[h, v] for h, v in zip(header, values, strict=True)]
    else:
        names = list(_STAT_8 if len(values) >= 8 else _STAT_5 if len(values) == 5
                     else [f"col{i}" for i in range(len(values))])
        out["columns"] = [[n, v] for n, v in zip(names, values, strict=False)]
        out["by"] = "position"
    # strict=False: the five-column names may be fewer than a longer row's values.
    for name, text in zip(names, values, strict=False):
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
