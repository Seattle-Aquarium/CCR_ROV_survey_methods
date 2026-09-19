"""
The network diagnosis, tested without a network.

Everything here is a pure function over readings, or a reading that is
allowed to be absent. The parts that talk to Windows are exercised where the
platform allows and skipped where it does not, because a test that needs a
bridged laptop with a vehicle on the end of it is not a test anybody runs.

What is worth asserting is the reasoning, not the syscall: that a subnet match
finds the right adapter, that a bridge is recognised and its members are kept,
that a counter column cannot silently land in another adapter's column, and
that the vehicle's own counters survive BlueOS renaming them.
"""

from __future__ import annotations

import json
import sys

import pytest

from rov_flight_ops import blueos, netdiag, nettrace

WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _forget_discovered_endpoints():
    """Clear what `blueos` has memoised about where a vehicle answers.

    Those caches exist so a blackout is not spent re-walking sixteen URLs at
    their full timeouts. They are module-level, so without this a test would
    inherit the endpoint a previous one discovered and pass for the wrong
    reason -- or, worse, be silently skipped by the re-probe cooldown.
    """
    for cache in (blueos._NETWORK_PATH_FOUND, blueos._TETHER_FOUND,
                  blueos._TETHER_PROBED_AT):
        cache.clear()
    yield
    for cache in (blueos._NETWORK_PATH_FOUND, blueos._TETHER_FOUND,
                  blueos._TETHER_PROBED_AT):
        cache.clear()


# --------------------------------------------------------------------------
#  addresses and masks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("prefix,expected", [
    (24, "255.255.255.0"),
    (16, "255.255.0.0"),
    (8, "255.0.0.0"),
    (32, "255.255.255.255"),
    (0, "0.0.0.0"),
])
def test_prefix_length_becomes_a_dotted_mask(prefix, expected):
    assert netdiag._mask(prefix) == expected


def test_a_nonsense_prefix_is_clamped_not_crashed():
    # GetAdaptersAddresses has been seen to report 255 on a tunnel adapter.
    # A monitor that raises on one odd interface stops reading the rest.
    assert netdiag._mask(255) == "255.255.255.255"
    assert netdiag._mask(-1) == "0.0.0.0"


# --------------------------------------------------------------------------
#  which adapter reaches the vehicle
# --------------------------------------------------------------------------


def _iface(index, alias, ipv4=(), description="", flags=0x01, type_=6):
    return netdiag.Iface(index=index, alias=alias, description=description,
                         flags=flags, type=type_, ipv4=list(ipv4))


def test_routing_interface_matches_on_subnet_not_on_name():
    ifaces = [
        _iface(1, "Wi-Fi", [("10.0.0.4", "255.255.255.0")]),
        _iface(2, "Ethernet", [("192.168.1.5", "255.255.255.0")]),
        _iface(3, "Network Bridge", [("192.168.2.1", "255.255.255.0")],
               description="MAC Bridge Miniport"),
    ]
    found = netdiag.routing_interface("192.168.2.2", ifaces)
    assert found is not None and found.alias == "Network Bridge"


def test_no_adapter_on_the_vehicles_subnet_is_none_not_a_guess():
    ifaces = [_iface(1, "Wi-Fi", [("10.0.0.4", "255.255.255.0")])]
    assert netdiag.routing_interface("192.168.2.2", ifaces) is None


def test_a_bridge_is_recognised_by_what_it_calls_itself():
    assert _iface(3, "Network Bridge", description="MAC Bridge Miniport").is_bridge
    assert _iface(3, "Ethernet 2", description="Realtek PCIe GbE").is_bridge is False


def test_bridge_members_are_watched_even_though_nothing_routes_over_them():
    """The member has no address, so the routing lookup can never find it.

    It is the whole reason this exists: the member is where the cable is, and
    the bridge above it reports itself connected regardless.
    """
    bridge = _iface(3, "Network Bridge", [("192.168.2.1", "255.255.255.0")],
                    description="MAC Bridge Miniport")
    member = _iface(4, "Ethernet 2", description="Realtek PCIe GbE")
    unrelated = _iface(5, "Wi-Fi", [("10.0.0.4", "255.255.255.0")], type_=71)
    chosen = netdiag.watched("192.168.2.2", [bridge, member, unrelated])
    aliases = [i.alias for i in chosen]
    assert aliases[0] == "Network Bridge"
    assert "Ethernet 2" in aliases
    assert "Wi-Fi" not in aliases


