"""
ROV Flight Operations (working title) -- the desktop application.

Everything to do with the vehicle and what it recorded, in the order a survey
day uses it:

1. **Monitoring** -- where the operator sits before a flight. The flight folder
   is chosen here first, usually before the ROV is even connected, because it
   is where everything the program writes is filed.
2. **Navigation** -- where the operator sits *during* a flight: the map, the
   flight and power instruments, and the navigation suite. Cockpit is on the
   monitor above and has the camera; this has everything else.
3. **Transects** -- straight after the flight, with the vehicle disarmed on
   deck: type the transect times, save them, and check them against the dive
   profile.
4. **BlueOS logs** -- see what is on the Pi, download it into the flight
   folder, and clear old files off it.
5. **Flight summary** -- the day read back, and whether the recordings are
   sound.
6. **Analyze transects** -- per-transect CSVs and sensor health.

Imagery -- photos and video -- is ROV Imagery Processing, a separate program.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import diagnostics, discovery, settings
from ..config import AppConfig
from ..survey import PLAN_FILENAME, Site, SurveyPlan, plan_path
from . import theme as T
from .shell import Shell
from .widgets import Card, SiteFrame, button, entry, output_box, say

APP_NAME = "ROV Flight Operations"

log = logging.getLogger(__name__)


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
        #: The recorder's "close" request while the window waits on it.
        self._close_request = None
        self._close_began = 0.0
        self._close_asked_at = 0.0
        super().__init__()

    def save_settings(self) -> None:
        settings.save(self.settings)

    # ------------------------------------------------------------------
    #  what the banner's lamps and versions read
    # ------------------------------------------------------------------

    def version_host(self) -> str | None:
        """The vehicle the banner asks for its software versions.

        The committed address, not whatever is half-typed in the box, and
        read straight out of the settings rather than through
        `vehicle_host()`: this runs once a second, and that one commits the
        box as a side effect. A poll must not be able to change anything.
        """
        return self.settings.get("vehicle_host", "") or "192.168.2.2"

    def vehicle_status(self) -> tuple[bool, str]:
        """(is the vehicle answering, what the logging lamp shows).

        "Logging" covers both halves of what the recorder does, because they
        are not the same thing and the difference is worth seeing: it watches
        for the ROV to arm ("waiting"), and it writes rows once it has ("on").
        Between two transects the first is true and the second is not, which
        is exactly the state an operator glancing at the banner wants to be
        able to tell from a recorder that has stopped.
        """
        rec = self.recorder
        if rec is None:
            return False, "off"
        st = rec.status
        connected = bool(st.reachable)
        if st.state in ("starting", "recording", "closing"):
            return connected, "on"
        return connected, "waiting" if rec.watching else "off"

    def vehicle_host(self) -> str | None:
        """The committed vehicle address, or None to search for the vehicle.

        Committed, not whatever is half-typed in the box: the address is
        applied -- to the recorder, the logs tab and the preview together --
        when the box is left or Enter is pressed, so nothing reads a value the
        recorder is not also using.
        """
        page = self.pages.get("monitor")
        commit = getattr(page, "_remember_host", None)
        if callable(commit):
            # Clicking a button does not take focus from the box, so a typed
            # address is committed here too, before anything reads it.
            try:
                commit()
            except Exception:
                pass
        return self.settings.get("vehicle_host", "") or None

    def set_vehicle_host(self, typed: str) -> None:
        """Commit a new vehicle address everywhere at once."""
        typed = (typed or "").strip()
        if typed == self.settings.get("vehicle_host", ""):
            return
        self.settings["vehicle_host"] = typed
        self.save_settings()
        shown = typed or "192.168.2.2"
        rec = self.recorder
        if rec is not None and not rec.retarget(shown):
            self._log(f"Vehicle address set to {shown}. The flight being recorded "
                      f"keeps using {rec.host} until it closes.")
        else:
            self._log(f"Vehicle address set to {shown}.")
        logs = self.pages.get("logs")
        if logs is not None:
            logs.forget_vehicle()

    # ------------------------------------------------------------------
    #  tabs
    # ------------------------------------------------------------------

    def build_tabs(self) -> None:
        from .healthpage import HealthPage
        from .logspage import LogsPage
        from .monitorpage import MonitorPage
        from .navpage import NavigationPage
        from .summarypage import SummaryPage
        from .transectpage import TransectPage
        from .transectsetup import TransectSetup

        # 1. Monitoring: the flight folder, then the vehicle path and the
        #    recorder side by side, then the live charts.
        tab = self.add_tab("Monitoring", "monitoring")
        body = self.scroll_body(tab)
        self._build_folder_card(body, row=0)
        left, right = self.pair(body, row=1)
        charts = ctk.CTkFrame(body, fg_color="transparent")
        charts.grid(row=2, column=0, sticky="ew")
        charts.grid_columnconfigure(0, weight=1)
        monitor = MonitorPage(tab, self, parents=(left, right, charts))
        self.mount("Monitoring", "monitor", monitor)

        # 2. Navigation. Deliberately *not* inside a scrolling body: this is
        #    an instrument panel, and an instrument that can be scrolled off
        #    the screen is an instrument that will be, at the worst moment.
        tab = self.add_tab("Navigation", "navigation")
        nav = NavigationPage(tab, self)
        nav.grid(row=0, column=0, sticky="nsew")
        self.mount("Navigation", "navigation", nav)

        # 3. Transects.
        tab = self.add_tab("Transects", "transects")
        self.mount("Transects", "transects", TransectSetup(tab, self))

        # 4. BlueOS logs.
        tab = self.add_tab("BlueOS logs", "blueos_logs")
        logs = LogsPage(tab, self)
        logs.grid(row=0, column=0, sticky="nsew")
        self.mount("BlueOS logs", "logs", logs)

        # 5. Flight summary: the report, then recording health, in one column.
        tab = self.add_tab("Flight summary", "flight_summary")
        body = self.scroll_body(tab)
        summary = SummaryPage(body, self, scroll=False)
        summary.grid(row=0, column=0, sticky="ew")
        health = HealthPage(body, self, scroll=False)
        health.grid(row=1, column=0, sticky="ew")
        self.mount("Flight summary", "summary", summary)
        self.mount("Flight summary", "health", health)

        # 6. Analyze transects.
        tab = self.add_tab("Analyze transects", "analyze_transects")
        analyze = TransectPage(tab, self)
        analyze.grid(row=0, column=0, sticky="nsew")
        self.mount("Analyze transects", "analyze", analyze)

        monitor.refresh()

        # The recorder's state, on every tab. A recording that fails while the
        # operator is on BlueOS logs must not wait to be noticed until someone
        # opens Monitoring again.
        self.recorder_badge = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                           text_color=T.TEXT_MUTED, anchor="e")
        self.recorder_badge.grid(row=1, column=0, sticky="e", padx=18, pady=(8, 0))
        self.after(1000, self._update_badge)

    def _update_badge(self) -> None:
        try:
            rec = self.recorder
            st = rec.status if rec is not None else None
            if rec is None:
                text, color = "Not watching for the ROV", T.TEXT_MUTED
            elif st.problem:
                short = st.problem if len(st.problem) < 90 else st.problem[:87] + "…"
                text, color = f"⚠  {short}", T.WARN
            elif st.state == "recording":
                text, color = (f"●  Recording {st.flight_id}  ·  {st.rows:,} rows  ·  "
                                f"{rec.host}"), T.OK
            elif st.state == "starting":
                text, color = f"Starting {st.flight_id}…", T.TEXT
            elif st.state == "closing":
                text, color = f"Closing {st.flight_id}…", T.TEXT
            elif st.transition:
                text, color = f"{st.transition}…", T.TEXT
            elif not rec.watching:
                text, color = "Not watching for the ROV", T.TEXT_MUTED
            else:
                text, color = f"Watching {rec.host} for arming", T.TEXT_MUTED
            if st is not None and st.degraded and not st.problem:
                text, color = f"{text}  ·  ⚠ a recorder worker is stuck", T.WARN
            self.recorder_badge.configure(text=text, text_color=color)
        except Exception:
            diagnostics.log_exception("recorder badge", *sys.exc_info(),
                                      level=logging.WARNING)
        self.after(1000, self._update_badge)

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
        """Adopt a flight folder as the current one, resetting what belonged
        to the last one.

        A flight being recorded is not moved: it finishes in the folder it
        started in, and only the next flight goes to the new one -- the
        operator is told so and asked first. Transects typed for the previous
        flight are not silently carried over when the new folder has none.
        """
        new = Path(path)
        old = self.flight_dir
        rec = self.recorder
        if (rec is not None and rec.status.state in ("starting", "recording", "closing")
                and old is not None and new != old):
            if not messagebox.askyesno(
                APP_NAME,
                f"{rec.status.flight_id} is being recorded into\n{old}\n\n"
                f"It will finish there. Flights after it will be recorded "
                f"into\n{new}\n\nSwitch the flight folder?"):
                return
        self.flight_dir = new
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan(previous=old)

    def refresh_files(self) -> None:
        """Look at the flight folder again after files have arrived in it.

        Only what is on disk -- not the plan, which may hold unsaved edits.
        """
        self._discover()

    def _discover(self) -> None:
        """Walk the flight folder off the window's thread.

        A flight folder on a synchronized or external drive can take seconds
        to walk, and used to hold the window for all of them. The result is
        shown only if the folder is still the one chosen: switching flights
        while a walk is under way drops the old walk's result.
        """
        flight = self.flight_dir
        if not flight:
            return
        say(self.found, f"Reading {flight}…")

        def shown(disc) -> None:
            if flight != self.flight_dir:
                return
            if isinstance(disc, Exception):
                say(self.found, f"Could not read {flight}: {disc}")
                return
            self.discovery = disc
            say(self.found, disc.summary())
            for key in ("analyze", "health", "summary"):
                page = self.pages.get(key)
                if page is not None and hasattr(page, "refresh"):
                    try:
                        page.refresh()
                    except Exception as ex:
                        diagnostics.log_exception(f"{type(page).__name__}.refresh",
                                                  *sys.exc_info())
                        self._log(f"{type(page).__name__}.refresh failed: {ex}")

        self.background("flight-folder", lambda: discovery.discover(flight), shown)

    def _scan(self, previous: Path | None = None) -> None:
        if not self.flight_dir:
            return
        # The last flight's file list is not this one's, even for the moment
        # before the new walk finishes.
        self.discovery = None
        self._discover()

        saved = plan_path(self.flight_dir)
        if not saved.is_file() and previous is not None and previous != self.flight_dir:
            typed = [t for sf in self._sites for t in sf.to_site().transects
                     if t.start_tc or t.end_tc]
            if not typed or messagebox.askyesno(
                APP_NAME,
                f"{self.flight_dir.name} has no saved transects.\n\n"
                f"Clear the {len(typed)} transect(s) entered for "
                f"{previous.name}?\n\n(No keeps them, to save into the new "
                f"folder.)"):
                self._apply_plan(SurveyPlan([]))

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
                self.recorder.retarget(host)
                # Queued, not done here: opening the performance counters and
                # asking WMI about the battery is slow on a busy laptop.
                self.recorder.request("watch")
                self._log("Monitoring: watching for the ROV to arm. The laptop "
                          "and tether will be recorded to logs/ for the length "
                          "of each flight.")
        except Exception as ex:
            diagnostics.log_exception("starting monitoring", *sys.exc_info())
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
        """Close the recorder properly, with the window still running.

        Closing a flight reads every parameter off the vehicle, which can take
        a minute and a half against one that has stopped answering. That used
        to happen inside this callback, and the window froze -- "not
        responding" -- until it was done or somebody ended the program. Now
        the recorder closes on its own thread, the window shows how it is
        going, and it closes when the recorder is finished.
        """
        # The Navigation collector, the tile fetcher and the navigation log
        # first, and unconditionally: they are cheap to stop, none of them can
        # block, and doing it here means they are stopped even on the paths
        # below that return False and come back round again. Stopping twice is
        # harmless; leaving a collector polling a vehicle after the window has
        # gone is not.
        nav = self.pages.get("navigation")
        if nav is not None and not getattr(self, "_nav_stopped", False):
            self._nav_stopped = True
            try:
                nav.shutdown()
            except Exception:
                diagnostics.log_exception("stopping Navigation", *sys.exc_info(),
                                          level=logging.WARNING)

        rec = self.recorder
        waiting = self._close_request
        if waiting is not None and not waiting.is_set():
            if messagebox.askyesno(
                APP_NAME,
                f"Still closing the recorder: {rec.status.line()}\n\n"
                f"Close now without waiting? Rows already written are on the "
                f"disk, but this flight's record may be missing or "
                f"incomplete.", icon="warning", default="no"):
                log.warning("window closed by the operator before the recorder "
                            "finished closing: %s", rec.status.line())
                self.close_now()
            return False
        if rec is None:
            return True
        if rec.status.state in ("starting", "recording", "closing"):
            if not messagebox.askyesno(
                APP_NAME,
                f"{rec.status.flight_id} is still being recorded.\n\n"
                f"Quitting will close it out — the parameters, versions and "
                f"deltas will be written now rather than when the ROV "
                f"disarms.\n\nQuit anyway?"
            ):
                return False
        self._close_request = rec.request("close")
        self._close_began = time.monotonic()
        self._close_asked_at = 0.0
        self._log("Closing the recorder before the window closes…")
        self._await_close()
        return False

    #: How long the window waits on the recorder before asking whether to
    #: stop waiting. Longer than a closing snapshot that gets no answer.
    CLOSE_ASK_AFTER_S = 150.0

    def _await_close(self) -> None:
        req, rec = self._close_request, self.recorder
        if req is None or self._closing:
            return
        waited = time.monotonic() - self._close_began
        if req.is_set():
            st = rec.status
            if req.result is True:
                log.info("recorder closed cleanly in %.1f s; closing the window",
                         waited)
                self.close_now()
                return
            detail = st.degraded or st.problem or st.outcome or st.line()
            log.warning("recorder did not close cleanly after %.1f s: %s",
                        waited, detail)
            messagebox.showwarning(
                APP_NAME,
                f"The recorder did not finish closing cleanly:\n\n{detail}\n\n"
                f"Rows already written are on the disk. Check the flight's "
                f"logs folder; the diagnostics log has the details.")
            self.close_now()
            return
        self.status.configure(text=f"Closing: {rec.status.line()} "
                                   f"({waited:.0f} s)")
        if waited > self.CLOSE_ASK_AFTER_S and (
                time.monotonic() - self._close_asked_at > self.CLOSE_ASK_AFTER_S):
            self._close_asked_at = time.monotonic()
            if messagebox.askyesno(
                APP_NAME,
                f"The recorder has been closing for {waited:.0f} s and has not "
                f"finished: {rec.status.line()}\n\nClose the window anyway? "
                f"Rows already written are on the disk, but this flight's "
                f"record may be missing or incomplete.",
                icon="warning", default="no"):
                log.warning("window closed after waiting %.0f s for the "
                            "recorder: %s", waited, rec.status.line())
                self.close_now()
                return
        self.after(200, self._await_close)


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
    # First, so that anything that goes wrong from here on leaves a record.
    diagnostics.setup()
    ctk.set_default_color_theme("blue")
    try:
        app = App()
        app.mainloop()
    except BaseException:
        diagnostics.log_exception("the window", *sys.exc_info(),
                                  level=logging.CRITICAL)
        raise
    finally:
        log.info("---- %s exited ----", APP_NAME)


if __name__ == "__main__":
    main()
