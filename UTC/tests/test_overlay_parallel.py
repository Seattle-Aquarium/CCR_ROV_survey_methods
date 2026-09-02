"""Drawing overlay frames across several processes.

Rendering panels was the pipeline's one genuinely serial stretch: ffmpeg
already saturates the machine when it encodes, but drawing held the GIL and
pegged exactly one core for tens of minutes.

Parallelising it is only safe if the output is *identical* to the serial path,
frame for frame — a composite built from a mix of the two must be
indistinguishable. That, and cancellation, are what these tests pin down.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import overlay  # noqa: E402
from utc.config import Layout  # noqa: E402


class _Store:
    """Telemetry that varies with time, so frames genuinely differ."""

    def sample(self, when: float) -> dict:
        k = when % 100
        return {
            "depth": 3.0 + k / 50.0, "altitude": 0.5 + (k % 7) / 10.0,
            "speed": 0.1, "lights": 80.0, "power_w": 240.0 + k,
            "mode": "STABILIZE", "heading": k * 3.0, "temp_c": 13.1,
            "roll": 0.0, "pitch": -1.0, "yaw": k, "climb": 0.0,
            "gain": 30.0, "cam_tilt": 0.5, "voltage_v": 13.5,
            "current_a": 18.0, "throttle": 50.0,
        }


def _digest(d: Path) -> list[tuple[str, str]]:
    return [(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(d.iterdir())]


def _footer(epoch: float) -> str:
    return f"Project  |  Site  |  T1  |  {int(epoch) % 86400:05d}"


@pytest.fixture
def cfg():
    # Small and fast, but still exercising panel + gauges + footer.
    return Layout()


def _render(tmp: Path, cfg, n_frames: int, workers: int, **kw) -> Path:
    out = tmp / f"w{workers}"
    dur = n_frames / cfg.overlay_fps
    overlay.render_sequence(out, _Store(), 1_788_000_000.0, dur, cfg,
                            footer_text=_footer, workers=workers, **kw)
    return out


# --------------------------------------------------------------------------
#  the guarantee that matters
# --------------------------------------------------------------------------


def test_parallel_output_is_byte_identical_to_serial(tmp_path, cfg):
    """The whole change is only safe if nobody can tell which path ran."""
    n = overlay._PARALLEL_FLOOR + 40
    one = _render(tmp_path, cfg, n, workers=1)
    many = _render(tmp_path, cfg, n, workers=4)
    assert _digest(one) == _digest(many)


def test_every_frame_is_written_exactly_once(tmp_path, cfg):
    n = overlay._PARALLEL_FLOOR + 7          # not a multiple of the chunk size
    out = _render(tmp_path, cfg, n, workers=4)
    panels = sorted(p.name for p in out.glob("panel_*.png"))
    assert len(panels) == n
    assert panels[0] == "panel_000000.png"
    assert panels[-1] == f"panel_{n - 1:06d}.png"


def test_a_short_sequence_stays_on_one_process(tmp_path, cfg, monkeypatch):
    """Starting a pool for a handful of frames costs more than it saves."""
    called = []
    monkeypatch.setattr(overlay, "_render_parallel",
                        lambda *a, **k: called.append(1))
    _render(tmp_path, cfg, overlay._PARALLEL_FLOOR - 1, workers=8)
    assert not called, "should not have started a pool"


# --------------------------------------------------------------------------
#  stopping
# --------------------------------------------------------------------------


def test_cancel_stops_a_parallel_render(tmp_path, cfg):
    """Stop has to work here too, and must raise the same error the rest of
    the pipeline already handles."""
    from utc.ffmpeg_tools import CancelledError

    cancel = threading.Event()
    cancel.set()                              # already cancelled
    with pytest.raises(CancelledError):
        _render(tmp_path, cfg, overlay._PARALLEL_FLOOR + 200,
                workers=4, cancel=cancel)


def test_cancel_stops_a_serial_render(tmp_path, cfg):
    from utc.ffmpeg_tools import CancelledError

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        _render(tmp_path, cfg, 10, workers=1, cancel=cancel)


# --------------------------------------------------------------------------
#  falling back
# --------------------------------------------------------------------------


def test_a_pool_that_will_not_start_falls_back_rather_than_failing(
        tmp_path, cfg, monkeypatch):
    """A partner's locked-down machine refusing to spawn processes should
    cost speed, not the run."""
    def boom(*_a, **_k):
        raise OSError("no processes for you")

    monkeypatch.setattr(overlay, "_render_parallel", boom)
    msgs = []
    n = overlay._PARALLEL_FLOOR + 10
    overlay.render_sequence(
        tmp_path / "fb", _Store(), 1_788_000_000.0, n / cfg.overlay_fps, cfg,
        footer_text=_footer, workers=4,
        progress=lambda f, m="": msgs.append(m))
    assert len(list((tmp_path / "fb").glob("panel_*.png"))) == n
    assert any("one core" in m for m in msgs), msgs


# --------------------------------------------------------------------------
#  how many
# --------------------------------------------------------------------------


def test_worker_count_leaves_the_machine_usable():
    n = overlay.worker_count()
    assert 1 <= n <= 12
    if (os.cpu_count() or 1) > 3:
        assert n <= (os.cpu_count() or 1) - 2, "must leave cores free"


def test_worker_count_can_be_forced(monkeypatch):
    assert overlay.worker_count(3) == 3
    monkeypatch.setenv("UTC_OVERLAY_WORKERS", "5")
    assert overlay.worker_count() == 5
    assert overlay.worker_count(2) == 2, "an explicit request wins"


def test_the_sequence_directory_is_rebuilt_not_appended_to(tmp_path, cfg):
    """A shorter re-render must not leave the previous run's tail behind, or
    ffmpeg reads frames from two different transects."""
    out = tmp_path / "seq"
    long_n = overlay._PARALLEL_FLOOR + 100
    overlay.render_sequence(out, _Store(), 1_788_000_000.0,
                            long_n / cfg.overlay_fps, cfg,
                            footer_text=_footer, workers=4)
    assert len(list(out.glob("panel_*.png"))) == long_n
    short_n = 30
    overlay.render_sequence(out, _Store(), 1_788_000_000.0,
                            short_n / cfg.overlay_fps, cfg,
                            footer_text=_footer, workers=4)
    assert len(list(out.glob("panel_*.png"))) == short_n
    shutil.rmtree(out, ignore_errors=True)
