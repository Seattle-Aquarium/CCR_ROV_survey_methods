"""
The files on the ROV's Pi: what is there, fetching it, and clearing it off.

This is the **one module in the program that can change the vehicle.** Every
other module that talks to BlueOS issues GET requests and nothing else, and
their tests hold them to it. Deleting lives here, alone, so that property
stays checkable: a test can assert that nothing outside this file ever sends
anything but a GET, and the few functions here that do are named for it.

Why deleting is here at all. BlueOS's recorder sweeps old recordings looking
for ones to repair, and it does so while the vehicle is flying -- on a Pi
already recording a dive, re-reading multi-gigabyte files whose video is
embedded in them. The survey protocol now downloads every flight's logs the
same day and checks them in the lab before the next flight, so by the time the
next dive starts the old files are known to be safe elsewhere, and clearing
them is what stops the sweep having anything to chew on.

What guards the delete:

* **Never while armed.** The vehicle is asked first, and a vehicle that says
  it is armed is refused outright.
* **Never a file that is still being written.** Anything modified in the last
  couple of minutes, by the vehicle's own clock, is left alone and reported.
* **Only what was listed.** Files are deleted one by one by the path they were
  listed under; a folder is removed only once it is empty.
* **A record is kept.** Every deletion is written to a text file, in the
  flight's logs folder when there is one.

The file types, and where BlueOS keeps them (as its File Browser addresses
them):

==============  ==========================================  =================
type            on the vehicle                              lands in
==============  ==========================================  =================
mcap            recorder/*.mcap                              logs/mcap
mcap video      recorder/<recording>/*.mp4                   logs/mcap_video
BIN             ardupilot_logs/firmware/logs/*.BIN           logs/BIN
tlog            ardupilot_logs/logs/**/*.tlog (older BlueOS) logs/tlog
C3 imagery      the folder Madrona saves into                photos/C3
==============  ==========================================  =================
"""

from __future__ import annotations

import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import blueos

ProgressCB = Callable[[float, str], None]

#: Leave anything modified this recently alone: it may still be being written.
RECENT_S = 120.0

#: How far outside a transect a file's span may end and still count as
#: covering it -- the same allowance the recording matcher has always used.
MARGIN_S = 120.0

TLOG_FB_PATHS = ("/ardupilot_logs/logs",)

#: Where Madrona's folder is looked for when none has been set. Its docs say
#: images go "underneath the folder they select", so there is no fixed path;
#: a folder holding left/, right/ and center/ is what gives it away.
C3_SEARCH_ROOTS = ("/system_root/usr/blueos/userdata",
                   "/system_root/usr/blueos/extensions",
                   "/system_root/root")
C3_EYES = ("left", "right", "center")
C3_SEARCH_DEPTH = 4

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".h264", ".h265", ".ts")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    #: Where it lands, relative to the flight folder.
    dest: str


CATEGORIES: tuple[Category, ...] = (
    Category("mcap", "mcap", "logs/mcap"),
    Category("video", "mcap video", "logs/mcap_video"),
    Category("bin", "BIN", "logs/BIN"),
    Category("tlog", "tlog", "logs/tlog"),
    Category("c3", "C3 imagery", "photos/C3"),
)
BY_KEY = {c.key: c for c in CATEGORIES}


@dataclass
class PiFile:
    """One file on the vehicle."""

    category: str
    path: str                     # as File Browser addresses it
    rel: str                      # under its category's root, "/"-separated
    size: int = 0
    modified: float | None = None
    #: The span it recorded, as epoch seconds on the vehicle's clock. None
    #: when it could not be worked out, which is reported rather than guessed.
    start: float | None = None
    end: float | None = None
    covers: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def span_known(self) -> bool:
        return self.start is not None and self.end is not None

    def dest_in(self, flight_dir: Path) -> Path:
        return Path(flight_dir) / BY_KEY[self.category].dest / Path(*self.rel.split("/"))

    def downloaded_to(self, flight_dir: Path | None) -> bool:
        """A copy of the same size is already in the flight folder."""
        if not flight_dir:
            return False
        p = self.dest_in(flight_dir)
        try:
            return p.is_file() and p.stat().st_size == self.size
        except OSError:
            return False


