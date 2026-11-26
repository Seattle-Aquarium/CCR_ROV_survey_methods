"""
Where each kind of log goes inside a flight's ``logs/`` folder.

Every recorder files into a folder of its own, so the top of ``logs/`` is a
short list rather than forty files::

    logs/
        flight_<id>.json          the flight's record: the index to the rest
        operator_marks.txt        what the operator noted, as it happened
        pi_downloads.jsonl        every download, with its SHA-256
        pi_cleanup_<when>.txt     what Clean the Pi approved, deleted and kept
        laptop/                   laptop_monitor_<id>.csv -- the topside, 1 Hz
        network/                  network_fast / _pings / _events / _trace /
                                  _topside, and the pre-dive network checks
        navigation/               nav_<id>.json / .jsonl -- the Navigation log
        dvl/  ugps/               the DVL and UGPS captures
        mcap/ tlog/ BIN/ ...      what came off the vehicle

Flight folders filed before November 2026 have the laptop, network and
navigation files loose in ``logs/``. Everything that reads them looks in both
places (`find`), so old flights read exactly as they did.
"""

from __future__ import annotations

from pathlib import Path

LAPTOP = "laptop"
NETWORK = "network"
NAVIGATION = "navigation"

#: Every subfolder a flight's own files are written into. A flight id is only
#: unique if nothing in any of them already carries it.
FLIGHT_SUBFOLDERS = (LAPTOP, NETWORK, NAVIGATION)


def laptop_dir(logs: Path) -> Path:
    return Path(logs) / LAPTOP


def network_dir(logs: Path) -> Path:
    return Path(logs) / NETWORK


def navigation_dir(logs: Path) -> Path:
    return Path(logs) / NAVIGATION


def find(logs: Path, sub: str, pattern: str) -> list[Path]:
    """Files matching `pattern` in ``logs/<sub>/`` and, for older flights,
    loose in ``logs/`` -- each once, sorted by name."""
    logs = Path(logs)
    found = {*logs.glob(pattern), *(logs / sub).glob(pattern)}
    return sorted((p for p in found if p.is_file()), key=lambda p: p.name)


def id_taken(logs: Path, candidate: str) -> bool:
    """Does any file in the logs folder or its flight subfolders already
    carry this flight id?"""
    logs = Path(logs)
    for folder in (logs, *(logs / s for s in FLIGHT_SUBFOLDERS)):
        if any(folder.glob(f"*_{candidate}.*")):
            return True
    return False
