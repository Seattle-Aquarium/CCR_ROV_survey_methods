"""
The canonical folder structure for an ROV survey flight.

One module owns the layout so that scaffolding a new flight, sorting imagery
into it, and finding folders to work on later can never drift apart.

::

    2026_08_25_Centennial/
        logs/                       *.mcap
        photos/
            GPR/                    raw GPR, as offloaded
            JPG/                    preview JPG, as offloaded
            transects/
                T1/
                    GPR/                sorted raws
                    JPG_preview/        sorted previews (banner applied in place)
                    JPG_edited/         colour-corrected exports -- NEVER modified
                    JPG_edited_banner/  generated banner copies of the above
                off_transect/       optional home for stills outside every transect
        videos/
            downward/  forward/  composites/

Naming follows what is already in the repo: container folders are plural
because they hold many things (``photos``, ``videos``, ``logs``); modifiers are
singular because they are adjectives (``downward``, ``forward``); and format
labels are singular because they name a format, not a count (``GPR``, ``JPG``).

``JPG_edited`` is load-bearing: those files feed downstream ML, so nothing here
ever writes to them. Banner versions go to ``JPG_edited_banner`` instead, which
makes "remove the banner" a matter of using the originals rather than trying to
undo a lossy edit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

# --------------------------------------------------------------------------
#  Names
# --------------------------------------------------------------------------

LOGS = "logs"
PHOTOS = "photos"
VIDEOS = "videos"

GPR = "GPR"
JPG = "JPG"
TRANSECTS = "transects"
OFF_TRANSECT = "off_transect"

JPG_PREVIEW = "JPG_preview"
JPG_EDITED = "JPG_edited"
JPG_EDITED_BANNER = "JPG_edited_banner"

DOWNWARD = "downward"
FORWARD = "forward"
COMPOSITES = "composites"

#: Created by scaffold(), relative to the flight folder.
BASE_DIRS: tuple[str, ...] = (
    LOGS,
    f"{PHOTOS}/{GPR}",
    f"{PHOTOS}/{JPG}",
    f"{VIDEOS}/{DOWNWARD}",
    f"{VIDEOS}/{FORWARD}",
)

#: Per-transect subfolders. JPG_EDITED is created empty as a signpost: the team
#: needs somewhere obvious to export colour-corrected frames to.
TRANSECT_DIRS: tuple[str, ...] = (GPR, JPG_PREVIEW, JPG_EDITED)

#: Folders that hold stills we may be asked to banner or strip.
IMAGE_FOLDERS: tuple[str, ...] = (JPG_PREVIEW, JPG_EDITED, JPG_EDITED_BANNER)

#: Folders whose contents must never be modified in place.
PROTECTED: frozenset[str] = frozenset({JPG_EDITED})

#: Where a banner copy goes, for folders we refuse to modify in place.
BANNER_COPY_OF = {JPG_EDITED: JPG_EDITED_BANNER}

_TRANSECT_RE = re.compile(r"^T\d+$", re.IGNORECASE)


# --------------------------------------------------------------------------
#  Naming a flight folder
# --------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")


def default_flight_name(on: _date | None = None) -> str:
    """``2026_08_25_`` -- the date prefix, ready for a site name."""
    return f"{(on or _date.today()).strftime('%Y_%m_%d')}_"


def clean_flight_name(text: str) -> str:
    """Make a typed folder name safe without silently changing its meaning.

    Spaces become underscores and characters Windows rejects are dropped;
    anything else is left alone, so the user's name is still recognisable.
    """
    name = str(text).strip().replace(" ", "_")
    name = _SAFE.sub("", name)
    return name.strip("_-")


def validate_flight_name(text: str) -> list[str]:
    """Problems with a proposed flight folder name, worst first."""
    errs: list[str] = []
    cleaned = clean_flight_name(text)
    if not cleaned:
        errs.append("Enter a name for the flight folder.")
        return errs
    # Test against the *cleaned* name for the site part: cleaning strips the
    # trailing separator, so checking the raw text for "ends with _" would let
    # a bare date prefix through and create a folder with no site in its name.
    m = re.match(r"^(\d{4})_(\d{2})_(\d{2})(.*)$", cleaned)
    if not m:
        errs.append("Start the name with the flight date as YYYY_MM_DD.")
    else:
        if not m.group(4).strip("_-"):
            errs.append("Add a site name after the date.")
        mo, day = int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= day <= 31):
            errs.append(f"{m.group(1)}_{m.group(2)}_{m.group(3)} is not a real date.")
    if len(cleaned) > 80:
        errs.append("That name is very long; keep it under 80 characters.")
    return errs


# --------------------------------------------------------------------------
#  Creating the structure
# --------------------------------------------------------------------------


@dataclass
class ScaffoldResult:
    root: Path
    created: list[Path] = field(default_factory=list)
    existed: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.root.is_dir()

    def summary(self) -> str:
        lines = [f"Flight folder: {self.root}"]
        if self.created:
            lines.append(f"  created {len(self.created)} folder(s):")
            for p in self.created:
                lines.append(f"    {p.relative_to(self.root).as_posix()}/")
        if self.existed:
            lines.append(f"  {len(self.existed)} already existed, left alone")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        lines.append("  Drop the mcap into logs/, the GoPro stills into "
                     "photos/GPR and photos/JPG,")
        lines.append("  and the GoPro video into videos/downward.")
        return "\n".join(lines)


def scaffold(parent: Path, name: str) -> ScaffoldResult:
    """Create the empty structure for a new flight.

    Existing folders are left as they are rather than raising, so this is safe
    to re-run on a flight that was half set up by hand.
    """
    parent = Path(parent)
    if not parent.is_dir():
        raise NotADirectoryError(f"not a folder: {parent}")

    cleaned = clean_flight_name(name)
    errs = validate_flight_name(cleaned)
    if errs:
        raise ValueError(errs[0])

    root = parent / cleaned
    res = ScaffoldResult(root=root)
    if root.exists() and any(root.iterdir()):
        res.warnings.append(
            f"{cleaned} already exists and is not empty; missing folders were "
            "added and nothing was removed."
        )
    for rel in BASE_DIRS:
        d = root / rel
        if d.is_dir():
            res.existed.append(d)
        else:
            d.mkdir(parents=True, exist_ok=True)
            res.created.append(d)
    return res


# --------------------------------------------------------------------------
#  Locating things afterwards
# --------------------------------------------------------------------------


def photos_dir(flight: Path) -> Path:
    return Path(flight) / PHOTOS


def transects_dir(flight: Path) -> Path:
    return photos_dir(flight) / TRANSECTS


def transect_dir(flight: Path, name: str) -> Path:
    return transects_dir(flight) / name


def ensure_transect(flight: Path, name: str) -> Path:
    """Create T<n>/ and its subfolders, returning the transect folder."""
    d = transect_dir(flight, name)
    for sub in TRANSECT_DIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def is_transect_name(name: str) -> bool:
    return bool(_TRANSECT_RE.match(name))


def transect_sort_key(name: str) -> tuple[int, str]:
    """T2 before T10. Plain alphabetical gets this wrong."""
    m = re.match(r"^T(\d+)$", name, re.IGNORECASE)
    return (int(m.group(1)), "") if m else (10**6, name.lower())


@dataclass
class ImageFolder:
    """A folder of stills we can act on."""

    path: Path
    kind: str                  # JPG_preview / JPG_edited / JPG_edited_banner
    transect: str | None       # owning transect, when there is one
    flight: Path | None        # owning flight folder, when identifiable
    count: int = 0

    @property
    def protected(self) -> bool:
        return self.kind in PROTECTED

    @property
    def label(self) -> str:
        who = f"{self.transect}/" if self.transect else ""
        return f"{who}{self.kind}  ({self.count} image(s))"


def _flight_of(path: Path) -> Path | None:
    """Walk up to the flight folder that owns a transect subfolder."""
    for parent in path.parents:
        if parent.name == TRANSECTS and parent.parent.name == PHOTOS:
            return parent.parent.parent
    return None


def find_image_folders(
    root: Path,
    *,
    exts: Iterable[str] = (".jpg", ".jpeg"),
) -> list[ImageFolder]:
    """Every JPG_preview / JPG_edited / JPG_edited_banner under `root`.

    Accepts a single transect folder, a flight folder, or a whole parent of
    flights, so the GUI can point at any level and offer what it finds. Empty
    folders are reported too -- knowing that JPG_edited exists but has nothing
    in it is more useful than it silently not appearing.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    exts = {e.lower() for e in exts}
    found: list[ImageFolder] = []
    seen: set[Path] = set()

    def add(d: Path) -> None:
        rp = d.resolve()
        if rp in seen or not d.is_dir():
            return
        seen.add(rp)
        n = sum(1 for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in exts)
        parent = d.parent
        found.append(ImageFolder(
            path=d,
            kind=d.name,
            transect=parent.name if is_transect_name(parent.name) else None,
            flight=_flight_of(d),
            count=n,
        ))

    # the root may itself be one of the folders we handle
    if root.name in IMAGE_FOLDERS:
        add(root)
    for d in sorted(root.rglob("*")):
        if d.is_dir() and d.name in IMAGE_FOLDERS:
            add(d)

    found.sort(key=lambda f: (
        str(f.flight or ""),
        transect_sort_key(f.transect or "~"),
        IMAGE_FOLDERS.index(f.kind) if f.kind in IMAGE_FOLDERS else 9,
    ))
    return found


def banner_target(folder: ImageFolder) -> Path:
    """Where bannered output for this folder should be written.

    Protected folders get a sibling; everything else is stamped in place.
    """
    if folder.kind in BANNER_COPY_OF:
        return folder.path.parent / BANNER_COPY_OF[folder.kind]
    return folder.path
