"""
Pulling imagery straight off a GoPro card into a flight's transect folders.

The point is to copy only what the transects need. A card holds a whole day;
a survey is three windows out of it, and copying the rest costs time twice --
once off the card and again into Dropbox.

Everything here is **read-only with respect to the card**. Nothing is moved,
renamed or deleted on it, ever. The card is the last surviving copy until the
import finishes, and no amount of convenience is worth writing to it.

Card layout is the standard GoPro one::

    DCIM/100GOPRO/GX010123.MP4  G0010124.JPG  G0010124.GPR

but the scan is recursive, so a card that has been reorganised still works.
"""

from __future__ import annotations

import shutil
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import layout
from . import photos as ph
from .telemetry import TelemetryStore

ProgressCB = Callable[[float, str], None]

JPG_EXTS = {".jpg", ".jpeg"}
RAW_EXTS = {".gpr", ".dng"}
VIDEO_EXTS = {".mp4", ".mov"}


# --------------------------------------------------------------------------
#  Finding a card
# --------------------------------------------------------------------------


@dataclass
class Drive:
    path: Path
    label: str
    total_gb: float
    free_gb: float
    removable: bool

    @property
    def caption(self) -> str:
        kind = "removable" if self.removable else "fixed"
        return (f"{self.path}  {self.label or '(no label)'}  "
                f"{self.total_gb:.0f} GB {kind}")


def list_drives(removable_only: bool = True) -> list[Drive]:
    """Mounted volumes, so a card can be picked rather than typed.

    Windows only in practice; elsewhere this returns nothing and the user
    browses to the mount point instead.
    """
    import ctypes
    import os

    out: list[Drive] = []
    if os.name != "nt":
        return out
    try:
        k32 = ctypes.WinDLL("kernel32")
    except Exception:
        return out

    DRIVE_REMOVABLE = 2
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            kind = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except Exception:
            continue
        if kind not in (DRIVE_REMOVABLE, 3):          # 3 = fixed
            continue
        removable = kind == DRIVE_REMOVABLE
        if removable_only and not removable:
            continue
        name = ctypes.create_unicode_buffer(261)
        try:
            k32.GetVolumeInformationW(ctypes.c_wchar_p(root), name, 260,
                                      None, None, None, None, 0)
        except Exception:
            pass
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue                                   # empty reader slot
        out.append(Drive(Path(root), name.value, usage.total / 1e9,
                         usage.free / 1e9, removable))
    return out


# --------------------------------------------------------------------------
#  Scanning
# --------------------------------------------------------------------------


@dataclass
class CardFrame:
    """One still on the card, with its raw partner when there is one."""

    stem: str
    epoch: float
    local: datetime
    jpg: Path | None = None
    gpr: Path | None = None
    transect: str | None = None
    name_base: str = ""

    @property
    def bytes(self) -> int:
        return sum(p.stat().st_size for p in (self.jpg, self.gpr) if p)


@dataclass
class CardScan:
    root: Path
    frames: list[CardFrame] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)
    undated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_photos(self) -> bool:
        return bool(self.frames)

    def summary(self) -> str:
        lines = [f"Card: {self.root}"]
        jpg = sum(1 for f in self.frames if f.jpg)
        gpr = sum(1 for f in self.frames if f.gpr)
        lines.append(f"  {len(self.frames)} frame(s): {jpg} JPG, {gpr} GPR")
        if self.videos:
            gb = sum(p.stat().st_size for p in self.videos) / 1e9
            lines.append(f"  {len(self.videos)} video file(s), {gb:.1f} GB")
        if self.undated:
            lines.append(f"  {len(self.undated)} file(s) with no readable time")
        if self.frames:
            lines.append(f"  spans {self.frames[0].local:%H:%M:%S} .. "
                         f"{self.frames[-1].local:%H:%M:%S} local")
        return "\n".join(lines)


