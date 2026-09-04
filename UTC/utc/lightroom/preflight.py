"""
Everything that has to be true before a batch is worth starting.

A run costs somewhere between fifteen minutes and an hour and takes the screen
away for most of it. Every condition that can be checked in a second is checked
in a second, and the answers are phrased as things an operator can act on
rather than as exceptions.

The checks split in two. **Problems** stop the run: no Lightroom, no seed
catalog, a locked screen, a folder of mixed resolutions, not enough disk. Each
one names what to do about it. **Notes** are things worth saying out loud in
the confirmation dialog -- how long this will take, how many gigabytes of TIF
are about to appear -- because a surprise at file 140 is worse than a warning
at file 0.
"""

from __future__ import annotations

import ctypes
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import gpr, install
from .spec import CropImpossible, CropRect, RawDevelopOptions, crop_fractions

#: 16-bit RGB. ZIP typically lands near 60% of this on underwater imagery, but
#: the estimate stays uncompressed -- running out of disk half way through is a
#: worse failure than over-reserving.
_BYTES_PER_PIXEL = 3 * 2

#: Rough wall-clock per photo for AI Denoise on a GPU-equipped workstation.
#: Wide because it depends entirely on the card.
_DENOISE_SECONDS = (5, 20)

_SM_REMOTESESSION = 0x1000
_MAXIMUM_ALLOWED = 0x02000000


@dataclass
class Preflight:
    """The answer, and everything the confirmation dialog needs to say."""

    source: Path
    tif_dir: Path
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    survey: gpr.Survey | None = None
    crops: dict[tuple[int, int], CropRect] = field(default_factory=dict)
    app: install.LightroomApp | None = None
    #: True when the one-time 'Set up Lightroom' step has work to do.
    needs_setup: bool = False
    needs_seed: bool = False
    estimated_bytes: int = 0
    estimated_seconds: tuple[int, int] = (0, 0)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def count(self) -> int:
        return len(self.survey.frames) if self.survey else 0

    def time_note(self) -> str:
        lo, hi = self.estimated_seconds
        if hi <= 0:
            return ""
        return (f"about {lo // 60}-{hi // 60} minutes" if hi >= 120
                else f"under {max(1, hi // 60) + 1} minutes")


def screen_is_available() -> str:
    """"" when the desktop can be driven, else why it cannot.

    UI automation needs a real, unlocked, attached session. A locked screen or
    a disconnected remote desktop has no input desktop to open, and the batch
    would sit forever on a dialog nobody can see.
    """
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):       # pragma: no cover -- not Windows
        return "this feature needs Windows"
    if user32.GetSystemMetrics(_SM_REMOTESESSION):
        return ("this is a remote-desktop session. Denoise has to drive "
                "Lightroom's own window, so run it at the machine itself, "
                "or leave the remote session connected and unlocked.")
    desk = user32.OpenInputDesktop(0, False, _MAXIMUM_ALLOWED)
    if not desk:
        return ("the screen is locked. Denoise takes over the keyboard and "
                "mouse, so the desktop has to be unlocked and signed in.")
    user32.CloseDesktop(desk)
    return ""


def _have_pywinauto() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:
        return False


def tif_dir_for(gpr_dir: Path) -> Path:
    """The TIF folder for a GPR folder: a sibling, never a child.

    Nested inside GPR, the exports would be picked up by anything that scans
    the raw folder -- including this feature's own next run.
    """
    d = Path(gpr_dir)
    return d.parent / "TIF"


