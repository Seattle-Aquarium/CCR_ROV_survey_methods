"""
Reading a Lightroom catalog while Lightroom is using it.

A ``.lrcat`` is a SQLite database in WAL mode, so a second *read-only*
connection alongside the running application is safe -- readers never block the
writer and never see a half-written transaction. Everything here opens with
``mode=ro`` and never issues anything but SELECT.

Why bother at all: AI Denoise has no SDK entry point, so the plugin cannot tell
us how far along it is. But Lightroom records the result per photo as it goes,
and that record is visible here the moment each photo's transaction commits.
That is the progress bar.

This reads an *undocumented* schema. Adobe can change it in any release, so
every query is defensive: a missing table or column degrades to "cannot tell"
rather than raising, and the caller falls back to an indeterminate progress
message. The one thing that must never happen is a schema change turning into a
crash after forty minutes of GPU time.

Schema facts this relies on, confirmed against Lightroom Classic 14.5.1:

* ``AgLibraryFile`` -> ``Adobe_images`` -> ``Adobe_imageDevelopSettings``
  chains a file on disk to its develop state.
* ``croppedWidth`` / ``croppedHeight`` hold the post-crop pixel size, or the
  string ``'uncropped'`` when no crop is set.
* ``removeChromaticAberration`` mirrors the AutoLateralCA checkbox.
* An AI-Denoised photo carries ``hasBigData = 1`` and a ``FilterList`` entry
  named ``Enhance`` in its settings text -- there is no separate DNG.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: Denoise writes its payload to the .lrcat-data blob store and leaves this
#: marker in the develop-settings text. GLOB is the case-sensitive one --
#: LIKE would also match "LuminanceNoiseReduction".
_DENOISE_GLOB = "*Denoise*"


class CatalogUnreadable(RuntimeError):
    """The catalog could not be opened or does not have the schema we expect."""


def open_readonly(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """A read-only connection to a catalog Lightroom may have open."""
    p = Path(path)
    if not p.exists():
        raise CatalogUnreadable(f"no catalog at {p}")
    uri = "file:" + p.resolve().as_posix().replace("?", "%3f").replace("#", "%23")
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=timeout)
        con.execute("select count(*) from sqlite_master").fetchone()
        return con
    except sqlite3.Error as ex:
        raise CatalogUnreadable(f"{p.name}: {ex}") from ex


@dataclass(frozen=True)
class PhotoState:
    """One photo's progress through the batch, as the catalog sees it."""

    image_id: int
    filename: str
    crop_w: int | None          # None while uncropped
    crop_h: int | None
    remove_ca: bool
    denoised: bool

    def cropped_to(self, w: int, h: int) -> bool:
        return self.crop_w == w and self.crop_h == h


def _as_px(value) -> int | None:
    """croppedWidth is a REAL, or the text 'uncropped'."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


_STATE_SQL = """
select i.id_local,
       f.idx_filename,
       d.croppedWidth,
       d.croppedHeight,
       d.removeChromaticAberration,
       d.hasBigData,
       case when d.text glob ? then 1 else 0 end
  from AgLibraryFile f
  join AgLibraryFolder fo on fo.id_local = f.folder
  join AgLibraryRootFolder rf on rf.id_local = fo.rootFolder
  join Adobe_images i on i.rootFile = f.id_local
  join Adobe_imageDevelopSettings d on d.image = i.id_local
 where lower(rf.absolutePath) || lower(fo.pathFromRoot) = ?
"""


def _folder_key(folder: Path) -> str:
    """The form Lightroom stores: forward slashes, trailing slash, lowercased.

    Lightroom splits a path into a root folder (absolute, trailing slash) and a
    pathFromRoot; concatenating them reproduces the folder, so matching on the
    concatenation works whichever way an import happened to split it.
    """
    s = Path(folder).resolve().as_posix().lower()
    return s if s.endswith("/") else s + "/"


def states_in(con: sqlite3.Connection, folder: Path) -> list[PhotoState]:
    """Every photo the catalog holds from `folder`, with its develop state."""
    try:
        rows = con.execute(_STATE_SQL, (_DENOISE_GLOB, _folder_key(folder))).fetchall()
    except sqlite3.Error as ex:
        raise CatalogUnreadable(f"catalog schema not as expected: {ex}") from ex
    out = []
    for img, name, cw, ch, ca, big, den in rows:
        out.append(PhotoState(
            image_id=int(img),
            filename=str(name or ""),
            crop_w=_as_px(cw),
            crop_h=_as_px(ch),
            remove_ca=bool(ca),
            # hasBigData alone also covers masks, so both must hold.
            denoised=bool(den) and bool(big),
        ))
    return out


@dataclass
class Progress:
    """A count of photos past each milestone, for the progress bar."""

    total: int = 0
    cropped: int = 0
    denoised: int = 0
    #: True when the catalog could not be read this tick -- show an
    #: indeterminate message rather than a wrong number.
    unknown: bool = False


class CatalogPoller:
    """Repeatedly counts how many photos in `folder` are done.

    Holds no connection between polls. Lightroom checkpoints WAL and can
    replace files underneath us; reopening each time costs about a millisecond
    and removes a whole class of stale-snapshot bug.
    """

    def __init__(self, catalog: Path, folder: Path, *,
                 crop_w: int, crop_h: int) -> None:
        self.catalog = Path(catalog)
        self.folder = Path(folder)
        self.crop_w = crop_w
        self.crop_h = crop_h
        self._last = Progress()
        self._last_change = time.monotonic()

    def poll(self) -> Progress:
        try:
            con = open_readonly(self.catalog)
        except CatalogUnreadable:
            out = Progress(total=self._last.total, cropped=self._last.cropped,
                           denoised=self._last.denoised, unknown=True)
            return out
        try:
            states = states_in(con, self.folder)
        except CatalogUnreadable:
            return Progress(total=self._last.total, cropped=self._last.cropped,
                            denoised=self._last.denoised, unknown=True)
        finally:
            con.close()

        now = Progress(
            total=len(states),
            cropped=sum(1 for s in states if s.cropped_to(self.crop_w, self.crop_h)),
            denoised=sum(1 for s in states if s.denoised),
        )
        if (now.total, now.cropped, now.denoised) != (
                self._last.total, self._last.cropped, self._last.denoised):
            self._last_change = time.monotonic()
        self._last = now
        return now

    @property
    def stalled_for(self) -> float:
        """Seconds since any count last moved -- for degrading to a spinner."""
        return time.monotonic() - self._last_change
