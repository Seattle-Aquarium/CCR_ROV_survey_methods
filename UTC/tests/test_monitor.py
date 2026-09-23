"""Unit tests for the topside monitor: sampling, the flight state machine,
and the files a flight leaves behind.

Hermetic. No vehicle, no display, and no assumption that the machine running
them has any particular sensor -- the point of the sampler is that a laptop
which cannot answer a question leaves a blank rather than failing, so the
tests assert the *shape* of a row and the behavior of the state machine, not
the values a particular Dell happens to report.

The state machine is exercised against a stub vehicle rather than a real one,
because the cases worth testing are the ones that are awkward to produce on a
boat: a dropped request in the middle of a dive, a disarm that turns out to be
a surface interval, and a flight folder nobody chose.

Runnable directly (``python tests/test_monitor.py``) or under pytest.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import blueos, flightlog, laptop  # noqa: E402

# --------------------------------------------------------------------------
#  the schema
# --------------------------------------------------------------------------


def test_columns_are_unique_and_grouped():
    assert len(laptop.COLUMNS) == len(set(laptop.COLUMNS)), "duplicate column"
    # Every grouped column must be a real one, or the page would plot a name
    # that is never written.
    for group, columns in laptop.GROUPS.items():
        assert group in laptop.GROUP_NOTES, f"{group} has no note"
        for c in columns:
            assert c in laptop.COLUMNS, f"{group} names unknown column {c}"
    for c in laptop.UNITS:
        assert c in laptop.COLUMNS, f"units given for unknown column {c}"


def test_row_has_every_column_and_nothing_else():
    """A row must match the header exactly, or the CSV silently shifts."""
    s = laptop.Sampler(flight_id="test")
    try:
        row = s.sample()
    finally:
        s.stop()
    assert set(row) == set(laptop.COLUMNS)
    assert row["flight_id"] == "test"
    assert row["timestamp_utc"].endswith("+00:00"), "timestamps must be UTC"


def test_booleans_are_true_false_or_blank():
    """Never 'False' for 'this machine has no such sensor'.

    A fan column reading FALSE would say the fan is stopped. Blank says
    nobody knows, which is the truth on a laptop with no fan sensor.
    """
    assert laptop._b(True) == "TRUE"
    assert laptop._b(False) == "FALSE"
    assert laptop._b(None) is None


def test_numbers_survive_nonsense():
    assert laptop._f(None) is None
    assert laptop._f("not a number") is None
    assert laptop._f(float("nan")) is None
    assert laptop._f(1.23456, 2) == 1.23


# --------------------------------------------------------------------------
#  history
# --------------------------------------------------------------------------


def test_history_drops_blanks_and_maps_booleans():
    h = laptop.History(minutes=1)
    h.add({"elapsed_time_s": 0.0, "cpu_usage_pct": 10.0, "rov_armed": "TRUE"})
    h.add({"elapsed_time_s": 1.0, "cpu_usage_pct": None, "rov_armed": "FALSE"})
    h.add({"elapsed_time_s": 2.0, "cpu_usage_pct": 30.0, "rov_armed": None})
    assert h.series("cpu_usage_pct") == [(0.0, 10.0), (2.0, 30.0)]
    assert h.series("rov_armed") == [(0.0, 1.0), (1.0, 0.0)]
    assert h.latest()["elapsed_time_s"] == 2.0


def test_history_is_bounded():
    h = laptop.History(minutes=1)
    for i in range(5000):
        h.add({"elapsed_time_s": float(i), "cpu_usage_pct": 1.0})
    assert len(h.series("cpu_usage_pct")) <= h.limit


# --------------------------------------------------------------------------
#  finding the interface that reaches the vehicle
# --------------------------------------------------------------------------


def test_rov_interface_matches_by_subnet(monkeypatch):
    """The tether does not always arrive on the adapter it appears to.

    On the station this was written for, the ROV port is inside a Windows
    network bridge, and it is the bridge that holds 192.168.2.1 and carries
    the counters. Matching on subnet finds it; matching on a name would not.
    """
    import socket as _socket
    from collections import namedtuple

    A = namedtuple("A", "family address netmask")
    monkeypatch.setattr(laptop.psutil, "net_if_addrs", lambda: {
        "Wi-Fi": [A(_socket.AF_INET, "10.59.140.149", "255.255.252.0")],
        "Network Bridge": [A(_socket.AF_INET, "192.168.2.1", "255.255.255.0")],
        "ROV_nereo": [],
    })
    assert laptop.find_rov_interface("192.168.2.2") == "Network Bridge"
    assert laptop.find_rov_interface("10.59.140.1") == "Wi-Fi"


# --------------------------------------------------------------------------
#  parameters and versions
# --------------------------------------------------------------------------


def test_parameter_diff_distinguishes_added_removed_and_changed():
    before = {"A": 1.0, "B": 2.0, "GONE": 3.0}
    after = {"A": 1.0, "B": 9.0, "NEW": 4.0}
    d = blueos.diff_parameters(before, after)
    assert d == {"B": (2.0, 9.0), "GONE": (3.0, None), "NEW": (None, 4.0)}


def test_automatic_parameters_are_marked():
    """The autopilot re-zeros the barometer at every arming.

    Without marking these, every flight's delta file would look as though
    something had been changed, and the one flight where something actually
    was would be indistinguishable.
    """
    assert blueos.is_automatic("BARO1_GND_PRESS")
    assert blueos.is_automatic("STAT_RUNTIME")
    assert not blueos.is_automatic("SURFTRAK_DEPTH")


def test_version_diff_is_flat_and_names_components():
    before = {"blueos": "1.5.0-beta.39", "ardusub": "4.5.0",
              "extensions": [{"name": "Madrona", "tag": "1.15.0",
                              "enabled": True}],
              "containers": [{"name": "blueos-core", "image": "core:39"}]}
    after = {"blueos": "1.5.0-beta.40", "ardusub": "4.5.0",
             "extensions": [{"name": "Madrona", "tag": "1.16.0",
                             "enabled": True},
                            {"name": "New Ext", "tag": "0.1", "enabled": True}],
             "containers": [{"name": "blueos-core", "image": "core:40"}]}
    d = blueos.diff_versions(before, after)
    assert d["blueos"] == ("1.5.0-beta.39", "1.5.0-beta.40")
    assert d["extension:Madrona"] == ("1.15.0", "1.16.0")
    assert d["extension:New Ext"] == (None, "0.1")
    assert d["container:blueos-core"] == ("core:39", "core:40")
    assert "ardusub" not in d, "unchanged components must not appear"


def test_arm_bit_is_read_from_base_mode(monkeypatch):
    """81 is disarmed; 209 is the same vehicle armed."""
    def answer(bits):
        return blueos.Answer(
            url="x", ok=True, status=200,
            body=json.dumps({"message": {"base_mode": {"bits": bits}}}))

    monkeypatch.setattr(blueos, "_get", lambda *a, **k: answer(81))
    assert blueos.read_arm_state("h")[0] is False
    monkeypatch.setattr(blueos, "_get", lambda *a, **k: answer(81 | 128))
    assert blueos.read_arm_state("h")[0] is True


def test_unreachable_vehicle_is_unknown_not_disarmed(monkeypatch):
    """The single most important behavior in the whole recorder.

    The tether drops packets -- measured doing so on 2026-09-11 -- and a
    recorder that read one lost GET as a disarm would stop recording in the
    middle of a transect and write a flight's closing snapshot from there.
    """
    monkeypatch.setattr(blueos, "_get",
                        lambda *a, **k: blueos.Answer(url="x", ok=False,
                                                      error="timeout"))
    armed, ms = blueos.read_arm_state("h")
    assert armed is None and ms is None


# --------------------------------------------------------------------------
#  the flight state machine
# --------------------------------------------------------------------------


class FakeVehicle:
    """A vehicle whose arm state the test drives directly."""

    def __init__(self):
        self.armed = False
        self.params = {"SURFTRAK_DEPTH": -100.0, "BARO1_GND_PRESS": 100.0}
        self.versions = {"blueos": "1.5.0-beta.39", "ardusub": "4.5.0",
                         "extensions": [], "containers": []}

    def install(self, monkeypatch):
        monkeypatch.setattr(blueos, "read_arm_state",
                            lambda *a, **k: (self.armed, 5.0))
        monkeypatch.setattr(blueos, "file_token", lambda *a, **k: "tok")
        monkeypatch.setattr(blueos, "newest_dataflash",
                            lambda *a, **k: "00000080.BIN")
        monkeypatch.setattr(blueos, "read_parameters_now",
                            lambda *a, **k: (dict(self.params), "00000080.BIN"))
        monkeypatch.setattr(blueos, "read_versions",
                            lambda *a, **k: json.loads(json.dumps(self.versions)))
        monkeypatch.setattr(blueos, "read_temperature", lambda *a, **k: (50.0, 55.0))


def _settle(rec, want: str, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if rec.status.state == want:
            return True
        time.sleep(0.05)
    return False


def test_arming_starts_a_flight_and_disarming_closes_it(tmp_path, monkeypatch):
    v = FakeVehicle()
    v.install(monkeypatch)
    monkeypatch.setattr(flightlog, "ARM_POLL_S", 0.05)
    monkeypatch.setattr(flightlog, "DISARM_GRACE_S", 0.5)

    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.start_watching()
    try:
        assert _settle(rec, "idle", 2.0)
        assert rec.status.state == "idle", "must not record before arming"

        v.armed = True
        assert _settle(rec, "recording"), "arming did not start a flight"
        # Something changes during the flight, as it would if someone turned
        # a knob in Cockpit.
        v.params["SURFTRAK_DEPTH"] = -1.75
        v.versions["blueos"] = "1.5.0-beta.40"
        time.sleep(1.2)

        v.armed = False
        assert _settle(rec, "idle"), "disarming did not close the flight"
    finally:
        rec.stop_watching(finish=False)

    logs = tmp_path / "logs"
    names = sorted(p.name.split("_2026")[0].split("_20")[0]
                   for p in logs.iterdir())
    # One record per flight now, not seven files. The reasons are in
    # flightfile's docstring; the test follows the file that replaced them.
    for want in ("laptop_monitor", "flight"):
        assert any(want in n for n in names), f"no {want} file in {names}"

    stamp = rec.status.flight_id
    record = json.loads((logs / f"flight_{stamp}.json").read_text())

    # A knob somebody turned goes in the operator half, on its own, where it
    # cannot be lost among the barometer and the boot counter.
    operator = record["changes"]["parameters_by_operator"]
    assert operator["SURFTRAK_DEPTH"]["before"] == -100.0
    assert operator["SURFTRAK_DEPTH"]["after"] == -1.75
    assert "SURFTRAK_DEPTH" not in record["changes"]["parameters_by_autopilot"]

    assert record["changes"]["versions"]["blueos"]["after"] == "1.5.0-beta.40"
    assert record["parameters"]["read"] is True
    assert record["parameters"]["values"]["SURFTRAK_DEPTH"] == -1.75
    assert record["schema"].startswith("utc.flight/")

    # The CSV's header must match the schema exactly.
    csv_path = logs / f"laptop_monitor_{stamp}.csv"
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(laptop.COLUMNS)


def test_brief_disarm_does_not_split_a_flight(tmp_path, monkeypatch):
    """A surface interval between transects is one dive, not two."""
    v = FakeVehicle()
    v.install(monkeypatch)
    monkeypatch.setattr(flightlog, "ARM_POLL_S", 0.05)
    monkeypatch.setattr(flightlog, "DISARM_GRACE_S", 2.0)

    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.start_watching()
    try:
        v.armed = True
        assert _settle(rec, "recording")
        flight = rec.status.flight_id

        v.armed = False           # a bump of the switch on the surface
        time.sleep(0.6)
        assert rec.status.state == "recording", "closed inside the grace period"
        v.armed = True            # back down for the next transect
        time.sleep(0.6)
        assert rec.status.state == "recording"
        assert rec.status.flight_id == flight, "one dive became two flights"

        v.armed = False
        assert _settle(rec, "idle")
    finally:
        rec.stop_watching(finish=False)

    record = json.loads(
        (tmp_path / "logs" / f"flight_{flight}.json").read_text())
    assert len(record["brief_disarms"]) == 1, "the gap was not recorded"
    assert record["brief_disarms"][0]["seconds"] > 0


def test_a_dropped_request_does_not_end_a_flight(tmp_path, monkeypatch):
    v = FakeVehicle()
    v.install(monkeypatch)
    monkeypatch.setattr(flightlog, "ARM_POLL_S", 0.05)
    monkeypatch.setattr(flightlog, "DISARM_GRACE_S", 0.5)

    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.start_watching()
    try:
        v.armed = True
        assert _settle(rec, "recording")
        # The tether goes quiet: the vehicle cannot be asked at all.
        monkeypatch.setattr(blueos, "read_arm_state", lambda *a, **k: (None, None))
        time.sleep(1.5)
        assert rec.status.state == "recording", \
            "an unanswered question was treated as a disarm"
    finally:
        rec.stop_watching(finish=False)


def test_no_flight_folder_refuses_loudly(monkeypatch):
    """Chosen behavior: never guess a folder, but never do it quietly."""
    v = FakeVehicle()
    v.install(monkeypatch)
    rec = flightlog.FlightRecorder(host="test", flight_dir=None)
    assert rec.start_manually() is False
    assert rec.status.state == "idle"
    assert "flight folder" in rec.status.problem
    assert rec.status.line() == rec.status.problem


def test_manual_flight_is_not_closed_by_the_disarm_watcher(tmp_path, monkeypatch):
    """The override exists for when arm detection is what is broken."""
    v = FakeVehicle()
    v.armed = False
    v.install(monkeypatch)
    monkeypatch.setattr(flightlog, "ARM_POLL_S", 0.05)
    monkeypatch.setattr(flightlog, "DISARM_GRACE_S", 0.3)

    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    rec.start_watching()
    try:
        assert rec.start_manually() is True
        time.sleep(1.2)
        assert rec.status.state == "recording", \
            "a hand-started recording was closed by the disarm watcher"
        assert rec.status.by_hand
        assert rec.stop_manually() is True
        assert _settle(rec, "idle")
    finally:
        rec.stop_watching(finish=False)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
