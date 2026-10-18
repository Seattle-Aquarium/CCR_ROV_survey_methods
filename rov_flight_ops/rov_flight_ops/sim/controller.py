"""
Switching the ROV flight simulator on and off.

**Only with no vehicle.** The simulator will not start while a vehicle is
answering -- the banner's Vehicle connected lamp, the Navigation tab's link,
the DVL's stream, and a direct question to the vehicle's address all have to
say there is none -- and it switches itself off the moment one answers while
it is running, so a vehicle plugged in mid-simulation is never hidden behind a
simulated one.

**What switching it on does.** It builds a simulated world (`world`), its
DVL and UGPS topside on localhost, a simulated flight recorder, and the
Navigation tab's simulated collector, and puts them where the program looks
for the real ones. The program's flight folder becomes the simulator's own
(`sandbox`), so anything a page writes during the simulation goes there. The
real ones are kept, untouched and still running, and put back when it is
switched off: the flight folder chosen before, the recorder watching it, the
transects typed, the survey plan drawn, the versions in the banner.

**What it never does.** Nothing reaches a vehicle -- every simulated address
is localhost or a name that cannot resolve -- and nothing is written outside
the simulator's own folder.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import sandbox
from .schedule import CATALOG, DVL, MONITOR_CHANNELS, NAV
from .world import SimWorld

log = logging.getLogger(__name__)

#: How often a real vehicle is looked for while the simulator runs.
PROBE_EVERY_S = 5.0
PROBE_TIMEOUT_S = 1.2

#: The software the simulated vehicle runs: what the 15 October 2026 dive's
#: record says, and the Cockpit release flown that autumn.
VERSIONS = {"blueos": "1.4.6", "ardusub": "4.5.7", "cockpit": "1.19.0-beta.2"}

#: Tabs that read recordings already on disk, which the simulator does not make.
POST_FLIGHT_TABS = ("Flight summary", "Analyze transects")

#: The tab each fault channel lives on.
TAB_CHANNEL = {"Navigation": NAV, "DVL": DVL}


def vehicle_answers(host: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Does a vehicle answer at `host`? A single small read-only GET."""
    from .. import blueos
    try:
        armed, ms = blueos.read_arm_state(host, timeout=timeout)
    except Exception:
        return False
    return armed is not None or ms is not None


