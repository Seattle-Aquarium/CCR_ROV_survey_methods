"""
What the EKF was using, and whether the sensors behind it were well.

The transect CSVs are only as good as the navigation that produced them, and a
recording says a great deal about that if asked. This reads the diagnostic
messages ArduSub publishes alongside the telemetry and turns them into something
a survey lead can act on:

  * which aiding sources the EKF actually had -- in particular whether it ever
    achieved an *absolute* horizontal position, or spent the whole dive dead
    reckoning off the DVL;
  * the filter's own innovation variances, which are how it reports that it is
    struggling with a sensor before anything visibly breaks;
  * the compass, which matters more here than it looks: a yaw error rotates the
    entire DVL track about its start point, and no amount of good DVL data
    corrects for it;
  * vibration and accelerometer clipping, which corrupt attitude first;
  * the warnings the autopilot itself raised.

One judgement is built in rather than left to the reader. Without GPS or a
locked USBL, ArduSub reports the AHRS bit unhealthy for the whole dive -- it
means "no absolute position", not "the attitude solution is broken". Reporting
that as a fault would cry wolf on every survey the team flies, so it is
annotated instead.
"""

from __future__ import annotations

import collections
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from .mcap_read import (
    ProgressCB, _brief, _iter_indexed, _iter_sequential, select_channels,
    select_mcaps,
)
from mcap.reader import make_reader

HEALTH_TYPES = (
    "EKF_STATUS_REPORT", "SYS_STATUS", "VIBRATION", "AHRS", "AHRS2",
    "ATTITUDE", "STATUSTEXT", "GPS_RAW_INT",
)

#: An EKF innovation variance above this means the filter is fighting a sensor.
VARIANCE_LIMIT = 1.0

#: ArduPilot's rule of thumb for sustained vibration.
VIBE_LIMIT = 30.0

#: STATUSTEXT at or below this severity is worth surfacing (0 = emergency).
SEVERITY_RANK = {
    "MAV_SEVERITY_EMERGENCY": 0, "MAV_SEVERITY_ALERT": 1,
    "MAV_SEVERITY_CRITICAL": 2, "MAV_SEVERITY_ERROR": 3,
    "MAV_SEVERITY_WARNING": 4, "MAV_SEVERITY_NOTICE": 5,
    "MAV_SEVERITY_INFO": 6, "MAV_SEVERITY_DEBUG": 7,
}
INTERESTING_SEVERITY = 4

_SENSOR_PREFIXES = ("MAV_SYS_STATUS_SENSOR_", "MAV_SYS_STATUS_")

#: What these bits mean on this vehicle, where the names are not self-evident.
_SENSOR_MEANING = {
    "VISION_POSITION": "the DVL's odometry",
    "LASER_POSITION": "the DVL's altitude rangefinder",
    "ABSOLUTE_PRESSURE": "the depth sensor",
    "3D_MAG": "the compass",
}


def _short(sensor: str) -> str:
    for p in _SENSOR_PREFIXES:
        if sensor.startswith(p):
            return sensor[len(p):]
    return sensor


def _describe(sensor: str) -> str:
    name = _short(sensor)
    meaning = _SENSOR_MEANING.get(name)
    return f"{name} ({meaning})" if meaning else name


def _bits(value) -> set[str]:
    """MAVLink bitmasks arrive as 'A | B | C'."""
    return {x.strip() for x in str(value or "").split("|") if x.strip()}


def _enum(value) -> str:
    if isinstance(value, dict):
        return str(value.get("type", ""))
    return str(value or "")


