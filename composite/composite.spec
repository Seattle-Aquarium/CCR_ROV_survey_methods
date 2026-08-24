# PyInstaller spec for CCR ROV Composite.
#   pyinstaller composite.spec
# Produces dist/CCR-ROV-Composite.exe -- a single file that needs no Python
# install, so it can be handed to teammates directly.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

datas = [(str(ROOT / "assets"), "assets")]
datas += collect_data_files("customtkinter")

# imageio-ffmpeg carries the static ffmpeg binary we shell out to
try:
    import imageio_ffmpeg, os
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    datas.append((exe, "imageio_ffmpeg/binaries"))
except Exception:
    pass

a = Analysis(
    ["launch.py"],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["PIL._tkinter_finder", "av", "mcap",
                   "composite.gui.app", "composite.cli"],
    excludes=["matplotlib", "pandas", "scipy", "pytest"],
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
    name="CCR-ROV-Composite-debug" if _debug else "CCR-ROV-Composite",
    console=_debug,
    icon=str(ROOT / "assets" / "app.ico"),
    upx=False,
)
