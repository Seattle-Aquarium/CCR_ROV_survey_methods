"""
Driving one batch from this side of the fence.

The shape of a run: mint a private catalog, point Lightroom at it, and then
watch. Lightroom does the work; this module's job is to know what is happening
and to say so, because a progress bar that stops moving for forty minutes is
indistinguishable from a hang.

Three sources of truth feed the progress bar, in descending order of trust:

1. ``status.txt``, which the plugin writes at every phase change. Authoritative
   for *what* is happening.
2. the scratch catalog's SQLite, polled read-only, which is the only way to see
   AI Denoise advance -- it has no SDK entry point to report from.
3. the count of TIFs on disk, which is the plainest possible measure of the
   export and needs no cooperation from anything.

Cancellation is honest about what it can do. Before Denoise starts it is
immediate. Once Lightroom is computing a Denoise there is no supported way to
interrupt it, so Stop stops *us* -- the export is skipped and the scratch
catalog is thrown away -- and says as much rather than pretending.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import install
from .catalog import CatalogPoller
from .preflight import Preflight, check, tif_dir_for
from .spec import RawDevelopOptions, RawReport

#: How often to look at the status file and the catalog.
_TICK = 0.35

#: Lightroom has to start, open the catalog, and load the plugin before the
#: first status line appears. Cold starts on a slow disk are not quick.
_STARTUP_GRACE = 180.0

#: Once the plugin is talking, a phase that reports no movement for this long
#: has hung. Generously long: importing several hundred raws is slow, and a
#: run killed for being slow is worse than one that finishes late.
_PROGRESS_STALL = 1800.0

#: If the catalog stops advancing for this long during Denoise, stop quoting a
#: number and say something honest instead.
_STALL = 90.0

#: Where each phase sits on the progress bar. Denoise owns most of it because
#: it owns most of the wall clock.
_BANDS = {
    "launching":        (0.00, 0.04),
    "started":          (0.04, 0.06),
    "importing":        (0.06, 0.20),
    "imported":         (0.20, 0.21),
    "cropping":         (0.21, 0.30),
    "cropped":          (0.30, 0.31),
    "awaiting_denoise": (0.31, 0.80),
    "denoising":        (0.31, 0.80),
    "denoised":         (0.80, 0.80),
    "exporting":        (0.80, 0.99),
    "done":             (1.00, 1.00),
}

_TERMINAL = ("done", "error", "stopped")


class RunFailed(RuntimeError):
    """The batch could not be started, or Lightroom reported an error."""


@dataclass
class _Status:
    phase: str = "launching"
    done: int = 0
    total: int = 0
    message: str = ""
    error: str = ""


def _read_status(path: Path) -> _Status | None:
    """The plugin's status file, or None if it is mid-rewrite."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    st = _Status()
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("phase", "message", "error"):
            setattr(st, key, value)
        elif key in ("done", "total"):
            try:
                setattr(st, key, int(float(value)))
            except ValueError:
                pass
    return st if st.phase else None


def _band(phase: str, done: int, total: int) -> float:
    lo, hi = _BANDS.get(phase, (0.0, 0.0))
    if total <= 0:
        return lo
    return lo + (hi - lo) * min(1.0, max(0.0, done / total))


def _write_job(run_dir: Path, source: Path, tif_dir: Path,
               pre: Preflight, opts: RawDevelopOptions) -> Path:
    """The job file, in the key=value form the Lua side parses.

    The crop rectangles are computed here, once, and handed over. Lightroom
    checks them against what it actually produces, so the two halves never have
    to agree about how rounding works -- only about the target.
    """
    lines = [
        "version=1",
        f"run_dir={run_dir}",
        f"source_dir={source}",
        f"tif_dir={tif_dir}",
        f"crop_w={opts.crop_w}",
        f"crop_h={opts.crop_h}",
        f"remove_ca={1 if opts.remove_ca else 0}",
        f"denoise={1 if opts.denoise else 0}",
        f"denoise_amount={opts.denoise_amount}",
        f"bit_depth={opts.bit_depth}",
        f"color_space={opts.color_space}",
        f"tiff_compression={opts.tiff_compression}",
        f"overwrite={1 if opts.overwrite else 0}",
    ]
    for (w, h), rect in sorted(pre.crops.items()):
        lines.append(f"group={w}x{h}|{rect.left:.6f}|{rect.top:.6f}"
                     f"|{rect.right:.6f}|{rect.bottom:.6f}")
    p = run_dir / "job.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _point_lightroom_at(run_dir: Path) -> list[Path]:
    """Leave the pointer where the plugin looks for it.

    Written to both spellings of the local application data folder: the plugin
    builds the path from the home directory, and %LOCALAPPDATA% is not always
    the same place. Writing both costs nothing and removes a failure that would
    look like "the plugin never started".
    """
    written = []
    homed = Path.home() / "AppData" / "Local" / "UTC" / "lightroom"
    for base in {install.utc_root().resolve(), homed.resolve()}:
        try:
            base.mkdir(parents=True, exist_ok=True)
            p = base / "current_job.txt"
            p.write_text(str(run_dir), encoding="utf-8")
            written.append(p)
        except OSError:
            continue
    if not written:
        raise RunFailed("could not write the Lightroom handoff file")
    return written


