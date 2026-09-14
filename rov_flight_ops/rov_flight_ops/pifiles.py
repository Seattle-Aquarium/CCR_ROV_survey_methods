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

What guards the delete. Every one of these was tightened after an independent
review (13 September 2026) showed the first version trusted things it had not
established:

* **Only a confirmed, current "disarmed".** The vehicle's heartbeat is read
  until mavlink2rest's counter advances, so a stale cached heartbeat cannot
  pass as current. Armed, unanswered, or unchanging is a refusal -- and the
  check is repeated during a long batch, which stops if the answer changes.
* **Fresh metadata, not the search's.** Each target's folder is listed again
  immediately before deleting. A file that has changed size or time since it
  was listed, has no modification time, or was modified in the last couple of
  minutes by the vehicle's clock is left alone. An unreadable vehicle clock
  refuses the whole batch.
* **Only what belongs to its type.** A target must sit inside the exact folder
  its type was listed from; C3 imagery only inside the left/, right/ and
  center/ folders (and the calibration file beside them) Madrona wrote.
* **A record is written before anything is deleted**, one line per file as it
  happens, synced to disk. If the record cannot be written, nothing is deleted.

The file types, and where BlueOS keeps them (as its File Browser addresses
them):

==============  ==========================================  =================
type            on the vehicle                              lands in
==============  ==========================================  =================
mcap            recorder/*.mcap                              logs/mcap
mcap video      recorder/<recording>/*.mp4                   logs/mcap_video
BIN             ardupilot_logs/firmware/logs/*.BIN           logs/BIN
tlog            ardupilot_logs/logs/**/*.tlog (older BlueOS) logs/tlog
C3 imagery      the folder(s) Madrona saves into             photos/C3
==============  ==========================================  =================
"""

from __future__ import annotations

import hashlib
import json
import os
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

#: During a long deletion batch, the disarmed state is confirmed again this
#: often. Arming part way through stops the batch.
RECHECK_EVERY_S = 10.0

#: Two modification times this close are the same time. File Browser and the
#: listing round-trip can lose sub-second precision.
SAME_TIME_S = 1.0

TLOG_FB_PATHS = ("/ardupilot_logs/logs",)

#: Where Madrona's folder is looked for when none has been set. Its docs say
#: images go "underneath the folder they select", so there is no fixed path;
#: a folder holding left/, right/ and center/ is what gives it away.
C3_SEARCH_ROOTS = ("/system_root/usr/blueos/userdata",
                   "/system_root/usr/blueos/extensions",
                   "/system_root/root")
C3_EYES = ("left", "right", "center")
C3_SEARCH_DEPTH = 4

#: Folders far too broad to be a C3 folder. Typing one of these is refused
#: rather than walked: everything under it would be classed as imagery.
BROAD_FOLDERS = frozenset({
    "/", "/system_root", "/system_root/usr", "/system_root/usr/blueos",
    "/system_root/usr/blueos/userdata", "/system_root/usr/blueos/extensions",
    "/system_root/root", "/system_root/etc", "/ardupilot_logs",
    "/ardupilot_logs/firmware", blueos.RECORDER_FB_PATH,
})

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".h264", ".h265", ".ts")

#: A line per downloaded file, in the flight folder: where it came from, its
#: size and time on the vehicle, and the SHA-256 of the bytes that arrived.
MANIFEST = "logs/pi_downloads.jsonl"


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
_ORDER = [c.key for c in CATEGORIES]


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
    #: True when `end` is an estimate rather than read from the file -- an
    #: mcap with no summary, whose end is worked out from its size.
    end_estimated: bool = False
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
        """A copy of the same size is in the flight folder -- verified or not.

        Use `copy_state` where the difference matters: this answers "is there
        something there", not "is it known to be intact".
        """
        return copy_state(self, flight_dir) in ("verified", "same size")


@dataclass
class Inventory:
    """What a search found, by type."""

    host: str = ""
    token: str = ""
    vehicle: str = ""
    files: dict[str, list[PiFile]] = field(default_factory=dict)
    #: The exact folders each type was listed from. Deleting checks every
    #: target against these, so nothing outside them can be touched.
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
    """Every file under `root`, each with `rel` set. (files, problem).

    A subfolder that cannot be listed is reported in the problem rather than
    dropped silently: an inventory that looks complete and is not is how the
    wrong selection gets made.
    """
    files: list[dict] = []
    top, why = list_dir(host, token, root)
    if top is None:
        return [], why
    missed: list[str] = []
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
                    if sub is None:
                        missed.append(rel)
                    elif sub:
                        stack.append((sub, rel + "/", level + 1))
            else:
                files.append({**item, "rel": rel})
    problem = (f"{len(missed)} subfolder(s) could not be listed: "
               + ", ".join(missed[:4])) if missed else ""
    return files, problem


def read_head(host: str, token: str, path: str, first: int, last: int) -> bytes:
    """Bytes `first`..`last` of a file, by range request. b"" on failure."""
    if last < first:
        return b""
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


_OP_FOOTER = 0x02
_OP_STATISTICS = 0x0B
_OP_SUMMARY_OFFSET = 0x0E
#: opcode(1) + length(8) + summary_start(8) + summary_offset_start(8) + crc(4),
#: then the closing magic.
_FOOTER_LEN = 1 + 8 + 8 + 8 + 4 + 8


def _records(buf: bytes):
    """(opcode, content) for every whole mcap record in `buf`."""
    pos = 0
    while pos + 9 <= len(buf):
        op = buf[pos]
        (length,) = struct.unpack_from("<Q", buf, pos + 1)
        body = pos + 9
        if length > len(buf) - body:
            return
        yield op, buf[body:body + length]
        pos = body + length


def _statistics_times(content: bytes) -> tuple[int, int] | None:
    """message_start_time and message_end_time from a Statistics record."""
    # message_count u64, schema_count u16, channel_count u32,
    # attachment_count u32, metadata_count u32, chunk_count u32 = 26 bytes.
    if len(content) < 42:
        return None
    return struct.unpack_from("<QQ", content, 26)


def mcap_summary_times(host: str, token: str, f: PiFile) -> tuple[float | None, float | None]:
    """The recorded span from the mcap's own summary, when it has one.

    The footer at the end of the file points at the summary; the summary
    offsets point at the Statistics record, which carries the first and last
    message times. Three small range reads, a few hundred bytes. A recording
    the vehicle never closed has no footer, and gets (None, None).
    """
    if f.size < _FOOTER_LEN + 8:
        return None, None
    tail = read_head(host, token, f.path, f.size - _FOOTER_LEN, f.size - 1)
    if len(tail) != _FOOTER_LEN or tail[-8:] != blueos.MCAP_MAGIC \
            or tail[0] != _OP_FOOTER:
        return None, None
    summary_start, offset_start = struct.unpack_from("<QQ", tail, 9)
    footer_at = f.size - _FOOTER_LEN
    stats = None
    if offset_start and summary_start <= offset_start < footer_at:
        offsets = read_head(host, token, f.path, offset_start,
                            min(footer_at, offset_start + (1 << 20)) - 1)
        for op, content in _records(offsets):
            if op == _OP_SUMMARY_OFFSET and len(content) >= 17 \
                    and content[0] == _OP_STATISTICS:
                g_start, g_len = struct.unpack_from("<QQ", content, 1)
                group = read_head(host, token, f.path, g_start,
                                  min(footer_at, g_start + g_len) - 1)
                for op2, c2 in _records(group):
                    if op2 == _OP_STATISTICS:
                        stats = _statistics_times(c2)
                break
    if stats is None and summary_start and summary_start < footer_at:
        summary = read_head(host, token, f.path, summary_start,
                            min(footer_at, summary_start + (4 << 20)) - 1)
        for op, content in _records(summary):
            if op == _OP_STATISTICS:
                stats = _statistics_times(content)
                break
    if not stats or not stats[1]:
        return None, None
    start_ns, end_ns = stats
    return (start_ns / 1e9 if start_ns else None), end_ns / 1e9


def mcap_span(host: str, token: str, f: PiFile) -> tuple[float | None, float | None, bool]:
    """(start, end, end_is_estimated) for a recording on the vehicle.

    The end comes from the mcap's summary. Only a recording without one -- a
    file the vehicle never closed -- has its end estimated from its size at
    the rate these dives write, and that estimate is flagged, because the
    true rate varies with the camera and a wrong end can drop a transect.
    """
    head = read_head(host, token, f.path, 0, 96 * 1024 - 1)
    if not head.startswith(blueos.MCAP_MAGIC):
        return None, None, False
    start = blueos._first_chunk_start(head)
    s_start, s_end = mcap_summary_times(host, token, f)
    if s_end is not None:
        return (s_start or start), s_end, False
    if start is None:
        return None, None, False
    return start, start + f.size / blueos.BYTES_PER_SECOND, True


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
                f.start, f.end, f.end_estimated = mcap_span(inv.host, inv.token, f)
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
            f.start, f.end, f.end_estimated = rec.start, rec.end, rec.end_estimated
        else:
            f.start = stamp_time(owner)
            f.end = f.modified if (f.start and f.modified and f.modified >= f.start) else f.start
            f.end_estimated = True
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
#  C3 imagery: exactly the folders Madrona wrote, nothing beside them
# --------------------------------------------------------------------------


def _eye_dirs(items: list[dict] | None) -> set[str]:
    return {i["name"].lower() for i in (items or [])
            if i["is_dir"] and i["name"].lower() in C3_EYES}


def c3_keep(root: str, rel: str) -> bool:
    """Is this file, under a C3 root, part of the imagery set?

    Inside a left/, right/ or center/ folder, yes; a file directly beside
    them -- the calibration file -- yes; anything in some other subfolder
    is not Madrona's and is not swept in. A root that *is* an eye folder
    keeps everything in it.
    """
    if PurePosixPath(root).name.lower() in C3_EYES:
        return True
    head, _, rest = rel.partition("/")
    return not rest or head.lower() in C3_EYES


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
            if len(_eye_dirs(items)) >= 2:
                found.append(path)
                continue
            if level < depth:
                # Not into the places that are known to be huge and not it.
                stack.extend((d["path"], level + 1) for d in items if d["is_dir"]
                             and d["name"] not in ("recorder", "ardupilot_logs",
                                                   "node_modules", ".git"))
    return sorted(set(found))


def resolve_c3_folders(host: str, token: str, typed: str = "",
                       cancel=None) -> tuple[list[str], list[str]]:
    """(the exact C3 folders to list, notes) -- from the typed path or a search.

    A typed folder is accepted when it is a left/right/center folder itself,
    holds at least two of them, or holds sessions that do. Anything broader is
    refused rather than walked, because every file under it would be treated
    as imagery.
    """
    notes: list[str] = []
    typed = (typed or "").strip()
    if not typed:
        found = find_c3_folders(host, token, cancel=cancel)
        if found:
            notes.append("found C3 imagery in " + ", ".join(found))
        else:
            notes.append("no folder with left/, right/ and center/ was found. "
                         "Set the C3 folder (the one chosen in Madrona) and "
                         "search again.")
        return found, notes

    folder = "/" + typed.strip("/") if typed.strip("/") else "/"
    if folder in BROAD_FOLDERS:
        notes.append(f"{folder} is far too broad to be the C3 folder -- choose "
                     f"the folder Madrona saves into")
        return [], notes
    items, why = list_dir(host, token, folder)
    if items is None:
        notes.append(f"{folder}: {why}")
        return [], notes
    if PurePosixPath(folder).name.lower() in C3_EYES or len(_eye_dirs(items)) >= 2:
        return [folder], notes
    sessions = []
    for d in items:
        if d["is_dir"]:
            sub, _why = list_dir(host, token, d["path"])
            if len(_eye_dirs(sub)) >= 2:
                sessions.append(d["path"])
    if sessions:
        return sorted(sessions), notes
    notes.append(f"{folder} holds no left/, right/ or center/ folders, so "
                 f"nothing in it was treated as C3 imagery")
    return [], notes


def _common_parent(folders: Sequence[str]) -> str:
    parts = [PurePosixPath(f).parts for f in folders]
    common = []
    for level in zip(*parts, strict=False):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    return str(PurePosixPath(*common)) if common else "/"


# --------------------------------------------------------------------------
#  listing
# --------------------------------------------------------------------------


def search(host: str | None, kinds: Iterable[str], *, c3_folder: str = "",
           progress: ProgressCB | None = None, cancel=None,
           spans: bool = True) -> Inventory:
    """List the chosen types on the vehicle, and when each was recorded."""
    kinds = [k for k in _ORDER if k in set(kinds)]
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
            for root in TLOG_FB_PATHS:
                raw, why = walk(found, token, root, depth=3, cancel=cancel)
                if why and not raw:
                    notes.append(f"{root}: not on this vehicle ({why}) -- tlogs are "
                                 f"only written by older BlueOS releases")
                    continue
                roots.append(root)
                if why:
                    notes.append(f"{root}: {why}")
                files += [_pifile(kind, r) for r in raw
                          if r["name"].lower().endswith(".tlog")]
        elif kind == "c3":
            if progress and not c3_folder:
                progress(n / max(1, len(kinds)) * 0.5,
                         "looking for Madrona's C3 folder (left/right/center)")
            roots, notes = resolve_c3_folders(found, token, c3_folder, cancel=cancel)
            # One set lands straight in photos/C3; several keep what tells
            # them apart, so two sessions never write into the same folder.
            parent = _common_parent(roots) if len(roots) > 1 else ""
            for root in roots:
                raw, why = walk(found, token, root, depth=6, cancel=cancel)
                if why:
                    notes.append(f"{root}: {why}")
                prefix = (root[len(parent):].strip("/") + "/") if parent else ""
                for r in raw:
                    if c3_keep(root, r["rel"]):
                        files.append(PiFile(category=kind, path=r["path"],
                                            rel=prefix + r["rel"], size=r["size"],
                                            modified=r["modified"]))
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


def contained(f: PiFile, inv: Inventory) -> bool:
    """Does this file sit where its type was listed from, and only there?"""
    for root in inv.roots.get(f.category, []):
        base = root.rstrip("/") + "/"
        if not f.path.startswith(base):
            continue
        rel = f.path[len(base):]
        if not rel or rel.startswith("/") or "/../" in f"/{rel}/":
            continue
        if f.category in ("mcap", "bin"):
            return "/" not in rel
        if f.category == "video":
            return "/" in rel
        if f.category == "c3":
            return c3_keep(root, rel)
        return True
    return False


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
    #: Left out of a destructive "transects only" because their end time is
    #: an estimate.
    estimated: int = 0
    note: str = ""

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def breakdown(self) -> str:
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.category] = counts.get(f.category, 0) + 1
        return ", ".join(f"{BY_KEY[k].label} {n}" for k, n
                         in sorted(counts.items(), key=lambda kv: _ORDER.index(kv[0])))


def choose(inv: Inventory | None, kinds: Iterable[str], period: str,
           picked: Iterable[PiFile] = (), *, destructive: bool = False) -> Choice:
    """File type (A) crossed with time period (B), plus what was clicked.

    * no period, or "manual selection" -> only the files clicked above
    * "all files"       -> every file of the chosen types, plus those clicked
    * "transects only"  -> files of the chosen types whose recorded span
                           overlaps a transect, plus those clicked

    Files whose recording time could not be worked out are never swept in by
    "transects only". For a destructive action, neither are files whose end
    time is only an estimate. Both are counted so the page can say so.
    """
    kinds = [k for k in _ORDER if k in set(kinds)]
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
                    if destructive and f.end_estimated:
                        out.estimated += 1
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
                       key=lambda f: (_ORDER.index(f.category), f.rel))
    return out


# --------------------------------------------------------------------------
#  copies in the flight folder
# --------------------------------------------------------------------------


def load_manifest(flight_dir: Path | None) -> dict[str, dict]:
    """The newest download record for each vehicle path. {} when there is none."""
    out: dict[str, dict] = {}
    if not flight_dir:
        return out
    try:
        with open(Path(flight_dir) / MANIFEST, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("pi_path"):
                    out[entry["pi_path"]] = entry
    except OSError:
        pass
    return out


def copy_state(f: PiFile, flight_dir: Path | None,
               manifest: dict[str, dict] | None = None) -> str:
    """What is in the flight folder for this file.

    * ``verified``  -- downloaded by this program: every byte arrived, its
      SHA-256 was recorded, and the file there is still that size
    * ``same size`` -- a file of the same size, with no such record (copied
      by hand, say). Probably the same; not known to be.
    * ``differs``   -- a file of a different size
    * ``""``        -- nothing there
    """
    if not flight_dir:
        return ""
    p = f.dest_in(flight_dir)
    try:
        if not p.is_file():
            return ""
        local = p.stat().st_size
    except OSError:
        return ""
    if local != f.size:
        return "differs"
    entry = (manifest if manifest is not None else load_manifest(flight_dir)).get(f.path)
    if (entry and entry.get("sha256") and entry.get("size") == f.size
            and _same_time(entry.get("modified"), f.modified)):
        return "verified"
    return "same size"


def _same_time(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= SAME_TIME_S


def _sync(fh) -> None:
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


# --------------------------------------------------------------------------
#  downloading
# --------------------------------------------------------------------------


@dataclass
class TransferReport:
    verb: str = "downloaded"
    done: list[PiFile] = field(default_factory=list)
    skipped: list[PiFile] = field(default_factory=list)
    failed: list[tuple[PiFile, str]] = field(default_factory=list)
    #: Why each skipped file was skipped, by vehicle path.
    reasons: dict[str, str] = field(default_factory=dict)
    #: Set when a deletion batch stopped part way, and why.
    stopped: str = ""
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
        if self.stopped:
            out.append(f"  STOPPED: {self.stopped}")
        why: dict[str, int] = {}
        for reason in self.reasons.values():
            why[reason] = why.get(reason, 0) + 1
        out += [f"  {n} {reason}" for reason, n in why.items()]
        out += [f"  FAILED {f.rel}: {reason}" for f, reason in self.failed[:20]]
        if self.log_path:
            out.append(f"  record: {self.log_path}")
        return "\n".join(out)


def download(files: Sequence[PiFile], flight_dir: Path, host: str, token: str, *,
             progress: ProgressCB | None = None, cancel=None,
             chunk: int = 1 << 20, vehicle: str = "") -> TransferReport:
    """Copy files into the flight folder, each into its type's own folder.

    Written to ``.part`` and renamed once the size matches, so an interrupted
    copy never looks finished. The file keeps the vehicle's modification time,
    which is the one clock an autopilot log carries. Every completed copy is
    recorded in the flight folder's download manifest with the SHA-256 of the
    bytes that arrived, which is what makes it "verified" afterwards.
    """
    rep = TransferReport()
    manifest = load_manifest(flight_dir)
    total = sum(f.size for f in files) or 1
    done_bytes = 0
    unverified_skips = 0
    for f in files:
        if cancel is not None and cancel.is_set():
            rep.warnings.append("stopped before every file was copied")
            break
        dest = f.dest_in(flight_dir)
        state = copy_state(f, flight_dir, manifest)
        if state in ("verified", "same size"):
            rep.skipped.append(f)
            rep.reasons[f.path] = ("already downloaded and verified" if state == "verified"
                                   else "already there, same size (not verified)")
            unverified_skips += state == "same size"
            done_bytes += f.size
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        got = 0
        digest = hashlib.sha256()
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
                    digest.update(buf)
                    got += len(buf)
                    if progress:
                        progress(min(0.999, (done_bytes + got) / total),
                                 f"{f.rel}  {got / 2 ** 20:,.0f} of "
                                 f"{f.size / 2 ** 20:,.0f} MiB")
                _sync(fh)
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
            entry = {"pi_path": f.path, "local": str(dest.relative_to(flight_dir)),
                     "category": f.category, "size": f.size, "modified": f.modified,
                     "sha256": digest.hexdigest(), "host": host, "vehicle": vehicle,
                     "downloaded": datetime.now().astimezone().isoformat(
                         timespec="seconds")}
            try:
                mpath = Path(flight_dir) / MANIFEST
                mpath.parent.mkdir(parents=True, exist_ok=True)
                with open(mpath, "a", encoding="utf-8") as mh:
                    mh.write(json.dumps(entry) + "\n")
                    _sync(mh)
                manifest[f.path] = entry
            except OSError as ex:
                rep.warnings.append(f"{f.rel} copied, but its record could not be "
                                    f"written to {MANIFEST} ({ex}) -- it will show as "
                                    f"unverified")
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
    if unverified_skips:
        rep.warnings.append(
            f"{unverified_skips} file(s) were skipped because a file of the same "
            f"size was already in the flight folder. They were not downloaded by "
            f"this program, so they are not verified copies.")
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
    """Files that must not be deleted on what is known of them now.

    Modified in the last couple of minutes by the vehicle's clock, or with no
    modification time at all -- or every file, when the vehicle's clock is
    unknown and "recent" cannot be judged.
    """
    files = list(files)
    if skew is None:
        return files
    pi_now = (now if now is not None else time.time()) + skew
    return [f for f in files if f.modified is None
            or f.modified > pi_now - RECENT_S]


class Journal:
    """The deletion record, written ahead of the deleting and synced per line.

    Opened -- and the list of approved targets written -- before the first
    request is sent. A batch that dies part way therefore still leaves a
    record of what was approved and of every outcome up to that point.
    """

    def __init__(self, path: Path, fh):
        self.path, self._fh = path, fh

    @classmethod
    def open(cls, folder: Path | None, inv: Inventory,
             targets: Sequence[PiFile]) -> Journal:
        if folder is None:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            folder = base / "CCR_ROV" / "rov_flight_ops" / "pi_cleanup"
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        try:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"pi_cleanup_{stamp}.txt"
            n = 1
            while path.exists():
                n += 1
                path = folder / f"pi_cleanup_{stamp}-{n}.txt"
            fh = open(path, "x", encoding="utf-8")
        except OSError as ex:
            raise Refused(f"The deletion record could not be created ({ex}), so "
                          f"nothing was deleted.") from ex
        journal = cls(path, fh)
        journal.write(
            f"Deleting files from {inv.vehicle or 'the vehicle'} at {inv.host}",
            f"opened    : {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"approved  : {len(targets)} file(s), "
            f"{sum(f.size for f in targets):,} bytes", "")
        journal.write(*(f"target    {f.size:>14,}  {f.path}" for f in targets), "")
        return journal

    def write(self, *lines: str) -> None:
        """Append and sync. Raises OSError, which stops the batch."""
        self._fh.write("\n".join(lines) + "\n")
        _sync(self._fh)

    def close(self, rep: TransferReport) -> None:
        try:
            self.write("", f"result    : {rep.summary().splitlines()[0]}",
                       *([f"stopped   : {rep.stopped}"] if rep.stopped else []),
                       f"closed    : "
                       f"{datetime.now().astimezone().isoformat(timespec='seconds')}")
        except OSError:
            rep.errors.append(f"the deletion record {self.path} could not be finished")
        finally:
            try:
                self._fh.close()
            except OSError:
                pass


def delete(files: Sequence[PiFile], inv: Inventory, *,
           progress: ProgressCB | None = None, cancel=None,
           record_dir: Path | None = None,
           confirm: Callable[[str], tuple[bool, str]] | None = None) -> TransferReport:
    """Delete files from the vehicle, one by one, with the guards above."""
    confirm = confirm or blueos.confirm_disarmed
    rep = TransferReport(verb="deleted")

    skew = blueos.clock_skew(inv.host)
    if skew is None:
        raise Refused("The vehicle's clock could not be read, so a file still "
                      "being written cannot be told from an old one. Nothing was "
                      "deleted.")
    ok, why = confirm(inv.host)
    if not ok:
        raise Refused(f"Nothing was deleted: {why}. Files are only deleted when a "
                      f"current heartbeat confirms the ROV is disarmed.")

    journal = Journal.open(record_dir, inv, files)
    try:
        _delete_batch(files, inv, rep, journal, skew, confirm, progress, cancel)
    except OSError as ex:
        rep.stopped = f"the deletion record could not be written ({ex})"
        rep.errors.append(rep.stopped)
    finally:
        journal.close(rep)
        rep.log_path = journal.path
    if progress:
        progress(1.0, rep.summary().splitlines()[0])
    return rep


def _delete_batch(files, inv, rep, journal, skew, confirm, progress, cancel) -> None:
    # Fresh metadata for every target, one listing per folder, taken now --
    # not the search's, which may be minutes old.
    by_parent: dict[str, list[PiFile]] = {}
    for f in files:
        by_parent.setdefault(str(PurePosixPath(f.path).parent), []).append(f)
    fresh: dict[str, dict] = {}
    unreadable: set[str] = set()
    for parent in by_parent:
        items, _why = list_dir(inv.host, inv.token, parent)
        if items is None:
            unreadable.add(parent)
            continue
        fresh.update({i["path"]: i for i in items})

    pi_now = time.time() + skew
    todo: list[PiFile] = []
    for f in files:
        entry = fresh.get(f.path)
        if not contained(f, inv):
            reason = "outside the folder its type was listed from"
        elif str(PurePosixPath(f.path).parent) in unreadable:
            reason = "its folder could not be listed again"
        elif entry is None:
            reason = "no longer on the vehicle"
        elif entry["is_dir"]:
            reason = "now a folder"
        elif f.modified is None or entry["modified"] is None:
            reason = "no modification time"
        elif entry["size"] != f.size or not _same_time(entry["modified"], f.modified):
            reason = "changed since it was listed"
        elif entry["modified"] > pi_now - RECENT_S:
            reason = "modified in the last two minutes"
        else:
            todo.append(f)
            continue
        rep.skipped.append(f)
        rep.reasons[f.path] = reason
        journal.write(f"kept      {f.size:>14,}  {f.path}   ({reason})")

    roots = {r.rstrip("/") for rs in inv.roots.values() for r in rs}
    parents: set[str] = set()
    last_check = time.monotonic()
    for n, f in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            rep.stopped = "stopped by the operator"
        elif time.monotonic() - last_check >= RECHECK_EVERY_S:
            ok, why = confirm(inv.host)
            last_check = time.monotonic()
            if not ok:
                rep.stopped = why
        if rep.stopped:
            rest = todo[n - 1:]
            journal.write(*(f"not tried {g.size:>14,}  {g.path}" for g in rest))
            rep.warnings.append(f"{len(rest)} file(s) were not deleted: {rep.stopped}")
            break
        if progress:
            progress(n / max(1, len(todo)), f"deleting {f.rel}")
        why = _delete_request(inv.host, inv.token, f.path)
        if why:
            rep.failed.append((f, why))
            rep.errors.append(f"{f.rel}: {why}")
            journal.write(f"FAILED    {f.size:>14,}  {f.path}   ({why})")
        else:
            rep.done.append(f)
            journal.write(f"deleted   {f.size:>14,}  {f.path}")
            parent = str(PurePosixPath(f.path).parent)
            if parent.rstrip("/") not in roots:
                parents.add(parent)

    # Folders emptied by the deletes go too -- deepest first, never a type's
    # own root, and only once a fresh listing says nothing is left in them.
    if not rep.stopped:
        for folder in sorted(parents, key=lambda p: p.count("/"), reverse=True):
            items, _why = list_dir(inv.host, inv.token, folder)
            if items == [] and not _delete_request(inv.host, inv.token, folder):
                journal.write(f"removed empty folder  {folder}")
