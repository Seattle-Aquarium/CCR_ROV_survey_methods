"""
What the topside network is actually doing, adapter by adapter.

`wincounters` measures the one interface that routes to the vehicle. That was
enough to prove the tether had gone quiet and not enough to say *where*, and
the gap is specific: on this station the vehicle is reached through a Windows
network bridge, and `psutil` reports the bridge miniport. A bridge miniport is
a software device. It says "connected, 100 Mbps" for as long as it exists,
whatever the Realtek adapter underneath it is doing, so a link that has
physically dropped and a bridge that has stopped forwarding produce the same
healthy-looking row.

This module reads what that row cannot:

  * **Every** interface, not only the routing one, from one `GetIfTable2`
    call -- so the bridge and the adapter under it are measured side by side.
    If the member is receiving while the bridge is not, the bridge is the
    fault, and that is a two-column comparison rather than an argument.
  * **`MediaConnectState`**, the carrier as the driver reports it, which is
    the reading `isup` is not.
  * **The NDIS status flags**, including `LowPower` and `Paused`. A laptop on
    battery that powers its network adapter down leaves a trace here and
    nowhere else.
  * **`InDiscards` and `InErrors` per adapter**, which distinguish a link
    dropping frames from a link delivering none.
  * **ARP**, because a resolver that has lost the vehicle's MAC address is a
    layer-2 failure and looks identical to a layer-3 one from a ping.
  * **The adapter's own settings** -- Energy Efficient Ethernet, Green
    Ethernet, selective suspend, and whether Windows is allowed to switch the
    device off to save power -- read from the registry, because the question
    "was power management on?" cannot be answered after the fact from a log
    that never recorded it.

Everything here is a read. Nothing is configured, and nothing is required:
a machine that cannot answer one of these produces a blank rather than an
exception, on the same principle as the rest of the monitor.

The units are the ones the API uses -- octets, packets, whole milliseconds --
and the conversions are left to whoever reads the file, because a rate
computed at the point of measurement hides the counter it came from.
"""

from __future__ import annotations

import ctypes
import socket
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:                                   # pragma: no cover
    psutil = None                                     # type: ignore

IS_WINDOWS = sys.platform == "win32"

#: The registry class that holds every network adapter's driver settings.
#: Stable across every Windows since 2000, which is why it can be a constant.
NET_CLASS_KEY = (r"SYSTEM\CurrentControlSet\Control\Class"
                 r"\{4D36E972-E325-11CE-BFC1-08002BE10318}")

#: Driver settings worth reading back, and what each one is called when it is
#: shown to a person. The names with a leading asterisk are the standardised
#: NDIS keywords; the others are vendor-specific and simply absent on an
#: adapter that does not have them.
#:
#: `PnPCapabilities` is the important one and the least obvious. It is the
#: "Allow the computer to turn off this device to save power" checkbox, stored
#: inverted: bit 0x18 set means Windows has been *forbidden* from powering the
#: adapter down. Absent or zero means it is allowed to, which on a laptop
#: running on battery is exactly the configuration under suspicion.
ADAPTER_SETTINGS: dict[str, str] = {
    "PnPCapabilities": "power management",
    "*EEE": "energy efficient ethernet",
    "EnableGreenEthernet": "green ethernet",
    "*SelectiveSuspend": "selective suspend",
    "*SpeedDuplex": "speed and duplex",
    "*FlowControl": "flow control",
    "*InterruptModeration": "interrupt moderation",
    "*JumboPacket": "jumbo packet",
    "WakeOnLink": "wake on link",
    "*WakeOnMagicPacket": "wake on magic packet",
    "AutoDisableGigabit": "auto disable gigabit",
    "AdvancedEEE": "advanced eee",
    "PowerSavingMode": "power saving mode",
    "ULPMode": "ultra low power mode",
    "S5WakeOnLan": "wake on lan from S5",
}

