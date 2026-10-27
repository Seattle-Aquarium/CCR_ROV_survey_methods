"""
The web demo: the program running its ROV flight simulator for visitors.

``python -m rov_flight_ops --demo`` (or ``ROV_FLIGHT_OPS_DEMO=1``) opens the
program with the simulator already on, and keeps it on. It is what the
container in ``web_demo/`` runs, on a virtual display in Linux, streamed to a
browser, so that somebody can be sent a link and click
through the tabs without a laptop, a vehicle or an install.

What demo mode changes, and nothing else:

* **The simulator is on from the first frame and cannot be switched off.** It
  is engaged before the window's loop starts, so nothing reads for a real
  vehicle first, and its switch in the banner is shown on and disabled.
* **No vehicle is looked for.** Off the demo the simulator asks the vehicle's
  address every few seconds and switches itself off if one answers. In a
  container on somebody else's network that address belongs to nobody we
  know, so it is never asked.
* **No folder can be chosen**, and every file dialog opens in the simulator's
  own folder and refuses anything outside it: nothing is written anywhere but
  the sandbox.
* **The simulated flight starts again** every `restart_every_s`, so a demo left
  running for days still shows the survey from its first lanes rather than a
  map full of track and a log full of the night.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

#: Either turns demo mode on.
FLAG = "--demo"
ENV = "ROV_FLIGHT_OPS_DEMO"
#: Minutes between restarts of the simulated flight; 0 never restarts it.
RESTART_ENV = "ROV_FLIGHT_OPS_DEMO_RESTART_MIN"
DEFAULT_RESTART_MIN = 60.0

#: What the output pane says first, so a visitor knows what they are looking at.
WELCOME = (
    "This is the web demo of ROV Flight Operations, the Seattle Aquarium's "
    "program for flying ROV surveys. Everything on screen is the program's "
    "ROV flight simulator: a vehicle flying survey lanes off Pier 59, with one "
    "thing going wrong on each live tab every 45 seconds. Press Show what's "
    "wrong in the banner to be told what it is. Anyone else on this page sees "
    "and drives the same window.")


def requested(argv: Sequence[str] | None = None,
              environ: Mapping[str, str] | None = None) -> bool:
    """Was demo mode asked for, by flag or environment?"""
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    if FLAG in argv:
        return True
    return environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def restart_every_s(environ: Mapping[str, str] | None = None) -> float:
    """Seconds between restarts of the simulated flight; 0 for never."""
    environ = os.environ if environ is None else environ
    raw = environ.get(RESTART_ENV, "").strip()
    try:
        minutes = float(raw) if raw else DEFAULT_RESTART_MIN
    except ValueError:
        minutes = DEFAULT_RESTART_MIN
    return max(0.0, minutes) * 60.0


def confine_file_dialogs(root: Path, refuse: Callable[[str], None]) -> None:
    """Open every file dialog in `root`, and refuse whatever is chosen outside it.

    Replaces Tk's three dialogs for the life of the process -- every page asks
    for them through ``filedialog.<name>`` at the moment it needs one, so this
    is the one place all of them pass through. `refuse` is told the path, and
    the page is told nothing was chosen.
    """
    from tkinter import filedialog

    root = Path(root)
    for name in ("askdirectory", "askopenfilename", "asksaveasfilename"):
        original = getattr(filedialog, name)

        def confined(*args, _original=original, **kw):
            root.mkdir(parents=True, exist_ok=True)
            kw["initialdir"] = str(root)
            chosen = _original(*args, **kw)
            if chosen and not Path(chosen).resolve().is_relative_to(root.resolve()):
                refuse(str(chosen))
                return ""
            return chosen

        setattr(filedialog, name, confined)
