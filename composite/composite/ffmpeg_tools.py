"""
Locating and driving ffmpeg.

ffmpeg is usually not on PATH on the lab machines, but the `imageio-ffmpeg`
wheel ships a static build, so that is the fallback. A real ffmpeg on PATH wins
if one is present.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

log_cb: Callable[[str], None] | None = None


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Carries the tail of stderr, which is where the
    actual reason lives."""

    def __init__(self, cmd: Sequence[str], code: int, tail: str):
        self.cmd = list(cmd)
        self.code = code
        self.tail = tail
        super().__init__(
            f"ffmpeg exited {code}\n"
            f"  command: {' '.join(str(c) for c in cmd[:14])} ...\n"
            f"  stderr tail:\n{tail}"
        )


# --------------------------------------------------------------------------
#  Discovery
# --------------------------------------------------------------------------

_cached_ffmpeg: str | None = None


def find_ffmpeg(explicit: str | None = None) -> str:
    """Absolute path to an ffmpeg binary.

    Raises with actionable instructions rather than failing obscurely later.
    """
    global _cached_ffmpeg
    if explicit and Path(explicit).is_file():
        return explicit
    if _cached_ffmpeg:
        return _cached_ffmpeg

    onpath = shutil.which("ffmpeg")
    if onpath:
        _cached_ffmpeg = onpath
        return onpath

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            _cached_ffmpeg = exe
            return exe
    except Exception:
        pass

    # PyInstaller bundles it next to the executable
    for base in (Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).parent):
        if not str(base):
            continue
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = base / name
            if p.is_file():
                _cached_ffmpeg = str(p)
                return str(p)

    raise FFmpegError(
        ["ffmpeg"], -1,
        "Could not find ffmpeg.\n"
        "  Install it with:  python -m pip install imageio-ffmpeg\n"
        "  or put ffmpeg.exe on your PATH.",
    )


# --------------------------------------------------------------------------
#  Running
# --------------------------------------------------------------------------

# Keep console windows from flashing up when the packaged GUI shells out.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run(
    args: Sequence[str],
    *,
    ffmpeg: str | None = None,
    progress: Callable[[float], None] | None = None,
    total_seconds: float | None = None,
    cancel: threading.Event | None = None,
    tail_lines: int = 25,
) -> str:
    """Run ffmpeg, optionally reporting fractional progress.

    Progress comes from ``-progress pipe:1``, which emits ``key=value`` lines --
    far more reliable to parse than the human-readable status line.

    `cancel` lets the GUI stop a long encode: the process is terminated and
    CancelledError raised, so partial files can be cleaned up by the caller.
    """
    exe = ffmpeg or find_ffmpeg()
    cmd = [exe, "-hide_banner", "-nostdin"]
    if progress is not None and total_seconds:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]

    if log_cb:
        log_cb("ffmpeg " + " ".join(str(a) for a in args[:12]) + " ...")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        errors="replace",
        creationflags=_NO_WINDOW,
    )

    tail: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())
            if len(tail) > tail_lines:
                tail.pop(0)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    try:
        if progress is not None and total_seconds:
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    raise CancelledError("cancelled")
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        micros = float(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    # ffmpeg's out_time_ms is actually microseconds; both keys
                    # carry the same units in practice, so treat them alike.
                    done = micros / 1e6
                    progress(max(0.0, min(1.0, done / total_seconds)))
        else:
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    raise CancelledError("cancelled")
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass

    code = proc.wait()
    t.join(timeout=2)
    if code != 0:
        raise FFmpegError(cmd, code, "\n".join(tail))
    if progress is not None:
        progress(1.0)
    return "\n".join(tail)


class CancelledError(RuntimeError):
    """The user stopped the run."""


# --------------------------------------------------------------------------
#  Probing
# --------------------------------------------------------------------------


@dataclass
class MediaInfo:
    path: str
    duration: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    timecode: str | None = None
    rotation: int = 0
    has_audio: bool = False

    @property
    def ok(self) -> bool:
        return self.duration is not None and self.width is not None


_DUR = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_WH = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")
_TC = re.compile(r"timecode\s*:\s*(\d\d:\d\d:\d\d[:;]\d\d)")
_ROT = re.compile(r"displaymatrix:\s*rotation of\s*(-?[\d.]+)\s*degrees")


def probe(path: str | Path, ffmpeg: str | None = None) -> MediaInfo:
    """Parse ``ffmpeg -i`` output.

    ffprobe is not shipped by imageio-ffmpeg, so we read the banner instead. It
    is stable enough for the handful of fields we need.
    """
    exe = ffmpeg or find_ffmpeg()
    p = subprocess.run(
        [exe, "-hide_banner", "-i", str(path)],
        capture_output=True, universal_newlines=True, errors="replace",
        creationflags=_NO_WINDOW,
    )
    txt = (p.stderr or "") + (p.stdout or "")
    info = MediaInfo(path=str(path))

    if m := _DUR.search(txt):
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if m := _FPS.search(txt):
        info.fps = float(m.group(1))
    if m := _WH.search(txt):
        info.width, info.height = int(m.group(1)), int(m.group(2))
    if m := _TC.search(txt):
        info.timecode = m.group(1)
    if m := _ROT.search(txt):
        info.rotation = int(round(float(m.group(1)))) % 360
    info.has_audio = "Audio:" in txt
    return info


_nvenc_cache: dict[str, bool] = {}


def nvenc_available(codec: str = "h264_nvenc", ffmpeg: str | None = None) -> bool:
    """Whether an NVENC encoder actually initialises on this machine.

    Listing the encoder is not enough -- ffmpeg advertises NVENC even without a
    usable NVIDIA GPU or driver, and it fails only at run time. So we do a
    two-frame trial encode once and remember the answer.
    """
    if codec in _nvenc_cache:
        return _nvenc_cache[codec]
    try:
        exe = ffmpeg or find_ffmpeg()
        p = subprocess.run(
            [exe, "-hide_banner", "-nostats", "-f", "lavfi",
             "-i", "testsrc2=size=256x256:rate=30:duration=0.1",
             "-c:v", codec, "-f", "null", "-"],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
        ok = p.returncode == 0
    except Exception:
        ok = False
    _nvenc_cache[codec] = ok
    if log_cb:
        log_cb(f"{codec}: {'available' if ok else 'not available'}")
    return ok


def fps_rational(fps: float | None) -> str:
    """Exact rational for the NTSC-family rates, so resampling cannot drift."""
    if not fps:
        return "24000/1001"
    for target, rat in (
        (23.976, "24000/1001"), (29.97, "30000/1001"),
        (59.94, "60000/1001"), (47.952, "48000/1001"),
    ):
        if abs(fps - target) < 0.03:
            return rat
    return f"{fps:.6f}"


def timecode_to_seconds(tc: str | None, fps: float | None) -> float | None:
    """GoPro timecode 'HH:MM:SS:FF' -> seconds since local midnight."""
    if not tc:
        return None
    parts = re.split(r"[:;]", tc)
    if len(parts) != 4:
        return None
    h, m, s, f = (float(x) for x in parts)
    return h * 3600 + m * 60 + s + (f / fps if fps else 0.0)