def test_an_unbridged_adapter_is_watched_alone():
    plain = _iface(4, "Ethernet", [("192.168.2.1", "255.255.255.0")],
                   description="Realtek PCIe GbE")
    spare = _iface(5, "Ethernet 3", description="Realtek PCIe GbE")
    chosen = netdiag.watched("192.168.2.2", [plain, spare])
    assert [i.alias for i in chosen] == ["Ethernet"]


# --------------------------------------------------------------------------
#  the NDIS flags, which are the reading psutil drops
# --------------------------------------------------------------------------


def test_low_power_and_paused_are_read_out_of_the_flag_byte():
    iface = _iface(1, "Ethernet", flags=0x01 | 0x20 | 0x40)
    assert iface.hardware and iface.paused and iface.low_power
    assert "low power" in iface.flag_names


def test_carrier_unknown_is_none_rather_than_false():
    """"Disconnected" and "this driver will not say" are different findings."""
    assert netdiag.Iface(media_state=1).connected is True
    assert netdiag.Iface(media_state=2).connected is False
    assert netdiag.Iface(media_state=0).connected is None


# --------------------------------------------------------------------------
#  the power-management checkbox
# --------------------------------------------------------------------------


def test_power_management_reads_the_inverted_bit_the_right_way_round():
    allowed = netdiag._pnp_capabilities(0)
    forbidden = netdiag._pnp_capabilities(24)
    assert "MAY power" in allowed and "NOT" not in allowed
    assert "NOT power" in forbidden
    # 280 is the other value the checkbox writes, with wake disabled too.
    assert "NOT power" in netdiag._pnp_capabilities(280)


def test_a_setting_that_is_not_a_number_is_passed_through_not_dropped():
    assert netdiag._pnp_capabilities("odd") == "odd"


# --------------------------------------------------------------------------
#  column naming in the fast trace
# --------------------------------------------------------------------------


def test_interface_names_become_usable_column_prefixes():
    assert nettrace._slug("Network Bridge") == "network_bridge"
    assert nettrace._slug("Ethernet 2") == "ethernet_2"
    assert nettrace._slug("Wi-Fi") == "wi_fi"
    assert nettrace._slug("!!!") == "iface"


def test_two_adapters_cannot_share_a_column(monkeypatch):
    """Two aliases can slug the same. Their counters must not merge.

    A silent merge would produce a column that is the sum of a live adapter
    and a dead one, which reads as a healthy link forever.
    """
    a = _iface(4, "Ethernet-2", [("192.168.2.1", "255.255.255.0")])
    b = _iface(5, "Ethernet 2")
    monkeypatch.setattr(netdiag, "watched", lambda host, ifaces=None: [a, b])
    tracer = nettrace.Tracer(host="192.168.2.2")
    tracer._order = tracer._rescan()
    slugs = [w.slug for w in tracer._order]
    assert len(slugs) == len(set(slugs)), slugs
    columns = tracer.columns()
    assert len(columns) == len(set(columns))
    assert columns[:2] == ["timestamp_utc", "elapsed_s"]


def test_a_watched_interface_is_never_dropped(monkeypatch):
    """An adapter that goes away mid-flight keeps its column.

    Dropping it would be the log deleting the evidence of the event it exists
    to record.
    """
    bridge = _iface(3, "Network Bridge", [("192.168.2.1", "255.255.255.0")],
                    description="MAC Bridge Miniport")
    member = _iface(4, "Ethernet 2")
    seen = [[bridge, member], [bridge]]
    monkeypatch.setattr(netdiag, "watched",
                        lambda host, ifaces=None: seen.pop(0) if seen else [bridge])
    tracer = nettrace.Tracer(host="192.168.2.2")
    tracer._order = tracer._rescan()
    assert len(tracer._order) == 2
    tracer._order = tracer._rescan()
    assert len(tracer._order) == 2, "a vanished adapter lost its column"


def test_events_do_not_grow_without_bound():
    tracer = nettrace.Tracer(host="192.168.2.2")
    for i in range(5000):
        tracer._note(f"event {i}")
    assert len(tracer.events) <= 4000


