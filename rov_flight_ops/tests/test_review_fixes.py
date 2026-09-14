"""
The failure cases from the independent evaluation of 13 September 2026.

Each test here reproduces one scenario the review demonstrated with its own
fault probes, and holds the repaired behaviour: a delete that cannot establish
a safe, current state does nothing; C3 imagery never sweeps in files beside
it; a recording never overwrites another or fails silently; a flight keeps its
own folder and vehicle; a changed recording is not read from an old cache; an
estimated span never decides a deletion; a copy is only "verified" when it was
verified; and a disarm is only attributed with evidence at the ending.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest
from test_mcap_recovery import write_mcap
from test_pifiles import (  # noqa: F401
    BIN_DIR,
    C3,
    REC,
    START,
    dataflash,
    mcap_head,
    vehicle,
)

from rov_flight_ops import blueos, flightlog, flightscan, laptop, mcap_extract
from rov_flight_ops import pifiles as PF

ALL = [c.key for c in PF.CATEGORIES]


# --------------------------------------------------------------------------
#  F01  cleanup needs a fresh, confirmed safe state
# --------------------------------------------------------------------------


def _bins(v):
    inv = PF.search(v.host, ["bin"])
    return inv, [f for f in inv.files["bin"] if f.rel == "00000081.BIN"]


def _no_deletes(v):
    return not any(m == "DELETE" for m, _ in v.seen)


def test_an_unanswered_heartbeat_deletes_nothing(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    vehicle.heartbeat_down = True
    with pytest.raises(PF.Refused, match="could not be read"):
        PF.delete(target, inv, record_dir=tmp_path)
    assert _no_deletes(vehicle)


def test_a_stale_cached_heartbeat_is_not_taken_as_disarmed(vehicle, tmp_path):  # noqa: F811
    """mavlink2rest serves the last heartbeat it had, however old. One whose
    counter never moves belongs to an autopilot that has stopped talking."""
    inv, target = _bins(vehicle)
    vehicle.heartbeat_stale = True
    with pytest.raises(PF.Refused, match="no new heartbeat"):
        PF.delete(target, inv, record_dir=tmp_path, confirm=_confirm_fast)
    assert _no_deletes(vehicle)


def _confirm_fast(host):
    return blueos.confirm_disarmed(host, within_s=1.0, poll_s=0.2)


def test_a_mavlink2rest_that_cannot_prove_freshness_is_refused(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    vehicle.heartbeat_status = False
    with pytest.raises(PF.Refused, match="cannot be confirmed"):
        PF.delete(target, inv, record_dir=tmp_path)
    assert _no_deletes(vehicle)


def test_an_unreadable_vehicle_clock_deletes_nothing(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    vehicle.clock_down = True
    with pytest.raises(PF.Refused, match="clock"):
        PF.delete(target, inv, record_dir=tmp_path)
    assert _no_deletes(vehicle)


def test_a_file_changed_since_the_search_is_left_alone(vehicle, tmp_path):  # noqa: F811
    """The repair sweep rewrote it after it was listed: fresh metadata wins."""
    inv, target = _bins(vehicle)
    entry = vehicle.fs[f"{BIN_DIR}/00000081.BIN"]
    entry["data"] = entry["data"] + b"\x00" * 30
    rep = PF.delete(target, inv, record_dir=tmp_path)
    assert not rep.done and rep.reasons[target[0].path] == "changed since it was listed"
    assert _no_deletes(vehicle)


def test_a_file_with_no_modification_time_is_left_alone(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    target[0].modified = None
    rep = PF.delete(target, inv, record_dir=tmp_path)
    assert not rep.done and _no_deletes(vehicle)


def test_arming_part_way_through_a_batch_stops_it(vehicle, tmp_path, monkeypatch):  # noqa: F811
    for i in range(4):
        vehicle.add(f"{BIN_DIR}/0000009{i}.BIN", dataflash(10),
                    START - 3600 * (i + 1))
    inv = PF.search(vehicle.host, ["bin"])
    old = [f for f in inv.files["bin"] if f.rel.startswith("0000009")]
    monkeypatch.setattr(PF, "RECHECK_EVERY_S", 0.0)

    def arm_after_first(_path):
        vehicle.armed = True

    vehicle.on_delete = arm_after_first
    rep = PF.delete(old, inv, record_dir=tmp_path, confirm=_confirm_fast)
    assert len(rep.done) == 1, rep.summary()
    assert "armed" in rep.stopped
    assert sum(1 for m, _ in vehicle.seen if m == "DELETE") == 1
    text = rep.log_path.read_text(encoding="utf-8")
    assert text.count("not tried") == 3


def test_the_record_is_written_before_the_first_delete(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    seen_at_delete = {}

    def check(_path):
        seen_at_delete["record"] = sorted(tmp_path.glob("pi_cleanup_*.txt"))
        seen_at_delete["text"] = seen_at_delete["record"][0].read_text(encoding="utf-8")

    vehicle.on_delete = check
    PF.delete(target, inv, record_dir=tmp_path)
    assert seen_at_delete["record"], "no record existed when the delete was sent"
    assert "target" in seen_at_delete["text"] and target[0].path in seen_at_delete["text"]


def test_no_record_means_no_deletion(vehicle, tmp_path):  # noqa: F811
    inv, target = _bins(vehicle)
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("a file where the record folder should be")
    with pytest.raises(PF.Refused, match="record"):
        PF.delete(target, inv, record_dir=blocker / "logs")
    assert _no_deletes(vehicle)


# --------------------------------------------------------------------------
#  F02  C3 discovery stays inside Madrona's folders
# --------------------------------------------------------------------------


def test_two_c3_sets_are_listed_separately_and_nothing_beside_them(vehicle):  # noqa: F811
    other = "/system_root/root/c3_sessions/survey_2"
    for eye in ("left", "right", "center"):
        vehicle.add(f"{other}/{eye}/img_0001.jpg", b"x" * 100, START)
    # Things that sit near the imagery and are not imagery.
    vehicle.add("/system_root/usr/blueos/userdata/madrona/config.json", b"{}", START)
    vehicle.add(f"{C3}/notes/field_notes.txt", b"n", START)

    inv = PF.search(vehicle.host, ["c3"])
    assert inv.roots["c3"] == sorted([C3, other])
    paths = {f.path for f in inv.files["c3"]}
    assert "/system_root/usr/blueos/userdata/madrona/config.json" not in paths
    assert f"{C3}/notes/field_notes.txt" not in paths
    assert f"{C3}/calibration.yaml" in paths, "the calibration file is kept"
    # Two sessions never land in the same folder.
    rels = [f.rel for f in inv.files["c3"]]
    assert len(rels) == len(set(rels))


def test_a_typed_folder_that_is_far_too_broad_is_refused(vehicle):  # noqa: F811
    inv = PF.search(vehicle.host, ["c3"], c3_folder="/system_root/usr/blueos/userdata")
    assert inv.files["c3"] == [] and inv.roots["c3"] == []
    assert any("too broad" in n for n in inv.notes["c3"])


def test_a_typed_folder_without_left_right_center_lists_nothing(vehicle):  # noqa: F811
    inv = PF.search(vehicle.host, ["c3"], c3_folder=BIN_DIR)
    assert inv.files["c3"] == []


def test_a_file_outside_its_types_folder_is_never_deleted(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["c3"])
    stray = PF.PiFile("c3", "/system_root/usr/blueos/userdata/madrona/config.json",
                      "config.json", size=2, modified=START)
    vehicle.add(stray.path, b"{}", START)
    rep = PF.delete([stray], inv, record_dir=tmp_path)
    assert not rep.done and "outside" in rep.reasons[stray.path]
    assert stray.path in vehicle.fs


# --------------------------------------------------------------------------
#  F03  two recordings in one minute do not overwrite each other
# --------------------------------------------------------------------------


def test_flight_ids_carry_seconds_and_never_repeat_in_a_folder(tmp_path):
    t = time.mktime((2026, 9, 13, 13, 5, 12, 0, 0, -1))
    first = flightlog.unique_flight_id(tmp_path, t)
    assert first.endswith("_130512")
    (tmp_path / f"laptop_monitor_{first}.csv").write_text("marker")
    second = flightlog.unique_flight_id(tmp_path, t)
    assert second != first and second.startswith(first)
    assert (tmp_path / f"laptop_monitor_{first}.csv").read_text() == "marker"


def test_the_flight_summary_still_links_a_flights_files():
    for name, want in (("laptop_monitor_2026-09-11_1314.csv", "2026-09-11_1314"),
                       ("laptop_monitor_2026-09-13_130512.csv", "2026-09-13_130512"),
                       ("network_pings_2026-09-13_130512-2.csv", "2026-09-13_130512-2")):
        assert flightscan.FLIGHT_ID.search(name).group(1) == want


# --------------------------------------------------------------------------
#  F04  a failed write is a visible failure
# --------------------------------------------------------------------------


class _BrokenWriter:
    def writerow(self, _row):
        raise OSError("disk full")


def _open_session(rec, folder: Path, **kw):
    """A flight in progress, without the sampler or the vehicle behind it."""
    session = flightlog._Session(1, "2026-09-13_130512", folder / "logs", folder,
                                 rec.host, kw.get("manual", False))
    rec._session = session
    rec.status.state = "recording"
    return session


def test_a_write_failure_is_shown_and_counted(tmp_path):
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    session = _open_session(rec, tmp_path)
    session.writer = _BrokenWriter()
    rec._write_row(session, dict.fromkeys(laptop.COLUMNS))
    rec._write_row(session, dict.fromkeys(laptop.COLUMNS))
    assert rec.status.problem.startswith("RECORDING FAILED")
    assert "disk full" in rec.status.problem
    assert rec.status.dropped == 2 and rec.status.rows == 0
    assert rec.status.line() == rec.status.problem


def test_a_recovered_disk_clears_the_alarm_but_keeps_the_count(tmp_path):
    rec = flightlog.FlightRecorder(host="test", flight_dir=tmp_path)
    session = _open_session(rec, tmp_path)
    session.writer = _BrokenWriter()
    rec._write_row(session, {})
    out = tmp_path / "rows.csv"
    session.fh = out.open("w", newline="", encoding="utf-8")
    session.writer = csv.DictWriter(session.fh, fieldnames=["a"], extrasaction="ignore")
    rec._write_row(session, {"a": 1})
    session.fh.close()
    assert rec.status.problem == ""
    assert "1 row(s) could not be written" in rec.status.note
    assert rec.status.last_write > 0


# --------------------------------------------------------------------------
#  F05 and F07  a flight keeps its own folder and its own vehicle
# --------------------------------------------------------------------------


def test_the_open_flight_keeps_its_folder_when_the_window_moves_on(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    rec = flightlog.FlightRecorder(host="test", flight_dir=a)
    session = _open_session(rec, a)
    rec.flight_dir = b                       # the operator chose another folder
    assert session.folder == a / "logs"
    assert rec._logs_dir() == b / "logs", "the next flight goes to the new folder"


def test_an_address_typed_while_idle_redirects_the_watcher():
    rec = flightlog.FlightRecorder(host="old-host")
    assert rec.retarget("new-host") is True
    assert rec.host == "new-host" and rec.status.host == "new-host"


def test_an_address_typed_during_a_flight_waits_for_it_to_close(tmp_path):
    rec = flightlog.FlightRecorder(host="old-host", flight_dir=tmp_path)
    session = _open_session(rec, tmp_path)
    assert rec.retarget("new-host") is False
    assert rec.host == "old-host" and rec.status.pending_host == "new-host"
    # The closing path applies it.
    rec._snapshot = lambda since_log="": flightlog.Snapshot()
    rec._write_files = lambda *a, **k: None
    assert rec.stop_manually(wait=True) is True
    assert session.done.is_set()
    assert rec.host == "new-host" and rec.status.pending_host == ""


# --------------------------------------------------------------------------
#  F06  a changed recording is not read from an old cache
# --------------------------------------------------------------------------


def _rows(csv_path: Path) -> int:
    with open(csv_path, encoding="utf-8") as fh:
        return sum(1 for _ in fh) - 1


def test_a_replaced_recording_rebuilds_the_cache(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    write_mcap(src, seconds=2)
    short = _rows(mcap_extract.extract([src], cache).telemetry_csv)
    write_mcap(src, seconds=10)                 # recopied, same path
    longer = _rows(mcap_extract.extract([src], cache).telemetry_csv)
    assert longer > short * 3, (short, longer)


def test_an_unchanged_recording_is_still_a_cache_hit(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    write_mcap(src, seconds=3)
    mcap_extract.extract([src], cache)
    said = []
    mcap_extract.extract([src], cache, progress=lambda f, m="": said.append(m))
    assert "telemetry cache hit" in said


def test_a_cache_missing_a_product_is_rebuilt(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    write_mcap(src, seconds=3)
    res = mcap_extract.extract([src], cache)
    res.telemetry_csv.unlink()
    again = mcap_extract.extract([src], cache)
    assert again.telemetry_csv.is_file() and _rows(again.telemetry_csv) > 0


def test_a_marker_from_an_older_extractor_is_not_trusted(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    write_mcap(src, seconds=3)
    mcap_extract.extract([src], cache)
    marker = cache / "extract.json"
    old = json.loads(marker.read_text())
    old.pop("schema")
    old.pop("sources")
    marker.write_text(json.dumps(old))
    said = []
    mcap_extract.extract([src], cache, progress=lambda f, m="": said.append(m))
    assert "telemetry cache hit" not in said
    assert json.loads(marker.read_text())["schema"] == mcap_extract.CACHE_SCHEMA


def test_two_programs_cannot_extract_one_flight_at_once(tmp_path):
    src = tmp_path / "recorder_20260831_165829.mcap"
    cache = tmp_path / "cache"
    cache.mkdir()
    write_mcap(src, seconds=2)
    # Someone else holds it. (Across real processes: test_resilience.)
    with mcap_extract._CacheLock(cache), pytest.raises(mcap_extract.ExtractionBusy):
        mcap_extract.extract([src], cache)


# --------------------------------------------------------------------------
#  F08  mcap end times come from the file, and estimates are labelled
# --------------------------------------------------------------------------


def test_a_closed_recording_s_end_is_read_from_its_summary(vehicle, tmp_path):  # noqa: F811
    real = tmp_path / "real.mcap"
    write_mcap(real, seconds=600)               # ten minutes, a few MB at most
    vehicle.add(f"{REC}/recorder_20260831_165829.mcap", real.read_bytes(), START)
    inv = PF.search(vehicle.host, ["mcap"])
    f = next(x for x in inv.files["mcap"] if "20260831" in x.rel)
    assert not f.end_estimated
    assert f.end - f.start == pytest.approx(599, abs=1), \
        "a size estimate would put this 600 s file at a fraction of a second"


def test_a_recording_with_no_summary_is_marked_estimated(vehicle):  # noqa: F811
    inv = PF.search(vehicle.host, ["mcap"])
    f = next(x for x in inv.files["mcap"] if "20260911" in x.rel)
    assert f.end_estimated


def test_an_estimated_span_never_decides_a_deletion(vehicle):  # noqa: F811
    inv = PF.search(vehicle.host, ["mcap"])
    PF.match(inv.all_files(), [("T1", START - 100, START + 700)])
    download = PF.choose(inv, ["mcap"], PF.PERIOD_TRANSECTS)
    clean = PF.choose(inv, ["mcap"], PF.PERIOD_TRANSECTS, destructive=True)
    assert any("20260911" in f.rel for f in download.files)
    assert not any("20260911" in f.rel for f in clean.files)
    assert clean.estimated >= 1


# --------------------------------------------------------------------------
#  F09  "verified" means verified
# --------------------------------------------------------------------------


def test_a_download_is_recorded_and_verified(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    target = [f for f in inv.files["bin"] if f.rel == "00000081.BIN"]
    PF.download(target, tmp_path, inv.host, inv.token, vehicle=inv.vehicle)
    assert PF.copy_state(target[0], tmp_path) == "verified"
    entry = PF.load_manifest(tmp_path)[target[0].path]
    assert len(entry["sha256"]) == 64 and entry["vehicle"] == "Nereo"


def test_a_same_size_file_copied_by_hand_is_not_called_verified(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    f = next(x for x in inv.files["bin"] if x.rel == "00000081.BIN")
    dest = f.dest_in(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"\x00" * f.size)          # same length, wrong bytes
    assert PF.copy_state(f, tmp_path) == "same size"
    rep = PF.download([f], tmp_path, inv.host, inv.token)
    assert any("not verified" in w for w in rep.warnings)


def test_a_different_size_copy_is_replaced(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    f = next(x for x in inv.files["bin"] if x.rel == "00000081.BIN")
    dest = f.dest_in(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"short")
    assert PF.copy_state(f, tmp_path) == "differs"
    rep = PF.download([f], tmp_path, inv.host, inv.token)
    assert rep.done and PF.copy_state(f, tmp_path) == "verified"


def test_mcap_head_helper_is_still_an_mcap():
    assert mcap_head(START).startswith(blueos.MCAP_MAGIC)
