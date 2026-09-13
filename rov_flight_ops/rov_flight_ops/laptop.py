"""
What the laptop flying the ROV is doing, once a second.

BlueOS records the vehicle. Nothing records the machine on the other end of
the tether, and that machine is a field laptop in a case, in the sun, running
a video client -- the two failures this programme has actually had on deck
were a laptop too hot to keep its clock up and a tether link that went quiet.
Neither leaves a trace in any log that exists today.

This module answers one question -- *was the topside struggling, and when?* --
as a row of numbers per second. It is deliberately a **measurement**, not a
diagnosis: it records what the counters said and leaves the interpreting to
whoever reads the file, because a threshold picked in an office is exactly
the thing that turns out to be wrong on a boat.

Three properties matter more than completeness:

**It must not cost anything.** A sample is a handful of counter reads and no
process spawns, about 15 ms on the Latitude 5420 Rugged this was written on.
The 1 Hz cadence is a timer, not a busy loop, and the process list behind the
Cockpit columns is refreshed every ten seconds rather than every one.

**It must never stop a flight being recorded.** Every reading is individually
optional. A missing sensor, a vanished process, a counter this Windows does
not carry -- each leaves a blank cell and the row is still written. There is
no reading here worth losing an hour of survey for.

**The schema must not move.** Columns are fixed and in a fixed order, present
whether or not this particular machine can fill them, so two flights from two
laptops read into the same data frame. What a blank means is recorded once,
beside the CSV, by `wincounters.capabilities()`.

Where a value the vehicle knows is cheap and relevant -- the arm state, the
Pi's own temperature -- it is carried in the same row, because correlating
them afterwards across two files with two clocks is precisely the work this
is meant to save.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    import psutil
except ImportError:                                   # pragma: no cover
    psutil = None                                     # type: ignore

from . import netdiag
from . import wincounters as W

#: Sample period. 1 Hz is fast enough to catch a thermal event building over a
#: minute and a tether dropout lasting seconds, and slow enough that an hour
#: of survey is a 3,600-row file nobody has to think about.
DEFAULT_PERIOD_S = 1.0

#: How often the Cockpit process group is re-enumerated. Electron spawns its
#: renderers at startup and rarely after, so walking every process once a
#: second would be the most expensive thing here for no new information.
PROCESS_REFRESH_S = 10.0

#: How often the readings that are dear to take and slow to change -- the
#: battery's draw and the adapter's link state -- are refreshed.
#:
#: They are taken on a thread of their own rather than every Nth sample. Both
#: were measured on the station this was written for: the WMI battery query
#: costs 59 ms and has been seen to take 148, and enumerating adapter stats
#: costs 19. Folding those into one sample in five would have made that
#: sample eight times dearer than its neighbours, which is a worse property
#: for a fixed-cadence recorder than simply being slower everywhere. On their
#: own thread they cost the sample nothing at all.
SLOW_REFRESH_S = 5.0

#: How often layer 2 is asked whether it still knows where the vehicle is.
#:
#: On its own thread, and slower than everything else, because `SendARP`
#: blocks until the stack gives up -- measured at 3.16 s against an absent
#: vehicle, which is most of the slow thread's period and longer than the
#: join its shutdown allows. The high-resolution version of this reading is
#: the status code on each echo in the fast trace, where a resolution failure
#: arrives as `destination host unreachable` five times a second and costs
#: nothing. This is the confirmation, not the measurement.
ARP_REFRESH_S = 10.0

#: Process names counted as "Cockpit". The Blue Robotics client is an Electron
#: app, so one window is a main process plus a GPU process plus a renderer per
#: view; they are summed rather than reported apart.
COCKPIT_PROCESSES = ("cockpit.exe",)

#: Counted too when they are up, because the same station is sometimes flown
#: from Cockpit in a browser tab instead of the desktop client. Only processes
#: whose command line mentions the vehicle are taken, so an unrelated browser
#: window is not blamed for the laptop being busy.
COCKPIT_BROWSERS = ("chrome.exe", "msedge.exe", "firefox.exe")


# --------------------------------------------------------------------------
#  the columns
# --------------------------------------------------------------------------
#
# Order is the order they are written. Grouped as the operator asked for them,
# because a CSV opened in a spreadsheet is read left to right and the groups
# are how the questions actually arrive.

COLUMNS: tuple[str, ...] = (
    # identification and timing
    "timestamp_utc", "elapsed_time_s", "flight_id", "computer_name",
    # CPU
    "cpu_usage_pct", "cpu_max_core_usage_pct", "cpu_frequency_mhz",
    "cpu_max_freq_ceiling_pct",
    "cpu_package_temp_c", "cpu_max_core_temp_c", "cpu_package_power_w",
    "cpu_thermal_throttle", "cpu_power_limit_throttle",
    # RAM and virtual memory
    "ram_used_gb", "ram_available_gb", "ram_usage_pct",
    "memory_committed_gb", "memory_commit_limit_gb",
    "pagefile_usage_pct", "pages_per_s",
    # GPU and video processing
    "gpu_usage_pct", "gpu_video_decode_pct", "gpu_memory_used_mb",
    "gpu_memory_usage_pct", "gpu_temp_c", "gpu_power_w", "gpu_thermal_throttle",
    # storage
    "disk_active_pct", "disk_read_mb_s", "disk_write_mb_s",
    "disk_queue_length", "disk_free_gb", "disk_free_pct", "ssd_temp_c",
    # Ethernet and ROV connectivity
    "network_receive_mbps", "network_send_mbps", "network_utilization_pct",
    "network_errors_received", "network_errors_sent",
    "network_packets_discarded", "ethernet_connected",
    "ethernet_link_speed_mbps", "rov_ping_latency_ms", "rov_packet_loss_pct",
    # Cockpit and browser processes
    "cockpit_running", "cockpit_cpu_pct", "cockpit_ram_mb",
    "cockpit_gpu_usage_pct", "cockpit_process_count",
    "cockpit_handle_count", "cockpit_thread_count",
    # power and general laptop health
    "power_source", "battery_charge_pct", "battery_discharge_w",
    "battery_temp_c", "fan_speed_rpm", "motherboard_temp_c", "system_uptime_s",
    # the vehicle, carried in the same row so the two need not be joined later
    "rov_reachable", "rov_armed", "rov_http_ms", "pi_soc_temp_c",
    "pi_throttling",
    # the tether, on both sides of it
    #
    # Added after a flight in which every column above said the laptop was
    # healthy and none of them could say where the link had gone. The first
    # four are the distinction the old network columns could not draw: the
    # interface that routes to the vehicle is a Windows bridge, so its carrier
    # is the bridge's own, and the adapter underneath it is measured here.
    "nic_carrier", "nic_low_power", "phy_carrier", "phy_rx_bytes",
    "rov_arp_ok",
    # and what the vehicle counted on its own side of the same link
    "pi_eth_rx_bytes", "pi_eth_rx_errors", "tether_link_mbps",
)

#: Units, for the header note written beside the CSV. Anything not named here
#: is a count, a name or a TRUE/FALSE.
UNITS: dict[str, str] = {
    "elapsed_time_s": "s", "cpu_usage_pct": "%", "cpu_max_core_usage_pct": "%",
    "cpu_frequency_mhz": "MHz", "cpu_max_freq_ceiling_pct": "%",
    "cpu_package_temp_c": "C", "cpu_max_core_temp_c": "C",
    "cpu_package_power_w": "W", "ram_used_gb": "GB", "ram_available_gb": "GB",
    "ram_usage_pct": "%", "memory_committed_gb": "GB",
    "memory_commit_limit_gb": "GB", "pagefile_usage_pct": "%",
    "pages_per_s": "pages/s", "gpu_usage_pct": "%", "gpu_video_decode_pct": "%",
    "gpu_memory_used_mb": "MB", "gpu_memory_usage_pct": "%", "gpu_temp_c": "C",
    "gpu_power_w": "W", "disk_active_pct": "%", "disk_read_mb_s": "MB/s",
    "disk_write_mb_s": "MB/s", "disk_free_gb": "GB", "disk_free_pct": "%",
    "ssd_temp_c": "C", "network_receive_mbps": "Mbps",
    "network_send_mbps": "Mbps", "network_utilization_pct": "%",
    "ethernet_link_speed_mbps": "Mbps", "rov_ping_latency_ms": "ms",
    "rov_packet_loss_pct": "%", "cockpit_cpu_pct": "%", "cockpit_ram_mb": "MB",
    "cockpit_gpu_usage_pct": "%", "battery_charge_pct": "%",
    "battery_discharge_w": "W", "battery_temp_c": "C", "fan_speed_rpm": "rpm",
    "motherboard_temp_c": "C", "system_uptime_s": "s", "rov_http_ms": "ms",
    "pi_soc_temp_c": "C", "phy_rx_bytes": "bytes", "pi_eth_rx_bytes": "bytes",
    "tether_link_mbps": "Mbps",
}

#: The groups the operator asked to be able to look at one at a time,
#: and what belongs to each. The monitoring page's selector is built from
#: this, so adding a column above puts it on screen without touching the GUI.
#:
#: Names are kept to one word because they become the buttons on a strip that
#: has to fit all of them across a field laptop's window; the sentence that
#: says what each one is for lives in GROUP_NOTES below.
GROUPS: dict[str, tuple[str, ...]] = {
    "CPU": ("cpu_usage_pct", "cpu_max_core_usage_pct", "cpu_frequency_mhz",
            "cpu_max_freq_ceiling_pct", "cpu_package_temp_c",
            "cpu_max_core_temp_c", "cpu_package_power_w"),
    "Memory": ("ram_used_gb", "ram_available_gb", "ram_usage_pct",
               "memory_committed_gb", "memory_commit_limit_gb",
               "pagefile_usage_pct", "pages_per_s"),
    "GPU": ("gpu_usage_pct", "gpu_video_decode_pct",
            "gpu_memory_used_mb", "gpu_memory_usage_pct",
            "gpu_temp_c", "gpu_power_w"),
    "Storage": ("disk_active_pct", "disk_read_mb_s", "disk_write_mb_s",
                "disk_queue_length", "disk_free_gb", "disk_free_pct",
                "ssd_temp_c"),
    "Network": ("network_receive_mbps", "network_send_mbps",
                "network_utilization_pct", "rov_ping_latency_ms",
                "rov_packet_loss_pct", "network_packets_discarded",
                "rov_http_ms"),
    "Cockpit": ("cockpit_cpu_pct", "cockpit_ram_mb", "cockpit_gpu_usage_pct",
                "cockpit_process_count", "cockpit_handle_count",
                "cockpit_thread_count"),
    "Power": ("battery_charge_pct", "battery_discharge_w",
              "battery_temp_c", "fan_speed_rpm",
              "motherboard_temp_c", "pi_soc_temp_c", "system_uptime_s"),
    "Tether": ("nic_carrier", "phy_carrier", "nic_low_power",
               "phy_rx_bytes", "rov_arp_ok", "pi_eth_rx_bytes",
               "tether_link_mbps"),
}

#: What each group is actually for, shown under the strip. Written as the
#: question the group answers rather than as a list of its contents -- the
#: contents are on screen already.
GROUP_NOTES: dict[str, str] = {
    "CPU": "Is the processor keeping up, and is it being held back? "
           "cpu_frequency_mhz falling while usage stays high is the signature "
           "of a hot or power-limited laptop.",
    "Memory": "Is anything running out of room? Committed memory climbing "
              "toward the limit, or paging that will not settle, means the "
              "machine is about to start swapping.",
    "GPU": "Video decode is where Cockpit's stream lands. On integrated "
           "graphics there is no temperature or power sensor to read, so "
           "those two stay blank.",
    "Storage": "The recorder and the camera both write here. Watch free space "
               "on a 256 GB disk, and the queue length for a disk that has "
               "stopped keeping up.",
    "Network": "The tether. Round-trip time and loss are measured against the "
               "vehicle itself; the throughput is whatever is flowing over "
               "the interface that routes to it.",
    "Cockpit": "The flight client, summed across its own processes and any "
               "browser pointed at the vehicle. Blank when it is not running.",
    "Power": "Battery, and the two temperatures there are. motherboard_temp_c "
             "is the chassis zone -- the one that moves when the laptop sits "
             "in the sun; pi_soc_temp_c is the vehicle's own.",
    "Tether": "The link itself, from both ends. nic_ is the interface that "
              "routes to the vehicle; phy_ is the physical adapter under it, "
              "which on a bridged station is a different device with a "
              "different carrier. phy_rx_bytes still climbing while the "
              "bridge has gone quiet means the bridge stopped forwarding, not "
              "that the tether dropped. pi_eth_rx_bytes is the vehicle's own "
              "count of what arrived.",
}


def _f(value, digits: int = 2):
    """A number rounded, or None. Keeps the CSV narrow and the plots honest."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:                                  # NaN
        return None
    return round(v, digits)