def test_a_trace_with_no_folder_refuses_rather_than_writing_somewhere_else(
        monkeypatch):
    monkeypatch.setattr(
        netdiag, "watched",
        lambda host, ifaces=None: [_iface(3, "Network Bridge",
                                          [("192.168.2.1", "255.255.255.0")])])
    tracer = nettrace.Tracer(host="192.168.2.2", folder=None)
    assert tracer.start() is False
    assert "folder" in tracer.problem.lower()


def test_a_trace_with_nothing_to_watch_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(netdiag, "watched", lambda host, ifaces=None: [])
    tracer = nettrace.Tracer(host="192.168.2.2", folder=tmp_path)
    assert tracer.start() is False
    assert "192.168.2.2" in tracer.problem


# --------------------------------------------------------------------------
#  the vehicle's own end of the tether
# --------------------------------------------------------------------------


class _Answer:
    def __init__(self, body, ok=True, kind="application/json"):
        self.ok, self.body, self.kind, self.status = ok, body, kind, 200


def test_vehicle_counters_survive_blueos_renaming_them(monkeypatch):
    """linux2rest has used more than one name for the same counter.

    A reader that knows one name reports a confident zero on a vehicle that
    uses the other, which is worse than reporting nothing.
    """
    old = [{"name": "eth0", "total_received_B": 1234,
            "total_transmitted_B": 99, "total_errors_on_received": 2}]
    new = [{"name": "eth0", "rx_bytes": 1234, "tx_bytes": 99, "rx_errors": 2}]
    for body in (old, new):
        monkeypatch.setattr(blueos, "_get",
                            lambda url, _b=body, **kw: _Answer(json.dumps(_b)))
        blueos._NETWORK_PATH_FOUND.pop("192.168.2.2", None)
        found = blueos.read_interfaces("192.168.2.2")
        assert found["eth0"]["rx_bytes"] == 1234
        assert found["eth0"]["rx_errors"] == 2


def test_an_unreachable_vehicle_gives_nothing_not_zero(monkeypatch):
    monkeypatch.setattr(blueos, "_get",
                        lambda url, **kw: _Answer("", ok=False))
    assert blueos.read_interfaces("192.168.2.2") == {}


def test_the_tether_interface_is_named_not_guessed():
    assert blueos.tether_interface({"lo": {}, "eth0": {}}) == "eth0"
    assert blueos.tether_interface({"lo": {}, "end0": {}}) == "end0"


def test_a_vehicle_with_no_eth0_picks_the_busiest_real_interface():
    found = blueos.tether_interface({
        "lo": {"rx_bytes": 10 ** 9},
        "docker0": {"rx_bytes": 10 ** 8},
        "enxabc": {"rx_bytes": 5000},
        "enxdef": {"rx_bytes": 900_000},
    })
    assert found == "enxdef"


def test_tether_diagnostics_keeps_the_body_even_when_it_knows_no_keys(
        monkeypatch):
    """An unrecognised key is still evidence once a person reads the file."""
    body = {"something_new": 41.5, "peer": "aa:bb"}
    monkeypatch.setattr(blueos, "_get",
                        lambda url, **kw: _Answer(json.dumps(body)))
    found = blueos.read_tether("192.168.2.2")
    assert found["raw"] == body
    assert "rx_mbps" not in found


def test_tether_diagnostics_finds_a_rate_it_does_know(monkeypatch):
    monkeypatch.setattr(
        blueos, "_get",
        lambda url, **kw: _Answer(json.dumps({"rx_rate": 38.2, "tx_rate": 41})))
    found = blueos.read_tether("192.168.2.2")
    assert found["rx_mbps"] == 38.2
    assert found["tx_mbps"] == 41.0


def test_a_vehicle_without_the_extension_is_empty_not_an_error(monkeypatch):
    monkeypatch.setattr(blueos, "_get", lambda url, **kw: _Answer("", ok=False))
    assert blueos.read_tether("192.168.2.2") == {}