def scan_card(
    root: Path,
    *,
    tz_name: str | None = None,
    progress: ProgressCB | None = None,
) -> CardScan:
    """Index a card without copying anything.

    Stills are timed from EXIF; a raw takes its partner's time, because GPR is
    a container whose own metadata we have not verified and the two are written
    as a pair.
    """
    root = Path(root)
    scan = CardScan(root=root)
    if not root.is_dir():
        scan.warnings.append(f"not a folder: {root}")
        return scan

    jpgs: list[Path] = []
    raws: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in JPG_EXTS:
            jpgs.append(p)
        elif ext in RAW_EXTS:
            raws.append(p)
        elif ext in VIDEO_EXTS:
            scan.videos.append(p)

    by_stem: dict[str, CardFrame] = {}
    total = max(1, len(jpgs))
    for i, p in enumerate(jpgs):
        got = ph.photo_time(p, tz_name=tz_name)
        if got is None:
            scan.undated.append(p.name)
            continue
        epoch, local, _ = got
        by_stem[p.stem.lower()] = CardFrame(p.stem, epoch, local, jpg=p)
        if progress and i % 25 == 0:
            progress(i / total, f"reading {i}/{total}")

    orphan_raw = 0
    for g in raws:
        key = g.stem.lower()
        if key in by_stem:
            by_stem[key].gpr = g
            continue
        got = ph.photo_time(g, tz_name=tz_name)
        if got is None:
            scan.undated.append(g.name)
            continue
        epoch, local, _ = got
        by_stem[key] = CardFrame(g.stem, epoch, local, gpr=g)
        orphan_raw += 1

    scan.frames = sorted(by_stem.values(), key=lambda f: f.epoch)
    if orphan_raw:
        scan.warnings.append(f"{orphan_raw} raw file(s) had no JPG partner")
    if scan.undated:
        shown = ", ".join(scan.undated[:3]) + (" ..." if len(scan.undated) > 3 else "")
        scan.warnings.append(
            f"{len(scan.undated)} file(s) carry no readable time and cannot be "
            f"placed on the timeline: {shown}"
        )
    if progress:
        progress(1.0, f"{len(scan.frames)} frame(s) on the card")
    return scan


def assign(scan: CardScan, windows: Sequence[tuple[str, float, float]]) -> None:
    """Label each frame with its transect, and give it its final name."""
    used: set[str] = set()
    for f in scan.frames:
        f.transect = None
        for name, lo, hi in windows:
            if lo <= f.epoch <= hi:
                f.transect = name
                break
        base = f.local.strftime(ph.TIMESTAMP_FMT)
        cand, n = base, 2
        while cand in used:
            cand, n = f"{base}_{n}", n + 1
        used.add(cand)
        f.name_base = cand


@dataclass
class ImportPlan:
    """What an import would do, so it can be shown before it is done."""

    per_transect: dict[str, int] = field(default_factory=dict)
    off_transect: int = 0
    copy_bytes: int = 0
    skip_bytes: int = 0

    @property
    def on_transect(self) -> int:
        return sum(self.per_transect.values())

    def summary(self) -> str:
        lines = []
        for name, n in self.per_transect.items():
            lines.append(f"   {name}: {n} frame(s)")
        lines.append(f"   off-transect: {self.off_transect}")
        lines.append(f"   to copy: {self.copy_bytes / 1e9:.2f} GB")
        if self.skip_bytes:
            lines.append(f"   skipped: {self.skip_bytes / 1e9:.2f} GB")
        return "\n".join(lines)


@dataclass
class ImportOptions:
    copy_gpr: bool = True
    copy_jpg: bool = True
    banner_previews: bool = True
    #: Bring frames outside every transect across too, into off_transect/.
    #: On by default: the card is usually wiped straight after, and a wrong
    #: transect time is only recoverable while this copy exists.
    include_off_transect: bool = True


def plan_import(
    scan: CardScan,
    windows: Sequence[tuple[str, float, float]],
    options: ImportOptions | None = None,
) -> ImportPlan:
    """Counts and bytes for a proposed import. Touches nothing."""
    opts = options or ImportOptions()
    assign(scan, windows)
    plan = ImportPlan()
    for name, _a, _b in windows:
        plan.per_transect.setdefault(name, 0)

    for f in scan.frames:
        size = sum(p.stat().st_size for p in
                   (f.jpg if opts.copy_jpg else None,
                    f.gpr if opts.copy_gpr else None) if p)
        if f.transect:
            plan.per_transect[f.transect] = plan.per_transect.get(f.transect, 0) + 1
            plan.copy_bytes += size
        else:
            plan.off_transect += 1
            if opts.include_off_transect:
                plan.copy_bytes += size
            else:
                plan.skip_bytes += size
    return plan


