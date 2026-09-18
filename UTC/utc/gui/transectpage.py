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
                  "The telemetry found in this flight — .mcap from BlueOS 1.5 "
                  "onwards, .tlog from before it. Several files are normal and "
                  "are read as one continuous dive; the two formats can even be "
                  "mixed, which a dive spanning an upgrade will be.")
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

        label(c3.body, "Transect ID prefix").grid(row=1, column=0, sticky="w", pady=4)
        self.prefix_entry = entry(c3.body, "e.g. EBM_W25", width=280)
        self.prefix_entry.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=4)
        label(c3.body,
              "Each transect becomes <prefix>_T1, _T2 … in the Transect_ID column "
              "and the filename. Left blank, the site name is used.",
              muted=True).grid(row=2, column=1, sticky="w", padx=(12, 0))

        label(c3.body, "Save to").grid(row=3, column=0, sticky="w", pady=4)
        self.out_entry = entry(c3.body, "<flight>/transects", width=520)
        self.out_entry.grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=4)

        self.make_map = ctk.CTkCheckBox(
            c3.body, text="Also build a Leaflet map of these transects",
            font=T.FONT_BODY, text_color=T.TEXT)
        self.make_map.select()
        self.make_map.grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(8, 4))

        # Without a USBL the vehicle's position has to be typed into the DVL
        # page in BlueOS before arming. Forget, and the dive has a perfectly
        # good DVL track with nothing to hang it on. This anchors it afterwards.
        label(c3.body, "Origin (lat, lon)").grid(row=5, column=0, sticky="w",
                                                 pady=(10, 4))
        orow = ctk.CTkFrame(c3.body, fg_color="transparent")
        orow.grid(row=5, column=1, sticky="w", padx=(12, 0), pady=(10, 4))
        self.origin_lat = entry(orow, "47.6176", width=130)
        self.origin_lat.pack(side="left")
        self.origin_lon = entry(orow, "-122.3610", width=130)
        self.origin_lon.pack(side="left", padx=(8, 0))
        label(orow, "vessel position at arming; only if it was not set in BlueOS",
              muted=True).pack(side="left", padx=(10, 0))

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

        # What each transect looked like, once written. Three distances on
        # purpose: the GPS one is inflated by surface-fix jitter, and the gap
        # to the DVL's figure is a direct measure of that noise.
        c4.body.grid_columnconfigure(0, weight=1)
        self.summary_box = ctk.CTkTextbox(c4.body, height=150, font=T.FONT_MONO,
                                          fg_color=T.FIELD_BG, text_color=T.TEXT,
                                          border_width=1, border_color=T.BORDER,
                                          corner_radius=6, wrap="none")
        self.summary_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._set_summary("Nothing extracted yet.")

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
        self.health_box = ctk.CTkTextbox(c5.body, height=260, font=T.FONT_MONO,
                                         fg_color=T.FIELD_BG, text_color=T.TEXT,
                                         border_width=1, border_color=T.BORDER,
                                         corner_radius=6, wrap="none")
        self.health_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._set_health("Not checked yet.")

        self.refresh()

    # ------------------------------------------------------------------

    def _set_summary(self, text: str) -> None:
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", text)
        self.summary_box.configure(state="disabled")

    def _origin(self) -> tuple[float, float] | None:
        """The typed origin, or None if both fields are empty.

        Half an origin, or one out of range, is refused rather than guessed at:
        a swapped lat/lon would put the transect in the Southern Ocean, and the
        map would look perfectly plausible while it did.
        """
        lat_s = self.origin_lat.get().strip()
        lon_s = self.origin_lon.get().strip()
        if not lat_s and not lon_s:
            return None
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            raise ValueError("Origin needs both latitude and longitude, in "
                             "decimal degrees.") from None
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError(f"Origin {lat}, {lon} is out of range "
                             "(latitude -90..90, longitude -180..180).")
        return lat, lon

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
        mcaps = list(disc.telemetry) if disc else []
        if not mcaps:
            messagebox.showinfo(APP_NAME, "Select a flight folder with .mcap or "
                                          ".tlog telemetry first.")
            return

        from ccr_m2c.health import read_health
        from ccr_m2c.pipeline import TransectSpec
        from ccr_m2c.transect import make_transect_id

        # Scope the report to the transects as well as the whole dive. Most of a
        # dive is transit -- on 2026-09-02, 85 minutes of recording held about 42
        # minutes of transect -- so whole-dive dropout counts describe the
        # surface intervals between them rather than the part being analysed.
        # The plan is already on screen; not passing it made this page report
        # numbers nobody could act on.
        specs: list[TransectSpec] = []
        try:
            for site in self.app._plan().sites:
                specs.extend(
                    TransectSpec(
                        make_transect_id(
                            self.prefix_entry.get().strip() or site.name,
                            n, t.name),
                        [(t.start_tc, t.end_tc)])
                    for n, t in enumerate(site.transects, start=1)
                    if t.start_tc and t.end_tc
                )
        except Exception:
            specs = []          # an unfinished plan still gets the dive-wide view

        def work(progress, cancel):
            return read_health(mcaps, transects=specs, progress=progress)

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
        mcaps = list(disc.telemetry) if disc else []

        if not self.app.flight_dir:
            self.found.configure(text="No flight folder selected yet — "
                                      "choose one on Flight setup.")
        elif not mcaps:
            self.found.configure(
                text="No .mcap or .tlog telemetry in this flight. It normally "
                     "sits in a logs/ folder beside the video.")
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
        from ccr_m2c.transect import make_transect_id

        if not self.app.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        disc = getattr(self.app, "discovery", None)
        mcaps = list(disc.telemetry) if disc else []
        if not mcaps:
            messagebox.showinfo(APP_NAME, "No .mcap or .tlog telemetry was found "
                                          "in this flight.")
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
        try:
            origin = self._origin()
        except ValueError as ex:
            messagebox.showerror(APP_NAME, str(ex))
            return
        # Transect names repeat across sites ("T1" at each), so several sites in
        # one flight need the site in the filename or they overwrite each other.
        id_prefix = self.prefix_entry.get().strip()
        # Several sites in one flight still get their own folder, so two
        # sites sharing a prefix cannot overwrite each other's CSVs.
        per_site_folder = len(sites) > 1

        results_by_site: list = []

        def work(progress, cancel):
            reports: list[str] = []
            for i, site in enumerate(sites):
                if cancel.is_set():
                    reports.append("Cancelled.")
                    break
                base = i / len(sites)
                span = 1.0 / len(sites)

                # The survey code is typed once and the ordinal filled in, so
                # every transect in a survey carries the same prefix and a
                # mistyped one cannot separate two of them.
                stem = id_prefix or site.name
                specs = [
                    pipeline.TransectSpec(
                        make_transect_id(stem, n, t.name),
                        [(t.start_tc, t.end_tc)],
                    )
                    for n, t in enumerate(site.transects, start=1)
                ]
                out = Path(out_root) / site.name if per_site_folder else Path(out_root)

                result = pipeline.run(
                    mcaps,
                    site_name=site.name,
                    survey_date=site.date.replace("-", ""),
                    station_id=station_id,
                    save_location=out,
                    transects=specs,
                    make_map=want_map,
                    origin=origin,
                    progress=lambda f, m, b=base, s=span: progress(b + s * f, m),
                )
                results_by_site.append((site.name, result))
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

        def done(_reports) -> None:
            # Back on the main thread. The table lives on this page rather than
            # in the footer log, which scrolls away under whatever runs next.
            from ccr_m2c.transect import format_stats_table
            blocks: list[str] = []
            for name, result in results_by_site:
                table = format_stats_table(result.results)
                if table:
                    if len(results_by_site) > 1:
                        blocks.append(name)
                    blocks.extend(table)
                    blocks.append("")
            self._set_summary("\n".join(blocks).strip() or "No transects were written.")
            n = sum(len(r.saved) for _name, r in results_by_site)
            self.note.configure(text=f"Wrote {n} transect CSV(s).")

        if self.app.submit(work, "Extracting transect CSVs", on_done=done):
            self.note.configure(text="Running — progress is in the footer.")
            self._set_summary("Extracting...")
