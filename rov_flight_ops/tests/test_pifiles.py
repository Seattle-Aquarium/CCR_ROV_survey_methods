"""
The files on the Pi: listing, when each was recorded, choosing, fetching, and
deleting -- against a small fake BlueOS whose File Browser keeps an in-memory
filesystem, so a delete can be seen to happen and a refused one to not.

What these pin down is the part that must not go wrong in the field: a
delete is refused while the vehicle is armed, never touches a file that is
still being written, removes only what was chosen, and leaves a record.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from rov_flight_ops import blueos, previewsource
from rov_flight_ops import pifiles as PF

TOKEN = "fake.jwt.token"
REC = "/system_root/usr/blueos/userdata/recorder"
BIN_DIR = "/ardupilot_logs/firmware/logs"
C3 = "/system_root/usr/blueos/userdata/madrona/survey_1"

#: 2026-09-11 18:49:53 UTC, the stamp in the recording's name.
START = 1_789_152_593.0


def mcap_head(start_s: float) -> bytes:
    out = bytearray(blueos.MCAP_MAGIC)
    out += bytes([0x01]) + struct.pack("<Q", 4) + b"prof"
    out += bytes([blueos.OP_CHUNK]) + struct.pack("<Q", 40)
    out += struct.pack("<Q", int(start_s * 1e9)) + bytes(32)
    return bytes(out)


def dataflash(seconds: float, rate_hz: int = 100, alt: float = -5.0) -> bytes:
    """A minimal ArduPilot log: an FMT for CTUN, then CTUN records.

    Long enough that its tail is past the head the span reader fetches, so
    both halves of the reader are exercised.
    """
    fmt = struct.pack("<BB4s16s64s", 2, 3 + 8 + 4, b"CTUN", b"Qf",
                      b"TimeUS,Alt")
    out = bytearray(b"\xa3\x95\x80" + fmt)
    n = int(seconds * rate_hz)
    for i in range(n):
        us = 5_000_000 + i * (1_000_000 // rate_hz)
        out += b"\xa3\x95\x02" + struct.pack("<Qf", us, alt - (i % 50) * 0.01)
    return bytes(out)


def build_fs() -> dict[str, dict]:
    """path -> {"data": bytes, "modified": epoch} for files; dirs are implied."""
    # Taken per vehicle, not at import: a full test run is longer than the
    # "still being written" window, and a stamp from the start of it has aged.
    now = time.time()
    return {
        f"{REC}/recorder_20260911_184953.mcap":
            {"data": mcap_head(START) + bytes(8000), "modified": START + 900},
        f"{REC}/recorder_20260911_184953/stream_0.mp4":
            {"data": b"\x00\x00\x00\x18ftypmp42" + bytes(500), "modified": START + 905},
        f"{REC}/recorder_20260801_120000.mcap":
            {"data": mcap_head(1_785_585_600.0) + bytes(3000),
             "modified": START + 3000},                 # rewritten by the sweep
        f"{BIN_DIR}/00000081.BIN":
            {"data": dataflash(2_800), "modified": START + 2_830},
        f"{BIN_DIR}/00000082.BIN":                    # still being written
            {"data": dataflash(30), "modified": now - 5},
        f"{BIN_DIR}/LASTLOG.TXT": {"data": b"82", "modified": now - 5},
        f"{C3}/left/img_0001.jpg": {"data": b"L" * 900, "modified": START + 600},
        f"{C3}/right/img_0001.jpg": {"data": b"R" * 900, "modified": START + 600},
        f"{C3}/center/img_0001.jpg": {"data": b"C" * 900, "modified": START + 600},
        f"{C3}/calibration.yaml": {"data": b"k: 1\n", "modified": START - 86400},
    }


class Vehicle:
    def __init__(self):
        self.fs = build_fs()
        self.armed = False
        self.seen: list[tuple[str, str]] = []
        #: Folders exist in their own right, so one emptied by a delete still
        #: lists -- as nothing -- the way File Browser's does.
        self.dirs = {str(Path(p).parent).replace("\\", "/") for p in self.fs}
        for d in list(self.dirs):
            while d.count("/") > 1:
                d = d.rsplit("/", 1)[0]
                self.dirs.add(d)

    def listing(self, path: str):
        path = path.rstrip("/") or "/"
        if path not in self.dirs:
            return None
        prefix = path + "/"
        items: dict[str, dict] = {}
        for p, f in self.fs.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            name = rest.split("/", 1)[0]
            if "/" in rest:
                items.setdefault(name, {"name": name, "isDir": True, "size": 0,
                                        "path": prefix + name,
                                        "modified": "2026-09-11T18:00:00Z"})
            else:
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(f["modified"]))
                items[name] = {"name": name, "isDir": False, "size": len(f["data"]),
                               "path": p, "modified": stamp + ".123456789Z"}
        for d in self.dirs:
            if d.startswith(prefix) and "/" not in d[len(prefix):]:
                name = d[len(prefix):]
                items.setdefault(name, {"name": name, "isDir": True, "size": 0,
                                        "path": d, "modified": "2026-09-11T18:00:00Z"})
        return list(items.values())


def handler_for(v: Vehicle):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, val in (extra or {}).items():
                self.send_header(k, val)
            self.end_headers()
            self.wfile.write(body)

        def _fb_path(self, api):
            raw = self.path.split("?", 1)[0][len(api):]
            return urllib.parse.unquote(raw)

        def do_GET(self):
            v.seen.append(("GET", self.path))
            if self.path.startswith("/api/login"):
                return self._send(200, TOKEN.encode(), "text/plain")
            if self.path.startswith("/api/resources"):
                if self.headers.get("X-Auth") != TOKEN:
                    return self._send(401)
                items = v.listing(self._fb_path("/api/resources"))
                if items is None:
                    return self._send(404, b"404 Not Found", "text/plain")
                return self._send(200, json.dumps({"items": items}).encode())
            if self.path.startswith("/api/raw"):
                f = v.fs.get(self._fb_path("/api/raw"))
                if f is None:
                    return self._send(404)
                data = f["data"]
                rng = self.headers.get("Range")
                if rng:
                    lo, _, hi = rng.split("=")[1].partition("-")
                    lo, hi = int(lo), min(int(hi), len(data) - 1)
                    return self._send(206, data[lo:hi + 1], "application/octet-stream",
                                      {"Content-Range": f"bytes {lo}-{hi}/{len(data)}"})
                return self._send(200, data, "application/octet-stream")
            if self.path.endswith("/vehicle_name"):
                return self._send(200, b'"Nereo"')
            if self.path.endswith("/unix_time_seconds"):
                return self._send(200, str(time.time()).encode(), "text/plain")
            if self.path.endswith("/HEARTBEAT"):
                bits = 0b1000_0000 if v.armed else 0
                return self._send(200, json.dumps(
                    {"message": {"base_mode": {"bits": bits}}}).encode())
            return self._send(404)

        def do_DELETE(self):
            v.seen.append(("DELETE", self.path))
            if self.headers.get("X-Auth") != TOKEN:
                return self._send(401)
            path = self._fb_path("/api/resources").rstrip("/")
            if path in v.fs:
                del v.fs[path]
                return self._send(200)
            if v.listing(path) == []:
                v.dirs.discard(path)
                return self._send(200)
            return self._send(404)

        def do_POST(self):
            v.seen.append(("POST", self.path))
            return self._send(405)
    return H


@pytest.fixture()
def vehicle():
    v = Vehicle()
    srv = HTTPServer(("127.0.0.1", 0), handler_for(v))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    v.host = f"127.0.0.1:{srv.server_port}"
    yield v
    srv.shutdown()
    srv.server_close()


ALL = [c.key for c in PF.CATEGORIES]


# --------------------------------------------------------------------------
#  listing and times
# --------------------------------------------------------------------------


def test_each_type_is_found_where_blueos_keeps_it(vehicle):
    inv = PF.search(vehicle.host, ALL)
    names = {k: sorted(f.rel for f in v) for k, v in inv.files.items()}
    assert names["mcap"] == ["recorder_20260801_120000.mcap",
                             "recorder_20260911_184953.mcap"]
    assert names["video"] == ["recorder_20260911_184953/stream_0.mp4"]
    assert names["bin"] == ["00000081.BIN", "00000082.BIN"], "LASTLOG.TXT is not a log"
    assert names["tlog"] == []
    assert any("older BlueOS" in n for n in inv.notes["tlog"])
    assert inv.vehicle == "Nereo"


def test_the_c3_folder_is_found_by_its_left_right_and_center(vehicle):
    inv = PF.search(vehicle.host, ["c3"])
    assert inv.roots["c3"] == [C3]
    assert sorted(f.rel for f in inv.files["c3"]) == [
        "calibration.yaml", "center/img_0001.jpg", "left/img_0001.jpg",
        "right/img_0001.jpg"]


def test_a_set_c3_folder_is_used_without_searching(vehicle):
    inv = PF.search(vehicle.host, ["c3"], c3_folder=f"{C3}/left")
    assert [f.rel for f in inv.files["c3"]] == ["img_0001.jpg"]


def test_a_recording_is_placed_by_its_content_not_its_file_time(vehicle):
    """The August recording was rewritten today by the repair sweep. Its file
    time says today; its first chunk says August."""
    inv = PF.search(vehicle.host, ["mcap"])
    old = next(f for f in inv.files["mcap"] if "20260801" in f.rel)
    assert old.start == pytest.approx(1_785_585_600.0)
    assert old.modified > START


def test_an_autopilot_log_is_placed_by_its_length_and_file_time(vehicle):
    inv = PF.search(vehicle.host, ["bin"])
    log = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    assert log.end == pytest.approx(START + 2_830, abs=1)
    assert log.end - log.start == pytest.approx(2_800, abs=0.5)


def test_video_takes_the_span_of_its_recording(vehicle):
    inv = PF.search(vehicle.host, ["mcap", "video"])
    vid = inv.files["video"][0]
    rec = next(f for f in inv.files["mcap"] if "20260911" in f.rel)
    assert (vid.start, vid.end) == (rec.start, rec.end)


def test_timestamps_with_nanoseconds_and_offsets_parse():
    assert PF.parse_time("2026-09-11T18:49:53.123456789Z") == pytest.approx(START + 0.123456)
    assert PF.parse_time("2026-09-11T11:49:53-07:00") == pytest.approx(START)
    assert PF.parse_time("") is None and PF.parse_time("yesterday") is None


def test_a_stray_header_inside_a_payload_is_not_read_as_a_time():
    log = dataflash(2)
    fmts = PF.dataflash_formats(log)
    noisy = log + b"\xa3\x95\x02" + b"\xff" * 5          # truncated, unchained
    assert max(PF.dataflash_times(noisy, fmts)) == max(PF.dataflash_times(log, fmts))


# --------------------------------------------------------------------------
#  choosing: file type (A) x time period (B), plus what was clicked
# --------------------------------------------------------------------------


def _windows():
    # The fake recording is a few kilobytes, which at the rate real dives write
    # "lasts" milliseconds -- so T1 starts inside the two-minute allowance.
    return [("T1", START + 60, START + 700), ("T2", START + 800, START + 1000)]


def test_transects_only_takes_what_overlaps_a_transect(vehicle):
    inv = PF.search(vehicle.host, ["mcap", "bin"])
    PF.match(inv.all_files(), _windows())
    c = PF.choose(inv, ["mcap", "bin"], PF.PERIOD_TRANSECTS)
    assert sorted(f.rel for f in c.files) == ["00000081.BIN",
                                              "recorder_20260911_184953.mcap"]


def test_all_files_takes_every_file_of_the_types(vehicle):
    inv = PF.search(vehicle.host, ["mcap", "bin"])
    c = PF.choose(inv, ["bin"], PF.PERIOD_ALL)
    assert sorted(f.rel for f in c.files) == ["00000081.BIN", "00000082.BIN"]


def test_no_period_means_only_the_clicked_files(vehicle):
    inv = PF.search(vehicle.host, ["mcap", "bin"])
    clicked = [inv.files["mcap"][0]]
    for period in ("", PF.PERIOD_MANUAL):
        c = PF.choose(inv, ["mcap", "bin"], period, clicked)
        assert c.files == clicked and c.by_hand == 1 and c.by_rule == 0


def test_clicked_files_are_added_to_the_rule(vehicle):
    inv = PF.search(vehicle.host, ["mcap", "bin"])
    c = PF.choose(inv, ["bin"], PF.PERIOD_ALL, [inv.files["mcap"][0]])
    assert c.by_rule == 2 and c.by_hand == 1 and len(c.files) == 3


def test_a_type_not_yet_listed_is_reported_not_guessed(vehicle):
    inv = PF.search(vehicle.host, ["mcap"])
    c = PF.choose(inv, ["mcap", "bin"], PF.PERIOD_ALL)
    assert c.not_listed == ["bin"]


# --------------------------------------------------------------------------
#  downloading
# --------------------------------------------------------------------------


def test_downloads_land_in_the_flight_folder_by_type(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ALL)
    rep = PF.download(inv.all_files(), tmp_path, inv.host, inv.token)
    assert not rep.failed, rep.summary()
    assert (tmp_path / "logs/mcap/recorder_20260911_184953.mcap").is_file()
    assert (tmp_path / "logs/mcap_video/recorder_20260911_184953/stream_0.mp4").is_file()
    assert (tmp_path / "logs/BIN/00000081.BIN").is_file()
    assert (tmp_path / "photos/C3/left/img_0001.jpg").is_file()
    assert (tmp_path / "photos/C3/calibration.yaml").is_file()
    # The vehicle's own time travels with the file.
    got = (tmp_path / "logs/BIN/00000081.BIN").stat().st_mtime
    assert got == pytest.approx(START + 2_830, abs=2)


def test_a_second_download_skips_what_is_already_there(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ["mcap"])
    PF.download(inv.all_files(), tmp_path, inv.host, inv.token)
    again = PF.download(inv.all_files(), tmp_path, inv.host, inv.token)
    assert not again.done and len(again.skipped) == 2
    assert all(f.downloaded_to(tmp_path) for f in inv.all_files())


def test_downloading_never_writes_to_the_vehicle(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ALL)
    PF.download(inv.all_files(), tmp_path, inv.host, inv.token)
    assert {m for m, _ in vehicle.seen} == {"GET"}


# --------------------------------------------------------------------------
#  deleting
# --------------------------------------------------------------------------


def test_nothing_is_deleted_while_the_rov_is_armed(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ALL)
    vehicle.armed = True
    before = dict(vehicle.fs)
    with pytest.raises(PF.Refused):
        PF.delete(inv.all_files(), inv, record_dir=tmp_path)
    assert vehicle.fs == before
    assert not any(m == "DELETE" for m, _ in vehicle.seen)


def test_a_file_still_being_written_is_left_alone(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ["bin"])
    rep = PF.delete(inv.files["bin"], inv, record_dir=tmp_path)
    assert [f.rel for f in rep.done] == ["00000081.BIN"]
    assert [f.rel for f in rep.skipped] == ["00000082.BIN"]
    assert f"{BIN_DIR}/00000082.BIN" in vehicle.fs
    assert f"{BIN_DIR}/00000081.BIN" not in vehicle.fs


def test_only_the_chosen_files_are_deleted_and_a_record_is_kept(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ALL)
    target = [f for f in inv.files["mcap"] if "20260801" in f.rel]
    others = set(vehicle.fs) - {target[0].path}
    rep = PF.delete(target, inv, record_dir=tmp_path)
    assert set(vehicle.fs) == others
    deletes = [p for m, p in vehicle.seen if m == "DELETE"]
    assert len(deletes) == 1 and "20260801" in deletes[0]
    text = rep.log_path.read_text(encoding="utf-8")
    assert "Nereo" in text and target[0].path in text


def test_a_folder_emptied_by_the_delete_goes_too_but_never_a_type_root(vehicle, tmp_path):
    inv = PF.search(vehicle.host, ["video", "c3"])
    PF.delete(inv.files["video"] + inv.files["c3"], inv, record_dir=tmp_path)
    deleted = {urllib.parse.unquote(p.split("/api/resources", 1)[1])
               for m, p in vehicle.seen if m == "DELETE"}
    assert f"{REC}/recorder_20260911_184953" in deleted
    assert f"{C3}/left" in deleted
    assert REC not in deleted and C3 not in deleted


def test_only_this_module_ever_sends_anything_but_a_get():
    """The vehicle client and every other module stay read-only; the words
    that would change the vehicle appear in exactly one file."""
    pkg = Path(PF.__file__).parent
    offenders = []
    for path in pkg.rglob("*.py"):
        if path.name == "pifiles.py":
            continue
        text = path.read_text(encoding="utf-8")
        for word in ('method="DELETE"', 'method="POST"', 'method="PUT"',
                     'method="PATCH"', "_delete_request"):
            if word in text:
                offenders.append(f"{path.name}: {word}")
    assert not offenders, offenders


# --------------------------------------------------------------------------
#  the transect preview, from an autopilot log
# --------------------------------------------------------------------------


def test_a_log_on_disk_is_placed_by_its_file_time(tmp_path):
    p = tmp_path / "00000081.BIN"
    p.write_bytes(dataflash(600))
    os.utime(p, (START + 610, START + 610))
    start, end = previewsource.local_bin_span(p)
    assert end == pytest.approx(START + 610)
    assert end - start == pytest.approx(600, abs=0.5)


def test_the_preview_reads_depth_from_the_vehicle_s_log(vehicle, tmp_path):
    """No flight folder, no download: the dive profile straight off the Pi."""
    windows = [("T1", START + 2000, START + 2400)]
    from rov_flight_ops.config import AppConfig
    cfg = AppConfig(cache_root=tmp_path / "cache")
    store, notes, where = previewsource.preview_store(
        None, windows, cfg=cfg, host=vehicle.host, source=previewsource.VEHICLE)
    assert "Nereo" in where and "00000081.BIN" in where
    assert store.t_start == pytest.approx(START + 30, abs=2)
    assert any("file time" in n for n in notes)
    assert {m for m, _ in vehicle.seen} == {"GET"}