@dataclass
class ImportReport:
    copied_jpg: int = 0
    copied_gpr: int = 0
    bannered: int = 0
    off_transect: int = 0
    skipped: int = 0
    failed: int = 0
    transects: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Imported into {len(self.transects)} transect folder(s): "
                 f"{', '.join(self.transects) if self.transects else 'none'}"]
        lines.append(f"  JPG copied: {self.copied_jpg}"
                     + (f" ({self.bannered} bannered)" if self.bannered else ""))
        lines.append(f"  GPR copied: {self.copied_gpr}")
        if self.off_transect:
            lines.append(f"  off-transect: {self.off_transect}")
        if self.skipped:
            lines.append(f"  already present, skipped: {self.skipped}")
        if self.failed:
            lines.append(f"  failed: {self.failed}")
        return "\n".join(lines)


def import_photos(
    scan: CardScan,
    flight: Path,
    windows: Sequence[tuple[str, float, float]],
    *,
    store: TelemetryStore | None = None,
    options: ImportOptions | None = None,
    style: ph.BandStyle | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> ImportReport:
    """Copy the wanted frames off the card into the flight's transect folders.

    Copies, never moves: the card keeps its originals until the operator
    chooses to reformat it.
    """
    opts = options or ImportOptions()
    if opts.banner_previews and opts.copy_jpg and store is None:
        raise ValueError("bannering previews needs telemetry; pass `store`")

    flight = Path(flight)
    rep = ImportReport()
    assign(scan, windows)
    wanted = [f for f in scan.frames
              if f.transect or opts.include_off_transect]
    total = max(1, len(wanted))

    for i, f in enumerate(wanted):
        if cancel is not None and cancel.is_set():
            from .ffmpeg_tools import CancelledError
            raise CancelledError("cancelled")

        if f.transect:
            tdir = layout.ensure_transect(flight, f.transect)
            gpr_dir = tdir / layout.GPR
            jpg_dir = tdir / layout.JPG_PREVIEW
            if f.transect not in rep.transects:
                rep.transects.append(f.transect)
        else:
            base = layout.transects_dir(flight) / layout.OFF_TRANSECT
            gpr_dir, jpg_dir = base / layout.GPR, base / layout.JPG
            rep.off_transect += 1

        if f.gpr and opts.copy_gpr:
            gpr_dir.mkdir(parents=True, exist_ok=True)
            dest = gpr_dir / f"{f.name_base}{f.gpr.suffix}"
            try:
                if dest.exists():
                    rep.skipped += 1
                else:
                    shutil.copy2(f.gpr, dest)
                    rep.copied_gpr += 1
            except Exception as ex:
                rep.failed += 1
                rep.errors.append(f"{f.gpr.name}: {ex}")

        if f.jpg and opts.copy_jpg:
            jpg_dir.mkdir(parents=True, exist_ok=True)
            name = f"{f.name_base}{f.jpg.suffix}"
            dest = jpg_dir / name
            try:
                if dest.exists():
                    rep.skipped += 1
                elif opts.banner_previews and f.transect:
                    photo = ph.Photo(f.jpg, f.epoch, f.local)
                    res = ph.stamp_photo(photo, store, out_dir=jpg_dir,
                                         style=style, name=name)
                    if res.ok:
                        rep.copied_jpg += 1
                        rep.bannered += 1
                else:
                    shutil.copy2(f.jpg, dest)
                    rep.copied_jpg += 1
            except Exception as ex:
                rep.failed += 1
                rep.errors.append(f"{f.jpg.name}: {ex}")

        if progress and (i + 1) % 5 == 0:
            progress((i + 1) / total, f"importing {i+1}/{total}")

    rep.transects.sort(key=layout.transect_sort_key)
    rep.warnings.extend(scan.warnings)
    if progress:
        progress(1.0, f"imported {rep.copied_jpg} JPG, {rep.copied_gpr} GPR")
    return rep
