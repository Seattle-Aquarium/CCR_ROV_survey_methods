"""
Finding Lightroom, installing the plugin, and minting a scratch catalog.

Three one-time-ish concerns live here so `runner` can stay about the run.

**The plugin.** Installed into UTC's own folder, then registered with
Lightroom *once* through its Plug-in Manager. The copy on disk is
version-stamped and idempotent; the registration is Lightroom's to write and
UTC's only to read.

The obvious shortcut does not work. ``%APPDATA%\\Adobe\\Lightroom\\Modules``
is widely described as an auto-load folder, and in Lightroom Classic 14.5.1 it
is not: a plugin placed there is never read at all -- its manifest is not
parsed and it never appears in ``AgSdkPluginLoader_installedPluginPaths``.
Nothing reports this, so the symptom is a batch that waits for a plugin that
will never speak. Hence `plugin_registration`, which checks the thing that
actually determines whether a run can work.

**The seed catalog.** The SDK cannot create or open a catalog, and
``lightroom.exe some.lrcat`` prompts rather than silently creating one. So the
operator makes a single empty catalog once, by hand, and every run *copies* it
to a fresh scratch file. Each run still gets its own brand-new catalog that is
deleted afterwards -- nothing is ever shared or reused between runs -- the seed
is only ever an empty stencil. It is stamped with the Lightroom build that made
it, so an application update asks for a new one rather than triggering a
catalog-upgrade prompt mid-run.

**The run directory.** One folder per run holding the job file, the status file
the plugin writes, the marker files the two sides hand off with, and the
scratch catalog. Deleted on success, kept on failure -- the plugin log inside
it is the only account of what Lightroom did.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PLUGIN_DIRNAME = "UTC.lrplugin"
SEED_NAME = "UTC_seed.lrcat"

#: Bumped when the Lua changes, so an installed copy is replaced.
PLUGIN_VERSION = "16"
_STAMP = ".utc-plugin-version"


# --------------------------------------------------------------------------
#  Where things are
# --------------------------------------------------------------------------

def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA")
                or Path.home() / "AppData" / "Local")


def utc_root() -> Path:
    """Everything this feature keeps between runs."""
    return _local_app_data() / "UTC" / "lightroom"


def lightroom_appdata() -> Path:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return appdata / "Adobe" / "Lightroom"


def legacy_modules_plugin() -> Path:
    """Where an older UTC put the plugin, on a wrong assumption.

    ``%APPDATA%\\Adobe\\Lightroom\\Modules`` is widely described as an
    auto-load folder. It is not, at least not in Lightroom Classic 14.5.1:
    a plugin dropped there is never read -- no manifest parse, no entry in
    ``AgSdkPluginLoader_installedPluginPaths`` -- and the batch simply waits
    for a plugin that will never speak. Any copy left there is removed so it
    cannot look like the installed one.
    """
    return lightroom_appdata() / "Modules" / PLUGIN_DIRNAME


def preferences_file() -> Path | None:
    """Lightroom's preferences, which record every registered plugin.

    Read-only, always. These are Adobe's settings: a bad write costs the
    operator every preference they have ever set, and Lightroom rewrites the
    file on exit anyway. UTC reads it to *verify* that registration happened,
    and asks Lightroom itself to do the writing.
    """
    d = lightroom_appdata() / "Preferences"
    if not d.is_dir():
        return None
    found = [p for p in d.glob("*Preferences.agprefs")
             if "startup" not in p.name.lower()]
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def plugin_source() -> Path:
    """The plugin as shipped -- in the repo, or unpacked from the .exe.

    The frozen location is only consulted when actually frozen. Path("") is
    Path("."), so a bare fallback would resolve against the working directory
    and find -- or fail to find -- the plugin depending on where UTC was
    started from.
    """
    bases = [Path(__file__).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.insert(0, Path(meipass))
        bases.append(Path(sys.executable).parent)
    for base in bases:
        for cand in (base / "plugin" / PLUGIN_DIRNAME,
                     base / "utc" / "lightroom" / "plugin" / PLUGIN_DIRNAME):
            if (cand / "Info.lua").is_file():
                return cand
    raise LightroomNotSetUp(
        f"the {PLUGIN_DIRNAME} folder is missing from this build of UTC")


class LightroomNotSetUp(RuntimeError):
    """Something Lightroom-side is missing, with an operator-readable reason."""


# --------------------------------------------------------------------------
#  The application
# --------------------------------------------------------------------------

_EXE_CANDIDATES = (
    Path("C:/Program Files/Adobe/Adobe Lightroom Classic/lightroom.exe"),
    Path("C:/Program Files/Adobe/Adobe Lightroom Classic CC/lightroom.exe"),
)


@dataclass(frozen=True)
class LightroomApp:
    exe: Path
    version: str          # "14.5.1"

    @property
    def major(self) -> int:
        try:
            return int(self.version.split(".")[0])
        except ValueError:
            return 0


def _file_version(exe: Path) -> str:
    """The product version, straight off the binary. Windows only."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                   # pragma: no cover
        return ""
    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(str(exe), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(exe), 0, size, buf):
        return ""
    root = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(root), ctypes.byref(length)):
        return ""
    # VS_FIXEDFILEINFO: dwFileVersionMS at offset 8, dwFileVersionLS at 12
    data = ctypes.string_at(root, length.value)
    ms = int.from_bytes(data[8:12], "little")
    ls = int.from_bytes(data[12:16], "little")
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"


