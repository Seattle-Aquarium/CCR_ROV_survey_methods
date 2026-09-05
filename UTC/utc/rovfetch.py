"""
Copying recordings off the ROV onto a drive you can carry home.

The field workflow this replaces is the slow one: download from the Pi to the
laptop, upload to Dropbox over a MiFi hotspot, then download again onto a
different machine. Every gigabyte crosses a marginal network twice. Writing
straight to a portable drive removes both hops, and works on the days the
hotspot does not.

Three things this is careful about.

**The destination is checked before anything is fetched.** A recording of five
gigabytes cannot be written to a FAT32 volume at all -- the limit is four, and
the failure looks like a permissions problem rather than a size one, which is
exactly how it presented in the field. Free space, the file system, and
whether the drive is even writable are all settled first.

**Files are verified after they land**, by size and by reading the header
back. A copy that ran out of drive halfway is worse than one that never
started, because it looks finished.

**Nothing on the vehicle is ever changed.** Fetching is a read. Freeing space
on the Pi stays a deliberate act in BlueOS's own interface.

The fetch itself takes an `opener` -- anything that turns a recording into a
readable stream. That keeps this testable against a folder on disk now, and
lets the BlueOS transport drop in once the probe has said what the vehicle
actually serves.
"""

from __future__ import annotations

import ctypes
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ProgressCB = Callable[[float, str], None]

#: FAT32 cannot store a file of this size or larger, however much room the
#: drive reports free. Two of the recordings on this programme's own flights
#: are past it (4.94 GiB and 4.41 GiB).
FAT32_MAX = 2 ** 32

#: Leave this much room rather than filling a drive to the last byte, which
#: makes a volume slow and, on some file systems, unreliable.
HEADROOM = 512 * 1024 * 1024

MCAP_MAGIC = b"\x89MCAP0\r\n"


# --------------------------------------------------------------------------
#  where it is going
# --------------------------------------------------------------------------


@dataclass
class Destination:
    root: Path
    filesystem: str = ""
    free_bytes: int = 0
    total_bytes: int = 0
    removable: bool = False
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        gb = self.free_bytes / 2 ** 30
        return (f"{self.root}  {self.filesystem or 'unknown'}  "
                f"{gb:,.1f} GiB free")


def _volume_root(path: Path) -> str:
    drive = Path(path).anchor
    return drive or str(path)


def filesystem_of(path: Path) -> str:
    """NTFS / exFAT / FAT32, or "" when it cannot be determined."""
    try:
        fsbuf = ctypes.create_unicode_buffer(256)
        namebuf = ctypes.create_unicode_buffer(256)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(_volume_root(path)), namebuf, 256,
            None, None, None, fsbuf, 256)
        return fsbuf.value if ok else ""
    except Exception:
        return ""


def is_removable(path: Path) -> bool:
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(
            ctypes.c_wchar_p(_volume_root(path))) == 2
    except Exception:
        return False


def inspect_destination(path: Path, *, need_bytes: int = 0,
                        largest_file: int = 0) -> Destination:
    """Everything that has to be true before a transfer is worth starting."""
    root = Path(path)
    dest = Destination(root=root)

    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
            dest.notes.append(f"created {root}")
        except OSError as ex:
            dest.problems.append(f"cannot create {root}: {ex.strerror or ex}")
            return dest
    if not root.is_dir():
        dest.problems.append(f"{root} is a file, not a folder")
        return dest

    dest.filesystem = filesystem_of(root)
    dest.removable = is_removable(root)
    try:
        usage = shutil.disk_usage(str(root))
        dest.free_bytes, dest.total_bytes = usage.free, usage.total
    except OSError as ex:
        dest.problems.append(f"cannot read free space on {root}: {ex}")
        return dest

    probe = root / ".utc-write-test"
    try:
        probe.write_bytes(b"utc")
        probe.unlink()
    except OSError as ex:
        dest.problems.append(
            f"{root} cannot be written to ({ex.strerror or ex}). Check the "
            f"drive is not read-only or still mounting.")
        return dest

    # The one that bit us: space free, write refused.
    if dest.filesystem.upper() == "FAT32" and largest_file >= FAT32_MAX:
        dest.problems.append(
            f"This drive is formatted FAT32, which cannot hold a file of "
            f"4 GiB or more -- the largest recording here is "
            f"{largest_file / 2 ** 30:.2f} GiB. Free space is not the problem. "
            f"Reformat the drive as exFAT (right-click the drive in Explorer "
            f"-> Format -> exFAT), or use a different drive.")
    elif dest.filesystem.upper() == "FAT32":
        dest.notes.append(
            "This drive is FAT32, so no single file may reach 4 GiB. Nothing "
            "in this batch does, but a longer dive would.")

    if need_bytes and dest.free_bytes < need_bytes + HEADROOM:
        short = (need_bytes + HEADROOM - dest.free_bytes) / 2 ** 30
        dest.problems.append(
            f"Not enough room: {need_bytes / 2 ** 30:,.1f} GiB to copy but "
            f"only {dest.free_bytes / 2 ** 30:,.1f} GiB free. Short by about "
            f"{short:,.1f} GiB.")

    if not dest.removable and dest.filesystem:
        dest.notes.append(
            "This is a fixed drive, not a removable one -- worth confirming it "
            "is the drive you meant.")
    return dest


