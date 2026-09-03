# PyInstaller spec for MCAP to CSV.
#   pyinstaller ccr_m2c.spec
# Produces dist/MCAP-to-CSV.exe -- a single file that needs no Python install,
# so it can be handed to teammates directly.
#
# Set M2C_DEBUG=1 to build a console variant. A windowed build discards stdout
# and stderr, so a startup failure -- a missing hidden import, typically --
# leaves no trace at all; the console build is how you find out why.
import os
from pathlib import Path

ROOT = Path(SPECPATH)

datas = [(str(ROOT / "assets"), "assets")]

a = Analysis(
    ["launch.py"],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=[
        "ccr_m2c.gui", "ccr_m2c.cli",
        # mcap resolves its decompressors at runtime, so the zstd path is not
        # visible to the dependency scanner
        "mcap", "mcap.reader", "zstandard",
        "pytz", "geopy", "geopy.distance",
        "scipy.interpolate",
    ],
    excludes=[
        # nothing here draws or decodes; leaving these in roughly doubles the
        # .exe for no benefit
        "matplotlib", "PIL", "av", "imageio", "imageio_ffmpeg",
        "customtkinter", "pytest", "IPython", "notebook",
        "scipy.spatial", "scipy.optimize", "scipy.stats",
    ],
    noarchive=False,
)

_debug = bool(os.environ.get("M2C_DEBUG"))

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=("MCAP-to-CSV-debug" if _debug else "MCAP-to-CSV"),
    console=_debug,
    icon=str(ROOT / "assets" / "app.ico"),
    upx=False,
)
