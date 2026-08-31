"""
Transect CSVs, as a step in the flight.

The extractor itself lives in the sibling ``mcap_to_csv`` package and has its
own standalone window. This page is the same tool driven from the flight that
is already open: the folder, the mcaps and the survey plan all come from
"Flight setup", so the transect windows are typed once and both the CSVs and
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

from . import theme as T
from .widgets import Card, button, entry, label

APP_NAME = "Underwater Telemetry Compositing"

#: What to tell someone whose environment predates this page.
_MISSING = (
    "The transect extractor is not installed in this environment.\n\n"
    "Close UTC and run run_UTC.bat again — it installs the extractor "
    "alongside UTC. Or install it by hand:\n\n"
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
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
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
                  "Taken from the survey plan on Flight setup. Edit them there "
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

        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the flight and plan. Called whenever this page is shown."""
        disc = getattr(self.app, "discovery", None)
        mcaps = list(disc.mcaps) if disc else []

        if not self.app.flight_dir:
            self.found.configure(text="No flight folder selected yet — "
                                      "choose one on Flight setup.")
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

        if self.app.flight_dir and not self.out_entry.get().strip():
            self.out_entry.insert(0, str(Path(self.app.flight_dir)))

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

        plan = self.app._plan()
        errors = plan.validate() if hasattr(plan, "validate") else []
        if errors:
            messagebox.showerror(APP_NAME,
                                 "Please fix these on Flight setup first:\n\n• "
                                 + "\n• ".join(errors))
            return
        sites = [s for s in plan.sites if s.transects]
        if not sites:
            messagebox.showinfo(APP_NAME, "No transects are defined on Flight setup.")
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
            for i, site in enumerate(sites):
                if cancel.is_set():
                    reports.append("Cancelled.")
                    break
                base = i / len(sites)
                span = 1.0 / len(sites)

                specs = [
                    pipeline.TransectSpec(
                        f"{site.name}_{t.name}" if prefix else t.name,
                        [(t.start_tc, t.end_tc)],
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
                    progress=lambda f, m, b=base, s=span: progress(b + s * f, m),
                )
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

        if self.app.submit(work, "Extracting transect CSVs"):
            self.note.configure(text="Running — progress is in the footer.")
