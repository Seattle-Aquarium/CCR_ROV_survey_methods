"""
The Windows readings that psutil does not offer, and nothing else.

Everything platform-specific about laptop monitoring is in this one file so
that `laptop.py` above it reads as a list of measurements rather than as a
tour of the Win32 API. Three mechanisms live here:

  * **PDH** (`pdh.dll`) for the performance counters — GPU engines, the ACPI
    thermal zone, disk queue depth, commit charge, and the processor's actual
    clock. Counters are opened once and read on a timer; a sample costs
    microseconds, which is what makes 1 Hz logging free rather than a job.
  * **ICMP** (`iphlpapi.dll`) for the round trip to the vehicle. The IP Helper
    API answers without administrator rights, where a raw socket would not,
    and without spawning `ping.exe` once a second.
  * **WMI**, reluctantly and rarely, for the battery's discharge rate — the
    only reading here with no counter behind it. It is polled on its own slow
    cadence because a WMI query costs tens of milliseconds.

**Nothing here is required.** Every reading is optional and every failure is
silent by design: a laptop with no battery, an integrated GPU with no
temperature sensor, or a Windows build with a counter missing must produce a
row with a blank in it, never an exception that stops a flight being recorded.
`capabilities()` reports what actually answered on this machine, so a blank
column can be explained rather than guessed at.

Counters are added with `PdhAddEnglishCounterW` rather than `PdhAddCounterW`:
counter names are localised, and a laptop set to another display language
would otherwise find none of them.
"""

from __future__ import annotations

import ctypes
import re
import socket
import struct
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
#  PDH — the performance counters
# --------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
#: Without this, a percentage counter is clamped at 100. `% Processor
#: Performance` legitimately exceeds it -- a core in turbo reads 145% -- and
#: clamping would hide exactly the headroom this is measuring.
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD),
                ("doubleValue", ctypes.c_double)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR),
                ("FmtValue", _CounterValue)]


def _load_pdh():
    if not IS_WINDOWS:
        return None
    try:
        dll = ctypes.WinDLL("pdh.dll")
    except OSError:
        return None
    dll.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_void_p)]
    dll.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                          ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
    dll.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    dll.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    dll.PdhGetFormattedCounterValue.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_CounterValue)]
    dll.PdhGetFormattedCounterArrayW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    return dll


_PDH = _load_pdh()


#: Single-instance counters, read as one number each.
#:
#: `% Idle Time` rather than `% Disk Time` for how busy the disk is: on a
#: multi-queue NVMe the latter routinely reads several hundred percent, which
#: is not a percentage of anything a reader would recognise. 100 minus idle is
#: the figure Task Manager shows.
SCALARS = {
    "cpu_perf_pct":        r"\Processor Information(_Total)\% Processor Performance",
    "cpu_max_freq_pct":    r"\Processor Information(_Total)\% of Maximum Frequency",
    "cpu_nominal_mhz":     r"\Processor Information(_Total)\Processor Frequency",
    "thermal_zone_k10":    r"\Thermal Zone Information(*)\High Precision Temperature",
    "thermal_throttle":    r"\Thermal Zone Information(*)\Throttle Reasons",
    "thermal_passive_pct": r"\Thermal Zone Information(*)\% Passive Limit",
    "disk_idle_pct":       r"\PhysicalDisk(_Total)\% Idle Time",
    "disk_queue":          r"\PhysicalDisk(_Total)\Current Disk Queue Length",
    "mem_committed_b":     r"\Memory\Committed Bytes",
    "mem_commit_limit_b":  r"\Memory\Commit Limit",
    "mem_pages_per_s":     r"\Memory\Pages/sec",
    "pagefile_pct":        r"\Paging File(_Total)\% Usage",
}

#: Wildcard counters, read as {instance: value}.
ARRAYS = {
    "gpu_engine":    r"\GPU Engine(*)\Utilization Percentage",
    "gpu_dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
    "gpu_shared":    r"\GPU Adapter Memory(*)\Shared Usage",
}

