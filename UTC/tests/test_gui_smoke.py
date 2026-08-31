"""GUI smoke test: build every page and check the layout programmatically.

Deliberately no screenshots. ImageGrab captures a screen *region*, so if the
app is not frontmost at the moment of the grab it silently photographs whatever
is -- which on a real desktop can be someone's private browser tab. Widget
geometry answers the same questions (is anything clipped, is anything
zero-sized, did every page build) without ever reading the screen.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
OUT.mkdir(parents=True, exist_ok=True)

import customtkinter as ctk  # noqa: E402

from utc.gui.app import App  # noqa: E402
from utc.survey import Site, SurveyPlan, Transect  # noqa: E402


def check_layout(app: App) -> list[str]:
    """Every page built, nothing zero-sized, nothing wider than the window.

    Replaces the old screenshot: it answers the questions a screenshot was
    being used for, without capturing the screen.
    """
    # Let the window actually lay out first. Measuring an unrealised window
    # returns placeholder sizes and every check fails -- a test that always
    # reports problems is one nobody reads.
    app.geometry("1300x940")
    for _ in range(3):
        app.update_idletasks()
        app.update()
        time.sleep(0.15)
    problems: list[str] = []
    win_w = app.winfo_width()
    if win_w <= 1 or app.nav.rail.winfo_height() <= 100:
        return ["window never realised; layout could not be checked"]
    for name in list(app.pages) + ["Flight setup"]:
        app.nav.select(name)
        app.update_idletasks()
        page = app.nav._pages[name]
        w, h = page.winfo_width(), page.winfo_height()
        if w <= 1 or h <= 1:
            problems.append(f"{name}: page is {w}x{h}")
        if w > win_w:
            problems.append(f"{name}: page {w}px wider than the {win_w}px window")
    # every rail row must be visible inside the rail
    rail_h = app.nav.rail.winfo_height()
    for label, (holder, _stripe, _btn) in app.nav._rows.items():
        y, h = holder.winfo_y(), holder.winfo_height()
        if y + h > rail_h:
            problems.append(f"rail row {label!r} runs past the rail "
                            f"({y + h} > {rail_h})")
        if h <= 1:
            problems.append(f"rail row {label!r} collapsed to {h}px")
    return problems



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
app.pages["Video"].res_vars["1080p"].set(True)
app.pages["Video"].res_vars["720p"].set(True)
app._log("Ready. Select a flight folder, add transects, then Create composites.")

app.theme_switch.deselect()
app._toggle_theme()

print("sites:", len(app._sites))
print("plan valid:", app._plan().validate() or "OK")
problems = check_layout(app)
for pr in problems:
    print("  LAYOUT:", pr)
print("layout OK" if not problems else f"{len(problems)} layout problem(s)")

app.destroy()
print("GUI smoke test complete")

