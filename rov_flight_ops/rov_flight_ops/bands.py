"""
What normal, unusual and bad look like for each Live monitoring reading.

The Monitoring charts colour every reading by how worried an operator should be
about it: **normal**, **unusual** (worth a look) and **bad** (act on it). The
judgements are here, apart from the drawing, so they can be read, argued with
and tested without a screen.

Where the numbers came from
---------------------------

*Normal* is measured. Every band was checked against the 27 laptop-monitor
logs recorded between 11 September and 15 October 2026 -- six Port of Seattle
survey days and the OTS test flights, 39,842 rows from L342-D -- so that a
healthy flight sits in the normal band nearly all the time. Where a log shows
that something already went wrong (the Pi above 70 °C for ten minutes at EBM_W
on 17 September; the vehicle unreachable for 229 s on 11 September; the CPU's
frequency ceiling dropping to 21 % at Grain Elevator), the band was set so that
episode reads as it should.

*Unusual* and *bad* are mostly judgement, because the logs hold few failures
of most kinds. They come from what the hardware is documented to do: Intel's
throttle points, Windows' memory manager, the Raspberry Pi's soft and hard
temperature limits, the Fathom-X's negotiated rates, Electron's habit of
leaking handles. Each band carries a sentence saying why, which
`specifications/monitoring_bands.md` collects into one table.

How a reading is judged
-----------------------

* **Smoothed where it is noisy.** A CPU at 100 % for one second is a busy
  second, not a fault, so most rates are judged on their mean over a few
  seconds (`smooth_s`). The line is still drawn raw.
* **Counters by how they move.** A cumulative count -- bytes received,
  packets discarded, carrier drops -- has no "high"; what matters is whether
  it is climbing when it should not (`rise`) or has stopped when it should be
  climbing (`stall`).
* **Some only in context.** A low clock speed on an idle laptop is the power
  plan doing its job, so `cpu_frequency_mhz` is judged only while the CPU is
  busy; little video arriving is only a fault while Cockpit is running.
* **A blank is not judged.** No value, no colour: the strip says "—".
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

NORMAL, UNUSUAL, BAD = 0, 1, 2

#: What each level is called, and the shape drawn beside it so the level is
#: never carried by colour alone -- a daylight-washed screen and colour-vision
#: deficiency both take the colour first.
LEVEL_WORDS = {NORMAL: "normal", UNUSUAL: "unusual", BAD: "bad"}
LEVEL_GLYPHS = {NORMAL: "●", UNUSUAL: "▲", BAD: "✖"}

#: The status colours as (light, dark) pairs, for the chart surface of each
#: mode. Their own scale, deliberately not the theme's WARN/ERROR: in the dark
#: theme those two are the same coral, and here the difference between
#: "worth a look" and "act on it" is the whole point. Each clears 3:1 against
#: its surface (white; #0A1E36), which is the floor for a line.
LEVEL_COLORS = {
    NORMAL: ("#00795A", "#00C389"),
    UNUSUAL: ("#A86A00", "#FAB219"),
    BAD: ("#C0392B", "#F05A5A"),
}

DAY_S = 86_400.0

Row = dict
Rows = Sequence[dict]


def _num(value) -> float | None:
    """A cell as a number: TRUE/FALSE as 1/0, a blank as None."""
    if value is None or value == "":
        return None
    if value == "TRUE":
        return 1.0
    if value == "FALSE":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


@dataclass(frozen=True)
class Band:
    """One way a reading can go wrong.

    `kind` says which way:

    ``high``   bigger is worse: unusual at `unusual`, bad at `bad`.
    ``low``    smaller is worse, the same way round.
    ``false``  FALSE is the fault, at `level`.
    ``true``   TRUE is the fault, at `level`.
    ``rise``   a cumulative count: climbing by `unusual` (or `bad`) within
               `window_s` is the fault. Any step down is bad -- the counter
               was reset, which means its owner restarted.
    ``stall``  a cumulative count that should be climbing: no change within
               `window_s` is bad.
    """

    kind: str
    unusual: float | None = None
    bad: float | None = None
    smooth_s: float = 0.0
    window_s: float = 10.0
    level: int = BAD
    why: str = ""
    #: row -> bool. The band is judged only on rows where this holds.
    applies: Callable[[Row], bool] | None = None
    #: row -> number. Judge this instead of the reading itself.
    derive: Callable[[Row], float | None] | None = None
    #: Shown under the reading's name. Worked out from the numbers when "".
    label: str = ""

    def threshold_text(self, unit: str = "") -> str:
        if self.label:
            return self.label
        u = f" {unit}" if unit and self.derive is None else ""
        if self.kind in ("high", "low"):
            op = "≥" if self.kind == "high" else "≤"
            bits = []
            if self.unusual is not None:
                bits.append(f"▲ {op} {_short(self.unusual)}{u}")
            if self.bad is not None:
                bits.append(f"✖ {op} {_short(self.bad)}{u}")
            return "  ".join(bits)
        if self.kind == "false":
            return f"{LEVEL_GLYPHS[self.level]} FALSE"
        if self.kind == "true":
            return f"{LEVEL_GLYPHS[self.level]} TRUE"
        if self.kind == "rise":
            return f"✖ climbing (in {self.window_s:g} s)"
        if self.kind == "stall":
            return f"✖ stops climbing ({self.window_s:g} s)"
        return ""

    def lines(self) -> list[tuple[float, int]]:
        """(value, level) of each threshold worth drawing on the strip."""
        if self.kind not in ("high", "low") or self.derive is not None:
            return []
        out = []
        if self.unusual is not None:
            out.append((self.unusual, UNUSUAL))
        if self.bad is not None:
            out.append((self.bad, BAD))
        return out


def _short(v: float) -> str:
    if abs(v) >= 10_000:
        return f"{v / 1000:g}k"
    return f"{v:g}"


# --------------------------------------------------------------------------
#  context the bands read from the rest of the row
# --------------------------------------------------------------------------


def _busy(row: Row) -> bool:
    v = _num(row.get("cpu_usage_pct"))
    return v is not None and v >= 50.0


def _cockpit_up(row: Row) -> bool:
    return row.get("cockpit_running") == "TRUE"


def _on_mains(row: Row) -> bool:
    return row.get("power_source") == "AC"


def _running(row: Row) -> bool:
    v = _num(row.get("cockpit_process_count"))
    return v is not None and v > 0


def _ram_pct(row: Row) -> float | None:
    used, free = _num(row.get("ram_used_gb")), _num(row.get("ram_available_gb"))
    if used is None or free is None or used + free <= 0:
        return None
    return 100.0 * used / (used + free)


def _commit_pct(row: Row) -> float | None:
    used = _num(row.get("memory_committed_gb"))
    limit = _num(row.get("memory_commit_limit_gb"))
    if used is None or not limit:
        return None
    return 100.0 * used / limit


def _tether_gap(row: Row) -> float | None:
    tx, rx = _num(row.get("tether_tx_mbps")), _num(row.get("tether_rx_mbps"))
    if tx is None or rx is None:
        return None
    return abs(tx - rx)


def _vehicle_gone(row: Row) -> bool:
    return row.get("rov_reachable") == "FALSE"


# --------------------------------------------------------------------------
#  the bands
# --------------------------------------------------------------------------

#: Column -> the ways it can go wrong. A reading's level is the worst any of
#: its bands gives it. Every column on the Monitoring page is here.
BANDS: dict[str, tuple[Band, ...]] = {
    # -- CPU ----------------------------------------------------------------
    "cpu_usage_pct": (Band(
        "high", 80, 95, smooth_s=5,
        why="Cockpit's video and the recorders together run at 30-50 % on "
            "L342-D (95th percentile 76 %). Held above 80 % the window starts "
            "to lag; above 95 % video frames are dropped and the 1 Hz row "
            "slips."),),
    "cpu_max_core_usage_pct": (Band(
        "high", 92, 99, smooth_s=10,
        why="One core pinned for ten seconds is a single-threaded job that "
            "cannot go faster -- usually a stuck renderer -- even while the "
            "average looks fine."),),
    "cpu_frequency_mhz": (Band(
        "low", 1600, 1000, smooth_s=5, applies=_busy,
        why="Judged only while the CPU is busy (≥ 50 %): an idle laptop "
            "clocks down on purpose. Busy and under 1.6 GHz is a laptop being "
            "held back; under 1 GHz is thermal or power throttling (the lowest "
            "seen on a survey was 981 MHz).",
        label="▲ ≤ 1600  ✖ ≤ 1000 (while busy)"),),
    "cpu_max_freq_ceiling_pct": (Band(
        "low", 80, 50, smooth_s=3,
        why="The highest clock Windows will currently allow. L342-D's power "
            "plan holds it at 89 %; a drop is firmware or Windows capping the "
            "processor for heat or power. It fell to 21 % for 17 s at Grain "
            "Elevator on 15 September."),),
    "cpu_package_temp_c": (Band(
        "high", 80, 90, smooth_s=5,
        why="Tiger Lake throttles at 100 °C. A laptop running video sits at "
            "60-75 °C; 80 °C is a hot case, 90 °C is minutes from throttling."),),
    "cpu_max_core_temp_c": (Band(
        "high", 85, 95, smooth_s=5,
        why="The hottest core, against the same 100 °C limit. 95 °C means the "
            "clock is about to be pulled down."),),
    "cpu_package_power_w": (Band(
        "high", 28, 45, smooth_s=10,
        why="Above 28 W the package is past its long-term limit and running "
            "on turbo budget; held near 45 W it is shedding heat faster than "
            "a closed rugged case can."),),
    # -- memory ---------------------------------------------------------------
    "ram_used_gb": (Band(
        "high", 70, 85, smooth_s=5, derive=_ram_pct,
        why="Judged as a share of installed memory. Surveys use 30-45 % of "
            "L342-D's 32 GB; past 85 % Windows starts trimming programs and "
            "compressing memory.",
        label="▲ ≥ 70 %  ✖ ≥ 85 % of RAM"),),
    "ram_available_gb": (Band(
        "low", 4, 2, smooth_s=5,
        why="Below 4 GB free the next large allocation competes for memory; "
            "below 2 GB Windows signals low memory and paging begins."),),
    "ram_usage_pct": (Band(
        "high", 70, 85, smooth_s=5,
        why="The same judgement as ram_used_gb: 30-45 % is a normal survey "
            "day; 85 % is memory pressure."),),
    "memory_committed_gb": (Band(
        "high", 70, 90, smooth_s=5, derive=_commit_pct,
        why="Judged against the commit limit. At 100 % allocations fail and "
            "programs crash; Windows warns from about 90 %. Surveys peak "
            "under 50 %.",
        label="▲ ≥ 70 %  ✖ ≥ 90 % of limit"),),
    "memory_commit_limit_gb": (Band(
        "high", 70, 90, smooth_s=5, derive=_commit_pct,
        why="Shown in the colour of how full it is. A step up in the limit "
            "itself means Windows grew the page file because commit was "
            "running out.",
        label="coloured by commit ÷ limit"),),
    "pagefile_usage_pct": (Band(
        "high", 10, 25,
        why="Surveys use 0-6 % of the page file. A quarter of it in use means "
            "memory has been pushed to disk and reading it back is slow."),),
    "pages_per_s": (Band(
        "high", 300, 1500, smooth_s=10,
        why="Hard page faults. Single spikes into the thousands are normal "
            "(a program starting); held above 300/s for ten seconds is "
            "paging, above 1,500/s the disk is being thrashed."),),
    # -- GPU ------------------------------------------------------------------
    "gpu_usage_pct": (Band(
        "high", 75, 90, smooth_s=5,
        why="Iris Xe draws Cockpit's video at 35-60 %. Held above 90 % it "
            "cannot keep up and frames are dropped or late."),),
    "gpu_video_decode_pct": (Band(
        "high", 40, 75, smooth_s=5,
        why="One 1080p H.264 stream costs about 7 % of the decoder. A second "
            "stream or a codec without hardware support pushes it up; held "
            "above 75 % the video stutters."),),
    "gpu_memory_used_mb": (Band(
        "high", 1500, 3000, smooth_s=5,
        why="Surveys use 180-680 MB of shared graphics memory. Climbing past "
            "1.5 GB through a flight is a leak, usually in the video client."),),
    "gpu_memory_usage_pct": (Band(
        "high", 10, 20, smooth_s=5,
        why="Of the half of RAM Windows lends an integrated GPU. Surveys use "
            "1-4 %."),),
    "gpu_temp_c": (Band(
        "high", 80, 90, smooth_s=5,
        why="Integrated graphics share the CPU's die and its 100 °C limit."),),
    "gpu_power_w": (Band(
        "high", 12, 18, smooth_s=10,
        why="An integrated GPU decoding one stream draws a few watts; "
            "sustained above 12 W it is rendering far more than video."),),
    # -- storage --------------------------------------------------------------
    "disk_active_pct": (Band(
        "high", 50, 85, smooth_s=10,
        why="Surveys keep the disk 1-3 % busy. Busy past 85 % for ten "
            "seconds, writes queue and the recorders' syncs stall."),),
    "disk_read_mb_s": (Band(
        "high", 50, 300, smooth_s=10,
        why="Flights read almost nothing. Sustained reads are another job -- "
            "an antivirus scan, indexing, a sync client -- competing with the "
            "recorders."),),
    "disk_write_mb_s": (Band(
        "high", 50, 200, smooth_s=10,
        why="The recorders write well under 1 MB/s. Sustained heavy writes "
            "are something else -- an update, a sync, a video copy -- and "
            "fill the disk quickly: 200 MB/s is 12 GB a minute."),),
    "disk_queue_length": (Band(
        "high", 2, 8, smooth_s=5,
        why="Requests waiting for the disk. Surveys sit at 0; held above 2 "
            "the disk is behind, above 8 it has stopped keeping up."),),
    "disk_free_gb": (Band(
        "low", 30, 10,
        why="A survey day downloads tens of GB of recordings and video. Under "
            "30 GB a day's downloads may not fit; under 10 GB Windows and the "
            "recorders run short (the DVL capture stops its snapshots under "
            "2 GiB). L342-D was down to 10 GB on 18 September."),),
    "disk_free_pct": (Band(
        "low", 10, 5,
        why="The same, as a share: SSDs slow down and wear faster with less "
            "than 5-10 % free. L342-D spent most of September between 5 and "
            "10 %, so this strip was amber on most survey days -- rightly."),),
    "ssd_temp_c": (Band(
        "high", 60, 70, smooth_s=10,
        why="Consumer SSDs throttle their writes from about 70 °C."),),
    # -- network --------------------------------------------------------------
    "network_receive_mbps": (
        Band("high", 40, 70, smooth_s=5,
             why="The camera stream arrives at 6-18 Mbps. Held above 70 Mbps "
                 "the tether, which carries 80-130 Mbps at best, is nearly "
                 "full and the video will stall."),
        Band("low", 5, 2, smooth_s=5, applies=_cockpit_up,
             why="While Cockpit is running, under 2 Mbps arriving means the "
                 "video stream has stopped reaching the laptop.",
             label="▲ ≥ 40  ✖ ≥ 70 · ✖ ≤ 2 with Cockpit up")),
    "network_send_mbps": (Band(
        "high", 5, 20, smooth_s=5,
        why="The laptop sends the vehicle almost nothing (0.1-1 Mbps of "
            "MAVLink and requests). Megabits going up the tether are an "
            "upload or an update competing with the video coming down."),),
    "network_utilization_pct": (Band(
        "high", 50, 80, smooth_s=5,
        why="Of the adapter's link speed. Surveys use 11-19 %."),),
    "rov_ping_latency_ms": (Band(
        "high", 25, 100, smooth_s=3,
        why="Round trip to the Pi over the tether: 3-6 ms normally, 19 ms at "
            "the 99th percentile. Past 25 ms the link is congested or "
            "retrying; past 100 ms control and video both lag."),),
    "rov_packet_loss_pct": (Band(
        "high", 5, 20,
        why="Lost pings to the Pi, over the last 30. One lost (3.3 %) is "
            "common and means nothing; two or more is a link problem, and at "
            "20 % the vehicle is dropping in and out."),),
    "network_packets_discarded": (Band(
        "rise", 1, 50, window_s=10,
        why="A count since the adapter started, normally flat all day. Any "
            "rise during a flight means the laptop is throwing away packets "
            "it received -- a driver or buffer problem."),),
    "rov_http_ms": (Band(
        "high", 250, 1000, smooth_s=5,
        why="How long the Pi takes to answer a small request: 40-160 ms "
            "normally. Over a second it is overloaded or the tether is "
            "struggling, and the recorder's arm checks start to fail."),),
    # -- Cockpit --------------------------------------------------------------
    "cockpit_cpu_pct": (Band(
        "high", 45, 70, smooth_s=10,
        why="Of the whole CPU. Cockpit uses 15-37 % on L342-D; held above "
            "70 % a renderer is spinning."),),
    "cockpit_ram_mb": (Band(
        "high", 2500, 4000, smooth_s=5,
        why="Cockpit holds 0.9-1.6 GB on a survey (most ever 2.1 GB). Electron "
            "apps leak; past 4 GB a restart between transects is cheaper "
            "than a crash during one."),),
    "cockpit_gpu_usage_pct": (Band(
        "high", 60, 85, smooth_s=5,
        why="Cockpit's share of the GPU, normally 15-45 %."),),
    "cockpit_process_count": (
        Band("low", 3, 2, applies=_running,
             why="Cockpit runs as four processes. One fewer usually means its "
                 "GPU or renderer process crashed, which freezes or blanks the "
                 "video while the window stays open.",
             label="▲ ≤ 3  ✖ ≤ 2 while running · ▲ ≥ 10"),
        Band("high", 10, None,
             why="More than ten means extra windows or a respawning "
                 "renderer.")),
    "cockpit_handle_count": (Band(
        "high", 5000, 10_000,
        why="Steady at about 2,800. A count that climbs all flight is a leak; "
            "past 10,000 the process is in trouble."),),
    "cockpit_thread_count": (Band(
        "high", 200, 400,
        why="Steady at about 110."),),
    # -- power ----------------------------------------------------------------
    "battery_charge_pct": (Band(
        "low", 40, 20,
        why="On the boat the laptop is meant to be on mains. Under 40 % there "
            "is perhaps an hour left on battery; under 20 % Windows will "
            "start saving power at the expense of the video."),),
    "battery_discharge_w": (
        Band("high", 35, 45, smooth_s=10,
             why="On battery, L342-D draws 20-38 W flying; past 45 W the "
                 "battery is being emptied in about an hour."),
        Band("high", 1, 10, smooth_s=10, applies=_on_mains,
             why="Any draw from the battery while plugged in means the "
                 "charger cannot keep up -- an underpowered adapter or a "
                 "failing inverter.",
             label="▲ ≥ 35  ✖ ≥ 45 · ✖ ≥ 10 on mains")),
    "battery_temp_c": (Band(
        "high", 40, 50, smooth_s=10,
        why="Lithium cells stop charging around 45 °C and age fast above it; "
            "a laptop in the sun on deck gets there."),),
    "fan_speed_rpm": (Band(
        "high", 4500, 5800, smooth_s=10,
        why="A fan held at its maximum is cooling at its limit; the next "
            "step is throttling."),),
    "motherboard_temp_c": (Band(
        "high", 55, 70, smooth_s=10,
        why="The chassis zone. It rises with the sun on the case. (On L342-D "
            "this sensor reads a constant 25.1 °C.)"),),
    "pi_soc_temp_c": (Band(
        "high", 70, 80, smooth_s=10,
        why="The Raspberry Pi in the vehicle: 55-65 °C on a survey. It slows "
            "its own processor at 80 °C, which starves video and telemetry. "
            "It sat above 70 °C for ten minutes at EBM_W on 17 September."),),
    "system_uptime_s": (
        Band("high", 7 * DAY_S, None,
             why="A laptop that has not restarted in a week carries pending "
                 "updates and slowly leaking drivers into the field. Worth a "
                 "restart the evening before; never an emergency. L342-D has "
                 "run 28 days without one.",
             label="▲ ≥ 7 days · ▲ < 10 min"),
        Band("low", 600, None,
             why="Up for less than ten minutes in the middle of a day means "
                 "it restarted -- planned or not.")),
    # -- tether ---------------------------------------------------------------
    "nic_carrier": (Band(
        "false", level=BAD,
        why="The interface that routes to the vehicle has no link."),),
    "phy_carrier": (Band(
        "false", level=BAD, applies=_vehicle_gone,
        why="No link on the cable while the vehicle is also not answering: "
            "the tether is unplugged or broken. FALSE on its own is not "
            "judged -- on L342-D the watched adapter is often the bridge's "
            "other port, and was FALSE two thirds of the time on healthy "
            "flights.",
        label="✖ FALSE with the vehicle gone"),),
    "nic_low_power": (Band(
        "true", level=BAD,
        why="Windows has put the tether adapter into a low-power state, "
            "which drops the link."),),
    "phy_rx_bytes": (Band(
        "stall", window_s=10,
        why="Bytes arriving on the cable. It should climb every second the "
            "video runs; flat means nothing is arriving at the laptop at "
            "all."),),
    "rov_arp_ok": (Band(
        "false", level=BAD,
        why="Layer 2 cannot find the vehicle's address: it is not on the "
            "wire."),),
    "pi_eth_rx_bytes": (Band(
        "stall", window_s=30,
        why="What the Pi has received, read every 5 s. Flat, or reset to a "
            "smaller number, is the Pi's own port going quiet or the Pi "
            "restarting -- the 15 October outages."),),
    "pi_eth_carrier_drops": (Band(
        "rise", 1, 1, window_s=30,
        why="How many times the Pi's port has lost its link since boot. A "
            "step during a flight is the cable or port physically "
            "dropping."),),
    "tether_link_mbps": (Band(
        "low", 80, 40,
        why="The Fathom-X pair's negotiated rate: 104-132 Mbps on surveys. "
            "Under 80 the tether is noisy or wet at a connector; under 40 "
            "video will not fit."),),
    "tether_tx_mbps": (
        Band("low", 80, 40,
             why="The same, one direction."),
        Band("high", 20, 40, derive=_tether_gap,
             why="The two directions more than 20 Mbps apart usually means a "
                 "wire disconnected rather than a noisy line.",
             label="▲ ≤ 80  ✖ ≤ 40 · ▲ ≥ 20 apart")),
    "tether_rx_mbps": (
        Band("low", 80, 40,
             why="The same, the other direction."),
        Band("high", 20, 40, derive=_tether_gap,
             why="As for tether_tx_mbps.",
             label="▲ ≤ 80  ✖ ≤ 40 · ▲ ≥ 20 apart")),
    "tether_remote_seen": (Band(
        "false", level=BAD,
        why="The topside Fathom-X cannot hear the vehicle's at all."),),
}


def bands_for(column: str) -> tuple[Band, ...]:
    return BANDS.get(column, ())


def threshold_text(column: str, unit: str = "") -> str:
    """The thresholds, short enough to sit under the reading's name."""
    texts = [b.threshold_text(unit) for b in bands_for(column)]
    labelled = [b.label for b in bands_for(column) if b.label]
    if labelled:
        return labelled[0]
    return "  ".join(t for t in texts if t)