@dataclass
class HealthReport:
    seconds: float = 0.0
    ekf_flags: dict[str, float] = field(default_factory=dict)     # flag -> % of dive
    variances: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    sensors_present: set[str] = field(default_factory=set)
    unhealthy: dict[str, float] = field(default_factory=dict)     # sensor -> % of dive
    compass: dict[str, float] = field(default_factory=dict)
    vibration: dict[str, float] = field(default_factory=dict)
    clipping: tuple[int, int, int] = (0, 0, 0)
    messages: list[tuple[int, str, str, int]] = field(default_factory=list)
    gps_fix_types: dict[str, float] = field(default_factory=dict)
    feeds: object | None = None          # ccr_m2c.feeds.FeedReport
    had_absolute_position: bool = False
    warnings: list[str] = field(default_factory=list)

    # -- interpretation ---------------------------------------------------

    def concerns(self) -> list[str]:
        """The things a survey lead should actually act on."""
        out: list[str] = []

        if not self.had_absolute_position:
            out.append(
                "The EKF never had an absolute horizontal position: no GPS or USBL "
                "fix was accepted at any point. Every horizontal position in the "
                "output is dead reckoning from the DVL, so the transects are right "
                "relative to each other but the whole set can sit off the true "
                "location, and rotates with any compass error."
            )
        for name, (med, p95, mx) in self.variances.items():
            if mx > VARIANCE_LIMIT:
                out.append(
                    f"{name.replace('_', ' ')} peaked at {mx:.2f} (above {VARIANCE_LIMIT:.0f}); "
                    "the filter was rejecting or fighting that sensor."
                )
        for sensor, pct in sorted(self.unhealthy.items(), key=lambda kv: -kv[1]):
            if pct < 1.0:
                continue     # a handful of samples, not a fault
            # The AHRS bit tracks "no absolute position" rather than a broken
            # attitude solution, so on a no-GPS dive it is always set and saying
            # so here would cry wolf on every survey the team flies.
            if _short(sensor) == "AHRS" and not self.had_absolute_position:
                continue
            out.append(f"{_describe(sensor)} reported unhealthy for "
                       f"{pct:.0f}% of the dive.")

        vz = max(self.vibration.values(), default=0.0)
        if vz > VIBE_LIMIT:
            out.append(f"Vibration peaked at {vz:.0f} (over {VIBE_LIMIT:.0f}); "
                       "attitude and velocity estimates degrade first.")
        if any(self.clipping):
            out.append(f"Accelerometer clipping occurred ({self.clipping}); "
                       "the IMU saturated, which corrupts attitude.")

        if self.feeds is not None:
            out.extend(self.feeds.concerns())

        cv = self.variances.get("compass_variance")
        if cv and cv[2] > 0.5:
            out.append(
                f"Compass innovation reached {cv[2]:.2f}. Below {VARIANCE_LIMIT:.0f} the "
                "filter still accepts it, but a yaw error rotates the whole DVL track, "
                "so it is worth a calibration check."
            )
        return out

    def lines(self) -> list[str]:
        L: list[str] = []
        add = L.append
        add(f"Dive length: {self.seconds / 60:.1f} min")

        add("")
        add("EKF aiding sources -- what the filter actually had")
        if self.ekf_flags:
            for flag, pct in sorted(self.ekf_flags.items(), key=lambda kv: -kv[1]):
                add(f"   {pct:5.1f}%  {flag}")
        else:
            add("   no EKF_STATUS_REPORT in this recording")
        add(f"   absolute horizontal position (GPS/USBL accepted): "
            f"{'YES' if self.had_absolute_position else 'NO -- dead reckoning only'}")

        add("")
        add(f"EKF innovation variances (above {VARIANCE_LIMIT:.0f} = struggling)")
        for name, (med, p95, mx) in self.variances.items():
            mark = "  <-- OVER" if mx > VARIANCE_LIMIT else ""
            add(f"   {name:<22} median {med:6.3f}   p95 {p95:6.3f}   max {mx:6.3f}{mark}")

        if self.feeds is not None:
            add("")
            L.extend(self.feeds.lines())
            L.extend(self.feeds.transect_lines())

        add("")
        add("Sensor health")
        if self.unhealthy:
            for sensor, pct in sorted(self.unhealthy.items(), key=lambda kv: -kv[1]):
                note = ""
                if _short(sensor) == "AHRS" and not self.had_absolute_position:
                    note = "   (expected with no GPS/USBL: it means no absolute position)"
                if pct < 1.0:
                    continue
                add(f"   unhealthy {pct:5.1f}% of the dive: {_describe(sensor)}{note}")
        else:
            add("   every enabled sensor stayed healthy")
        if self.sensors_present:
            add("   present: " + ", ".join(sorted(_short(s) for s in self.sensors_present)))

        if self.compass:
            add("")
            add("Compass and attitude")
            for k, v in self.compass.items():
                add(f"   {k:<34} {v:.3f}")

        if self.vibration:
            add("")
            add("Vibration")
            add("   " + "   ".join(f"{k} max {v:.2f}" for k, v in self.vibration.items())
                + f"   clipping {self.clipping}")

        if self.gps_fix_types:
            add("")
            add("GPS fix type")
            for k, pct in sorted(self.gps_fix_types.items(), key=lambda kv: -kv[1]):
                add(f"   {pct:5.1f}%  {k}")

        if self.messages:
            add("")
            add("Autopilot warnings and errors")
            for _rank, sev, text, count in self.messages:
                add(f"   {count:>4}x  [{sev:<9}] {text}")

        concerns = self.concerns()
        add("")
        if concerns:
            add("What to look at")
            for c in concerns:
                add(f"   - {c}")
        else:
            add("Nothing of concern found.")
        for w in self.warnings:
            add(f"   ! {w}")
        return L


