# PyInstaller spec for Underwater Telemetry Compositing (UTC).
#   pyinstaller utc.spec
# Produces dist/Underwater-Telemetry-Compositing.exe -- a single file that needs no Python
# install, so it can be handed to teammates directly.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
# The transect extractor is a sibling package, installed editable during
# development. PyInstaller cannot follow an editable install's import hook, so
# the source directory goes on the search path and it is found as plain source.
EXTRACTOR = ROOT.parent / "mcap_to_csv"

binaries = []
datas = [(str(ROOT / "assets"), "assets")]
# The Lightroom plugin is Lua source that gets copied into Lightroom's
# Modules folder at run time, so it has to survive the freeze as files.
datas += [(str(ROOT / "utc" / "lightroom" / "plugin"),
           "utc/lightroom/plugin")]
datas += collect_data_files("customtkinter")
# tzdata is a data-only package: without this the packaged app
# cannot resolve local times and every transect lands wrong.
datas += collect_data_files("tzdata")

# imageio-ffmpeg carries the static ffmpeg binary we shell out to
try:
    import imageio_ffmpeg, os
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    datas.append((exe, "imageio_ffmpeg/binaries"))
except Exception:
    pass

# Tcl/Tk from a conda-style Python.
#
# PyInstaller resolves these automatically for a python.org layout, where the
# DLLs sit beside _tkinter.pyd in DLLs/. Anaconda puts them in Library/bin,
# which is only on PATH while the environment is activated -- so a build from a
# conda environment silently omits them and the .exe dies with "DLL load failed
# while importing _tkinter" the moment it opens its first window. The build
# looks entirely successful.
import os as _os_env

_base = Path(sys.base_prefix)
_conda_bin = _base / "Library" / "bin"
if _conda_bin.is_dir():
    # Anaconda keeps the C libraries its extension modules link against in
    # Library/bin, which is only on PATH while the environment is activated.
    # PyInstaller resolves an extension's DLL dependencies by searching PATH,
    # so without this it silently omits libexpat, tcl, tk and friends -- and
    # the .exe fails at runtime on whichever one is imported first. Putting the
    # directory on PATH for the build fixes the whole class at once rather than
    # one DLL at a time.
    _os_env.environ["PATH"] = str(_conda_bin) + _os_env.pathsep + _os_env.environ.get("PATH", "")

if (_conda_bin / "tcl86t.dll").is_file():
    for _dll in ("tcl86t.dll", "tk86t.dll", "zlib1.dll"):
        _f = _conda_bin / _dll
        if _f.is_file():
            binaries.append((str(_f), "."))
    for _name, _dest in (("tcl8.6", "_tcl_data"), ("tk8.6", "_tk_data")):
        _d = _base / "Library" / "lib" / _name
        if _d.is_dir():
            datas.append((str(_d), _dest))

a = Analysis(
    ["launch.py"],
    pathex=[str(ROOT), str(EXTRACTOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["PIL._tkinter_finder", "av", "mcap", "tzdata",
                   "pymavlink", "pymavlink.mavutil",
                   "pymavlink.DFReader",
                   "utc.gui.app", "utc.cli", "utc.selftest", "utc.blueos",
                   "multiprocessing.spawn", "multiprocessing.popen_spawn_win32",
                   # RAW develop drives Lightroom through Windows UI Automation
                   "pywinauto", "comtypes", "win32api"]
                  # The Transects page imports the extractor lazily, inside the
                  # function that runs it, so nothing in the source tree points
                  # at it for the dependency scanner to follow.
                  + collect_submodules("ccr_m2c"),
    # pandas and scipy were excluded to keep the build small, which also kept
    # the extractor out of it -- the packaged app had a Transects page that
    # could only tell you to install something. They are its dependencies.
    excludes=["matplotlib", "pytest"],
    noarchive=False,
)
# Set COMPOSITE_DEBUG=1 to build a console variant. A windowed build discards
# stdout and stderr, so a startup failure (a missing hidden import, typically)
# leaves no trace at all -- the console build is how you find out why.
import os as _os
_debug = bool(_os.environ.get("COMPOSITE_DEBUG"))

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=("Underwater-Telemetry-Compositing-debug" if _debug
          else "Underwater-Telemetry-Compositing"),
    console=_debug,
    icon=str(ROOT / "assets" / "app.ico"),
    upx=False,
)
