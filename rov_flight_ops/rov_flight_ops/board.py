"""
Go or no-go: every part of the system that has to work, at one glance.

The Monitoring tab opens on this. Each row is one piece of hardware or one
link, judged from what the program already reads -- mavlink2rest, the DVL
capture, the UGPS topside, the flight recorder -- into one of four states:

=======  =====  ============================================================
GO       ●      working as it should
CHECK    ▲      working, but not as it should; look before diving
NO-GO    ✖      not working; do not dive on it
—        ○      not heard from yet (nothing connected, or not started)
=======  =====  ============================================================

and the verdict at the top is the worst of them. Nothing here touches Tk or
the network, so every judgement is tested on its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import bands
from .nav import trust as TR
from .nav.model import NavSnapshot, Quality

GO, CHECK, NOGO = 0, 1, 2
WORDS = {GO: "GO", CHECK: "CHECK", NOGO: "NO-GO", None: "—"}
GLYPHS = {GO: "●", CHECK: "▲", NOGO: "✖", None: "○"}
#: (light, dark), matching the Live monitoring levels.
COLORS = {GO: bands.LEVEL_COLORS[bands.NORMAL],
          CHECK: bands.LEVEL_COLORS[bands.UNUSUAL],
          NOGO: bands.LEVEL_COLORS[bands.BAD]}

#: A heartbeat older than this means the autopilot has gone quiet.
HEARTBEAT_STALE_S = 6.0
#: The DVL is quiet when its last report is older than this.
DVL_QUIET_S = 3.0

#: Laptop readings judged on the Tether row rather than the Laptop row.
TETHER_COLUMNS = ("tether_link_mbps", "rov_ping_latency_ms",
                  "rov_packet_loss_pct", "tether_remote_seen")


@dataclass
class Row:
    key: str
    name: str
    level: int | None
    detail: str = ""
    #: Little marks after the detail, e.g. one per UGPS receiver.
    chips: list[tuple[str, int | None]] = field(default_factory=list)


@dataclass
class Board:
    rows: list[Row]
    verdict: int | None
    headline: str
    volts: float | None = None
    amps: float | None = None
    watts: float | None = None


@dataclass
class Inputs:
    """Everything the board reads, gathered by the window each second."""

    snapshot: NavSnapshot | None = None
    #: The flight recorder's status and its newest laptop-monitor row.
    recorder_status: object | None = None
    recorder_watching: bool = False
    latest_row: dict | None = None
    history_rows: list | None = None
    #: The DVL capture's live snapshot.
    dvl: object | None = None
    #: The UGPS capture's snapshot dict.
    ugps: dict | None = None
    now: float = field(default_factory=time.monotonic)


def _age(reading, now: float) -> float | None:
    return reading.age(now) if reading is not None else None


def judge(inp: Inputs) -> Board:
    s = inp.snapshot
    now = inp.now
    rows = [
        _vehicle_link(inp),
        _navigator(s, now),
        _depth(s, now),
        _estimator(s),
        _dvl(inp.dvl, s, now),
        _gnss_compass(s),
        _g2(inp.ugps),
        _receivers(inp.ugps, now),
        _locator(inp.ugps, s),
        _leak(s),
        _tether(inp),
        _laptop(inp),
        _recording(inp),
    ]
    if rows[0].level is None:
        verdict, headline = None, "NO VEHICLE — nothing connected"
    else:
        verdict = max(r.level for r in rows if r.level is not None)
        worst = [r.name for r in rows if r.level == verdict and verdict != GO]
        headline = WORDS[verdict] + (" — " + ", ".join(worst[:3]) if worst
                                     else " — everything is working")
    board = Board(rows=rows, verdict=verdict, headline=headline)
    if s is not None:
        board.volts = s.voltage.number()
        board.amps = s.current.number()
        # Held: V × I is stale between BATTERY_STATUS messages, not wrong.
        board.watts = s.watts.held()
    return board


# --------------------------------------------------------------------------
#  the rows
# --------------------------------------------------------------------------


def _vehicle_link(inp: Inputs) -> Row:
    st, s = inp.recorder_status, inp.snapshot
    reachable = bool(st is not None and getattr(st, "reachable", False))
    link = s.link if s is not None else None
    live = link is not None and link.mode == "live"
    if reachable or (live and link.connected):
        ms = getattr(st, "link_ms", None) if st is not None else None
        return Row("link", "Vehicle link", GO,
                   "BlueOS answering" + (f" · {ms:.0f} ms" if ms else ""))
    if live and link.last_ok_age is not None:
        return Row("link", "Vehicle link", NOGO,
                   link.problem[:60] if link.problem else "no answer")
    if st is not None and getattr(st, "last_seen", 0):
        return Row("link", "Vehicle link", NOGO,
                   f"no answer for {time.time() - st.last_seen:.0f} s")
    return Row("link", "Vehicle link", None, "not connected")


def _navigator(s: NavSnapshot | None, now: float) -> Row:
    name = "Navigator (autopilot)"
    if s is None or not s.armed.quality.has_value:
        return Row("navigator", name, None, "no heartbeat yet")
    age = _age(s.armed, now)
    if age is not None and age > HEARTBEAT_STALE_S:
        return Row("navigator", name, NOGO, f"no heartbeat for {age:.0f} s")
    armed = "armed" if s.armed.value else "disarmed"
    mode = s.mode.value if s.mode.quality.has_value else "mode unknown"
    return Row("navigator", name, GO, f"{armed} · {mode}")


def _depth(s: NavSnapshot | None, now: float) -> Row:
    name = "Depth sensor"
    if s is None or s.depth.quality is Quality.NEVER_RECEIVED:
        return Row("depth", name, None, "no reading yet")
    h = s.messages.get("SCALED_PRESSURE2")
    if h is not None and h.seen and h.quality(10.0, now) is Quality.STALE:
        return Row("depth", name, CHECK, "pressure sensor messages stopped")
    if s.depth.quality is not Quality.OK:
        return Row("depth", name, NOGO, s.depth.note or "no current depth")
    return Row("depth", name, GO, f"{-s.depth.value:.1f} m")


def _estimator(s: NavSnapshot | None) -> Row:
    name = "Estimator (EKF)"
    if s is None:
        return Row("ekf", name, None, "no status yet")
    mode, _note = TR.aiding_mode(s)
    if mode == TR.ABSOLUTE:
        return Row("ekf", name, GO, "absolute position")
    if mode == TR.RELATIVE:
        return Row("ekf", name, CHECK, "relative — dead reckoning, will drift")
    if mode == TR.NO_AIDING:
        return Row("ekf", name, NOGO, "no position aiding")
    return Row("ekf", name, None, "no status yet")


def _dvl(dvl, s: NavSnapshot | None, now: float) -> Row:
    name = "DVL"
    if dvl is not None and getattr(dvl, "tcp_connected", False):
        v = dvl.velocity or {}
        age = dvl.last_rx_age_s
        rate = (dvl.rates or {}).get("velocity", 0.0)
        if age is not None and age > DVL_QUIET_S:
            return Row("dvl", name, CHECK, f"connected, quiet for {age:.0f} s")
        status = dvl.status or {}
        if status.get("ext_reachable") is False:
            return Row("dvl", name, CHECK, "streaming, but the BlueOS extension "
                                           "is not forwarding it")
        if v.get("velocity_valid") in (True, 1):
            alt = v.get("altitude")
            return Row("dvl", name, GO, "bottom lock"
                       + (f" · {alt:.2f} m" if isinstance(alt, (int, float)) else "")
                       + (f" · {rate:.1f} Hz" if rate else ""))
        if v:
            return Row("dvl", name, CHECK, "no bottom lock")
        return Row("dvl", name, CHECK, "connected, no reports yet")
    lock = s.dvl.get("bottom_lock") if s is not None else None
    if lock is not None and lock.quality is Quality.OK:
        return Row("dvl", name, GO, "bottom lock (autopilot range)")
    if lock is not None and lock.quality is Quality.INVALID:
        return Row("dvl", name, CHECK, "no bottom lock (autopilot range)")
    if dvl is not None and getattr(dvl, "tcp_error", ""):
        link_up = s is not None and s.link.connected
        return Row("dvl", name, NOGO if link_up else None,
                   "not streaming" if link_up else "not connected")
    return Row("dvl", name, None, "not connected")


def _gnss_compass(s: NavSnapshot | None) -> Row:
    name = "GNSS compass (topside)"
    if s is None or not s.vessel:
        return Row("gnss", name, None, "not heard from yet")
    pos, hdg = s.vessel.get("position"), s.vessel.get("heading")
    pos_ok = pos is not None and pos.quality is Quality.OK
    hdg_ok = hdg is not None and hdg.quality is Quality.OK
    if pos is not None and pos.quality is Quality.NEVER_RECEIVED and \
            hdg is not None and hdg.quality is Quality.NEVER_RECEIVED and \
            not (s.vessel.get("inject") and s.vessel["inject"].quality.has_value):
        return Row("gnss", name, None, "the UGPS External extension is not answering")
    if pos_ok and hdg_ok:
        return Row("gnss", name, GO, f"position + heading {hdg.value:.0f}°")
    if pos_ok:
        return Row("gnss", name, CHECK, "position, but no heading (HDT)")
    return Row("gnss", name, NOGO, "no position or heading")


def _g2(ugps: dict | None) -> Row:
    name = "UGPS G2 topside"
    if not ugps:
        return Row("g2", name, None, "not started")
    if ugps.get("connected"):
        st = ugps.get("status") or {}
        temp = st.get("board_temp_c")
        return Row("g2", name, GO, "answering"
                   + (f" · board {temp:.0f} °C" if isinstance(temp, (int, float)) else ""))
    if ugps.get("polls") or ugps.get("failures"):
        return Row("g2", name, NOGO, (ugps.get("error") or "not answering")[:60])
    return Row("g2", name, None, "not heard from yet")


def _receivers(ugps: dict | None, now: float) -> Row:
    name = "UGPS receivers ×4"
    if not ugps or not ugps.get("history"):
        return Row("rx", name, None, "no acoustic solutions yet")
    from .ugps import health as H
    judged = H.judge(ugps.get("history") or [], now)
    chips = []
    for rh in judged.receivers:
        lv = CHECK if rh.flags else (GO if rh.snr_db is not None else None)
        chips.append((f"Rx{rh.index}", lv))
    if judged.solutions < H.MIN_SOLUTIONS:
        return Row("rx", name, CHECK, judged.verdict[:60], chips)
    if judged.flagged:
        return Row("rx", name, CHECK, judged.verdict[:60], chips)
    snrs = [rh.snr_db for rh in judged.receivers if rh.snr_db is not None]
    return Row("rx", name, GO, f"all alike · SNR {min(snrs):.0f}–{max(snrs):.0f} dB"
               if snrs else "all alike", chips)


def _groups(ugps: dict) -> dict[str, str]:
    st = (ugps or {}).get("status") or {}
    out = {}
    for part in str(st.get("status_groups") or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _locator(ugps: dict | None, s: NavSnapshot | None) -> Row:
    name = "U1 locator (on the ROV)"
    fix = s.ugps.get("fix") if s is not None else None
    acoustic = (ugps or {}).get("acoustic") or {}
    group = _groups(ugps or {}).get("locator")
    if group and group != "ok":
        return Row("u1", name, NOGO, f"G2 reports the locator {group}")
    if fix is not None and fix.quality is Quality.OK:
        std = s.ugps.get("std_m")
        return Row("u1", name, GO, "acoustic fix"
                   + (f" · σ {std.number():.1f} m" if std is not None and std.number() else ""))
    if fix is not None and fix.quality is Quality.INVALID:
        return Row("u1", name, NOGO, "no acoustic fix")
    if acoustic.get("position_valid") in (True, 1):
        return Row("u1", name, GO, "acoustic fix (G2)")
    if ugps and ugps.get("connected"):
        return Row("u1", name, CHECK, "no acoustic solution from the G2")
    return Row("u1", name, None, "not heard from yet")


def _leak(s: NavSnapshot | None) -> Row:
    name = "Leak sensors"
    if s is None or not s.leak.quality.has_value:
        return Row("leak", name, None, "not heard from yet")
    if s.leak.value:
        text = s.statustext.value if s.statustext.quality.has_value else ""
        return Row("leak", name, NOGO, f"LEAK — {text}" if text else "LEAK DETECTED")
    enabled = (s.params or {}).get("FS_LEAK_ENABLE")
    if enabled is not None and not enabled:
        return Row("leak", name, CHECK, "dry, but the leak failsafe is off")
    return Row("leak", name, GO, "dry")


def _latest_levels(inp: Inputs, columns) -> list[tuple[str, int]]:
    rows = inp.history_rows or []
    out = []
    for c in columns:
        lv = bands.level_now(c, rows) if rows else None
        if lv:
            out.append((c, lv))
    return out


def _tether(inp: Inputs) -> Row:
    name = "Tether"
    row = inp.latest_row or {}
    if not row:
        return Row("tether", name, None, "not measured yet")
    worst = _latest_levels(inp, TETHER_COLUMNS)
    link = row.get("tether_link_mbps")
    ping = row.get("rov_ping_latency_ms")
    bits = []
    if isinstance(link, (int, float)):
        bits.append(f"{link:.0f} Mbps")
    if isinstance(ping, (int, float)):
        bits.append(f"ping {ping:.0f} ms")
    if worst:
        lv = max(lv for _c, lv in worst)
        names = ", ".join(c for c, x in worst if x == lv)
        return Row("tether", name, NOGO if lv == bands.BAD else CHECK,
                   f"{names} {bands.LEVEL_WORDS[lv]}")
    return Row("tether", name, GO if bits else None, " · ".join(bits) or "not measured yet")


def _laptop(inp: Inputs) -> Row:
    name = "Topside laptop"
    if not inp.latest_row:
        return Row("laptop", name, None, "not recording yet")
    from . import laptop as L
    columns = [c for g in L.GROUPS.values() for c in g
               if c not in TETHER_COLUMNS and not c.startswith("pi_")]
    worst = _latest_levels(inp, columns)
    if not worst:
        return Row("laptop", name, GO, "every reading normal")
    lv = max(x for _c, x in worst)
    names = [c for c, x in worst if x == lv]
    more = f" (+{len(names) - 2})" if len(names) > 2 else ""
    return Row("laptop", name, NOGO if lv == bands.BAD else CHECK,
               ", ".join(names[:2]) + more + f" {bands.LEVEL_WORDS[lv]}")


def _recording(inp: Inputs) -> Row:
    name = "Recording"
    st = inp.recorder_status
    if st is None:
        return Row("rec", name, CHECK, "not watching — choose a flight folder")
    if getattr(st, "problem", ""):
        return Row("rec", name, NOGO, st.problem[:60])
    state = getattr(st, "state", "idle")
    if state == "recording":
        return Row("rec", name, GO, f"recording {st.flight_id}")
    if inp.recorder_watching:
        return Row("rec", name, GO, "watching for the ROV to arm")
    return Row("rec", name, CHECK, "not watching")