#: How `OperStatus` and `MediaConnectState` read in a report. The numbers are
#: the IF_OPER_STATUS and NET_IF_MEDIA_CONNECT_STATE enumerations.
OPER_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown",
               5: "dormant", 6: "not present", 7: "lower layer down"}
MEDIA_STATE = {0: "unknown", 1: "connected", 2: "disconnected"}

#: The NDIS flags packed into one byte of MIB_IF_ROW2, lowest bit first.
#: `low power` and `paused` are the two this module exists to surface.
IF_FLAGS = ("hardware", "filter", "connector present", "not authenticated",
            "not media connected", "paused", "low power", "endpoint")


# --------------------------------------------------------------------------
#  GetIfTable2 -- every interface, with the readings psutil drops
# --------------------------------------------------------------------------

IF_MAX_STRING_SIZE = 256
IF_MAX_PHYS_ADDRESS_LENGTH = 32


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self) -> str:
        tail = "".join(f"{b:02X}" for b in self.Data4)
        return (f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
                f"{tail[:4]}-{tail[4:]}}}")


class _MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", _GUID),
        ("Alias", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("Description", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        # Eight one-bit flags in a single byte. Declared whole and unpacked in
        # Python: ctypes bitfields on a BOOLEAN are a portability argument
        # nobody needs to have.
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG),
                ("Table", _MIB_IF_ROW2 * 1)]


def _load_iphlpapi():
    if not IS_WINDOWS:
        return None
    try:
        dll = ctypes.WinDLL("iphlpapi.dll")
    except OSError:                                   # pragma: no cover
        return None
    dll.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(_MIB_IF_TABLE2))]
    dll.GetIfTable2.restype = wintypes.DWORD
    dll.FreeMibTable.argtypes = [ctypes.c_void_p]
    dll.FreeMibTable.restype = None
    dll.SendARP.argtypes = [wintypes.ULONG, wintypes.ULONG,
                            ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
    dll.SendARP.restype = wintypes.DWORD
    dll.GetBestInterfaceEx.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.DWORD)]
    dll.GetBestInterfaceEx.restype = wintypes.DWORD
    dll.GetAdaptersAddresses.argtypes = [wintypes.ULONG, wintypes.ULONG,
                                         ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.ULONG)]
    dll.GetAdaptersAddresses.restype = wintypes.ULONG
    return dll


_IP = _load_iphlpapi()


@dataclass
class Iface:
    """One network interface, as the driver and NDIS describe it.

    Counters are cumulative since the adapter came up, not rates. Whoever
    reads two of these takes the difference; keeping the raw counter means a
    gap in sampling is visible as a jump rather than hidden as a plausible
    average.
    """

    index: int = 0
    guid: str = ""
    alias: str = ""
    description: str = ""
    mac: str = ""
    mtu: int = 0
    type: int = 0
    oper_status: int = 0
    media_state: int = 0
    admin_status: int = 0
    rx_link_bps: int = 0
    tx_link_bps: int = 0
    flags: int = 0
    in_octets: int = 0
    out_octets: int = 0
    in_ucast: int = 0
    out_ucast: int = 0
    in_nucast: int = 0
    in_discards: int = 0
    in_errors: int = 0
    out_discards: int = 0
    out_errors: int = 0
    out_qlen: int = 0
    #: Filled in by `interfaces()` from psutil, because GetIfTable2 does not
    #: carry addresses and the subnet is how the vehicle's adapter is found.
    ipv4: list[tuple[str, str]] = field(default_factory=list)

    # ---- the readings that are a yes or a no -----------------------------

    @property
    def connected(self) -> bool | None:
        """Carrier, as the driver reports it. None when it will not say."""
        if self.media_state == 1:
            return True
        if self.media_state == 2:
            return False
        return None

    @property
    def up(self) -> bool:
        return self.oper_status == 1

    @property
    def hardware(self) -> bool:
        """A real adapter rather than a bridge, tunnel or loopback."""
        return bool(self.flags & 0x01)

    @property
    def low_power(self) -> bool:
        return bool(self.flags & 0x40)

    @property
    def paused(self) -> bool:
        return bool(self.flags & 0x20)

    @property
    def flag_names(self) -> list[str]:
        return [n for i, n in enumerate(IF_FLAGS) if self.flags & (1 << i)]

    @property
    def speed_mbps(self) -> float | None:
        speed = self.rx_link_bps or self.tx_link_bps
        return round(speed / 1e6, 1) if speed else None

    @property
    def is_bridge(self) -> bool:
        """A Windows network bridge miniport, by the name it presents.

        Detected by description rather than by asking the bridge service,
        because the service's interface is not documented and this only has
        to be right enough to label a column.
        """
        text = f"{self.description} {self.alias}".lower()
        return ("mac bridge" in text or "network bridge" in text
                or "multiplexor" in text)


