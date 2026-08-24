"""
1 Hz telemetry export.

Covers the whole span the mcap recorded, not just the transects: rows outside a
transect are labelled ``off_transect`` so descents, ascents and between-transect
manoeuvring stay in the record.

Values are held forward from the last sample (these are sampled states, not
continuous signals), but only up to a per-field staleness limit -- see
telemetry.MAX_AGE. A DVL that drops out leaves blanks rather than a flat line,
which is the honest representation and stops a dead sensor looking healthy.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from .survey import ResolvedTransect, SurveyPlan, format_hhmmss
from .telemetry import (
    EXPORT_COLUMNS, EXPORT_STRINGS, TelemetryStore, dvl_beam_columns,
)

ProgressCB = Callable[[float, str], None]

BASE_COLUMNS = (
    "utc_iso", "epoch_s", "tc25_local", "date", "project", "site", "transect",
)

DERIVED_COLUMNS = ("power_W",)


@dataclass
class ExportResult:
    path: Path
    rows: int
    t_start: float
    t_end: float
    columns: list[str]
    transect_rows: int


def _label_for(epoch: float, resolved: Sequence[ResolvedTransect]):
    for r in resolved:
        if r.epoch_start <= epoch < r.epoch_end:
            return r.site.project, r.site.name, r.transect.name
    return None


def export_1hz(
    store: TelemetryStore,
    out_path: Path,
    *,
    plan: SurveyPlan | None = None,
    resolved: Sequence[ResolvedTransect] = (),
    utc_offset_hours: float = -7.0,
    t_start: float | None = None,
    t_end: float | None = None,
    progress: ProgressCB | None = None,
    cancel=None,
) -> ExportResult:
    """Write one row per second across the recorded span."""
    t0 = t_start if t_start is not None else store.t_start
    t1 = t_end if t_end is not None else store.t_end
    if t0 is None or t1 is None:
        raise ValueError("no telemetry to export")

    t0, t1 = math.floor(t0), math.ceil(t1)
    n = max(1, int(t1 - t0) + 1)

    beams = dvl_beam_columns(store)
    columns = (
        list(BASE_COLUMNS)
        + list(DERIVED_COLUMNS)
        + [name for name, _f, _s in EXPORT_COLUMNS]
        + [name for name, _f in EXPORT_STRINGS]
        + [name for name, _f in beams]
    )

    # When the flight has a single site, its project/site apply to the whole
    # record; with several, off-transect rows cannot be attributed, so they are
    # left blank rather than guessed at.
    lone = None
    if plan is not None and len(plan.sites) == 1:
        s = plan.sites[0]
        lone = (s.project, s.name)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    transect_rows = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)

        for i in range(n):
            if cancel is not None and cancel.is_set():
                from .ffmpeg_tools import CancelledError
                raise CancelledError("cancelled")
            epoch = t0 + i

            hit = _label_for(epoch, resolved)
            if hit:
                project, site, transect = hit
                transect_rows += 1
            else:
                project, site = lone if lone else ("", "")
                transect = "off_transect"

            utc = datetime.fromtimestamp(epoch, timezone.utc)
            local = utc + timedelta(hours=utc_offset_hours)

            row: list = [
                utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{epoch:.0f}",
                local.strftime("%H:%M:%S"),
                local.strftime("%Y-%m-%d"),
                project, site, transect,
            ]

            # derived power, from the same BATTERY_STATUS message
            mv = store.num("BATTERY_STATUS.voltage_mv", epoch)
            ca = store.num("BATTERY_STATUS.current_battery", epoch)
            if mv is not None and ca is not None and ca != -1:
                row.append(f"{mv / 1000.0 * ca / 100.0:.1f}")
            else:
                row.append("")

            for _name, field_, scale in EXPORT_COLUMNS:
                v = store.num(field_, epoch)
                row.append("" if v is None else f"{v * scale:.6g}")
            for _name, field_ in EXPORT_STRINGS:
                v = store.get(field_, epoch)
                row.append("" if v is None else str(v))
            for _name, field_ in beams:
                v = store.num(field_, epoch)
                row.append("" if v is None else f"{v / 100.0:.3f}")   # cm -> m

            w.writerow(row)

            if progress and i % max(1, n // 50) == 0:
                progress(i / n, f"telemetry CSV {i}/{n}")

    if progress:
        progress(1.0, f"{n} rows -> {out_path.name}")

    return ExportResult(out_path, n, float(t0), float(t1), columns, transect_rows)
