"""
Reading the frame size out of a GoPro .GPR without decoding it.

A GPR is a DNG in a trench coat: a TIFF container with the same IFD structure,
holding VC-5 wavelet-compressed sensor data instead of Adobe's own compression.
Lightroom reads them natively -- the extension is in Camera Raw's table and the
HERO12 Black is in its camera list -- so nothing here has to decode anything.
We only walk the tags for the pixel dimensions.

Why bother when Lightroom knows the dimensions itself: the crop rectangle is a
fraction of the frame, so its arithmetic needs the frame size *before* the run
starts. Knowing it up front means a folder of mixed resolutions is caught in
preflight -- with a message naming the odd files -- rather than after twenty
minutes of Denoise has already been spent on them.

Deliberately tolerant: a file that will not parse is reported as unknown rather
than raising, because "UTC could not read three of these" is a better failure
than a stack trace over a folder of otherwise fine imagery.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: The extensions a GoPro card offers. Case is whatever the camera wrote.
GPR_SUFFIXES = (".gpr",)

_TAG_WIDTH = 256
_TAG_LENGTH = 257
_TAG_SUBIFDS = 330
_TAG_MODEL = 272
_TAG_ACTIVE_AREA = 50829

#: TIFF type code -> (struct code, byte size)
_FMT = {1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
        6: ("b", 1), 7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8),
        11: ("f", 4), 12: ("d", 8)}


@dataclass(frozen=True)
class Frame:
    """One GPR's native geometry."""

    path: Path
    width: int
    height: int
    model: str = ""

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass
class Survey:
    """What a folder of GPRs looks like, geometrically."""

    folder: Path
    frames: list[Frame] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)

    @property
    def sizes(self) -> Counter:
        return Counter(f.size for f in self.frames)

    @property
    def count(self) -> int:
        return len(self.frames) + len(self.unreadable)

    @property
    def uniform(self) -> bool:
        return len(self.sizes) <= 1

    def describe(self) -> str:
        if not self.frames:
            return "no readable GPR files"
        bits = [f"{w}x{h} ({n})" for (w, h), n in self.sizes.most_common()]
        line = f"{len(self.frames)} GPR: " + ", ".join(bits)
        models = {f.model for f in self.frames if f.model}
        if models:
            line += "   " + ", ".join(sorted(models))
        if self.unreadable:
            line += f"   [{len(self.unreadable)} unreadable]"
        return line


def _read_ifd(buf: bytes, off: int, bo: str) -> dict:
    """One IFD as {tag: [values]}. Unknown types are skipped, not fatal."""
    if off <= 0 or off + 2 > len(buf):
        return {}
    (n,) = struct.unpack_from(bo + "H", buf, off)
    out: dict[int, list] = {}
    for i in range(n):
        e = off + 2 + i * 12
        if e + 12 > len(buf):
            break
        tag, typ, cnt = struct.unpack_from(bo + "HHI", buf, e)
        if typ not in _FMT or cnt > 1 << 20:
            continue
        code, size = _FMT[typ]
        total = size * cnt
        raw = buf[e + 8:e + 12]
        if total > 4:
            (p,) = struct.unpack_from(bo + "I", raw)
            if p + total > len(buf):
                continue
            raw = buf[p:p + total]
        try:
            if typ == 2:
                out[tag] = [raw[:total].split(b"\0")[0].decode("ascii", "replace")]
            elif typ in (5, 10):
                v = struct.unpack(bo + code[0] * 2 * cnt, raw[:total])
                out[tag] = [v[j] / v[j + 1] if v[j + 1] else 0
                            for j in range(0, len(v), 2)]
            else:
                out[tag] = list(struct.unpack(bo + code * cnt, raw[:total]))
        except struct.error:
            continue
    return out


#: The tags all sit in the first few kilobytes. Reading the whole 13 MB of
#: every frame to find two integers is 2.3 GB of I/O over a survey folder, so
#: take a head first and only fall back to the full file if an offset in it
#: points past the end.
_HEAD_BYTES = 1 << 20


def read_frame(path: Path) -> Frame | None:
    """The native pixel size of one GPR, or None if it will not parse."""
    p = Path(path)
    for limit in (_HEAD_BYTES, None):
        got = _parse(p, limit)
        if got is not None:
            return got
        if limit is None:
            break
    return None


def _parse(p: Path, limit: int | None) -> Frame | None:
    try:
        with p.open("rb") as fh:
            buf = fh.read(limit) if limit else fh.read()
    except OSError:
        return None
    if len(buf) < 16 or buf[:2] not in (b"II", b"MM"):
        return None
    bo = "<" if buf[:2] == b"II" else ">"
    try:
        (first,) = struct.unpack_from(bo + "I", buf, 4)
    except struct.error:
        return None

    ifd0 = _read_ifd(buf, first, bo)
    model = str((ifd0.get(_TAG_MODEL) or [""])[0]).strip()

    # The full-resolution image lives in a SubIFD; IFD0 usually holds a
    # thumbnail, so take the largest plane rather than the first.
    best = (0, 0)
    for sub in ifd0.get(_TAG_SUBIFDS, []):
        d = _read_ifd(buf, int(sub), bo)
        w = int((d.get(_TAG_WIDTH) or [0])[0])
        h = int((d.get(_TAG_LENGTH) or [0])[0])
        if not (w and h):
            aa = d.get(_TAG_ACTIVE_AREA)
            if aa and len(aa) == 4:
                h, w = int(aa[2] - aa[0]), int(aa[3] - aa[1])
        if w * h > best[0] * best[1]:
            best = (w, h)
    if best == (0, 0):
        best = (int((ifd0.get(_TAG_WIDTH) or [0])[0]),
                int((ifd0.get(_TAG_LENGTH) or [0])[0]))
    if best[0] <= 0 or best[1] <= 0:
        return None
    return Frame(path=p, width=best[0], height=best[1], model=model)


def list_gpr(folder: Path) -> list[Path]:
    """Every GPR directly in `folder`, sorted. Not recursive -- a batch is one
    folder, and walking into subfolders would silently widen the job."""
    d = Path(folder)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in GPR_SUFFIXES),
                  key=lambda p: p.name.lower())


def survey(folder: Path) -> Survey:
    """Read every GPR in `folder` and report the geometry found."""
    out = Survey(folder=Path(folder))
    for p in list_gpr(folder):
        f = read_frame(p)
        if f is None:
            out.unreadable.append(p)
        else:
            out.frames.append(f)
    return out


def find_folders(root: Path) -> list[Path]:
    """Every non-empty ``GPR`` folder under `root`, transect order first.

    A batch is one folder, but an import scatters raws across one folder per
    transect -- so the card offers what is actually there rather than guessing
    which was meant. Lives here rather than in `layout` to keep this feature's
    footprint inside its own package.
    """
    from .. import layout

    root = Path(root)
    if not root.is_dir():
        return []

    found: list[Path] = []
    seen: set[Path] = set()

    def add(d: Path) -> None:
        rp = d.resolve()
        if rp in seen or not d.is_dir():
            return
        try:
            if not any(p.suffix.lower() in GPR_SUFFIXES
                       for p in d.iterdir() if p.is_file()):
                return
        except OSError:
            return
        seen.add(rp)
        found.append(d)

    if root.name == layout.GPR:
        add(root)
    for d in sorted(root.rglob(layout.GPR)):
        if d.is_dir():
            add(d)

    def key(d: Path):
        parent = d.parent.name
        if layout.is_transect_name(parent):
            return (0, layout.transect_sort_key(parent))
        return (1, (0, str(d).lower()))

    found.sort(key=key)
    return found
