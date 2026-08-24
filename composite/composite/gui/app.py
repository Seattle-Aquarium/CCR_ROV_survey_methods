"""
The CCR ROV Composite desktop application.

Layout follows the order of the job: pick the flight folder, confirm what was
found, describe the sites and transects, choose output sizes, run.

The pipeline runs on a worker thread and reports progress through a queue that
the Tk main loop drains on a timer. Tk is not thread-safe, so no worker ever
touches a widget directly.
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import brand, discovery
from ..config import AppConfig, RENDITIONS
from ..pipeline import RunRequest, RunResult, run as run_pipeline
from ..survey import Site, SurveyPlan, Transect
from . import theme as T
from .widgets import Card, SiteFrame, button, entry, label

APP_NAME = "CCR ROV Composite"
PLAN_FILENAME = "composite_plan.json"


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x900")
        self.minsize(980, 700)

        self.mode = "dark"
        T.apply(ctk, self.mode)
        self.configure(fg_color=T.BG)

        self.cfg = AppConfig()
        self.flight_dir: Path | None = None
        self.discovery: discovery.Discovery | None = None
        self._sites: list[SiteFrame] = []
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._logo_img = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_body()
        self._build_footer()
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    #  chrome
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        h = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0, height=76)
        h.grid(row=0, column=0, sticky="ew")
        h.grid_columnconfigure(2, weight=1)
        h.grid_propagate(False)

        self.logo_label = ctk.CTkLabel(h, text="")
        self.logo_label.grid(row=0, column=0, rowspan=2, padx=(18, 14), pady=12)
        self._load_logo()

        ctk.CTkLabel(h, text=APP_NAME, font=T.FONT_TITLE, text_color=T.HEADING
                     ).grid(row=0, column=1, sticky="sw", pady=(14, 0))
        ctk.CTkLabel(h, text="ROV telemetry overlays for downward GoPro transects",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED
                     ).grid(row=1, column=1, sticky="nw", pady=(0, 14))

        self.theme_switch = ctk.CTkSwitch(
            h, text="Dark mode", command=self._toggle_theme,
            font=T.FONT_SMALL, text_color=T.TEXT,
            progress_color=T.ACCENT, button_color=T.SURFACE_ALT,
        )
        self.theme_switch.select()
        self.theme_switch.grid(row=0, column=3, rowspan=2, padx=18)

    def _load_logo(self) -> None:
        path = T.logo_for(self.mode)
        if not path:
            self.logo_label.configure(text="Seattle Aquarium", font=T.FONT_H2,
                                      text_color=T.HEADING)
            return
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            h = 46
            w = max(1, int(im.width * h / im.height))
            self._logo_img = ctk.CTkImage(light_image=im, dark_image=im, size=(w, h))
            self.logo_label.configure(image=self._logo_img, text="")
        except Exception:
            self.logo_label.configure(text="Seattle Aquarium", font=T.FONT_H2,
                                      text_color=T.HEADING)

    def _toggle_theme(self) -> None:
        self.mode = "dark" if self.theme_switch.get() else "light"
        T.apply(ctk, self.mode)
        self.theme_switch.configure(text="Dark mode" if self.mode == "dark"
                                    else "Light mode")
        self._load_logo()

    # ------------------------------------------------------------------
    #  body
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(14, 8))
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. flight folder ----------------------------------------
        c1 = Card(body, "1.  Flight folder",
                  "Point at the folder for the dive. Expected inside: "
                  "logs/ (mcap), videos/downward/ (GoPro). Older layouts are "
                  "handled too, and forward/ video is ignored.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        r = ctk.CTkFrame(c1.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="ew")
        r.grid_columnconfigure(0, weight=1)
        self.folder_entry = entry(r, "No folder selected", width=700)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(r, "Browse…", self._pick_folder, "primary", width=110
               ).grid(row=0, column=1)

        self.found = ctk.CTkTextbox(c1.body, height=118, font=T.FONT_MONO,
                                    fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                    border_width=1, border_color=T.BORDER,
                                    corner_radius=6, wrap="none")
        self.found.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.found.insert("1.0", "Nothing selected yet.")
        self.found.configure(state="disabled")

        # ---- 2. sites & transects ------------------------------------
        c2 = Card(body, "2.  Sites and transects",
                  "Times are TC-25, as written down in the field — the clock the "
                  "GoPro shows after a GoPro Labs precision-time sync.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.sites_holder = ctk.CTkFrame(c2.body, fg_color="transparent")
        self.sites_holder.grid(row=0, column=0, sticky="ew")
        self.sites_holder.grid_columnconfigure(0, weight=1)

        srow = ctk.CTkFrame(c2.body, fg_color="transparent")
        srow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        button(srow, "+ Add site", self.add_site, "primary", width=120
               ).grid(row=0, column=0, sticky="w")
        button(srow, "Load…", self._load_plan, "ghost", width=90
               ).grid(row=0, column=1, padx=(8, 0))
        button(srow, "Save", self._save_plan, "ghost", width=90
               ).grid(row=0, column=2, padx=(8, 0))

        # ---- 3. outputs ----------------------------------------------
        c3 = Card(body, "3.  Output",
                  "One video per transect per resolution, written to "
                  "videos/composites/. A 1 Hz telemetry CSV goes to logs/.")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        rr = ctk.CTkFrame(c3.body, fg_color="transparent")
        rr.grid(row=0, column=0, sticky="w")
        self.res_vars: dict[str, ctk.BooleanVar] = {}
        for i, key in enumerate(("4K", "1080p", "720p")):
            v = ctk.BooleanVar(value=(key == "1080p"))
            self.res_vars[key] = v
            ctk.CTkCheckBox(rr, text=key, variable=v, font=T.FONT_BODY,
                            text_color=T.TEXT, fg_color=T.ACCENT,
                            hover_color=T.ACCENT_HOVER, checkmark_color=T.ACCENT_TEXT,
                            border_color=T.FIELD_BORDER, corner_radius=4
                            ).grid(row=0, column=i, padx=(0, 22))
        self.csv_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rr, text="1 Hz telemetry CSV", variable=self.csv_var,
                        font=T.FONT_BODY, text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER, checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4
                        ).grid(row=0, column=3, padx=(10, 0))

        ctk.CTkLabel(c3.body,
                     text="4K keeps 10-bit for analysis; 720p is 8-bit H.264 for "
                          "sharing. Longer transects at 4K can take hours.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.add_site()

    # ------------------------------------------------------------------
    #  footer
    # ------------------------------------------------------------------

    def _build_footer(self) -> None:
        f = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        f.grid(row=2, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)

        self.log = ctk.CTkTextbox(f, height=150, font=T.FONT_MONO,
                                  fg_color=T.FIELD_BG, text_color=T.TEXT,
                                  border_width=1, border_color=T.BORDER,
                                  corner_radius=6, wrap="word")
        self.log.grid(row=0, column=0, columnspan=3, sticky="ew",
                      padx=16, pady=(12, 8))
        self.log.configure(state="disabled")

        self.progress = ctk.CTkProgressBar(f, height=12, corner_radius=6,
                                           progress_color=T.ACCENT,
                                           fg_color=T.SURFACE_ALT)
        self.progress.set(0.0)
        self.progress.grid(row=1, column=0, sticky="ew", padx=(16, 12), pady=(0, 6))

        self.status = ctk.CTkLabel(f, text="Ready.", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.run_btn = button(f, "Create composites", self._start, "primary", 170)
        self.run_btn.grid(row=1, column=1, rowspan=2, padx=(0, 8), pady=(0, 12))
        self.cancel_btn = button(f, "Stop", self._cancel_run, "danger", 90)
        self.cancel_btn.grid(row=1, column=2, rowspan=2, padx=(0, 16), pady=(0, 12))
        self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    #  actions
    # ------------------------------------------------------------------

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select the flight folder")
        if not d:
            return
        self.flight_dir = Path(d)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan()

    def _scan(self) -> None:
        if not self.flight_dir:
            return
        disc = discovery.discover(self.flight_dir)
        self.discovery = disc
        self._set_found(disc.summary())

        # adopt the project/date suggested by the folder path
        guess_project, guess_date = _guess_from_path(self.flight_dir)
        for sf in self._sites:
            if not sf.project.get().strip() and guess_project:
                sf.project.insert(0, guess_project)
            if guess_date and sf.date.get().strip() == _date.today().isoformat():
                sf.date.delete(0, "end")
                sf.date.insert(0, guess_date)

        plan_file = self.flight_dir / PLAN_FILENAME
        if plan_file.is_file():
            try:
                self._apply_plan(SurveyPlan.load(plan_file))
                self._log(f"Loaded saved transects from {PLAN_FILENAME}")
            except Exception as ex:
                self._log(f"Could not read {PLAN_FILENAME}: {ex}")

    def _set_found(self, text: str) -> None:
        self.found.configure(state="normal")
        self.found.delete("1.0", "end")
        self.found.insert("1.0", text)
        self.found.configure(state="disabled")

    def add_site(self, site: Site | None = None) -> None:
        gp, gd = _guess_from_path(self.flight_dir) if self.flight_dir else ("", "")
        sf = SiteFrame(self.sites_holder, self._remove_site,
                       index=len(self._sites) + 1, site=site,
                       default_project=gp, default_date=gd)
        sf.grid(row=len(self._sites), column=0, sticky="ew", pady=5)
        self._sites.append(sf)

    def _remove_site(self, sf: SiteFrame) -> None:
        if len(self._sites) == 1:
            messagebox.showinfo(APP_NAME, "At least one site is needed.")
            return
        self._sites.remove(sf)
        sf.destroy()
        for i, s in enumerate(self._sites):
            s.grid(row=i, column=0, sticky="ew", pady=5)

    def _plan(self) -> SurveyPlan:
        return SurveyPlan([sf.to_site() for sf in self._sites])

    def _apply_plan(self, plan: SurveyPlan) -> None:
        for sf in self._sites:
            sf.destroy()
        self._sites.clear()
        for s in plan.sites:
            self.add_site(s)
        if not self._sites:
            self.add_site()

    def _save_plan(self) -> None:
        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        try:
            self._plan().save(self.flight_dir / PLAN_FILENAME)
            self._log(f"Saved transects to {PLAN_FILENAME}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not save: {ex}")

    def _load_plan(self) -> None:
        start = str(self.flight_dir) if self.flight_dir else None
        p = filedialog.askopenfilename(title="Load transects",
                                       initialdir=start,
                                       filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            self._apply_plan(SurveyPlan.load(p))
            self._log(f"Loaded transects from {Path(p).name}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not load: {ex}")

    # ------------------------------------------------------------------
    #  run
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        if self.discovery is None or not self.discovery.ok:
            if not messagebox.askyesno(
                APP_NAME,
                "The flight folder does not look complete (see the panel under "
                "step 1).\n\nTry anyway?"
            ):
                return

        plan = self._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(APP_NAME, "Please fix these first:\n\n• " +
                                 "\n• ".join(errs[:10]))
            return

        rends = tuple(k for k, v in self.res_vars.items() if v.get())
        if not rends:
            messagebox.showinfo(APP_NAME, "Choose at least one output resolution.")
            return

        try:
            plan.save(self.flight_dir / PLAN_FILENAME)
        except Exception:
            pass

        self._cancel.clear()
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.set(0.0)
        self._log("─" * 60)
        self._log(f"Starting: {len(plan.sites)} site(s), "
                  f"{sum(len(s.transects) for s in plan.sites)} transect(s), "
                  f"{', '.join(rends)}")

        req = RunRequest(flight_dir=self.flight_dir, plan=plan,
                         renditions=rends, app=self.cfg,
                         write_csv=bool(self.csv_var.get()))

        def work() -> None:
            try:
                res = run_pipeline(
                    req,
                    progress=lambda f, m: self._queue.put(("progress", f, m)),
                    cancel=self._cancel,
                )
                self._queue.put(("done", res))
            except Exception:
                self._queue.put(("crash", traceback.format_exc()))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel_run(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self.status.configure(text="Stopping…")

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, frac, msg = item
                    self.progress.set(frac)
                    if msg:
                        self.status.configure(text=msg)
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "crash":
                    self._log("Unexpected error:\n" + item[1])
                    self._reset_buttons()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, res: RunResult) -> None:
        self._reset_buttons()
        self.progress.set(1.0 if res.ok else self.progress.get())
        for line in res.summary().splitlines():
            self._log(line)
        if res.cancelled:
            self.status.configure(text="Cancelled.")
        elif res.errors:
            self.status.configure(text="Finished with errors — see the log.")
        else:
            self.status.configure(text=f"Done — {len(res.outputs)} file(s).")
            if res.outputs:
                try:
                    import os
                    os.startfile(res.outputs[0].parent)   # noqa: S606
                except Exception:
                    pass

    def _reset_buttons(self) -> None:
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A run is in progress. Quit anyway?"):
                return
            self._cancel.set()
        self.destroy()


def _guess_from_path(p: Path | None) -> tuple[str, str]:
    """Infer project and date from a path like .../HSIL/2025/2025_09_27_Shaw_Island."""
    if p is None:
        return "", ""
    import re
    name = p.name
    m = re.match(r"(\d{4})[_-](\d{2})[_-](\d{2})", name)
    date_s = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    project = ""
    for parent in list(p.parents)[:3]:
        if parent.name.lower() == "flights":
            break
        if not re.fullmatch(r"\d{4}", parent.name):
            project = parent.name
    return project, date_s


def main() -> None:
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
