"""
The whole run, in one place: mcap files in, transect CSVs and a map out.

Kept free of tkinter so the same code path serves the GUI, the command line, and
the tests. Progress and log messages are handed back through callbacks.

The tide lookup is the only step that needs the internet, and a field laptop
often has none. It is therefore non-fatal: ``Depth_std`` is left blank, the run
is reported as partial, and the transects are still written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

from . import mapping
from .mcap_read import ReadResult, read_mcaps
from .tide import add_empty_tide, fetch_tide_dataframe, merge_tide
from .transect import (
    TransectResult, export_transect, georeference_dvl, whole_log_window,
)

log = logging.getLogger(__name__)

ProgressCB = Callable[[float, str], None]
LogCB = Callable[[str], None]


@dataclass
class TransectSpec:
    transect_id: str
    windows: list[tuple[str, str]]


@dataclass
class RunResult:
    transects_folder: Path
    read: ReadResult
    results: list[TransectResult] = field(default_factory=list)
    map_path: Path | None = None
    tide_ok: bool = False
    tide_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def saved(self) -> list[TransectResult]:
        return [r for r in self.results if r.path]

    @property
    def skipped(self) -> list[TransectResult]:
        return [r for r in self.results if not r.path]

    def summary_lines(self) -> list[str]:
        lines = [
            f"Saved {len(self.saved)} of {len(self.results)} transect CSV(s) to:",
            str(self.transects_folder),
            "",
        ]
        for r in self.results:
            if r.path:
                lines.append(f"{r.transect_id} ({r.window_desc}) -> {r.path.name}")
            else:
                lines.append(f"{r.transect_id} ({r.window_desc}): SKIPPED (no data)")
        if self.map_path:
            lines += ["", f"Map: {self.map_path.name}"]
        if not self.tide_ok:
            lines += ["", f"Depth_std is blank -- no NOAA tide data ({self.tide_error})."]
        extra = self.warnings + self.read.warnings
        if extra:
            lines += ["", "Notes:"] + [f"  - {w}" for w in extra[:12]]
            if len(extra) > 12:
                lines.append(f"  ... and {len(extra) - 12} more (see the console).")
        return lines


def run(
    mcap_paths: Sequence[Path | str],
    *,
    site_name: str,
    survey_date: str,
    station_id: str | None,
    save_location: Path | str,
    transects: Sequence[TransectSpec],
    make_map: bool = True,
    progress: ProgressCB | None = None,
    on_log: LogCB | None = None,
) -> RunResult:
    """Read the recordings, cut them into transects, write CSVs and a map."""
    def say(msg: str) -> None:
        log.info(msg)
        if on_log:
            on_log(msg)

    def step(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    transects_folder = Path(save_location) / "transects"
    transects_folder.mkdir(parents=True, exist_ok=True)
    say(f"Saving outputs to: {transects_folder}")

    # ---- 1. read the recordings ------------------------------------------
    say(f"Reading {len(mcap_paths)} .mcap file(s)...")
    read = read_mcaps(mcap_paths, progress=lambda f, m: step(0.05 + 0.6 * f, m))
    df_all = read.df
    say(f"Parsed {len(df_all):,} seconds "
        f"({df_all['Date'].iloc[0]} {df_all['Time'].iloc[0]} - {df_all['Time'].iloc[-1]} local)")
    say(f"DVL track source: {read.dvl_source}")
    if read.depth_sources:
        say("Depth sources: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(read.depth_sources.items(),
                                           key=lambda kv: -kv[1])))
    for w in read.warnings:
        say(f"  ! {w}")

    result = RunResult(transects_folder=transects_folder, read=read)

    # ---- 2. tide ----------------------------------------------------------
    step(0.7, "fetching NOAA tide data")
    if station_id:
        try:
            say("Fetching NOAA tide data...")
            df_all = merge_tide(df_all, fetch_tide_dataframe(survey_date, station_id))
            result.tide_ok = True
        except Exception as ex:
            result.tide_error = f"{type(ex).__name__}: {ex}".splitlines()[0][:160]
            say(f"  ! NOAA tide lookup failed: {result.tide_error}")
            say("    Continuing; Depth_std will be blank.")
            df_all = add_empty_tide(df_all)
    else:
        result.tide_error = "no station selected"
        df_all = add_empty_tide(df_all)

    # ---- 3. one georeferenced track for the whole dive --------------------
    #
    # Seeding each transect separately puts them all at the same coordinate
    # whenever the surface fix does not move -- which is the normal case, since
    # a USBL that has not locked reports one static position for the entire
    # dive. The transects then pile up on one point on the map and their real
    # separation is lost, even though the DVL knew it all along: the local frame
    # runs continuously between transects, so the distance from the end of one
    # to the start of the next is measured, not guessed.
    #
    # So the track is propagated once, across the dive, and each transect keeps
    # its slice. DVLx/DVLy are still re-zeroed per transect afterwards, so those
    # columns mean exactly what they did in the tlog workflow.
    step(0.74, "building the dive track")
    site_frame = False
    try:
        df_all, _steps, seed_warning = georeference_dvl(df_all)
        if seed_warning:
            say(f"  ! {seed_warning}")
            result.warnings.append(seed_warning)
        else:
            site_frame = True
    except Exception as ex:
        result.warnings.append(f"dive-wide track failed ({ex}); "
                               "each transect will be seeded on its own")
        say(f"  ! {result.warnings[-1]}")

    # ---- 4. transects -----------------------------------------------------
    specs = list(transects)
    if not specs:
        start, end = whole_log_window(df_all)
        specs = [TransectSpec(f"{survey_date}_T1", [(start, end)])]
        say(f"No transect windows given; treating the whole log as one transect "
            f"({start}-{end}).")

    for i, spec in enumerate(specs, start=1):
        step(0.75 + 0.2 * (i - 1) / max(1, len(specs)),
             f"transect {i}/{len(specs)}: {spec.transect_id}")
        r = export_transect(
            df_all, spec.windows, i, spec.transect_id, site_name,
            transects_folder, dvl_source=read.dvl_source, site_frame=site_frame,
        )
        say(r.message)
        for w in r.warnings:
            say(f"  ! {w}")
        result.results.append(r)

    # ---- 5. map -----------------------------------------------------------
    if make_map and result.saved:
        step(0.96, "building the transect map")
        pairs: list[tuple[str, pd.DataFrame]] = []
        for r in result.saved:
            assert r.path is not None
            try:
                pairs.append((r.transect_id, pd.read_csv(r.path)))
            except Exception as ex:
                result.warnings.append(f"map: could not re-read {r.path.name}: {ex}")
        if pairs:
            try:
                path, warns = mapping.write_map(
                    pairs, transects_folder / "transect_map.html",
                    site_name=site_name, survey_date=survey_date,
                )
                result.map_path = path
                result.warnings.extend(warns)
                say(f"Map written: {path}")
            except Exception as ex:
                result.warnings.append(f"map could not be built: {ex}")
                say(f"  ! map could not be built: {ex}")

    step(1.0, "done")
    say("Done.")
    return result