def _mac(raw, length: int) -> str:
    return ":".join(f"{raw[i]:02X}" for i in range(min(length, 8)))


def _row_to_iface(r) -> Iface:
    return Iface(
        index=int(r.InterfaceIndex),
        guid=str(r.InterfaceGuid),
        alias=r.Alias,
        description=r.Description,
        mac=_mac(r.PhysicalAddress, int(r.PhysicalAddressLength)),
        mtu=int(r.Mtu),
        type=int(r.Type),
        oper_status=int(r.OperStatus),
        media_state=int(r.MediaConnectState),
        admin_status=int(r.AdminStatus),
        rx_link_bps=int(r.ReceiveLinkSpeed),
        tx_link_bps=int(r.TransmitLinkSpeed),
        flags=int(r.InterfaceAndOperStatusFlags),
        in_octets=int(r.InOctets),
        out_octets=int(r.OutOctets),
        in_ucast=int(r.InUcastPkts),
        out_ucast=int(r.OutUcastPkts),
        in_nucast=int(r.InNUcastPkts),
        in_discards=int(r.InDiscards),
        in_errors=int(r.InErrors),
        out_discards=int(r.OutDiscards),
        out_errors=int(r.OutErrors),
        out_qlen=int(r.OutQLen),
    )


def _walk_table(want: set[int] | None = None):
    """Yield MIB_IF_ROW2 rows, optionally only the wanted interface indices.

    The filter is applied before the row is turned into an `Iface`, which is
    the whole cost: this machine carries 55 interfaces and building all of
    them takes six milliseconds, against a fifth of that for the two or three
    that the tether actually runs on.
    """
    if _IP is None:
        return
    ptr = ctypes.POINTER(_MIB_IF_TABLE2)()
    if _IP.GetIfTable2(ctypes.byref(ptr)) != 0 or not ptr:
        return
    try:
        table = ptr.contents
        n = int(table.NumEntries)
        base = ctypes.addressof(table) + _MIB_IF_TABLE2.Table.offset
        rows = (_MIB_IF_ROW2 * n).from_address(base)
        for r in rows:
            if want is None or int(r.InterfaceIndex) in want:
                yield r
    finally:
        _IP.FreeMibTable(ptr)


def interfaces() -> list[Iface]:
    """Every interface on this machine, with carrier state and counters.

    Costs a few milliseconds, almost all of it building the objects rather
    than in the API. Use `counters()` on a sampling path; this is for the
    report and for deciding which interfaces are worth watching. Returns an
    empty list rather than raising on a machine that cannot answer.
    """
    out = [_row_to_iface(r) for r in _walk_table()]
    _attach_addresses(out)
    return out


def counters(indices) -> dict[int, Iface]:
    """Counters and carrier for a few named interfaces, as {index: Iface}.

    The sampling-path version of `interfaces()`: one API call, no address
    lookup, and only the rows asked for turned into objects. Addresses are
    not filled in, because they do not change over a flight and the caller
    that wants them has already read them once.
    """
    want = set(indices)
    if not want:
        return {}
    return {int(r.InterfaceIndex): _row_to_iface(r)
            for r in _walk_table(want)}


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p),
                ("iSockaddrLength", ctypes.c_int)]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG),
    ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


