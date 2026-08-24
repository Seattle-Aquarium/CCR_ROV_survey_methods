"""Construct the GUI, screenshot both themes, and close.

Not a substitute for using it, but it catches construction errors, missing
assets and theme regressions without a human in the loop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
OUT.mkdir(parents=True, exist_ok=True)

import customtkinter as ctk  # noqa: E402
from composite.gui.app import App  # noqa: E402
from composite.survey import Site, SurveyPlan, Transect  # noqa: E402


def shot(app: App, name: str) -> None:
    # ImageGrab captures the screen, so the window must actually be frontmost --
    # otherwise it silently photographs whatever is underneath.
    app.deiconify()
    app.lift()
    app.attributes("-topmost", True)
    app.focus_force()
    for _ in range(6):
        app.update_idletasks()
        app.update()
        time.sleep(0.15)
    try:
        from PIL import ImageGrab
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(OUT / name)
        print(f"wrote {name}  ({w}x{h} at {x},{y})")
    except Exception as ex:
        print("screenshot failed:", ex)
    finally:
        app.attributes("-topmost", False)


app = App()
app.geometry("1180x900+40+20")

# populate so the screenshots show a realistic state
app.flight_dir = Path(
    r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience"
    r"\flights\testing\2026_08_21_DeepSea_light_testing"
)
app.folder_entry.insert(0, str(app.flight_dir))
app._scan()
app._apply_plan(SurveyPlan([
    Site("Centennial", "HSIL", "2026-08-24", [
        Transect("T1", "13:12:00", "13:27:30"),
        Transect("T2", "13:35:00", "13:50:00"),
    ]),
]))
app.res_vars["1080p"].set(True)
app.res_vars["720p"].set(True)
app._log("Ready. Select a flight folder, add transects, then Create composites.")

shot(app, "gui_dark.png")

app.theme_switch.deselect()
app._toggle_theme()
shot(app, "gui_light.png")

print("sites:", len(app._sites))
print("plan valid:", app._plan().validate() or "OK")
app.destroy()
print("GUI smoke test complete")
