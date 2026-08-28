"""
Sorting a flight's imagery into its transect folders.

Files are **moved**, not copied, and renamed to ``YYYY_MM_DD_hh-mm-ss`` so a raw
and its preview end up with identical stems::

    photos/transects/T1/GPR/2026_08_25_13-23-17.GPR
    photos/transects/T1/JPG_preview/2026_08_25_13-23-17.JPG

That pairing is the point. It used to depend on the camera's own stem, which
renaming destroyed; deriving both names from one timestamp makes it structural
instead, so it survives anything either file goes through later.

Two decisions worth knowing about:

* **A GPR takes its time from its JPG partner.** GPR is a raw container whose
  metadata we have not verified, and the two are written as a pair by the
  camera, so the preview's EXIF is the more trustworthy source. Its own
  metadata is only consulted when it has no partner.
* **The name for a pair is chosen once.** Deciding independently would let a
  collision (two frames in one second) rename the JPG but not the GPR, quietly
  breaking exactly the pairing this is meant to guarantee.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import layout
from . import photos as ph
from .telemetry import TelemetryStore

ProgressCB = Callable[[float, str], None]

GPR_EXTS = {".gpr", ".dng"}

KEEP, MOVE, DELETE = ph.KEEP, ph.MOVE, ph.DELETE


@dataclass
class SortOptions:
    """What to do during a sort. Defaults are the conservative ones."""

    move_gpr: bool = True
    move_jpg: bool = True
    #: Stamp the telemetry banner onto previews as they are sorted.
    banner_previews: bool = True
    #: keep / move / delete, decided separately because the raws are precious
    #: and the previews are not.
    off_transect_gpr: str = KEEP
    off_transect_jpg: str = KEEP

    def validate(self) -> list[str]:
        errs = []
        for label, v in (("GPR", self.off_transect_gpr),
                         ("JPG", self.off_transect_jpg)):
            if v not in ph.OFF_TRANSECT_CHOICES:
                errs.append(f"off-transect {label} policy must be one of "
                            f"{ph.OFF_TRANSECT_CHOICES}, got {v!r}")
        return errs


@dataclass
class SortReport:
    gpr_moved: int = 0
    jpg_moved: int = 0
    bannered: int = 0
    off_gpr: int = 0
    off_jpg: int = 0
    unmatched_gpr: int = 0
    failed: int = 0
    transects: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Sorted into {len(self.transects)} transect folder(s): "
                 f"{', '.join(self.transects) if self.transects else 'none'}"]
        lines.append(f"  GPR moved : {self.gpr_moved}")
        lines.append(f"  JPG moved : {self.jpg_moved}"
                     + (f" ({self.bannered} bannered)" if self.bannered else ""))
        if self.unmatched_gpr:
            lines.append(f"  {self.unmatched_gpr} GPR had no preview partner "
                         f"and were timed from their own metadata or name")
        if self.off_gpr or self.off_jpg:
            lines.append(f"  off-transect: {self.off_gpr} GPR, {self.off_jpg} JPG")
        if self.failed:
            lines.append(f"  {self.failed} failed")
        return "\n".join(lines)


def _retry(fn, tries: int = 20, wait: float = 0.5):
    """Dropbox holds handles while it uploads; such locks are transient."""
    last = None
    for _ in range(tries):
        try:
            return fn()
        except PermissionError as ex:
            last = ex
            time.sleep(wait)
    raise last if last else OSError("retry failed")


@dataclass
class _Item:
    """One frame, possibly with both a raw and a preview."""

    stem: str
    local: datetime
    epoch: float
    jpg: Path | None = None
    gpr: Path | None = None
    transect: str | None = None
    name_base: str = ""


def plan_sort(
    flight: Path,
    windows: Sequence[tuple[str, float, float]],
    *,
    offset_hours: float | None = None,
) -> tuple[list[_Item], list[str]]:
    """Work out what goes where, without touching anything.

    Separated from the doing so the GUI can show a count before a move that
    cannot be undone.
    """
    flight = Path(flight)
    photos_root = layout.photos_dir(flight)
    jpg_dir = photos_root / layout.JPG
    gpr_dir = photos_root / layout.GPR
    warnings: list[str] = []

    # The preview folder may still be called JPEG on older flights.
    if not jpg_dir.is_dir():
        alt = ph.find_photo_dir(photos_root)
        if alt is not None:
            jpg_dir = alt
            if alt.name != layout.JPG:
                warnings.append(
                    f"previews found in {alt.name}/ rather than {layout.JPG}/"
                )

    items: dict[str, _Item] = {}

    if jpg_dir.is_dir():
        stills, w = ph.index_photos(jpg_dir, fallback_offset_hours=offset_hours)
        warnings.extend(w)
        for p in stills:
            items[p.path.stem.lower()] = _Item(
                stem=p.path.stem, local=p.local, epoch=p.epoch, jpg=p.path
            )

    unmatched = 0
    if gpr_dir.is_dir():
        for g in sorted(gpr_dir.iterdir()):
            if not g.is_file() or g.suffix.lower() not in GPR_EXTS:
                continue
            key = g.stem.lower()
            if key in items:
                items[key].gpr = g
                continue
            # No preview partner: fall back to the raw's own metadata, then to
            # its name. Reported rather than silently dropped.
            got = ph.photo_time(g, offset_hours=offset_hours)
            if got is None:
                warnings.append(
                    f"{g.name}: no preview partner and no readable time; "
                    f"left where it is"
                )
                continue
            epoch, local, _ = got
            items[key] = _Item(stem=g.stem, local=local, epoch=epoch, gpr=g)
            unmatched += 1

    ordered = sorted(items.values(), key=lambda i: i.epoch)
    used: set[str] = set()
    for it in ordered:
        for name, lo, hi in windows:
            if lo <= it.epoch <= hi:
                it.transect = name
                break
        # One name per pair, made unique against the names already handed out
        # so a raw and its preview can never diverge.
        base = it.local.strftime(ph.TIMESTAMP_FMT)
        cand, n = base, 2
        while cand in used:
            cand, n = f"{base}_{n}", n + 1
        used.add(cand)
        it.name_base = cand

    if unmatched:
        warnings.append(f"{unmatched} GPR file(s) had no matching preview")
    return ordered, warnings


def sort_flight(
    flight: Path,
    windows: Sequence[tuple[str, float, float]],
    *,
    store: TelemetryStore | None = None,
    options: SortOptions | None = None,
    style: ph.BandStyle | None = None,
    offset_hours: float | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> SortReport:
    """Move and rename a flight's imagery into its transect folders."""
    opts = options or SortOptions()
    errs = opts.validate()
    if errs:
        raise ValueError(errs[0])
    if opts.banner_previews and store is None:
        raise ValueError("bannering previews needs telemetry; pass `store`")

    flight = Path(flight)
    rep = SortReport()
    items, warns = plan_sort(flight, windows, offset_hours=offset_hours)
    rep.warnings.extend(warns)
    rep.unmatched_gpr = sum(1 for i in items if i.gpr and not i.jpg)
    if not items:
        rep.warnings.append("no imagery found to sort")
        return rep

    total = max(1, len(items))
    for i, it in enumerate(items):
        if cancel is not None and cancel.is_set():
            from .ffmpeg_tools import CancelledError
            raise CancelledError("cancelled")

        if it.transect:
            tdir = layout.ensure_transect(flight, it.transect)
            if it.transect not in rep.transects:
                rep.transects.append(it.transect)

            if it.gpr and opts.move_gpr:
                dest = tdir / layout.GPR / f"{it.name_base}{it.gpr.suffix}"
                try:
                    _retry(lambda it=it, dest=dest: it.gpr.replace(dest))
                    rep.gpr_moved += 1
                except Exception as ex:
                    rep.failed += 1
                    rep.errors.append(f"{it.gpr.name}: {ex}")

            if it.jpg and opts.move_jpg:
                out = tdir / layout.JPG_PREVIEW
                name = f"{it.name_base}{it.jpg.suffix}"
                try:
                    if opts.banner_previews:
                        photo = ph.Photo(it.jpg, it.epoch, it.local)
                        res = ph.stamp_photo(photo, store, out_dir=out,
                                             style=style, name=name)
                        if res.ok and res.output.is_file():
                            _retry(lambda it=it: it.jpg.unlink())
                            rep.jpg_moved += 1
                            rep.bannered += 1
                        elif res.skipped:
                            _retry(lambda it=it, out=out, name=name: it.jpg.replace(out / name))
                            rep.jpg_moved += 1
                    else:
                        _retry(lambda it=it, out=out, name=name: it.jpg.replace(out / name))
                        rep.jpg_moved += 1
                except Exception as ex:
                    rep.failed += 1
                    rep.errors.append(f"{it.jpg.name}: {ex}")
        else:
            _dispose(it.gpr, opts.off_transect_gpr, flight, rep, "gpr")
            _dispose(it.jpg, opts.off_transect_jpg, flight, rep, "jpg")

        if progress and (i + 1) % 5 == 0:
            progress((i + 1) / total, f"sorting {i+1}/{total}")

    rep.transects.sort(key=layout.transect_sort_key)
    if progress:
        progress(1.0, f"sorted {rep.jpg_moved} preview(s), "
                      f"{rep.gpr_moved} raw(s)")
    return rep


def _dispose(path: Path | None, policy: str, flight: Path,
             rep: SortReport, kind: str) -> None:
    """Apply the off-transect policy to one file."""
    if path is None or not path.is_file():
        return
    if kind == "gpr":
        rep.off_gpr += 1
    else:
        rep.off_jpg += 1
    if policy == KEEP:
        return
    try:
        if policy == MOVE:
            dest_dir = (layout.transects_dir(flight) / layout.OFF_TRANSECT
                        / (layout.GPR if kind == "gpr" else layout.JPG))
            dest_dir.mkdir(parents=True, exist_ok=True)
            _retry(lambda: path.replace(ph.unique_path(dest_dir, path.name)))
        elif policy == DELETE:
            _retry(lambda: path.unlink())
    except Exception as ex:
        rep.warnings.append(f"could not {policy} {path.name}: {ex}")
