"""Measuring the GoPro's clock against the vehicle's turns.

On the 14 September 2026 OTS flight the GoPro's timecode was 34.8 s behind the
vehicle's clock. The composite's telemetry panel and ROV inset agreed with
each other and both showed what had happened half a minute before the GoPro
frame beneath them. The light check could not see it -- the transect was a
trim, and the lights never changed -- so these tests pin the check that can:
the down-facing picture rotates as the vehicle yaws.

The footage here is synthetic: a texture turned by a known yaw profile and
shifted by a known clock offset, so the answer is known exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_imagery_processing import motion_sync as M  # noqa: E402

EPOCH = 1_789_404_165.0          # OTS T1, 09:42:45 PDT


def _texture(seed: int = 3, n: int = 220) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = rng.random((n, n)) * 255
    for _ in range(3):
        t = M._blur(t)
    return t


def _view(tex: np.ndarray, deg: float, dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """A THUMB-sized window on the texture, rotated `deg` about its center."""
    th = np.radians(deg)
    c, s = np.cos(th), np.sin(th)
    h, w = M.THUMB_H, M.THUMB_W
    Y, X = np.mgrid[0:h, 0:w].astype(float)
    Xc, Yc = X - (w - 1) / 2, Y - (h - 1) / 2
    cx, cy = tex.shape[1] / 2 + dx, tex.shape[0] / 2 + dy
    xs = c * Xc + s * Yc + cx
    ys = -s * Xc + c * Yc + cy
    v, _ = M._sample(tex, xs, ys)
    return v.astype(np.uint8)


def _yaw_profile(seconds: float, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Telemetry-like yaw rate in deg/s at 10 Hz: a pilot's irregular turns."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 0.1)
    v = rng.normal(0, 1, t.size)
    for _ in range(40):                       # smooth into turns of a few seconds
        v = np.convolve(v, np.ones(5) / 5, mode="same")
    return t, v / (np.std(v) + 1e-9) * 6.0


def _footage(yaw_t, yaw_v, seconds, label_s, offset_s, sign=1.0):
    """Frames at RATE labeled as starting `label_s` into the telemetry, but
    really shot `offset_s` later than that."""
    tex = _texture()
    tau = np.arange(0, seconds, 1 / M.RATE)
    rate = np.interp(label_s + tau + offset_s, yaw_t, yaw_v)
    angle = np.concatenate([[0.0], np.cumsum(rate[1:] / M.RATE)]) * sign
    frames = np.stack([_view(tex, a, dx=0.2 * np.sin(k / 9))
                       for k, a in enumerate(angle)])
    return tau, frames


def test_rotation_between_recovers_a_known_turn():
    tex = _texture()
    a = _view(tex, 0.0)
    for want in (-3.0, -0.8, 0.0, 1.5, 4.0):
        b = _view(tex, want, dx=0.7, dy=-0.4)
        assert M.rotation_between(a, b) == pytest.approx(want, abs=0.15)


def test_measure_finds_the_clock_offset():
    """The OTS shape: the camera's clock behind the vehicle's."""
    yaw_t, yaw_v = _yaw_profile(400)
    tau, frames = _footage(yaw_t, yaw_v, 150, label_s=60.0, offset_s=34.8)
    rot = M.rotation_rate(frames)
    ms = M.measure(tau, rot, EPOCH + yaw_t, yaw_v, EPOCH + 60.0)

    # the footage starts at vehicle time 60 + 34.8 but is labeled 60
    assert ms.checked and ms.confident, ms.message
    assert ms.offset_s == pytest.approx(34.8, abs=0.2)
    assert all(abs(h - 34.8) < 1.0 for h in ms.halves_s)
    assert isinstance(ms.offset_s, float)


def test_measure_does_not_care_how_the_camera_is_mounted():
    """Mirror the rotation: an inverted or flipped mount turns the picture the
    other way. The lag is the same."""
    yaw_t, yaw_v = _yaw_profile(400)
    tau, frames = _footage(yaw_t, yaw_v, 150, label_s=100.0, offset_s=-12.0,
                           sign=-1.0)
    ms = M.measure(tau, M.rotation_rate(frames), EPOCH + yaw_t, yaw_v,
                   EPOCH + 100.0)
    assert ms.confident, ms.message
    assert ms.offset_s == pytest.approx(-12.0, abs=0.2)


def test_a_straight_transect_is_unverified_not_guessed():
    """No turns, nothing to measure: say so rather than pick a lag."""
    yaw_t = np.arange(0, 400, 0.1)
    yaw_v = np.random.default_rng(1).normal(0, 0.05, yaw_t.size)
    tex = _texture()
    frames = np.stack([_view(tex, 0.0) for _ in range(int(150 * M.RATE))])
    frames = np.clip(frames + np.random.default_rng(2).normal(
        0, 2, frames.shape), 0, 255).astype(np.uint8)
    tau = np.arange(len(frames)) / M.RATE
    ms = M.measure(tau, M.rotation_rate(frames), EPOCH + yaw_t, yaw_v, EPOCH + 60)
    assert not ms.confident


def test_too_little_to_compare_is_not_checked():
    ms = M.measure(np.arange(10) / M.RATE, np.zeros(10),
                   np.arange(100.0), np.zeros(100), 0.0)
    assert not ms.checked and not ms.confident


# --------------------------------------------------------------------------
#  what the pipeline does with the answer
# --------------------------------------------------------------------------


def _resolved():
    from rov_imagery_processing.survey import (
        Chapter,
        ResolvedTransect,
        Segment,
        Site,
        Transect,
    )
    ch = Chapter(Path("T1_4K_source.mp4"), 1078.0, 23.976, 3840, 2160, 180, 0.0)
    return ResolvedTransect(
        site=Site("OTS_testing", "testing", "2026-09-14", []),
        transect=Transect("T1", "09:42:45", "10:00:41"),
        segments=[Segment(ch, 0.95, 1076.0)],
        epoch_start=EPOCH, epoch_end=EPOCH + 1076.0,
        covered_s=1076.0, requested_s=1076.0)


def _align(monkeypatch, ms):
    from rov_imagery_processing import pipeline
    from rov_imagery_processing.config import AppConfig

    monkeypatch.setattr(M, "check_transect", lambda *a, **k: ms)
    r = _resolved()
    res = pipeline.RunResult()
    pipeline._align_with_motion([r], store=None, cache=Path("."),
                                app=AppConfig(), res=res)
    return r, res


def test_a_confident_offset_moves_the_telemetry_not_the_footage(monkeypatch):
    ms = M.MotionSync(checked=True, offset_s=34.8, r=-0.79, peak_ratio=33,
                      halves_s=(34.9, 34.8), confident=True, message="ok")
    r, res = _align(monkeypatch, ms)
    assert r.epoch_start == pytest.approx(EPOCH + 34.8)
    assert r.epoch_end - r.epoch_start == pytest.approx(1076.0)
    assert r.segments[0].in_s == pytest.approx(0.95), "the footage is unchanged"
    assert any("34.80s behind" in w for w in res.warnings), res.warnings
    assert "OTS_testing/T1" in res.motion


def test_an_unsure_measurement_changes_nothing_and_says_so(monkeypatch):
    ms = M.MotionSync(checked=True, offset_s=20.0, r=0.2, peak_ratio=2,
                      halves_s=(20.0, -80.0), confident=False, message="weak")
    r, res = _align(monkeypatch, ms)
    assert r.epoch_start == EPOCH
    assert any("trusted as-is" in w for w in res.warnings)


def test_a_negligible_offset_is_reported_but_not_applied(monkeypatch):
    ms = M.MotionSync(checked=True, offset_s=0.1, r=0.8, peak_ratio=30,
                      halves_s=(0.1, 0.1), confident=True, message="ok")
    r, res = _align(monkeypatch, ms)
    assert r.epoch_start == EPOCH
    assert not res.warnings
    assert "OK" in res.summary()
