"""
The simulator's fault clock, and every fault it can put on the screen.

One clock runs the whole program: thirty seconds with a fault, fifteen with
everything green, then the next fault. During each faulty half-minute every
live tab has one thing wrong -- the Navigation tab one, the DVL tab one, and on
Monitoring one reading in every group (CPU, Memory, GPU, ...) -- so whichever
tab and whichever group is open, there is something to find. Each tab works
through its own list, one entry per cycle, and starts the list again at the
end.

A fault lives on its own tab. A DVL that loses bottom lock on the DVL tab does
not also lose it on the Navigation tab: each tab is a self-contained exercise
in recognising one failure. The exception says so in its own text: a Pi
Ethernet outage, the failure the 15 October 2026 dive actually had, reaches
everything that talks to the Pi -- which is the point of it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How long each fault lasts, and the all-green interval after it.
FAULT_S = 30.0
GREEN_S = 15.0
CYCLE_S = FAULT_S + GREEN_S

#: The Monitoring groups, in the order the page shows them. Each is a channel
#: of its own here.
MONITOR_CHANNELS = ("CPU", "Memory", "GPU", "Storage", "Network", "Cockpit",
                    "Power", "Tether")
NAV = "Navigation"
DVL = "DVL"


@dataclass(frozen=True)
class Fault:
    """One thing that can go wrong, and how to recognise it."""

    key: str
    channel: str
    #: Shown in the banner once revealed: a few words.
    title: str
    #: Written to the log when revealed: what is wrong, what it looks like on
    #: this tab, and what to do about it in the field.
    detail: str


def _f(channel: str, key: str, title: str, detail: str) -> Fault:
    return Fault(key=key, channel=channel, title=title, detail=" ".join(detail.split()))


CATALOG: dict[str, tuple[Fault, ...]] = {
    "CPU": (
        _f("CPU", "thermal_throttle", "the laptop is thermal throttling",
           """The processor has reached its temperature limit and Windows is
           holding its clock down. cpu_frequency_mhz falls toward 1 GHz and the
           ceiling drops, while cpu_usage_pct climbs: the same work, done
           slower. In the field: shade the laptop, open the case vents, close
           anything that is not flying the ROV."""),
        _f("CPU", "runaway_process", "a process is pinning the CPU",
           """Something is using every core -- an update, an antivirus scan, a
           stuck renderer. cpu_usage_pct and the busiest core sit near 100 %
           and the package draws more power and runs hotter. Cockpit's video
           will start to drop frames. Find it in Task Manager and end it."""),
        _f("CPU", "power_limited", "the CPU is power-limited",
           """Windows is capping the processor because the power supply cannot
           keep up -- a small car charger or a weak inverter on the boat. The
           ceiling drops to about half and the clock with it, while the package
           power sits flat and low. Use the full-size charger."""),
    ),
    "Memory": (
        _f("Memory", "memory_leak", "memory is leaking",
           """A program keeps allocating memory and never gives it back. RAM in
           use and committed memory climb steadily toward the limit and the
           memory still available falls below 2 GB. Restart the program that is
           growing -- usually the video client -- between transects."""),
        _f("Memory", "paging_storm", "the laptop is paging hard",
           """Memory has been pushed out to disk and is being read back:
           pages_per_s stays in the thousands and the page file fills. Everything
           on the laptop becomes sluggish, video included. Close what is not
           needed."""),
        _f("Memory", "commit_limit_grew", "the commit limit was reached",
           """Committed memory hit the limit and Windows grew the page file to
           make room: watch memory_commit_limit_gb step up. The next allocation
           that does not fit makes a program crash. Restart whatever has grown
           before it does."""),
    ),
    "GPU": (
        _f("GPU", "decoder_saturated", "the video decoder is saturated",
           """The graphics chip's video decoder is nearly fully booked -- a second
           camera stream, or a codec without hardware support. gpu_video_decode_pct
           jumps from about 7 % to near 90 % and the whole GPU is busy. Expect
           stutter and lag in Cockpit's video."""),
        _f("GPU", "gpu_memory_leak", "graphics memory is leaking",
           """Shared graphics memory climbs past 3 GB and does not come back --
           a leak in the video client, which grows until the client crashes.
           Restart Cockpit between transects."""),
        _f("GPU", "gpu_hot", "the graphics chip is hot",
           """The GPU, which shares the CPU's die, is near its temperature limit
           and drawing more power than video needs. A hot case in the sun, or
           something rendering heavily in the background."""),
    ),
    "Storage": (
        _f("Storage", "disk_almost_full", "the disk is nearly full",
           """A large copy is filling the disk: free space falls under 10 GB and
           keeps falling. Downloads from the Pi will fail and the recorders run
           short. Free space before the next flight -- move old flights off
           the laptop."""),
        _f("Storage", "background_writer", "something is hammering the disk",
           """A background job -- a sync client, Windows Update, a video copy --
           is writing hundreds of MB a second. The disk is busy past 85 % and
           requests queue up, so the recorders' writes wait. Pause syncing
           during flights."""),
        _f("Storage", "ssd_hot", "the SSD is overheating",
           """The SSD is above 70 °C, where it slows its own writes to cool
           down. A laptop in the sun with its vents blocked."""),
    ),
    "Network": (
        _f("Network", "tether_congestion", "the tether is congested",
           """Pings to the Pi take hundreds of milliseconds and a quarter are
           lost; the Pi's replies slow past a second. Control and video both
           lag. Usually a noisy or overloaded link: something else is using the
           tether, or the line is wet at a connector."""),
        _f("Network", "video_stopped", "the video stream has stopped",
           """Cockpit is running but almost nothing is arriving: network_receive_mbps
           falls from about 12 Mbps to nearly zero. The camera stream has stopped
           -- the camera, its encoder or its stream on the Pi. Check the
           camera's own page."""),
        _f("Network", "packets_discarded", "the adapter is discarding packets",
           """network_packets_discarded, normally flat all day, starts climbing:
           the laptop is throwing away packets it received. A driver or buffer
           problem on the laptop's side of the tether."""),
        _f("Network", "link_saturated", "the tether link is saturated",
           """Something is pulling 80+ Mbps down the tether -- a large download
           off the Pi during the flight. The link is nearly full, latency
           climbs, and the video competes with the download. Download after
           surfacing, not during."""),
    ),
    "Cockpit": (
        _f("Cockpit", "cockpit_leak", "Cockpit is leaking",
           """Cockpit's memory and handle count climb without stopping -- an
           Electron leak. Past about 4 GB it will crash, usually at the worst
           moment. Restart it between transects."""),
        _f("Cockpit", "gpu_process_crashed", "a Cockpit process crashed",
           """Cockpit normally runs as four processes; one has died -- usually
           its GPU process. The window stays open but the video freezes or goes
           black, and Cockpit's GPU share drops to nothing. Reload Cockpit."""),
        _f("Cockpit", "renderer_spinning", "Cockpit's renderer is spinning",
           """Cockpit is using most of the CPU and GPU on its own -- a widget or
           a renderer stuck in a loop. Reload the view."""),
    ),
    "Power": (
        _f("Power", "charger_failing", "the charger cannot keep up",
           """The laptop is plugged in but still drawing on its battery: an
           underpowered adapter or a failing inverter. The charge falls slowly
           while the power source still says AC. Change the supply before the
           battery runs out."""),
        _f("Power", "battery_draining", "the laptop is on battery and draining",
           """Mains power has gone -- an unplugged cable or a tripped inverter --
           and the laptop is drawing over 40 W from its battery. The charge
           falls fast toward 20 %, where Windows starts saving power at the
           expense of the video."""),
        _f("Power", "pi_overheating", "the Pi in the vehicle is overheating",
           """The Raspberry Pi inside the vehicle passes 80 °C and starts slowing
           its own processor, which starves video and telemetry. It reached 76 °C
           on a survey in September. Shorter dives, or a cooler enclosure."""),
        _f("Power", "laptop_in_sun", "the laptop is cooking in the sun",
           """The chassis and battery are hot and the fan is at its maximum:
           a laptop in direct sun on deck. Lithium batteries stop charging
           around 45 °C. Shade it."""),
    ),
    "Tether": (
        _f("Tether", "pi_port_dropped", "the Pi's Ethernet port dropped",
           """The Pi's own end of the tether lost its link: pi_eth_carrier_drops
           steps up by one, the Pi's byte count stops, and layer 2 briefly
           cannot find the vehicle. The Fathom-X pair still hears each other,
           which places the fault on the Pi's cable or port -- the 15 October
           2026 finding."""),
        _f("Tether", "fathom_degraded", "the Fathom-X link has degraded",
           """The two Fathom-X boards renegotiated far lower -- 31 Mbps one way
           and 66 the other. Under 40 Mbps the video will not fit, and the two
           directions this far apart usually means a wire disconnected rather
           than a noisy line. Check the tether connectors."""),
        _f("Tether", "adapter_asleep", "Windows put the tether adapter to sleep",
           """Windows' power management has flagged the tether adapter for a
           low-power state -- the step before the link drops. Turn off "Allow
           the computer to turn off this device to save power" for that adapter;
           Check the network on this tab says which one."""),
        _f("Tether", "pi_interface_restarted", "the Pi's network interface restarted",
           """The Pi's own count of bytes received went back to almost nothing:
           its Ethernet interface was reset, or BlueOS restarted its networking.
           Anything the Pi was doing over the tether was interrupted."""),
    ),
    NAV: (
        _f(NAV, "dvl_lock_lost", "the DVL lost bottom lock",
           """The autopilot's downward range reads zero, so altitude goes
           invalid -- not zero -- and the DVL rows in the sensor matrix go red.
           Speed comes only from the estimator now. Flying too high, over a
           drop-off, or through a cloud of silt."""),
        _f(NAV, "ugps_weak_receiver", "one UGPS receiver is weak",
           """Receiver 2's signal is 14 dB below the other three, and the row
           under the map flags it. A fouled or shadowed hydrophone, or a cable
           partly out of its connector. The solution still works on three,
           with less margin."""),
        _f(NAV, "vessel_hdt_stale", "the topside heading (HDT) stopped",
           """The topside GNSS compass is still sending its position but not
           its heading, so the acoustic solution cannot be oriented: the vessel
           heading row goes red while its position stays green."""),
        _f(NAV, "acoustic_lost", "the acoustic position is lost",
           """The UGPS stops producing a valid solution: GPS_RAW_INT's fix type
           drops to 0, the acoustic row goes red and the estimator falls back to
           relative aiding -- the position is now dead-reckoned from the DVL and
           will drift."""),
        _f(NAV, "pi_outage", "the Pi dropped off the network",
           """The Pi's Ethernet went quiet for eight seconds, both ways -- the
           failure of the 15 October 2026 dive. Every MAVLink read times out,
           the banner's Vehicle connected lamp goes out, and three seconds in
           the autopilot's GCS failsafe disarms the vehicle. It reaches every
           tab: the DVL tab can no longer see the extension's MAVLink, though
           the DVL itself keeps streaming. Monitoring shows the vehicle
           unreachable and, once it answers again, disarmed."""),
        _f(NAV, "estimator_reset", "the estimator reset and the position jumped",
           """The estimator restarted its horizontal solution and the position
           jumped about 15 m before converging back. The map breaks the track
           rather than drawing across the jump, and the jump is listed beside
           the position."""),
        _f(NAV, "ugps_noisy_receiver", "one UGPS receiver is noisy",
           """Receiver 1's noise floor is 12 dB above the others -- a thruster,
           a pump or the vessel's own sounder near that hydrophone. The row
           under the map flags it."""),
        _f(NAV, "vessel_nmea_lost", "the topside GNSS compass went silent",
           """Neither position nor heading is reaching the UGPS topside: the
           GNSS compass stopped sending NMEA, or the extension's listener died
           on a sentence it does not know (the October 2026 firmware-update
           problem). Both vessel rows go red."""),
        _f(NAV, "leak_detected", "a leak sensor is wet",
           """ArduSub is sending "Leak Detected" every 20 seconds: a moisture
           sensor in one of the enclosures is wet. The Leak sensors row on the
           Monitoring board goes red. Surface, power down and open the
           enclosures before the water reaches the electronics."""),
        _f(NAV, "compass_interference", "the ROV's compass is disturbed",
           """The vehicle's heading swings by twenty degrees either way while it
           flies straight -- magnetic interference from a piling or a steel
           hull nearby. Watch the heading arrow on the map."""),
    ),
    DVL: (
        _f(DVL, "beam_dead", "beam T3 is not firing",
           """Transducer T3 (forward-port) returns nothing: no range, signal at the
           noise floor, and its tile goes red. The DVL still solves velocity on
           the other three, but its figure of merit gets worse. A damaged
           transducer, a cable fault, or something covering the face."""),
        _f(DVL, "snr_collapse", "electrical noise is drowning the beams",
           """The noise floor on all four beams has risen about 40 dB -- electrical
           noise from thrusters or lights coupling into the transducers. Signal
           to noise collapses, beams flicker invalid and velocity drops in and
           out. Look at the spectrum."""),
        _f(DVL, "bottom_lock_lost", "the DVL has lost bottom lock",
           """No beam finds the bottom: altitude reads −1, velocity is invalid,
           and the stock BlueOS extension drops every invalid report -- so its
           rates under Delivery and stream health fall to zero. These are the
           gaps that end up in the mcap."""),
        _f(DVL, "extension_stopped", "the BlueOS DVL extension stopped",
           """The DVL is fine and streaming, but the BlueOS extension that
           forwards it to the autopilot has stopped: its MAVLink counts freeze
           and DISTANCE_SENSOR goes stale. The DVL's own page would look
           healthy -- this is why the capture reads both sides."""),
        _f(DVL, "piling_multipath", "beam T1 is hitting a piling",
           """Beam T1's (aft-starboard) range is far shorter than the others -- it
           is striking a
           pier piling rather than the bottom. The altitude estimate drops, the
           figure of merit worsens and velocity occasionally goes invalid. Move
           away from the pier."""),
        _f(DVL, "periodic_cycling", "periodic cycling is switched on",
           """The DVL's periodic cycling is on, so every ten seconds it pauses to
           check its range settings and reports stop for about a second. The
           team runs with it off for exactly this reason."""),
        _f(DVL, "delivery_stall", "reports are arriving in bursts",
           """The DVL makes its reports on time but they reach the laptop late,
           several at once -- the network between them is stalling. The gaps
           are classed as delivery stalls, read from the DVL's own clock."""),
        _f(DVL, "dvl_hot", "the DVL is overheating",
           """The DVL's temperature climbs past 50 °C and it raises its own
           high-temperature warning. It happens when it runs out of the water
           for long -- on deck, in the sun. Turn the acoustics off on deck."""),
        _f(DVL, "acoustics_off", "the DVL's acoustics are switched off",
           """Somebody switched the acoustics off: the DVL stays connected but
           makes no velocity reports at all. The silence is classed as
           acoustics-off rather than as a fault in the stream."""),
        _f(DVL, "missing_reports", "reports are going missing",
           """The DVL makes its reports -- its own clock and report counter say
           so -- but some never arrive. The gaps are classed as missing reports,
           which places the loss between the DVL and this laptop."""),
    ),
}