#: `pid_1628_luid_0x00000000_0x025ACE85_phys_0_eng_0_engtype_3D`
_ENGINE_RE = re.compile(
    r"^pid_(\d+)_luid_([0-9A-Fa-fx]+_[0-9A-Fa-fx]+)_phys_\d+_eng_(\d+)_engtype_(.+)$")


class Counters:
    """One PDH query holding every counter this programme reads.

    Rate counters (bytes/sec, pages/sec, % busy) are differences between two
    collections, so the first `sample()` after construction returns mostly
    zeroes and the ones after it are real. The caller samples on a timer, so
    the spacing looks after itself.
    """

    def __init__(self) -> None:
        self._query = None
        self._scalars: dict[str, ctypes.c_void_p] = {}
        self._arrays: dict[str, ctypes.c_void_p] = {}
        self.missing: list[str] = []
        self._primed = False
        if _PDH is None:
            self.missing = sorted({*SCALARS, *ARRAYS})
            return
        q = ctypes.c_void_p()
        if _PDH.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
            self.missing = sorted({*SCALARS, *ARRAYS})
            return
        self._query = q
        for name, path in SCALARS.items():
            h = ctypes.c_void_p()
            if _PDH.PdhAddEnglishCounterW(q, path, None, ctypes.byref(h)) == 0:
                self._scalars[name] = h
            else:
                self.missing.append(name)
        for name, path in ARRAYS.items():
            h = ctypes.c_void_p()
            if _PDH.PdhAddEnglishCounterW(q, path, None, ctypes.byref(h)) == 0:
                self._arrays[name] = h
            else:
                self.missing.append(name)

    @property
    def available(self) -> bool:
        return self._query is not None

    def collect(self) -> None:
        """Take one reading of every counter. Cheap: this is the whole cost."""
        if self._query is not None:
            _PDH.PdhCollectQueryData(self._query)
            self._primed = True

    def scalar(self, name: str) -> float | None:
        h = self._scalars.get(name)
        if h is None or not self._primed:
            return None
        v = _CounterValue()
        rc = _PDH.PdhGetFormattedCounterValue(
            h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(v))
        return None if rc != 0 else v.doubleValue

    def array(self, name: str) -> dict[str, float]:
        """Every instance of a wildcard counter.

        The two-call sizing dance is PDH's own: ask with no buffer to be told
        how big one must be, then ask again. The status code comes back as a
        signed long, so it is masked before it is compared -- an unmasked
        comparison against PDH_MORE_DATA never matches and the counter looks
        empty, which is exactly what happened the first time this was written.
        """
        h = self._arrays.get(name)
        if h is None or not self._primed:
            return {}
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        rc = _PDH.PdhGetFormattedCounterArrayW(
            h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(size), ctypes.byref(count), None)
        if (rc & 0xFFFFFFFF) != PDH_MORE_DATA or size.value == 0:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        rc = _PDH.PdhGetFormattedCounterArrayW(
            h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
            ctypes.byref(size), ctypes.byref(count), buf)
        if rc != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_CounterItem))
        out: dict[str, float] = {}
        for i in range(count.value):
            name_w = items[i].szName
            if name_w:
                out[name_w] = items[i].FmtValue.doubleValue
        return out

    def close(self) -> None:
        if self._query is not None:
            try:
                _PDH.PdhCloseQuery(self._query)
            except Exception:
                pass
            self._query = None


# --------------------------------------------------------------------------
#  GPU: turning per-process engine counters into two numbers
# --------------------------------------------------------------------------


@dataclass
class GpuReading:
    total_pct: float | None = None
    video_decode_pct: float | None = None
    memory_used_mb: float | None = None
    #: Utilisation attributable to a given set of pids, same units as `total`.
    by_pid_pct: dict[int, float] = field(default_factory=dict)


