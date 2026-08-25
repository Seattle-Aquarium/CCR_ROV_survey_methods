"""
Stamping ROV telemetry onto flight stills.

The downward GoPro shoots a still every ~3 s alongside the video. The JPEGs are
otherwise discarded -- the GPR raws carry the ecological data -- so they are
free to carry diagnostics instead.

Three things happen to a photo:

1. Its capture time is read from EXIF. GoPro Labs precision time writes both
   ``DateTimeOriginal`` and ``OffsetTimeOriginal``, so a still carries its own
   UTC offset and needs no timezone guessing at all -- a stronger position than
   the video path, which derives the offset from the flight date.
2. A band is added *above* the frame carrying the telemetry at that instant.
   Extending the canvas rather than drawing over the image keeps every original
   pixel intact, which matters because the top of the frame is where light
   levels get inspected.
3. It is renamed to carry the values that decide whether a frame is usable:
   time, altitude, light power and speed.

Writing in place is supported and intended, so **double-stamping is the hazard
that matters**: a second pass would add a second band and there is no original
left to recover. Every stamped file is marked in EXIF and skipped on re-run.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image, ExifTags, ImageDraw, ImageFont, ImageOps, JpegImagePlugin

from . import brand
from .telemetry import TelemetryStore

ProgressCB = Callable[[float, str], None]

_TAG = {v: k for k, v in ExifTags.TAGS.items()}
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = _TAG["DateTimeOriginal"]
_OFFSET_ORIGINAL = _TAG["OffsetTimeOriginal"]
_OFFSET = _TAG["OffsetTime"]
_IMAGE_DESCRIPTION = _TAG["ImageDescription"]
_ORIENTATION = _TAG["Orientation"]

#: Written into EXIF ImageDescription so a second pass can recognise its own
#: work. GoPro leaves this tag empty, so there is nothing to collide with.
STAMP_MARKER = "UTC-telemetry-stamp"

PHOTO_EXTS = {".jpg", ".jpeg"}


# --------------------------------------------------------------------------
#  Reading capture time
# --------------------------------------------------------------------------


def _parse_offset(text: str | None) -> timedelta | None:
    """'-07:00' -> timedelta. GoPro writes this when precision time is set."""
    if not text:
        return None
    m = re.fullmatch(r"\s*([+-])(\d{2}):(\d{2})\s*", str(text))
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


@dataclass
class Photo:
    """One still, placed on the absolute epoch timeline."""

    path: Path
    epoch: float
    local: datetime
    stamped: bool = False

    @property
    def tc25(self) -> str:
        return self.local.strftime("%H:%M:%S")


def read_photo_time(
    path: Path, fallback_offset_hours: float | None = None
) -> tuple[float, datetime, bool] | None:
    """(epoch, local datetime, already_stamped), or None if there is no time.

    A still without ``DateTimeOriginal`` cannot be placed on the timeline and is
    reported rather than guessed at.
    """
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            ifd = exif.get_ifd(_EXIF_IFD)
    except Exception:
        return None

    raw = ifd.get(_DATETIME_ORIGINAL) or exif.get(_TAG["DateTime"])
    if not raw:
        return None
    try:
        naive = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

    delta = _parse_offset(ifd.get(_OFFSET_ORIGINAL) or ifd.get(_OFFSET))
    if delta is None:
        if fallback_offset_hours is None:
            return None
        delta = timedelta(hours=fallback_offset_hours)

    local = naive.replace(tzinfo=timezone(delta))
    desc = str(exif.get(_IMAGE_DESCRIPTION) or "")
    return local.timestamp(), local, STAMP_MARKER in desc


def index_photos(
    directory: Path,
    *,
    fallback_offset_hours: float | None = None,
    recursive: bool = False,
) -> tuple[list[Photo], list[str]]:
    """Every readable still in a folder, in time order, plus any warnings."""
    directory = Path(directory)
    it: Iterable[Path] = (
        directory.rglob("*") if recursive else directory.iterdir()
    )
    photos: list[Photo] = []
    undated: list[str] = []
    for p in sorted(it):
        if not p.is_file() or p.suffix.lower() not in PHOTO_EXTS:
            continue
        got = read_photo_time(p, fallback_offset_hours)
        if got is None:
            undated.append(p.name)
            continue
        epoch, local, stamped = got
        photos.append(Photo(p, epoch, local, stamped))

    photos.sort(key=lambda ph: ph.epoch)
    warnings: list[str] = []
    if undated:
        shown = ", ".join(undated[:4]) + (" ..." if len(undated) > 4 else "")
        warnings.append(
            f"{len(undated)} photo(s) have no EXIF capture time and were "
            f"skipped: {shown}. Was the camera time-synced?"
        )
    return photos, warnings


# --------------------------------------------------------------------------
#  The telemetry band
# --------------------------------------------------------------------------


@dataclass
class BandStyle:
    """Geometry and colour of the strip added above the frame.

    Sizes are fractions of the photo so a band looks the same on any capture
    mode. The band covers no image pixels, so its height costs nothing but a
    little canvas: 3% of frame height reads immediately at fit-to-screen and is
    still negligible against 27 megapixels.
    """

    height_frac: float = 0.030
    min_height: int = 60
    value_frac: float = 0.56          # value type, as a fraction of band height
    max_value_frac: float = 0.62      # ceiling, so tall type cannot crowd the band
    label_frac: float = 0.44
    pad_frac: float = 0.35            # left pad within a slot, of band height
    rule_frac: float = 0.055          # accent rule under the band

    bg: str = brand.FATHOM
    label: str = "#A8BBD4"
    value: str = brand.WHITE
    rule: str = brand.SEAFOAM
    time_color: str = brand.SEAFOAM

    def height(self, photo_h: int) -> int:
        return max(self.min_height, int(round(photo_h * self.height_frac)))


@dataclass(frozen=True)
class Field:
    label: str
    key: str
    unit: str
    digits: int | None
    #: Widest value this field can produce. Slots are sized to it so a long
    #: value can never be clipped and, just as importantly, so every value keeps
    #: the same position in every photo of a transect.
    widest: str


#: The values that decide whether a frame is usable, in the order asked for.
#: `key` must be produced by TelemetryStore.sample().
BAND_FIELDS: tuple[Field, ...] = (
    Field("ALT", "altitude", "m", 2, "00.00 m"),
    Field("SPEED", "speed", "m/s", 2, "0.00 m/s"),
    Field("LIGHTS", "lights", "%", 0, "000%"),
    Field("DEPTH", "depth", "m", 2, "000.00 m"),
    Field("POWER", "power_w", "W", 0, "0000 W"),
    # MOTOR_DETECT is the longest ArduSub mode name; sizing to ALT_HOLD clips it.
    Field("MODE", "mode", "", None, "MOTOR_DETECT"),
)

TIME_FIELD = Field("TC25", "", "", None, "00:00:00")


def format_value(value, field: Field) -> str:
    if value is None:
        return "--"
    if field.digits is None:
        return str(value)
    try:
        return f"{float(value):.{field.digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _font(size: int, weight: str) -> ImageFont.FreeTypeFont:
    path = brand.font_path(weight)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def render_band(
    width: int,
    height: int,
    sample: dict,
    tc25: str,
    style: BandStyle,
) -> Image.Image:
    """Draw the telemetry strip.

    Each field is given exactly the width its own widest possible value needs,
    and the slack is shared out as equal gaps. Equal-width slots would force
    ``LIGHTS 000%`` to reserve as much room as ``MODE MOTOR_DETECT``, which
    wastes most of the width and holds the type far smaller than it need be.

    Two properties this has to keep:

    * **Nothing is ever clipped.** Montserrat is proportional and its digits are
      not even the same width as each other, so the size is chosen against the
      widest content, never against the values in front of us.
    * **Positions do not move between photos.** Layout depends only on the field
      list and those worst-case strings, so a value sits at the same x in every
      photo of a transect and can be read straight down a folder. Sizing to the
      actual values would make the row shuffle frame to frame.
    """
    band = Image.new("RGB", (width, height), style.bg)
    d = ImageDraw.Draw(band)
    margin = int(height * style.pad_frac)
    gap_lv = height * 0.16                    # label to its value
    ratio = style.label_frac / style.value_frac

    fields = (TIME_FIELD,) + BAND_FIELDS

    def measure(size: int):
        fv = _font(size, "semibold")
        fl = _font(max(6, int(size * ratio)), "medium")
        widths = [d.textlength(f.label, font=fl) + gap_lv
                  + d.textlength(f.widest, font=fv) for f in fields]
        return fv, fl, widths

    # Largest type whose worst-case row still leaves a readable gap between
    # fields. Bounded above by the band's own height so tall type cannot crowd
    # it, and below so a pathological field list still renders something.
    min_gap = height * 0.55
    vsize, f_value, f_label, widths = 8, *measure(8)
    for size in range(int(height * style.max_value_frac), 7, -2):
        fv, fl, w = measure(size)
        slack = width - 2 * margin - sum(w)
        if slack >= min_gap * (len(fields) - 1):
            vsize, f_value, f_label, widths = size, fv, fl, w
            break

    gap = ((width - 2 * margin - sum(widths)) / (len(fields) - 1)
           if len(fields) > 1 else 0.0)

    asc, desc = f_value.getmetrics()          # shared baseline: one clean line
    baseline = (height + asc - desc) // 2
    la, _ld = f_label.getmetrics()

    def draw_pair(x: float, label: str, value: str, fill: str) -> None:
        d.text((x, baseline - la), label, font=f_label, fill=style.label)
        lw = d.textlength(label, font=f_label)
        d.text((x + lw + gap_lv, baseline - asc), value, font=f_value, fill=fill)

    x = float(margin)
    draw_pair(x, TIME_FIELD.label, tc25, style.time_color)
    x += widths[0] + gap
    for fld, w in zip(BAND_FIELDS, widths[1:]):
        text = format_value(sample.get(fld.key), fld)
        if fld.unit and text != "--":
            text = f"{text}{'' if fld.unit == '%' else ' '}{fld.unit}"
        draw_pair(x, fld.label, text, style.value)
        x += w + gap

    rule = max(1, int(height * style.rule_frac))
    d.rectangle([0, height - rule, width, height], fill=style.rule)
    return band


# --------------------------------------------------------------------------
#  Renaming
# --------------------------------------------------------------------------


def _num(value, digits: int) -> str:
    if value is None:
        return "NA"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "NA"
    if not math.isfinite(v):
        return "NA"
    return f"{v:.{digits}f}"


def stamped_name(
    local: datetime,
    sample: dict,
    suffix: str = ".JPG",
    stem: str | None = None,
) -> str:
    """``YYYY-MM-DD_hh-mm-ss_0.80m_80p_0.124ms_G0014606.JPG``

    Altitude, light power and speed are the three values that decide whether a
    frame is worth looking at, so putting them in the name makes a folder
    listing sortable and filterable without opening anything.

    The camera's original stem is kept on the end because the GPR raws share it,
    and those are what the ecological analysis uses -- without it the pairing
    would only be recoverable by reading EXIF on every file.
    """
    stamp = local.strftime("%Y-%m-%d_%H-%M-%S")
    alt = _num(sample.get("altitude"), 2)
    lights = _num(sample.get("lights"), 0)
    speed = _num(sample.get("speed"), 3)
    tail = f"_{stem}" if stem else ""
    return f"{stamp}_{alt}m_{lights}p_{speed}ms{tail}{suffix}"


def unique_path(directory: Path, name: str) -> Path:
    """Avoid clobbering: two stills can share a second at a 3 s interval only
    if the clock stalls, but a collision must never silently delete a photo."""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        alt = directory / f"{stem}_{i}{suffix}"
        if not alt.exists():
            return alt
    raise OSError(f"could not find a free name for {name} in {directory}")


# --------------------------------------------------------------------------
#  Stamping one photo
# --------------------------------------------------------------------------


@dataclass
class StampResult:
    source: Path
    output: Path | None
    renamed: bool
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.output is not None and self.skipped is None


def stamp_photo(
    photo: Photo,
    store: TelemetryStore,
    *,
    out_dir: Path | None = None,
    style: BandStyle | None = None,
    rename: bool = True,
    quality: int | None = None,
) -> StampResult:
    """Add the band and rename. Writes in place when `out_dir` is None.

    Already-stamped photos are left alone. The band is not reversible, so a
    second pass would deface a file we cannot restore.
    """
    style = style or BandStyle()
    if photo.stamped:
        return StampResult(photo.path, None, False, skipped="already stamped")

    sample = store.sample(photo.epoch)

    with Image.open(photo.path) as im:
        im.load()
        exif = im.getexif()
        # The camera is mounted backwards, so GoPro records a 180 degree flip as
        # EXIF Orientation rather than in the pixels. Pillow does NOT apply that
        # on open, so the buffer we get is stored-upside-down. Pasting a band on
        # it and keeping the tag makes every viewer rotate the result: photo
        # upright, band at the bottom and inverted. Same trap as the video path.
        #
        # So bake the rotation into the pixels and reset the tag to 1. After
        # this the file means exactly what it shows, and nothing downstream can
        # rotate it a second time.
        # Reuse the source's quantisation tables and chroma subsampling so
        # re-encoding costs no visible quality -- the pixels are untouched, only
        # the canvas grows. "keep" cannot be used here because the image we save
        # is a new canvas rather than the decoded JPEG, so the sampling has to
        # be read off the source and passed as a value.
        qtables = getattr(im, "quantization", None)
        try:
            subsampling = JpegImagePlugin.get_sampling(im)
        except Exception:
            subsampling = -1
        upright = ImageOps.exif_transpose(im)
        src = upright.convert("RGB") if upright.mode != "RGB" else upright.copy()
    exif[_ORIENTATION] = 1

    w, h = src.size
    band_h = style.height(h)
    band = render_band(w, band_h, sample, photo.tc25, style)

    canvas = Image.new("RGB", (w, h + band_h), style.bg)
    canvas.paste(band, (0, 0))
    canvas.paste(src, (0, band_h))

    # The original name is recorded because renaming breaks the 1:1 pairing with
    # the GPR raws, which share the camera's stem and are what the ecological
    # analysis actually uses. Keeping it here means the link is always
    # recoverable from the file itself.
    exif[_IMAGE_DESCRIPTION] = (
        f"{STAMP_MARKER}; source={photo.path.name}; band={band_h}px; "
        f"tc25={photo.tc25}"
    )

    target_dir = Path(out_dir) if out_dir else photo.path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    name = (stamped_name(photo.local, sample, photo.path.suffix, photo.path.stem)
            if rename else photo.path.name)
    dest = unique_path(target_dir, name)

    save_kw: dict = {"exif": exif.tobytes()}
    if subsampling in (0, 1, 2):
        save_kw["subsampling"] = subsampling
    if qtables and quality is None:
        save_kw["qtables"] = qtables
    else:
        save_kw["quality"] = quality or 95

    canvas.save(dest, format="JPEG", **save_kw)

    # In-place means the original is replaced. Only remove it once the new file
    # exists, and never when the name did not change (we just overwrote it).
    if out_dir is None and dest != photo.path:
        photo.path.unlink(missing_ok=True)

    return StampResult(photo.path, dest, renamed=dest.name != photo.path.name)


# --------------------------------------------------------------------------
#  Driving a whole flight
# --------------------------------------------------------------------------

#: What to do with stills that fall outside every transect.
KEEP = "keep"
MOVE = "move"
DELETE = "delete"
OFF_TRANSECT_CHOICES = (KEEP, MOVE, DELETE)

OFF_TRANSECT_DIR = "off_transect"

#: Where the stills sit inside a flight's photos/ folder, best first. Older
#: flights vary, so this is searched rather than assumed.
_JPEG_DIRS = ("JPEG", "JPG", "jpeg", "jpg", "JPEGs", "photos", "")


def find_photo_dir(photos_root: Path | None) -> Path | None:
    """The folder holding the stills, or None.

    The GPR raws live beside them in their own folder and are never touched --
    they are the files the ecological analysis uses.
    """
    if photos_root is None:
        return None
    root = Path(photos_root)
    if not root.is_dir():
        return None
    for rel in _JPEG_DIRS:
        d = (root / rel) if rel else root
        if d.is_dir() and any(
            q.suffix.lower() in PHOTO_EXTS for q in d.iterdir() if q.is_file()
        ):
            return d
    return None


def _retry_unlink(path: Path, tries: int = 20, wait: float = 0.5) -> bool:
    """Dropbox holds a handle while it uploads; a lock here is transient."""
    import time

    for _ in range(tries):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            time.sleep(wait)
    return False


@dataclass
class PhotoReport:
    stamped: int = 0
    skipped: int = 0
    failed: int = 0
    off_transect: int = 0
    off_transect_action: str = KEEP
    folders: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Photos: {self.stamped} stamped into "
                 f"{len(self.folders)} transect folder(s)"]
        if self.skipped:
            lines.append(f"  {self.skipped} already stamped, left alone")
        if self.failed:
            lines.append(f"  {self.failed} failed")
        verb = {KEEP: "left in place", MOVE: f"moved to {OFF_TRANSECT_DIR}/",
                DELETE: "deleted"}[self.off_transect_action]
        lines.append(f"  {self.off_transect} off-transect still(s) {verb}")
        return "\n".join(lines)


def process_flight(
    photo_dir: Path,
    store: TelemetryStore,
    windows: Sequence[tuple[str, float, float]],
    *,
    off_transect: str = KEEP,
    style: BandStyle | None = None,
    fallback_offset_hours: float | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> PhotoReport:
    """Stamp every on-transect still, and dispose of the rest as asked.

    `windows` is (name, epoch_start, epoch_end) per transect, so this needs
    nothing from the video path -- stills carry their own UTC offset and are
    placed on the timeline independently.

    A still is written into its transect folder *before* the original is
    removed, so an interruption can cost time but never a photo.
    """
    if off_transect not in OFF_TRANSECT_CHOICES:
        raise ValueError(f"off_transect must be one of {OFF_TRANSECT_CHOICES}")

    photo_dir = Path(photo_dir)
    rep = PhotoReport(off_transect_action=off_transect)
    photos, warns = index_photos(
        photo_dir, fallback_offset_hours=fallback_offset_hours
    )
    rep.warnings.extend(warns)
    if not photos:
        rep.warnings.append(f"no stills found in {photo_dir}")
        return rep

    def transect_for(p: Photo) -> str | None:
        for name, lo, hi in windows:
            if lo <= p.epoch <= hi:
                return name
        return None

    assigned = [(p, transect_for(p)) for p in photos]
    on = [(p, n) for p, n in assigned if n]
    off = [p for p, n in assigned if not n]
    total = max(1, len(on) + len(off))
    done = 0

    for p, name in on:
        if cancel is not None and cancel.is_set():
            from .ffmpeg_tools import CancelledError
            raise CancelledError("cancelled")
        out_dir = photo_dir / name
        try:
            res = stamp_photo(p, store, out_dir=out_dir, style=style)
        except Exception as ex:
            rep.failed += 1
            rep.errors.append(f"{p.path.name}: {ex}")
            done += 1
            continue
        if res.skipped:
            rep.skipped += 1
        elif res.output and res.output.is_file():
            if res.output != p.path and not _retry_unlink(p.path):
                rep.warnings.append(
                    f"stamped {res.output.name} but could not remove the "
                    f"original {p.path.name}; it is open in another program"
                )
            rep.stamped += 1
            if out_dir not in rep.folders:
                rep.folders.append(out_dir)
        done += 1
        if progress and done % 5 == 0:
            progress(done / total, f"photos {done}/{total}")

    rep.off_transect = len(off)
    if off and off_transect == MOVE:
        dest = photo_dir / OFF_TRANSECT_DIR
        dest.mkdir(parents=True, exist_ok=True)
        for p in off:
            target = unique_path(dest, p.path.name)
            try:
                p.path.replace(target)
            except OSError as ex:
                rep.warnings.append(f"could not move {p.path.name}: {ex}")
    elif off and off_transect == DELETE:
        for p in off:
            if not _retry_unlink(p.path):
                rep.warnings.append(f"could not delete {p.path.name}")

    if progress:
        progress(1.0, f"photos: {rep.stamped} stamped")
    return rep