def threshold_lines(column: str) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for b in bands_for(column):
        if b.applies is None:
            out.extend(b.lines())
    return out


# --------------------------------------------------------------------------
#  judging
# --------------------------------------------------------------------------


def _level_high(v: float, b: Band) -> int:
    if b.bad is not None and v >= b.bad:
        return BAD
    if b.unusual is not None and v >= b.unusual:
        return UNUSUAL
    return NORMAL


def _level_low(v: float, b: Band) -> int:
    if b.bad is not None and v <= b.bad:
        return BAD
    if b.unusual is not None and v <= b.unusual:
        return UNUSUAL
    return NORMAL


def _band_levels(column: str, b: Band, times: list[float],
                 rows: Rows) -> list[int | None]:
    """One band's level at every row, or None where it cannot be judged."""
    n = len(rows)
    out: list[int | None] = [None] * n
    if b.derive is not None:
        values = [b.derive(r) for r in rows]
    else:
        values = [_num(r.get(column)) for r in rows]

    if b.kind in ("false", "true"):
        bad_value = 0.0 if b.kind == "false" else 1.0
        for i, v in enumerate(values):
            if v is None:
                continue
            if b.applies is not None and not b.applies(rows[i]):
                out[i] = NORMAL
                continue
            out[i] = b.level if v == bad_value else NORMAL
        return out

    if b.kind in ("rise", "stall"):
        for i, v in enumerate(values):
            if v is None:
                continue
            j = bisect.bisect_left(times, times[i] - b.window_s)
            # The oldest value inside the window, and the one just before.
            past = None
            for k in range(j, i):
                if values[k] is not None:
                    past = values[k]
                    break
            if past is None or times[i] - times[j] < b.window_s * 0.5:
                out[i] = NORMAL
                continue
            if v < past:
                out[i] = BAD                    # reset: its owner restarted
                continue
            if b.kind == "rise":
                out[i] = _level_high(v - past, b)
            else:
                # A count that has never moved is a port not in use, not one
                # that stopped.
                out[i] = BAD if (v == past and v > 0) else NORMAL
        return out

    # high / low, smoothed over `smooth_s`. The few samples in the window are
    # summed afresh each time rather than kept as a running total: a running
    # total drifts in the last bits, and a reading judged a row at a time
    # (the Monitoring page) must come out exactly as one judged all at once.
    judge = _level_high if b.kind == "high" else _level_low
    window: deque = deque()
    for i, v in enumerate(values):
        t = times[i]
        if v is not None:
            window.append((t, v))
        while window and window[0][0] < t - max(b.smooth_s, 0.0) - 1e-6:
            window.popleft()
        if v is None or not window:
            continue
        if b.applies is not None and not b.applies(rows[i]):
            out[i] = NORMAL
            continue
        mean = sum(x for _t, x in window) / len(window) if b.smooth_s > 0 else v
        out[i] = judge(mean, b)
    return out


def levels(column: str, rows: Rows) -> list[int | None]:
    """The level of `column` at every row: the worst any of its bands gives.

    None where the reading is blank, or there is no band for it. Rows must be
    in time order and carry ``elapsed_time_s``.
    """
    bands = bands_for(column)
    if not bands or not rows:
        return [None] * len(rows)
    times = [(_num(r.get("elapsed_time_s")) or 0.0) for r in rows]
    present = [_num(r.get(column)) is not None for r in rows]
    worst: list[int | None] = [None] * len(rows)
    for b in bands:
        for i, lv in enumerate(_band_levels(column, b, times, rows)):
            if lv is None or not present[i]:
                continue
            if worst[i] is None or lv > worst[i]:
                worst[i] = lv
    return worst


def level_now(column: str, rows: Rows) -> int | None:
    """The level of the newest row that has a value."""
    for lv in reversed(levels(column, rows[-120:])):
        if lv is not None:
            return lv
    return None


def why(column: str) -> list[str]:
    return [b.why for b in bands_for(column) if b.why]
