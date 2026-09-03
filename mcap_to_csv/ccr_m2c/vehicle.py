"""
What the vehicle was, and how it was configured, at the moment it flew.

A survey CSV records what the ROV measured. This records the machine that
measured it -- the autopilot firmware, the computer it ran on, and the
parameters in force. Six months later, when two dives disagree, the first
question is usually whether anything about the vehicle changed between them,
and the recording can answer that without anyone having written it down.

**Prefer the .BIN.** The autopilot's own dataflash log writes every parameter at
boot and an explicit version record, unconditionally. On 2026-09-02 the .BIN
held all 1,038 parameters and ``ArduSub V4.5.0 (03c12698)``; the .mcap from the
same dive held 6 parameters and no version at all. So a .BIN is read in
preference and the mcap only fills what is left -- which still matters, because
the mcap is the file that always exists and carries the BlueOS side.

Three caveats are built into the reporting, because each would otherwise make a
partial answer look like a complete one:

* **In an mcap, parameters are only present if something asked for them.**
  ArduPilot sends PARAM_VALUE in response to a download, not continuously, so a
  recording may hold every parameter, a handful, or none. Each message carries
  the total the vehicle has, so the report says how many it actually saw.

* **AUTOPILOT_VERSION is likewise a reply, not a broadcast.** It appears only if
  a ground station requested capabilities during that recording. Absent means
  nobody asked, not that the firmware is unknown -- another file from the same
  dive may well have it, which is why several are read together.

* **BlueOS does not stamp its own version anywhere in either file.** The
  operating system, kernel and board come through from the on-board service
  logs, but the BlueOS release does not, so it is reported as unknown rather
  than guessed at from the Debian version underneath it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from mcap.reader import make_reader

from .mcap_read import ProgressCB, _brief, select_mcaps

#: Parameters worth pulling out by default. Everything is captured; these are
#: the ones a survey question usually turns on.
NOTABLE_PREFIXES = ("RNGFND", "SURFTRAK", "EK3_SRC", "VISO", "GPS_", "COMPASS_",
                    "AHRS_", "BARO", "PSC_", "WPNAV_", "FRAME_", "SERVO", "BATT")


@dataclass
class Vehicle:
    ardusub: str | None = None
    ardusub_build: str | None = None
    board: str | None = None
    os_name: str | None = None
    kernel: str | None = None
    hostname: str | None = None
    params: dict[str, float] = field(default_factory=dict)
    param_total: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def params_complete(self) -> bool:
        return bool(self.param_total) and len(self.params) >= self.param_total

    def notable(self) -> dict[str, float]:
        return {k: v for k, v in sorted(self.params.items())
                if k.startswith(NOTABLE_PREFIXES)}

    def lines(self, grep: str = "", full: bool = False) -> list[str]:
        L = ["The vehicle, as recorded", ""]

        L.append("Firmware and hardware")
        L.append(f"   ArduSub          {self.ardusub or 'not recorded'}"
                 + (f"  (build {self.ardusub_build})"
                    if self.ardusub and self.ardusub_build else ""))
        if not self.ardusub:
            L.append("                    AUTOPILOT_VERSION is a reply to a ground "
                     "station request, so it is absent unless something asked")
        L.append(f"   BlueOS           not recorded -- BlueOS does not stamp its "
                 f"version into the log")
        L.append(f"   board            {self.board or 'not recorded'}")
        L.append(f"   operating system {self.os_name or 'not recorded'}"
                 + (f", kernel {self.kernel}" if self.kernel else ""))
        if self.hostname:
            L.append(f"   hostname         {self.hostname}")

        L.append("")
        if not self.params:
            L.append("Parameters")
            L.append("   none in this recording. ArduPilot sends them only when a "
                     "ground station")
            L.append("   downloads them, so connect one and refresh the parameter "
                     "list while recording.")
            return L

        seen = len(self.params)
        total = self.param_total
        if total and seen < total:
            L.append(f"Parameters  ({seen} of the vehicle's {total} were captured "
                     f"-- a partial download)")
        elif total:
            L.append(f"Parameters  (all {total})")
        else:
            L.append(f"Parameters  ({seen} captured)")

        if grep:
            wanted = {k: v for k, v in sorted(self.params.items())
                      if grep.upper() in k.upper()}
            L.append(f"   matching {grep!r}:")
        elif full:
            wanted = dict(sorted(self.params.items()))
        else:
            wanted = self.notable()
            L.append("   the ones a survey usually turns on "
                     "(use --all for every parameter):")

        if not wanted:
            L.append("      nothing matched")
        for k, v in wanted.items():
            L.append(f"      {k:<24} {_fmt(v)}")

        for w in self.warnings:
            L.append(f"   ! {w}")
        return L


def _fmt(v: float) -> str:
    """Whole numbers as integers: a parameter of 10 is a type code, not 10.0."""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _text(value) -> str:
    """os_custom_version and friends arrive as a byte array."""
    if isinstance(value, list):
        return "".join(chr(c) for c in value if 32 <= int(c) < 127).strip()
    return str(value or "").strip()


def read_vehicle(paths: Sequence[Path | str], *,
                 progress: ProgressCB | None = None) -> Vehicle:
    """Firmware, hardware and parameters, gathered across a dive's recordings.

    Accepts .BIN and .mcap together. The .BIN is authoritative and is read
    first; the mcap then fills anything still missing.
    """
    paths = [Path(p) for p in paths]
    bins = [p for p in paths if p.suffix.lower() == ".bin"]
    mcaps = [p for p in paths if p.suffix.lower() == ".mcap"]

    v = Vehicle()
    if bins:
        _read_bins(v, bins, progress=progress)
    if not mcaps:
        if not v.params and not v.ardusub:
            raise ValueError("nothing readable in the files given")
        return v

    ordered, warnings = select_mcaps(mcaps)
    v.warnings.extend(warnings)
    if not ordered:
        if v.params or v.ardusub:
            return v                      # the .BIN carried it
        raise ValueError("none of the .mcap files could be read")

    for i, path in enumerate(ordered):
        if progress:
            progress(i / len(ordered), f"reading {path.name}")
        try:
            with open(path, "rb") as fh:
                summary = make_reader(fh).get_summary()
                if not summary:
                    continue
                topics = [
                    c.topic for c in summary.channels.values()
                    if c.topic.endswith(("/PARAM_VALUE", "/AUTOPILOT_VERSION"))
                    or c.topic.startswith("services/system_information/")
                ]
            if not topics:
                continue
            with open(path, "rb") as fh:
                for _s, ch, msg in make_reader(fh).iter_messages(topics=topics):
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        continue
                    _absorb(v, ch.topic, payload)
        except Exception as ex:
            v.warnings.append(f"{path.name}: {_brief(ex)}")

    if progress:
        progress(1.0, "vehicle configuration read")
    return v


def _read_bins(v: Vehicle, paths: Sequence[Path], *,
               progress: ProgressCB | None = None) -> None:
    """Parameters and firmware from the autopilot's own dataflash logs.

    pymavlink is imported here rather than at module scope: it is a heavy
    dependency for one diagnostic, and everything else in this package works
    without it. A missing one degrades to the mcap rather than failing.
    """
    try:
        from pymavlink import mavutil
    except ImportError:
        v.warnings.append(
            ".BIN files were given but pymavlink is not installed, so they were "
            "skipped. Install it with: python -m pip install pymavlink")
        return

    for i, path in enumerate(paths):
        if progress:
            progress(i / max(1, len(paths)), f"reading {path.name}")
        try:
            log = mavutil.mavlink_connection(str(path))
            while True:
                rec = log.recv_match(type=["PARM", "VER", "MSG"])
                if rec is None:
                    break
                kind = rec.get_type()
                if kind == "PARM":
                    v.params[rec.Name] = rec.Value
                elif kind == "VER":
                    v.ardusub = f"{rec.Maj}.{rec.Min}.{rec.Pat}"
                    fws = str(getattr(rec, "FWS", "") or "")
                    if "(" in fws and ")" in fws:
                        v.ardusub_build = fws.split("(", 1)[1].split(")", 1)[0]
                elif kind == "MSG" and not v.ardusub:
                    # older firmware writes no VER record, only the boot banner
                    text = str(rec.Message).strip()
                    if text.startswith(("ArduSub", "ArduPilot")):
                        parts = text.split()
                        if len(parts) > 1:
                            v.ardusub = parts[1].lstrip("Vv")
                        if "(" in text:
                            v.ardusub_build = text.split("(", 1)[1].split(")", 1)[0]
        except Exception as ex:
            v.warnings.append(f"{path.name}: {_brief(ex)}")

    # A dataflash log carries the whole parameter set by definition, so there is
    # no partial-download caveat to report for it.
    if v.params and v.param_total is None:
        v.param_total = len(v.params)


def _absorb(v: Vehicle, topic: str, payload: dict) -> None:
    if topic.endswith("/PARAM_VALUE"):
        m = payload.get("message", {})
        name = str(m.get("param_id", "")).rstrip("\x00")
        if name:
            v.params[name] = m.get("param_value")
        total = m.get("param_count")
        if isinstance(total, (int, float)) and total > 0:
            v.param_total = int(total)

    elif topic.endswith("/AUTOPILOT_VERSION"):
        m = payload.get("message", {})
        raw = m.get("flight_sw_version")
        if isinstance(raw, int) and raw:
            # packed as major<<24 | minor<<16 | patch<<8 | release type
            v.ardusub = f"{(raw >> 24) & 255}.{(raw >> 16) & 255}.{(raw >> 8) & 255}"
        build = _text(m.get("flight_custom_version"))
        if build:
            v.ardusub_build = build

    elif topic.endswith("/info"):
        v.os_name = f"{payload.get('system_name', '')} {payload.get('os_version', '')}".strip() or None
        v.kernel = payload.get("kernel_version") or None
        v.hostname = payload.get("host_name") or None

    elif topic.endswith("/platform"):
        ok = payload.get("Ok") or {}
        for _family, spec in ok.items():
            if isinstance(spec, dict) and spec.get("model"):
                v.board = spec["model"]
                if spec.get("soc"):
                    v.board += f" ({spec['soc']})"
                break