@dataclass
class Inventory:
    """What a search found, by type."""

    host: str = ""
    token: str = ""
    vehicle: str = ""
    files: dict[str, list[PiFile]] = field(default_factory=dict)
    roots: dict[str, list[str]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    skew: float | None = None
    listed_at: float = 0.0

    @property
    def listed(self) -> set[str]:
        return set(self.files)

    def all_files(self) -> list[PiFile]:
        return [f for c in CATEGORIES for f in self.files.get(c.key, [])]

    def merge(self, other: Inventory) -> None:
        """Fold a later search of other types into this one."""
        self.host, self.token = other.host or self.host, other.token or self.token
        self.vehicle = other.vehicle or self.vehicle
        self.skew = other.skew if other.skew is not None else self.skew
        self.files.update(other.files)
        self.roots.update(other.roots)
        self.notes.update(other.notes)
        self.listed_at = other.listed_at


# --------------------------------------------------------------------------
#  File Browser
# --------------------------------------------------------------------------


def fb_url(host: str, api: str, path: str) -> str:
    return (blueos._base(host, blueos.FILE_BROWSER_PORT) + api
            + urllib.parse.quote(path))


def parse_time(stamp: str) -> float | None:
    """File Browser's ISO timestamp as epoch seconds. Never raises.

    It writes nanoseconds, which `fromisoformat` will not take, and "Z" on
    some releases and an offset on others.
    """
    if not stamp:
        return None
    s = str(stamp).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def list_dir(host: str, token: str, path: str) -> tuple[list[dict] | None, str]:
    """The entries of one folder, or (None, why)."""
    a = blueos._get(fb_url(host, "/api/resources", path),
                    headers={"X-Auth": token}, limit=64_000_000, timeout=20)
    if not a.ok:
        return None, a.error or f"HTTP {a.status}"
    try:
        items = json.loads(a.body).get("items") or []
    except Exception:
        return None, "the folder listing did not parse"
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        out.append({
            "name": name,
            "path": str(i.get("path") or f"{path.rstrip('/')}/{name}"),
            "is_dir": bool(i.get("isDir")),
            "size": int(i.get("size") or 0),
            "modified": parse_time(i.get("modified", "")),
        })
    return out, ""


def walk(host: str, token: str, root: str, *, depth: int = 8,
         cancel=None) -> tuple[list[dict], str]:
    """Every file under `root`, each with `rel` set. (files, problem)."""
    files: list[dict] = []
    top, why = list_dir(host, token, root)
    if top is None:
        return [], why
    stack = [(top, "", 0)]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        items, prefix, level = stack.pop()
        for item in items:
            rel = f"{prefix}{item['name']}"
            if item["is_dir"]:
                if level < depth:
                    sub, _why = list_dir(host, token, item["path"])
                    if sub:
                        stack.append((sub, rel + "/", level + 1))
            else:
                files.append({**item, "rel": rel})
    return files, ""


def read_head(host: str, token: str, path: str, first: int, last: int) -> bytes:
    """Bytes `first`..`last` of a file, by range request. b"" on failure."""
    a = blueos._get(fb_url(host, "/api/raw", path)
                    + f"?auth={urllib.parse.quote(token)}",
                    headers={"Range": f"bytes={first}-{last}"},
                    limit=last - first + 1, binary=True, timeout=20)
    return a.raw if a.ok else b""


# --------------------------------------------------------------------------
#  when each file was recorded
# --------------------------------------------------------------------------

_DF_HEAD = b"\xa3\x95"
_DF_FMT = 0x80
_DF_FMT_LEN = 89


def dataflash_formats(buf: bytes) -> dict[int, tuple[int, str, str]]:
    """The FMT records in a dataflash log's head: type -> (length, format, columns)."""
    out: dict[int, tuple[int, str, str]] = {}
    i = buf.find(_DF_HEAD + bytes([_DF_FMT]))
    while i >= 0 and i + _DF_FMT_LEN <= len(buf):
        typ, length, _name, fmt, cols = struct.unpack_from("<BB4s16s64s", buf, i + 3)
        if length >= 3:
            out[typ] = (length,
                        fmt.rstrip(b"\x00").decode("ascii", "replace"),
                        cols.rstrip(b"\x00").decode("ascii", "replace"))
        i = buf.find(_DF_HEAD + bytes([_DF_FMT]), i + 1)
    return out


def dataflash_times(buf: bytes, fmts: dict[int, tuple[int, str, str]]) -> list[int]:
    """Every TimeUS in `buf` that sits in a properly chained record.

    A record is only believed when the next one starts exactly where its
    length says it ends, which is what keeps a stray 0xA3 0x95 inside a
    payload from being read as a time.
    """
    out: list[int] = []
    n = len(buf)
    i = buf.find(_DF_HEAD)
    while 0 <= i and i + 3 <= n:
        typ = buf[i + 2]
        spec = fmts.get(typ)
        if spec is not None:
            length, fmt, cols = spec
            end = i + length
            chained = end == n or (end + 2 <= n and buf[end:end + 2] == _DF_HEAD)
            if end <= n and chained:
                if fmt[:1] == "Q" and cols.split(",")[0] == "TimeUS" and i + 11 <= n:
                    (us,) = struct.unpack_from("<Q", buf, i + 3)
                    if 0 < us < 10 ** 13:
                        out.append(us)
                i = buf.find(_DF_HEAD, end)
                continue
        i = buf.find(_DF_HEAD, i + 1)
    return out


def bin_span(host: str, token: str, f: PiFile, *, head_kib: int = 256,
             tail_kib: int = 64) -> tuple[float | None, float | None]:
    """When an autopilot log ran, from its head, its tail and its file time.

    A dataflash log has no wall clock of its own on a vehicle without GPS --
    only microseconds since the autopilot booted. Its first and last
    timestamps give how long it ran; its modification time is when it last
    grew, which is when it ended. The two together place it.
    """
    if f.modified is None or f.size <= 0:
        return None, None
    head = read_head(host, token, f.path, 0, min(f.size, head_kib * 1024) - 1)
    fmts = dataflash_formats(head)
    if not fmts:
        return None, None
    first = dataflash_times(head, fmts)
    if f.size > head_kib * 1024:
        lo = max(0, f.size - tail_kib * 1024)
        tail = read_head(host, token, f.path, lo, f.size - 1)
        # The tail starts mid-record; begin at the first header in it.
        k = tail.find(_DF_HEAD)
        last = dataflash_times(tail[k:] if k >= 0 else b"", fmts)
    else:
        last = first
    if not first or not last:
        return None, None
    duration = (max(last) - min(first)) / 1e6
    if not 0 <= duration < 86400:
        return None, None
    return f.modified - duration, f.modified


def tlog_span(host: str, token: str, f: PiFile) -> tuple[float | None, float | None]:
    """A telemetry log opens with its first packet's wall time, big-endian µs."""
    head = read_head(host, token, f.path, 0, 7)
    if len(head) < 8:
        return None, f.modified
    (us,) = struct.unpack(">Q", head)
    start = us / 1e6
    if not 1.4e9 < start < 4.1e9:
        return None, f.modified
    return start, f.modified or start


def mcap_span(host: str, token: str, f: PiFile) -> tuple[float | None, float | None]:
    """The first chunk's start, and an end estimated from the size.

    The end is in the summary at the end of the file, and a truncated
    recording has none, so it is estimated at the rate these dives write --
    the same estimate the vehicle listing has always used.
    """
    head = read_head(host, token, f.path, 0, 96 * 1024 - 1)
    if not head.startswith(blueos.MCAP_MAGIC):
        return None, None
    start = blueos._first_chunk_start(head)
    if start is None:
        return None, None
    return start, start + f.size / blueos.BYTES_PER_SECOND


_STAMP = re.compile(r"(\d{8})_(\d{6})")


def stamp_time(text: str) -> float | None:
    """`recorder_20260911_202519` -> epoch seconds. The stamp is UTC."""
    from datetime import timezone
    m = _STAMP.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"
                                 ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def fill_spans(inv: Inventory, *, progress: ProgressCB | None = None,
               cancel=None) -> None:
    """Work out when each listed file was recorded."""
    todo = [f for f in inv.all_files() if f.category in ("mcap", "bin", "tlog")]
    for n, f in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            return
        if progress:
            progress(n / max(1, len(todo)), f"reading when {f.name} was recorded")
        try:
            if f.category == "mcap":
                f.start, f.end = mcap_span(inv.host, inv.token, f)
            elif f.category == "bin":
                f.start, f.end = bin_span(inv.host, inv.token, f)
            elif f.category == "tlog":
                f.start, f.end = tlog_span(inv.host, inv.token, f)
        except Exception:
            f.start = f.end = None

    by_stem = {PurePosixPath(f.rel).stem: f for f in inv.files.get("mcap", [])}
    for f in inv.files.get("video", []):
        # Extracted video sits in a folder named for its recording.
        owner = PurePosixPath(f.rel).parts[0] if "/" in f.rel else PurePosixPath(f.rel).stem
        rec = by_stem.get(owner)
        if rec is not None and rec.span_known:
            f.start, f.end = rec.start, rec.end
        else:
            f.start = stamp_time(owner)
            f.end = f.modified if (f.start and f.modified and f.modified >= f.start) else f.start
    for f in inv.files.get("c3", []):
        f.start = f.end = f.modified


def match(files: Iterable[PiFile], windows: Sequence[tuple[str, float, float]],
          margin_s: float = MARGIN_S) -> None:
    """Label each file with the transects its span overlaps."""
    for f in files:
        f.covers = []
        if not f.span_known:
            continue
        for name, lo, hi in windows:
            if f.start <= hi + margin_s and f.end >= lo - margin_s:
                f.covers.append(name)


# --------------------------------------------------------------------------
#  listing
# --------------------------------------------------------------------------


def find_c3_folders(host: str, token: str,
                    roots: Sequence[str] = C3_SEARCH_ROOTS,
                    depth: int = C3_SEARCH_DEPTH, cancel=None) -> list[str]:
    """Folders that hold Madrona's left/, right/ and center/ image folders."""
    found: list[str] = []
    for root in roots:
        stack = [(root, 0)]
        while stack:
            if cancel is not None and cancel.is_set():
                return found
            path, level = stack.pop()
            items, _why = list_dir(host, token, path)
            if not items:
                continue
            dirs = [i for i in items if i["is_dir"]]
            names = {i["name"].lower() for i in dirs}
            if sum(eye in names for eye in C3_EYES) >= 2:
                found.append(path)
                continue
            if level < depth:
                # Not into the places that are known to be huge and not it.
                stack.extend((d["path"], level + 1) for d in dirs
                             if d["name"] not in ("recorder", "ardupilot_logs",
                                                  "node_modules", ".git"))
    return found


def c3_root(folders: Sequence[str]) -> str:
    """One folder of C3 sets -> that folder; several -> what they share."""
    if not folders:
        return ""
    if len(folders) == 1:
        return folders[0]
    parts = [PurePosixPath(f).parts for f in folders]
    common = []
    for level in zip(*parts, strict=False):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    return str(PurePosixPath(*common)) if common else "/"


def search(host: str | None, kinds: Iterable[str], *, c3_folder: str = "",
           progress: ProgressCB | None = None, cancel=None,
           spans: bool = True) -> Inventory:
    """List the chosen types on the vehicle, and when each was recorded."""
    kinds = [k for k in (c.key for c in CATEGORIES) if k in set(kinds)]
    found = host or blueos.find_host()
    if found is None:
        raise RuntimeError("No vehicle answered. Check the tether, and that this "
                           "laptop has an address on the vehicle's network.")
    token = blueos.file_token(found)
    if not token:
        raise RuntimeError(f"{found} answered, but its File Browser would not open "
                           f"a session, so its files cannot be listed.")
    inv = Inventory(host=found, token=token, listed_at=time.time())
    inv.vehicle = blueos.vehicle_name(found)
    inv.skew = blueos.clock_skew(found)

    for n, kind in enumerate(kinds):
        if cancel is not None and cancel.is_set():
            break
        label = BY_KEY[kind].label
        if progress:
            progress(n / max(1, len(kinds)) * 0.5, f"listing {label} on {found}")
        notes: list[str] = []
        files: list[PiFile] = []
        roots: list[str] = []
        if kind in ("mcap", "video"):
            roots = [blueos.RECORDER_FB_PATH]
            raw, why = walk(found, token, roots[0], depth=3, cancel=cancel)
            if why:
                notes.append(f"{roots[0]}: {why}")
            for r in raw:
                name = r["name"].lower()
                if kind == "mcap" and "/" not in r["rel"] and name.endswith(".mcap"):
                    files.append(_pifile(kind, r))
                elif kind == "video" and "/" in r["rel"] and name.endswith(VIDEO_EXTS):
                    files.append(_pifile(kind, r))
        elif kind == "bin":
            roots = [blueos.DATAFLASH_FB_PATH]
            raw, why = walk(found, token, roots[0], depth=0, cancel=cancel)
            if why:
                notes.append(f"{roots[0]}: {why}")
            files = [_pifile(kind, r) for r in raw if r["name"].upper().endswith(".BIN")]
        elif kind == "tlog":
            roots = list(TLOG_FB_PATHS)
            for root in roots:
                raw, why = walk(found, token, root, depth=3, cancel=cancel)
                if why:
                    notes.append(f"{root}: not on this vehicle ({why}) -- tlogs are "
                                 f"only written by older BlueOS releases")
                files += [_pifile(kind, r) for r in raw
                          if r["name"].lower().endswith(".tlog")]
        elif kind == "c3":
            root = c3_folder
            if not root:
                if progress:
                    progress(n / max(1, len(kinds)) * 0.5,
                             "looking for Madrona's C3 folder (left/right/center)")
                root = c3_root(find_c3_folders(found, token, cancel=cancel))
                if root:
                    notes.append(f"found C3 imagery under {root}")
            if not root:
                notes.append("no folder with left/, right/ and center/ was found. "
                             "Set the C3 folder (the one chosen in Madrona) and "
                             "search again.")
            else:
                roots = [root]
                raw, why = walk(found, token, root, depth=6, cancel=cancel)
                if why:
                    notes.append(f"{root}: {why}")
                files = [_pifile(kind, r) for r in raw]
        files.sort(key=lambda f: f.rel)
        inv.files[kind] = files
        inv.roots[kind] = roots
        inv.notes[kind] = notes

    if spans:
        fill_spans(inv, cancel=cancel,
                   progress=(lambda f, m="": progress(0.5 + f * 0.5, m))
                   if progress else None)
    if progress:
        progress(1.0, "listed")
    return inv


def _pifile(kind: str, raw: dict) -> PiFile:
    return PiFile(category=kind, path=raw["path"], rel=raw["rel"],
                  size=raw["size"], modified=raw["modified"])


# --------------------------------------------------------------------------
#  choosing
# --------------------------------------------------------------------------

PERIOD_ALL = "all"
PERIOD_TRANSECTS = "transects"
PERIOD_MANUAL = "manual"


@dataclass
class Choice:
    """The files an action would touch, and why each is in it."""

    files: list[PiFile] = field(default_factory=list)
    by_rule: int = 0
    by_hand: int = 0
    not_listed: list[str] = field(default_factory=list)
    no_time: int = 0
    note: str = ""

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def breakdown(self) -> str:
        counts: dict[str, list[int]] = {}
        for f in self.files:
            c = counts.setdefault(f.category, [0, 0])
            c[0] += 1
            c[1] += f.size
        return ", ".join(f"{BY_KEY[k].label} {n}" for k, (n, _s)
                         in sorted(counts.items(),
                                   key=lambda kv: [c.key for c in CATEGORIES].index(kv[0])))


def choose(inv: Inventory | None, kinds: Iterable[str], period: str,
           picked: Iterable[PiFile] = ()) -> Choice:
    """File type (A) crossed with time period (B), plus what was clicked.

    * no period, or "manual selection" -> only the files clicked above
    * "all files"       -> every file of the chosen types, plus those clicked
    * "transects only"  -> files of the chosen types whose recorded span
                           overlaps a transect, plus those clicked

    Files whose recording time could not be worked out are never swept in by
    "transects only"; they are counted so the page can say so.
    """
    kinds = [k for k in (c.key for c in CATEGORIES) if k in set(kinds)]
    out = Choice()
    chosen: dict[str, PiFile] = {}
    hand = {f.path: f for f in picked}

    if period in (PERIOD_ALL, PERIOD_TRANSECTS) and kinds:
        for kind in kinds:
            if inv is None or kind not in inv.files:
                out.not_listed.append(kind)
                continue
            for f in inv.files[kind]:
                if period == PERIOD_TRANSECTS:
                    if not f.span_known:
                        out.no_time += 1
                        continue
                    if not f.covers:
                        continue
                chosen[f.path] = f
        out.by_rule = len(chosen)
    elif kinds and not period:
        out.note = "choose a time period to use the file types"
    elif period in (PERIOD_ALL, PERIOD_TRANSECTS) and not kinds:
        out.note = "choose at least one file type"

    for path, f in hand.items():
        if path not in chosen:
            chosen[path] = f
            out.by_hand += 1
    out.files = sorted(chosen.values(),
                       key=lambda f: ([c.key for c in CATEGORIES].index(f.category), f.rel))
    return out


# --------------------------------------------------------------------------
#  downloading
# --------------------------------------------------------------------------


@dataclass
class TransferReport:
    verb: str = "downloaded"
    done: list[PiFile] = field(default_factory=list)
    skipped: list[PiFile] = field(default_factory=list)
    failed: list[tuple[PiFile, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log_path: Path | None = None

    def summary(self) -> str:
        gib = sum(f.size for f in self.done) / 2 ** 30
        bits = [f"{len(self.done)} {self.verb} ({gib:,.2f} GiB)"]
        if self.skipped:
            bits.append(f"{len(self.skipped)} "
                        + ("already in the flight folder" if self.verb == "downloaded"
                           else "left alone"))
        if self.failed:
            bits.append(f"{len(self.failed)} FAILED")
        out = [", ".join(bits)]
        out += [f"  FAILED {f.rel}: {why}" for f, why in self.failed[:20]]
        if self.log_path:
            out.append(f"  record: {self.log_path}")
        return "\n".join(out)


def download(files: Sequence[PiFile], flight_dir: Path, host: str, token: str, *,
             progress: ProgressCB | None = None, cancel=None,
             chunk: int = 1 << 20) -> TransferReport:
    """Copy files into the flight folder, each into its type's own folder.

    Written to ``.part`` and renamed once the size matches, so an interrupted
    copy never looks finished. The file keeps the vehicle's modification time,
    which is the one clock an autopilot log carries.
    """
    import os

    rep = TransferReport()
    total = sum(f.size for f in files) or 1
    done_bytes = 0
    for f in files:
        if cancel is not None and cancel.is_set():
            rep.warnings.append("stopped before every file was copied")
            break
        dest = f.dest_in(flight_dir)
        if f.downloaded_to(flight_dir):
            rep.skipped.append(f)
            done_bytes += f.size
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        got = 0
        try:
            req = urllib.request.Request(
                fb_url(host, "/api/raw", f.path) + f"?auth={urllib.parse.quote(token)}",
                method="GET")
            req.add_header("User-Agent", blueos._UA)
            with urllib.request.urlopen(req, timeout=60) as src, open(part, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("stopped")
                    buf = src.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    got += len(buf)
                    if progress:
                        progress(min(0.999, (done_bytes + got) / total),
                                 f"{f.rel}  {got / 2 ** 20:,.0f} of "
                                 f"{f.size / 2 ** 20:,.0f} MiB")
            if f.size and part.stat().st_size != f.size:
                raise OSError(f"{part.stat().st_size:,} bytes arrived, the vehicle "
                              f"reported {f.size:,}")
            if f.category == "mcap":
                with open(part, "rb") as fh:
                    if fh.read(len(blueos.MCAP_MAGIC)) != blueos.MCAP_MAGIC:
                        raise OSError("it does not begin like an mcap")
            part.replace(dest)
            if f.modified:
                os.utime(dest, (f.modified, f.modified))
            rep.done.append(f)
        except InterruptedError:
            part.unlink(missing_ok=True)
            rep.warnings.append(f"stopped part way through {f.rel}")
            break
        except Exception as ex:
            part.unlink(missing_ok=True)
            rep.failed.append((f, f"{type(ex).__name__}: {str(ex)[:120]}"))
            rep.errors.append(f"{f.rel}: {ex}")
        finally:
            done_bytes += f.size
    if progress:
        progress(1.0, rep.summary().splitlines()[0])
    return rep


# --------------------------------------------------------------------------
#  deleting -- the only writes to the vehicle anywhere in the program
# --------------------------------------------------------------------------


class Refused(RuntimeError):
    """The vehicle was not in a state where deleting is safe."""


def _delete_request(host: str, token: str, path: str, timeout: float = 20.0) -> str:
    """DELETE one File Browser path. "" on success, otherwise why not."""
    req = urllib.request.Request(fb_url(host, "/api/resources", path),
                                 method="DELETE")
    req.add_header("User-Agent", blueos._UA)
    req.add_header("X-Auth", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "" if 200 <= r.status < 300 else f"HTTP {r.status}"
    except urllib.error.HTTPError as ex:
        return f"HTTP {ex.code}"
    except Exception as ex:
        return f"{type(ex).__name__}: {str(ex)[:80]}"


def protected(files: Iterable[PiFile], skew: float | None,
              now: float | None = None) -> list[PiFile]:
    """Files modified so recently they may still be being written."""
    pi_now = (now if now is not None else time.time()) + (skew or 0.0)
    return [f for f in files if f.modified is not None
            and f.modified > pi_now - RECENT_S]


def delete(files: Sequence[PiFile], inv: Inventory, *,
           progress: ProgressCB | None = None, cancel=None,
           record_dir: Path | None = None) -> TransferReport:
    """Delete files from the vehicle, one by one, with the guards above."""
    rep = TransferReport(verb="deleted")
    armed, _ms = blueos.read_arm_state(inv.host)
    if armed:
        raise Refused("The ROV is armed. Nothing was deleted -- disarm it first.")

    busy = {f.path for f in protected(files, blueos.clock_skew(inv.host))}
    roots = {r.rstrip("/") for rs in inv.roots.values() for r in rs}
    parents: set[str] = set()
    todo = [f for f in files if f.path not in busy]
    rep.skipped = [f for f in files if f.path in busy]
    if rep.skipped:
        rep.warnings.append(f"{len(rep.skipped)} file(s) modified in the last "
                            f"{RECENT_S / 60:.0f} minutes were left alone -- they "
                            f"may still be being written")
    for n, f in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            rep.warnings.append("stopped before every file was deleted")
            break
        if progress:
            progress(n / max(1, len(todo)), f"deleting {f.rel}")
        why = _delete_request(inv.host, inv.token, f.path)
        if why:
            rep.failed.append((f, why))
            rep.errors.append(f"{f.rel}: {why}")
        else:
            rep.done.append(f)
            parent = str(PurePosixPath(f.path).parent)
            if parent.rstrip("/") not in roots:
                parents.add(parent)

    # Folders emptied by the deletes go too -- deepest first, never a type's
    # own root, and only once a fresh listing says nothing is left in them.
    for folder in sorted(parents, key=lambda p: p.count("/"), reverse=True):
        items, _why = list_dir(inv.host, inv.token, folder)
        if items == []:
            _delete_request(inv.host, inv.token, folder)

    rep.log_path = write_record(rep, inv, record_dir)
    if progress:
        progress(1.0, rep.summary().splitlines()[0])
    return rep


def write_record(rep: TransferReport, inv: Inventory,
                 folder: Path | None) -> Path | None:
    """Keep a plain-text record of what was deleted, and from which vehicle."""
    import os
    if folder is None:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        folder = base / "CCR_ROV" / "rov_flight_ops" / "pi_cleanup"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    lines = [
        f"Files deleted from {inv.vehicle or 'the vehicle'} at {inv.host}",
        f"when      : {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"result    : {rep.summary().splitlines()[0]}",
        "",
    ]
    for f in rep.done:
        lines.append(f"deleted   {f.size:>14,}  {f.path}")
    for f in rep.skipped:
        lines.append(f"kept      {f.size:>14,}  {f.path}   (modified too recently)")
    for f, why in rep.failed:
        lines.append(f"FAILED    {f.size:>14,}  {f.path}   ({why})")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"pi_cleanup_{stamp}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except OSError:
        return None
