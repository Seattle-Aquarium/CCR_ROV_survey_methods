"""
The web demo's build-time check, run once on a throwaway display.

    xvfb-run -a -s "-screen 0 1500x950x24 -dpi 96" python web_demo/selfcheck.py OUT

1. Every module of the program imports here, on Linux. One that does not
   fails the build: the program was written for Windows, and this is the only
   place that finds out.
2. Every non-ASCII character in the program's source has a font to be drawn
   in, and the bundled Montserrat, the symbol face and the monospace face are
   all known to Tk.
3. The program opens in demo mode, simulating, and every tab is opened and
   photographed into OUT, with what was found written to OUT/report.txt.
   The running Space serves OUT at /selfcheck/, so the build can be seen
   without a shell in the container.

Only an import failure or a program that will not open (or opens without its
simulator) fails the build; everything else is reported.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

#: How long the window runs before the first photograph, and on each tab.
SETTLE_S = 8.0
TAB_S = 4.0


def check_imports(report: list[str]) -> bool:
    import rov_flight_ops

    failed = []
    for mod in pkgutil.walk_packages(rov_flight_ops.__path__, "rov_flight_ops."):
        try:
            importlib.import_module(mod.name)
        except Exception:
            failed.append(mod.name)
            report.append(f"IMPORT FAILED {mod.name}\n{traceback.format_exc()}")
    report.append(f"imports: {'all modules import' if not failed else failed}")
    return not failed


def check_glyphs(report: list[str]) -> None:
    chars = set()
    for path in (PROJECT / "rov_flight_ops").rglob("*.py"):
        chars.update(ch for ch in path.read_text(encoding="utf-8") if ord(ch) > 127)
    missing = []
    for ch in sorted(chars):
        found = subprocess.run(["fc-list", f":charset={ord(ch):x}", "family"],
                               capture_output=True, text=True).stdout.strip()
        if not found:
            missing.append(f"U+{ord(ch):04X} {ch}")
    report.append(f"glyphs: {len(chars)} non-ASCII characters in the source; "
                  f"no font for {missing or 'none'}")


def fontconfig_choice(name: str) -> str:
    """The file fontconfig picks for `name` (a pattern such as
    "Montserrat:weight=100") in this process -- where the program's own fonts
    are registered, which the fc-match command cannot see."""
    import ctypes
    import ctypes.util

    fc = ctypes.CDLL(ctypes.util.find_library("fontconfig") or "libfontconfig.so.1")
    fc.FcNameParse.restype = ctypes.c_void_p
    fc.FcNameParse.argtypes = [ctypes.c_char_p]
    fc.FcConfigSubstitute.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    fc.FcDefaultSubstitute.argtypes = [ctypes.c_void_p]
    fc.FcFontMatch.restype = ctypes.c_void_p
    fc.FcFontMatch.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_int)]
    fc.FcPatternGetString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_char_p)]
    pattern = fc.FcNameParse(name.encode())
    fc.FcConfigSubstitute(None, pattern, 0)              # FcMatchPattern
    fc.FcDefaultSubstitute(pattern)
    result = ctypes.c_int()
    match = fc.FcFontMatch(None, pattern, ctypes.byref(result))
    path = ctypes.c_char_p()
    if not match or fc.FcPatternGetString(match, b"file", 0, ctypes.byref(path)):
        return "nothing"
    return Path(path.value.decode()).name


def check_window(out: Path, report: list[str]) -> bool:
    import tkinter.font as tkfont

    from PIL import ImageGrab

    from rov_flight_ops import brand, diagnostics
    from rov_flight_ops.gui import theme as T
    from rov_flight_ops.gui.app import App

    diagnostics.setup()
    report.append(f"bundled fonts registered: {sorted(brand._registered)}")
    app = App(demo=True)
    errors = []

    def caught(exc, val, tb):
        errors.append("".join(traceback.format_exception(exc, val, tb)))

    app.report_callback_exception = caught
    # Tk lists only a font's first family name. The bundled faces are all
    # "Montserrat" first and "Montserrat Medium" and so on second, which
    # fontconfig still matches -- so a name Tk does not list is looked up there.
    families = set(tkfont.families(app))
    for name in (T.FAMILY, T.FAMILY_MEDIUM, T.FAMILY_SEMIBOLD, T.SYMBOL, T.MONO):
        if name in families:
            found = "known to Tk"
        else:
            try:
                found = f"not listed by Tk; fontconfig draws it in {fontconfig_choice(name)}"
            except Exception as ex:
                found = f"MISSING ({ex})"
        report.append(f"font {name!r}: {found}")
    # What Tk asks for: weight 100 ("medium") for ordinary text, 200 for bold.
    for name, weight in ((T.FAMILY, 100), (T.FAMILY, 200), (T.FAMILY_MEDIUM, 100),
                         (T.FAMILY_SEMIBOLD, 100)):
        try:
            got = fontconfig_choice(f"{name}:weight={weight}")
        except Exception as ex:
            got = f"not known ({ex})"
        report.append(f"fontconfig: {name} at weight {weight} draws in {got}")

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.update()
            time.sleep(0.02)

    pump(SETTLE_S)
    ok = app.simulating
    report.append(f"simulating: {app.simulating}; switch on: "
                  f"{bool(app.sim_controls.switch.get())}, "
                  f"{app.sim_controls.switch.cget('state')}; window "
                  f"{app.winfo_width()}x{app.winfo_height()} on a "
                  f"{app.winfo_screenwidth()}x{app.winfo_screenheight()} display")
    for n, tab in enumerate(app.nav.sections, start=1):
        app.nav.select(tab)
        pump(TAB_S)
        name = f"{n}_{app.nav.key_of(tab)}.png"
        try:
            ImageGrab.grab().save(out / name)
            report.append(f"tab {tab}: {name}")
        except Exception as ex:
            report.append(f"tab {tab}: no photograph ({ex})")
    app.nav.select(app.nav.sections[0])
    pump(1.0)
    report.append(f"callback errors: {len(errors)}")
    report.extend(errors)
    log = diagnostics.folder()
    if log is not None and (log / diagnostics.LOG_NAME).is_file():
        lines = (log / diagnostics.LOG_NAME).read_text(encoding="utf-8").splitlines()
        report.append("diagnostics log, warnings and worse:")
        report.extend(ln for ln in lines if " WARNING " in ln or " ERROR " in ln
                      or " CRITICAL " in ln)
    return ok


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "selfcheck")
    out.mkdir(parents=True, exist_ok=True)
    report = [f"built {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
              f"python {sys.version.split()[0]} on {sys.platform}"]
    ok = check_imports(report)
    if ok:
        check_glyphs(report)
        try:
            ok = check_window(out, report)
        except Exception:
            report.append(f"THE WINDOW DID NOT OPEN\n{traceback.format_exc()}")
            ok = False
    text = "\n".join(report) + "\n"
    (out / "report.txt").write_text(text, encoding="utf-8")
    print(text, flush=True)
    # Not a clean exit: the simulator's readers are still running, and there
    # is no vehicle to hand back to.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