def _b(value) -> str | None:
    """TRUE/FALSE as the operator asked, or blank when it is not known.

    Blank rather than FALSE: "the fan is not spinning" and "this laptop has no
    readable fan sensor" are different findings, and a column that renders the
    second as the first is worse than no column.
    """
    return None if value is None else ("TRUE" if value else "FALSE")


# --------------------------------------------------------------------------
#  which Ethernet interface reaches the vehicle
# --------------------------------------------------------------------------


def _ip_to_int(addr: str) -> int | None:
    try:
        return struct.unpack("!I", socket.inet_aton(addr))[0]
    except OSError:
        return None


def find_rov_interface(host: str = "192.168.2.2") -> str | None:
    """The adapter whose own subnet contains the vehicle.

    Not a hard-coded name. On this station the tether does not arrive on the
    Ethernet adapter it appears to: the ROV port and the UGPS port are joined
    into a Windows network bridge, and it is the bridge -- not the Realtek
    adapter under it -- that holds 192.168.2.1 and carries the counters. A
    name baked in here would have measured an interface with no traffic on it
    and reported a perfectly healthy zero.

    Matching on the subnet finds whichever interface actually routes to the
    vehicle, bridged or not, on this laptop or the next one.
    """
    if psutil is None:
        return None
    target = _ip_to_int(host.split(":")[0])
    if target is None:
        try:
            target = _ip_to_int(socket.gethostbyname(host.split(":")[0]))
        except OSError:
            return None
    if target is None:
        return None
    try:
        addrs = psutil.net_if_addrs()
    except Exception:
        return None
    for name, entries in addrs.items():
        for e in entries:
            if e.family != socket.AF_INET or not e.netmask:
                continue
            ip, mask = _ip_to_int(e.address), _ip_to_int(e.netmask)
            if ip is None or mask is None:
                continue
            if (ip & mask) == (target & mask):
                return name
    return None