CHANNELS = (*MONITOR_CHANNELS, NAV, DVL)


@dataclass(frozen=True)
class Phase:
    """Where the clock is: which cycle, and whether it is the faulty half."""

    cycle: int
    faulty: bool
    #: Seconds into this half, and seconds left in it.
    elapsed: float
    left: float

    @property
    def progress(self) -> float:
        span = FAULT_S if self.faulty else GREEN_S
        return max(0.0, min(1.0, self.elapsed / span))


class Schedule:
    """The fault clock, on the simulation's own time.

    `start` is when the first fault begins. Everything before it is green --
    that is the history drawn when the simulator is switched on, so the charts
    open on a healthy flight rather than an empty one.
    """

    def __init__(self, start: float) -> None:
        self.start = float(start)

    def phase(self, t: float) -> Phase:
        if t < self.start:
            return Phase(cycle=-1, faulty=False, elapsed=0.0, left=self.start - t)
        cycle, into = divmod(t - self.start, CYCLE_S)
        if into < FAULT_S:
            return Phase(int(cycle), True, into, FAULT_S - into)
        return Phase(int(cycle), False, into - FAULT_S, CYCLE_S - into)

    @staticmethod
    def fault_for(channel: str, cycle: int) -> Fault | None:
        faults = CATALOG.get(channel, ())
        if cycle < 0 or not faults:
            return None
        return faults[cycle % len(faults)]

    def fault(self, channel: str, t: float) -> tuple[Fault | None, float]:
        """(the fault active on `channel` at `t`, how far through it 0..1)."""
        ph = self.phase(t)
        if not ph.faulty:
            return None, 0.0
        return self.fault_for(channel, ph.cycle), ph.progress

    def cycle_start(self, cycle: int) -> float:
        return self.start + cycle * CYCLE_S

    def windows(self, channel: str, key: str, t_end: float) -> list[float]:
        """Start times of every window of fault `key` on `channel` before `t_end`."""
        faults = CATALOG.get(channel, ())
        if not faults or t_end < self.start:
            return []
        idx = [i for i, f in enumerate(faults) if f.key == key]
        if not idx:
            return []
        out = []
        last = int((t_end - self.start) // CYCLE_S)
        for c in range(0, last + 1):
            if c % len(faults) in idx:
                out.append(self.cycle_start(c))
        return out