def read_gpu(counters: Counters, pids: set[int] | None = None) -> GpuReading:
    """Aggregate the per-process GPU engine counters the way Task Manager does.

    Windows reports one counter per process *per engine* -- 3D, VideoDecode,
    VideoProcessing, Copy -- and there are typically a few hundred of them.
    Summing the lot would report several hundred percent on an idle machine,
    because a process sitting on two engines counts twice.

    The convention Task Manager uses, and this follows, is: sum within each
    engine type, then take the busiest engine type. That is a real answer to
    "how loaded is the GPU" and it cannot exceed 100 for a single engine.
    """
    out = GpuReading()
    engines = counters.array("gpu_engine")
    if engines:
        per_type: dict[str, float] = {}
        per_pid: dict[int, float] = {}
        for instance, value in engines.items():
            m = _ENGINE_RE.match(instance)
            if not m:
                continue
            pid, _luid, _eng, engtype = m.groups()
            per_type[engtype] = per_type.get(engtype, 0.0) + value
            if pids and int(pid) in pids:
                per_pid[int(pid)] = per_pid.get(int(pid), 0.0) + value
        if per_type:
            out.total_pct = min(100.0, max(per_type.values()))
            decode = sum(v for k, v in per_type.items()
                         if "videodecode" in k.lower())
            out.video_decode_pct = min(100.0, decode)
        out.by_pid_pct = per_pid

    # One adapter is the real GPU and the others are software renderers that
    # hold a few kilobytes forever. Taking the largest picks the real one
    # without having to identify it by LUID.
    used = 0.0
    for key in ("gpu_dedicated", "gpu_shared"):
        values = counters.array(key)
        if values:
            used += max(values.values())
    if used:
        out.memory_used_mb = used / 2 ** 20
    return out


# --------------------------------------------------------------------------
#  ICMP — the round trip to the vehicle
# --------------------------------------------------------------------------


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [("Address", wintypes.ULONG),
                ("Status", wintypes.ULONG),
                ("RoundTripTime", wintypes.ULONG),
                ("DataSize", wintypes.USHORT),
                ("Reserved", wintypes.USHORT),
                ("Data", ctypes.c_void_p),
                ("Ttl", ctypes.c_ubyte),
                ("Tos", ctypes.c_ubyte),
                ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte),
                ("OptionsData", ctypes.c_void_p)]


def _load_icmp():
    if not IS_WINDOWS:
        return None
    try:
        dll = ctypes.WinDLL("iphlpapi.dll")
    except OSError:
        return None
    dll.IcmpCreateFile.restype = wintypes.HANDLE
    dll.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    dll.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG,
                                 ctypes.c_void_p, wintypes.WORD,
                                 ctypes.c_void_p, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD]
    dll.IcmpSendEcho.restype = wintypes.DWORD
    return dll


_ICMP = _load_icmp()

#: Short enough that a dead tether costs a fifth of a second rather than a
#: second, long enough that a busy Pi answering in 90 ms is never called a
#: loss. Measured round trips to the vehicle on the bench are 2-16 ms.
PING_TIMEOUT_MS = 400
#: How many recent pings the loss figure is computed over. At 1 Hz this is the
#: last half minute, which is long enough to mean something and short enough
#: that a tether coming back is visible while someone is still looking.
PING_WINDOW = 30


#: What `IcmpSendEcho` puts in `Status` when it does not get a reply. The
#: distinction is the point: a timeout is a packet that went out and never
#: came back, and an unreachable is the local stack saying it could not send
#: one at all -- which is what a lost ARP entry or a withdrawn route looks
#: like. A log that records both as "no answer" throws that away.
ICMP_STATUS = {
    0: "reply",
    11001: "buffer too small",
    11002: "destination net unreachable",
    11003: "destination host unreachable",
    11004: "destination protocol unreachable",
    11005: "destination port unreachable",
    11006: "no resources",
    11007: "bad option",
    11008: "hardware error",
    11009: "packet too big",
    11010: "request timed out",
    11011: "bad request",
    11012: "bad route",
    11013: "ttl expired in transit",
    11050: "general failure",
}