def test_tether_diagnostics_reads_the_extensions_actual_shape(monkeypatch):
    """/v1.0/rate is a bare [tx, rx] list, not the dict the first cut guessed.

    Confirmed from williangalvani/plc-extension's own source on 2026-09-18,
    after a flight where the guessed paths and shape found nothing all day
    despite the extension being installed and running.
    """
    hits = []

    def fake_get(url, **kw):
        hits.append(url)
        if url.endswith("/v1.0/rate"):
            return _Answer(json.dumps([163.0, 176.0]))
        return _Answer("", ok=False)

    monkeypatch.setattr(blueos, "_get", fake_get)
    found = blueos.read_tether("192.168.2.2")
    assert found["tx_mbps"] == 163.0
    assert found["rx_mbps"] == 176.0
    assert found["values"] == [163.0, 176.0]
    assert found["port"] == 1142
    assert any("/v1.0/rate" in h for h in hits)


def test_tether_diagnostics_null_rate_is_not_a_missing_extension(monkeypatch):
    """The extension answers `null` when it has never seen a remote device.

    That is "installed, nothing to report yet" -- distinct from the empty
    dict a vehicle without the extension at all returns, and the CSV column
    needs to tell the two apart rather than showing the same blank for both.
    """
    monkeypatch.setattr(
        blueos, "_get",
        lambda url, **kw: (_Answer("null") if url.endswith("/v1.0/rate")
                            else _Answer("", ok=False)))
    found = blueos.read_tether("192.168.2.2")
    assert found  # present: port/path/raw were filled in
    assert "tx_mbps" not in found and "rx_mbps" not in found


def test_tether_devices_reports_how_many_nodes_are_heard(monkeypatch):
    devices = [
        {"mac": "aa:aa", "firmware": "f1", "node": "001", "is_local": True},
        {"mac": "bb:bb", "firmware": "f2", "node": "002", "is_local": False},
    ]
    monkeypatch.setattr(
        blueos, "_get",
        lambda url, **kw: (_Answer(json.dumps(devices))
                            if url.endswith("/v1.0/devices") else _Answer("", ok=False)))
    found = blueos.read_tether_devices("192.168.2.2")
    assert found["count"] == 2
    assert found["remote_mac"] == "bb:bb"


def test_tether_devices_and_rate_do_not_share_a_cached_path(monkeypatch):
    """The two routes live on one extension but are cached independently.

    A bug fixed in the same pass this test guards: both calls shared one
    `_TETHER_FOUND[host]` slot, so whichever endpoint was probed first
    poisoned the other's cache and the second one silently served the wrong
    URL's body forever after.
    """
    def fake_get(url, **kw):
        if url.endswith("/v1.0/rate"):
            return _Answer(json.dumps([10.0, 12.0]))
        if url.endswith("/v1.0/devices"):
            return _Answer(json.dumps([{"mac": "a", "is_local": True}]))
        return _Answer("", ok=False)

    monkeypatch.setattr(blueos, "_get", fake_get)
    rate = blueos.read_tether("192.168.2.2")
    devices = blueos.read_tether_devices("192.168.2.2")
    assert rate["tx_mbps"] == 10.0
    assert devices["count"] == 1


# --------------------------------------------------------------------------
#  against this actual machine, where it is a Windows one
# --------------------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="reads the Windows IP Helper API")
def test_the_interface_table_can_be_read_on_this_machine():
    ifaces = netdiag.interfaces()
    assert ifaces, "GetIfTable2 returned nothing"
    loopback = [i for i in ifaces if i.type == 24]
    assert loopback, "no loopback interface, so the table was misread"
    assert any(i.ipv4 for i in ifaces), "no addresses were attached"


@pytest.mark.skipif(not WINDOWS, reason="reads the Windows IP Helper API")
def test_the_fast_path_returns_the_same_counters_as_the_slow_one():
    ifaces = netdiag.interfaces()
    wanted = [i.index for i in ifaces[:3]]
    fast = netdiag.counters(wanted)
    assert set(fast) == set(wanted)
    for index in wanted:
        assert fast[index].alias == next(
            i.alias for i in ifaces if i.index == index)


@pytest.mark.skipif(not WINDOWS, reason="reads the Windows registry")
def test_adapter_settings_are_keyed_by_the_same_guid_the_table_reports():
    settings = netdiag.adapter_settings()
    if not settings:
        pytest.skip("this machine publishes no adapter settings")
    guids = {i.guid.upper() for i in netdiag.interfaces()}
    assert guids & set(settings), "no adapter setting could be joined to an interface"
