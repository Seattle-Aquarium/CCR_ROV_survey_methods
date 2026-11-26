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
    #: The DVL's address. Blank means "the one the BlueOS DVL extension
    #: reports".
    "dvl_host": "",
    #: Acoustic snapshots (echo profile and spectrum) per second under
    #: Intensive logging; Standard logging takes none.
    "dvl_snapshot_hz": 5.0,
    #: How much to log: "standard" (the vehicle is working) or "intensive"
    #: (chasing a fault). See logprofile.py.
    "logging_profile": "standard",
    #: Shelved parts of the program to bring back (shelf.py), e.g.
    #: ["flight_summary", "analyze_transects", "survey_plan"].
    "unshelve": [],
    #: BlueOS logs -> Download files: make the 1 Hz transect CSVs, sort C3
    #: imagery by transect, and build the flight report afterwards.
    "download_transect_csv": True,
    "download_c3_mode": "all",
    "download_flight_report": True,
    #: The tide station for the transect CSVs' Depth_std ("" skips it).
    "tide_station": "Elliott Bay (9447130)",
    #: Where the dividers between sections were left, as the share of the
    #: space left of or above each. None is the page's own default.
    "nav_map_share": None,
    "nav_map_share_hud": None,
    "nav_map_share_narrow": None,
    "nav_readiness_share": None,
    "nav_plan_list_share": None,
    "monitor_pair_share": None,
    "dvl_pair_share": None,
    "logs_pair_share": None,
    #: The window. `banner_open` is None until the fold button is first
    #: pressed -- folded on a small screen, open on a large one -- and then
    #: what it was left as; likewise `window_zoomed` for maximized.
    "banner_open": None,
    "window_zoomed": None,
    #: "auto" chooses from the screen: "compact" when its work area is under
    #: 860 logical pixels tall (the field laptops), else "comfortable". Set
    #: either by hand to override.
    "ui_density": "auto",
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
