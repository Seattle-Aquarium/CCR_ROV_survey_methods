"""
Finding the inputs inside a flight folder.

The going-forward convention is::

    <flight>/
        logs/          *.mcap
        photos/
        videos/
            downward/  GoPro MP4s  <- what we composite
            forward/   GoPro MP4s  <- deliberately ignored

Older flights vary (``video/`` vs ``videos/``, mcaps loose at the root,
``downward/video/``), so discovery searches a ranked list of candidate
locations. Nothing is assumed silently: the result carries a note of where each
input came from and any ambiguity, and the GUI shows that for confirmation
before a run starts. Guessing wrong here would composite the forward camera and
waste hours, so it is worth the extra step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}

#: Where downward GoPro video may live, best first. Relative to the flight root.
_DOWNWARD_DIRS = (
    "videos/downward",
    "video/downward",
    "downward/video",
    "downward/videos",
    "downward",
    "videos",
    "video",
    "GoPro",
    "",              # loose in the flight root
)

#: Never composite these -- the forward view comes from the mcap instead.
#: "transects" holds per-transect trims, which are handled separately: they
#: carry the source chapter's timecode, so mixing them in with full-length
#: footage makes a transect resolve against several files at once.
_EXCLUDE_PARTS = ("forward", "composites", "composite", "archive", "gpr",
                  "photos", "photo", "transects", "clips")

_MCAP_DIRS = ("logs", "log", "", "mcap", "telemetry")

# GoPro chaptered naming: GX<chapter><fileno>.MP4, e.g. GX014075 -> file 4075 ch 1
_GOPRO_RE = re.compile(r"^(G[XHOPL])(\d{2})(\d{4})$", re.IGNORECASE)


@dataclass
class VideoChapter:
    path: Path
    file_no: int | None       # GoPro recording id, shared by its chapters
    chapter: int | None       # 1-based chapter within that recording

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Discovery:
    root: Path
    mcaps: list[Path] = field(default_factory=list)
    videos: list[VideoChapter] = field(default_factory=list)
    mcap_dir: Path | None = None
    video_dir: Path | None = None
    logs_dir: Path | None = None
    photos_dir: Path | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.mcaps) and bool(self.videos)

    @property
    def video_paths(self) -> list[Path]:
        return [v.path for v in self.videos]

    def summary(self) -> str:
        lines = [f"Flight folder: {self.root}"]
        lines.append(f"  mcap   : {len(self.mcaps)} file(s)"
                     + (f" in {self._rel(self.mcap_dir)}" if self.mcap_dir else ""))
        for m in self.mcaps:
            lines.append(f"           {m.name}  ({m.stat().st_size / 1e9:.2f} GB)")
        lines.append(f"  video  : {len(self.videos)} file(s)"
                     + (f" in {self._rel(self.video_dir)}" if self.video_dir else ""))
        for v in self.videos:
            lines.append(f"           {v.name}  ({v.path.stat().st_size / 1e9:.2f} GB)")
        for n in self.notes:
            lines.append(f"  note   : {n}")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)

    def _rel(self, p: Path | None) -> str:
        if p is None:
            return "?"
        try:
            r = p.relative_to(self.root)
            return f"./{r}" if str(r) != "." else "./"
        except ValueError:
            return str(p)


def _is_excluded(p: Path, root: Path) -> bool:
    try:
        parts = [s.lower() for s in p.relative_to(root).parts[:-1]]
    except ValueError:
        return False
    return any(part in _EXCLUDE_PARTS for part in parts)


def _parse_gopro(stem: str) -> tuple[int | None, int | None]:
    m = _GOPRO_RE.match(stem)
    if not m:
        return None, None
    return int(m.group(3)), int(m.group(2))


def _prefer_gopro(paths: list[Path]) -> tuple[list[Path], str | None]:
    """If any file carries GoPro chaptered naming, use only those.

    Guards against sweeping up files that merely share the folder -- our own
    rendered composites, for instance, or hand-exported clips. Flights whose
    files were renamed wholesale (no GoPro names at all) are unaffected.
    """
    gopro = [p for p in paths if _GOPRO_RE.match(p.stem)]
    if gopro and len(gopro) != len(paths):
        skipped = sorted(p.name for p in paths if p not in gopro)
        shown = ", ".join(skipped[:4]) + (" ..." if len(skipped) > 4 else "")
        return gopro, f"ignored {len(skipped)} non-GoPro file(s) alongside: {shown}"
    return paths, None


def _sort_videos(paths: list[Path]) -> list[VideoChapter]:
    """Chapter order within a recording, recordings in id order.

    GoPro splits long recordings into ``GX01xxxx``, ``GX02xxxx`` ... where the
    trailing four digits identify the *recording* and the two before it the
    chapter, so a naive alphabetical sort interleaves recordings wrongly.
    """
    chapters = []
    for p in paths:
        fno, ch = _parse_gopro(p.stem)
        chapters.append(VideoChapter(p, fno, ch))

    def key(v: VideoChapter):
        if v.file_no is None:
            return (1, 0, 0, v.path.name.lower())
        return (0, v.file_no, v.chapter or 0, "")

    return sorted(chapters, key=key)


def _find_videos(root: Path, disc: Discovery) -> None:
    for rel in _DOWNWARD_DIRS:
        d = (root / rel) if rel else root
        if not d.is_dir():
            continue
        hits = [
            p for p in sorted(d.iterdir())
            if p.is_file()
            and p.suffix.lower() in VIDEO_EXTS
            and not _is_excluded(p, root)
            and not p.name.startswith(".")
        ]
        if hits:
            hits, note = _prefer_gopro(hits)
            if note:
                disc.notes.append(note)
            disc.videos = _sort_videos(hits)
            disc.video_dir = d
            if rel not in ("videos/downward",):
                disc.notes.append(
                    f"downward video found in './{rel or ''}' rather than "
                    "'videos/downward'"
                )
            return

    # last resort: recursive, still excluding forward/
    hits = [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        and not _is_excluded(p, root)
    ]
    if hits:
        hits, note = _prefer_gopro(hits)
        if note:
            disc.notes.append(note)
        disc.videos = _sort_videos(hits)
        disc.video_dir = hits[0].parent
        disc.warnings.append(
            "video located by a recursive search; confirm these are the "
            "DOWNWARD-facing files before running"
        )


def _find_mcaps(root: Path, disc: Discovery) -> None:
    for rel in _MCAP_DIRS:
        d = (root / rel) if rel else root
        if not d.is_dir():
            continue
        hits = sorted(p for p in d.glob("*.mcap") if p.is_file())
        if hits:
            disc.mcaps = hits
            disc.mcap_dir = d
            if rel != "logs":
                disc.notes.append(f"mcap found in './{rel or ''}' rather than 'logs'")
            return
    hits = sorted(p for p in root.rglob("*.mcap") if p.is_file())
    if hits:
        disc.mcaps = hits
        disc.mcap_dir = hits[0].parent
        disc.warnings.append("mcap located by a recursive search")


def discover(root: str | Path) -> Discovery:
    """Scan a flight folder. Never raises for missing inputs -- the caller
    inspects `ok`, `notes` and `warnings` and shows them to the user."""
    root = Path(root).expanduser().resolve()
    disc = Discovery(root=root)

    if not root.is_dir():
        disc.warnings.append(f"not a folder: {root}")
        return disc

    for name in ("logs", "log"):
        if (root / name).is_dir():
            disc.logs_dir = root / name
            break
    for name in ("photos", "photo"):
        if (root / name).is_dir():
            disc.photos_dir = root / name
            break

    _find_mcaps(root, disc)
    _find_videos(root, disc)

    if not disc.mcaps:
        disc.warnings.append("no .mcap telemetry found")
    if not disc.videos:
        disc.warnings.append("no downward GoPro video found")

    if (root / "videos" / "forward").is_dir() or (root / "forward").is_dir():
        disc.notes.append("a forward/ folder exists and is being ignored, as intended")

    for w in check_local(list(disc.mcaps) + disc.video_paths):
        disc.warnings.append(w)

    if len(disc.mcaps) > 1:
        disc.notes.append(
            f"{len(disc.mcaps)} mcap files will be merged into one timeline"
        )

    return disc


# Windows file attributes marking a cloud file that is not really on disk.
# Dropbox "online-only" files look normal -- full size, right name -- but every
# read streams from the network. A 4 GB mcap in that state takes the tool from
# minutes to hours and looks exactly like a hang, so it is worth naming.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_PLACEHOLDER_MASK = (
    _FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_cloud_placeholder(path: Path) -> bool:
    """True if the file's contents live in the cloud rather than on disk."""
    try:
        attrs = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _PLACEHOLDER_MASK)


def check_local(paths: Iterable[Path]) -> list[str]:
    """Warn about inputs Dropbox has not actually downloaded.

    Recommends the fix rather than only reporting the symptom: pinning makes
    Dropbox fetch the file and keep it, which is what the tool needs.
    """
    out: list[str] = []
    for p in paths:
        if is_cloud_placeholder(p):
            gb = p.stat().st_size / 1e9
            out.append(
                f"{p.name} ({gb:.1f} GB) is an online-only Dropbox file, not yet "
                f"downloaded. Reading it will be extremely slow. Right-click it "
                f"in Explorer and choose 'Make available offline' (or run: "
                f'attrib +P -U "{p}") and wait for it to finish syncing.'
            )
    return out


def output_dirs(root: Path, create: bool = False) -> tuple[Path, Path]:
    """(composites_dir, logs_dir) for a flight, following the new convention."""
    videos = root / "videos"
    if not videos.is_dir() and (root / "video").is_dir():
        videos = root / "video"
    composites = videos / "composites"

    logs = root / "logs"
    if not logs.is_dir() and (root / "log").is_dir():
        logs = root / "log"

    if create:
        composites.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
    return composites, logs
