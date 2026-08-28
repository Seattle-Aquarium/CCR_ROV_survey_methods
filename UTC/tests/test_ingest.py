"""Tests for pulling imagery off a card.

The case that matters most is that the **card is never written to**. An import
runs immediately before the operator reformats the card, so a bug that moved
instead of copied, or deleted a source, would destroy the only copy of a
survey. Several tests here exist purely to pin that down.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_photos import SAMPLE, _make_jpeg, _Store  # noqa: E402

from utc import ingest, layout  # noqa: E402

PDT = timezone(timedelta(hours=-7))
TZ = "America/Los_Angeles"


def _card(root: Path, times=("13:24:10", "13:24:20", "14:00:00"),
          raws=True) -> Path:
    """A GoPro card: nested DCIM tree, camera names, paired JPG and GPR."""
    d = root / "DCIM" / "100GOPRO"
    d.mkdir(parents=True)
    for i, t in enumerate(times, 1):
        _make_jpeg(d / f"G00{i:04d}.jpg", when=f"2026:08:25 {t}")
        if raws:
            (d / f"G00{i:04d}.GPR").write_bytes(f"raw-{i}".encode())
    return root


def _windows():
    lo = datetime(2026, 8, 25, 13, 24, 0, tzinfo=PDT).timestamp()
    return [("T1", lo, lo + 60)]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
#  scanning
# --------------------------------------------------------------------------


def test_scan_finds_pairs_in_a_nested_dcim_tree():
    with tempfile.TemporaryDirectory() as td:
        root = _card(Path(td))
        scan = ingest.scan_card(root, tz_name=TZ)
        assert len(scan.frames) == 3, scan.summary()
        assert all(f.jpg and f.gpr for f in scan.frames)
        assert [f.local.strftime("%H:%M:%S") for f in scan.frames] == [
            "13:24:10", "13:24:20", "14:00:00"]


def test_scan_is_in_time_order_not_name_order():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "DCIM" / "100GOPRO"
        d.mkdir(parents=True)
        _make_jpeg(d / "G0009.jpg", when="2026:08:25 13:24:10")
        _make_jpeg(d / "G0001.jpg", when="2026:08:25 13:25:10")
        scan = ingest.scan_card(Path(td), tz_name=TZ)
        assert [p.jpg.name for p in scan.frames] == ["G0009.jpg", "G0001.jpg"]


def test_orphan_raw_is_reported_not_guessed():
    """A GPR with no preview cannot be timed, and must not be given a made-up
    time that would file it under the wrong transect."""
    with tempfile.TemporaryDirectory() as td:
        root = _card(Path(td), times=("13:24:10",))
        (root / "DCIM" / "100GOPRO" / "G0099.GPR").write_bytes(b"lonely")
        scan = ingest.scan_card(root, tz_name=TZ)
        assert len(scan.frames) == 1
        assert any("no readable time" in w for w in scan.warnings), scan.warnings


def test_scan_reports_video_separately():
    with tempfile.TemporaryDirectory() as td:
        root = _card(Path(td), times=("13:24:10",))
        (root / "DCIM" / "100GOPRO" / "GX010001.MP4").write_bytes(b"\x00" * 32)
        scan = ingest.scan_card(root, tz_name=TZ)
        assert len(scan.videos) == 1
        assert len(scan.frames) == 1, "video must not be counted as a frame"


# --------------------------------------------------------------------------
#  planning
# --------------------------------------------------------------------------


def test_plan_counts_without_touching_anything():
    with tempfile.TemporaryDirectory() as td:
        root = _card(Path(td))
        before = {p: _sha(p) for p in root.rglob("*") if p.is_file()}
        scan = ingest.scan_card(root, tz_name=TZ)
        plan = ingest.plan_import(scan, _windows(), ingest.ImportOptions())
        assert plan.on_transect == 2 and plan.off_transect == 1
        assert plan.copy_bytes > 0
        after = {p: _sha(p) for p in root.rglob("*") if p.is_file()}
        assert after == before, "planning must not modify the card"


def test_plan_excludes_off_transect_bytes_when_told_to():
    with tempfile.TemporaryDirectory() as td:
        scan = ingest.scan_card(_card(Path(td)), tz_name=TZ)
        keep = ingest.plan_import(scan, _windows(),
                                  ingest.ImportOptions(include_off_transect=True))
        skip = ingest.plan_import(scan, _windows(),
                                  ingest.ImportOptions(include_off_transect=False))
        assert skip.copy_bytes < keep.copy_bytes
        assert skip.skip_bytes > 0 and keep.skip_bytes == 0


def test_a_pair_gets_one_name_even_on_a_collision():
    """Deciding names per file would rename the JPG and not the GPR."""
    with tempfile.TemporaryDirectory() as td:
        scan = ingest.scan_card(
            _card(Path(td), times=("13:24:10", "13:24:10", "14:00:00")),
            tz_name=TZ)
        ingest.assign(scan, _windows())
        names = [f.name_base for f in scan.frames]
        assert len(set(names)) == len(names), names


# --------------------------------------------------------------------------
#  importing
# --------------------------------------------------------------------------


def test_import_copies_and_leaves_the_card_untouched():
    """The card is the only copy until this finishes. Nothing may move."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        card = _card(td / "CARD")
        before = {p.relative_to(card): _sha(p)
                  for p in card.rglob("*") if p.is_file()}
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        scan = ingest.scan_card(card, tz_name=TZ)
        rep = ingest.import_photos(
            scan, flight, _windows(), store=_Store(SAMPLE),
            options=ingest.ImportOptions(banner_previews=False))
        assert rep.copied_jpg == 3 and rep.copied_gpr == 3, rep.summary()

        after = {p.relative_to(card): _sha(p)
                 for p in card.rglob("*") if p.is_file()}
        assert after == before, "the card must be byte-for-byte unchanged"