def find_lightroom() -> LightroomApp:
    """The installed Lightroom Classic, or a message saying it is not there."""
    seen = [p for p in _EXE_CANDIDATES if p.is_file()]
    if not seen:
        base = Path("C:/Program Files/Adobe")
        if base.is_dir():
            seen = sorted(base.glob("Adobe Lightroom Classic*/lightroom.exe"))
    if not seen:
        raise LightroomNotSetUp(
            "Adobe Lightroom Classic is not installed on this machine.")
    exe = seen[0]
    return LightroomApp(exe=exe, version=_file_version(exe) or "unknown")


def lightroom_is_running() -> bool:
    """True if any lightroom.exe is up. We must own the catalog, so it cannot be."""
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq lightroom.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "lightroom.exe" in (out.stdout or "").lower()
    except Exception:
        return False


# --------------------------------------------------------------------------
#  The plugin
# --------------------------------------------------------------------------

def installed_plugin() -> Path:
    """Where the plugin lives once installed.

    Inside UTC's own folder rather than anywhere of Adobe's. Lightroom is told
    about it once, through its Plug-in Manager, and remembers the path -- so
    the location only has to be somewhere stable that UTC controls and can
    keep up to date.
    """
    return utc_root() / "plugin" / PLUGIN_DIRNAME


def boot_log() -> Path:
    """Where the plugin records how far it got at load time."""
    return utc_root() / "plugin_boot.log"


def ran_marker() -> Path:
    """A directory the plugin creates through the SDK's own file API.

    Deliberately a different mechanism from `boot_log`, which uses plain Lua
    file handles. A plugin that never runs and a plugin that runs but cannot
    write look identical from here, and the two marks tell them apart.
    """
    return utc_root() / "plugin_ran"


#: The toolkit identifier declared in Info.lua, used to spot the plugin in
#: Lightroom's disabled list.
TOOLKIT_ID = "org.seattleaquarium.utc.rawdevelop"

_PREF_INSTALLED = "AgSdkPluginLoader_installedPluginPaths"
_PREF_DISABLED_PATHS = "AgSdkPluginLoader_disabledPluginPaths"
_PREF_DISABLED_IDS = "AgSdkPluginLoader_disabledPluginIDs"


