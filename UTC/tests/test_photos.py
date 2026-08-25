"""Tests for stamping telemetry onto stills.

The orientation cases exist because this exact bug has now appeared twice in
this project: once in the video path (autorotate applied the display matrix to
the frames *and* copied it to the output) and once here (the band was pasted on
the stored-upside-down buffer while EXIF Orientation was preserved, so viewers
rotated the result and the band came out at the bottom, inverted).

Both times the mistake was invisible to a check that reads raw pixels, because
Pillow and ffmpeg alike ignore the rotation metadata unless asked. So these
tests assert on what a *viewer* would see.

Runnable directly (``python tests/test_photos.py``) or under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ExifTags, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import brand, photos as ph  # noqa: E402

_ORI = {v: k for k, v in ExifTags.TAGS.items()}["Orientation"]
_DTO = {v: k for k, v in ExifTags.TAGS.items()}["DateTimeOriginal"]
_OFF = {v: k for k, v in ExifTags.TAGS.items()}["OffsetTimeOriginal"]


class _Store:
    """Minimal stand-in for TelemetryStore."""

    def __init__(self, sample: dict):
        self._sample = sample

    def sample(self, when: float) -> dict:
        return dict(self._sample)


SAMPLE = {
    "altitude": 0.77, "speed": 0.12, "lights": 80.0, "depth": 11.32,
    "power_w": 248.0, "gain": 30.0, "mode": "ALT_HOLD",
}


def _make_jpeg(path: Path, orientation: int = 3, size=(640, 480),
               when: str = "2026:08:24 13:24:17") -> None:
    """A still whose STORED top strip is blue and stored bottom strip is red.

    With Orientation=3 a viewer rotates 180, so the RED strip is what the user
    sees at the top of the image.
    """
    im = Image.new("RGB", size, (20, 60, 20))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, size[0], 40], fill=(0, 0, 255))            # stored top
    d.rectangle([0, size[1] - 40, size[0], size[1]], fill=(255, 0, 0))
    exif = im.getexif()
    exif[_ORI] = orientation
    ifd = exif.get_ifd(0x8769)
    ifd[_DTO] = when
    ifd[_OFF] = "-07:00"
    im.save(path, "JPEG", exif=exif.tobytes(), quality=95)


def test_reads_time_and_offset():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.jpg"
        _make_jpeg(p)
        got = ph.read_photo_time(p)
        assert got is not None
        epoch, local, stamped = got
        assert local.strftime("%H:%M:%S") == "13:24:17"
        assert local.utcoffset() == timedelta(hours=-7), local.utcoffset()
        assert stamped is False


def test_offset_parsing():
    assert ph._parse_offset("-07:00") == timedelta(hours=-7)
    assert ph._parse_offset("+05:30") == timedelta(hours=5, minutes=30)
    assert ph._parse_offset("garbage") is None
    assert ph._parse_offset(None) is None


def test_orientation_is_baked_and_tag_reset():
    """The saved file must mean what it shows: no viewer may rotate it again."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.jpg"
        _make_jpeg(p, orientation=3)
        photo = ph.Photo(p, 0.0, datetime(2026, 8, 24, 13, 24, 17,
                                          tzinfo=timezone(timedelta(hours=-7))))
        res = ph.stamp_photo(photo, _Store(SAMPLE), out_dir=Path(td) / "out")
        assert res.ok, res.skipped
        # Close the handle: Windows will not remove the temp dir while a file
        # inside it is still open.
        with Image.open(res.output) as out:
            assert out.getexif().get(_ORI) == 1, "orientation tag must be neutralised"