class Echo:
    """One ICMP handle, pinged synchronously by whoever owns it.

    The thread and the cadence belong to the caller. `Pinger` wraps this in a
    thread for the 1 Hz row; the fast network trace drives it several times a
    second and records every result, including the failures, which is the
    difference between knowing a link went away and knowing when.
    """

    def __init__(self, host: str = "192.168.2.2"):
        self.host = host
        self._handle = None
        self._addr: int | None = None

    def open(self) -> bool:
        if _ICMP is None:
            return False
        if self._handle is None:
            handle = _ICMP.IcmpCreateFile()
            if not handle or handle == wintypes.HANDLE(-1).value:
                return False
            self._handle = handle
        return self._resolve()

    def _resolve(self) -> bool:
        if self._addr is not None:
            return True
        try:
            ip = socket.gethostbyname(self.host.split(":")[0])
            self._addr = struct.unpack("<I", socket.inet_aton(ip))[0]
            return True
        except OSError:
            return False

    def ping(self, timeout_ms: int | None = None,
             payload: bytes = b"UTC") -> tuple[float | None, int]:
        """(round trip in ms or None, the API's status code).

        A round trip of 0 is a reply under half a millisecond, not a miss --
        the API reports whole milliseconds only.
        """
        if self._handle is None or not self._resolve():
            return None, 11050
        size = ctypes.sizeof(_IcmpEchoReply) + len(payload) + 16
        buf = ctypes.create_string_buffer(size)
        n = _ICMP.IcmpSendEcho(self._handle, self._addr, payload, len(payload),
                               None, buf, size,
                               PING_TIMEOUT_MS if timeout_ms is None else timeout_ms)
        if not n:
            return None, 11010
        reply = ctypes.cast(buf, ctypes.POINTER(_IcmpEchoReply)).contents
        status = int(reply.Status)
        if status != 0:
            return None, status
        return float(reply.RoundTripTime), 0

    def close(self) -> None:
        if self._handle is not None and _ICMP is not None:
            try:
                _ICMP.IcmpCloseHandle(self._handle)
            except Exception:
                pass
        self._handle = None


class Pinger:
    """Round-trip time to one address, sampled on its own thread.

    On its own thread so that a vehicle which has stopped answering cannot
    slow the 1 Hz sampler down: the sampler reads the last result rather than
    waiting for the next one. A lost ping costs 200 ms here and nothing at all
    to the row being written.
    """

    def __init__(self, host: str = "192.168.2.2", period: float = 1.0):
        self.host = host
        self.period = period
        self._handle = None
        self._addr = None
        self._lock = threading.Lock()
        self._recent: deque[float | None] = deque(maxlen=PING_WINDOW)
        self._last: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _resolve(self) -> bool:
        try:
            ip = socket.gethostbyname(self.host.split(":")[0])
            self._addr = struct.unpack("<I", socket.inet_aton(ip))[0]
            return True
        except OSError:
            return False

    def start(self) -> None:
        if _ICMP is None or self._thread is not None:
            return
        handle = _ICMP.IcmpCreateFile()
        if not handle or handle == wintypes.HANDLE(-1).value:
            return
        self._handle = handle
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="utc-ping")
        self._thread.start()

    def _ping_once(self) -> float | None:
        if self._addr is None and not self._resolve():
            return None
        payload = b"UTC"
        size = ctypes.sizeof(_IcmpEchoReply) + len(payload) + 16
        buf = ctypes.create_string_buffer(size)
        n = _ICMP.IcmpSendEcho(self._handle, self._addr, payload, len(payload),
                               None, buf, size, PING_TIMEOUT_MS)
        if not n:
            return None
        reply = ctypes.cast(buf, ctypes.POINTER(_IcmpEchoReply)).contents
        if reply.Status != 0:
            return None
        # The API reports whole milliseconds, so a 0 is a round trip under
        # half a millisecond rather than a missing reading.
        return float(reply.RoundTripTime)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                rtt = self._ping_once()
            except Exception:
                rtt = None
            with self._lock:
                self._last = rtt
                self._recent.append(rtt)
            self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))

    def read(self) -> tuple[float | None, float | None]:
        """(latest round trip in ms, packet loss % over the recent window)."""
        with self._lock:
            if not self._recent:
                return None, None
            lost = sum(1 for r in self._recent if r is None)
            return self._last, 100.0 * lost / len(self._recent)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._handle is not None and _ICMP is not None:
            try:
                _ICMP.IcmpCloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


