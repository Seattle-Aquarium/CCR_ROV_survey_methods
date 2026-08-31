"""Tests for the flight folder layout and the imagery sort.

The pairing cases carry the weight here. A GPR and its preview must come out of
a sort with identical stems, because that pairing is what the ecological
analysis relies on and renaming is what used to break it.

Runnable directly (``python tests/test_sorting.py``) or under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_photos import SAMPLE, _make_jpeg, _Store  # noqa: E402

from utc import layout, sorting  # noqa: E402
from utc import photos as ph

PDT = timezone(timedelta(hours=-7))


# --------------------------------------------------------------------------
#  layout
# --------------------------------------------------------------------------


def test_flight_name_cleaning():
    assert layout.clean_flight_name(" 2026_08_25 Centennial ") == "2026_08_25_Centennial"
    assert layout.clean_flight_name("a/b:c*d") == "abcd"
    assert layout.clean_flight_name("__x__") == "x"


def test_flight_name_validation():
    assert layout.validate_flight_name("")
    assert layout.validate_flight_name("2026_08_25_"), "bare date needs a site"
    assert layout.validate_flight_name("Centennial"), "must start with a date"
    assert not layout.validate_flight_name("2026_08_25_Centennial")


def test_default_name_is_todays_date_prefix():
    assert layout.default_flight_name(date(2026, 8, 25)) == "2026_08_25_"


def test_scaffold_creates_the_structure_and_is_repeatable():
    with tempfile.TemporaryDirectory() as td:
        first = layout.scaffold(Path(td), "2026_08_25_Centennial")
        assert first.ok
        for rel in layout.BASE_DIRS:
            assert (first.root / rel).is_dir(), rel
        assert len(first.created) == len(layout.BASE_DIRS)

        again = layout.scaffold(Path(td), "2026_08_25_Centennial")
        assert not again.created, "second run must not recreate anything"
        assert len(again.existed) == len(layout.BASE_DIRS)


def test_scaffold_rejects_a_bad_name():
    with tempfile.TemporaryDirectory() as td:
        try:
            layout.scaffold(Path(td), "no-date-here")
        except ValueError:
            return
        raise AssertionError("expected ValueError")


def test_transect_sort_key_orders_numerically():
    names = ["T10", "T2", "T1", "T12"]
    assert sorted(names, key=layout.transect_sort_key) == ["T1", "T2", "T10", "T12"]


def test_edited_frames_are_protected_and_bannered_to_a_sibling():
    """The ML-bound folder must never be a write target."""
    assert layout.JPG_EDITED in layout.PROTECTED
    assert layout.JPG_PREVIEW not in layout.PROTECTED
    edited = layout.ImageFolder(Path("/f/T1/JPG_edited"), layout.JPG_EDITED, "T1", None)
    assert layout.banner_target(edited).name == layout.JPG_EDITED_BANNER
    preview = layout.ImageFolder(Path("/f/T1/JPG_preview"), layout.JPG_PREVIEW, "T1", None)
    assert layout.banner_target(preview) == preview.path


def test_find_image_folders_at_every_level():
    with tempfile.TemporaryDirectory() as td:
        root = layout.scaffold(Path(td), "2026_08_25_Centennial").root
        for t in ("T1", "T2"):
            layout.ensure_transect(root, t)
            (layout.transect_dir(root, t) / layout.JPG_EDITED).mkdir(exist_ok=True)
        _make_jpeg(layout.transect_dir(root, "T1") / layout.JPG_PREVIEW / "a.jpg")

        # pointed at the whole flight
        found = layout.find_image_folders(root)
        kinds = {(f.transect, f.kind) for f in found}
        assert ("T1", layout.JPG_PREVIEW) in kinds
        assert ("T2", layout.JPG_EDITED) in kinds
        assert next(f for f in found
                    if f.transect == "T1" and f.kind == layout.JPG_PREVIEW).count == 1

        # pointed at one transect
        one = layout.find_image_folders(layout.transect_dir(root, "T1"))
        assert {f.kind for f in one} >= {layout.JPG_PREVIEW, layout.JPG_EDITED}
        assert all(f.transect == "T1" for f in one)

        # pointed at a parent of flights
        assert len(layout.find_image_folders(Path(td))) >= len(found)

        # flight is recovered from the path
        assert found[0].flight == root


# --------------------------------------------------------------------------
#  sorting
# --------------------------------------------------------------------------


def _flight(td: Path, times=("13:24:10", "13:24:20", "14:00:00")):
    """A scaffolded flight with paired GPR/JPG, the last one off-transect."""
    root = layout.scaffold(td, "2026_08_25_Centennial").root
    jd, gd = root / "photos" / "JPG", root / "photos" / "GPR"
    for i, t in enumerate(times, 1):
        _make_jpeg(jd / f"G000{i}.jpg", when=f"2026:08:25 {t}")
        (gd / f"G000{i}.GPR").write_bytes(f"raw-{i}".encode())
    lo = datetime(2026, 8, 25, 13, 24, 0, tzinfo=PDT).timestamp()
    return root, [("T1", lo, lo + 60)]


def test_sort_pairs_gpr_and_jpg_onto_identical_stems():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        rep = sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False),
            offset_hours=-7.0,
        )
        assert rep.gpr_moved == 2 and rep.jpg_moved == 2, rep.summary()
        t1 = layout.transect_dir(root, "T1")
        gprs = sorted(p.stem for p in (t1 / layout.GPR).glob("*.GPR"))
        jpgs = sorted(p.stem for p in (t1 / layout.JPG_PREVIEW).glob("*.jpg"))
        assert gprs == jpgs, (gprs, jpgs)
        assert gprs == ["2026_08_25_13-24-10", "2026_08_25_13-24-20"]


def test_collision_keeps_a_pair_together():
    """Two frames in one second must not rename the JPG and not the GPR."""
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td), times=("13:24:10", "13:24:10", "14:00:00"))
        sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False),
            offset_hours=-7.0,
        )
        t1 = layout.transect_dir(root, "T1")
        gprs = sorted(p.stem for p in (t1 / layout.GPR).glob("*.GPR"))
        jpgs = sorted(p.stem for p in (t1 / layout.JPG_PREVIEW).glob("*.jpg"))
        assert len(gprs) == 2 and gprs == jpgs, (gprs, jpgs)


def test_sort_banners_previews_but_never_the_raws():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        rep = sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=True),
            offset_hours=-7.0,
        )
        assert rep.bannered == 2
        t1 = layout.transect_dir(root, "T1")
        for p in (t1 / layout.JPG_PREVIEW).glob("*.jpg"):
            assert ph.band_height_of(p) is not None
        for p in (t1 / layout.GPR).glob("*.GPR"):
            assert p.read_bytes().startswith(b"raw-"), "raw must be byte-identical"


def test_off_transect_policies_are_independent():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False,
                                        off_transect_gpr="move",
                                        off_transect_jpg="delete"),
            offset_hours=-7.0,
        )
        off = layout.transects_dir(root) / layout.OFF_TRANSECT
        assert (off / layout.GPR / "G0003.GPR").is_file(), "GPR should be moved"
        assert not (root / "photos" / "JPG" / "G0003.jpg").exists(), "JPG deleted"
        assert not (off / layout.JPG).exists() or not any(
            (off / layout.JPG).iterdir())


def test_off_transect_keep_leaves_both_alone():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False),
            offset_hours=-7.0,
        )
        assert (root / "photos" / "JPG" / "G0003.jpg").is_file()
        assert (root / "photos" / "GPR" / "G0003.GPR").is_file()


def test_gpr_without_a_preview_is_still_placed():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        # a raw named with our own convention, no preview alongside
        extra = root / "photos" / "GPR" / "2026_08_25_13-24-30.GPR"
        extra.write_bytes(b"lonely")
        rep = sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False),
            offset_hours=-7.0,
        )
        assert rep.unmatched_gpr == 1, rep.summary()
        moved = layout.transect_dir(root, "T1") / layout.GPR / "2026_08_25_13-24-30.GPR"
        assert moved.is_file(), sorted(
            p.name for p in (layout.transect_dir(root, "T1") / layout.GPR).iterdir())


def test_sort_rejects_an_unknown_off_transect_policy():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        try:
            sorting.sort_flight(
                root, windows, store=_Store(SAMPLE),
                options=sorting.SortOptions(off_transect_gpr="incinerate"),
            )
        except ValueError:
            return
        raise AssertionError("expected ValueError for an unknown policy")


def test_bannering_without_telemetry_is_refused_before_moving_anything():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        try:
            sorting.sort_flight(root, windows, store=None,
                                options=sorting.SortOptions(banner_previews=True))
        except ValueError:
            assert (root / "photos" / "JPG" / "G0001.jpg").is_file(), \
                "nothing may move before the request is validated"
            return
        raise AssertionError("expected ValueError")


def test_plan_sort_does_not_touch_the_files():
    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        items, _ = sorting.plan_sort(root, windows, offset_hours=-7.0)
        assert len(items) == 3
        assert sum(1 for i in items if i.transect == "T1") == 2
        assert sum(1 for i in items if i.transect is None) == 1
        assert all(i.jpg.is_file() for i in items if i.jpg)
        assert all(i.gpr.is_file() for i in items if i.gpr)


def _strip_exif_time(path: Path) -> None:
    """Save without any EXIF, as a Lightroom export can."""
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        rgb = im.convert("RGB")
    rgb.save(path, "JPEG", quality=95)


def test_edited_frames_are_never_modified_and_time_falls_back_to_the_name():
    """The whole point of the protected folder, end to end.

    An edited export may carry no EXIF at all, so its time comes from the name
    the sort gave it. The banner then goes to a sibling, and the originals must
    come out byte-for-byte identical -- they feed downstream ML.
    """
    from utc import photos as ph

    with tempfile.TemporaryDirectory() as td:
        root, windows = _flight(Path(td))
        sorting.sort_flight(
            root, windows, store=_Store(SAMPLE),
            options=sorting.SortOptions(banner_previews=False),
            offset_hours=-7.0,
        )
        t1 = layout.transect_dir(root, "T1")

        # the team exports colour-corrected frames, EXIF stripped
        edited = t1 / layout.JPG_EDITED
        edited.mkdir(exist_ok=True)
        for src in sorted((t1 / layout.JPG_PREVIEW).glob("*.jpg")):
            dst = edited / src.name
            dst.write_bytes(src.read_bytes())
            _strip_exif_time(dst)
        assert all(ph.read_photo_time(p) is None for p in edited.glob("*.jpg")), \
            "test setup: EXIF time should be gone"
        before = {p.name: p.read_bytes() for p in edited.glob("*.jpg")}

        # discovery finds it, and routes the banner to a sibling
        folders = layout.find_image_folders(t1)
        ed = next(f for f in folders if f.kind == layout.JPG_EDITED)
        assert ed.protected and ed.count == 2
        target = layout.banner_target(ed)
        assert target.name == layout.JPG_EDITED_BANNER

        rep = ph.banner_folder(ed.path, _Store(SAMPLE), out_dir=target,
                               offset_hours=-7.0)
        assert rep.done == 2, rep.summary() + " " + "; ".join(rep.warnings)

        after = {p.name: p.read_bytes() for p in edited.glob("*.jpg")}
        assert after == before, "JPG_edited must be byte-for-byte untouched"

        made = sorted(target.glob("*.jpg"))
        assert len(made) == 2
        assert all(ph.band_height_of(p) is not None for p in made)
        # names carried over, so each banner copy still pairs with its original
        assert sorted(p.name for p in made) == sorted(before)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as ex:
                failed += 1
                print(f"  FAIL  {name}: {ex}")
    print(f"\n{'all passed' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