def test_import_files_by_transect_with_matching_stems():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        card = _card(td / "CARD")
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        scan = ingest.scan_card(card, tz_name=TZ)
        ingest.import_photos(scan, flight, _windows(), store=_Store(SAMPLE),
                             options=ingest.ImportOptions(banner_previews=False))
        t1 = layout.transect_dir(flight, "T1")
        gprs = sorted(p.stem for p in (t1 / layout.GPR).glob("*.GPR"))
        jpgs = sorted(p.stem for p in (t1 / layout.JPG_PREVIEW).glob("*.jpg"))
        assert gprs == jpgs == ["2026_08_25_13-24-10", "2026_08_25_13-24-20"]

        off = layout.transects_dir(flight) / layout.OFF_TRANSECT
        assert len(list((off / layout.GPR).glob("*.GPR"))) == 1
        assert len(list((off / layout.JPG).glob("*.jpg"))) == 1


def test_import_can_skip_off_transect_entirely():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        scan = ingest.scan_card(_card(td / "CARD"), tz_name=TZ)
        ingest.import_photos(
            scan, flight, _windows(), store=_Store(SAMPLE),
            options=ingest.ImportOptions(banner_previews=False,
                                         include_off_transect=False))
        off = layout.transects_dir(flight) / layout.OFF_TRANSECT
        assert not off.exists() or not any(off.rglob("*.jpg"))


def test_import_banners_previews_but_never_the_raws():
    from utc import photos as ph
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        scan = ingest.scan_card(_card(td / "CARD"), tz_name=TZ)
        rep = ingest.import_photos(scan, flight, _windows(),
                                   store=_Store(SAMPLE),
                                   options=ingest.ImportOptions())
        assert rep.bannered == 2, rep.summary()
        t1 = layout.transect_dir(flight, "T1")
        for p in (t1 / layout.JPG_PREVIEW).glob("*.jpg"):
            assert ph.band_height_of(p) is not None
        for p in (t1 / layout.GPR).glob("*.GPR"):
            assert p.read_bytes().startswith(b"raw-")


def test_rerunning_an_import_skips_what_is_already_there():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        card = _card(td / "CARD")
        opts = ingest.ImportOptions(banner_previews=False)
        first = ingest.import_photos(ingest.scan_card(card, tz_name=TZ), flight,
                                     _windows(), store=_Store(SAMPLE),
                                     options=opts)
        second = ingest.import_photos(ingest.scan_card(card, tz_name=TZ), flight,
                                      _windows(), store=_Store(SAMPLE),
                                      options=opts)
        assert first.copied_jpg == 3
        assert second.copied_jpg == 0 and second.skipped > 0, second.summary()


def test_bannering_without_telemetry_is_refused_up_front():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        flight = layout.scaffold(td, "2026_08_25_Centennial").root
        card = _card(td / "CARD")
        scan = ingest.scan_card(card, tz_name=TZ)
        try:
            ingest.import_photos(scan, flight, _windows(), store=None,
                                 options=ingest.ImportOptions(banner_previews=True))
        except ValueError:
            assert not any(layout.transects_dir(flight).rglob("*.jpg")), \
                "nothing may be written before the request is validated"
            return
        raise AssertionError("expected ValueError")


def test_list_drives_never_raises():
    """Called on every visit to the Import page, including with no card in."""
    drives = ingest.list_drives(removable_only=True)
    assert isinstance(drives, list)
    for d in ingest.list_drives(removable_only=False):
        assert d.caption
