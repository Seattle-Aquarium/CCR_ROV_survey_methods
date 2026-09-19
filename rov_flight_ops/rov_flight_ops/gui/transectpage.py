"""
Transect CSVs, as a step in the flight.

The extractor itself lives in the sibling ``mcap_to_csv`` package and has its
own standalone window. This page is the same tool driven from the flight that
is already open: the folder, the mcaps and the survey plan all come from
the Transects tab, so the windows are typed once and both the CSVs and
the video overlays are cut from the same numbers. Two copies of those times
drifting apart is exactly the kind of error nobody notices until the analysis
disagrees with the footage.

The extractor is imported lazily. It is a separate install, and a missing one
should produce a sentence explaining what to run rather than stopping the whole
application from starting.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import binlog
from . import theme as T
from .shell import JobStopped
from .widgets import Card, button, entry, label, output_box

APP_NAME = "ROV Flight Operations"

#: What to tell someone whose environment predates this page.
_MISSING = (
    "The transect extractor is not installed in this environment.\n\n"
    "Close this program and run run_rov_flight_ops.bat again — it installs "
    "the extractor alongside it. Or install it by hand:\n\n"
    "    python -m pip install -e ../mcap_to_csv"
)


def _extractor():
    """Import the extractor, or return None."""
    try:
        from ccr_m2c import pipeline, tide
        return pipeline, tide
    except ImportError:
        return None


class TransectPage(ctk.CTkFrame):
    def __init__(self, master, app, scroll: bool = True):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Built straight into this frame when a tab stacks several pages in
        # one scrolling body; a scroller inside a scroller cannot size itself.
        if scroll:
            body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
            body.grid(row=0, column=0, sticky="nsew")
        else:
            body = self
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. what will be read ------------------------------------
        c1 = Card(body, "1.  Recordings",
                  "The .mcap files found in this flight. Several are normal — "
                  "BlueOS starts a new one every time recording restarts, and "
                  "they are read as one continuous dive.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.found = label(c1.body, "No flight folder selected yet.", muted=True)
        self.found.grid(row=0, column=0, sticky="w")

        # ---- 2. what will be cut -------------------------------------
        c2 = Card(body, "2.  Transects",
                  "Taken from the survey plan on the Transects tab. Edit them there "
                  "and they change here too — and in the video overlays.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.plan_summary = label(c2.body, "", muted=True)
        self.plan_summary.grid(row=0, column=0, sticky="w")

        # ---- 3. options ----------------------------------------------
        c3 = Card(body, "3.  Options",
                  "Depth_std puts every transect on the MLLW datum so dives at "
                  "different tide stages compare. It needs the internet; "
                  "without it the column is left blank and everything else "
                  "still runs.")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(1, weight=1)

        label(c3.body, "Tide station").grid(row=0, column=0, sticky="w", pady=4)
        pipeline_tide = _extractor()
        stations = [s[0] for s in pipeline_tide[1].STATIONS] if pipeline_tide else ["—"]
        self.station = ctk.CTkOptionMenu(
            c3.body, values=stations + ["Skip the tide lookup"], width=280,
            font=T.FONT_BODY, fg_color=T.FIELD_BG, button_color=T.SURFACE_ALT,
            text_color=T.TEXT, dropdown_font=T.FONT_BODY)
        self.station.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=4)

        label(c3.body, "Save to").grid(row=1, column=0, sticky="w", pady=4)
        self.out_entry = entry(c3.body, "<flight>/transects", width=520)
        self.out_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)

        self.make_map = ctk.CTkCheckBox(
            c3.body, text="Also build a Leaflet map of these transects",
            font=T.FONT_BODY, text_color=T.TEXT)
        self.make_map.select()
        self.make_map.grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(8, 4))

        # ---- 4. run ---------------------------------------------------
        c4 = Card(body, "4.  Extract",
                  "One CSV per transect, plus a map of the site. Existing files "
                  "are replaced; anything open in Excel is written alongside "
                  "instead of being lost.")
        c4.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        row = ctk.CTkFrame(c4.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="w")
        self.run_btn = button(row, "Extract transect CSVs", self._run,
                              "primary", width=200)
        self.run_btn.pack(side="left")
        button(row, "Open folder", self._open_out, "ghost", width=130
               ).pack(side="left", padx=8)
        self.note = label(c4.body, "", muted=True)
        self.note.grid(row=1, column=0, sticky="w", pady=(8, 0))

        # ---- 5. sensor health -----------------------------------------
        c5 = Card(body, "5.  Sensor health",
                  "Where each column's numbers came from, and whether the "
                  "instruments behind them behaved.")
        c5.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        c5.body.grid_columnconfigure(0, weight=1)

        hrow = ctk.CTkFrame(c5.body, fg_color="transparent")
        hrow.grid(row=0, column=0, sticky="w")
        self.health_btn = button(hrow, "Check sensors", self._check_health,
                                 "primary", width=150)
        self.health_btn.pack(side="left")
        self.health_note = label(hrow, "Reads the recordings; takes a moment.",
                                 muted=True)
        self.health_note.pack(side="left", padx=10)

        # Its own box rather than the shared footer log: the report is 40-odd
        # lines meant to be read together, and the footer scrolls away under
        # whatever runs next.
        self.health_box = output_box(c5.body, wrap="none", muted=False)
        self.health_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        c5.add_grip(self.health_box, self.health_box.min_height)
        self._set_health("Not checked yet.")

        self.refresh()

    # ------------------------------------------------------------------

    def _set_health(self, text: str) -> None:
        self.health_box.configure(state="normal")
        self.health_box.delete("1.0", "end")
        self.health_box.insert("1.0", text)
        self.health_box.configure(state="disabled")

    def _check_health(self) -> None:
        mod = _extractor()
        if mod is None:
            messagebox.showerror(APP_NAME, _MISSING)
            return
        disc = getattr(self.app, "discovery", None)
        mcaps = list(disc.mcaps) if disc else []
        if not mcaps:
            messagebox.showinfo(APP_NAME, "Select a flight folder with .mcap "
                                          "recordings first.")
            return

        from ccr_m2c.health import read_health

        def work(progress, cancel):
            return read_health(mcaps, progress=progress)

        def done(report) -> None:
            # Back on the main thread, so the textbox can be touched safely.
            self.health_btn.configure(state="normal")
            if isinstance(report, Exception) or report is None:
                self._set_health(f"Could not read the recordings: {report}")
                return
            self._set_health("\n".join(report.lines()))
            n = len(report.concerns())
            self.health_note.configure(
                text="Nothing of concern." if not n
                else f"{n} thing(s) worth looking at — see the end of the report.")

        self._set_health("Reading...")
        self.health_btn.configure(state="disabled")
        if not self.app.submit(work, "Checking sensor health", on_done=done):
            self.health_btn.configure(state="normal")

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the flight and plan. Called whenever this page is shown."""
        disc = getattr(self.app, "discovery", None)
        mcaps = list(disc.mcaps) if disc else []

        if not self.app.flight_dir:
            self.found.configure(text="No flight folder selected yet — "
                                      "choose one on Monitoring.")
        elif not mcaps:
            self.found.configure(
                text="No .mcap files in this flight. They normally sit in a "
                     "logs/ folder beside the video.")
        else:
            total_mb = sum(m.stat().st_size for m in mcaps) / 1e6
            names = "\n".join(f"    {m.name}" for m in mcaps[:6])
            more = f"\n    … and {len(mcaps) - 6} more" if len(mcaps) > 6 else ""
            self.found.configure(
                text=f"{len(mcaps)} file(s), {total_mb:,.0f} MB\n{names}{more}")

        try:
            plan = self.app._plan()
        except Exception:
            plan = None

        if not plan or not plan.sites:
            self.plan_summary.configure(text="No sites defined yet.")
        else:
            lines = []
            for s in plan.sites:
                names = ", ".join(t.name for t in s.transects) or "no transects"
                lines.append(f"    {s.name} ({s.date}) — {names}")
            self.plan_summary.configure(text="\n".join(lines))

        # Follow the flight folder unless someone has typed a destination of
        # their own. Filling the box only when empty used to leave the
        # previous flight's folder in it after switching flights.
        typed = self.out_entry.get().strip()
        auto = getattr(self, "_auto_out", "")
        if self.app.flight_dir and (not typed or typed == auto):
            self._auto_out = str(Path(self.app.flight_dir))
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, self._auto_out)

    # ------------------------------------------------------------------

    def _out_dir(self) -> Path | None:
        text = self.out_entry.get().strip()
        if text:
            return Path(text)
        return Path(self.app.flight_dir) if self.app.flight_dir else None

    def _open_out(self) -> None:
        out = self._out_dir()
        if out and (out / "transects").is_dir():
            self.app._reveal(out / "transects")
        elif out and out.is_dir():
            self.app._reveal(out)
        else:
            messagebox.showinfo(APP_NAME, "Nothing has been written yet.")

    def _origin_fallback(self) -> "binlog.OriginFix | None":
        """The dive's own ORIGIN_LAT/ORIGIN_LON, for when nothing else has one.

        Read from whichever .BIN under this flight folder has it, newest
        first -- one origin for the whole run, on the assumption behind this
        page's own folder layout: one flight folder is one site, flown on one
        boot of the vehicle. A day that changes ORIGIN_LAT/ORIGIN_LON between
        sites *without* rebooting, inside one flight folder, is not something
        this looks for.
        """
        if not self.app.flight_dir:
            return None
        try:
            bins = binlog.list_bins(Path(self.app.flight_dir))
        except Exception:
            return None
        for path in reversed(bins):
            try:
                fix = binlog.read_origin_params(path)
            except Exception:
                continue
            if fix:
                return fix
        return None

    def _run(self) -> None:
        mod = _extractor()
        if mod is None:
            messagebox.showerror(APP_NAME, _MISSING)
            return
        pipeline, tide = mod

        if not self.app.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        disc = getattr(self.app, "discovery", None)
        mcaps = list(disc.mcaps) if disc else []
        if not mcaps:
            messagebox.showinfo(APP_NAME, "No .mcap files were found in this flight.")
            return

        origin_fix = self._origin_fallback()
        manual_origin = (origin_fix.lat, origin_fix.lon) if origin_fix else None

        plan = self.app._plan()
        errors = plan.validate() if hasattr(plan, "validate") else []
        if errors:
            messagebox.showerror(APP_NAME,
                                 "Please fix these on the Transects tab first:\n\n• "
                                 + "\n• ".join(errors))
            return
        sites = [s for s in plan.sites if s.transects]
        if not sites:
            messagebox.showinfo(APP_NAME, "No transects are defined on the Transects tab.")
            return

        choice = self.station.get()
        station_id = dict(tide.STATIONS).get(choice)      # None = skip
        out_root = self._out_dir()
        want_map = bool(self.make_map.get())
        # Transect names repeat across sites ("T1" at each), so several sites in
        # one flight need the site in the filename or they overwrite each other.
        prefix = len(sites) > 1

        def work(progress, cancel):
            reports: list[str] = []
            if origin_fix and not origin_fix.confirmed:
                reports.append(
                    f"! Using ORIGIN_LAT/ORIGIN_LON from the vehicle's own "
                    f"BIN log ({origin_fix.lat:.6f}, {origin_fix.lon:.6f}) to "
                    f"seed the DVL track -- the EKF itself never logged "
                    f"adopting an origin (no ORGN event), so treat this run's "
                    f"map as dead reckoning from that starting point, not a "
                    f"confirmed fix.")
            for i, site in enumerate(sites):
                if cancel.is_set():
                    raise JobStopped(f"before site {site.name}")
                base = i / len(sites)
                span = 1.0 / len(sites)

                def report(f, m, b=base, s=span):
                    # The extractor has no Stop of its own, but it reports
                    # progress every 20,000 messages while reading and between
                    # every later stage -- so a Stop is raised from here, at
                    # the next report, rather than waiting for the site.
                    if cancel.is_set():
                        raise JobStopped(f"while {m}" if m else "")
                    progress(b + s * f, m)

                # The whole transect, with its pauses passed alongside rather
                # than cut out of the windows: the rows stay in the CSV and
                # are marked, so an analysis can drop them and a check on the
                # recording still sees an unbroken stretch of telemetry.
                specs = [
                    pipeline.TransectSpec(
                        f"{site.name}_{t.name}" if prefix else t.name,
                        [(t.start_tc, t.end_tc)],
                        pauses=[(p.start_tc, p.end_tc) for p in t.pauses],
                    )
                    for t in site.transects
                ]
                out = Path(out_root) / site.name if prefix else Path(out_root)

                result = pipeline.run(
                    mcaps,
                    site_name=site.name,
                    survey_date=site.date.replace("-", ""),
                    station_id=station_id,
                    save_location=out,
                    transects=specs,
                    make_map=want_map,
                    manual_origin=manual_origin,
                    progress=report,
                )
                if cancel.is_set():
                    # Stopped inside a stage that finished without another
                    # report: what it wrote is on disk but the run is not whole.
                    raise JobStopped(f"after site {site.name}; its CSVs may be "
                                     f"incomplete")
                reports.append(f"{site.name}: " + "; ".join(
                    result.summary_lines()[0:1]))
                for r in result.results:
                    reports.append("   " + r.message)
                if result.map_path:
                    reports.append(f"   map: {result.map_path}")
                if not result.tide_ok:
                    reports.append(f"   Depth_std blank — {result.tide_error}")
                for w in result.warnings + result.read.warnings:
                    reports.append(f"   ! {w}")
            return reports

        def done(res) -> None:
            if isinstance(res, JobStopped):
                self.note.configure(
                    text=f"Stopped {res}. Transect CSVs written before the stop "
                         f"are in {out_root}; the rest were not written.")
            elif isinstance(res, BaseException):
                self.note.configure(text=f"Failed: {res}")
            else:
                self.note.configure(text="Finished — the report is in the log below.")

        if self.app.submit(work, "Extracting transect CSVs", on_done=done):
            self.note.configure(text="Running — progress is in the footer.")