def _pref_block(text: str, key: str) -> str:
    """The serialised list stored under `key`, or "" if it is not there.

    The list ends with a closing brace at the start of a line. Terminating on
    the first ``",`` instead looks right and is not: every entry is written
    ``\\"path\\",`` so that sequence occurs *inside* the first entry, and the
    block gets cut after one item -- which reads as "the list has one thing
    in it" rather than as a parse failure.
    """
    start = text.find(key)
    if start < 0:
        return ""
    open_at = text.find("{", start)
    if open_at < 0:
        return ""
    close_at = text.find("\n}", open_at)
    if close_at < 0:
        close_at = text.find('",', open_at)
    return text[open_at:close_at] if close_at > 0 else text[open_at:]


def _squash(s: str) -> str:
    """Comparable form: no backslashes, no case. The preferences file stores
    paths with backslashes doubled twice, so comparing them literally is a
    losing game."""
    return s.replace("\\", "").replace("/", "").lower()


def plugin_registration() -> str:
    """Whether Lightroom knows about the plugin: registered / disabled /
    absent / unknown.

    The Modules folder is not an auto-load location, so being installed on
    disk means nothing on its own -- Lightroom has to have been told. This is
    the check that turns a silent forty-minute wait into a sentence.
    """
    prefs = preferences_file()
    if prefs is None:
        return "unknown"
    try:
        text = prefs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"

    want = _squash(str(installed_plugin()))
    if _squash(TOOLKIT_ID) in _squash(_pref_block(text, _PREF_DISABLED_IDS)):
        return "disabled"
    if want and want in _squash(_pref_block(text, _PREF_DISABLED_PATHS)):
        return "disabled"
    if want and want in _squash(_pref_block(text, _PREF_INSTALLED)):
        return "registered"
    return "absent"


_PREF_RECENT = "recentLibraries20"


def last_real_catalog() -> Path | None:
    """The operator's own most recent catalog, ignoring UTC's scratch ones.

    Lightroom is set to reopen whatever it had last, and after a run that is a
    scratch catalog UTC then deletes -- so launching Lightroom with no argument
    lands on "catalog was not found". Anything UTC needs Lightroom for is
    launched against this instead, which is the catalog the operator would have
    got anyway.
    """
    prefs = preferences_file()
    if prefs is None:
        return None
    try:
        text = prefs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    block = _pref_block(text, _PREF_RECENT)
    ours = _squash(str(utc_root()))
    for raw in re.findall(r'\\"(.+?)\\"', block):
        # Lightroom doubles every backslash twice over.
        candidate = Path(raw.replace("\\\\\\\\", "\\").replace("\\\\", "\\"))
        if ours and ours in _squash(str(candidate)):
            continue
        if candidate.is_file():
            return candidate
    return None


#: Lightroom refuses to open a catalog whose full path is longer than this,
#: because its preview-cache paths would overflow the platform limit. Found
#: the hard way: the application puts up a modal Warning at startup and never
#: reaches the plugin, so the batch waits for something that cannot happen.
MAX_CATALOG_PATH = 174


def catalog_path_problem(path: Path) -> str:
    """Why Lightroom will refuse this catalog path, or "" if it will not."""
    n = len(str(path))
    if n <= MAX_CATALOG_PATH:
        return ""
    return (f"the scratch catalog path is {n} characters and Lightroom "
            f"refuses anything over {MAX_CATALOG_PATH}")


def plugin_is_current() -> bool:
    stamp = installed_plugin() / _STAMP
    try:
        return stamp.read_text(encoding="utf-8").strip() == PLUGIN_VERSION
    except OSError:
        return False