def _never_started(run_dir: Path) -> str:
    """Explain a silent Lightroom, using whatever evidence there is.

    A plugin that fails to load leaves nothing behind, so this failure used to
    read as "something went wrong somewhere". The boot log distinguishes the
    two real cases: Lightroom never read the plugin at all (not registered, or
    it never finished starting), or it read it and the plugin itself failed.
    """
    boot = install.boot_log()
    trail = ""
    try:
        lines = boot.read_text(encoding="utf-8", errors="replace").strip()
        trail = lines.splitlines()[-1] if lines else ""
    except OSError:
        pass

    if not trail:
        state = install.plugin_registration()
        if state != "registered":
            return ("Lightroom started but never loaded the UTC plug-in "
                    f"(it is '{state}' in Lightroom's own list). Run 'Set up "
                    f"Lightroom' on this page — installing the plug-in on disk "
                    f"is not enough, Lightroom has to be told about it in its "
                    f"Plug-in Manager.")
        return ("Lightroom started but never read the plug-in, even though it "
                "is registered. It may have stopped on a dialog of its own — "
                "open Lightroom by hand and see what it says.")
    return (f"Lightroom loaded the plug-in but it did not get going. Last "
            f"thing it recorded: {trail!r}. See {run_dir / 'plugin.log'} and "
            f"{boot}.")


def _count_tifs(d: Path) -> int:
    try:
        return sum(1 for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in (".tif", ".tiff"))
    except OSError:
        return 0


def _quit_lightroom(proc: subprocess.Popen | None) -> None:
    """End the session we started.

    Terminated rather than asked politely: a graceful quit can raise a
    "back up the catalog?" prompt, and there is nothing in a scratch catalog
    worth backing up -- it is deleted moments later.
    """
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "lightroom.exe"],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass


# --------------------------------------------------------------------------
#  The run
# --------------------------------------------------------------------------

