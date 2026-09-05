"""Copying recordings off the vehicle onto a drive.

Every check here exists because of something that went wrong in the field: a
thumb drive that refused a 4.94 GiB file while reporting space free, a
recording from a previous day that looked current because BlueOS had rewritten
its modification time, and a flight whose covering recording was simply never
downloaded.

The transfer is exercised against a folder on disk rather than a vehicle, so
this runs in CI and on a desk.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import rovfetch as rf  # noqa: E402


def _rec(name: str, size: int = 64, start=None, end=None) -> rf.Recording:
    return rf.Recording(name=name, size=size, start=start, end=end)


def _write(folder: Path, name: str, payload: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(payload)
    return p


def _mcap_bytes(n: int = 4096) -> bytes:
    return rf.MCAP_MAGIC + b"\x00" * (n - len(rf.MCAP_MAGIC))


# --------------------------------------------------------------------------
#  the destination
# --------------------------------------------------------------------------


def test_fat32_refuses_a_large_file_and_says_free_space_is_not_the_problem(
        tmp_path, monkeypatch):
    """The failure we actually hit: a 4.94 GiB recording would not write to a
    thumb drive that reported plenty of room. FAT32 cannot hold 4 GiB or more,
    and the error the operating system gives does not say so."""
    monkeypatch.setattr(rf, "filesystem_of", lambda _p: "FAT32")
    monkeypatch.setattr(rf, "is_removable", lambda _p: True)

    d = rf.inspect_destination(tmp_path, largest_file=int(4.94 * 2 ** 30))
    assert not d.ok
    (msg,) = d.problems
    assert "FAT32" in msg
    assert "4 GiB" in msg
    assert "Free space is not the problem" in msg
    assert "exFAT" in msg, "must say how to fix it"


def test_fat32_with_only_small_files_is_allowed_but_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "filesystem_of", lambda _p: "FAT32")
    monkeypatch.setattr(rf, "is_removable", lambda _p: True)
    d = rf.inspect_destination(tmp_path, largest_file=2 * 2 ** 30)
    assert d.ok
    assert any("4 GiB" in n for n in d.notes), "a longer dive would hit it"


def test_exfat_takes_a_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "filesystem_of", lambda _p: "exFAT")
    monkeypatch.setattr(rf, "is_removable", lambda _p: True)
    d = rf.inspect_destination(tmp_path, largest_file=int(6.3 * 2 ** 30))
    assert d.ok, d.problems


def test_a_drive_without_room_is_refused_before_anything_is_fetched(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "filesystem_of", lambda _p: "exFAT")
    monkeypatch.setattr(rf, "is_removable", lambda _p: True)
    monkeypatch.setattr(rf.shutil, "disk_usage",
                        lambda _p: type("U", (), {"free": 2 * 2 ** 30,
                                                  "total": 64 * 2 ** 30})())
    d = rf.inspect_destination(tmp_path, need_bytes=20 * 2 ** 30)
    assert not d.ok and "Not enough room" in d.problems[0]
    assert "Short by" in d.problems[0]


def test_the_layout_matches_the_dropbox_tree(tmp_path):
    """Laid out the same way, the folder drops into flights/ unchanged."""
    p = rf.flight_logs_dir(tmp_path, "2026-09-09", "Magnolia")
    assert p.parts[-3:] == ("flights", "2026_09_09_Magnolia", "logs")
    # a site with awkward characters still yields a usable folder
    q = rf.flight_logs_dir(tmp_path, "2026-09-09", "Pier 62 / North")
    assert not any(c in q.parts[-2] for c in '\\/:*?"<>|')


# --------------------------------------------------------------------------
#  choosing what to take
# --------------------------------------------------------------------------


def test_recordings_are_matched_on_their_span_not_their_name():
    """The 6.7 GB September 1st file was pulled into a September 3rd download
    because BlueOS had rewritten its modification time. Only the recorded
    span can settle which dive a file belongs to."""
    windows = [("T1", 1000.0, 1600.0), ("T2", 2000.0, 2600.0)]
    recs = [
        _rec("covers_T1.mcap", start=900.0, end=1700.0),
        _rec("covers_both.mcap", start=900.0, end=2700.0),
        _rec("outside.mcap", start=50_000.0, end=51_000.0),
        _rec("unknown_span.mcap"),
    ]
    rf.match_transects(recs, windows)
    assert recs[0].covers == ["T1"]
    assert recs[1].covers == ["T1", "T2"]
    assert recs[2].covers == []
    assert recs[3].covers == [], "an unknown span must not be guessed at"


def test_a_recording_just_outside_still_counts_within_the_margin():
    """A transect that starts moments after the recorder does is normal."""
    recs = [_rec("edge.mcap", start=1660.0, end=1800.0)]
    rf.match_transects(recs, [("T1", 1000.0, 1600.0)], margin_s=120.0)
    assert recs[0].covers == ["T1"]
    recs = [_rec("far.mcap", start=1900.0, end=2000.0)]
    rf.match_transects(recs, [("T1", 1000.0, 1600.0)], margin_s=120.0)
    assert recs[0].covers == []


# --------------------------------------------------------------------------
#  the transfer
# --------------------------------------------------------------------------


def test_a_copy_is_verified_and_lands_under_its_own_name(tmp_path):
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    payload = _mcap_bytes(8192)
    _write(src, "a.mcap", payload)
    rec = _rec("a.mcap", size=len(payload))

    rep = rf.fetch([rec], rf.local_opener(src), dst)
    assert rep.copied and not rep.failed, rep.summary()
    assert (dst / "a.mcap").read_bytes() == payload
    assert not list(dst.glob("*.part")), "no partial left behind"


def test_a_short_copy_is_failed_not_kept(tmp_path):
    """A transfer that ran out of drive looks finished. It must not."""
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    _write(src, "a.mcap", _mcap_bytes(1000))
    rec = _rec("a.mcap", size=99_999)          # vehicle says it is bigger

    rep = rf.fetch([rec], rf.local_opener(src), dst)
    assert rep.failed and not rep.copied
    assert "bytes arrived" in rep.failed[0].detail
    assert not (dst / "a.mcap").exists(), "a short file must not be published"
    assert not list(dst.glob("*.part"))


def test_something_that_is_not_an_mcap_is_rejected(tmp_path):
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    junk = b"not an mcap at all" * 10
    _write(src, "a.mcap", junk)
    rep = rf.fetch([_rec("a.mcap", size=len(junk))], rf.local_opener(src), dst)
    assert rep.failed and "begin like an mcap" in rep.failed[0].detail


def test_a_file_already_there_is_left_alone(tmp_path):
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    payload = _mcap_bytes(4096)
    _write(src, "a.mcap", payload)
    dst.mkdir(parents=True)
    (dst / "a.mcap").write_bytes(payload)
    stamp = (dst / "a.mcap").stat().st_mtime_ns

    rep = rf.fetch([_rec("a.mcap", size=len(payload))], rf.local_opener(src), dst)
    assert [i.status for i in rep.items] == ["skipped"]
    assert (dst / "a.mcap").stat().st_mtime_ns == stamp


def test_a_half_finished_earlier_attempt_is_fetched_again(tmp_path):
    """Resuming matters on a tether: the previous attempt may have died."""
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    payload = _mcap_bytes(4096)
    _write(src, "a.mcap", payload)
    dst.mkdir(parents=True)
    (dst / "a.mcap").write_bytes(payload[:500])      # truncated earlier run

    rep = rf.fetch([_rec("a.mcap", size=len(payload))], rf.local_opener(src), dst)
    assert [i.status for i in rep.items] == ["copied"]
    assert (dst / "a.mcap").read_bytes() == payload
    assert any("fetching it again" in w for w in rep.warnings)


def test_cancelling_leaves_no_partial_file(tmp_path):
    from utc.ffmpeg_tools import CancelledError

    src, dst = tmp_path / "rov", tmp_path / "ssd"
    _write(src, "a.mcap", _mcap_bytes(4096))
    cancel = threading.Event()
    cancel.set()

    rep = rf.fetch([_rec("a.mcap", size=4096)], rf.local_opener(src), dst,
                   cancel=cancel)
    assert rep.failed
    assert CancelledError.__name__ in rep.failed[0].detail
    assert not list(dst.glob("*")), "nothing should survive a cancel"


def test_one_bad_recording_does_not_stop_the_others(tmp_path):
    src, dst = tmp_path / "rov", tmp_path / "ssd"
    good = _mcap_bytes(2048)
    _write(src, "good.mcap", good)
    # "missing.mcap" is never written, so opening it fails
    rep = rf.fetch([_rec("missing.mcap", size=10), _rec("good.mcap", size=len(good))],
                   rf.local_opener(src), dst)
    assert len(rep.failed) == 1 and len(rep.copied) == 1
    assert (dst / "good.mcap").is_file()


def test_verify_accepts_a_truncated_recording_that_copied_correctly(tmp_path):
    """A dive that lost power writes a file the recorder never closed. That is
    a broken *recording*, not a broken copy, and UTC repairs it later -- so
    the transfer must not reject it."""
    p = tmp_path / "t.mcap"
    p.write_bytes(_mcap_bytes(4096))          # valid magic, no footer
    ok, why = rf.verify(p, expected_size=4096)
    assert ok, why