def install_plugin(*, force: bool = False) -> Path:
    """Put the plugin where Lightroom will auto-load it. Idempotent.

    Copies over the top and prunes what is no longer shipped, rather than
    deleting the folder and recreating it. Windows regularly refuses to remove
    a directory something else has open -- an indexer, Explorer, a sync client
    -- and the failure mode there is an *empty* .lrplugin folder, which
    Lightroom loads as a broken plugin. Writing files into a folder that
    already exists cannot leave that state behind.
    """
    dest = installed_plugin()
    if not force and plugin_is_current():
        return dest
    src = plugin_source()
    dest.mkdir(parents=True, exist_ok=True)

    # Stamp last: until it is written the copy counts as out of date, so an
    # interrupted install is repaired by the next run rather than trusted.
    stamp = dest / _STAMP
    stamp.unlink(missing_ok=True)

    shipped = set()
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        shipped.add(rel)
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)

    for item in sorted(dest.rglob("*"), reverse=True):
        rel = item.relative_to(dest)
        if rel in shipped or rel == Path(_STAMP):
            continue
        try:
            item.unlink() if item.is_file() else item.rmdir()
        except OSError:
            pass

    stamp.write_text(PLUGIN_VERSION, encoding="utf-8")

    remove_legacy_plugin()
    return dest


def remove_legacy_plugin() -> bool:
    """Delete the copy an earlier UTC left in Lightroom's Modules folder.

    Windows will refuse to remove a directory another process has open even
    when it is empty -- an indexer holding it for a moment is enough -- so
    emptying it is what matters and the folder itself is a retry. An empty
    folder there is inert; a populated one is a second plugin to reason about.
    """
    d = legacy_modules_plugin()
    if not d.exists():
        return True
    for item in sorted(d.rglob("*"), reverse=True):
        try:
            item.unlink() if item.is_file() else item.rmdir()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
    return not any(d.rglob("*")) if d.exists() else True


# --------------------------------------------------------------------------
#  The seed catalog
# --------------------------------------------------------------------------

def seed_dir(version: str) -> Path:
    """Seeds are per Lightroom build: a catalog made by 14.5.1 opens in 14.5.1
    without the upgrade prompt that would stall an unattended run."""
    return utc_root() / "seed" / (version or "unknown")


def seed_path(app: LightroomApp) -> Path:
    return seed_dir(app.version) / SEED_NAME


def seed_is_ready(app: LightroomApp) -> bool:
    p = seed_path(app)
    return p.is_file() and not describe_seed_problem(p)


