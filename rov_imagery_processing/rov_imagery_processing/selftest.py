"""
A health check the packaged build can run on itself.

Several things that work in development break only once frozen: a hidden import
that PyInstaller did not find, a bundled font or timezone database that did not
make it in, and — the reason this exists — multiprocessing. On Windows a worker
process is started by re-launching the executable, so a packaged app that omits
``multiprocessing.freeze_support()`` does not fail with an error. Each worker
reaches the GUI's entry point and opens another window, which starts workers of
its own.

A windowed build discards stdout, so the report is written to a file as well as
printed. Run it with::

    Underwater-Telemetry-Compositing.exe --selftest
    Underwater-Telemetry-Compositing.exe --selftest C:\\path\\to\\report.txt
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

#: More than `overlay._PARALLEL_FLOOR`, so the parallel path is the one tested.
_FRAMES = 480


class _Store:
    """Telemetry that varies with time, so the frames genuinely differ."""

    def sample(self, when: float) -> dict:
        k = when % 100
        return {"depth": 3.0 + k / 50.0, "altitude": 0.5 + (k % 7) / 10.0,
                "speed": 0.1, "lights": 80.0, "power_w": 240.0 + k,
                "mode": "STABILIZE", "heading": k * 3.0, "temp_c": 13.1,
                "roll": 0.0, "pitch": -1.0, "yaw": k, "climb": 0.0,
                "gain": 30.0, "cam_tilt": 0.5, "voltage_v": 13.5,
                "current_a": 18.0, "throttle": 50.0}


def _checks():
    """Yield (name, ok, detail) for each thing a fresh machine can get wrong."""
    frozen = bool(getattr(sys, "frozen", False))
    yield ("build", None,           # None = informational, never a failure
           "packaged executable" if frozen else "running from source")

    # Timezone database -- without it every transect resolves to the wrong instant.
    try:
        import datetime as dt

        from .survey import timezone_data_available, utc_offset_hours
        ok = timezone_data_available()
        off = utc_offset_hours(dt.date(2026, 8, 31)) if ok else None
        yield ("timezone database", ok, f"Aug 2026 offset {off}" if ok else "MISSING")
    except Exception as ex:
        yield ("timezone database", False, f"{type(ex).__name__}: {ex}")

    # ffmpeg -- every trim, composite and clip shells out to it.
    try:
        from .ffmpeg_tools import find_ffmpeg
        exe = find_ffmpeg()
        yield ("ffmpeg", bool(exe), exe)
    except Exception as ex:
        yield ("ffmpeg", False, f"{type(ex).__name__}: {ex}")

    # The brand typeface, used by both the GUI and the photo banner.
    try:
        from . import brand
        p = brand.font_path("medium")
        yield ("Montserrat", bool(p), p or "not found -- falling back")
    except Exception as ex:
        yield ("Montserrat", False, f"{type(ex).__name__}: {ex}")

    # Reading the autopilot's own logs.
    try:
        from pymavlink import mavutil  # noqa: F401
        yield ("pymavlink", True, "importable")
    except Exception as ex:
        yield ("pymavlink", False, f"{type(ex).__name__}: {ex}")

    # The Transects page. Worth its own line because the page imports the
    # extractor lazily and reports a missing one as a message rather than a
    # crash -- so a build that shipped without it looks healthy right up until
    # someone tries to extract a transect.
    try:
        from ccr_m2c import tide, transect
        yield ("transect extractor", True,
               f"{len(transect.OUTPUT_COLUMNS)} columns, "
               f"{len(tide.STATIONS)} tide stations")
    except Exception as ex:
        yield ("transect extractor", False, f"{type(ex).__name__}: {ex}")

    # The one this file exists for.
    yield _overlay_check()


def _overlay_check():
    """Render a short sequence twice -- on one core, then on several.

    Identical output proves the workers really ran and really produced the same
    thing. A packaged build missing freeze_support does not reach this line
    cleanly; it spawns GUIs instead.
    """
    import hashlib
    import shutil

    from . import overlay
    from .config import Layout

    cfg = Layout()
    tmp = Path(tempfile.mkdtemp(prefix="utc_selftest_"))
    try:
        def digest(d: Path):
            return [(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in sorted(d.iterdir())]

        def footer(epoch: float) -> str:
            return f"selftest  |  {int(epoch) % 86400:05d}"

        dur = _FRAMES / cfg.overlay_fps
        t0 = time.time()
        overlay.render_sequence(tmp / "one", _Store(), 1_788_000_000.0, dur,
                                cfg, footer_text=footer, workers=1)
        serial = time.time() - t0

        nw = min(4, overlay.worker_count())
        t0 = time.time()
        overlay.render_sequence(tmp / "many", _Store(), 1_788_000_000.0, dur,
                                cfg, footer_text=footer, workers=nw)
        par = time.time() - t0

        same = digest(tmp / "one") == digest(tmp / "many")
        n = len(list((tmp / "many").glob("panel_*.png")))
        detail = (f"{n} frames, {serial:.1f}s on 1 core vs {par:.1f}s on {nw}; "
                  f"output {'identical' if same else 'DIFFERS'}")
        return ("parallel overlay render", same and n == _FRAMES, detail)
    except Exception as ex:
        return ("parallel overlay render", False, f"{type(ex).__name__}: {ex}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(argv: list[str] | None = None) -> int:
    """Run every check, report, and return a process exit code."""
    argv = list(argv or sys.argv)
    dest = None
    if "--selftest" in argv:
        i = argv.index("--selftest")
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            dest = Path(argv[i + 1])
    if dest is None:
        dest = Path(tempfile.gettempdir()) / "utc_selftest.txt"

    lines = [f"UTC self-test  --  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"python {sys.version.split()[0]}, {os.cpu_count()} cores", ""]
    failed = 0
    for name, ok, detail in _checks():
        if ok is None:                       # context, not a check
            mark = "--  "
        else:
            mark = "ok  " if ok else "FAIL"
            failed += 0 if ok else 1
        lines.append(f"  [{mark}]  {name:24s} {detail}")
    lines += ["", "ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED"]

    text = "\n".join(lines)
    print(text)
    try:
        dest.write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {dest}")
    except Exception:
        pass
    return 1 if failed else 0
