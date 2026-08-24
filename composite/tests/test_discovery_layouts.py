"""Discovery against synthetic folder layouts, including the going-forward one.

The new convention (logs/ + videos/downward/) does not exist in any archived
flight yet, so it has to be tested against a fabricated tree -- otherwise the
first real use of it would be the test.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from composite.discovery import discover, output_dirs  # noqa: E402


def _make(root: Path, files: list[str]) -> None:
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00" * 16)


def test_new_convention():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "2026_08_24_Centennial"
        _make(root, [
            "logs/recorder_20260824_200000.mcap",
            "photos/GOPR0001.JPG",
            "videos/downward/GX010001.MP4",
            "videos/downward/GX020001.MP4",
            "videos/forward/GX010002.MP4",
        ])
        d = discover(root)
        assert d.ok, d.summary()
        assert len(d.mcaps) == 1
        assert [v.name for v in d.videos] == ["GX010001.MP4", "GX020001.MP4"]
        # the forward camera must never be composited
        assert all("forward" not in str(v.path) for v in d.videos)
        assert any("forward/ folder exists and is being ignored" in n for n in d.notes)
        # the canonical layout should produce no "found somewhere else" notes
        assert not any("rather than" in n for n in d.notes), d.notes


def test_chapter_ordering_across_recordings():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "f"
        _make(root, [
            "logs/a.mcap",
            # two recordings, chapters interleaved alphabetically
            "videos/downward/GX010042.MP4",
            "videos/downward/GX020042.MP4",
            "videos/downward/GX010041.MP4",
            "videos/downward/GX030041.MP4",
        ])
        d = discover(root)
        assert [v.name for v in d.videos] == [
            "GX010041.MP4", "GX030041.MP4", "GX010042.MP4", "GX020042.MP4",
        ], [v.name for v in d.videos]


def test_multiple_mcaps_are_reported():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "f"
        _make(root, [
            "logs/recorder_20260824_200000.mcap",
            "logs/recorder_20260824_201500.mcap",
            "logs/recorder_20260824_203000.mcap",
            "videos/downward/GX010001.MP4",
        ])
        d = discover(root)
        assert len(d.mcaps) == 3
        assert any("merged into one timeline" in n for n in d.notes)


def test_previous_composites_are_not_re_ingested():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "f"
        _make(root, [
            "logs/a.mcap",
            "videos/downward/GX010001.MP4",
            "videos/composites/2026-08-24_HSIL_Cove_T1_1080p.mp4",
        ])
        d = discover(root)
        assert [v.name for v in d.videos] == ["GX010001.MP4"]


def test_renamed_files_still_work():
    """HSIL flights have wholesale-renamed MP4s with no GoPro naming."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "f"
        _make(root, [
            "logs/a.mcap",
            "downward/video/2025_09_27_11-57-09.MP4",
            "downward/video/2025_09_27_12-33-29.MP4",
            "forward/2025_09_27_11-57-21.MP4",
        ])
        d = discover(root)
        assert len(d.videos) == 2
        assert all("forward" not in str(v.path) for v in d.videos)


def test_missing_inputs_are_reported_not_raised():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "empty"
        root.mkdir()
        d = discover(root)
        assert not d.ok
        assert any("no .mcap" in w for w in d.warnings)
        assert any("no downward GoPro" in w for w in d.warnings)

    d2 = discover(Path(td) / "does_not_exist")
    assert not d2.ok and d2.warnings


def test_output_dirs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "f"
        _make(root, ["logs/a.mcap", "videos/downward/GX010001.MP4"])
        comps, logs = output_dirs(root, create=True)
        assert comps == root / "videos" / "composites" and comps.is_dir()
        assert logs == root / "logs" and logs.is_dir()


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