class Simulator:
    """The simulator, bound to the application window."""

    def __init__(self, app) -> None:
        self.app = app
        self.active = False
        self.world: SimWorld | None = None
        self.folder: Path | None = None
        self.recorder = None
        self.dvl = None
        self.dvl_device = None
        self.g2 = None
        self.ugps = None
        self.collector = None
        self._saved: dict = {}
        self._revealed: int | None = None
        self._told: set = set()
        self._probe_at = 0.0
        self._probing = False
        self._found_vehicle = threading.Event()
        self.started_at = 0.0

    # ------------------------------------------------------------------
    #  may it start?
    # ------------------------------------------------------------------

    def blockers(self) -> list[str]:
        """Reasons it cannot start now, from what the program already knows."""
        app = self.app
        out = []
        if app.busy:
            out.append("a job is running — wait for it to finish, or press Stop")
        connected, _logging = app.vehicle_status()
        if connected:
            out.append(f"the vehicle at {app.version_host()} is answering "
                       f"(the Vehicle connected lamp is lit)")
        nav = app.pages.get("navigation")
        col = getattr(nav, "collector", None)
        if col is not None and not getattr(col, "world", None):
            link = col.snapshot().link
            if link.mode == "live" and link.connected:
                out.append("the Navigation tab is reading a live vehicle")
        real_dvl = app.dvl
        if real_dvl is not None:
            snap = real_dvl.live.snapshot(history=False)
            if snap.tcp_connected:
                out.append(f"the DVL at {snap.address} is streaming")
        return out

    # ------------------------------------------------------------------
    #  on
    # ------------------------------------------------------------------

    def engage(self) -> None:
        """Build the simulated vehicle and put it where the program looks.

        Called on the window's thread, after the vehicle's address has been
        asked and nothing answered.
        """
        if self.active:
            return
        from ..ugps import UgpsCapture
        from .dvl import FieldDvl, SimDvlRecorder
        from .g2 import FieldG2
        from .recorder import SimRecorder

        app = self.app
        world = SimWorld()
        self.world = world
        try:
            self.folder = sandbox.prepare(world)
            self.started_at = time.monotonic()
            self.dvl_device = FieldDvl(world).start()
            self.g2 = FieldG2(world).start()
            self.recorder = SimRecorder(world, flight_dir=self.folder).start()
            rec = self.recorder
            try:
                hz = float(app.settings.get("dvl_snapshot_hz", 2.0))
            except (TypeError, ValueError):
                hz = 2.0
            self.dvl = SimDvlRecorder(world, self.dvl_device,
                                      snapshot_hz=min(hz, 2.0) or 1.0,
                                      flight_id=lambda: rec.status.flight_id)
            self.dvl.use_folder(None)
            self.ugps = UgpsCapture(self.g2.host, flight_id=lambda: rec.status.flight_id)
            self.ugps.ensure_running()
        except Exception:
            # Nothing has been swapped in yet: stop what started, and say why.
            self._stop_parts()
            raise

        # What is put back afterwards.
        s = self._saved = {
            "flight_dir": app.flight_dir,
            "recorder": app.recorder,
            "discovery": app.discovery,
            "versions": dict(app._versions),
            "plan": app._plan(),
            "folder_text": app.folder_entry.get(),
        }
        app.flight_dir = self.folder
        app.recorder = rec
        app.discovery = None
        self.active = True
        app._versions.update(VERSIONS)
        app._show_versions()
        app.folder_entry.delete(0, "end")
        app.folder_entry.insert(0, str(self.folder))
        was = s["flight_dir"]
        app._log("The flight folder is now the simulator's own, emptied each "
                 "time it starts. "
                 + (f"Yours ({was}) is put back when it is switched off." if was
                    else "None was chosen before; none will be afterwards."))
        from ..survey import SurveyPlan, plan_path
        app._apply_plan(SurveyPlan.load(plan_path(self.folder)))

        from .nav import SimNavCollector
        nav = app.pages.get("navigation")
        if nav is not None:
            self.collector = SimNavCollector(world, on_event=nav._collector_event)
            nav.enter_simulation(self.collector, sandbox.nav_plan(), self.folder)
        logs = app.pages.get("logs")
        if logs is not None:
            logs.reset_listing("Simulated vehicle — search to list what is on its Pi.")
        self._show_post_flight_notes(True)
        self._revealed = None
        self._told.clear()
        for key in ("monitor", "dvl", "transects"):
            page = app.pages.get(key)
            if page is not None and hasattr(page, "refresh"):
                try:
                    page.refresh()
                except Exception:
                    log.exception("refreshing %s for the simulator", key)
        log.info("ROV flight simulator on: %s", self.folder)
        app._log("ROV flight simulator ON. Every tab now shows a simulated flight "
                 "off Pier 59. One thing on each live tab goes wrong for 30 s, "
                 "then 15 s all green, then the next — press Show what's wrong "
                 "in the banner to be told what it is. Nothing reaches a vehicle "
                 f"and nothing is written outside {self.folder}.")

    # ------------------------------------------------------------------
    #  off
    # ------------------------------------------------------------------

    def disengage(self, why: str = "") -> None:
        if not self.active:
            return
        app = self.app
        self.active = False
        s = self._saved
        nav = app.pages.get("navigation")
        if nav is not None:
            try:
                nav.leave_simulation()
            except Exception:
                log.exception("restoring the Navigation tab")
        self._stop_parts()

        app.flight_dir = s.get("flight_dir")
        app.recorder = s.get("recorder")
        app.discovery = s.get("discovery")
        app._versions.clear()
        app._versions.update(s.get("versions") or {})
        app._show_versions()
        app.folder_entry.delete(0, "end")
        if app.flight_dir:
            app.folder_entry.insert(0, str(app.flight_dir))
        plan = s.get("plan")
        if plan is not None:
            app._apply_plan(plan)
        if app.flight_dir:
            app.refresh_files()
        logs = app.pages.get("logs")
        if logs is not None:
            logs.reset_listing("The simulator is off — search to list the real vehicle.")
        self._show_post_flight_notes(False)
        self.recorder = self.dvl = self.dvl_device = self.g2 = self.ugps = None
        self.collector = None
        for key in ("monitor", "dvl", "transects"):
            page = app.pages.get(key)
            if page is not None and hasattr(page, "refresh"):
                try:
                    page.refresh()
                except Exception:
                    log.exception("refreshing %s after the simulator", key)
        log.info("ROV flight simulator off%s", f": {why}" if why else "")
        app._log("ROV flight simulator OFF" + (f" — {why}" if why else "") + ".")

    def _stop_parts(self) -> None:
        """Stop every simulated part that was started, off the window's thread:
        the DVL capture can take seconds to join its readers."""
        parts = (("recorder", self.recorder, lambda p: p.shutdown()),
                 ("UGPS reader", self.ugps, lambda p: p.shutdown(wait=2.0)),
                 ("DVL capture", self.dvl, lambda p: p.shutdown(timeout=6.0)),
                 ("DVL", self.dvl_device, lambda p: p.stop()),
                 ("G2", self.g2, lambda p: p.stop()))
        world = self.world

        def stop_all() -> None:
            for what, part, stop in parts:
                if part is None:
                    continue
                try:
                    stop(part)
                except Exception:
                    log.exception("stopping the simulated %s", what)
            if world is not None:
                world.stopped.set()

        threading.Thread(target=stop_all, daemon=True, name="sim-stop").start()

    # ------------------------------------------------------------------
    #  once a second, from the banner
    # ------------------------------------------------------------------

    def tick(self) -> str:
        """Housekeeping while running. Returns a reason it must stop, or ""."""
        if not self.active:
            return ""
        if self._found_vehicle.is_set():
            self._found_vehicle.clear()
            return f"a vehicle answered at {self.app.version_host()}"
        real = self._saved.get("recorder")
        if real is not None and getattr(real, "watching", False) and real.status.reachable:
            return f"a vehicle answered at {real.host}"
        now = time.monotonic()
        if not self._probing and now - self._probe_at >= PROBE_EVERY_S:
            self._probe_at = now
            self._probing = True
            host = self.app.version_host()

            def probe() -> None:
                try:
                    if vehicle_answers(host):
                        self._found_vehicle.set()
                finally:
                    self._probing = False

            threading.Thread(target=probe, daemon=True, name="sim-probe").start()
        nav = self.app.pages.get("navigation")
        if nav is not None:
            try:
                nav.follow_simulated_lane(self.world.vehicle())
            except Exception:
                log.debug("following the simulated lane", exc_info=True)
        return ""

    # ------------------------------------------------------------------
    #  the banner's fault line
    # ------------------------------------------------------------------

    def channel_for(self, tab: str | None) -> str | None:
        if tab == "Monitoring":
            page = self.app.pages.get("monitor")
            group = getattr(page, "_group", None)
            return group if group in MONITOR_CHANNELS else None
        return TAB_CHANNEL.get(tab or "")

    def banner(self, tab: str | None) -> tuple[str, str, str]:
        """(what the banner says, its tone, the reveal button's text or "").

        Tones are "fault", "green" and "muted".
        """
        if not self.active or self.world is None:
            return "", "muted", ""
        ph = self.world.schedule.phase(self.world.t())
        left = max(0, int(round(ph.left)))
        if not ph.faulty:
            return f"All green · next fault in {left} s", "green", ""
        channel = self.channel_for(tab)
        if channel is None:
            return (f"Faults running on Monitoring, Navigation and DVL · {left} s",
                    "muted", "")
        if self._revealed != ph.cycle:
            return f"A simulated fault is active · {left} s", "fault", "Show what's wrong"
        fault = self.world.schedule.fault_for(channel, ph.cycle)
        return f"{channel}: {fault.title} · {left} s", "fault", "Hide"

    def toggle_reveal(self, tab: str | None) -> None:
        if self.world is None:
            return
        ph = self.world.schedule.phase(self.world.t())
        if not ph.faulty:
            return
        if self._revealed == ph.cycle:
            self._revealed = None
            return
        self._revealed = ph.cycle
        channel = self.channel_for(tab)
        if channel is None:
            return
        fault = self.world.schedule.fault_for(channel, ph.cycle)
        key = (ph.cycle, channel)
        if fault is not None and key not in self._told:
            self._told.add(key)
            self.app._log(f"Simulated fault — {channel}: {fault.title}. {fault.detail}")

    def all_faults(self) -> dict[str, list[str]]:
        return {ch: [f.title for f in fs] for ch, fs in CATALOG.items()}

    # ------------------------------------------------------------------

    def _show_post_flight_notes(self, on: bool) -> None:
        """A line on the tabs that read recordings, which the simulator does not make."""
        import customtkinter as ctk

        from ..gui import theme as T
        notes = self._saved.setdefault("_notes", {})
        for tab in POST_FLIGHT_TABS:
            frame = self.app.nav._page_frames.get(tab)
            if frame is None:
                continue
            note = notes.get(tab)
            if on:
                if note is None:
                    note = ctk.CTkLabel(
                        frame, font=T.FONT_SMALL, text_color=T.WARN, anchor="w",
                        justify="left",
                        text="ROV flight simulator: this tab reads recordings "
                             "already on disk — mcaps, dataflash logs, the "
                             "laptop monitor's CSVs — and the simulator writes "
                             "none, so it shows the folder chosen before the "
                             "simulator was switched on, or nothing.")
                    notes[tab] = note
                note.grid(row=1, column=0, sticky="ew", padx=4, pady=(6, 0))
            elif note is not None:
                note.grid_remove()