def flight_logs_dir(root: Path, date: str, site: str) -> Path:
    """``flights/<YYYY_MM_DD>_<site>/logs`` -- the Dropbox layout.

    Laying the drive out the same way means the folder drops into the flights
    tree unchanged when it gets home, rather than needing to be unpicked.
    """
    day = (date or "").replace("-", "_").strip()
    name = f"{day}_{_safe(site)}" if site else day or "flight"
    return Path(root) / "flights" / name / "logs"


def _safe(text: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-_") else "_"
                  for c in (text or "").strip())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


# --------------------------------------------------------------------------
#  what to fetch
# --------------------------------------------------------------------------


@dataclass
class Recording:
    """One recording on the vehicle, as far as it is known before fetching."""

    name: str
    size: int = 0
    #: Absolute epoch span, when the header could be read. A recorder folder
    #: holds strays from other days, and the file name is the arm time in UTC
    #: rather than anything about the content, so the span is what decides.
    start: float | None = None
    end: float | None = None
    ref: str = ""                       # opaque handle for the opener
    covers: list[str] = field(default_factory=list)

    @property
    def span_known(self) -> bool:
        return self.start is not None and self.end is not None


def match_transects(recordings: Iterable[Recording],
                    windows: Sequence[tuple[str, float, float]],
                    margin_s: float = 120.0) -> list[Recording]:
    """Label each recording with the transects it covers.

    Judged on the recorded span, never on the file name or its modification
    time. BlueOS rewrites old recordings when it repairs them, which is how a
    file from a previous day came to look like it belonged to the dive.
    """
    for rec in recordings:
        rec.covers = []
        if not rec.span_known:
            continue
        for name, lo, hi in windows:
            if rec.start <= hi + margin_s and rec.end >= lo - margin_s:
                rec.covers.append(name)
    return list(recordings)


@dataclass
class FetchItem:
    recording: Recording
    dest: Path
    status: str = "pending"     # pending | copied | skipped | failed
    detail: str = ""


@dataclass
class FetchReport:
    items: list[FetchItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def copied(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "copied"]

    @property
    def failed(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "failed"]

    def summary(self) -> str:
        n = len(self.copied)
        gb = sum(i.dest.stat().st_size for i in self.copied
                 if i.dest.is_file()) / 2 ** 30
        bits = [f"{n} copied ({gb:,.1f} GiB)"]
        skipped = sum(1 for i in self.items if i.status == "skipped")
        if skipped:
            bits.append(f"{skipped} already present")
        if self.failed:
            bits.append(f"{len(self.failed)} FAILED")
        return ", ".join(bits)


# --------------------------------------------------------------------------
#  doing it
# --------------------------------------------------------------------------


def verify(path: Path, expected_size: int = 0) -> tuple[bool, str]:
    """Did the file really land whole?

    A transfer that ran out of drive part way looks finished, so the size is
    checked against what the vehicle reported and the magic is read back. This
    does not judge whether the recording itself is sound -- a dive that lost
    power writes a truncated file that is nonetheless copied perfectly.
    """
    if not path.is_file():
        return False, "the file is not there"
    got = path.stat().st_size
    if expected_size and got != expected_size:
        return False, (f"{got:,} bytes arrived but the vehicle reported "
                       f"{expected_size:,}")
    if got < len(MCAP_MAGIC):
        return False, "the file is empty"
    with open(path, "rb") as f:
        if f.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
            return False, "it does not begin like an mcap"
    return True, ""


def fetch(
    recordings: Sequence[Recording],
    opener: Callable[[Recording], object],
    dest_dir: Path,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
    overwrite: bool = False,
    chunk: int = 4 * 1024 * 1024,
) -> FetchReport:
    """Copy each recording into `dest_dir`, verifying every one.

    Writes to a ``.part`` file and renames on success, so an interrupted
    transfer never leaves something that looks like a finished recording.
    """
    rep = FetchReport()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = sum(r.size for r in recordings) or 1
    done = 0

    for rec in recordings:
        out = dest_dir / rec.name
        item = FetchItem(recording=rec, dest=out)
        rep.items.append(item)

        if out.is_file() and not overwrite:
            good, why = verify(out, rec.size)
            if good:
                item.status, item.detail = "skipped", "already downloaded"
                done += rec.size
                if progress:
                    progress(done / total, f"{rec.name}: already here")
                continue
            rep.warnings.append(
                f"{rec.name} was already here but {why}; fetching it again")

        part = out.with_name(out.name + ".part")
        got = 0
        try:
            src = opener(rec)
            with src, open(part, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        from .ffmpeg_tools import CancelledError
                        raise CancelledError("cancelled")
                    buf = src.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    got += len(buf)
                    if progress:
                        progress(min(0.999, (done + got) / total),
                                 f"{rec.name}  {got / 2 ** 30:,.1f} GiB")
            good, why = verify(part, rec.size)
            if not good:
                part.unlink(missing_ok=True)
                item.status, item.detail = "failed", why
                continue
            part.replace(out)
            item.status = "copied"
        except Exception as ex:
            part.unlink(missing_ok=True)
            item.status = "failed"
            item.detail = f"{type(ex).__name__}: {str(ex).splitlines()[0][:120]}"
        finally:
            done += rec.size

    if progress:
        progress(1.0, rep.summary())
    return rep


def local_opener(folder: Path) -> Callable[[Recording], object]:
    """An opener backed by a folder on disk.

    Lets the whole path be exercised without a vehicle, and stands in for the
    BlueOS transport until the probe says what to build.
    """
    folder = Path(folder)

    def _open(rec: Recording):
        return open(folder / rec.name, "rb")

    return _open
