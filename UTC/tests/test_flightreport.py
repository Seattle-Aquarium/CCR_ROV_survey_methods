"""
The post-flight analysis, tested on flights that never happened.

Every test here builds its own logs -- a CSV with an outage in it, a snapshot
that failed to read, a parameter set with one knob turned -- so none of it
needs a flight folder or five gigabytes of recordings.

What is worth asserting is the judgement, because the judgement is the part
that can be wrong without anything raising. Several of these exist because the
first version got them wrong against real data:

  * a snapshot that failed to read must not diff as twenty removals;
  * a ground station must be credited with the outages that happened while it
    was connected, including the ones between its recordings;
  * a parameter the autopilot maintains must not be reported as somebody
    turning a knob;
  * and a float32 parameter must not diff against itself.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from utc import flightfile
from utc import flightreport as R
from utc import flightscan as S

T0 = datetime(2026, 9, 11, 20, 0, 0, tzinfo=timezone.utc)


def _iso(offset: float) -> str:
    return (T0 + timedelta(seconds=offset)).isoformat(timespec="milliseconds")


def _monitor_csv(path, seconds=300, dead=(), armed=True, rx=22.0):
    """A topside CSV with outages where asked for them."""
    columns = ["timestamp_utc", "rov_ping_latency_ms", "network_receive_mbps",
               "rov_reachable", "rov_armed", "cpu_usage_pct",
               "ram_available_gb", "network_errors_received",
               "battery_discharge_w", "pi_soc_temp_c"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for i in range(seconds):
            out = any(a <= i < b for a, b in dead)
            writer.writerow({
                "timestamp_utc": _iso(i),
                "rov_ping_latency_ms": "" if out else 4.0,
                "network_receive_mbps": 0.0 if out else rx,
                "rov_reachable": "FALSE" if out else "TRUE",
                "rov_armed": "TRUE" if armed and not out else "FALSE",
                "cpu_usage_pct": 40.0,
                "ram_available_gb": 19.0,
                "network_errors_received": 0,
                "battery_discharge_w": 33.0,
                "pi_soc_temp_c": 62.0,
            })


# --------------------------------------------------------------------------
#  reading the topside row
# --------------------------------------------------------------------------


def test_an_outage_is_found_and_measured(tmp_path):
    path = tmp_path / "laptop_monitor_2026-09-11_2000.csv"
    _monitor_csv(path, 300, dead=[(100, 160)])
    session = S.read_monitor(path)
    assert session.rows == 300
    assert len(session.icmp_dead) == 1
    assert session.icmp_dead[0].seconds == pytest.approx(60, abs=2)


def test_an_outage_that_runs_to_the_end_is_not_dropped(tmp_path):
    """A flight whose last act was losing the link still recorded one."""
    path = tmp_path / "laptop_monitor_2026-09-11_2000.csv"
    _monitor_csv(path, 200, dead=[(150, 999)])
    session = S.read_monitor(path)
    assert len(session.icmp_dead) == 1
    assert session.icmp_dead[0].seconds >= 45


def test_a_blank_reading_is_not_a_zero(tmp_path):
    """An unanswered ping is missing, not fast."""
    path = tmp_path / "laptop_monitor_2026-09-11_2000.csv"
    _monitor_csv(path, 60, dead=[(10, 20)])
    session = S.read_monitor(path)
    _t, values = session.series("rov_ping_latency_ms")
    assert len(values) == 50, "blanks were counted as readings"
    assert all(v == 4.0 for v in values)


# --------------------------------------------------------------------------
#  the parameter record
# --------------------------------------------------------------------------


def test_float32_noise_does_not_survive_into_the_record():
    """`0.30000001192092896` is how a float32 0.3 prints. It is still 0.3.

    Left alone it makes an unchanged parameter diff against itself, which is
    a false positive in the one file whose whole job is not to have any.
    """
    assert flightfile.clean(0.30000001192092896) == 0.3
    assert flightfile.clean(0.009999999776482582) == 0.01
    assert flightfile.clean(4.5) == 4.5
    assert flightfile.clean(0.0) == 0.0


def test_a_value_that_needs_its_digits_keeps_them():
    """Shortening must never land on a different float32."""
    import struct
    for value in (101453.421875, 102389.6953125, 1.0000001, -0.004294687):
        cleaned = flightfile.clean(value)
        assert (struct.unpack("<f", struct.pack("<f", cleaned))[0]
                == struct.unpack("<f", struct.pack("<f", value))[0]), value


def test_operator_changes_are_separated_from_autopilot_bookkeeping():
    """The question is "did somebody turn a knob", and it has its own half."""
    before = {"SURFTRAK_DEPTH": -100.0, "BARO1_GND_PRESS": 101453.4,
              "STAT_BOOTCNT": 54.0}
    after = {"SURFTRAK_DEPTH": -1.75, "BARO1_GND_PRESS": 99846.2,
             "STAT_BOOTCNT": 55.0}
    operator, autopilot = flightfile.partition_parameters(before, after)
    assert list(operator) == ["SURFTRAK_DEPTH"]
    assert set(autopilot) == {"BARO1_GND_PRESS", "STAT_BOOTCNT"}


def test_an_unchanged_parameter_set_reports_nothing():
    values = {"ACRO_EXPO": 0.30000001192092896, "AHRS_EKF_TYPE": 3.0}
    operator, autopilot = flightfile.partition_parameters(values, dict(values))
    assert operator == {} and autopilot == {}


# --------------------------------------------------------------------------
#  a snapshot that did not happen
# --------------------------------------------------------------------------


def test_an_empty_versions_block_is_not_a_vehicle_with_nothing_installed():
    assert flightfile.versions_were_read(
        {"blueos": "1.5.0", "containers": [{"name": "x"}]}) is True
    assert flightfile.versions_were_read(
        {"blueos": "", "containers": [], "extensions": []}) is False
    assert flightfile.versions_were_read({}) is False


def test_a_diff_against_a_failed_snapshot_is_refused():
    """The 11 September logs reported twenty components removed. None were."""
    real = {"blueos": "1.5.0-beta.39", "ardusub": "4.5.7",
            "containers": [{"name": f"c{i}", "image": f"i{i}"}
                           for i in range(8)],
            "extensions": [{"name": f"e{i}", "tag": "1"} for i in range(6)]}
    failed = {"blueos": "", "containers": [], "extensions": []}

    changes, why = flightfile.diff_versions(real, failed)
    assert changes == {}, "a failed read was reported as twenty removals"
    assert "did not read" in why

    changes, why = flightfile.diff_versions(failed, real)
    assert changes == {}
    assert "did not read" in why


def test_a_real_upgrade_still_reports():
    before = {"blueos": "1.5.0-beta.39", "containers": [{"name": "core",
                                                         "image": "core:39"}]}
    after = {"blueos": "1.5.0-beta.40", "containers": [{"name": "core",
                                                        "image": "core:40"}]}
    changes, why = flightfile.diff_versions(before, after)
    assert why == ""
    assert changes["blueos"]["after"] == "1.5.0-beta.40"


def test_a_mass_disappearance_is_suppressed_even_when_both_sides_look_read():
    """The opening snapshot can fail while the closing one is fine.

    Its versions block then looks healthy on its own and the diff between them
    is confidently wrong. Catching that needs the shape of the diff, not the
    shape of either side.
    """
    before = {"blueos": "1.5.0",
              "containers": [{"name": f"c{i}", "image": f"i{i}"}
                             for i in range(6)]}
    after = {"blueos": "1.5.0", "containers": [{"name": "c0", "image": "i0"}]}
    changes, why = flightfile.diff_versions(before, after)
    assert changes == {}
    assert "failed" in why


# --------------------------------------------------------------------------
#  the whole analysis
# --------------------------------------------------------------------------


def _day_with(tmp_path, **kw):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _monitor_csv(logs / "laptop_monitor_2026-09-11_2000.csv", **kw)
    return S.scan(tmp_path, deep=False)


def test_a_clean_flight_says_the_laptop_kept_up(tmp_path):
    day = _day_with(tmp_path, seconds=600)
    report = R.analyze(day)
    titles = [f.title for f in report.findings]
    assert any("laptop kept up" in t for t in titles)
    assert not report.of(R.CRITICAL)


def test_outages_are_raised_and_counted(tmp_path):
    day = _day_with(tmp_path, seconds=600, dead=[(100, 200), (400, 430)])
    report = R.analyze(day)
    outages = [o for o in report.outages if o.seconds >= 2]
    assert len(outages) == 2
    assert any("went silent" in f.title for f in report.findings)


def test_flying_on_battery_is_noted(tmp_path):
    day = _day_with(tmp_path, seconds=120)
    report = R.analyze(day)
    assert any("battery" in f.title.lower() for f in report.findings)


def test_an_empty_folder_reports_rather_than_raises(tmp_path):
    (tmp_path / "logs").mkdir()
    report = R.analyze(S.scan(tmp_path, deep=False))
    assert report.headline
    assert isinstance(report.findings, list)


def test_a_missing_folder_is_a_problem_not_an_exception(tmp_path):
    day = S.scan(tmp_path / "nope", deep=False)
    assert day.problems
    assert R.analyze(day).headline


# --------------------------------------------------------------------------
#  ground stations
# --------------------------------------------------------------------------


def _recording(start, end, gcs, texts=()):
    from pathlib import Path
    rec = S.Recording(path=Path(f"recorder_{int(start)}.mcap"))
    rec.start, rec.end, rec.gcs = start, end, gcs
    rec.armed_from, rec.armed_to = start, end
    rec.statustexts = list(texts)
    return rec


def test_a_client_is_credited_with_the_outages_between_its_recordings():
    """An outage disarms the vehicle, so it falls *between* recordings.

    Measuring availability over the recordings alone therefore reports the
    client that kept dropping the link as the one that held it best. It did
    exactly that in the first version.
    """
    day = S.FlightDay(folder=__import__("pathlib").Path("."))
    base = T0.timestamp()
    day.recordings = [
        _recording(base + 60, base + 120, "255/240"),
        _recording(base + 400, base + 900, "255/190"),
    ]
    session = S.MonitorSession()
    session.start, session.end = base, base + 900
    session.times = [base + i for i in range(900)]
    # Dead from 120 to 240 -- between the two recordings, while Cockpit had it.
    session.columns = {
        "rov_ping_latency_ms": [None if 120 <= i < 240 else 4.0
                                for i in range(900)],
        "network_receive_mbps": [0.0 if 120 <= i < 240 else 20.0
                                 for i in range(900)],
    }
    day.monitors = [session]

    report = R.analyze(day)
    by_name = {p.name: p for p in report.gcs}
    cockpit, qgc = by_name["Cockpit"], by_name["QGroundControl"]
    # The whole 120 s outage lands on Cockpit and none of it on QGC.
    assert cockpit.monitored_s - cockpit.alive_s == pytest.approx(120, abs=2)
    assert qgc.monitored_s - qgc.alive_s < 2
    assert cockpit.availability_pct < qgc.availability_pct - 25


def test_a_failsafe_message_names_the_disarm():
    day = S.FlightDay(folder=__import__("pathlib").Path("."))
    base = T0.timestamp()
    day.recordings = [_recording(
        base, base + 300, "255/240",
        [(base + 299, "WARNING", "MYGCS: 255, heartbeat lost"),
         (base + 299.5, "CRITICAL", "Lost manual control")])]
    report = R.analyze(day)
    assert len(report.disarms) == 1
    assert report.disarms[0].cause == "gcs failsafe"
    assert any("failsafe" in f.title.lower() for f in report.findings)
    assert report.of(R.CRITICAL)


def test_a_recording_that_simply_ended_is_not_called_a_failsafe():
    day = S.FlightDay(folder=__import__("pathlib").Path("."))
    base = T0.timestamp()
    day.recordings = [_recording(base, base + 300, "255/190")]
    report = R.analyze(day)
    assert report.disarms[0].cause == "operator"
    assert not report.of(R.CRITICAL)


# --------------------------------------------------------------------------
#  the flight record, round-tripped
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, parameters=None, versions=None, taken=0.0, source=""):
        self.parameters = parameters or {}
        self.versions = versions or {}
        self.taken = taken
        self.parameters_from = source


def test_a_flight_record_carries_its_own_provenance(tmp_path):
    """A parameter dump that cannot say where it came from cannot be compared.

    The file this replaced held `{"count": 1020, "parameters": {…}}` and not
    one word about the vehicle, the time, or the log it was read from.
    """
    record = flightfile.build(
        flight_id="2026-09-11_1314", started=T0.timestamp(),
        ended=T0.timestamp() + 600, reason="disarmed", rows=600,
        computer="L342-D", host="192.168.2.2", interface="Network Bridge",
        opening=_Snap({"A": 1.0}, {"blueos": "1.5.0", "containers": [{"name": "core", "image": "i"}]}),
        closing=_Snap({"A": 2.0}, {"blueos": "1.5.0", "containers": [{"name": "core", "image": "i"}]},
                      taken=T0.timestamp() + 600, source="00000002.BIN"),
        brief_disarms=[], capabilities={}, site="OTS")
    assert record["schema"].startswith("utc.flight/")
    assert record["vehicle"]["host"] == "192.168.2.2"
    assert record["computer"]["name"] == "L342-D"
    assert record["parameters"]["read_from"] == "00000002.BIN"
    assert record["parameters"]["taken"]
    assert record["started"] and record["ended"]
    path = flightfile.write(tmp_path, record)
    assert json.loads(path.read_text())["flight_id"] == "2026-09-11_1314"


def test_a_record_says_why_a_reading_is_absent(tmp_path):
    record = flightfile.build(
        flight_id="2026-09-11_1314", started=T0.timestamp(),
        ended=T0.timestamp() + 60, reason="disarmed", rows=60,
        computer="x", host="h", interface="i",
        opening=_Snap({}, {}), closing=_Snap({}, {"blueos": "", "containers": []}),
        brief_disarms=[], capabilities={})
    assert record["versions"]["read"] is False
    assert record["versions"]["values"] is None
    assert "did not answer" in record["versions"]["why_absent"]


def test_the_previous_flight_is_found_by_its_id(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for flight_id in ("2026-09-10_0900", "2026-09-11_1314", "2026-09-11_1422"):
        flightfile.write(logs, {"flight_id": flight_id, "parameters": {},
                                "versions": {}})
    found = flightfile.find_previous(logs, "2026-09-11_1422")
    assert found is not None and "1314" in found.name
    found = flightfile.find_previous(logs, "2026-09-10_0900")
    assert found is None, "nothing precedes the first flight"


def test_comparing_with_the_previous_flight_finds_a_turned_knob(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    flightfile.write(logs, {
        "flight_id": "2026-09-11_1314",
        "ended": "2026-09-11T20:40:00+00:00",
        "parameters": {"read": True,
                       "values": {"SURFTRAK_DEPTH": -1.75, "ACRO_EXPO": 0.3}},
        "versions": {"read": True, "values": {"blueos": "1.5.0",
                                              "containers": [{"name": "core", "image": "i"}]}}})
    earlier = flightfile.find_previous(logs, "2026-09-11_1422")
    found = flightfile.compare_with_previous(
        earlier,
        {"SURFTRAK_DEPTH": -0.8, "ACRO_EXPO": 0.30000001192092896},
        {"blueos": "1.5.0", "containers": [{"name": "core", "image": "i"}]})
    assert list(found["parameters_by_operator"]) == ["SURFTRAK_DEPTH"], \
        "float32 noise was reported as a change"
    assert found["flight_id"] == "2026-09-11_1314"
