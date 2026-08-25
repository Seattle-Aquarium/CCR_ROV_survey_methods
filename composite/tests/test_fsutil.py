"""Tests for lock-tolerant publishing.

These use a *real* Windows file lock rather than a mock, because the whole
point of the module is how the operating system behaves when another program
holds a handle. Python's open() on Windows does not grant FILE_SHARE_DELETE,
so an open reader is exactly the situation Excel or Dropbox creates.

Runnable directly (``python tests/test_fsutil.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from composite.fsutil import _is_lock_error, _numbered, publish  # noqa: E402

ON_WINDOWS = os.name == "nt"


def _mk(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_publish_to_new_destination():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _mk(d, "src.mp4", "payload")
        dst = d / "out" / "final.mp4"
        got = publish(src, dst)
        assert got == dst, got
        assert dst.read_text(encoding="utf-8") == "payload"
        assert not src.exists(), "source should be consumed"


def test_publish_overwrites_unlocked_destination():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _mk(d, "src.mp4", "new")
        dst = _mk(d, "final.mp4", "old")
        got = publish(src, dst)
        assert got == dst
        assert dst.read_text(encoding="utf-8") == "new"


def test_numbered_skips_taken_names():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        dst = _mk(d, "clip.mp4", "x")
        _mk(d, "clip (1).mp4", "x")
        assert _numbered(dst).name == "clip (2).mp4"


def test_waits_for_a_lock_that_clears():
    """The realistic case: Dropbox finishes its upload and lets go."""
    if not ON_WINDOWS:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _mk(d, "src.mp4", "new")
        dst = _mk(d, "final.mp4", "old")

        handle = open(dst, "r")
        released = threading.Event()

        def release_soon():
            time.sleep(1.5)
            handle.close()
            released.set()

        threading.Thread(target=release_soon, daemon=True).start()
        t0 = time.time()
        msgs: list[str] = []
        got = publish(src, dst, timeout_s=30.0, log=msgs.append)
        waited = time.time() - t0

        assert released.is_set(), "should not have won before the lock cleared"
        assert got == dst, f"should land on the real name, got {got}"
        assert dst.read_text(encoding="utf-8") == "new"
        assert waited >= 1.4, f"returned suspiciously fast ({waited:.2f}s)"
        assert any("another program" in m for m in msgs), msgs


def test_falls_back_when_the_lock_never_clears():
    """Never throw away finished work: write beside it and say so."""
    if not ON_WINDOWS:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _mk(d, "src.mp4", "new")
        dst = _mk(d, "final.mp4", "old")

        with open(dst, "r"):
            msgs: list[str] = []
            got = publish(src, dst, timeout_s=1.0, log=msgs.append)

            assert got != dst, "should not have replaced a locked file"
            assert got.name == "final (1).mp4", got.name
            assert got.read_text(encoding="utf-8") == "new"
            assert dst.read_text(encoding="utf-8") == "old", "original untouched"
            assert any("still open" in m for m in msgs), msgs
        assert not src.exists(), "source should be consumed either way"


def test_real_lock_is_classified_as_transient():
    """Guards the predicate itself: if Windows ever reported a different
    error number here, the retry would silently become a hard failure."""
    if not ON_WINDOWS:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _mk(d, "src.mp4", "new")
        dst = _mk(d, "final.mp4", "old")
        with open(dst, "r"):
            try:
                os.replace(src, dst)
            except OSError as exc:
                assert _is_lock_error(exc), (
                    f"winerror {getattr(exc, 'winerror', None)} not treated as "
                    f"a lock: {exc}"
                )
            else:
                raise AssertionError("expected the open handle to block replace")


def test_non_lock_errors_still_raise():
    """A missing source is a bug, not something to wait out."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        try:
            publish(d / "nope.mp4", d / "out.mp4", timeout_s=0.5)
        except OSError:
            return
        raise AssertionError("expected an error for a missing source")


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
