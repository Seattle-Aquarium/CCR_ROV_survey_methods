"""
Transects: the times, saved, and checked against the dive.

The tab opened the moment the vehicle is back on deck and disarmed. The times
come off the dive slate, go in here, and are saved into the flight folder as
the survey plan every later step reads. Then the preview draws the dive
profile with those windows on it -- the gut check that the transects were
flown and recorded where they were written down, before anyone moves on to
the logs.

The preview does not need the logs downloaded. It reads the flight folder when
the recordings are already there, and otherwise the autopilot's own log
straight off the vehicle (see `previewsource`).
"""

from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from .. import previewsource as P
from ..survey import PLAN_FILENAME
from . import theme as T
from .widgets import Card, button, fit_wrap

SOURCES = {"Automatic": P.AUTO, "Flight folder": P.FOLDER, "Vehicle": P.VEHICLE}


class TransectSetup(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid(row=0, column=0, sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._profile_img = None

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. sites & transects ------------------------------------
        c1 = Card(body, "1.  Sites and transects",
                  "Times are TC-25, as written down in the field — the clock "
                  "the GoPro shows after a GoPro Labs precision-time sync. "
                  f"Save writes them into the flight folder as {PLAN_FILENAME}.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.folder_note = ctk.CTkLabel(c1.body, text="", font=T.FONT_SMALL,
                                        text_color=T.TEXT_MUTED, anchor="w")
        self.folder_note.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        app.sites_holder = ctk.CTkFrame(c1.body, fg_color="transparent")
        app.sites_holder.grid(row=1, column=0, sticky="ew")
        app.sites_holder.grid_columnconfigure(0, weight=1)

        srow = ctk.CTkFrame(c1.body, fg_color="transparent")
        srow.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        button(srow, "+ Add site", app.add_site, "primary", width=120
               ).grid(row=0, column=0, sticky="w")
        button(srow, "Load…", app._load_plan, "ghost", width=90
               ).grid(row=0, column=1, padx=(8, 0))
        button(srow, "Save", app._save_plan, "primary", width=90
               ).grid(row=0, column=2, padx=(8, 0))

        # ---- 2. preview -----------------------------------------------
        c2 = Card(body, "2.  Preview the transects",
                  "Draws the dive profile with your transects marked. Reads "
                  "the flight folder's recordings when they are there; "
                  "otherwise reads the autopilot's log straight off the "
                  "vehicle — a few megabytes, not the gigabyte recordings — "
                  "so this works before anything is downloaded.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)
        prow = ctk.CTkFrame(c2.body, fg_color="transparent")
        prow.grid(row=0, column=0, sticky="w")
        button(prow, "Preview transects", self._preview, "primary", width=170
               ).grid(row=0, column=0)
        ctk.CTkLabel(prow, text="read from", font=T.FONT_SMALL,
                     text_color=T.TEXT_MUTED).grid(row=0, column=1, padx=(18, 8))
        self.source = ctk.CTkSegmentedButton(
            prow, values=list(SOURCES), font=T.FONT_SMALL,
            fg_color=T.SURFACE_ALT, selected_color=T.ACCENT,
            selected_hover_color=T.ACCENT_HOVER, unselected_color=T.SURFACE_ALT,
            unselected_hover_color=T.BORDER, text_color=T.TEXT)
        self.source.set("Automatic")
        self.source.grid(row=0, column=2)

        self.preview_note = ctk.CTkLabel(
            c2.body, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
            anchor="w", justify="left", wraplength=900)
        self.preview_note.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        fit_wrap(c2.body, self.preview_note)
        self.preview_img = ctk.CTkLabel(c2.body, text="")
        self.preview_img.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.preview_img.grid_remove()          # takes no room until drawn

        app.add_site()
        self.refresh()

    def refresh(self) -> None:
        flight = self.app.flight_dir
        self.folder_note.configure(
            text=f"Flight folder: {flight}" if flight else
            "No flight folder yet — choose one on Monitoring so Save has "
            "somewhere to write.")

    # ------------------------------------------------------------------

    def _preview(self) -> None:
        from .. import depthplot
        from ..survey import utc_offset_hours
        from ..telemetry_cache import plan_windows

        plan = self.app._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(self.app.APP_NAME, "Please fix these first:\n\n• "
                                 + "\n• ".join(errs[:10]))
            return
        windows = plan_windows(plan)
        try:
            off = utc_offset_hours(plan.sites[0].date_obj(), plan.timezone)
        except Exception:
            off = -7.0
        flight, cfg, mode = self.app.flight_dir, self.app.cfg, self.app.mode
        source = SOURCES[self.source.get()]
        host = self.app.vehicle_host()
        self.preview_note.configure(text="Reading…", text_color=T.TEXT_MUTED)

        def work(progress, cancel):
            store, notes, where = P.preview_store(
                flight, windows, cfg=cfg, host=host, source=source,
                progress=progress, cancel=cancel)
            style = (depthplot.PlotStyle() if mode == "dark"
                     else depthplot.PlotStyle.light())
            img = depthplot.render_profile(store, windows, width=980, height=300,
                                           style=style, tz_offset_hours=off)
            return _Preview(img, depthplot.transect_stats(store, windows),
                            notes, where)

        self.app.submit(work, "Reading telemetry for the transect preview…",
                        on_done=self._show)

    def _show(self, res) -> None:
        if not isinstance(res, _Preview):
            self.preview_note.configure(
                text=f"No preview: {res}", text_color=T.WARN)
            return
        img = res.img
        self._profile_img = ctk.CTkImage(light_image=img, dark_image=img,
                                         size=img.size)
        self.preview_img.configure(image=self._profile_img, text="")
        self.preview_img.grid()
        bits = []
        for r in res.stats:
            d = (f"  {r['depth_min']:.1f}–{r['depth_max']:.1f} m"
                 if "depth_min" in r else "  no depth data")
            bits.append(f"{r['name']}: {r['seconds'] / 60:.1f} min{d}")
        lines = [f"From {res.where}.", "     ".join(bits)]
        lines += [f"note: {n}" for n in res.notes]
        self.preview_note.configure(text="\n".join(lines), text_color=T.TEXT)
        self.app.status.configure(text="Preview drawn — check the transects.")


class _Preview:
    """What the preview job hands back. Its summary goes to the shared log."""

    def __init__(self, img, stats, notes, where):
        self.img, self.stats, self.notes, self.where = img, stats, notes, where
        self.warnings: list[str] = []

    def summary(self) -> str:
        return f"Transect preview drawn from {self.where}."