# --------------------------------------------------------------------------
#  WMI — the battery's discharge rate, and nothing else
# --------------------------------------------------------------------------
#
# `root\WMI\BatteryStatus` is the only place Windows reports how fast the pack
# is draining, in milliwatts. psutil gives the percentage and whether the mains
# are connected, which covers most of it; the draw is what says whether a
# survey will outlast the battery.
#
# It is a WMI query, so it costs tens of milliseconds and is polled on a slow
# cadence rather than every second. A laptop with two packs -- this one has --
# reports an instance each, and they are summed.


def read_battery_power() -> dict:
    """{discharge_w, charge_w, voltage_v, on_mains} or {} if unreadable.

    Shelling out to PowerShell would cost a process per reading; this goes
    through COM in-process. It is wrapped whole because a machine with no
    battery, a locked-down WMI, or a missing provider must return nothing
    rather than raise.
    """
    if not IS_WINDOWS:
        return {}
    try:
        import win32com.client  # noqa: PLC0415
    except ImportError:
        return _read_battery_power_fallback()
    try:
        wmi = win32com.client.GetObject("winmgmts:\\\\.\\root\\WMI")
        rows = wmi.ExecQuery("SELECT * FROM BatteryStatus")
        out = {"discharge_w": 0.0, "charge_w": 0.0, "voltage_v": None,
               "on_mains": None}
        seen = False
        for r in rows:
            seen = True
            out["discharge_w"] += (r.DischargeRate or 0) / 1000.0
            out["charge_w"] += (r.ChargeRate or 0) / 1000.0
            if r.Voltage:
                out["voltage_v"] = (r.Voltage or 0) / 1000.0
            out["on_mains"] = bool(r.PowerOnline)
        return out if seen else {}
    except Exception:
        return {}


def _read_battery_power_fallback() -> dict:
    """Without pywin32 there is no cheap route, so there is no reading.

    Deliberately not a PowerShell subprocess: this is polled for the length of
    a dive, and a process per poll is a real cost for a column that is a
    convenience. The column stays blank and `capabilities()` says why.
    """
    return {}


# --------------------------------------------------------------------------
#  what this machine can actually answer
# --------------------------------------------------------------------------


def capabilities(counters: Counters | None = None) -> dict[str, str]:
    """Which readings work here, and the reason when one does not.

    A blank column in a CSV is only honest if somebody can find out why it is
    blank. This is that answer, written beside the recording and shown on the
    monitoring page.
    """
    out: dict[str, str] = {}
    if not IS_WINDOWS:
        return {"platform": f"not Windows ({sys.platform}); "
                            f"laptop monitoring is Windows-only"}
    c = counters or Counters()
    out["performance counters"] = (
        "available" if c.available else "pdh.dll did not open")
    if c.missing:
        out["counters not on this Windows"] = ", ".join(sorted(c.missing))

    c.collect()
    time.sleep(0.05)
    c.collect()
    zone = c.scalar("thermal_zone_k10")
    out["chassis temperature"] = (
        "ACPI thermal zone" if zone else "no ACPI thermal zone counter")
    out["cpu package temperature"] = (
        "unavailable -- Windows exposes core temperatures only to a signed "
        "driver or an elevated WMI read, and UTC runs as neither")
    gpu = read_gpu(c)
    out["gpu utilisation"] = (
        "per-process engine counters" if gpu.total_pct is not None
        else "no GPU engine counters")
    out["gpu temperature"] = (
        "unavailable -- integrated Intel graphics publish no temperature "
        "sensor to Windows")
    out["fan speed"] = (
        "unavailable -- this Dell publishes no fan sensor without Dell "
        "Command | Monitor installed")
    out["icmp to the vehicle"] = (
        "IP Helper" if _ICMP is not None else "iphlpapi.dll did not open")
    out["battery draw"] = (
        "root\\WMI BatteryStatus" if read_battery_power()
        else "unavailable -- needs pywin32, or this machine has no battery")
    if counters is None:
        c.close()
    return out