def read_health(paths: Sequence[Path | str], *,
                transects: Sequence = (),
                progress: ProgressCB | None = None) -> HealthReport:
    """Collect the diagnostic messages from one or more recordings."""
    ordered, warnings = select_mcaps(paths)
    if not ordered:
        raise ValueError("none of the .mcap files could be read:\n  " + "\n  ".join(warnings))

    rep = HealthReport(warnings=list(warnings))
    flags = collections.Counter()
    var: dict[str, list[float]] = collections.defaultdict(list)
    unhealthy = collections.Counter()
    n_ekf = n_sys = 0
    vib = {"X": [], "Y": [], "Z": []}
    clip = (0, 0, 0)
    err_yaw: list[float] = []
    err_rp: list[float] = []
    att_yaw: list[tuple[float, float]] = []
    dcm_yaw: list[tuple[float, float]] = []
    texts = collections.Counter()
    fixes = collections.Counter()
    t_first = t_last = None

    for i, path in enumerate(ordered):
        if progress:
            progress(i / len(ordered), f"reading {path.name}")
        try:
            with open(path, "rb") as fh:
                reader = make_reader(fh)
                chosen = {k: v for k, v in select_channels(reader).items()}
                # select_channels only returns types the extractor wants; the
                # diagnostic ones are found the same way, from the topic names.
                summary = reader.get_summary()
                topics = {}
                if summary:
                    for ch in summary.channels.values():
                        p = ch.topic.split("/")
                        if len(p) == 4 and p[3] in HEALTH_TYPES and p[1] == "1" and p[2] == "1":
                            topics[p[3]] = ch.topic
                if not topics:
                    continue
                stream = _iter_indexed(reader, topics)
                for mt, m, t in stream:
                    t_first = t if t_first is None else min(t_first, t)
                    t_last = t if t_last is None else max(t_last, t)
                    if mt == "EKF_STATUS_REPORT":
                        n_ekf += 1
                        for fl in _bits(m.get("flags")):
                            flags[fl] += 1
                        for k in ("compass_variance", "velocity_variance",
                                  "pos_horiz_variance", "pos_vert_variance",
                                  "terrain_alt_variance"):
                            v = m.get(k)
                            if isinstance(v, (int, float)) and math.isfinite(v):
                                var[k].append(float(v))
                    elif mt == "SYS_STATUS":
                        n_sys += 1
                        rep.sensors_present |= _bits(m.get("onboard_control_sensors_present"))
                        bad = (_bits(m.get("onboard_control_sensors_enabled"))
                               - _bits(m.get("onboard_control_sensors_health")))
                        for s in bad:
                            unhealthy[s] += 1
                    elif mt == "VIBRATION":
                        for ax, key in (("X", "vibration_x"), ("Y", "vibration_y"),
                                        ("Z", "vibration_z")):
                            v = m.get(key)
                            if isinstance(v, (int, float)):
                                vib[ax].append(float(v))
                        clip = (max(clip[0], int(m.get("clipping_0", 0) or 0)),
                                max(clip[1], int(m.get("clipping_1", 0) or 0)),
                                max(clip[2], int(m.get("clipping_2", 0) or 0)))
                    elif mt == "AHRS":
                        if isinstance(m.get("error_yaw"), (int, float)):
                            err_yaw.append(float(m["error_yaw"]))
                        if isinstance(m.get("error_rp"), (int, float)):
                            err_rp.append(float(m["error_rp"]))
                    elif mt == "AHRS2":
                        if isinstance(m.get("yaw"), (int, float)):
                            dcm_yaw.append((t, float(m["yaw"])))
                    elif mt == "ATTITUDE":
                        if isinstance(m.get("yaw"), (int, float)):
                            att_yaw.append((t, float(m["yaw"])))
                    elif mt == "STATUSTEXT":
                        sev = _enum(m.get("severity"))
                        rank = SEVERITY_RANK.get(sev, 9)
                        if rank <= INTERESTING_SEVERITY:
                            texts[(rank, sev.replace("MAV_SEVERITY_", ""),
                                   str(m.get("text", ""))[:70])] += 1
                    elif mt == "GPS_RAW_INT":
                        fixes[_enum(m.get("fix_type"))] += 1
        except Exception as ex:
            rep.warnings.append(f"{path.name}: {_brief(ex)}")

    if t_first is not None and t_last is not None:
        rep.seconds = t_last - t_first
    if n_ekf:
        rep.ekf_flags = {k: 100.0 * v / n_ekf for k, v in flags.items()}
        rep.had_absolute_position = flags.get("EKF_POS_HORIZ_ABS", 0) > 0
    for k, vals in var.items():
        a = np.asarray(vals, dtype=float)
        rep.variances[k] = (float(np.median(a)), float(np.percentile(a, 95)), float(a.max()))
    if n_sys:
        rep.unhealthy = {k: 100.0 * v / n_sys for k, v in unhealthy.items()}
    rep.vibration = {ax: float(np.max(v)) for ax, v in vib.items() if v}
    rep.clipping = clip
    if fixes:
        total = sum(fixes.values())
        rep.gps_fix_types = {k: 100.0 * v / total for k, v in fixes.items()}

    if err_yaw:
        a = np.asarray(err_yaw, float)
        rep.compass["AHRS yaw error (median)"] = float(np.median(a))
        rep.compass["AHRS yaw error (max)"] = float(a.max())
    if err_rp:
        rep.compass["AHRS roll/pitch error (max)"] = float(np.max(err_rp))
    if att_yaw and dcm_yaw:
        A = np.asarray(att_yaw, float)
        D = np.asarray(dcm_yaw, float)
        d = np.interp(A[:, 0], D[:, 0], np.unwrap(D[:, 1]))
        diff = np.degrees((np.unwrap(A[:, 1]) - d + np.pi) % (2 * np.pi) - np.pi)
        rep.compass["EKF vs DCM yaw, deg (median)"] = float(np.median(np.abs(diff)))
        rep.compass["EKF vs DCM yaw, deg (p95)"] = float(np.percentile(np.abs(diff), 95))

    try:
        from .feeds import read_feeds
        rep.feeds = read_feeds(ordered, transects=transects)
        rep.warnings.extend(rep.feeds.warnings)
    except Exception as ex:
        rep.warnings.append(f"could not work out column sources: {_brief(ex)}")

    rep.messages = [(r, s, t, c) for (r, s, t), c in
                    sorted(texts.items(), key=lambda kv: (kv[0][0], -kv[1]))][:12]
    if progress:
        progress(1.0, "health report complete")
    return rep
