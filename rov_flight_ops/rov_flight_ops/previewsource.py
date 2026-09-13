"""
Where the transect preview gets its depth trace.

The preview is a gut check taken with the ROV back on deck: were the transects
flown, and did the recording catch them? It has to work whether or not the
logs have been downloaded yet, so it looks in order:

1. **The flight folder's recordings** (logs/mcap, or logs) -- the same
   telemetry every other tool reads, including an autopilot log someone chose
   on Flight summary.
2. **An autopilot log in the flight folder** (logs/BIN), when no recording is --
   one downloaded by the BlueOS logs tab, which keeps the vehicle's file time.
3. **The autopilot's log on the vehicle.** A recording on the Pi carries its
   video inside it -- gigabytes, where the depth trace needs a few megabytes --
   so the vehicle's own dataflash log is read instead: tens of megabytes, a few
   seconds over the tether, into the cache rather than the flight folder.

A dataflash log has no wall clock on a vehicle without GPS, only the time
since the autopilot booted. It is placed by its **file modification time**,
which is when it last grew -- the end of the flight. That is good to a second
or two on a closed log, which is plenty for "did the dive happen where I
think", and the preview says so when it has used it. It is *not* used to cut
imagery or CSVs; those still come from the recordings.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from pathlib import Path

from . import binlog, pifiles
from .config import AppConfig
from .telemetry import TelemetryStore
from .telemetry_cache import cache_dir_for, ensure_telemetry

ProgressCB = Callable[[float, str], None]

AUTO, FOLDER, VEHICLE = "auto", "folder", "vehicle"


def local_bin_times(path: Path) -> tuple[float, float, float] | None:
    """(first boot seconds, last boot seconds, file time) of a log on disk."""
    path = Path(path)
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
            fh.seek(max(0, size - 64 * 1024))
            tail = fh.read()
    except OSError:
        return None
    fmts = pifiles.dataflash_formats(head)
    first = pifiles.dataflash_times(head, fmts)
    k = tail.find(pifiles._DF_HEAD)
    last = pifiles.dataflash_times(tail[k:] if k >= 0 else b"", fmts)
    if not first or not last:
        return None
    lo, hi = min(first) / 1e6, max(last) / 1e6
    if not 0 <= hi - lo < 86400:
        return None
    return lo, hi, mtime


def local_bin_span(path: Path) -> tuple[float | None, float | None]:
    """(start, end) of a log on disk, from its head, tail and file time."""
    t = local_bin_times(path)
    if t is None:
        return None, None
    lo, hi, mtime = t
    return mtime - (hi - lo), mtime


def _overlaps(span, windows, margin=pifiles.MARGIN_S) -> bool:
    a, b = span
    if a is None or b is None:
        return False
    return any(a <= hi + margin and b >= lo - margin for _n, lo, hi in windows)


def store_from_bins(paths: Sequence[Path], cache: Path, *,
                    progress: ProgressCB | None = None,
                    cancel=None) -> TelemetryStore:
    """One telemetry store from several logs, each placed by its file time."""
    cache.mkdir(parents=True, exist_ok=True)
    merged = cache / "preview_from_bin.csv"
    parts = []
    for n, p in enumerate(paths):
        t = local_bin_times(p)
        if t is None:
            continue
        _boot_first, boot_last, mtime = t
        # wall = TimeUS/1e6 + offset, and the log's last stamp is its file time.
        al = binlog.BinAlignment(offset=mtime - boot_last, method="file time",
                                 note="placed by the log's modification time")
        out = cache / f"preview_{Path(p).stem}.csv"
        binlog.write_telemetry_csv(
            p, al, out, cancel=cancel,
            progress=(lambda f, m="", n=n: progress((n + f) / len(paths), m))
            if progress else None)
        parts.append(out)
    if not parts:
        raise FileNotFoundError("none of the autopilot logs could be placed on "
                                "the clock")
    with open(merged, "w", newline="", encoding="utf-8") as dst:
        w = csv.writer(dst)
        w.writerow(["t", "field", "value", "sval"])
        for part in parts:
            with open(part, newline="", encoding="utf-8") as src:
                r = csv.reader(src)
                next(r, None)
                w.writerows(r)
    return TelemetryStore.load(merged)


def preview_store(flight_dir: Path | None, windows: Sequence[tuple[str, float, float]],
                  *, cfg: AppConfig | None = None, host: str | None = None,
                  source: str = AUTO, progress: ProgressCB | None = None,
                  cancel=None) -> tuple[TelemetryStore, list[str], str]:
    """(store, notes, where it came from) for the dive profile."""
    cfg = cfg or AppConfig()
    notes: list[str] = []
    plain = [(a, b) for _n, a, b in windows]

    if source in (AUTO, FOLDER) and flight_dir:
        try:
            store, warns = ensure_telemetry(Path(flight_dir), cfg, windows=plain,
                                            progress=progress)
            return store, list(warns), "the flight folder's recordings"
        except FileNotFoundError as ex:
            notes.append(str(ex))

        # Only logs/BIN, where the BlueOS logs tab puts them with the vehicle's
        # own modification time preserved. A log copied in by hand carries the
        # time it was copied -- measured on the 2 September flight, hours from
        # when it was recorded -- and would put the trace in the wrong place.
        bins = [p for p in binlog.list_bins(Path(flight_dir) / "logs" / "BIN")
                if _overlaps(local_bin_span(p), windows)]
        if bins:
            cache = cache_dir_for(Path(flight_dir), cfg.cache_root) / "preview"
            store = store_from_bins(bins, cache, progress=progress, cancel=cancel)
            return store, notes + [
                "placed on the clock by each log's file time -- close, not exact"
            ], f"the autopilot log(s) in the flight folder ({', '.join(b.name for b in bins)})"
        if source == FOLDER:
            raise FileNotFoundError(
                "Nothing in the flight folder covers the transects -- no "
                "recording in logs/mcap and no autopilot log in logs/BIN.")

    if source == FOLDER and not flight_dir:
        raise FileNotFoundError("Choose a flight folder on Monitoring first.")

    # The vehicle.
    if progress:
        progress(0.02, "asking the vehicle for its autopilot logs")
    inv = pifiles.search(host, ["bin"], progress=(
        lambda f, m="": progress(f * 0.3, m)) if progress else None, cancel=cancel)
    pifiles.match(inv.all_files(), windows)
    wanted = [f for f in inv.files.get("bin", []) if f.covers]
    if not wanted:
        raise FileNotFoundError(
            f"No autopilot log on {inv.vehicle or inv.host} covers the transect "
            f"times. Check the times and the date, and that the vehicle's clock "
            f"was right.")
    base = (cache_dir_for(Path(flight_dir), cfg.cache_root) if flight_dir
            else Path(cfg.cache_root) / "no_flight") / "vehicle_preview"
    rep = pifiles.download(wanted, base, inv.host, inv.token, cancel=cancel,
                           progress=(lambda f, m="": progress(0.3 + f * 0.4, m))
                           if progress else None)
    if rep.failed:
        raise RuntimeError("Could not read the autopilot log from the vehicle: "
                           + "; ".join(why for _f, why in rep.failed))
    paths = [f.dest_in(base) for f in wanted]
    store = store_from_bins(paths, base, cancel=cancel, progress=(
        lambda f, m="": progress(0.7 + f * 0.3, m)) if progress else None)
    if inv.skew is not None and abs(inv.skew) > 120:
        notes.append(f"the vehicle's clock is {inv.skew / 60:+.0f} min off this "
                     f"laptop, so the trace may sit that far from the transects")
    notes.append("read from the vehicle's autopilot log and placed on the clock "
                 "by its file time -- close, not exact")
    return store, notes, (f"{inv.vehicle or inv.host}'s autopilot log "
                          f"({', '.join(f.name for f in wanted)})")
