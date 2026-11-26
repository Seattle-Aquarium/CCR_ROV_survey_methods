"""
The November 2026 field refinements: tlogs read wherever mcaps were, the
recording setup on Monitoring, C3 imagery filed by transect, logging profiles,
the logs folder's subfolders, and the shelf.

The tlogs here are built frame by frame with pymavlink -- an 8-byte big-endian
microsecond timestamp before each MAVLink frame, which is all a tlog is -- so
every reader is exercised against bytes in the real format.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from rov_flight_ops import (
    discovery,
    flightscan,
    logpaths,
    logprofile,
    shelf,
    tlog_extract,
)
from rov_flight_ops import (
    pifiles as PF,
)
from rov_flight_ops import (
    recordingsetup as RS,
)

T0 = 1_795_000_000.0        # 2026-11-18, near enough


# --------------------------------------------------------------------------
#  a tlog, written frame by frame
# --------------------------------------------------------------------------


def write_tlog(path: Path, *, armed_spans=((60, 300),), until: float = 400.0,
               texts=(), gcs=(255, 240)) -> Path:
    """A tlog of `until` seconds: heartbeats at 1 Hz (armed inside each span),
    depth at 5 Hz, SYSTEM_TIME at 1 Hz, a ground station's heartbeat at 1 Hz,
    and STATUSTEXTs at the times given."""
    from pymavlink.dialects.v20 import ardupilotmega as mavlink

    ap = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    gs = mavlink.MAVLink(None, srcSystem=gcs[0], srcComponent=gcs[1])
    frames: list[tuple[float, bytes]] = []
    t = 0.0
    while t <= until:
        if abs(t - round(t)) < 1e-9:
            armed = any(a <= t < b for a, b in armed_spans)
            base = 81 | (128 if armed else 0)
            frames.append((t, ap.heartbeat_encode(12, 3, base, 19, 4).pack(ap)))
            frames.append((t, gs.heartbeat_encode(6, 8, 0, 0, 4).pack(gs)))
            frames.append((t, ap.system_time_encode(int((T0 + t) * 1e6),
                                                    int(t * 1000) + 5000).pack(ap)))
        depth_mm = -int(1000 * min(10.0, max(0.0, t - 60)))   # 10 m by t=70
        frames.append((t, ap.global_position_int_encode(
            int(t * 1000), 0, 0, 0, depth_mm, 0, 0, 0, 0).pack(ap)))
        t = round(t + 0.2, 6)
    for when, text in texts:
        frames.append((when, ap.statustext_encode(4, text.encode()).pack(ap)))
    frames.sort(key=lambda f: f[0])
    with open(path, "wb") as fh:
        for when, buf in frames:
            fh.write(struct.pack(">Q", int((T0 + when) * 1e6)) + buf)
    return path


@pytest.fixture
def tlog(tmp_path) -> Path:
    pytest.importorskip("pymavlink")
    folder = tmp_path / "flight" / "logs" / "tlog"
    folder.mkdir(parents=True)
    return write_tlog(folder / "00044-2026-11-18_20-00-00.tlog",
                      texts=((299.5, "GCS Failsafe"),))


# --------------------------------------------------------------------------
#  tlogs
# --------------------------------------------------------------------------


def test_a_tlogs_span_comes_from_its_first_and_last_headers(tlog):
    start, end = tlog_extract.tlog_span(tlog)
    assert start == pytest.approx(T0)
    assert end == pytest.approx(T0 + 400.0, abs=0.3)


def test_discovery_finds_tlogs_and_uses_them_when_there_is_no_mcap(tlog):
    disc = discovery.discover(tlog.parents[2])
    assert disc.tlogs == [tlog]
    assert disc.telemetry == [tlog]
    assert disc.telemetry_kind == "tlog"
    assert "no .mcap" not in " ".join(disc.warnings)


def test_a_tlog_is_read_into_the_same_long_csv_an_mcap_is(tlog, tmp_path):
    from rov_flight_ops.telemetry import TelemetryStore

    ex = tlog_extract.extract([tlog], tmp_path / "cache")
    store = TelemetryStore.load(ex.telemetry_csv)
    # 10 m down from seventy seconds in, as written.
    assert store.sample(T0 + 120)["depth"] == pytest.approx(10.0)
    assert "SAFETY_ARMED" in store.get("HEARTBEAT.base_mode", T0 + 100)
    assert "SAFETY_ARMED" not in store.get("HEARTBEAT.base_mode", T0 + 350)
    # The ground station's heartbeat is not taken for the autopilot's.
    assert store.num("HEARTBEAT.custom_mode", T0 + 100) == 19


def test_a_second_read_is_a_cache_hit_and_a_changed_tlog_is_not(tlog, tmp_path):
    cache = tmp_path / "cache"
    tlog_extract.extract([tlog], cache)
    said = []
    tlog_extract.extract([tlog], cache, progress=lambda f, m="": said.append(m))
    assert said == ["telemetry cache hit"]
    with open(tlog, "ab") as fh:
        fh.write(b"\x00" * 8)
    said.clear()
    tlog_extract.extract([tlog], cache, progress=lambda f, m="": said.append(m))
    assert "telemetry cache hit" not in said


def test_the_preview_reads_a_tlog_when_the_flight_has_no_mcap(tlog, tmp_path,
                                                              monkeypatch):
    from rov_flight_ops import previewsource
    from rov_flight_ops.config import AppConfig

    cfg = AppConfig()
    monkeypatch.setattr(cfg, "cache_root", tmp_path / "cache", raising=False)
    store, notes, where = previewsource.preview_store(
        tlog.parents[2], [("T1", T0 + 100, T0 + 200)], cfg=cfg, source="folder")
    assert "tlog" in where
    assert store.sample(T0 + 150)["depth"] == pytest.approx(10.0)


def test_a_tlog_is_cut_into_one_recording_per_armed_period(tmp_path):
    pytest.importorskip("pymavlink")
    path = write_tlog(tmp_path / "t.tlog", armed_spans=((20, 80), (120, 390)),
                      until=400, texts=((79.5, "Disarming motors"),))
    recs = flightscan.read_tlog(path)
    assert len(recs) == 2
    first, second = recs
    assert first.armed_from == pytest.approx(T0 + 20)
    assert first.closed and first.end == pytest.approx(T0 + 80)
    assert first.gcs == "255/240"
    assert any("Disarming" in text for _t, _s, text in first.statustexts)
    assert "armed" in first.name and first.kind == "tlog"
    assert second.closed
    assert first.size_bytes > 0 and second.size_bytes == 0     # counted once


def test_a_tlog_that_ends_armed_is_reported_as_not_closed(tmp_path):
    pytest.importorskip("pymavlink")
    path = write_tlog(tmp_path / "t.tlog", armed_spans=((20, 1e9),), until=200)
    (rec,) = flightscan.read_tlog(path)
    assert not rec.closed


def test_a_bench_tlog_that_never_armed_is_no_recording(tmp_path):
    pytest.importorskip("pymavlink")
    path = write_tlog(tmp_path / "t.tlog", armed_spans=(), until=100)
    assert flightscan.read_tlog(path) == []


def test_the_flight_report_reads_tlogs_and_names_a_failsafe(tlog):
    from rov_flight_ops import flightreport

    day = flightscan.scan(tlog.parents[2])
    assert day.telemetry_kind == "tlog"
    assert len(day.recordings) == 1
    report = flightreport.analyze(day)
    assert [d.cause for d in report.disarms] == ["gcs failsafe"]
    assert "MB" in report.headline


# --------------------------------------------------------------------------
#  the logs folder's subfolders
# --------------------------------------------------------------------------


def test_logs_are_found_in_their_subfolder_and_loose_in_older_flights(tmp_path):
    logs = tmp_path / "logs"
    (logs / "network").mkdir(parents=True)
    old = logs / "network_pings_2026-09-01_101010.csv"
    new = logs / "network" / "network_pings_2026-11-24_131501.csv"
    old.write_text("x")
    new.write_text("x")
    assert logpaths.find(logs, logpaths.NETWORK, "network_pings_*.csv") == [old, new]


def test_a_flight_id_is_unique_across_the_subfolders(tmp_path):
    from rov_flight_ops import flightlog

    logs = tmp_path / "logs"
    (logs / "laptop").mkdir(parents=True)
    when = 1_795_000_000.0
    first = flightlog.unique_flight_id(logs, when)
    (logs / "laptop" / f"laptop_monitor_{first}.csv").write_text("x")
    assert flightlog.unique_flight_id(logs, when) == f"{first}-2"


# --------------------------------------------------------------------------
#  what the vehicle will record: the router check
# --------------------------------------------------------------------------


def _setup(**kw) -> RS.RecordingSetup:
    s = RS.RecordingSetup(**kw)
    s.expects = RS.expected_format(s.blueos)
    RS.judge(s)
    return s


def test_blueos_1_5_records_mcaps_and_1_4_tlogs():
    assert RS.expected_format("1.5.0-beta.39") == "mcap"
    assert RS.expected_format("v1.4.6") == "tlog"
    assert RS.expected_format("") == ""


def test_a_1_4_vehicle_on_mavlink_server_records_nothing_and_says_so():
    s = _setup(blueos="1.4.6", router="MAVLink-Server",
               routers=["MAVLinkRouter", "MAVLink-Server", "MAVP2P"])
    assert s.level == RS.BAD
    assert s.fix_router == "MAVLinkRouter"
    assert "writes none" in s.summary


def test_a_1_4_vehicle_on_mavlinkrouter_with_a_growing_tlog_is_fine():
    s = _setup(blueos="1.4.6", router="MAVLinkRouter", newest="00044.tlog",
               newest_age_s=3.0)
    assert s.level == RS.OK and not s.fix_router
    assert "growing" in s.summary


def test_tlog_mode_off_is_caught_on_releases_that_have_it():
    s = _setup(blueos="1.4.8", router="MAVLinkRouter", tlog_mode="disabled")
    assert s.level == RS.BAD and "OFF" in s.summary


def test_the_router_is_not_switched_unless_the_rov_is_confirmed_disarmed(monkeypatch):
    posted = []
    monkeypatch.setattr(RS.blueos, "confirm_disarmed",
                        lambda host: (False, "the ROV is armed"))
    monkeypatch.setattr(RS, "_post", lambda url, timeout=20.0: posted.append(url) or (True, ""))
    monkeypatch.setattr(RS.blueos, "_get", lambda *a, **k: RS.blueos.Answer(
        url="", ok=True, status=200, body='["MAVLinkRouter","MAVLink-Server"]'))
    done, why = RS.set_router("192.0.2.1", "MAVLinkRouter")
    assert not done and "armed" in why
    assert posted == []


def test_only_a_tlog_writing_router_can_be_switched_to(monkeypatch):
    done, why = RS.set_router("192.0.2.1", "MAVP2P")
    assert not done and "not a router" in why


# --------------------------------------------------------------------------
#  C3 imagery filed by transect
# --------------------------------------------------------------------------


def _c3(rel: str, size: int = 10, modified: float | None = None) -> PF.PiFile:
    return PF.PiFile("c3", f"/pi/c3/{rel}", rel, size, modified)


WINDOWS = [("T1", 1795555200.0, 1795555580.0)]       # 21:20:00 to 21:26:20 UTC


def test_a_c3_image_is_timed_by_the_utc_stamp_in_its_name():
    f = _c3("left/snapshot-2026-11-24-21-19-48-568789.jpg", modified=1.0)
    assert PF.c3_time(f) == pytest.approx(1795555188.568789, abs=1e-3)
    assert PF.c3_time(_c3("left/other.jpg", modified=42.0)) == 42.0


def test_c3_imagery_is_routed_three_ways():
    inside = _c3("left/snapshot-2026-11-24-21-21-00-000000.jpg")
    outside = _c3("left/snapshot-2026-11-24-21-40-00-000000.jpg")
    cal = _c3("calibration.json")
    files = [inside, outside, cal]

    every = {f.rel: f.targets for f in PF.route_c3(files, PF.C3_ALL, WINDOWS)}
    assert every[inside.rel] == [inside.rel, f"transects/T1/{inside.rel}"]
    assert every[outside.rel] == [outside.rel]
    assert every["calibration.json"] == ["calibration.json",
                                         "transects/T1/calibration.json"]

    only = {f.rel: f.targets for f in PF.route_c3(files, PF.C3_TRANSECTS, WINDOWS)}
    assert outside.rel not in only                    # left on the vehicle
    assert only[inside.rel] == [f"transects/T1/{inside.rel}"]

    sorted_ = {f.rel: f.targets for f in PF.route_c3(files, PF.C3_SORTED, WINDOWS)}
    assert sorted_[outside.rel] == [f"off_transect/{outside.rel}"]
    assert "off_transect/calibration.json" in sorted_["calibration.json"]
    # The listing's own entries are never changed.
    assert inside.targets == []


def test_two_sites_with_a_t1_each_get_two_folders():
    windows = [("T1", 0.0, 10.0), ("T1", 20.0, 30.0)]
    a = _c3("left/a.jpg", modified=5.0)
    b = _c3("left/b.jpg", modified=25.0)
    got = PF.c3_assign([a, b], windows)
    assert got == {a.path: "T1", b.path: "T1_2"}


def _write_downloaded(flight: Path, f: PF.PiFile, data: bytes) -> None:
    dest = f.dest_in(flight)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    entry = {"pi_path": f.path, "local": str(dest.relative_to(flight)),
             "category": "c3", "size": len(data), "modified": f.modified,
             "sha256": hashlib.sha256(data).hexdigest()}
    manifest = flight / PF.MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def test_imagery_already_downloaded_is_copied_into_its_transect_not_fetched(tmp_path):
    data = b"jpegbytes"
    f = _c3("left/snapshot-2026-11-24-21-21-00-000000.jpg", size=len(data),
            modified=1795555260.0)
    _write_downloaded(tmp_path, f, data)
    (routed,) = PF.route_c3([f], PF.C3_ALL, WINDOWS)
    assert PF.pending([routed], tmp_path) == [routed]
    rep = PF.download([routed], tmp_path, "192.0.2.1", "no-token")
    assert rep.copied == 1 and not rep.failed and not rep.done
    copy = tmp_path / "photos/C3/transects/T1" / f.rel
    assert copy.read_bytes() == data
    assert PF.pending([routed], tmp_path) == []
    # And the listing still calls the file verified.
    assert PF.copy_state(f, tmp_path) == "verified"


def test_a_file_filed_only_into_a_transect_folder_still_reads_as_verified(tmp_path):
    data = b"x" * 5
    f = _c3("left/snapshot-2026-11-24-21-21-00-000000.jpg", size=5, modified=7.0)
    (routed,) = PF.route_c3([f], PF.C3_TRANSECTS, WINDOWS)
    _write_downloaded(tmp_path, routed, data)
    # The listing's entry knows nothing of transects; the manifest does.
    assert PF.copy_state(f, tmp_path) == "verified"


# --------------------------------------------------------------------------
#  logging profiles, and the shelf
# --------------------------------------------------------------------------


def test_standard_is_the_default_and_unknown_names_fall_back_to_it():
    assert logprofile.from_settings({}).key == logprofile.STANDARD
    assert logprofile.get("INTENSIVE").key == logprofile.INTENSIVE
    assert logprofile.get("nonsense").key == logprofile.STANDARD


def test_standard_logging_writes_no_byte_exact_dvl_stream(tmp_path):
    from rov_flight_ops.dvl import capture as CAP

    cap = CAP.DvlCapture(folder=tmp_path, vehicle=False,
                         profile=logprofile.get(logprofile.STANDARD))
    cap.stem = "dvl_test"
    raw = CAP._Sink(cap, "tcp_raw", binary=True,
                    enabled=cap.profile.dvl_raw_stream)
    rows = CAP._Sink(cap, "velocity", header=["a"])
    assert raw.path is None and not raw.write(b"bytes")
    assert rows.path is not None and rows.path.is_file()
    rows.close()
    assert cap.status_period_s() == 10.0
    full = CAP.DvlCapture(folder=None, vehicle=False)
    assert full.status_period_s() == CAP.STATUS_PERIOD_S


def test_the_shelf_hides_by_default_and_unshelve_brings_back():
    for key in shelf.SHELVED:
        assert not shelf.enabled({}, key)
        assert shelf.enabled({"unshelve": [key]}, key)
    assert shelf.enabled({}, "monitoring")          # not on the shelf at all
    assert shelf.unshelved({"unshelve": "flight_summary"}) == {"flight_summary"}


def test_every_shelved_module_is_still_there():
    pkg = Path(shelf.__file__).parent
    for item in shelf.SHELVED.values():
        for rel in item.modules:
            assert (pkg / rel).is_file(), rel


def test_the_window_opens_without_the_shelved_tabs(app):
    sections = list(app.nav.sections)
    assert "Flight summary" not in sections and "Analyze transects" not in sections
    nav = app.pages["navigation"]
    assert nav.plan_shelved and nav.plan_card is None
    # The heads-up display heads the right-hand column, the receivers under it.
    assert nav.numbers.master is nav.side_col
    assert int(nav.numbers.grid_info()["row"]) == 0
    assert int(nav.ugps_strip.grid_info()["row"]) == 1


def test_the_parameters_tables_say_which_column_is_the_rov(app):
    from rov_flight_ops.gui import paramspage
    heads = paramspage.column_heads()
    texts = [c[0] for c in heads["cells"]]
    assert paramspage.HEAD_ROV in texts and paramspage.HEAD_REFERENCE in texts
    assert texts.index(paramspage.HEAD_ROV) < texts.index(paramspage.HEAD_REFERENCE)


if os.name != "nt":                                  # pragma: no cover
    pytestmark = pytest.mark.skip("the program is Windows-only")