#: Declared only as far as FriendlyName. The structure continues well past
#: it, but ctypes only needs the leading fields to reach the ones being read,
#: and every field declared is a field that has to stay right across Windows
#: versions.
_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
]

GAA_FLAG_SKIP_ANYCAST = 0x02
GAA_FLAG_SKIP_MULTICAST = 0x04
GAA_FLAG_SKIP_DNS_SERVER = 0x08
AF_UNSPEC = 0


def adapter_addresses() -> dict[int, list[tuple[str, str]]]:
    """IPv4 addresses and masks per interface index.

    `GetIfTable2` carries counters and carrier state but no addresses, and the
    address is how the vehicle's adapter is identified. Read here rather than
    from psutil so that this module answers on a machine where psutil is not
    installed -- the whole point of it is to work when something else has not.

    Keyed by interface index rather than by name: two adapters can share a
    friendly name across a rename, and the index is what `GetIfTable2` gives.
    """
    if _IP is None:
        return {}
    flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
             | GAA_FLAG_SKIP_DNS_SERVER)
    size = wintypes.ULONG(15 * 1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        rc = _IP.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf,
                                      ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:                                 # ERROR_BUFFER_OVERFLOW
            return {}
    else:                                             # pragma: no cover
        return {}
    out: dict[int, list[tuple[str, str]]] = {}
    node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while node:
        entry = node.contents
        found: list[tuple[str, str]] = []
        addr = entry.FirstUnicastAddress
        while addr:
            unicast = addr.contents
            sa = unicast.Address.lpSockaddr
            if sa:
                family = ctypes.cast(
                    sa, ctypes.POINTER(wintypes.USHORT)).contents.value
                if family == socket.AF_INET:
                    raw = ctypes.string_at(sa + 4, 4)
                    prefix = int(unicast.OnLinkPrefixLength)
                    found.append((socket.inet_ntoa(raw), _mask(prefix)))
            addr = unicast.Next
        if found:
            out[int(entry.IfIndex)] = found
        node = entry.Next
    return out


def _mask(prefix: int) -> str:
    """A prefix length as a dotted mask, because that is how the CSV reads."""
    prefix = max(0, min(32, prefix))
    bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
    return socket.inet_ntoa(struct.pack("!I", bits))


def _attach_addresses(ifaces: list[Iface]) -> None:
    """Put each interface's IPv4 addresses on it, joined by index."""
    try:
        addrs = adapter_addresses()
    except Exception:
        return
    for iface in ifaces:
        iface.ipv4 = list(addrs.get(iface.index, ()))


# --------------------------------------------------------------------------
#  which interfaces matter, and what the bridge is made of
# --------------------------------------------------------------------------


def _ip_to_int(addr: str) -> int | None:
    try:
        return struct.unpack("!I", socket.inet_aton(addr))[0]
    except OSError:
        return None


def routing_interface(host: str = "192.168.2.2",
                      ifaces: list[Iface] | None = None) -> Iface | None:
    """The interface whose own subnet contains the vehicle."""
    target = _ip_to_int(host.split(":")[0])
    if target is None:
        return None
    for iface in ifaces if ifaces is not None else interfaces():
        for addr, mask in iface.ipv4:
            ip, m = _ip_to_int(addr), _ip_to_int(mask)
            if ip is None or m is None:
                continue
            if (ip & m) == (target & m):
                return iface
    return None


def best_interface_index(host: str = "192.168.2.2") -> int | None:
    """Which interface Windows says it would route to the vehicle over.

    Worth recording alongside the subnet match: they agree until something
    changes a route mid-flight, and the day they disagree is the day the
    throughput column was measuring the wrong adapter.
    """
    if _IP is None:
        return None
    target = host.split(":")[0]
    try:
        packed = socket.inet_aton(target)
    except OSError:
        return None
    sockaddr = ctypes.create_string_buffer(16)
    ctypes.memmove(sockaddr, struct.pack("<H", socket.AF_INET), 2)
    ctypes.memmove(ctypes.byref(sockaddr, 4), packed, 4)
    index = wintypes.DWORD()
    if _IP.GetBestInterfaceEx(sockaddr, ctypes.byref(index)) != 0:
        return None
    return int(index.value)


def watched(host: str = "192.168.2.2",
            ifaces: list[Iface] | None = None) -> list[Iface]:
    """The interfaces worth logging at speed: the tether's path, both layers.

    That is the interface holding the vehicle's subnet, plus -- when that one
    is a bridge -- every physical Ethernet adapter that could be underneath
    it. A bridged member has no address of its own, which is exactly how it
    is recognised here, and is also why the routing lookup never finds it.

    Erring towards including an adapter is deliberate. An extra column of
    zeroes costs nothing; the missing column is the one that would have
    settled the argument.
    """
    ifaces = interfaces() if ifaces is None else ifaces
    route = routing_interface(host, ifaces)
    if route is None:
        # Nothing on this machine holds the vehicle's subnet. Returning the
        # address-less adapters here looked helpful and was not: it filled a
        # trace with columns for ports that could not reach the vehicle and
        # took away the caller's chance to say so plainly.
        return []
    chosen: list[Iface] = [route]
    if route.is_bridge:
        for iface in ifaces:
            if iface is route or not iface.hardware:
                continue
            # Ethernet (6) or the IEEE 802.3 variants, with no address of its
            # own: the shape of a bridge member.
            if iface.type not in (6, 117) or iface.ipv4:
                continue
            chosen.append(iface)
    return chosen


# --------------------------------------------------------------------------
#  ARP -- has the resolver still got the vehicle
# --------------------------------------------------------------------------


def arp(host: str = "192.168.2.2", source: str | None = None) -> str | None:
    """The vehicle's MAC address, or None when layer 2 cannot resolve it.

    `SendARP` answers from the cache when it can and puts a request on the
    wire when it cannot, which is the behaviour wanted: a cached hit means
    the entry is still valid, and a failure means the resolver has given up
    on the vehicle entirely. A ping cannot tell those apart.
    """
    if _IP is None:
        return None
    dest = _ip_to_int(host.split(":")[0])
    if dest is None:
        return None
    src = _ip_to_int(source) if source else 0
    # SendARP takes addresses in network order as they sit in memory, which
    # on a little-endian machine is the byte-swap of the integer above.
    dest_le = struct.unpack("<I", struct.pack("!I", dest))[0]
    src_le = struct.unpack("<I", struct.pack("!I", src or 0))[0]
    buf = (ctypes.c_ubyte * 8)()
    length = wintypes.ULONG(8)
    if _IP.SendARP(dest_le, src_le, ctypes.byref(buf),
                   ctypes.byref(length)) != 0:
        return None
    n = int(length.value)
    if n < 6:
        return None
    return ":".join(f"{buf[i]:02X}" for i in range(6))


def tcp_probe(host: str = "192.168.2.2", port: int = 80,
              timeout: float = 1.0) -> float | None:
    """Milliseconds to complete a TCP handshake, or None if it did not.

    A second opinion on reachability that is not ICMP. Echo replies are the
    first thing a loaded router deprioritises, so a ping that stops while a
    handshake still completes is congestion, and both stopping together is a
    path that has gone away.
    """
    t0 = time.monotonic()
    try:
        with socket.create_connection((host.split(":")[0], port), timeout):
            return round((time.monotonic() - t0) * 1000, 1)
    except OSError:
        return None


# --------------------------------------------------------------------------
#  the adapter's own settings, from the registry
# --------------------------------------------------------------------------


def adapter_settings() -> dict[str, dict[str, str]]:
    """Driver settings per interface GUID, as {guid: {setting: value}}.

    Read from the adapter class key rather than from WMI: this needs no
    elevation, no COM, and no second dependency, and the values are the same
    ones the adapter's Advanced tab writes.

    `PnPCapabilities` is translated on the way out, because the raw number is
    an inverted bitmask that nobody should have to remember at a dock.
    """
    if not IS_WINDOWS:
        return {}
    try:
        import winreg  # noqa: PLC0415
    except ImportError:                               # pragma: no cover
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NET_CLASS_KEY)
    except OSError:
        return {}
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not name.isdigit():
                continue
            try:
                key = winreg.OpenKey(root, name)
            except OSError:
                continue
            with key:
                try:
                    guid, _ = winreg.QueryValueEx(key, "NetCfgInstanceId")
                except OSError:
                    continue
                found: dict[str, str] = {}
                for value, label in ADAPTER_SETTINGS.items():
                    try:
                        raw, _ = winreg.QueryValueEx(key, value)
                    except OSError:
                        continue
                    if value == "PnPCapabilities":
                        found[label] = _pnp_capabilities(raw)
                    else:
                        found[label] = str(raw)
                try:
                    desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                    found["driver"] = str(desc)
                except OSError:
                    pass
                try:
                    ver, _ = winreg.QueryValueEx(key, "DriverVersion")
                    found["driver version"] = str(ver)
                except OSError:
                    pass
                if found:
                    out[str(guid).upper()] = found
    return out