def describe_seed_problem(path: Path) -> str:
    """Why this file cannot serve as a seed, or "" if it can."""
    p = Path(path)
    if not p.is_file():
        return f"there is no catalog at {p}"
    if p.suffix.lower() != ".lrcat":
        return f"{p.name} is not a .lrcat file"
    try:
        con = sqlite3.connect(
            "file:" + p.resolve().as_posix() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as ex:
        return f"{p.name} could not be opened: {ex}"
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
        if "Adobe_images" not in tables or "AgLibraryFile" not in tables:
            return f"{p.name} does not look like a Lightroom catalog"
        n = con.execute("select count(*) from Adobe_images").fetchone()[0]
        if n:
            return (f"{p.name} already holds {n} photo(s). The seed has to be "
                    f"an empty catalog -- make a new one rather than reusing "
                    f"a working catalog.")
    except sqlite3.Error as ex:
        return f"{p.name} could not be read: {ex}"
    finally:
        con.close()
    return ""


def adopt_seed(made: Path, app: LightroomApp) -> Path:
    """Take the empty catalog the operator just made and keep it as the seed."""
    problem = describe_seed_problem(made)
    if problem:
        raise LightroomNotSetUp(problem)
    dest = seed_path(app)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(made, dest)
    return dest


def find_new_catalog(under: Path) -> Path | None:
    """The catalog Lightroom's File > New Catalog just wrote, if any.

    New Catalog makes a folder with the catalog inside it, so look one level
    down as well as directly in the folder we pointed the operator at.
    """
    under = Path(under)
    if not under.is_dir():
        return None
    found = sorted(under.glob("*.lrcat")) + sorted(under.glob("*/*.lrcat"))
    found = [p for p in found if not describe_seed_problem(p)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# --------------------------------------------------------------------------
#  The scratch catalog and the run directory
# --------------------------------------------------------------------------

#: Catalog variables that tie a catalog to an Adobe cloud identity. Cleared in
#: every clone: Lightroom only ever syncs one catalog and prompts before
#: switching, but a scratch catalog carrying a store id is an invitation to
#: upload 179 survey frames to somebody's Creative Cloud by accident.
_SYNC_VARIABLES = ("Adobe_storeProviderID", "Adobe_lastScannedCatalogPath")


def new_run_dir() -> Path:
    d = utc_root() / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    prune_runs()
    return d


def mint_catalog(run_dir: Path, app: LightroomApp) -> Path:
    """A fresh, empty, private catalog for this run and no other."""
    seed = seed_path(app)
    problem = describe_seed_problem(seed)
    if problem:
        raise LightroomNotSetUp(
            f"the Lightroom seed catalog is not usable -- {problem}")
    dest = Path(run_dir) / "UTC_scratch.lrcat"
    shutil.copyfile(seed, dest)
    _depersonalise(dest)
    return dest


def _depersonalise(catalog: Path) -> None:
    """Strip the cloud identity the seed may carry. Best effort by design --
    a scratch catalog that cannot be scrubbed is still usable offline."""
    try:
        con = sqlite3.connect(catalog, timeout=10)
        with con:
            for name in _SYNC_VARIABLES:
                con.execute("delete from Adobe_variablesTable where name = ?",
                            (name,))
        con.close()
    except sqlite3.Error:
        pass


#: The small text files worth keeping after a failure. Everything else in a
#: run directory is Lightroom's -- catalog, WAL, lock, the .lrcat-data blob
#: store and the preview pyramids -- and on a 179-frame run that is one to two
#: gigabytes. An allowlist rather than a list of things to delete, because
#: Lightroom's sidecar names are its business and it may add more.
_DIAGNOSTICS = ("plugin.log", "job.txt", "status.txt",
                "enhance_dialog.txt", "menu_items.txt",
                "denoise_notes.txt", "main_window_at_enhance.txt",
                "menu_photo_missing.txt", "menu_enhance_missing.txt",
                "develop_controls.txt", "develop_denoise_hits.txt")


def clean_run_dir(run_dir: Path, *, keep_diagnostics: bool = False) -> None:
    """Strip a finished run down to almost nothing.

    Two things survive. A failed run keeps its log and the job it was given --
    the only account of what Lightroom did. And *every* run keeps the bare
    ``.lrcat``, about two megabytes, for a reason that is not obvious:

    Lightroom is set to reopen whatever catalog it had last, and after a run
    that is this scratch catalog. Deleting it means the operator's own next
    launch of Lightroom greets them with "the catalog was not found" -- UTC
    breaking Lightroom for everything else the machine is used for. Leaving
    the husk costs two megabytes and the pointer resolves.

    Everything heavy goes: the preview pyramid and the ``.lrcat-data`` blob
    store are a gigabyte or more for a full transect and are worth nothing
    once the TIFs are written.
    """
    d = Path(run_dir)
    if not d.exists():
        return
    for item in d.iterdir():
        keep = (item.is_file()
                and (item.suffix.lower() == ".lrcat"
                     or (keep_diagnostics and item.name in _DIAGNOSTICS)))
        if keep:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except OSError:
            pass


#: Failed runs are kept for diagnosis, but not forever and not without limit.
KEEP_RUNS = 10
KEEP_RUN_DAYS = 30


def prune_runs(*, keep: int = KEEP_RUNS, days: int = KEEP_RUN_DAYS) -> int:
    """Drop old run directories. Returns how many went.

    Called when a new run starts rather than on a timer: the moment before a
    batch is the moment the disk space is about to matter.
    """
    root = utc_root() / "runs"
    if not root.is_dir():
        return 0
    try:
        runs = sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    cutoff = time.time() - days * 86400
    gone = 0
    for i, d in enumerate(runs):
        try:
            stale = i >= keep or d.stat().st_mtime < cutoff
        except OSError:
            stale = True
        if stale:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    return gone


def scratch_bytes() -> int:
    """How much disk the run directories are holding right now."""
    root = utc_root() / "runs"
    if not root.is_dir():
        return 0
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total