def test_band_is_above_the_image_as_displayed():
    """The regression that matters: band at the top of what the user sees."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.jpg"
        _make_jpeg(p, orientation=3)
        photo = ph.Photo(p, 0.0, datetime(2026, 8, 24, 13, 24, 17,
                                          tzinfo=timezone(timedelta(hours=-7))))
        res = ph.stamp_photo(photo, _Store(SAMPLE), out_dir=Path(td) / "out")
        with Image.open(res.output) as im:
            a = np.asarray(im.convert("RGB")).astype(int)

        band_h = a.shape[0] - 480
        assert band_h > 0, "canvas should have grown"

        # Band ground is Fathom, and it sits at the very top.
        fathom = np.array(brand.hex_to_rgb(brand.FATHOM))
        assert np.abs(a[2, :10] - fathom).sum() < 40, a[2, :10]

        # Immediately under the band is the RED strip -- the one a viewer sees
        # at the top. If orientation were mishandled it would be blue here.
        strip = a[band_h + 10, :, :].mean(axis=0)
        assert strip[0] > 150 and strip[2] < 90, f"expected red under band, got {strip}"

        # ...and the bottom of the image is blue.
        bottom = a[-5, :, :].mean(axis=0)
        assert bottom[2] > 150 and bottom[0] < 90, f"expected blue at bottom, got {bottom}"


def test_worst_case_band_does_not_clip():
    """Montserrat is proportional and its digits differ in width, so a size
    chosen for one value silently truncates a longer one."""
    style = ph.BandStyle()
    W, H = 5568, style.height(4872)
    worst = {f.key: v for f, v in zip(
        ph.BAND_FIELDS,
        [88.88, 8.88, 100.0, 188.88, 1888.0, "MOTOR_DETECT"])}
    band = np.asarray(ph.render_band(W, H, worst, "23:59:59", style).convert("RGB"))
    rule = max(1, int(H * style.rule_frac))
    ink = (np.abs(band[: H - rule].astype(int)
                  - np.array(brand.hex_to_rgb(style.bg))).sum(axis=2) > 30)
    cols = np.nonzero(ink.any(axis=0))[0]
    assert cols.max() < W - 2, f"text reaches x={cols.max()} of {W-1}"
    assert cols.min() > 0


def test_second_pass_refuses_to_stamp_again():
    """In-place stamping is not reversible, so a re-run must not deface."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.jpg"
        _make_jpeg(p)
        out = Path(td) / "out"
        photo = ph.Photo(p, 0.0, datetime(2026, 8, 24, 13, 24, 17,
                                          tzinfo=timezone(timedelta(hours=-7))))
        first = ph.stamp_photo(photo, _Store(SAMPLE), out_dir=out)
        assert first.ok

        again = ph.read_photo_time(first.output)
        assert again is not None and again[2] is True, "marker not recognised"
        second = ph.stamp_photo(
            ph.Photo(first.output, 0.0, photo.local, stamped=True),
            _Store(SAMPLE), out_dir=out,
        )
        assert second.skipped, "a stamped photo must be skipped"


