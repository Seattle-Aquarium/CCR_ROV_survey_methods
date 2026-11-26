"""
A flight's telemetry, read once and cached.

The four functions here are the part of UTC's ``pipeline`` module that this
program actually uses: where a flight's cache lives, which telemetry it should
be read from, reading it, and turning the survey plan into time windows. The
rest of ``pipeline`` is the video compositor, which belongs to ROV Imagery
Processing -- carrying it here would mean carrying the overlay renderer, the
photo sorter and ffmpeg orchestration to draw one dive profile.

The bodies are unchanged from UTC, so a cache built by either program reads
the same way.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path

from . import binlog, discovery, mcap_extract
from .config import AppConfig
from .survey import SurveyPlan
from .survey import plan_windows as _plan_windows
from .telemetry import TelemetryStore

ProgressCB = Callable[[float, str], None]


def cache_dir_for(flight_dir: Path, root: Path) -> Path:
    """A stable per-flight cache path.

    Keyed by name plus a hash of the full path, so two flights that happen to
    share a folder name (``2026`` under different projects) do not collide.
    """
    h = hashlib.sha1(str(Path(flight_dir).resolve()).encode("utf-8")).hexdigest()[:8]
    return Path(root) / f"{Path(flight_dir).name}_{h}"


def telemetry_csv_for(flight_dir: Path, cache_root: Path) -> tuple[Path | None, str]:
    """The telemetry a flight should be read from, and where it came from.

    One place decides this, because the answer is not "the cache's
    telemetry.csv": a flight whose mcap failed can be pointed at the
    autopilot's dataflash log instead, and every caller has to honor that.
    """
    cache = cache_dir_for(Path(flight_dir), cache_root)
    over = binlog.override_active(cache)
    if over:
        return Path(over["csv"]), f"{Path(over['source']).name} (autopilot log)"
    csv = cache / "telemetry.csv"
    if csv.is_file():
        return csv, "mcap"
    from .tlog_extract import TLOG_CSV, TLOG_MARKER
    tl = cache / TLOG_CSV
    return (tl, "tlog") if tl.is_file() and (cache / TLOG_MARKER).is_file() else (None, "none")


def ensure_telemetry(
    flight_dir: Path,
    app: AppConfig | None = None,
    *,
    windows: Sequence[tuple[float, float]] | None = None,
    progress: ProgressCB | None = None,
    force: bool = False,
    cancel=None,
) -> tuple[TelemetryStore, list[str]]:
    """Telemetry for a flight, reading its recordings only if the cache is cold.

    The recordings are the flight's mcaps (BlueOS 1.5) or, failing those, its
    tlogs (BlueOS 1.4 and earlier, whose router writes a .tlog instead).
    Returns (store, warnings). Raises FileNotFoundError if there is neither,
    and `mcap_extract.ExtractionCancelled` if `cancel` is set part way.
    """
    app = app or AppConfig()

    # A flight whose telemetry was rebuilt from the autopilot's own dataflash
    # log reads that instead. Checked before the mcap is even looked for,
    # because the reason for choosing BIN is usually that the mcap is the
    # thing that failed.
    cache = cache_dir_for(flight_dir, app.cache_root)
    over = binlog.override_active(cache)
    if over:
        note = (f"telemetry is coming from {Path(over['source']).name} "
                f"(the autopilot's own log), not the mcap")
        if over.get("depth_agreement") is not None:
            note += f"; clock aligned to r={over['depth_agreement']:.4f}"
        return TelemetryStore.load(over["csv"]), [note]

    disc = discovery.discover(flight_dir)
    if not disc.mcaps and disc.tlogs:
        return _ensure_from_tlogs(disc.tlogs, cache, windows, progress, force, cancel)
    if not disc.mcaps:
        raise FileNotFoundError(
            f"No .mcap or .tlog telemetry found in {flight_dir}. Download the "
            f"recordings on BlueOS logs (mcap on BlueOS 1.5, tlog on 1.4), or "
            f"put them in the flight's logs/mcap or logs/tlog folder."
        )

    # Narrow to the recordings that actually cover the work before insisting
    # anything is downloaded. A day of testing leaves a folder full of them,
    # and requiring the lot means waiting on gigabytes that will never be read.
    warnings: list[str] = []
    chosen = list(disc.mcaps)
    if windows:
        chosen, _skipped, warns = mcap_extract.select_for_windows(chosen, windows)
        warnings.extend(warns)
        if not chosen:
            raise FileNotFoundError(
                "None of the recordings in logs/ overlap the transect times. "
                "Check the times, or that the right mcap was copied over."
            )

    blocked = discovery.check_local(chosen)
    if blocked:
        raise RuntimeError("\n".join(blocked))

    ex = mcap_extract.extract(chosen, cache, progress=progress, force=force,
                              cancel=cancel)
    return TelemetryStore.load(ex.telemetry_csv), warnings + list(ex.warnings)


def _ensure_from_tlogs(tlogs, cache: Path, windows, progress, force, cancel
                       ) -> tuple[TelemetryStore, list[str]]:
    """The tlog half of `ensure_telemetry`: the same narrowing, the same cache
    discipline, the same long-format CSV."""
    from . import tlog_extract

    warnings: list[str] = []
    chosen = list(tlogs)
    if windows:
        chosen, _skipped, warns = tlog_extract.select_for_windows(chosen, windows)
        warnings.extend(warns)
        if not chosen:
            raise FileNotFoundError(
                "None of the tlogs in logs/tlog overlap the transect times. "
                "Check the times, or that the right tlog was downloaded.")
    blocked = discovery.check_local(chosen)
    if blocked:
        raise RuntimeError("\n".join(blocked))
    ex = tlog_extract.extract(chosen, cache, progress=progress, force=force,
                              cancel=cancel)
    return (TelemetryStore.load(ex.telemetry_csv),
            warnings + ["read from the tlog(s): "
                        + ", ".join(p.name for p in ex.tlogs)] + list(ex.warnings))


def plan_windows(plan: SurveyPlan, *, exclude_pauses: bool = False
                 ) -> list[tuple[str, float, float]]:
    """(name, epoch_start, epoch_end) for every transect in a plan.

    Re-exported from `survey` so the two programs turn a plan into windows
    with one piece of code rather than two that can drift.
    """
    return _plan_windows(plan, exclude_pauses=exclude_pauses)
