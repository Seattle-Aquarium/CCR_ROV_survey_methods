"""
ROV Imagery Processing (working title) -- the desktop application.

Everything to do with the imagery that came back from a flight:

1. **Flight & transects** -- the flight folder, and the transect times every
   other tab sorts and cuts by. Saved by ROV Flight Operations into the flight
   folder, so opening the folder usually fills them in.
2. **Import photos** -- a GoPro card, or the flight's own photos, into
   per-transect folders.
3. **Process photos** -- GPR raws developed to TIF through Lightroom Classic.
4. **Banner tools** -- the telemetry banner on folders of stills.
5. **Video** -- trims, telemetry composites, clips and side-by-sides.

The vehicle itself -- monitoring, logs, flight reports -- is ROV Flight
Operations, a separate program.
"""

from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import discovery
from ..config import AppConfig
from ..pipeline import RunResult
from ..survey import PLAN_FILENAME, Site, SurveyPlan, plan_path
from . import theme as T
from .shell import Shell
from .widgets import Card, SiteFrame, button, entry, fit_wrap, output_box, say

APP_NAME = "ROV Imagery Processing"


class App(Shell):
    APP_NAME = APP_NAME
    #: A working title, like the program's name; the folder is rov_imagery_processing.
    DISPLAY_TITLE = "ROV Imagery Processing"
    SLUG = "rov_imagery_processing"

    def __init__(self) -> None:
        self.cfg = AppConfig()
        self.flight_dir: Path | None = None
        self.discovery: discovery.Discovery | None = None
        self._sites: list[SiteFrame] = []
        self._profile_img = None
        super().__init__()

    # ------------------------------------------------------------------
    #  tabs
    # ------------------------------------------------------------------

    def build_tabs(self) -> None:
        from .bannertools import BannerToolsTab
        from .importpage import ImportPage
        from .processpage import ProcessPage
        from .videopage import VideoPage

        tab = self.add_tab("Flight & transects")
        self._build_flight_tab(tab)

        for name, key, cls in (("Import photos", "import", ImportPage),
                               ("Process photos", "process", ProcessPage),
                               ("Banner tools", "banner", BannerToolsTab),
                               ("Video", "video", VideoPage)):
            tab = self.add_tab(name)
            page = cls(tab, self)
            page.grid(row=0, column=0, sticky="nsew")
            self.mount(name, key, page)

    def _build_flight_tab(self, parent) -> None:
        body = self.scroll_body(parent)

        c1 = Card(body, "1.  Flight folder",
                  "Point at the folder for the dive. Expected inside: logs/mcap "
                  "(or logs) for the recordings, videos/downward for the GoPro, "
                  "photos for the stills. Older layouts are handled too.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        r = ctk.CTkFrame(c1.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="ew")
        r.grid_columnconfigure(0, weight=1)
        self.folder_entry = entry(r, "No folder selected", width=700)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(r, "Browse…", self._pick_folder, "primary", width=110
               ).grid(row=0, column=1)
        self.found = output_box(c1.body, wrap="none")
        self.found.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        c1.add_grip(self.found, self.found.min_height)
        say(self.found, "Nothing selected yet.")

        c2 = Card(body, "2.  Sites and transects",
                  "Times are TC-25, as written down in the field. ROV Flight "
                  f"Operations saves them into the flight folder as "
                  f"{PLAN_FILENAME}, and they load from there when the folder "
                  f"is chosen.")
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

        c3 = Card(body, "3.  Preview the transects",
                  "Reads the flight's recordings and draws the dive profile "
                  "with your transects marked. Worth doing before importing "
                  "imagery — especially if the card is about to be wiped.")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(0, weight=1)
        button(c3.body, "Preview transects", self._preview_transects, "primary",
               width=170).grid(row=0, column=0, sticky="w")
        self.preview_note = ctk.CTkLabel(
            c3.body, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
            anchor="w", justify="left", wraplength=900)
        self.preview_note.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        fit_wrap(c3.body, self.preview_note)
        self.preview_img = ctk.CTkLabel(c3.body, text="")
        self.preview_img.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.preview_img.grid_remove()          # takes no room until drawn
        self.add_site()

    # ------------------------------------------------------------------
    #  the flight
    # ------------------------------------------------------------------

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select the flight folder")
        if d:
            self.use_flight(Path(d))

    def use_flight(self, path: Path) -> None:
        self.flight_dir = Path(path)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan()

    def _scan(self) -> None:
        if not self.flight_dir:
            return
        disc = discovery.discover(self.flight_dir)
        self.discovery = disc
        say(self.found, disc.summary())
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
                self._log(f"Loaded saved transects from {saved.name}")
            except Exception as ex:
                self._log(f"Could not read {saved.name}: {ex}")
        for pages in self._tab_pages.values():
            for page in pages:
                if hasattr(page, "refresh"):
                    try:
                        page.refresh()
                    except Exception as ex:
                        self._log(f"{type(page).__name__}.refresh failed: {ex}")

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
            out = plan_path(self.flight_dir, for_writing=True)
            self._plan().save(out)
            self._log(f"Saved transects to {out}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not save: {ex}")

    def _load_plan(self) -> None:
        start = str(self.flight_dir) if self.flight_dir else None
        p = filedialog.askopenfilename(title="Load transects", initialdir=start,
                                       filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            self._apply_plan(SurveyPlan.load(p))
            self._log(f"Loaded transects from {Path(p).name}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not load: {ex}")

    # ------------------------------------------------------------------
    #  preview
    # ------------------------------------------------------------------

    def _preview_transects(self) -> None:
        from .. import depthplot
        from ..pipeline import ensure_telemetry, plan_windows
        from ..survey import utc_offset_hours

        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        plan = self._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(APP_NAME, "Please fix these first:\n\n• "
                                 + "\n• ".join(errs[:10]))
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

    def finish_special(self, res) -> bool:
        if isinstance(res, tuple) and res and res[0] == "profile":
            _tag, img, stats, warns = res
            self.progress.set(1.0)
            self._profile_img = ctk.CTkImage(light_image=img, dark_image=img,
                                             size=img.size)
            self.preview_img.configure(image=self._profile_img, text="")
            self.preview_img.grid()
            bits = []
            for r in stats:
                d = (f"  {r['depth_min']:.1f}–{r['depth_max']:.1f} m"
                     if "depth_min" in r else "  no depth data")
                bits.append(f"{r['name']}: {r['seconds'] / 60:.1f} min{d}")
            self.preview_note.configure(text="     ".join(bits), text_color=T.TEXT)
            for w in warns:
                self._log(f"note: {w}")
            self.status.configure(text="Preview drawn — check the transects.")
            return True
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
            return True
        return False


def _guess_from_path(p: Path | None) -> tuple[str, str]:
    """Infer project and date from a path like .../HSIL/2025/2025_09_27_Shaw_Island."""
    if p is None:
        return "", ""
    m = re.match(r"(\d{4})[_-](\d{2})[_-](\d{2})", p.name)
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
