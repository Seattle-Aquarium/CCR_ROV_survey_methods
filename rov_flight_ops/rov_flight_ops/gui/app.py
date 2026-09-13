"""
ROV Flight Operations (working title) -- the desktop application.

Everything to do with the vehicle and what it recorded, in the order a survey
day uses it:

1. **Monitoring** -- where the operator sits during a flight. The flight folder
   is chosen here first, usually before the ROV is even connected, because it
   is where everything the program writes is filed.
2. **Transects** -- straight after the flight, with the vehicle disarmed on
   deck: type the transect times, save them, and check them against the dive
   profile.
3. **BlueOS logs** -- see what is on the Pi, download it into the flight
   folder, and clear old files off it.
4. **Flight summary** -- the day read back, and whether the recordings are
   sound.
5. **Analyze transects** -- per-transect CSVs and sensor health.

Imagery -- photos and video -- is ROV Imagery Processing, a separate program.
"""

from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import discovery, settings
from ..config import AppConfig
from ..survey import PLAN_FILENAME, Site, SurveyPlan, plan_path
from .shell import Shell
from .widgets import Card, SiteFrame, button, entry, output_box, say

APP_NAME = "ROV Flight Operations"


class App(Shell):
    APP_NAME = APP_NAME
    #: A working title, like the program's name; the folder is rov_flight_ops.
    DISPLAY_TITLE = "ROV Flight Operations"
    SLUG = "rov_flight_ops"

    def __init__(self) -> None:
        self.cfg = AppConfig()
        self.settings = settings.load()
        self.flight_dir: Path | None = None
        #: The laptop/tether recorder. Held here so it goes on recording
        #: whichever tab is open -- the operator is busy flying.
        self.recorder = None
        self.discovery: discovery.Discovery | None = None
        self._sites: list[SiteFrame] = []
        super().__init__()

    def save_settings(self) -> None:
        settings.save(self.settings)

    def vehicle_host(self) -> str | None:
        """The address typed on Monitoring, or None to search for the vehicle."""
        page = self.pages.get("monitor")
        try:
            typed = page.host_entry.get().strip()
        except Exception:
            typed = self.settings.get("vehicle_host", "")
        return typed or None

    # ------------------------------------------------------------------
    #  tabs
    # ------------------------------------------------------------------

    def build_tabs(self) -> None:
        from .healthpage import HealthPage
        from .logspage import LogsPage
        from .monitorpage import MonitorPage
        from .summarypage import SummaryPage
        from .transectpage import TransectPage
        from .transectsetup import TransectSetup

        # 1. Monitoring: the flight folder, then the vehicle path and the
        #    recorder side by side, then the live charts.
        tab = self.add_tab("Monitoring")
        body = self.scroll_body(tab)
        self._build_folder_card(body, row=0)
        left, right = self.pair(body, row=1)
        charts = ctk.CTkFrame(body, fg_color="transparent")
        charts.grid(row=2, column=0, sticky="ew")
        charts.grid_columnconfigure(0, weight=1)
        monitor = MonitorPage(tab, self, parents=(left, right, charts))
        self.mount("Monitoring", "monitor", monitor)

        # 2. Transects.
        tab = self.add_tab("Transects")
        self.mount("Transects", "transects", TransectSetup(tab, self))

        # 3. BlueOS logs.
        tab = self.add_tab("BlueOS logs")
        logs = LogsPage(tab, self)
        logs.grid(row=0, column=0, sticky="nsew")
        self.mount("BlueOS logs", "logs", logs)

        # 4. Flight summary: the report, then recording health, in one column.
        tab = self.add_tab("Flight summary")
        body = self.scroll_body(tab)
        summary = SummaryPage(body, self, scroll=False)
        summary.grid(row=0, column=0, sticky="ew")
        health = HealthPage(body, self, scroll=False)
        health.grid(row=1, column=0, sticky="ew")
        self.mount("Flight summary", "summary", summary)
        self.mount("Flight summary", "health", health)

        # 5. Analyze transects.
        tab = self.add_tab("Analyze transects")
        analyze = TransectPage(tab, self)
        analyze.grid(row=0, column=0, sticky="nsew")
        self.mount("Analyze transects", "analyze", analyze)

        monitor.refresh()

    # ------------------------------------------------------------------
    #  the flight folder
    # ------------------------------------------------------------------

    def _build_folder_card(self, body, row: int) -> None:
        c = Card(body, "1.  Flight folder",
                 "Choose it first, before the dive — even before the ROV is "
                 "connected. The flight recorder, the network checks, the "
                 "transect plan and every download from the Pi are filed "
                 "inside it.")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        r = ctk.CTkFrame(c.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="ew")
        r.grid_columnconfigure(0, weight=1)
        self.folder_entry = entry(r, "No folder selected", width=700)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(r, "Browse…", self._pick_folder, "primary", width=110
               ).grid(row=0, column=1)
        button(r, "Open", self._open_folder, "ghost", width=80
               ).grid(row=0, column=2, padx=(8, 0))
        self.found = output_box(c.body, wrap="none")
        self.found.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        c.add_grip(self.found, self.found.min_height)
        say(self.found, "Nothing selected yet.")

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select the flight folder")
        if d:
            self.use_flight(Path(d))

    def _open_folder(self) -> None:
        if self.flight_dir:
            self._reveal(self.flight_dir)

    def use_flight(self, path: Path) -> None:
        """Adopt a flight folder as the current one and rescan it."""
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

        self._arm_monitor()
        for pages in self._tab_pages.values():
            for page in pages:
                if hasattr(page, "refresh"):
                    try:
                        page.refresh()
                    except Exception as ex:
                        self._log(f"{type(page).__name__}.refresh failed: {ex}")

    def _arm_monitor(self) -> None:
        """Start watching for arming as soon as there is somewhere to write.

        Choosing the flight folder is the one step that always happens, and
        the monitor needs nothing else. Read-only: one small GET every two
        seconds, and nothing is recorded until the ROV is armed.
        """
        if not self.flight_dir:
            return
        try:
            from ..flightlog import FlightRecorder
            host = self.vehicle_host() or "192.168.2.2"
            if self.recorder is None:
                self.recorder = FlightRecorder(host=host, flight_dir=self.flight_dir)
            self.recorder.flight_dir = self.flight_dir
            if not self.recorder.watching:
                self.recorder.host = host
                self.recorder.start_watching()
                self._log("Monitoring: watching for the ROV to arm. The laptop "
                          "and tether will be recorded to logs/ for the length "
                          "of each flight.")
        except Exception as ex:
            self._log(f"Monitoring could not start: {ex}")

    # ------------------------------------------------------------------
    #  the survey plan -- edited on Transects, read everywhere
    # ------------------------------------------------------------------

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
            messagebox.showinfo(APP_NAME, "Choose a flight folder on Monitoring "
                                          "first — the transects are saved into it.")
            return
        try:
            out = plan_path(self.flight_dir, for_writing=True)
            self._plan().save(out)
            self._log(f"Saved transects to {out}")
            self.status.configure(text=f"Transects saved to {PLAN_FILENAME}.")
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

    def before_close(self) -> bool:
        rec = self.recorder
        if rec is not None and rec.status.state == "recording":
            if not messagebox.askyesno(
                APP_NAME,
                f"{rec.status.flight_id} is still being recorded.\n\n"
                f"Quitting will close it out — the parameters, versions and "
                f"deltas will be written now rather than when the ROV "
                f"disarms.\n\nQuit anyway?"
            ):
                return False
        if rec is not None:
            try:
                rec.stop_watching()
            except Exception:
                pass
        return True


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
