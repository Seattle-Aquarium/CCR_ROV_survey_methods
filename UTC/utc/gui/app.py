"""
The Underwater Telemetry Compositing (UTC) desktop application.

Layout follows the order of the job: pick the flight folder, confirm what was
found, describe the sites and transects, choose output sizes, run.

The pipeline runs on a worker thread and reports progress through a queue that
the Tk main loop drains on a timer. Tk is not thread-safe, so no worker ever
touches a widget directly.
"""

from __future__ import annotations

import queue
import threading
import traceback
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import discovery
from ..config import AppConfig
from ..pipeline import RunResult
from ..survey import (
    PLAN_FILENAME,
    Site,
    SurveyPlan,
    plan_path,
)
from . import theme as T
from .widgets import Card, SiteFrame, button, entry

APP_NAME = "Underwater Telemetry Compositing"
#: Short form, for window chrome and generated file names.
APP_ABBREV = "UTC"
# Plan filename and legacy fallback live in survey.py, so the CLI and the
# GUI cannot drift apart on which file they read.


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
        self._queue: queue.Queue[tuple] = queue.Queue()
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
        ctk.CTkLabel(h, text="Telemetry overlays for ROV transect video and stills",
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
        """A left rail, one page per stage of a flight's life.

        Vertical rather than tabs across the top: pages are added by growing
        downward, so a fifth never has to fight for horizontal room or lose its
        label to truncation.
        """
        from .bannertools import BannerToolsTab
        from .importpage import ImportPage
        from .nav import Navigator
        from .videopage import VideoPage

        nav = Navigator(self)
        nav.grid(row=1, column=0, sticky="nsew", padx=(0, 16), pady=(4, 8))
        self.nav = nav

        self._build_flight_page(nav.add("Flight setup", "folder · transects"))
        self.pages = {}
        for name, sub, cls in (
            ("Import photos", "card or folder", ImportPage),
            ("Video", "trim · composite", VideoPage),
            ("Banner tools", "edited JPGs", BannerToolsTab),
        ):
            page = cls(nav.add(name, sub), self)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
        nav.select("Flight setup")

    def use_flight(self, path: Path) -> None:
        """Adopt a flight folder as the current one and rescan it.

        Creating a flight selects it, so the natural next step needs no
        re-navigation. Every page reads `self.flight_dir`, so one setter serves
        all of them.
        """
        self.flight_dir = Path(path)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan()

    def _build_flight_page(self, parent) -> None:
        body = ctk.CTkScrollableFrame(parent, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
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

        # ---- 3. preview -----------------------------------------------
        c3 = Card(body, "3.  Preview the transects",
                  "Reads the flight's mcap and draws the dive profile with your "
                  "transects marked. Worth doing before importing imagery — "
                  "especially if the card is about to be wiped.")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(0, weight=1)
        prow = ctk.CTkFrame(c3.body, fg_color="transparent")
        prow.grid(row=0, column=0, sticky="w")
        button(prow, "Preview transects", self._preview_transects, "primary",
               width=170).grid(row=0, column=0)
        button(prow, "Save plan", self._save_plan, "ghost", width=110
               ).grid(row=0, column=1, padx=(10, 0))
        self.preview_note = ctk.CTkLabel(
            c3.body, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
            anchor="w", justify="left")
        self.preview_note.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.preview_img = ctk.CTkLabel(c3.body, text="")
        self.preview_img.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._profile_img = None

        self.add_site()

    def _build_footer(self) -> None:
        """Progress, status and log, shared by every page.

        There is no global Run button: each page starts its own job, and one
        button would have to mean something different on each of them. Stop
        stays here because there is only ever one worker to stop.
        """
        f = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        f.grid(row=2, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)

        self.log = ctk.CTkTextbox(f, height=140, font=T.FONT_MONO,
                                  fg_color=T.FIELD_BG, text_color=T.TEXT,
                                  border_width=1, border_color=T.BORDER,
                                  corner_radius=6, wrap="word")
        self.log.grid(row=0, column=0, columnspan=2, sticky="ew",
                      padx=16, pady=(12, 8))
        self.log.configure(state="disabled")

        self.progress = ctk.CTkProgressBar(f, height=12, corner_radius=6,
                                           progress_color=T.ACCENT,
                                           fg_color=T.SURFACE_ALT)
        self.progress.set(0.0)
        self.progress.grid(row=1, column=0, sticky="ew", padx=(16, 12),
                           pady=(0, 6))

        self.status = ctk.CTkLabel(f, text="Ready.", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.cancel_btn = button(f, "Stop", self._cancel_run, "danger", 90)
        self.cancel_btn.grid(row=1, column=1, rowspan=2, padx=(0, 16),
                             pady=(0, 12))
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

        saved = plan_path(self.flight_dir)
        if saved.is_file():
            try:
                self._apply_plan(SurveyPlan.load(saved))
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
            self._plan().save(plan_path(self.flight_dir, for_writing=True))
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

    def _preview_transects(self) -> None:
        """Draw the dive profile with the transects marked.

        The sanity check before imagery is imported and a card is wiped: it
        shows the times landing on the part of the dive the user believes they
        describe, rather than asking them to trust six typed digits.
        """
        from .. import depthplot
        from ..pipeline import ensure_telemetry, plan_windows
        from ..survey import utc_offset_hours

        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        plan = self._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(APP_NAME, "Please fix these first:\n\n• " +
                                 "\n• ".join(errs[:10]))
            return

        flight, cfg, mode = self.flight_dir, self.cfg, self.mode
        windows = plan_windows(plan)
        try:
            off = utc_offset_hours(plan.sites[0].date_obj(), plan.timezone)
        except Exception:
            off = -7.0

        def work(progress, cancel):
            store, warns = ensure_telemetry(
                flight, cfg, windows=[(a, b) for _n, a, b in windows],
                progress=progress)
            style = (depthplot.PlotStyle() if mode == "dark"
                     else depthplot.PlotStyle.light())
            img = depthplot.render_profile(store, windows, width=980, height=300,
                                           style=style, tz_offset_hours=off)
            return ("profile", img, depthplot.transect_stats(store, windows), warns)

        self.submit(work, "Reading telemetry for the transect preview…")

    def _show_profile(self, img, stats, warns) -> None:
        """Put the rendered profile on the Flight setup page."""
        self._profile_img = ctk.CTkImage(light_image=img, dark_image=img,
                                         size=img.size)
        self.preview_img.configure(image=self._profile_img, text="")
        bits = []
        for r in stats:
            d = (f"  {r['depth_min']:.1f}–{r['depth_max']:.1f} m"
                 if "depth_min" in r else "  no depth data")
            bits.append(f"{r['name']}: {r['seconds']/60:.1f} min{d}")
        self.preview_note.configure(text="     ".join(bits), text_color=T.TEXT)
        for w in warns:
            self._log(f"note: {w}")

    # ------------------------------------------------------------------
    #  the one worker
    # ------------------------------------------------------------------

    def submit(self, work, label: str | None = None) -> bool:
        """Run ``work(progress, cancel)`` on the worker thread.

        Every tab goes through here, so there is exactly one worker, one queue,
        and one place that touches widgets. Returns False if a job is already
        running rather than starting a second one -- two jobs moving the same
        files would race.
        """
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(APP_NAME, "A job is already running. Wait for "
                                          "it to finish, or press Stop.")
            return False

        self._cancel.clear()
        self.cancel_btn.configure(state="normal")
        self.progress.set(0.0)
        if label:
            self._log("─" * 60)
            self._log(label)

        def runner() -> None:
            try:
                out = work(
                    lambda f, m="": self._queue.put(("progress", f, m)),
                    self._cancel,
                )
                self._queue.put(("done", out))
            except Exception:
                self._queue.put(("crash", traceback.format_exc()))

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()
        return True

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

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

    def _finish(self, res) -> None:
        """Report whatever the worker returned.

        A full pipeline run gets the detailed treatment; the smaller jobs the
        other tabs submit just report their own summary, so one worker can
        serve every tab without each needing its own plumbing.
        """
        self._reset_buttons()

        if isinstance(res, RunResult):
            self.progress.set(1.0 if res.ok else self.progress.get())
            for line in res.summary().splitlines():
                self._log(line)
            if res.cancelled:
                self.status.configure(text="Cancelled.")
            elif res.errors:
                self.status.configure(text="Finished with errors — see the log.")
            else:
                self.status.configure(text=f"Done — {len(res.outputs)} file(s).")
                self._reveal(res.outputs[0].parent if res.outputs else None)
            return

        if isinstance(res, tuple) and res and res[0] == "profile":
            _tag, img, stats, warns = res
            self.progress.set(1.0)
            self._show_profile(img, stats, warns)
            self.status.configure(text="Preview drawn — check the transects.")
            return

        self.progress.set(1.0)
        reports = res if isinstance(res, (list, tuple)) else [res]
        opened: Path | None = None
        for r in reports:
            if r is None:
                continue
            text = r.summary() if hasattr(r, "summary") else str(r)
            for line in str(text).splitlines():
                self._log(line)
            for w in getattr(r, "warnings", []):
                self._log(f"WARNING: {w}")
            for e in getattr(r, "errors", []):
                self._log(f"ERROR: {e}")
            opened = opened or getattr(r, "root", None) or getattr(r, "target", None)
        self.status.configure(text="Done.")
        self._reveal(opened)

    def _reveal(self, path: Path | None) -> None:
        """Open a folder in Explorer, best effort."""
        if not path:
            return
        try:
            import os
            os.startfile(Path(path))          # noqa: S606
        except Exception:
            pass

    def _reset_buttons(self) -> None:
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
