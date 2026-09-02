"""
Diagnosing a folder of recordings, and writing repaired copies.

Two failures have now cost a day of telemetry each, and they look identical
from the outside -- a transect with no depth -- while needing opposite
responses:

* **The recorder never closed the file.** Power or the tether goes at the wrong
  moment and the last ~50 bytes (DATA_END, the summary, the footer) are never
  written. The data is all there; standard readers just refuse the file.
  ``mcap_extract`` already recovers this automatically.
* **The vehicle stopped talking.** On 2026-09-01 the MAVLink router died at
  09:40:59 while the recorder happily carried on for another 57 minutes of
  video. The file is structurally perfect and contains no telemetry for three
  of the four transects.

The first is a file problem and is repairable. The second is a dive problem and
is not -- those numbers do not exist anywhere. Telling them apart used to mean
asking someone to go read the mcap by hand, so that is what this module is for.

Nothing here writes to an original recording. ``repair_copy`` produces a new
file and leaves the source untouched.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .mcap_extract import (
    MCAP_MAGIC,
    VIDEO_SCHEMA,
    McapHealth,
    _synthetic_tail,
    open_repaired,
    scan_health,
)

ProgressCB = Callable[[float, str], None]

#: Topics are grouped by what losing them would cost you.
TELEMETRY = "telemetry"      # mavlink/* -- depth, altitude, mode, power
VIDEO = "video"              # the ROV camera
SERVICES = "services"        # BlueOS logs; useful for *why*, not for science

#: How much of a transect must carry telemetry before it counts as covered.
#: Well below 1.0 because a window's edges routinely fall between samples.
COVERED_FRACTION = 0.90


def topic_group(topic: str, schema_name: str = "") -> str:
    if VIDEO_SCHEMA in schema_name or topic.startswith("video/"):
        return VIDEO
    if topic.startswith("mavlink/"):
        return TELEMETRY
    return SERVICES


@dataclass
class Span:
    first: float | None = None
    last: float | None = None
    count: int = 0

    def add(self, t: float) -> None:
        if self.first is None or t < self.first:
            self.first = t
        if self.last is None or t > self.last:
            self.last = t
        self.count += 1

    @property
    def seconds(self) -> float:
        if self.first is None or self.last is None:
            return 0.0
        return self.last - self.first

    def covers(self, lo: float, hi: float) -> float:
        """Fraction of [lo, hi] this span overlaps."""
        if self.first is None or hi <= lo:
            return 0.0
        return max(0.0, min(hi, self.last) - max(lo, self.first)) / (hi - lo)


@dataclass
class RecordingReport:
    path: Path
    size: int = 0
    health: McapHealth | None = None
    error: str | None = None
    #: group -> Span, populated only by a deep scan
    groups: dict[str, Span] = field(default_factory=dict)
    topics: dict[str, Span] = field(default_factory=dict)
    deep: bool = False

    @property
    def truncated(self) -> bool:
        return bool(self.health and self.health.recoverable)

    @property
    def readable(self) -> bool:
        return self.error is None and bool(
            self.health and (self.health.complete or self.health.recoverable))

    @property
    def status(self) -> str:
        if self.error:
            return "unreadable"
        if self.truncated:
            return "truncated"
        if self.health and self.health.complete:
            return "ok"
        return "empty"

    @property
    def repairable(self) -> bool:
        """A repaired copy would gain something a plain copy would not."""
        return self.truncated

    def headline(self) -> str:
        if self.error:
            return f"cannot be read: {self.error}"
        if self.truncated:
            h = self.health
            return (f"never closed by the recorder -- the last "
                    f"{h.lost_bytes:,} bytes of {h.size / 1e9:.2f} GB were "
                    f"never written; UTC reads it anyway")
        return "complete"

    def telemetry_ended_early_by(self) -> float:
        """Seconds of recording that carried no telemetry at the end.

        The 2026-09-01 signature: video runs on long after MAVLink stops.
        """
        t, v = self.groups.get(TELEMETRY), self.groups.get(VIDEO)
        if not t or not v or t.last is None or v.last is None:
            return 0.0
        return max(0.0, v.last - t.last)


@dataclass
class WindowVerdict:
    """What a transect window can expect from the recordings behind it."""

    name: str
    start: float
    end: float
    telemetry: float = 0.0          # fraction covered
    video: float = 0.0
    recordings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.telemetry >= COVERED_FRACTION

    def explain(self) -> str:
        if self.ok:
            return "telemetry present"
        if not self.recordings:
            return "no recording covers this window"
        # A window that clips the last second of a dying link is not "1%
        # covered", it is a window whose telemetry was already gone.
        if self.telemetry < 0.01:
            if self.video > 0.0:
                return ("a recording covers this window but contains no "
                        "telemetry -- the vehicle stopped reporting")
            return "the recordings covering this window hold no telemetry"
        return f"only {self.telemetry * 100:.0f}% of this window has telemetry"


# --------------------------------------------------------------------------
#  scanning
# --------------------------------------------------------------------------


def quick_scan(paths: Sequence[Path]) -> list[RecordingReport]:
    """Structural health only. Reads nine bytes per record, so a folder of
    multi-gigabyte recordings answers in well under a second."""
    out = []
    for p in paths:
        p = Path(p)
        rep = RecordingReport(p)
        try:
            rep.size = p.stat().st_size
            rep.health = scan_health(p)
            if rep.health.good_end == 0:
                rep.error = "not an mcap file (bad magic)"
            elif not (rep.health.complete or rep.health.recoverable):
                rep.error = "no readable chunks"
        except Exception as ex:
            rep.error = f"{type(ex).__name__}: {ex}".split("\n")[0][:120]
        out.append(rep)
    return out


def deep_scan(
    rep: RecordingReport,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> RecordingReport:
    """Read the whole recording to find what each topic actually covers.

    This is the pass that distinguishes "the file broke" from "the vehicle
    stopped": it costs a full read (about 18 s per 5 GB), which is why it is
    separate from `quick_scan` rather than folded into it.
    """
    from mcap.reader import NonSeekingReader

    if not rep.readable or rep.health is None:
        return rep
    rep.topics, rep.groups = {}, {}
    n = 0
    size = max(1, rep.health.good_end)
    # Only a truncated file needs the synthetic tail. Wrapping a complete one
    # would hand the reader a second footer after the real one.
    opener = (open_repaired(rep.path, rep.health) if rep.truncated
              else open(rep.path, "rb"))
    try:
        with opener as f:
            for schema, ch, msg in NonSeekingReader(f).iter_messages():
                if cancel is not None and cancel.is_set():
                    from .ffmpeg_tools import CancelledError
                    raise CancelledError("cancelled")
                t = msg.log_time / 1e9
                rep.topics.setdefault(ch.topic, Span()).add(t)
                g = topic_group(ch.topic, schema.name if schema else "")
                rep.groups.setdefault(g, Span()).add(t)
                n += 1
                if progress and n % 50_000 == 0:
                    progress(min(0.99, f.tell() / size),
                             f"{rep.path.name}: {n:,} messages")
    except Exception as ex:
        from .ffmpeg_tools import CancelledError
        if isinstance(ex, CancelledError):
            raise
        rep.error = rep.error or (
            f"stopped after {n:,} messages: {type(ex).__name__}")
    rep.deep = True
    if progress:
        progress(1.0, f"{rep.path.name}: {n:,} messages")
    return rep


def judge_windows(
    reports: Sequence[RecordingReport],
    windows: Sequence[tuple[str, float, float]],
) -> list[WindowVerdict]:
    """Say, per transect, whether the telemetry behind it actually exists.

    Only deep-scanned reports can answer this; a structural scan knows the
    recording's span but not which topics live inside it.
    """
    out = []
    for name, lo, hi in windows:
        v = WindowVerdict(name, lo, hi)
        for rep in reports:
            if not rep.deep:
                continue
            tel, vid = rep.groups.get(TELEMETRY), rep.groups.get(VIDEO)
            covers_any = any(
                s and s.covers(lo, hi) > 0 for s in (tel, vid, rep.groups.get(SERVICES)))
            if covers_any:
                v.recordings.append(rep.path.name)
            if tel:
                v.telemetry = min(1.0, v.telemetry + tel.covers(lo, hi))
            if vid:
                v.video = min(1.0, v.video + vid.covers(lo, hi))
        out.append(v)
    return out


# --------------------------------------------------------------------------
#  repair
# --------------------------------------------------------------------------


def repaired_name(path: Path) -> str:
    return f"{path.stem}_repaired{path.suffix}"


def repair_copy(
    rep: RecordingReport,
    dest: Path | None = None,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> Path:
    """Write a valid mcap containing this recording's good bytes.

    The original stays exactly as it was -- it is opened read-only and never
    truncated -- because a recording that broke in an interesting way is
    evidence, and the crew may want to hand it to Blue Robotics.

    The copy is written to a temporary name and moved into place at the end, so
    an interrupted repair cannot leave a half-written file looking finished.
    """
    if rep.health is None or not rep.repairable:
        raise ValueError(f"{rep.path.name} does not need repairing")

    dest = Path(dest) if dest else rep.path.with_name(repaired_name(rep.path))
    # Order matters: writing onto the original is also "the file exists", and
    # that is the refusal worth naming.
    if dest.resolve() == rep.path.resolve():
        raise ValueError("refusing to overwrite the original recording")
    if dest.exists():
        raise FileExistsError(dest)

    free = shutil.disk_usage(dest.parent).free
    need = rep.health.good_end + len(_synthetic_tail())
    if free < need:
        raise OSError(
            f"need {need / 1e9:.2f} GB for the repaired copy but only "
            f"{free / 1e9:.2f} GB is free on {dest.drive or dest.parent}")

    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Windows still refuses paths past 260 characters unless long-path support
    # is on, and a flight folder nested inside Dropbox gets close. Say so,
    # rather than letting it surface as a bare "file not found" on a file we
    # were trying to create.
    if len(str(tmp)) > 255:
        raise OSError(
            f"the repaired copy's path is {len(str(tmp))} characters, which "
            f"Windows will not create. Choose a shorter destination "
            f"folder:\n{tmp}")
    done = 0
    chunk = 8 << 20
    try:
        with open(rep.path, "rb") as src, open(tmp, "wb") as out:
            while done < rep.health.good_end:
                if cancel is not None and cancel.is_set():
                    from .ffmpeg_tools import CancelledError
                    raise CancelledError("cancelled")
                buf = src.read(min(chunk, rep.health.good_end - done))
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if progress:
                    progress(done / max(1, rep.health.good_end),
                             f"{dest.name}: {done / 1e9:.2f} GB")
            out.write(_synthetic_tail())
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    # Prove it: the copy must now scan clean, or we hand back nothing.
    check = scan_health(dest)
    if not check.complete:
        dest.unlink(missing_ok=True)
        raise OSError(f"the repaired copy did not verify ({dest.name})")
    if progress:
        progress(1.0, f"wrote {dest.name}")
    return dest


def looks_like_mcap(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MCAP_MAGIC)) == MCAP_MAGIC
    except OSError:
        return False