def test_source_name_recorded():
    """Renaming breaks the JPG<->GPR pairing, so provenance has to survive."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "G0014626.jpg"
        _make_jpeg(p)
        photo = ph.Photo(p, 0.0, datetime(2026, 8, 24, 13, 24, 17,
                                          tzinfo=timezone(timedelta(hours=-7))))
        res = ph.stamp_photo(photo, _Store(SAMPLE), out_dir=Path(td) / "out")
        with Image.open(res.output) as im:
            desc = str(im.getexif().get(
                {v: k for k, v in ExifTags.TAGS.items()}["ImageDescription"]))
        assert "G0014626.jpg" in desc, desc


def test_naming():
    local = datetime(2026, 8, 24, 13, 23, 17, tzinfo=timezone(timedelta(hours=-7)))
    name = ph.stamped_name(local, SAMPLE)
    assert name == "2026-08-24_13-23-17_0.77m_80p_0.120ms.JPG", name


def test_naming_carries_the_camera_stem():
    """The GPR raws share the camera stem, so it has to survive the rename or
    the pairing is only recoverable by reading EXIF on every file."""
    local = datetime(2026, 8, 24, 13, 23, 17, tzinfo=timezone(timedelta(hours=-7)))
    name = ph.stamped_name(local, SAMPLE, ".JPG", "G0014606")
    assert name == "2026-08-24_13-23-17_0.77m_80p_0.120ms_G0014606.JPG", name


def test_stamped_file_keeps_the_stem_in_its_name():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "G0014606.jpg"
        _make_jpeg(p)
        photo = ph.Photo(p, 0.0, datetime(2026, 8, 24, 13, 24, 17,
                                          tzinfo=timezone(timedelta(hours=-7))))
        res = ph.stamp_photo(photo, _Store(SAMPLE), out_dir=Path(td) / "out")
        assert res.output.name.endswith("_G0014606.jpg"), res.output.name


def test_naming_survives_missing_values():
    """A dropout must not produce 'Nonem' or crash the run."""
    local = datetime(2026, 8, 24, 13, 23, 17, tzinfo=timezone(timedelta(hours=-7)))
    name = ph.stamped_name(local, {"altitude": None, "lights": 80.0, "speed": None})
    assert name == "2026-08-24_13-23-17_NAm_80p_NAms.JPG", name


def test_unique_path_never_clobbers():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "x.JPG").write_bytes(b"1")
        assert ph.unique_path(d, "x.JPG").name == "x_2.JPG"


def _flight(td: Path) -> tuple[Path, list]:
    """Three stills: two inside a transect window, one well outside it."""
    d = td / "JPEG"
    d.mkdir(parents=True)
    _make_jpeg(d / "G01.jpg", when="2026:08:24 13:24:10")
    _make_jpeg(d / "G02.jpg", when="2026:08:24 13:24:20")
    _make_jpeg(d / "G03.jpg", when="2026:08:24 14:00:00")   # off-transect
    lo = datetime(2026, 8, 24, 13, 24, 0,
                  tzinfo=timezone(timedelta(hours=-7))).timestamp()
    return d, [("T1", lo, lo + 60)]


def test_process_flight_keep_leaves_off_transect_alone():
    with tempfile.TemporaryDirectory() as td:
        d, windows = _flight(Path(td))
        rep = ph.process_flight(d, _Store(SAMPLE), windows, off_transect=ph.KEEP)
        assert rep.stamped == 2, rep.summary()
        assert rep.off_transect == 1
        assert rep.failed == 0
        assert len(list((d / "T1").glob("*.jpg"))) == 2
        assert (d / "G03.jpg").is_file(), "off-transect still must be untouched"
        # the two on-transect originals were consumed
        assert not (d / "G01.jpg").exists()


def test_process_flight_move_relocates_off_transect():
    with tempfile.TemporaryDirectory() as td:
        d, windows = _flight(Path(td))
        rep = ph.process_flight(d, _Store(SAMPLE), windows, off_transect=ph.MOVE)
        moved = d / ph.OFF_TRANSECT_DIR / "G03.jpg"
        assert moved.is_file(), list((d / ph.OFF_TRANSECT_DIR).iterdir())
        assert not (d / "G03.jpg").exists()
        assert rep.off_transect == 1


def test_process_flight_delete_removes_only_off_transect():
    with tempfile.TemporaryDirectory() as td:
        d, windows = _flight(Path(td))
        rep = ph.process_flight(d, _Store(SAMPLE), windows, off_transect=ph.DELETE)
        assert not (d / "G03.jpg").exists()
        assert len(list((d / "T1").glob("*.jpg"))) == 2, "on-transect must survive"
        assert rep.off_transect == 1


def test_process_flight_rejects_an_unknown_policy():
    """A typo must not silently fall through to deleting anything."""
    with tempfile.TemporaryDirectory() as td:
        d, windows = _flight(Path(td))
        try:
            ph.process_flight(d, _Store(SAMPLE), windows, off_transect="purge")
        except ValueError:
            return
        raise AssertionError("expected ValueError for an unknown policy")


def test_process_flight_is_safe_to_rerun():
    with tempfile.TemporaryDirectory() as td:
        d, windows = _flight(Path(td))
        ph.process_flight(d, _Store(SAMPLE), windows, off_transect=ph.KEEP)
        before = sorted(p.name for p in (d / "T1").glob("*.jpg"))
        again = ph.process_flight(d, _Store(SAMPLE), windows, off_transect=ph.KEEP)
        after = sorted(p.name for p in (d / "T1").glob("*.jpg"))
        assert after == before, "a re-run must not add or rename anything"
        assert again.stamped == 0


def test_find_photo_dir_locates_jpegs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "photos"
        (root / "GPR").mkdir(parents=True)
        (root / "GPR" / "G01.GPR").write_bytes(b"raw")
        jd = root / "JPEG"
        jd.mkdir()
        _make_jpeg(jd / "G01.jpg")
        assert ph.find_photo_dir(root) == jd
        assert ph.find_photo_dir(Path(td) / "nope") is None


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
