"""
The topside laptop, simulated one row a second -- every Monitoring column.

The healthy values are L342-D's own: the medians and spreads of the 27
laptop-monitor logs from September and October 2026 (six Port of Seattle
survey days and the OTS test flights). A few columns that L342-D leaves blank
-- it publishes no CPU or GPU temperatures, no fan and no SSD sensor -- are
simulated too, from what a laptop like it typically reads, and the Monitoring
page marks them as not on the field laptop (`NOT_ON_FIELD_LAPTOP`).

Each Monitoring group has its own fault list (see `schedule.CATALOG`); during a
fault half-minute the group's fault reshapes the few columns it would really
touch. Temperatures and memory move toward their fault values with a lag, as
they do, rather than jumping; the rest change at once.

Columns the vehicle answers -- reachability, the Pi's temperature and port
counters, the Fathom-X rates -- change only on the cadence the real recorder
reads them at, and are held in between.
"""

from __future__ import annotations

import math
import random
import socket
from collections import deque
from datetime import datetime, timezone

from .. import flightlog
from .. import laptop as L
from .world import SimWorld

#: Readings L342-D cannot publish, filled here so the strips and their colour
#: bands can be seen. The page labels them.
NOT_ON_FIELD_LAPTOP = frozenset({
    "cpu_package_temp_c", "cpu_max_core_temp_c", "cpu_package_power_w",
    "gpu_temp_c", "gpu_power_w", "ssd_temp_c", "battery_temp_c",
    "fan_speed_rpm", "motherboard_temp_c", "pi_eth_carrier_drops",
})

RAM_GB = 31.88
GPU_BUDGET_MB = RAM_GB * 1024 / 2
DISK_GB = 237.3

#: How quickly a column follows its target, in seconds. Unlisted: at once.
LAG_S = {
    "cpu_package_temp_c": 7.0, "cpu_max_core_temp_c": 5.0, "gpu_temp_c": 7.0,
    "ssd_temp_c": 8.0, "battery_temp_c": 9.0, "motherboard_temp_c": 9.0,
    "pi_soc_temp_c": 7.0, "fan_speed_rpm": 3.0, "ram_used_gb": 2.0,
    "memory_committed_gb": 2.0, "gpu_memory_used_mb": 2.0,
    "cockpit_ram_mb": 2.0, "disk_free_gb": 1.0,
}


#: A fault's effects end this far into its 30 s, so the readings that lag --
#: temperatures, memory, anything judged on a mean -- have settled by the time
#: the all-green interval starts.
EFFECT_S = 26.0


def _ramp(p: float, over_s: float = 6.0) -> float:
    """0 → 1 over the first `over_s` seconds of a fault."""
    return max(0.0, min(1.0, p * 30.0 / over_s))


