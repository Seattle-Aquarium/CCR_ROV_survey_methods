"""
What normal, unusual and bad look like on the Monitoring page.

The bands colour every Live monitoring strip, on real flights as well as in
the simulator, so what is checked here is that each kind of judgement does
what its description says -- a one-second spike is not an alarm, a counter
that resets is, a slow clock on an idle laptop is not -- and that every
reading on the page has one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import bands as B  # noqa: E402
from rov_flight_ops import laptop as L  # noqa: E402

N, U, X = B.NORMAL, B.UNUSUAL, B.BAD


def rows(column, values, **also):
    out = []
    for i, v in enumerate(values):
        r = {"elapsed_time_s": float(i), column: v}
        for k, seq in also.items():
            r[k] = seq[i] if isinstance(seq, list) else seq
        out.append(r)
    return out


def test_every_reading_on_the_page_has_a_band_and_a_reason():
    for group, columns in L.GROUPS.items():
        for column in columns:
            bands = B.bands_for(column)
            assert bands, f"{group}: {column} has no band"
            assert all(b.why for b in bands), column
            assert B.threshold_text(column, L.UNITS.get(column, "")), column


def test_a_one_second_spike_is_not_an_alarm_but_a_held_one_is():
    spike = rows("cpu_usage_pct", [40] * 10 + [100] + [40] * 10)
    assert max(lv for lv in B.levels("cpu_usage_pct", spike)) == N
    held = rows("cpu_usage_pct", [40] * 10 + [99] * 10)
    assert B.levels("cpu_usage_pct", held)[-1] == X


def test_a_slow_clock_matters_only_while_the_cpu_is_busy():
    idle = rows("cpu_frequency_mhz", [800] * 10, cpu_usage_pct=10)
    busy = rows("cpu_frequency_mhz", [800] * 10, cpu_usage_pct=90)
    assert B.levels("cpu_frequency_mhz", idle)[-1] == N
    assert B.levels("cpu_frequency_mhz", busy)[-1] == X


def test_memory_is_judged_against_the_memory_there_is():
    r = rows("ram_used_gb", [28.0] * 6, ram_available_gb=4.0)      # 87.5 %
    assert B.levels("ram_used_gb", r)[-1] == X
    r = rows("ram_used_gb", [11.0] * 6, ram_available_gb=21.0)     # 34 %
    assert B.levels("ram_used_gb", r)[-1] == N


def test_a_counter_that_climbs_or_resets_is_flagged():
    flat = rows("network_packets_discarded", [4] * 20)
    assert set(B.levels("network_packets_discarded", flat)) == {N}
    rising = rows("network_packets_discarded", [4] * 10 + list(range(5, 200, 19)))
    assert B.levels("network_packets_discarded", rising)[-1] == X
    reset = rows("pi_eth_carrier_drops", [3] * 40 + [0] * 5)
    assert B.levels("pi_eth_carrier_drops", reset)[-1] == X


def test_a_byte_count_that_stops_is_bad_but_one_never_used_is_not():
    moving = rows("phy_rx_bytes", list(range(0, 40_000_000, 1_000_000)))
    assert B.levels("phy_rx_bytes", moving)[-1] == N
    stopped = rows("phy_rx_bytes", list(range(1_000_000, 21_000_000, 1_000_000))
                   + [20_000_000] * 15)
    assert B.levels("phy_rx_bytes", stopped)[-1] == X
    unused = rows("phy_rx_bytes", [0] * 30)
    assert set(B.levels("phy_rx_bytes", unused)) == {N}


def test_flags_and_their_context():
    assert B.levels("nic_low_power", rows("nic_low_power", ["TRUE"]))[0] == X
    assert B.levels("nic_low_power", rows("nic_low_power", ["FALSE"]))[0] == N
    # The watched adapter is often the bridge's other port: FALSE alone is
    # normal, FALSE with the vehicle gone is a broken tether.
    here = rows("phy_carrier", ["FALSE"], rov_reachable="TRUE")
    gone = rows("phy_carrier", ["FALSE"], rov_reachable="FALSE")
    assert B.levels("phy_carrier", here)[0] == N
    assert B.levels("phy_carrier", gone)[0] == X


def test_the_fathom_x_pair_far_apart_is_unusual_even_when_both_are_fast():
    r = rows("tether_tx_mbps", [125.0] * 3, tether_rx_mbps=[100.0] * 3)
    assert B.levels("tether_tx_mbps", r)[-1] == U


def test_a_blank_is_not_judged():
    r = rows("cpu_package_temp_c", [None, None, None])
    assert B.levels("cpu_package_temp_c", r) == [None, None, None]


def test_the_page_judging_a_row_at_a_time_matches_judging_all_at_once():
    """The Monitoring page judges each new row once, with only the band
    memory before it as context. That must give exactly the answer a fresh
    judgement of the whole history gives -- otherwise a strip's colours would
    depend on when the page happened to be looked at."""
    from rov_flight_ops.gui import monitorpage as MP
    from rov_flight_ops.sim.laptop import LaptopModel
    from rov_flight_ops.sim.world import SimWorld

    class Rec:
        history = L.History(minutes=30)

    model = LaptopModel(SimWorld(mono=1000.0))
    page = MP.MonitorPage.__new__(MP.MonitorPage)
    page._series_cache, page._series_from = {}, None
    page._rows, page._times, page._rows_at, page._fresh = [], [], None, set()
    columns = [c for g in L.GROUPS.values() for c in g]
    rec = Rec()
    for t in range(900):
        rec.history.add(model.row(float(t)))
        if t % 11 == 0:
            for c in columns:
                page._judged(rec, c)
    rows = rec.history.rows()
    for c in columns:
        times, _values, levels = page._judged(rec, c)
        full = {r["elapsed_time_s"]: lv for r, lv in zip(rows, B.levels(c, rows),
                                                         strict=True)}
        assert all(full[t] == lv for t, lv in zip(times, levels, strict=True)), c


def _contrast(a: str, b: str) -> float:
    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_three_levels_are_three_colours_in_both_modes():
    """The theme's WARN and ERROR are the same coral in dark mode; the levels
    must not be, and each must be visible as a line on its chart surface."""
    from rov_flight_ops.gui import theme as T
    for mode, surface in ((0, T.FIELD_BG[0]), (1, T.FIELD_BG[1])):
        colors = [B.LEVEL_COLORS[lv][mode] for lv in (N, U, X)]
        assert len(set(colors)) == 3
        for c in colors:
            assert _contrast(c, surface) >= 3.0, (mode, c)