def check(source: Path, options: RawDevelopOptions | None = None) -> Preflight:
    """Look at everything, decide nothing. The GUI does the deciding."""
    opts = options or RawDevelopOptions()
    # Absolute from here on. Lightroom runs with a working directory of its
    # own, so a relative path that resolves perfectly well in the shell finds
    # nothing at all once it is handed across.
    src = Path(source).expanduser().resolve()
    out = Preflight(source=src, tif_dir=tif_dir_for(src))

    if not src.is_dir():
        out.problems.append(f"There is no folder at {src}.")
        return out

    # ---- the imagery -------------------------------------------------
    out.survey = gpr.survey(src)
    if not out.survey.frames:
        out.problems.append(
            f"No readable GPR files in {src}."
            + (f" ({len(out.survey.unreadable)} file(s) would not parse.)"
               if out.survey.unreadable else ""))
        return out
    if out.survey.unreadable:
        out.notes.append(
            f"{len(out.survey.unreadable)} file(s) could not be read and will "
            f"be left out.")

    for size in out.survey.sizes:
        try:
            out.crops[size] = crop_fractions(*size, opts.crop_w, opts.crop_h)
        except CropImpossible as ex:
            out.problems.append(f"{size[0]}x{size[1]} frames: {ex}.")
    if not out.survey.uniform:
        sizes = ", ".join(f"{w}x{h} ({n})"
                          for (w, h), n in out.survey.sizes.most_common())
        out.notes.append(
            f"This folder holds more than one frame size ({sizes}). Each gets "
            f"its own crop rectangle, and every export still comes out "
            f"{opts.crop_label}.")

    # ---- the application ---------------------------------------------
    try:
        out.app = install.find_lightroom()
    except install.LightroomNotSetUp as ex:
        out.problems.append(str(ex))
    else:
        if install.lightroom_is_running():
            out.problems.append(
                "Lightroom Classic is already open. This batch runs in its own "
                "scratch catalog, so Lightroom has to be closed first.")
        # Registration, not installation. A plugin sitting on disk that
        # Lightroom has never been told about behaves exactly like no plugin
        # at all, except that the failure takes three minutes to arrive.
        state = install.plugin_registration()
        if state == "absent":
            out.needs_setup = True
            out.problems.append(
                "Lightroom has not been told about the UTC plug-in yet. Use "
                "'Set up Lightroom' -- it is a one-time step.")
        elif state == "disabled":
            out.needs_setup = True
            out.problems.append(
                "The UTC plug-in is installed but switched off in Lightroom's "
                "Plug-in Manager. Enable it there, or use 'Set up Lightroom' "
                "to add it again.")

        if not install.seed_is_ready(out.app):
            out.needs_setup = True
            out.needs_seed = True
            out.problems.append(
                f"Lightroom {out.app.version} still needs its one-time empty "
                f"seed catalog. Use 'Set up Lightroom' first.")

        # Lightroom puts up a modal Warning and never starts if the catalog
        # path is too long, which reads from here as "the plugin never ran".
        why = install.catalog_path_problem(
            install.utc_root() / "runs" / "00000000-000000-000000"
            / "UTC_scratch.lrcat")
        if why:
            out.problems.append(
                f"Lightroom cannot open a catalog from "
                f"{install.utc_root()} -- {why}. That folder follows "
                f"%LOCALAPPDATA%, so a shorter user profile path or an "
                f"un-redirected AppData would fix it.")

    # ---- the desktop --------------------------------------------------
    if opts.denoise:
        why = screen_is_available()
        if why:
            out.problems.append(
                why[0].upper() + why[1:] + " Turn off AI Denoise to run the "
                "crop and export unattended.")
        if not _have_pywinauto():
            out.problems.append(
                "AI Denoise needs the pywinauto package, which is not "
                "installed. Run:  python -m pip install pywinauto")

    # ---- room to land -------------------------------------------------
    n = len(out.survey.frames)
    out.estimated_bytes = n * opts.crop_w * opts.crop_h * _BYTES_PER_PIXEL
    try:
        free = shutil.disk_usage(_nearest_existing(out.tif_dir)).free
    except OSError:
        free = None
    if free is not None and free < out.estimated_bytes:
        out.problems.append(
            f"Not enough room on that drive: {n} TIF(s) need about "
            f"{out.estimated_bytes / 1e9:.0f} GB and {free / 1e9:.0f} GB is "
            f"free.")
    out.notes.append(
        f"{n} TIF at {opts.crop_label}, 16-bit ProPhoto -- roughly "
        f"{out.estimated_bytes / 1e9:.0f} GB into {out.tif_dir.name}/.")

    if opts.denoise:
        out.estimated_seconds = (n * _DENOISE_SECONDS[0], n * _DENOISE_SECONDS[1])
        out.notes.append(
            f"AI Denoise will take {out.time_note()}, and Lightroom holds "
            f"the screen, mouse and keyboard for all of it.")

    # Scratch from earlier runs. Successful ones clean up after themselves and
    # old ones are pruned when a run starts, so this is only ever a handful of
    # failures -- but a handful of failures is still a couple of gigabytes.
    held = install.scratch_bytes()
    if held > 1e9:
        out.notes.append(
            f"{held / 1e9:.1f} GB of scratch from earlier runs is still held "
            f"in {install.utc_root() / 'runs'}; starting a run prunes it.")

    if out.tif_dir.is_dir():
        existing = sum(1 for p in out.tif_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in (".tif", ".tiff"))
        if existing:
            out.notes.append(
                f"{out.tif_dir.name}/ already holds {existing} TIF(s); new "
                + ("exports overwrite matching names."
                   if opts.overwrite else
                   "exports are given new names rather than replacing them."))
    return out


def _nearest_existing(path: Path) -> Path:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p
