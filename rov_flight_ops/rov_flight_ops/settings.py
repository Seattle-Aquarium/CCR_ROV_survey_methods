"""
The few things worth remembering between runs.

Kept outside the repository and outside Dropbox, in the same per-user place the
environment lives, so one laptop's vehicle address or C3 folder never follows
the code onto another laptop. Every read and write is best-effort: a settings
file that cannot be read is a program that starts with its defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    #: Blank means the tether address, 192.168.2.2.
    "vehicle_host": "",
    #: The folder Madrona saves C3 imagery under, as BlueOS's File Browser
    #: addresses it. Blank means "search the vehicle for it".
    "c3_folder": "",
}


def path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CCR_ROV" / "rov_flight_ops" / "settings.json"


def load() -> dict:
    out = dict(DEFAULTS)
    try:
        out.update(json.loads(path().read_text(encoding="utf-8")))
    except Exception:
        pass
    return out


def save(values: dict) -> None:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**DEFAULTS, **values}, indent=2), encoding="utf-8")
    except Exception:
        pass