def network_report(host: str = "192.168.2.2") -> str:
    """What Check the network says in simulation: a healthy station, labelled."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    return (
        f"SIMULATED — the ROV flight simulator has no tether to read. This is what\n"
        f"a healthy station's check looks like.\n\n"
        f"Topside network - {stamp} UTC\n"
        f"Machine: this laptop    Vehicle: {host}\n"
        f"Power plan: Balanced\nRunning on: AC  (100%)\n\n"
        f"Route to the vehicle: Network Bridge  (Microsoft Network Adapter "
        f"Multiplexor Driver)\n"
        f"  ** This is a Windows network bridge, not a physical adapter. **\n"
        f"     Its carrier state and link speed are the bridge's own and say "
        f"nothing\n     about the adapter underneath it.\n"
        f"  Bridge members: ROV_nereo, UGPS\n\n"
        f"Interfaces\n" + "-" * 72 + "\n"
        f"- ROV_nereo\n    Realtek USB GbE Family Controller\n"
        f"    state up | carrier connected | 100 Mbps | mtu 1500\n"
        f"    since it came up: in 1567.6 MiB, out 122.0 MiB, 0 rx errors, "
        f"0 tx errors, 0 discards   (clean)\n"
        f"    allow the computer to turn off this device: no   (good)\n\n"
        f"- UGPS\n    Intel(R) Ethernet Connection (4) I219-LM\n"
        f"    state up | carrier connected | 1000 Mbps | mtu 1500\n"
        f"    since it came up: in 92.1 MiB, out 33.6 MiB, 0 rx errors, "
        f"0 tx errors, 0 discards   (clean)\n\n"
        f"Vehicle {host}: ARP resolved, ping 4 ms, 0 of 4 lost.\n")