def _pnp_capabilities(raw) -> str:
    """The power-management checkbox, said out loud.

    Bit 0x18 forbids Windows from powering the adapter down; bit 0x100
    forbids it from waking the machine. The default -- the value absent, or
    zero -- is that both are allowed, which is the state worth noticing on a
    laptop that spends a flight on battery.
    """
    try:
        bits = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if bits & 0x18:
        return f"{bits} - Windows may NOT power this adapter down"
    return f"{bits} - Windows MAY power this adapter down to save energy"


# --------------------------------------------------------------------------
#  the active power plan
# --------------------------------------------------------------------------


def power_plan() -> str:
    """The name of the active Windows power scheme, or "".

    Recorded because the difference between a flight on battery and a flight
    on mains is not only the battery column: the scheme decides whether PCI
    Express link state power management and adapter power saving are in play
    at all, and the scheme can differ between the two.
    """
    if not IS_WINDOWS:
        return ""
    try:
        power = ctypes.WinDLL("powrprof.dll")
    except OSError:                                   # pragma: no cover
        return ""
    guid_ptr = ctypes.POINTER(_GUID)()
    try:
        if power.PowerGetActiveScheme(None, ctypes.byref(guid_ptr)) != 0:
            return ""
    except Exception:                                 # pragma: no cover
        return ""
    try:
        size = wintypes.DWORD(0)
        power.PowerReadFriendlyName(None, guid_ptr, None, None, None,
                                    ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if power.PowerReadFriendlyName(None, guid_ptr, None, None, buf,
                                       ctypes.byref(size)) != 0:
            return ""
        return buf.raw.decode("utf-16-le", "ignore").rstrip("\x00")
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(guid_ptr)
        except Exception:
            pass


# --------------------------------------------------------------------------
#  what Windows itself logged about the adapters
# --------------------------------------------------------------------------

#: Event providers that say something when a network adapter changes state.
#: NDIS logs the carrier; Tcpip logs address conflicts and interface resets;
#: the bridge service logs its own failures under its driver name.
EVENT_PROVIDERS = ("NDIS", "Tcpip", "Tcpip6", "netbt", "e1dexpress",
                   "rt640x64", "rt68x64", "RTWlanE", "BridgeMP", "Microsoft-Windows-NDIS")


def ndis_events(since: float, until: float | None = None,
                limit: int = 400) -> list[str]:
    """Windows' own record of adapter state changes over a time window.

    Spawns `wevtutil`, which is why it is not on any sampling path: it is
    called once when a flight closes, over the window that flight covered.
    An empty list means Windows logged nothing, which is itself a finding --
    a carrier that never dropped leaves no event, so a blackout with no NDIS
    event behind it was not the cable coming out.
    """
    if not IS_WINDOWS:
        return []
    start = datetime.fromtimestamp(since, timezone.utc)
    end = datetime.fromtimestamp(until or time.time(), timezone.utc)
    span_ms = max(60_000, int((end - start).total_seconds() * 1000) + 60_000)
    providers = " or ".join(f"@Name='{p}'" for p in EVENT_PROVIDERS)
    query = (f"*[System[Provider[{providers}] and "
             f"TimeCreated[timediff(@SystemTime) <= {span_ms}]]]")
    try:
        proc = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{query}", "/f:text",
             f"/c:{limit}", "/rd:true"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return [block.strip() for block in proc.stdout.split("\n\n") if block.strip()]


# --------------------------------------------------------------------------
#  the report a person reads before the dive
# --------------------------------------------------------------------------


def report(host: str = "192.168.2.2") -> str:
    """Everything static about the topside network, as plain text.

    Written into the flight's own logs folder when a flight opens, so that a
    configuration change between two flights is a diff rather than a memory.
    Reads, in order, the way the question arrives: what is the path to the
    vehicle, is it a bridge, what is underneath it, and is anything set to
    power it down.
    """
    lines: list[str] = []
    add = lines.append
    add(f"Topside network - {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    add(f"Machine: {socket.gethostname()}    Vehicle: {host}")
    plan = power_plan()
    if plan:
        add(f"Power plan: {plan}")
    if psutil is not None:
        try:
            b = psutil.sensors_battery()
            if b is not None:
                add(f"Running on: {'AC' if b.power_plugged else 'BATTERY'}"
                    f"  ({b.percent:.0f}%)")
        except Exception:
            pass
    add("")

    ifaces = interfaces()
    if not ifaces:
        add("No interfaces could be read on this machine.")
        return "\n".join(lines)

    route = routing_interface(host, ifaces)
    best = best_interface_index(host)
    if route is None:
        add(f"NOTHING ON THIS MACHINE HOLDS A SUBNET CONTAINING {host}.")
    else:
        add(f"Route to the vehicle: {route.alias}  ({route.description})")
        if route.is_bridge:
            add("  ** This is a Windows network bridge, not a physical "
                "adapter. **")
            add("     Its carrier state and link speed are the bridge's own "
                "and say nothing")
            add("     about the adapter underneath it.")
        if best is not None and best != route.index:
            add(f"  Windows would route over interface {best}, which is not "
                f"this one. Worth resolving before flying.")
    members = [i for i in watched(host, ifaces) if i is not route]
    if members:
        add("  Physical adapters with no address of their own "
            "(bridge members, most likely): "
            + ", ".join(i.alias for i in members))
    add("")

    settings = adapter_settings()
    add("Interfaces")
    add("-" * 72)
    for iface in ifaces:
        if not iface.hardware and not iface.is_bridge and not iface.ipv4:
            continue
        mark = ">" if iface is route else ("-" if iface in members else " ")
        speed = f"{iface.speed_mbps:g} Mbps" if iface.speed_mbps else "-"
        add(f"{mark} {iface.alias}")
        add(f"    {iface.description}")
        add(f"    state {OPER_STATUS.get(iface.oper_status, '?')}"
            f" | carrier {MEDIA_STATE.get(iface.media_state, '?')}"
            f" | {speed} | mtu {iface.mtu}")
        if iface.ipv4:
            add("    address " + ", ".join(f"{a}/{m}" for a, m in iface.ipv4))
        flags = [f for f in iface.flag_names if f != "hardware"]
        if flags:
            add("    flags: " + ", ".join(flags))
        errs = (iface.in_errors + iface.out_errors
                + iface.in_discards + iface.out_discards)
        add(f"    since it came up: in {iface.in_octets / 2 ** 20:.1f} MiB,"
            f" out {iface.out_octets / 2 ** 20:.1f} MiB,"
            f" {iface.in_errors} rx errors, {iface.out_errors} tx errors,"
            f" {iface.in_discards + iface.out_discards} discards"
            + ("" if errs else "   (clean)"))
        found = settings.get(iface.guid.upper())
        if found:
            for label, value in found.items():
                add(f"    {label}: {value}")
        add("")

    mac = arp(host)
    add(f"ARP for {host}: {mac if mac else 'DID NOT RESOLVE'}")
    http = tcp_probe(host, 80)
    add(f"TCP 80 handshake: {f'{http} ms' if http is not None else 'refused or timed out'}")
    return "\n".join(lines)


def snapshot(host: str = "192.168.2.2", *, probe: bool = True) -> dict:
    """The same picture as `report`, as data, for the flight's JSON.

    `probe=False` leaves out the ARP resolution. It is the only reading here
    that blocks -- three seconds against a vehicle that is not answering,
    which is precisely the state a flight is in when it closes -- and the
    caller closing a flight has better things to wait for. Everything else is
    a table read and costs milliseconds.
    """
    ifaces = interfaces()
    route = routing_interface(host, ifaces)
    settings = adapter_settings()
    return {
        "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
        "power_plan": power_plan(),
        "route_alias": route.alias if route else "",
        "route_is_bridge": bool(route and route.is_bridge),
        "best_interface_index": best_interface_index(host),
        "arp": (arp(host) or "") if probe else "",
        "interfaces": [
            {
                "alias": i.alias,
                "description": i.description,
                "index": i.index,
                "guid": i.guid,
                "mac": i.mac,
                "mtu": i.mtu,
                "oper_status": OPER_STATUS.get(i.oper_status, str(i.oper_status)),
                "carrier": MEDIA_STATE.get(i.media_state, str(i.media_state)),
                "speed_mbps": i.speed_mbps,
                "flags": i.flag_names,
                "ipv4": [f"{a}/{m}" for a, m in i.ipv4],
                "settings": settings.get(i.guid.upper(), {}),
            }
            for i in ifaces
            if i.hardware or i.is_bridge or i.ipv4
        ],
    }


# --------------------------------------------------------------------------
#  --netcheck
# --------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    """`--netcheck [report.txt] [--host 192.168.2.2] [--measure]`.

    The whole network picture without opening the application, so it can be
    run on a deck in ten seconds and sent to somebody. Mirrors `--selftest`
    and `--probe-rov`: a file argument if one is given, standard output
    either way, and an exit code that means something.

    Exit codes are deliberately coarse, because the interesting failures are
    the ones a person has to look at:

      0  the vehicle's subnet was found and layer 2 resolved it
      1  an adapter holds the subnet but ARP could not resolve the vehicle
      2  nothing on this machine holds a subnet containing the vehicle
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                 # pragma: no cover
        pass
    argv = list(sys.argv if argv is None else argv)
    host = "192.168.2.2"
    if "--host" in argv:
        i = argv.index("--host")
        if i + 1 < len(argv):
            host = argv[i + 1]
            del argv[i:i + 2]

    measure = "--measure" in argv
    if measure:
        argv.remove("--measure")

    out = report(host)
    if measure:
        # Imported here rather than at the top: `nettrace` imports this
        # module, and the check has to work whether or not a trace ever runs.
        from . import nettrace  # noqa: PLC0415
        out += "\n\n" + nettrace.calibration_text(nettrace.calibrate(host))

    events = ndis_events(time.time() - 3600)
    out += "\n\nWindows adapter events in the last hour\n" + "-" * 72 + "\n"
    out += ("none — no adapter reported a state change, which is itself a "
            "finding when a link has been dropping\n" if not events
            else "\n\n".join(events[:20]) + "\n")

    print(out)

    i = argv.index("--netcheck") if "--netcheck" in argv else -1
    if i >= 0 and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
        try:
            path = Path(argv[i + 1]).expanduser()
            path.write_text(out, encoding="utf-8")
            print(f"\nWritten to {path}")
        except OSError as ex:
            print(f"\nCould not write the report: {ex}", file=sys.stderr)

    if "NOTHING ON THIS MACHINE HOLDS" in out:
        return 2
    return 1 if "DID NOT RESOLVE" in out else 0


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(run(["--netcheck"] + sys.argv[1:]))