def run_batch(source: Path, options: RawDevelopOptions | None = None,
              progress=None, cancel=None, *,
              preflight: Preflight | None = None) -> RawReport:
    """Process one folder of GPRs. Shaped for `App.submit`."""
    opts = options or RawDevelopOptions()
    src = Path(source).expanduser().resolve()
    say = progress or (lambda f, m="": None)
    stopped = (lambda: bool(cancel and cancel.is_set()))

    started = time.monotonic()
    rep = RawReport(source=src, root=tif_dir_for(src))

    pre = preflight or check(src, opts)
    rep.found = pre.count
    if not pre.ok:
        rep.errors.extend(pre.problems)
        return rep
    assert pre.app is not None

    say(0.01, "preparing Lightroom…")
    install.install_plugin()
    run_dir = install.new_run_dir()
    catalog = install.mint_catalog(run_dir, pre.app)
    tif_dir = pre.tif_dir
    tif_dir.mkdir(parents=True, exist_ok=True)
    before = _count_tifs(tif_dir)

    _write_job(run_dir, src, tif_dir, pre, opts)
    _point_lightroom_at(run_dir)

    status_file = run_dir / "status.txt"
    poller = CatalogPoller(catalog, src, crop_w=opts.crop_w, crop_h=opts.crop_h)

    proc = None
    denoise_started = False
    keep_run_dir = False
    try:
        say(0.02, "starting Lightroom…")
        proc = subprocess.Popen([str(pre.app.exe), str(catalog)])

        # Lightroom does not run a plugin just because it is installed and
        # enabled -- it initialises one only when an entry point it declares
        # is used. So the batch has to be invoked from Lightroom's own menu.
        say(0.03, "waiting for Lightroom, then starting the batch…")
        from .menu import CannotStart, start_batch
        try:
            rep.warnings.extend(start_batch(proc.pid, log_dir=run_dir))
        except CannotStart as ex:
            raise RunFailed(f"The batch could not be started: {ex}") from ex

        st = _Status()
        last_seen = time.monotonic()
        seen_any = False
        while True:
            if stopped():
                (run_dir / "cancel").write_text("x", encoding="utf-8")
                rep.cancelled = True
                if denoise_started:
                    rep.warnings.append(
                        "Stopped during AI Denoise. Lightroom finishes the "
                        "batch it had already started; nothing was exported.")
                break

            fresh = _read_status(status_file)
            if fresh is not None:
                # Movement is a phase change *or* another photo done: a long
                # import holds one phase for minutes while the count climbs.
                if (fresh.phase, fresh.done) != (st.phase, st.done):
                    last_seen = time.monotonic()
                seen_any = True
                st = fresh
            idle = time.monotonic() - last_seen
            if not seen_any and idle > _STARTUP_GRACE:
                raise RunFailed(_never_started(run_dir))
            if seen_any and idle > _PROGRESS_STALL:
                raise RunFailed(
                    f"Lightroom stopped reporting progress during "
                    f"'{st.phase}' after {int(idle / 60)} minutes. See "
                    f"{run_dir / 'plugin.log'}.")

            if proc.poll() is not None and st.phase not in _TERMINAL:
                raise RunFailed(
                    "Lightroom closed before the batch finished. See "
                    f"{run_dir / 'plugin.log'}.")

            # 'awaiting_denoise' means the plugin has parked and is waiting
            # for the Detail panel and Sync to be driven from this side.
            if st.phase == "awaiting_denoise" and not denoise_started:
                denoise_started = True
                _do_denoise(run_dir, pre, opts, poller, say, stopped, rep,
                            pid=proc.pid)
                st.phase = "denoised"
                continue

            if st.phase in _TERMINAL:
                break

            say(_band(st.phase, st.done, st.total),
                st.message or st.phase.replace("_", " "))
            time.sleep(_TICK)

        if st.phase == "error":
            rep.errors.append(st.error or "Lightroom reported an error")
            keep_run_dir = True
        if st.phase == "stopped":
            rep.cancelled = True

        rep.exported = max(0, _count_tifs(tif_dir) - before)
        counts = poller.poll()
        rep.imported = counts.total or rep.imported
        rep.cropped = counts.cropped
        rep.denoised = counts.denoised
        if rep.exported:
            say(1.0, f"{rep.exported} TIF written")

    except RunFailed as ex:
        rep.errors.append(str(ex))
        keep_run_dir = True
    except Exception as ex:                      # pragma: no cover -- last ditch
        rep.errors.append(f"{type(ex).__name__}: {ex}")
        keep_run_dir = True
    finally:
        # Quit first: the catalog, its blob store and its previews cannot be
        # deleted while Lightroom still has them open.
        _quit_lightroom(proc)
        rep.seconds = time.monotonic() - started
        keep = keep_run_dir or rep.cancelled
        install.clean_run_dir(run_dir, keep_diagnostics=keep)
        if keep:
            rep.warnings.append(
                f"Lightroom's log for this run: {run_dir / 'plugin.log'} "
                f"(the scratch catalog and its previews have been deleted)")

    if rep.exported and rep.exported < rep.found and not rep.cancelled:
        rep.warnings.append(
            f"{rep.found - rep.exported} frame(s) produced no TIF.")
    return rep


def _do_denoise(run_dir: Path, pre: Preflight, opts: RawDevelopOptions,
                poller: CatalogPoller, say, stopped, rep: RawReport,
                pid: int | None = None) -> None:
    """Denoise one photo in the Detail panel, then Sync onto the folder."""
    from .denoise_ui import DenoiseUnavailable, denoise_all

    total = pre.count

    def wait_denoised(n: int, timeout: float) -> bool:
        """Block until the catalog shows at least `n` photos denoised.

        The catalog, not the screen: Denoise is a computation, and a control
        that has been clicked is not the same as work that has been done.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stopped():
                return False
            got = poller.poll()
            if got.denoised >= n:
                return True
            label = (f"AI Denoise {got.denoised} of {total}" if not got.unknown
                     else f"AI Denoise running — {got.denoised} of {total} so far")
            say(_band("awaiting_denoise", got.denoised, total), label)
            time.sleep(1.5)
        return False

    say(_band("awaiting_denoise", 0, total), "starting AI Denoise…")
    try:
        rep.warnings.extend(denoise_all(
            pid, total=total, wait_denoised=wait_denoised,
            amount=opts.denoise_amount, log_dir=run_dir,
            cancelled=stopped))
    except DenoiseUnavailable as ex:
        rep.errors.append(f"AI Denoise could not be completed: {ex}")
        (run_dir / "cancel").write_text("x", encoding="utf-8")
        return

    if not stopped():
        (run_dir / "denoise_done").write_text("x", encoding="utf-8")
        say(_band("exporting", 0, total), "exporting TIF…")