# --------------------------------------------------------------------------
#  one sample
# --------------------------------------------------------------------------


@dataclass
class VehicleState:
    """What the vehicle side last reported, carried into each row.

    Filled by whoever is polling the vehicle -- the flight recorder does it on
    its own slow cadence -- and read here without blocking. A sample must never
    wait on the tether.
    """

    reachable: bool | None = None
    armed: bool | None = None
    http_ms: float | None = None
    soc_temp_c: float | None = None
    throttling: bool | None = None
    #: What the Pi counted on its own end of the tether. Cumulative, so the
    #: reading taken when a link returns says how much arrived while it was
    #: down -- which is the one thing the topside cannot work out alone.
    eth_rx_bytes: int | None = None
    eth_rx_errors: int | None = None
    #: The negotiated rate between the two Fathom-X boards, when the tether
    #: diagnostics extension is installed to report it.
    tether_mbps: float | None = None


class Sampler:
    """Assembles one row per call. Holds the counters and the process group.

    Construct once and call `sample()` on a timer. The first row after
    construction has zeroes in the rate columns -- a rate needs two readings --
    which is why `start()` takes one throwaway reading before the log opens.
    """

    def __init__(self, *, rov_host: str = "192.168.2.2",
                 flight_id: str = "", disk: str = "C:\\"):
        self.rov_host = rov_host
        self.flight_id = flight_id
        self.disk = disk
        self.computer_name = socket.gethostname()
        self.counters = W.Counters()
        self.pinger = W.Pinger(rov_host)
        self.vehicle = VehicleState()
        self.nic = find_rov_interface(rov_host)
        #: The interface that routes to the vehicle and, when that one is a
        #: bridge, the physical adapter underneath it. Worked out once: a
        #: bridge does not change its membership mid-flight, and re-deriving
        #: it every second would cost more than reading it does.
        self._route_index: int | None = None
        self._phy_index: int | None = None
        self._resolve_interfaces()

        self._t0 = time.time()
        self._last_net: tuple[float, object] | None = None
        self._last_disk: tuple[float, object] | None = None
        self._procs: list = []
        self._procs_at = 0.0
        self._cores = (psutil.cpu_count(logical=True) or 1) if psutil else 1
        #: Filled by the slow thread, read by the sample. A plain dict swapped
        #: whole rather than mutated, so a reader always sees one consistent
        #: set of readings and neither side needs a lock.
        self._slow: dict = {}
        self._slow_stop = threading.Event()
        self._slow_thread: threading.Thread | None = None
        #: Last ARP result and whether one has ever been attempted. The second
        #: matters: "no MAC" and "not asked yet" must not write the same cell.
        self._arp: str | None = None
        self._arp_read = False
        self._arp_stop = threading.Event()
        self._arp_thread: threading.Thread | None = None
        #: The GPU engine counters are read once per sample and shared: both
        #: the GPU columns and Cockpit's share of them come out of the same
        #: few hundred instances, and reading them twice was pure waste.
        self._gpu_now: W.GpuReading | None = None
        #: Shared-memory budget for the percentage column. Windows gives an
        #: integrated GPU half of system RAM by convention; without a real
        #: budget to divide by, the percentage would be invented, so it is
        #: left blank when this cannot be established.
        self._gpu_budget_mb: float | None = None
        if psutil is not None:
            try:
                self._gpu_budget_mb = psutil.virtual_memory().total / 2 / 2 ** 20
            except Exception:
                self._gpu_budget_mb = None

    # ---- which interfaces carry the tether ------------------------------

    def _resolve_interfaces(self) -> None:
        """Find the routing interface and, if it is a bridge, its member.

        Never raises and never blocks on the tether. A machine where this
        cannot be worked out leaves the two tether columns blank, which is
        the same contract as every other reading here.
        """
        try:
            ifaces = netdiag.interfaces()
            route = netdiag.routing_interface(self.rov_host, ifaces)
            if route is None:
                return
            self._route_index = route.index
            if not route.is_bridge:
                return
            for iface in netdiag.watched(self.rov_host, ifaces):
                if iface.index != route.index:
                    self._phy_index = iface.index
                    break
        except Exception:
            pass

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Prime the rate counters and start the ping and slow threads."""
        self._t0 = time.time()
        self.counters.collect()
        self.pinger.start()
        if psutil is not None:
            try:
                psutil.cpu_percent(percpu=True)       # prime psutil's own delta
            except Exception:
                pass
        self._refresh_processes()
        self._read_slow()                             # so row one is not blank
        self._slow_stop.clear()
        self._slow_thread = threading.Thread(target=self._slow_loop, daemon=True,
                                             name="utc-slow-readings")
        self._slow_thread.start()
        self._arp_stop.clear()
        self._arp_thread = threading.Thread(target=self._arp_loop, daemon=True,
                                            name="utc-arp")
        self._arp_thread.start()

    def stop(self) -> None:
        self._slow_stop.set()
        self._arp_stop.set()
        if self._slow_thread is not None:
            self._slow_thread.join(timeout=3.0)
            self._slow_thread = None
        # Four seconds, because a resolution already in flight takes three and
        # a thread joined too early is a thread still running at exit.
        if self._arp_thread is not None:
            self._arp_thread.join(timeout=4.0)
            self._arp_thread = None
        self.pinger.stop()
        self.counters.close()

    # ---- the readings that are dear to take ----------------------------

    def _read_slow(self) -> None:
        """Battery draw and adapter link state, off the sample path."""
        slow: dict = {}
        try:
            slow["battery"] = W.read_battery_power()
        except Exception:
            slow["battery"] = {}
        if psutil is not None:
            try:
                if self.nic is None:
                    self.nic = find_rov_interface(self.rov_host)
                if self.nic is not None:
                    slow["nic_stats"] = psutil.net_if_stats().get(self.nic)
            except Exception:
                pass
        self._slow = slow                             # swapped whole

    def _arp_loop(self) -> None:
        """Layer 2, on a thread that is allowed to block.

        Nothing waits on this. The row reads whatever it last left behind,
        and a resolution that takes three seconds delays only the next
        resolution.
        """
        while True:
            try:
                self._arp = netdiag.arp(self.rov_host)
                self._arp_read = True
            except Exception:
                pass
            if self._arp_stop.wait(ARP_REFRESH_S):
                return

    def _slow_loop(self) -> None:
        """The slow readings, on their own thread.

        COM is initialised here because it is per-thread: the battery query
        goes through WMI, and a thread that has not called CoInitialize gets a
        failure rather than a reading. That is exactly what happened when this
        moved off the sample path -- the column went blank and the sample got
        faster, which is the kind of improvement worth noticing.
        """
        try:
            import pythoncom  # noqa: PLC0415
            pythoncom.CoInitialize()
            started_com = True
        except Exception:
            started_com = False
        try:
            while not self._slow_stop.wait(SLOW_REFRESH_S):
                try:
                    self._read_slow()
                except Exception:
                    continue
        finally:
            if started_com:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    # ---- the Cockpit process group -------------------------------------

    def _refresh_processes(self) -> None:
        """Find the Cockpit processes, and any browser showing the vehicle."""
        self._procs_at = time.monotonic()
        if psutil is None:
            self._procs = []
            return
        found = []
        host = self.rov_host.split(":")[0]
        for p in psutil.process_iter(["name", "pid"]):
            try:
                name = (p.info["name"] or "").lower()
                if name in COCKPIT_PROCESSES:
                    found.append(p)
                elif name in COCKPIT_BROWSERS:
                    # Only if this browser was pointed at the vehicle. Reading
                    # a command line can be refused for another user's process,
                    # which simply means it is not ours to count.
                    try:
                        cmd = " ".join(p.cmdline())
                    except Exception:
                        continue
                    if host in cmd or "cockpit" in cmd.lower():
                        found.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for p in found:                       # prime each process's CPU delta
            try:
                p.cpu_percent()
            except Exception:
                pass
        self._procs = found

    def _pids(self) -> set[int]:
        """The Cockpit group's process ids, for attributing GPU work.

        Taken from the cached list rather than from a fresh walk: the GPU
        counters are read before the Cockpit columns are assembled, and a
        process that has exited since the last refresh simply contributes
        nothing to the sum.
        """
        return {p.pid for p in self._procs}

    def _cockpit(self) -> dict:
        """CPU, memory, handles and threads for the whole Cockpit group."""
        out = {"running": False, "cpu": None, "ram": None, "count": 0,
               "handles": None, "threads": None, "gpu": None}
        if psutil is None:
            return out
        if time.monotonic() - self._procs_at > PROCESS_REFRESH_S:
            self._refresh_processes()
        cpu = ram = handles = threads = 0.0
        alive, pids = 0, set()
        for p in list(self._procs):
            try:
                with p.oneshot():
                    cpu += p.cpu_percent()
                    ram += p.memory_info().rss
                    handles += p.num_handles()
                    threads += p.num_threads()
                    pids.add(p.pid)
                alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        if not alive:
            return out
        out.update(running=True, count=alive,
                   # psutil counts a process at 100% per core it saturates.
                   # Divided by the core count it becomes the same scale as
                   # cpu_usage_pct, which is what makes the two comparable.
                   cpu=_f(cpu / self._cores, 2),
                   ram=_f(ram / 2 ** 20, 1),
                   handles=int(handles), threads=int(threads))
        gpu = self._gpu_now
        if gpu is not None and gpu.by_pid_pct:
            share = sum(v for pid, v in gpu.by_pid_pct.items() if pid in pids)
            out["gpu"] = _f(min(100.0, share), 2)
        return out

    # ---- the row -------------------------------------------------------

    def sample(self) -> dict:
        """One row. Never raises: a broken reading is a blank cell."""
        now = time.time()
        row = dict.fromkeys(COLUMNS)
        row["timestamp_utc"] = datetime.fromtimestamp(
            now, timezone.utc).isoformat(timespec="milliseconds")
        row["elapsed_time_s"] = _f(now - self._t0, 1)
        row["flight_id"] = self.flight_id
        row["computer_name"] = self.computer_name

        self.counters.collect()
        try:
            self._gpu_now = W.read_gpu(self.counters, pids=self._pids())
        except Exception:
            self._gpu_now = None
        for fn in (self._cpu, self._memory, self._gpu, self._storage,
                   self._network, self._tether, self._cockpit_columns,
                   self._power, self._vehicle_columns):
            try:
                fn(row)
            except Exception:
                # One broken group must not cost the other six.
                continue
        return row

    def _cpu(self, row: dict) -> None:
        if psutil is not None:
            per = psutil.cpu_percent(percpu=True)
            if per:
                row["cpu_usage_pct"] = _f(sum(per) / len(per), 1)
                row["cpu_max_core_usage_pct"] = _f(max(per), 1)
        nominal = self.counters.scalar("cpu_nominal_mhz")
        perf = self.counters.scalar("cpu_perf_pct")
        if nominal and perf is not None:
            # `Processor Frequency` is the nominal clock and does not move;
            # `% Processor Performance` is how far above or below it the part
            # is actually running. Their product is the real number, and it is
            # the one that falls when the package is power-limited.
            row["cpu_frequency_mhz"] = _f(nominal * perf / 100.0, 0)
        ceiling = self.counters.scalar("cpu_max_freq_pct")
        row["cpu_max_freq_ceiling_pct"] = _f(ceiling, 0)

        throttle = self.counters.scalar("thermal_throttle")
        passive = self.counters.scalar("thermal_passive_pct")
        if throttle is not None or passive is not None:
            row["cpu_thermal_throttle"] = _b(
                bool(throttle) or (passive is not None and passive < 100))
        if ceiling is not None:
            row["cpu_power_limit_throttle"] = _b(ceiling < 99)
        # cpu_package_temp_c, cpu_max_core_temp_c and cpu_package_power_w stay
        # blank: Windows publishes neither to an unelevated process without a
        # vendor driver. See wincounters.capabilities().

    def _memory(self, row: dict) -> None:
        if psutil is not None:
            vm = psutil.virtual_memory()
            row["ram_used_gb"] = _f(vm.used / 2 ** 30, 2)
            row["ram_available_gb"] = _f(vm.available / 2 ** 30, 2)
            row["ram_usage_pct"] = _f(vm.percent, 1)
        committed = self.counters.scalar("mem_committed_b")
        limit = self.counters.scalar("mem_commit_limit_b")
        row["memory_committed_gb"] = _f(
            None if committed is None else committed / 2 ** 30, 2)
        row["memory_commit_limit_gb"] = _f(
            None if limit is None else limit / 2 ** 30, 2)
        row["pagefile_usage_pct"] = _f(self.counters.scalar("pagefile_pct"), 2)
        row["pages_per_s"] = _f(self.counters.scalar("mem_pages_per_s"), 1)

    def _gpu(self, row: dict) -> None:
        g = self._gpu_now
        if g is None:
            return
        row["gpu_usage_pct"] = _f(g.total_pct, 2)
        row["gpu_video_decode_pct"] = _f(g.video_decode_pct, 2)
        row["gpu_memory_used_mb"] = _f(g.memory_used_mb, 1)
        if g.memory_used_mb is not None and self._gpu_budget_mb:
            row["gpu_memory_usage_pct"] = _f(
                100.0 * g.memory_used_mb / self._gpu_budget_mb, 2)
        # gpu_temp_c, gpu_power_w and gpu_thermal_throttle stay blank on
        # integrated Intel graphics, which publish no such sensors.

    def _storage(self, row: dict) -> None:
        idle = self.counters.scalar("disk_idle_pct")
        if idle is not None:
            row["disk_active_pct"] = _f(max(0.0, 100.0 - idle), 1)
        row["disk_queue_length"] = _f(self.counters.scalar("disk_queue"), 2)
        if psutil is not None:
            try:
                u = psutil.disk_usage(self.disk)
                row["disk_free_gb"] = _f(u.free / 2 ** 30, 2)
                row["disk_free_pct"] = _f(100.0 - u.percent, 1)
            except Exception:
                pass
            io = psutil.disk_io_counters()
            now = time.monotonic()
            if io is not None:
                if self._last_disk is not None:
                    dt = now - self._last_disk[0]
                    prev = self._last_disk[1]
                    if dt > 0:
                        row["disk_read_mb_s"] = _f(
                            (io.read_bytes - prev.read_bytes) / dt / 2 ** 20, 3)
                        row["disk_write_mb_s"] = _f(
                            (io.write_bytes - prev.write_bytes) / dt / 2 ** 20, 3)
                self._last_disk = (now, io)
        # ssd_temp_c needs the storage reliability counters, which Windows
        # refuses to an unelevated caller.

    def _network(self, row: dict) -> None:
        if psutil is None:
            return
        if self.nic is None:
            self.nic = find_rov_interface(self.rov_host)
        nic = self.nic
        if nic is None:
            row["ethernet_connected"] = _b(False)
            return
        stats = self._slow.get("nic_stats")
        if stats is not None:
            row["ethernet_connected"] = _b(bool(stats.isup))
            row["ethernet_link_speed_mbps"] = stats.speed or None
        counters = psutil.net_io_counters(pernic=True).get(nic)
        if counters is None:
            return
        row["network_errors_received"] = counters.errin
        row["network_errors_sent"] = counters.errout
        row["network_packets_discarded"] = counters.dropin + counters.dropout
        now = time.monotonic()
        if self._last_net is not None:
            dt = now - self._last_net[0]
            prev = self._last_net[1]
            if dt > 0:
                rx = (counters.bytes_recv - prev.bytes_recv) * 8 / dt / 1e6
                tx = (counters.bytes_sent - prev.bytes_sent) * 8 / dt / 1e6
                row["network_receive_mbps"] = _f(rx, 3)
                row["network_send_mbps"] = _f(tx, 3)
                if stats is not None and stats.speed:
                    row["network_utilization_pct"] = _f(
                        100.0 * (rx + tx) / stats.speed, 2)
        self._last_net = (now, counters)
        rtt, loss = self.pinger.read()
        row["rov_ping_latency_ms"] = _f(rtt, 1)
        row["rov_packet_loss_pct"] = _f(loss, 1)

    def _tether(self, row: dict) -> None:
        """The link itself, from whichever ends will answer.

        The two carriers are the point. `ethernet_connected` above reads the
        interface that routes to the vehicle, and on a bridged station that is
        a software device which reports itself connected for as long as it
        exists. `phy_carrier` is the adapter under it -- the one with a cable
        in it -- and the two disagreeing is the finding.
        """
        want = [i for i in (self._route_index, self._phy_index) if i is not None]
        if want:
            try:
                found = netdiag.counters(want)
            except Exception:
                found = {}
            route = found.get(self._route_index) if self._route_index else None
            if route is not None:
                row["nic_carrier"] = _b(route.connected)
                row["nic_low_power"] = _b(route.low_power)
            phy = found.get(self._phy_index) if self._phy_index else None
            if phy is not None:
                row["phy_carrier"] = _b(phy.connected)
                row["phy_rx_bytes"] = phy.in_octets
        if self._arp_read:
            row["rov_arp_ok"] = _b(bool(self._arp))
        v = self.vehicle
        row["pi_eth_rx_bytes"] = v.eth_rx_bytes
        row["pi_eth_rx_errors"] = v.eth_rx_errors
        row["tether_link_mbps"] = _f(v.tether_mbps, 1)

    def _cockpit_columns(self, row: dict) -> None:
        c = self._cockpit()
        row["cockpit_running"] = _b(c["running"])
        row["cockpit_cpu_pct"] = c["cpu"]
        row["cockpit_ram_mb"] = c["ram"]
        row["cockpit_gpu_usage_pct"] = c["gpu"]
        row["cockpit_process_count"] = c["count"]
        row["cockpit_handle_count"] = c["handles"]
        row["cockpit_thread_count"] = c["threads"]

    def _power(self, row: dict) -> None:
        if psutil is not None:
            b = psutil.sensors_battery()
            if b is not None:
                row["power_source"] = "AC" if b.power_plugged else "battery"
                row["battery_charge_pct"] = _f(b.percent, 1)
            row["system_uptime_s"] = _f(time.time() - psutil.boot_time(), 0)
        # The discharge rate is a WMI query and dear; the slow thread takes
        # it and this reads whatever it last left behind.
        battery = self._slow.get("battery") or {}
        if battery:
            row["battery_discharge_w"] = _f(battery.get("discharge_w"), 2)
            if row["power_source"] is None:
                online = battery.get("on_mains")
                if online is not None:
                    row["power_source"] = "AC" if online else "battery"
        zone = self.counters.scalar("thermal_zone_k10")
        if zone:
            # The ACPI zone reports tenths of a kelvin. It is the system-board
            # zone, not a core sensor -- it tracks the case rather than the
            # silicon, which is the right thing for a laptop left in the sun
            # and the wrong thing for spotting a busy core.
            row["motherboard_temp_c"] = _f(zone / 10.0 - 273.15, 1)
        # battery_temp_c and fan_speed_rpm stay blank: this chassis publishes
        # neither without Dell Command | Monitor installed.

    def _vehicle_columns(self, row: dict) -> None:
        v = self.vehicle
        row["rov_reachable"] = _b(v.reachable)
        row["rov_armed"] = _b(v.armed)
        row["rov_http_ms"] = _f(v.http_ms, 1)
        row["pi_soc_temp_c"] = _f(v.soc_temp_c, 1)
        row["pi_throttling"] = _b(v.throttling)


# --------------------------------------------------------------------------
#  a ring of recent samples, for the live plot
# --------------------------------------------------------------------------


@dataclass
class History:
    """The last N samples, for drawing. Written by one thread, read by Tk.

    A plain list under a lock rather than anything cleverer: at 1 Hz the
    contention is nil, and the GUI must be able to take a consistent copy
    without stopping the recorder.
    """

    minutes: int = 15
    _rows: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def limit(self) -> int:
        return max(60, self.minutes * 60)

    def add(self, row: dict) -> None:
        with self._lock:
            self._rows.append(row)
            if len(self._rows) > self.limit:
                del self._rows[:len(self._rows) - self.limit]

    def series(self, column: str) -> list[tuple[float, float]]:
        """(elapsed seconds, value) for one column, blanks dropped."""
        with self._lock:
            rows = list(self._rows)
        out = []
        for r in rows:
            t, v = r.get("elapsed_time_s"), r.get(column)
            if t is None or v is None:
                continue
            if isinstance(v, str):
                if v == "TRUE":
                    v = 1.0
                elif v == "FALSE":
                    v = 0.0
                else:
                    continue
            out.append((float(t), float(v)))
        return out

    def latest(self) -> dict:
        with self._lock:
            return dict(self._rows[-1]) if self._rows else {}

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