class LaptopModel:
    """Makes `laptop.COLUMNS` rows for a simulated flight."""

    def __init__(self, world: SimWorld, *, flight_id: str = "", seed: int = 11) -> None:
        self.world = world
        self.flight_id = flight_id
        self.rng = random.Random(seed)
        self.computer_name = socket.gethostname()
        self._ou: dict[str, float] = {}
        self._shown: dict[str, float] = {}
        self._t_last: float | None = None
        self._burst_until = 0.0
        self._burst = 0.0
        self._pings: deque = deque(maxlen=30)
        # Cumulative counters, and the values only polled every few seconds.
        self._phy_rx = 3_352_801_301.0
        self._pi_rx = 77_776_975.0
        self._discarded = 4
        self._carrier_drops = 1
        self._last_drop_cycle = -1
        self._held: dict[str, object] = {}
        self._poll_at: dict[str, float] = {}
        self._disk_free = 48.6
        self._cockpit_base = 1150.0
        self._uptime0 = 2.3 * 86_400

    # -- noise ---------------------------------------------------------------

    def _ou_step(self, name: str, mean: float, sd: float, tau: float, dt: float) -> float:
        """An Ornstein-Uhlenbeck wander about `mean`: smooth, never runs away."""
        x = self._ou.get(name, mean)
        a = min(1.0, dt / max(tau, 1e-6))
        x += (mean - x) * a + sd * math.sqrt(2 * a) * self.rng.gauss(0, 1)
        self._ou[name] = x
        return x

    def _lag(self, name: str, target: float, dt: float) -> float:
        tau = LAG_S.get(name)
        if tau is None or name not in self._shown:
            self._shown[name] = target
            return target
        cur = self._shown[name]
        if target < cur:
            tau /= 3.0             # recovering: cooling, freeing, settling
        cur += (target - cur) * min(1.0, dt / tau)
        self._shown[name] = cur
        return cur

    def _due(self, key: str, period: float, t: float) -> bool:
        """True once per `period` of simulation time, for polled readings."""
        at = self._poll_at.get(key)
        if at is None or t - at >= period - 1e-6:
            self._poll_at[key] = t
            return True
        return False

    # -- one row ----------------------------------------------------------------

    def row(self, t: float) -> dict:
        dt = 1.0 if self._t_last is None else max(0.05, min(5.0, t - self._t_last))
        self._t_last = t
        w, rng = self.world, self.rng
        faults = {ch: w.fault(ch, t) for ch in
                  ("CPU", "Memory", "GPU", "Storage", "Network", "Cockpit",
                   "Power", "Tether")}

        def active(channel: str, key: str) -> float | None:
            f, p = faults[channel]
            if f is None or f.key != key or p * 30.0 >= EFFECT_S:
                return None
            return p

        row = dict.fromkeys(L.COLUMNS)
        wall = w.wall(t)
        row["timestamp_utc"] = datetime.fromtimestamp(wall, timezone.utc).isoformat(
            timespec="milliseconds")
        row["elapsed_time_s"] = round(t, 1)
        row["flight_id"] = self.flight_id
        row["computer_name"] = self.computer_name

        # -- CPU ----------------------------------------------------------------
        if t >= self._burst_until and rng.random() < 0.012:
            self._burst_until = t + rng.uniform(2, 5)
            self._burst = rng.uniform(15, 35)
        burst = self._burst if t < self._burst_until else 0.0
        cpu = self._ou_step("cpu", 38.0, 6.5, 6.0, dt) + burst
        core = cpu + 14.0 + self._ou_step("core", 0.0, 4.0, 3.0, dt)
        freq = self._ou_step("freq", 2260.0, 140.0, 4.0, dt) + 5.0 * (cpu - 38.0)
        ceiling = 89.0
        power = 8.0 + 0.17 * cpu + self._ou_step("pkgw", 0.0, 0.9, 3.0, dt)
        pkg_target = 52.0 + 0.36 * cpu + self._ou_step("pkgt", 0.0, 1.1, 20.0, dt)
        core_extra = 5.0 + self._ou_step("coret", 0.0, 0.8, 3.0, dt)
        throttle = False
        if (p := active("CPU", "thermal_throttle")) is not None:
            r = _ramp(p, 10)
            pkg_target, core_extra = 97.0, 3.0
            ceiling = 89.0 - 55.0 * r
            freq = 2200.0 - 1250.0 * r + rng.gauss(0, 40)
            cpu = 60.0 + 30.0 * r + rng.gauss(0, 2.5)
            core = 100.0
            power = 26.0 - 13.0 * r
            throttle = r > 0.5
        elif (p := active("CPU", "runaway_process")) is not None:
            cpu = 96.5 + rng.gauss(0, 1.5)
            core = 100.0
            freq = 2950.0 + rng.gauss(0, 40)
            power = 41.0 + rng.gauss(0, 1.5)
            pkg_target = 89.0
        elif (p := active("CPU", "power_limited")) is not None:
            ceiling = 45.0
            freq = 1100.0 + rng.gauss(0, 35)
            power = 9.5 + rng.gauss(0, 0.3)
            cpu = 72.0 + rng.gauss(0, 4)
            core = min(100.0, cpu + 20)
        pkg = self._lag("cpu_package_temp_c", pkg_target, dt)
        row["cpu_usage_pct"] = round(max(2.0, min(100.0, cpu)), 1)
        row["cpu_max_core_usage_pct"] = round(max(5.0, min(100.0, core)), 1)
        row["cpu_frequency_mhz"] = round(max(700.0, min(3200.0, freq)))
        row["cpu_max_freq_ceiling_pct"] = round(ceiling)
        row["cpu_package_temp_c"] = round(pkg, 1)
        row["cpu_max_core_temp_c"] = round(min(100.0, self._lag(
            "cpu_max_core_temp_c", pkg + core_extra, dt)), 1)
        row["cpu_package_power_w"] = round(max(2.0, power), 2)
        row["cpu_thermal_throttle"] = "TRUE" if throttle else "FALSE"
        row["cpu_power_limit_throttle"] = "TRUE" if ceiling < 99 else "FALSE"

        # -- memory -------------------------------------------------------------
        used_t = self._ou_step("ram", 10.9, 0.45, 120.0, dt)
        commit_extra = -0.15 + self._ou_step("commit", 0.0, 0.15, 60.0, dt)
        limit = 33.88
        pagefile = max(0.0, self._ou_step("pagefile", 2.4, 0.5, 300.0, dt))
        pages = 0.0 if rng.random() < 0.82 else rng.expovariate(1 / 25.0)
        if rng.random() < 0.004:
            pages = rng.uniform(300, 1200)
        if (p := active("Memory", "memory_leak")) is not None:
            used_t = 10.9 + 17.5 * _ramp(p, 24)
            commit_extra = 2.6 * _ramp(p, 24)
        elif (p := active("Memory", "paging_storm")) is not None:
            used_t = 29.4
            pages = rng.uniform(2400, 7200)
            pagefile = 31.0 + rng.gauss(0, 0.5)
        elif (p := active("Memory", "commit_limit_grew")) is not None:
            used_t = 21.0 + 3.0 * _ramp(p, 12)
            commit_extra = 11.5 * _ramp(p, 12)
            if p * 30 >= 13:
                limit = 41.88
                commit_extra = 12.6
                pagefile = 18.0 + rng.gauss(0, 0.4)
        used = self._lag("ram_used_gb", used_t, dt)
        committed = self._lag("memory_committed_gb", used + commit_extra, dt)
        row["ram_used_gb"] = round(used, 2)
        row["ram_available_gb"] = round(max(0.2, RAM_GB - used), 2)
        row["ram_usage_pct"] = round(100.0 * used / RAM_GB, 1)
        row["memory_committed_gb"] = round(max(1.0, committed), 2)
        row["memory_commit_limit_gb"] = limit
        row["pagefile_usage_pct"] = round(pagefile, 2)
        row["pages_per_s"] = round(pages, 1)

        # -- GPU ----------------------------------------------------------------
        gpu = self._ou_step("gpu", 43.0, 4.5, 5.0, dt)
        decode = 7.1 + rng.gauss(0, 0.2)
        gmem_t = self._ou_step("gmem", 355.0, 22.0, 200.0, dt)
        gtemp_t = pkg - 3.0 + rng.gauss(0, 0.5)
        gpow = 3.8 + 0.035 * gpu + rng.gauss(0, 0.25)
        if (p := active("GPU", "decoder_saturated")) is not None:
            decode = 88.0 + rng.gauss(0, 2.5)
            gpu = 96.0 + rng.gauss(0, 1.5)
        elif (p := active("GPU", "gpu_memory_leak")) is not None:
            gmem_t = 355.0 + 3300.0 * _ramp(p, 24)
        elif (p := active("GPU", "gpu_hot")) is not None:
            gtemp_t, gpu = 94.0, 88.0 + rng.gauss(0, 2)
            gpow = 19.0 + rng.gauss(0, 0.6)
        gmem = self._lag("gpu_memory_used_mb", gmem_t, dt)
        row["gpu_usage_pct"] = round(max(0.0, min(100.0, gpu)), 2)
        row["gpu_video_decode_pct"] = round(max(0.0, min(100.0, decode)), 2)
        row["gpu_memory_used_mb"] = round(gmem, 1)
        row["gpu_memory_usage_pct"] = round(100.0 * gmem / GPU_BUDGET_MB, 2)
        row["gpu_temp_c"] = round(self._lag("gpu_temp_c", gtemp_t, dt), 1)
        row["gpu_power_w"] = round(max(0.5, gpow), 2)
        row["gpu_thermal_throttle"] = "FALSE"

        # -- storage --------------------------------------------------------------
        active_pct = 0.6 + rng.expovariate(1 / 0.6)
        if rng.random() < 0.01:
            active_pct += rng.uniform(5, 25)
        read = 0.0 if rng.random() < 0.9 else rng.uniform(0.05, 0.5)
        write = 0.12 + rng.expovariate(1 / 0.12)
        queue = 1.0 if rng.random() < 0.01 else 0.0
        self._disk_free -= 0.15 / 1024 * dt                 # the recorders
        free_t = self._disk_free
        ssd_t = 39.0 + 0.05 * active_pct + self._ou_step("ssdt", 0.0, 0.5, 60.0, dt)
        if (p := active("Storage", "disk_almost_full")) is not None:
            free_t = self._disk_free - (self._disk_free - 6.5) * _ramp(p, 25)
            write = 160.0 + rng.gauss(0, 15)
            active_pct = 70.0 + rng.gauss(0, 5)
            queue = float(rng.randint(1, 3))
        elif (p := active("Storage", "background_writer")) is not None:
            write = 260.0 + rng.gauss(0, 25)
            read = 35.0 + rng.gauss(0, 6)
            active_pct = 97.0 + rng.gauss(0, 1.5)
            queue = float(rng.randint(9, 16))
        elif (p := active("Storage", "ssd_hot")) is not None:
            ssd_t = 75.0
            active_pct = 55.0 + rng.gauss(0, 5)
            write = 80.0 + rng.gauss(0, 8)
        free = self._lag("disk_free_gb", free_t, dt)
        row["disk_active_pct"] = round(max(0.0, min(100.0, active_pct)), 1)
        row["disk_read_mb_s"] = round(max(0.0, read), 3)
        row["disk_write_mb_s"] = round(max(0.0, write), 3)
        row["disk_queue_length"] = round(queue, 2)
        row["disk_free_gb"] = round(free, 2)
        row["disk_free_pct"] = round(100.0 * free / DISK_GB, 1)
        row["ssd_temp_c"] = round(self._lag("ssd_temp_c", ssd_t, dt), 1)

        # -- the vehicle side, read on the recorder's own cadences --------------
        silent = w.pi_silent(t)
        armed = not w.disarmed(t)

        # -- network ----------------------------------------------------------------
        rx = max(0.0, self._ou_step("rx", 12.2, 1.1, 2.0, dt))
        tx = max(0.02, self._ou_step("tx", 0.14, 0.05, 2.0, dt))
        ping = 2.0 + rng.lognormvariate(math.log(2.0), 0.5)
        lost = rng.random() < 0.002
        http = rng.lognormvariate(math.log(65.0), 0.33)
        if (p := active("Network", "tether_congestion")) is not None:
            ping = rng.uniform(80, 320)
            # Losses early on: the loss figure covers the last 30 pings, and
            # it must have drained by the time the board goes green.
            lost = p * 30 < 10 and rng.random() < 0.45
            http = 1300.0 + rng.gauss(0, 250)
        elif (p := active("Network", "video_stopped")) is not None:
            rx = 0.25 + rng.gauss(0, 0.04)
        elif (p := active("Network", "packets_discarded")) is not None:
            self._discarded += rng.randint(3, 9)
        elif (p := active("Network", "link_saturated")) is not None:
            rx = 86.0 + rng.gauss(0, 3)
            tx = 5.5 + rng.gauss(0, 0.5)
            ping = rng.uniform(40, 70)
        if silent:
            lost = True
        self._pings.append(lost)
        row["network_receive_mbps"] = round(rx, 3)
        row["network_send_mbps"] = round(tx, 3)
        row["network_utilization_pct"] = round(100.0 * (rx + tx) / 100.0, 2)
        row["network_errors_received"] = 0
        row["network_errors_sent"] = 0
        row["network_packets_discarded"] = self._discarded
        row["ethernet_connected"] = "TRUE"
        row["ethernet_link_speed_mbps"] = 100
        row["rov_ping_latency_ms"] = None if lost else float(round(ping))
        row["rov_packet_loss_pct"] = round(100.0 * sum(self._pings) / len(self._pings), 1)

        # Reachability and the Pi's answer time: every ARM_POLL_S.
        if self._due("arm", flightlog.ARM_POLL_S, t):
            self._held["rov_reachable"] = "FALSE" if silent else "TRUE"
            self._held["rov_http_ms"] = None if silent else round(http, 1)
            if not silent:
                self._held["rov_armed"] = "TRUE" if armed else "FALSE"
        row["rov_reachable"] = self._held.get("rov_reachable", "TRUE")
        row["rov_armed"] = self._held.get("rov_armed", "TRUE")
        row["rov_http_ms"] = self._held.get("rov_http_ms")

        # -- Cockpit ------------------------------------------------------------------
        self._cockpit_base += 0.02 * dt
        c_cpu = self._ou_step("ccpu", 20.5, 2.8, 4.0, dt)
        c_ram_t = self._cockpit_base + self._ou_step("cram", 0.0, 18.0, 30.0, dt)
        c_gpu = self._ou_step("cgpu", 38.0, 2.6, 4.0, dt)
        procs = 4
        handles = 2795.0 + self._ou_step("hand", 0.0, 14.0, 60.0, dt)
        threads = 108.0 + self._ou_step("thr", 0.0, 2.0, 30.0, dt)
        if (p := active("Cockpit", "cockpit_leak")) is not None:
            r = _ramp(p, 25)
            c_ram_t = self._cockpit_base + 3500.0 * r
            handles = 2800.0 + 9800.0 * r
            threads = 108.0 + 160.0 * r
        elif (p := active("Cockpit", "gpu_process_crashed")) is not None:
            if p * 30 >= 2:
                procs, c_gpu, c_cpu, c_ram_t = 2, 0.0, 6.0 + rng.gauss(0, 1), 650.0
                handles, threads = 1500.0, 61.0
        elif (p := active("Cockpit", "renderer_spinning")) is not None:
            c_cpu = 78.0 + rng.gauss(0, 3.5)
            c_gpu = 88.0 + rng.gauss(0, 2.5)
        row["cockpit_running"] = "TRUE"
        row["cockpit_cpu_pct"] = round(max(0.0, c_cpu), 2)
        row["cockpit_ram_mb"] = round(self._lag("cockpit_ram_mb", c_ram_t, dt), 1)
        row["cockpit_gpu_usage_pct"] = round(max(0.0, min(100.0, c_gpu)), 2)
        row["cockpit_process_count"] = procs
        row["cockpit_handle_count"] = int(round(handles))
        row["cockpit_thread_count"] = int(round(threads))

        # -- power ----------------------------------------------------------------------
        source, charge, discharge = "AC", 100.0, 0.0
        batt_t = 32.5 + self._ou_step("batt", 0.0, 0.3, 120.0, dt)
        board_t = 37.0 + 0.08 * (pkg - 60.0) + self._ou_step("board", 0.0, 0.3, 90.0, dt)
        fan_t = 2600.0 + 45.0 * (pkg - 60.0) + rng.gauss(0, 50)
        pi_t = 61.0 + self._ou_step("pi", 0.0, 1.4, 60.0, dt)
        if (p := active("Power", "charger_failing")) is not None:
            discharge = 18.0 + rng.gauss(0, 1.5)
            charge = 100.0 - 4.0 * p
        elif (p := active("Power", "battery_draining")) is not None:
            source = "battery"
            discharge = 47.0 + rng.gauss(0, 1.5)
            charge = 38.0 - 21.0 * p
        elif (p := active("Power", "pi_overheating")) is not None:
            pi_t = 84.0
        elif (p := active("Power", "laptop_in_sun")) is not None:
            batt_t, board_t, fan_t = 53.0, 72.0, 6100.0 + rng.gauss(0, 60)
        row["power_source"] = source
        row["battery_charge_pct"] = round(charge, 1)
        if self._due("battery", L.SLOW_REFRESH_S, t) or "battery_discharge_w" not in self._held:
            self._held["battery_discharge_w"] = round(max(0.0, discharge), 2)
        row["battery_discharge_w"] = self._held["battery_discharge_w"]
        row["battery_temp_c"] = round(self._lag("battery_temp_c", batt_t, dt), 1)
        row["fan_speed_rpm"] = int(round(max(0.0, self._lag("fan_speed_rpm", fan_t, dt))))
        row["motherboard_temp_c"] = round(self._lag("motherboard_temp_c", board_t, dt), 1)
        row["system_uptime_s"] = round(self._uptime0 + t)
        pi_now = self._lag("pi_soc_temp_c", pi_t, dt)
        if self._due("pi", flightlog.PI_POLL_S, t) and not silent:
            self._held["pi_soc_temp_c"] = round(pi_now, 1)
            self._held["pi_throttling"] = "TRUE" if pi_now >= 80.0 else "FALSE"
        row["pi_soc_temp_c"] = self._held.get("pi_soc_temp_c", round(pi_now, 1))
        row["pi_throttling"] = self._held.get("pi_throttling", "FALSE")

        # -- tether ------------------------------------------------------------------------
        nic_low = "FALSE"
        arp = "TRUE"
        pi_rx_flow = True
        carrier = "TRUE"
        # The pair renegotiates now and then, a megabit or two, not every read.
        if "tx_rate" not in self._held or rng.random() < 0.08:
            self._held["tx_rate"] = 127.0 + rng.choice((-1, 0, 0, 1))
            self._held["rx_rate"] = 128.0 + rng.choice((-1, 0, 1, 2))
        tx_rate, rx_rate = self._held["tx_rate"], self._held["rx_rate"]
        if (p := active("Tether", "pi_port_dropped")) is not None:
            into = p * 30
            cyc = w.schedule.phase(t).cycle
            if into >= 1 and self._last_drop_cycle != cyc:
                self._carrier_drops += 1
                self._last_drop_cycle = cyc
            if 4 <= into < 26:
                pi_rx_flow = False
            if 5 <= into < 17:
                arp, carrier = "FALSE", "FALSE"
        elif (p := active("Tether", "fathom_degraded")) is not None:
            tx_rate, rx_rate = 31.0 + rng.choice((-1, 0, 1)), 66.0 + rng.choice((-1, 0, 1))
        elif (p := active("Tether", "adapter_asleep")) is not None:
            nic_low = "TRUE"
        elif (p := active("Tether", "pi_interface_restarted")) is not None:
            if p * 30 >= 3 and self._held.get("pi_restart_cycle") != w.schedule.phase(t).cycle:
                self._held["pi_restart_cycle"] = w.schedule.phase(t).cycle
                self._pi_rx = 41_000.0
        self._phy_rx += rx * 1e6 / 8 * dt
        row["nic_carrier"] = "TRUE"
        row["nic_low_power"] = nic_low
        row["phy_carrier"] = "TRUE"
        row["phy_rx_bytes"] = int(self._phy_rx)
        if self._due("arp", L.ARP_REFRESH_S, t) or "rov_arp_ok" not in self._held \
                or arp == "FALSE":
            self._held["rov_arp_ok"] = arp
        row["rov_arp_ok"] = self._held["rov_arp_ok"]
        if self._due("tether", flightlog.TETHER_POLL_S, t) and not silent:
            if pi_rx_flow:
                self._pi_rx += (tx * 1e6 / 8 + 4200.0) * flightlog.TETHER_POLL_S
            self._held.update(
                pi_eth_rx_bytes=int(self._pi_rx), pi_eth_rx_errors=0,
                pi_eth_carrier=carrier, pi_eth_carrier_drops=self._carrier_drops,
                tether_tx_mbps=tx_rate, tether_rx_mbps=rx_rate,
                tether_link_mbps=min(tx_rate, rx_rate), tether_remote_seen="TRUE")
        for key in ("pi_eth_rx_bytes", "pi_eth_rx_errors", "pi_eth_carrier",
                    "pi_eth_carrier_drops", "tether_tx_mbps", "tether_rx_mbps",
                    "tether_link_mbps", "tether_remote_seen"):
            row[key] = self._held.get(key)
        return row
