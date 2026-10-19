"""
Monitoring: what the laptop and the tether are doing, while they do it.

The page is in two halves. The top one is the recorder -- whether it is
watching, what it is writing, and where. The bottom one is seven small charts,
one group of readings at a time, updating once a second.

**Small multiples rather than one chart.** A first version drew every reading
in a group on shared axes, and it was useless: in the memory group alone the
values run from 1.2 (pagefile percent) to 700 (pages per second), so six of
the seven series were flat lines along the bottom while one filled the frame.
Each reading gets its own strip and its own scale here, with its current value
beside it. Comparing shapes is what this is for -- *did the frequency drop when
the temperature rose* -- and shapes survive separate scales.

**Coloured by level, not by series.** Each strip used to take the next colour
in a cycle, which said nothing. Now every sample is coloured by how the reading
compares with a healthy flight -- green normal, amber unusual, red bad, from
`bands` -- with a stripe and a word beside the current value and the
thresholds dashed across the strip, so a group can be scanned for trouble in
one look and the reason is under each name.

**Drawn on a Tk canvas, not with a plotting library.** The whole point is that
this stays light enough to leave running for a survey day on a field laptop.
Seven polylines and a few labels, redrawn once a second, cost well under a
millisecond; matplotlib would have brought a figure pipeline and a redraw
measured in tens. The series is also decimated to the pixel width before it is
drawn, so an hour of data costs the same as a minute.

**Nothing here drives the recording.** The page reads a `FlightRecorder`'s
history and status. Closing the page, or never opening it, changes nothing
about what is written -- which matters, because the operator is flying and is
not going to be looking at this.
"""

from __future__ import annotations

import bisect
import time
import tkinter
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import bands, flightlog, laptop, netdiag, nettrace
from ..sim.laptop import NOT_ON_FIELD_LAPTOP
from . import theme as T
from .widgets import (
    Card,
    Repaint,
    button,
    entry,
    fit_wrap,
    label,
)

#: How far back the charts look. A survey transect is a few minutes, so the
#: default shows one whole transect and its approach.
WINDOWS = {"2 min": 120, "10 min": 600, "30 min": 1800}
DEFAULT_WINDOW = "10 min"

#: One strip per reading, in logical pixels -- everything drawn on this
#: canvas is multiplied by the display's scaling, because a raw Tk canvas is
#: not scaled by CustomTkinter and Tk reports 96 DPI regardless. Unscaled, on
#: the 250 % screen this was redrawn on, the strips came out at two-fifths of
#: their size and the axis text was too small to read.
#:
#: Tall enough for a name, its unit and rate, its thresholds, a value and a
#: line that can be read at arm's length on a laptop in daylight.
ROW_H = 86
#: Room at the left for the reading's name and thresholds, and for its value.
NAME_W = 236
VALUE_W = 136
#: Kept clear on the right for the strip's own axis labels and its unit.
AXIS_W = 124
#: The space between one strip's box and the next.
STRIP_GAP = 6
#: The shared time axis along the foot.
TIME_H = 30
#: Space above and below the line inside each strip.
PAD_Y = 11

FONT_NAME = (T.MONO, 11)
FONT_VALUE = (T.MONO, 15, "bold")
FONT_LEVEL = (T.FAMILY_SEMIBOLD, 10)
FONT_AXIS = (T.FAMILY, 10)
FONT_NOTE = (T.FAMILY, 9)
FONT_TIP = (T.FAMILY, 10)

#: Colours that are not a reading's level, as (light, dark).
GRID_FAINT = ("#E4E8EE", "#16304F")
#: Each strip's box, and its edge: a strip should read as one thing, and the
#: strip under it as another.
STRIP_FILL = ("#F3F6F9", "#0F2643")
STRIP_EDGE = ("#C9D2DD", "#29507A")
#: A line for a reading with no thresholds -- not a status colour.
PLAIN_LINE = ("#4F6D8F", "#9FB4C7")
#: The simulator's own colour, for the strips it fills that L342-D cannot.
SIM_MARK = ("#5B3F8C", "#B9A2E6")

#: What a reading with no unit of its own is counted in, for the axis.
COUNT_UNITS = {
    "disk_queue_length": "requests",
    "network_packets_discarded": "packets",
    "cockpit_process_count": "processes",
    "cockpit_handle_count": "handles",
    "cockpit_thread_count": "threads",
    "pi_eth_carrier_drops": "drops",
}


def axis_scale(column: str, top: float) -> tuple[float, str]:
    """(divisor, unit) for a strip's axis: bytes in KiB/MiB/GiB and uptime
    in minutes or hours, so the labels are plain numbers and the unit is said
    once, beside them."""
    unit = laptop.UNITS.get(column, "")
    if unit == "bytes":
        for size, suffix in ((2 ** 30, "GiB"), (2 ** 20, "MiB"), (2 ** 10, "KiB")):
            if abs(top) >= size:
                return float(size), suffix
        return 1.0, "bytes"
    if column == "system_uptime_s":
        return (3600.0, "h") if abs(top) >= 2 * 3600 else (60.0, "min")
    if column in BOOL_COLUMNS:
        return 1.0, "state"
    if unit == "C":
        return 1.0, "°C"
    return 1.0, unit or COUNT_UNITS.get(column, "")


def axis_number(v: float) -> str:
    if v == int(v) and abs(v) < 1e6:
        return f"{int(v):,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


#: Readings that are TRUE or FALSE, drawn on a fixed two-value axis.
BOOL_COLUMNS = frozenset({"nic_carrier", "phy_carrier", "nic_low_power",
                          "rov_arp_ok", "tether_remote_seen"})

#: The least a strip's scale may span, as a share of the reading's size.
MIN_SPAN_SHARE = 0.06

#: How much history before the window a band can need: the longest smoothing
#: or counting window in `bands`.
BAND_MEMORY_S = 70.0

#: How much coarser the lines are drawn while the window is being resized.
#: The shape is what these charts are for and the shape survives it; the
#: accurate line is drawn as soon as the edge is let go.
DRAG_DECIMATION = 3

#: How often each reading actually changes, where that is not the 1 Hz row.
#: The row is written once a second, but some of what goes into it is read on a
#: slower cadence and held forward between reads -- a strip that looks flat for
#: five seconds and then steps is doing exactly what it should.
SLOWER_THAN_ROW = {
    "rov_armed": flightlog.ARM_POLL_S,
    "rov_reachable": flightlog.ARM_POLL_S,
    "rov_http_ms": flightlog.ARM_POLL_S,
    "pi_soc_temp_c": flightlog.PI_POLL_S,
    "pi_eth_rx_bytes": flightlog.TETHER_POLL_S,
    "pi_eth_rx_errors": flightlog.TETHER_POLL_S,
    "pi_eth_carrier": flightlog.TETHER_POLL_S,
    "pi_eth_carrier_drops": flightlog.TETHER_POLL_S,
    "tether_link_mbps": flightlog.TETHER_POLL_S,
    "tether_tx_mbps": flightlog.TETHER_POLL_S,
    "tether_rx_mbps": flightlog.TETHER_POLL_S,
    "tether_remote_seen": flightlog.TETHER_POLL_S,
    "battery_discharge_w": laptop.SLOW_REFRESH_S,
    "ethernet_connected": laptop.SLOW_REFRESH_S,
    "ethernet_link_speed_mbps": laptop.SLOW_REFRESH_S,
    "rov_arp_ok": laptop.ARP_REFRESH_S,
}


def refresh_hz(column: str) -> float:
    """How many times a second a reading's value is actually refreshed."""
    return 1.0 / SLOWER_THAN_ROW.get(column, laptop.DEFAULT_PERIOD_S)


def hz_text(hz: float) -> str:
    return f"{hz:g} Hz"


def achieved_hz(history, seconds: float = 60.0) -> float | None:
    """The row rate actually achieved over the last minute, or None.

    The labels are targets; this is the measurement. A laptop that cannot
    keep up shows it here as a number below one.
    """
    try:
        times = [t for t, _v in history.series("elapsed_time_s")]
    except Exception:
        return None
    if len(times) < 5:
        return None
    recent = [t for t in times if t >= times[-1] - seconds]
    if len(recent) < 5 or recent[-1] <= recent[0]:
        return None
    return (len(recent) - 1) / (recent[-1] - recent[0])


#: The fast network trace runs beside the row while a flight records. It is
#: written to its own files for reading afterwards and is not drawn here.
FAST_TRACE_NOTE = (
    f"While a flight records, the tether is also traced faster than this: "
    f"adapter counters at {1 / nettrace.FAST_PERIOD_S:g} Hz "
    f"(logs/network_fast_*.csv) and pings at {1 / nettrace.ECHO_PERIOD_S:g} Hz "
    f"(logs/network_pings_*.csv). Those are for afterwards and are not drawn "
    f"here.")


def _short_status(st) -> str:
    """The recorder's state in a few words; the flight id is on every tab's
    status line already, and the whole story is in the log."""
    if st.problem:
        return st.problem if len(st.problem) < 60 else st.problem[:57] + "…"
    if st.state == "recording":
        mins = (time.time() - st.started) / 60 if st.started else 0
        tail = " · by hand" if st.by_hand else (
            f" · closing in {max(0, flightlog.DISARM_GRACE_S - st.since_disarm):.0f} s"
            if st.since_disarm is not None else "")
        return f"Recording · {st.rows:,} rows · {mins:.1f} min{tail}"
    if st.state == "starting":
        return "Starting…"
    if st.state == "closing":
        return "Closing — reading the vehicle…"
    if st.transition:
        return f"{st.transition}…"
    return "Waiting for the vehicle" if st.armed is None else "Waiting for the ROV to arm"


def _set_text(widget, text: str, color) -> None:
    if getattr(widget, "_shown", None) != (text, color):
        widget._shown = (text, color)
        widget.configure(text=text, text_color=color)


class MonitorPage(ctk.CTkFrame):
    """The system at a glance, the flight (folder, recording, vehicle), and
    the live charts.

    Built into whatever parents the Monitoring tab gives it -- the glance on
    the left, the flight on the right, the charts beneath -- or, given none,
    into a scrolling column of its own. Every message goes to the window's
    log at the foot of the program: this tab has no report boxes of its own.
    """

    def __init__(self, master, app, parents=None):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._recorder = None
        self._group = next(iter(laptop.GROUPS))
        self._window = DEFAULT_WINDOW
        self._ticking = False
        #: column -> (history version, the series as drawn). The rows change
        #: once a second; the width changes as fast as a window edge moves,
        #: and rebuilding seven series of nine hundred samples for a width
        #: change is the same answer at a real cost.
        self._series_cache: dict[str, tuple] = {}
        self._series_from = None
        self._rows: list[dict] = []
        self._times: list[float] = []
        self._rows_at = None
        #: Readings already brought up to date with the current rows.
        self._fresh: set[str] = set()
        #: Tk fonts for measuring, by (font, scale).
        self._fonts: dict = {}
        #: Where the last redraw put things, for the hover tip.
        self._geom: dict = {}
        #: (reading, x, y) under the pointer, or None.
        self._hover = None

        if parents is None:
            body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
            body.grid(row=0, column=0, sticky="nsew")
            body.grid_columnconfigure(0, weight=1)
            parents = (body, body, body)
            rows = (0, 1, 2)
        else:
            rows = (0, 0, 0)
        board_parent, flight_parent, chart_parent = parents
        from .goboard import GoBoard
        self.board = GoBoard(board_parent)
        self.board.grid(row=rows[0], column=0, sticky="nsew", pady=(0, 12))
        self._build_flight_card(flight_parent, rows[1])
        self._build_chart_card(chart_parent, rows[2])

    # ------------------------------------------------------------------
    #  the flight: folder, recording, vehicle
    # ------------------------------------------------------------------

    def _build_flight_card(self, parent, row: int) -> None:
        """Three lines, one per thing done here: choose the folder, start or
        stop the recording, check the path to the vehicle."""
        c = Card(parent, "2.  Flight")
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 12))
        b = c.body
        b.grid_columnconfigure(1, weight=1)
        for r, text in enumerate(("Folder", "Recording", "Vehicle")):
            label(b, text, muted=True).grid(row=r, column=0, sticky="w",
                                           padx=(0, 12), pady=3)

        builder = getattr(self.app, "build_folder_row", None)
        if callable(builder):
            builder(b, row=0, column=1)

        r = ctk.CTkFrame(b, fg_color="transparent")
        r.grid(row=1, column=1, sticky="ew", pady=3)
        r.grid_columnconfigure(3, weight=1)
        self.watch_btn = button(r, "Start monitoring", self._toggle_watch,
                                "primary", width=150)
        self.watch_btn.grid(row=0, column=0)
        self.manual_btn = button(r, "Record now", self._toggle_manual, "ghost",
                                 width=110)
        self.manual_btn.grid(row=0, column=1, padx=(6, 0))
        button(r, "Logs", self._open_logs, "ghost", width=60
               ).grid(row=0, column=2, padx=(6, 0))
        # One line: what the recorder is doing. Everything longer goes to the
        # log at the foot of the window.
        self.state_label = ctk.CTkLabel(r, text="Not watching.", font=T.FONT_SMALL,
                                        text_color=T.TEXT_MUTED, anchor="w")
        self.state_label.grid(row=0, column=3, sticky="ew", padx=(10, 0))

        # The one place the vehicle's address is typed. Everything that talks
        # to the ROV -- the recorder, the logs tab, the preview -- reads it.
        hr = ctk.CTkFrame(b, fg_color="transparent")
        hr.grid(row=2, column=1, sticky="ew", pady=3)
        self.host_entry = entry(hr, "192.168.2.2 (blank = tether)", width=170)
        self.host_entry.grid(row=0, column=0)
        saved = getattr(self.app, "settings", {}).get("vehicle_host", "")
        if saved:
            self.host_entry.insert(0, saved)
        self.host_entry.bind("<FocusOut>", lambda _e: self._remember_host())
        self.host_entry.bind("<Return>", lambda _e: self._remember_host())
        button(hr, "Check the network", self._check_network, "ghost",
               width=140).grid(row=0, column=1, padx=(6, 0))
        button(hr, "Measure the link", self._calibrate, "ghost", width=130
               ).grid(row=0, column=2, padx=(6, 0))
        button(hr, "Save", self._save_network, "ghost", width=60
               ).grid(row=0, column=3, padx=(6, 0))
        self._net_text = ""
        self._capabilities_said: tuple = ()

    def _say(self, text: str) -> None:
        """To the log at the foot of the window, where every message goes."""
        log_to = getattr(self.app, "_log", None)
        if callable(log_to):
            log_to(text)

    def _remember_host(self) -> None:
        setter = getattr(self.app, "set_vehicle_host", None)
        if callable(setter):
            setter(self.host_entry.get())

    def _check_network(self) -> None:
        host = self._host()
        simulating = getattr(self.app, "simulating", False)

        def work(progress, cancel):
            if simulating:
                from ..sim.controller import network_report
                return network_report(host)
            return netdiag.report(host)

        self.app.submit(work, "Reading the topside network…",
                        on_done=self._network_done)

    def _calibrate(self) -> None:
        host = self._host()
        self._say("Watching the tether's counters for four seconds — leave the "
                  "video running.")

        simulating = getattr(self.app, "simulating", False)

        def work(progress, cancel):
            if simulating:
                return ("SIMULATED — the ROV flight simulator has no tether to "
                        "measure. On a real station this watches the adapter "
                        "counters for four seconds with the video running and "
                        "says whether they move fast enough for the fast trace "
                        "to mean anything on this laptop.")
            return nettrace.calibration_text(nettrace.calibrate(host))

        self.app.submit(work, "Measuring the link…",
                        on_done=self._network_done)

    def _network_done(self, result) -> None:
        if result is None or isinstance(result, Exception):
            self._say(f"Could not read the network: {result}")
            return
        self._net_text = str(result)
        self._say(self._net_text)

    def _save_network(self) -> None:
        """Keep the check beside the flight it was taken for.

        Named by the moment it was taken rather than by the flight, because
        the useful one is often the check run *before* a flight exists.
        """
        if not self._net_text:
            messagebox.showinfo("Nothing to save",
                                "Run Check the network or Measure the link "
                                "first.")
            return
        folder = self._logs_folder()
        if folder is None:
            messagebox.showinfo(
                "No flight folder",
                "Choose a flight folder on Monitoring first — this "
                "saves into its logs folder.")
            return
        from datetime import datetime
        name = f"network_check_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(self._net_text, encoding="utf-8")
        except OSError as ex:
            messagebox.showerror("Could not save", str(ex))
            return
        self._say(f"Network check saved to logs\\{name}.")

    def _logs_folder(self) -> Path | None:
        rec = self.recorder
        if rec is None or not getattr(rec, "flight_dir", None):
            return None
        return Path(rec.flight_dir) / "logs"

    def _build_chart_card(self, body, row: int) -> None:
        c = Card(body, "3.  Live monitoring",
                 "Colour compares with a healthy flight: ● normal (green), "
                 "▲ unusual (amber), ✖ bad (red). Dashed lines are the "
                 "thresholds; point at a name for details.")
        c.grid(row=row, column=0, sticky="ew")
        c.body.grid_columnconfigure(0, weight=1)

        strip = ctk.CTkFrame(c.body, fg_color="transparent")
        strip.grid(row=0, column=0, sticky="ew")
        self.group_pick = ctk.CTkSegmentedButton(
            strip, values=list(laptop.GROUPS), command=self._pick_group,
            font=T.FONT_SECTION, fg_color=T.SURFACE_ALT,
            selected_color=T.SEGMENT_SELECTED, selected_hover_color=T.SEGMENT_SELECTED_HOVER,
            unselected_color=T.SURFACE_ALT, unselected_hover_color=T.BORDER,
            text_color=T.TEXT)
        self.group_pick.set(self._group)
        self.group_pick.grid(row=0, column=0, sticky="w")

        self.window_pick = ctk.CTkSegmentedButton(
            strip, values=list(WINDOWS), command=self._pick_window,
            font=T.FONT_SMALL, fg_color=T.SURFACE_ALT,
            selected_color=T.SURFACE, selected_hover_color=T.BORDER,
            unselected_color=T.SURFACE_ALT, unselected_hover_color=T.BORDER,
            text_color=T.TEXT_MUTED)
        self.window_pick.set(self._window)
        self.window_pick.grid(row=0, column=1, sticky="e", padx=(18, 0))

        self.group_note = ctk.CTkLabel(
            c.body, text=laptop.GROUP_NOTES.get(self._group, ""),
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
            justify="left", wraplength=880)
        self.group_note.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        fit_wrap(c.body, self.group_note)
        self._pick_group(self._group, redraw=False)

        self.canvas = tkinter.Canvas(
            c.body, highlightthickness=1, borderwidth=0,
            height=int(ROW_H * 7 * (T.scale_of(c.body) or 1.0)),
            background=self._apply_appearance_mode(T.FIELD_BG),
            highlightbackground=self._apply_appearance_mode(T.BORDER))
        self.canvas.grid(row=2, column=0, sticky="ew")
        # Seven strips is a few hundred canvas items. Rebuilding them on every
        # <Configure> a window drag delivers is the single most expensive
        # thing on this tab; once a frame is as often as a screen can show it.
        self._repaint = Repaint(self, self._redraw)
        self._canvas_at: tuple[int, int] | None = None
        self.canvas.bind("<Configure>", self._canvas_configured)
        self.canvas.bind("<Motion>", self._hovered)
        self.canvas.bind("<Leave>", self._unhovered)

    # ------------------------------------------------------------------
    #  wiring
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Called when the rail raises this page."""
        self._sync_buttons()
        self._redraw()
        if not self._ticking:
            self._ticking = True
            self._tick()

    @property
    def recorder(self):
        """The application's recorder, made on first use.

        Held on the application rather than on this page so that it keeps
        running when the operator navigates away -- which they will, because
        the whole point is to be flying while this records.
        """
        rec = getattr(self.app, "recorder", None)
        if rec is None:
            from ..flightlog import FlightRecorder
            rec = FlightRecorder(host=self._host(),
                                 flight_dir=self.app.flight_dir)
            self.app.recorder = rec
        rec.flight_dir = self.app.flight_dir
        rec.dvl_host = getattr(self.app, "dvl_address", "")
        return rec

    def _host(self) -> str:
        # Whatever is in the box is committed first, so the recorder never
        # starts on one address while the box shows another.
        try:
            self._remember_host()
        except Exception:
            pass
        committed = getattr(self.app, "vehicle_host", lambda: None)()
        return committed or "192.168.2.2"

    def _toggle_watch(self) -> None:
        """Queue the change and return: the recorder does the work on its own
        thread, and the buttons wait until it is done."""
        rec = self.recorder
        if rec.transitioning:
            return
        if rec.watching:
            rec.request("unwatch")
        else:
            if self.app.flight_dir is None:
                messagebox.showinfo(
                    self.app.title(),
                    "Choose a flight folder first (section 2).\n\n"
                    "The monitor writes into its logs folder and will not "
                    "guess one.")
                return
            rec.retarget(self._host())
            rec.request("watch")
        self._follow()

    def _toggle_manual(self) -> None:
        rec = self.recorder
        if rec.transitioning or rec.status.state in ("starting", "closing"):
            return
        if rec.status.state == "recording":
            rec.request("stop")
        else:
            if self.app.flight_dir is None:
                messagebox.showinfo(self.app.title(),
                                    "Choose a flight folder first.")
                return
            rec.retarget(self._host())
            rec.request("record")
        self._follow()

    def _follow(self) -> None:
        """Show a lifecycle request's progress promptly, until it is done."""
        rec = getattr(self.app, "recorder", None)
        try:
            self._sync_buttons()
            if self.canvas.winfo_ismapped():
                self._update_state()
        except Exception:
            pass
        if rec is not None and (rec.transitioning
                                or rec.status.state in ("starting", "closing")):
            self.after(250, self._follow)

    def _open_logs(self) -> None:
        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(), "No flight folder selected.")
            return
        folder = Path(self.app.flight_dir) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(folder)                      # noqa: S606
        except Exception:
            pass

    def _pick_group(self, name: str, redraw: bool = True) -> None:
        self._group = name
        self.group_note.configure(text=laptop.GROUP_NOTES.get(name, ""))
        if redraw:
            self._redraw()

    def refresh_theme(self) -> None:
        self.canvas.configure(
            background=self._apply_appearance_mode(T.FIELD_BG),
            highlightbackground=self._apply_appearance_mode(T.BORDER))
        self._redraw()

    def _pick_window(self, name: str) -> None:
        self._window = name
        self._redraw()

    def _sync_buttons(self) -> None:
        rec = getattr(self.app, "recorder", None)
        watching = bool(rec and rec.watching)
        busy = bool(rec and rec.transitioning)
        if busy and rec.status.transition:
            watch_text = f"{rec.status.transition}…"
        else:
            watch_text = "Stop monitoring" if watching else "Start monitoring"
        # Disabled while a request is under way, so a second press cannot
        # queue the opposite of what the first one is still doing.
        self.watch_btn.configure(text=watch_text,
                                 state="disabled" if busy else "normal")
        state = rec.status.state if rec else "idle"
        recording = state == "recording"
        self.manual_btn.configure(
            text={"starting": "Starting…", "closing": "Closing…"}.get(
                state, "Stop" if recording else "Record now"),
            state="disabled" if busy or state in ("starting", "closing")
            else "normal")

    def _tick(self) -> None:
        """Once a second, matching the data. Reschedules itself forever.

        Redrawing is skipped while the page is not on screen. The recorder
        goes on recording either way -- it is on its own thread and knows
        nothing about this page -- but repainting seven strips onto a canvas
        nobody is looking at, for the length of a survey day, is exactly the
        kind of cost this tool is supposed not to have.
        """
        try:
            # The canvas, not this frame: on the Monitoring tab the cards are
            # built into the tab's own column and this frame is never shown.
            if self.canvas.winfo_ismapped():
                self._update_state()
                self._redraw()
            if self.board.winfo_ismapped():
                self._update_board()
        except Exception:
            import logging
            import sys

            from .. import diagnostics
            diagnostics.log_exception("Monitoring tick", *sys.exc_info(),
                                      level=logging.WARNING)
        self.after(1000, self._tick)

    def _update_board(self) -> None:
        """Gather what the board judges, judge it, paint it.

        The first time the board is on screen it starts the readers it needs
        -- the Navigation tab's collector, the DVL view, the UGPS topside --
        each of which is read-only and would start anyway when its own tab
        was opened.
        """
        from .. import board as B
        app = self.app
        app.ensure_vehicle_readers()
        nav = app.pages.get("navigation")
        col = getattr(nav, "collector", None)
        rec = getattr(app, "recorder", None)
        dvl = app.dvl_recorder().live.snapshot(history=False)
        # Read, not started: starting it is `ensure_vehicle_readers`'s call.
        cap = app.simulator.ugps if getattr(app, "simulating", False) else app.ugps
        ugps = cap.snapshot() if cap is not None else None
        rows = rec.history.rows()[-120:] if rec is not None else []
        inp = B.Inputs(snapshot=col.snapshot() if col is not None else None,
                       recorder_status=rec.status if rec is not None else None,
                       recorder_watching=bool(rec and rec.watching),
                       latest_row=rows[-1] if rows else None,
                       history_rows=rows, dvl=dvl, ugps=ugps)
        self.board.update_from(B.judge(inp))
        self.board.fit(self.board.winfo_width())

    def _update_state(self) -> None:
        rec = getattr(self.app, "recorder", None)
        if rec is None:
            _set_text(self.state_label, "Not watching.", T.TEXT_MUTED)
            return
        st = rec.status
        bits = [_short_status(st)]
        warn = bool(st.problem or st.degraded)
        if st.state == "recording":
            # Written is not the same as on the disk: rows are synced every
            # few seconds, and this is the last time that succeeded.
            if st.last_write and time.time() - st.last_write > 5:
                bits.append(f"NOT WRITING for {time.time() - st.last_write:.0f} s")
                warn = True
            elif st.last_sync:
                bits.append(f"synced {time.time() - st.last_sync:.0f} s")
        elif st.state == "idle":
            achieved = achieved_hz(rec.history)
            if achieved is not None and st.outcome:
                bits.append(f"{achieved:.2f} rows/s")
        if st.degraded:
            bits.append(f"⚠ {st.degraded}")
        if st.pending_host:
            bits.append(f"→ {st.pending_host} next flight")
        color = T.WARN if warn else (T.OK if st.state == "recording"
                                     else T.TEXT_MUTED)
        _set_text(self.state_label, "  ·  ".join(bits), color)
        # Said once in the log, not repeated on screen.
        missing = tuple(k for k, v in (rec.capabilities or {}).items()
                        if v.startswith("unavailable"))
        if missing and missing != self._capabilities_said:
            self._capabilities_said = missing
            self._say("Not readable on this laptop: " + ", ".join(missing) + ".")
        self._sync_buttons()

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def _canvas_configured(self, event) -> None:
        """Redraw only when the strip's own size actually changed."""
        size = (int(event.width), int(event.height))
        if size == self._canvas_at:
            return
        self._canvas_at = size
        self._repaint.ask()

    def _judged(self, rec, column: str) -> tuple[list, list, list]:
        """(times, values, levels) for one reading, kept up to date a row at a
        time.

        Rows are only ever appended, and a sample's level depends only on the
        samples before it -- at most `BAND_MEMORY_S` of them -- so each new
        row is judged once, with that much of the past for context, and the
        answer kept. Judging the whole half hour afresh every second cost
        20-30 ms a second on the Tk thread; this costs well under one.
        A redraw for a window edge moving, where nothing new has been
        written, costs nothing at all.
        """
        history = rec.history
        if self._series_from is not history:
            self._series_from = history
            self._series_cache.clear()
            self._rows_at = None
        version = getattr(history, "version", None)
        if version is None or version != self._rows_at:
            self._rows_at = version
            self._rows = history.rows() if hasattr(history, "rows") else []
            self._times = [r.get("elapsed_time_s") or 0.0 for r in self._rows]
            self._fresh = set()
        hit = self._series_cache.get(column)
        if hit is not None and column in self._fresh:
            return hit[:3]
        rows, times_all = self._rows, self._times
        if not rows:
            out = ([], [], [], None)
        else:
            newest = times_all[-1]
            last = hit[3] if hit is not None else None
            if last is None or last > newest:
                # First time, or the history was cleared: everything that can
                # be drawn, plus the context its first sample needs.
                first = bisect.bisect_left(
                    times_all, newest - max(WINDOWS.values()) - BAND_MEMORY_S)
                times, values, levels = [], [], []
                new_from = first
            else:
                times, values, levels = (list(hit[0]), list(hit[1]), list(hit[2]))
                new_from = bisect.bisect_right(times_all, last)
            if new_from < len(rows):
                ctx = bisect.bisect_left(times_all,
                                         times_all[new_from] - BAND_MEMORY_S)
                lv = bands.levels(column, rows[ctx:])[new_from - ctx:]
                for r, level in zip(rows[new_from:], lv, strict=False):
                    t, v = r.get("elapsed_time_s"), bands._num(r.get(column))
                    if t is None or v is None:
                        continue
                    times.append(float(t))
                    values.append(v)
                    levels.append(level)
            # What has fallen out of the longest window is let go.
            cut = bisect.bisect_left(times, newest - max(WINDOWS.values()) - 5.0)
            if cut:
                del times[:cut], values[:cut], levels[:cut]
            out = (times, values, levels, newest)
        self._series_cache[column] = out
        self._fresh.add(column)
        return out[:3]

    def _font(self, font: tuple, s: float):
        from tkinter import font as tkfont
        key = (font, round(s, 3))
        got = self._fonts.get(key)
        if got is None:
            spec = T.scale_font(font, s)
            got = tkfont.Font(family=spec[0], size=spec[1],
                              weight="bold" if "bold" in spec[2:] else "normal")
            self._fonts[key] = got
        return got

    def _fit(self, text: str, font: tuple, s: float, room: float) -> str:
        """`text` cut to `room` pixels in `font`, with an ellipsis if it was."""
        f = self._font(font, s)
        if f.measure(text) <= room:
            return text
        while text and f.measure(text + "…") > room:
            text = text[:-1]
        return text + "…"

    def _colors(self) -> dict:
        m = self._apply_appearance_mode
        out = {"text": m(T.TEXT), "muted": m(T.TEXT_MUTED), "grid": m(T.BORDER),
               "faint": m(GRID_FAINT), "plain": m(PLAIN_LINE),
               "tip_bg": m(T.SURFACE), "tip_edge": m(T.BORDER), "sim": m(SIM_MARK),
               "strip": m(STRIP_FILL), "edge": m(STRIP_EDGE), "plot": m(T.FIELD_BG)}
        for level, pair in bands.LEVEL_COLORS.items():
            out[level] = m(pair)
        return out

    def _redraw(self) -> None:
        # Not `_draw`: CTkFrame has one of its own, with a different signature,
        # and overriding it stops the frame being constructed at all.
        cv = self.canvas
        cv.delete("all")
        s = T.scale_of(self) or 1.0
        width = cv.winfo_width()
        if width < 50 * s:
            return
        rec = getattr(self.app, "recorder", None)
        columns = laptop.GROUPS.get(self._group, ())
        total = int((ROW_H * max(1, len(columns)) + TIME_H) * s)
        if int(cv.cget("height")) != total:
            cv.configure(height=total)

        if rec is None:
            self._placeholder(cv, width, s, "Press Start monitoring to begin.")
            return
        latest = rec.history.latest()
        if not latest:
            self._placeholder(
                cv, width, s,
                "Waiting for the first sample — nothing is recorded until the "
                "ROV arms." if rec.watching else
                "Press Start monitoring to begin.")
            return

        span = WINDOWS[self._window]
        now = latest.get("elapsed_time_s") or 0.0
        c = self._colors()
        x0 = (NAME_W + VALUE_W) * s
        x1 = width - (AXIS_W + 8) * s
        plot_bottom = ROW_H * len(columns) * s
        self._geom = {"s": s, "width": width, "columns": columns,
                      "x0": x0, "x1": x1}

        # The time grid is drawn inside each strip's plot.
        ticks = _time_ticks(span)
        self._ticks = ticks if x1 - x0 > 40 * s else ()

        simulated = bool(getattr(self.app, "simulating", False))
        for i, column in enumerate(columns):
            self._strip(cv, rec, i, column, latest, now, span, c, s, width,
                        x0, x1, simulated)

        # The time axis, once, along the foot.
        cv.create_line(x0, plot_bottom, x1, plot_bottom, fill=c["grid"])
        f_axis = T.scale_font(FONT_AXIS, s)
        if x1 - x0 > 40 * s:
            for ago in ticks:
                x = x1 - ago / span * (x1 - x0)
                cv.create_line(x, plot_bottom, x, plot_bottom + 5 * s, fill=c["grid"])
                # The two ends sit inside the plot, so neither runs into the
                # axis's name on the left or the strip labels on the right.
                anchor = "ne" if ago < 0.5 else "nw" if ago >= span - 0.5 else "n"
                cv.create_text(x, plot_bottom + 7 * s, anchor=anchor, fill=c["text"],
                               font=f_axis, text=_ago_text(ago, span))
        cv.create_text(x0 - 14 * s, plot_bottom + 7 * s, anchor="ne", fill=c["muted"],
                       font=f_axis, text=f"time · last {self._window}")
        self._draw_tip()

    def _strip(self, cv, rec, i, column, latest, now, span, c, s, width,
               x0, x1, simulated) -> None:
        """One reading: its name and thresholds, its value and level, its line."""
        top = i * ROW_H * s
        bottom = top + ROW_H * s
        inset = STRIP_GAP / 2 * s
        cv.create_rectangle(2 * s, top + inset, width - 2 * s, bottom - inset,
                            fill=c["strip"], outline=c["edge"])

        times, values, levels = self._judged(rec, column)
        cut = bisect.bisect_left(times, now - span)
        level = next((lv for lv in reversed(levels[cut:]) if lv is not None), None)
        if not times or times[-1] < now - 30:
            level = None                               # nothing current to judge

        # A stripe down the left edge in the level's colour: the one thing to
        # see when scanning a group for trouble.
        cv.create_rectangle(2 * s, top + inset, 8 * s, bottom - inset,
                            fill=c[level] if level is not None else c["edge"],
                            outline="")

        name_room = (NAME_W - 22) * s
        f_name = T.scale_font(FONT_NAME, s)
        f_note = T.scale_font(FONT_NOTE, s)
        cv.create_text(14 * s, top + 17 * s, anchor="w", fill=c["text"], font=f_name,
                       text=self._fit(column, FONT_NAME, s, name_room))
        unit = laptop.UNITS.get(column, "")
        cv.create_text(14 * s, top + 36 * s, anchor="w", fill=c["muted"],
                       font=f_note, text=hz_text(refresh_hz(column)))
        limits = bands.threshold_text(column, unit)
        if limits:
            cv.create_text(14 * s, top + 53 * s, anchor="w", fill=c["muted"],
                           font=f_note, text=self._fit(limits, FONT_NOTE, s, name_room))
        if simulated and column in NOT_ON_FIELD_LAPTOP:
            cv.create_text(14 * s, top + 70 * s, anchor="w", fill=c["sim"],
                           font=f_note,
                           text=self._fit("◇ simulated (blank on L342-D)",
                                          FONT_NOTE, s, name_room))

        value = latest.get(column)
        cv.create_text(x0 - 14 * s, top + ROW_H * s / 2 - 9 * s, anchor="e",
                       text=_pretty(value, column), fill=c["text"],
                       font=T.scale_font(FONT_VALUE, s))
        if level is not None:
            cv.create_text(x0 - 14 * s, top + ROW_H * s / 2 + 15 * s, anchor="e",
                           fill=c[level], font=T.scale_font(FONT_LEVEL, s),
                           text=f"{bands.LEVEL_GLYPHS[level]} {bands.LEVEL_WORDS[level]}")

        self._plot(cv, times[cut:], values[cut:], levels[cut:], column, top, c, s,
                   x0, x1, now, span)

    def _plot(self, cv, times, values, levels, column, top, c, s,
              x0, x1, now, span) -> None:
        """The line, coloured by level, on its own scale with its own axis."""
        y0, y1 = top + PAD_Y * s, top + (ROW_H - PAD_Y) * s
        if x1 - x0 < 30 * s:
            return
        f_axis = T.scale_font(FONT_AXIS, s)
        box_top, box_bottom = top + (STRIP_GAP / 2 + 3) * s, top + (ROW_H - STRIP_GAP / 2 - 3) * s
        cv.create_rectangle(x0, box_top, x1, box_bottom, fill=c["plot"],
                            outline=c["edge"])
        for ago in getattr(self, "_ticks", ()):
            x = x1 - ago / span * (x1 - x0)
            cv.create_line(x, box_top + 1, x, box_bottom, fill=c["faint"], dash=(2, 4))
        if len(values) < 2:
            cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="—",
                           fill=c["muted"], font=f_axis)
            return

        flag = column in BOOL_COLUMNS
        if flag:
            lo, hi = 0.0, 1.0
        else:
            lo, hi = min(values), max(values)
            # Thresholds near the data are drawn, and the scale stretched to
            # show them: how close a reading is to trouble is the point.
            room = (hi - lo) or abs(hi) * 0.1 or 1.0
            for val, _lv in bands.threshold_lines(column):
                if lo - room <= val <= hi + room:
                    lo, hi = min(lo, val), max(hi, val)
            if hi - lo < 1e-9:
                lo, hi = lo - 0.5, hi + 0.5
            # Never stretch a wobble of a few parts in a hundred across the
            # whole strip: 127 to 128 Mbps is a steady link, and drawn
            # floor-to-ceiling it looked like one in trouble.
            least = MIN_SPAN_SHARE * max(abs(lo), abs(hi))
            if hi - lo < least:
                mid = (hi + lo) / 2
                lo, hi = mid - least / 2, mid + least / 2
        pad = (hi - lo) * 0.08
        lo_d, hi_d = lo - pad, hi + pad

        def y_of(v: float) -> float:
            return y1 - (v - lo_d) / (hi_d - lo_d) * (y1 - y0)

        def x_of(t: float) -> float:
            fx = (t - (now - span)) / span if span else 1.0
            return x0 + max(0.0, min(1.0, fx)) * (x1 - x0)

        # The axis: three labelled ticks and faint lines across the strip.
        div, unit = axis_scale(column, max(abs(hi), abs(lo)))
        if flag:
            marks = ((1.0, "TRUE"), (0.0, "FALSE"))
        else:
            mid = (hi + lo) / 2
            marks = tuple((v, axis_number(v / div)) for v in (hi, mid, lo))
        for v, text in marks:
            y = y_of(v)
            cv.create_line(x0 + 1, y, x1, y, fill=c["faint"])
            cv.create_line(x1, y, x1 + 4 * s, y, fill=c["grid"])
            cv.create_text(x1 + 7 * s, y, anchor="w", fill=c["text"], font=f_axis,
                           text=text)
        # The unit once, beside the labels -- every strip has one.
        if unit:
            font = self._font(FONT_AXIS, s)
            room = max(font.measure(text) for _v, text in marks)
            cv.create_text(x1 + 7 * s + room + 6 * s, y_of((hi + lo) / 2), anchor="w",
                           fill=c["muted"], font=f_axis, text=unit)
        if not flag:
            for val, lv in bands.threshold_lines(column):
                if lo <= val <= hi:
                    y = y_of(val)
                    cv.create_line(x0, y, x1, y, fill=c[lv], dash=(5, 4))

        # Decimated to the pixel width -- coarser still while a window edge is
        # being dragged -- with each bucket coloured by the worst level in it,
        # so a one-second spike into red is never decimated away.
        pixels = max(1, int(x1 - x0))
        if self._repaint.busy:
            pixels = max(1, pixels // DRAG_DECIMATION)
        n = len(values)
        step = max(1, n // pixels)
        gap = max(3.0, 2.5 * SLOWER_THAN_ROW.get(column, laptop.DEFAULT_PERIOD_S))
        lw = max(1, int(round(2 * s)))
        runs: list[tuple[str, list]] = []
        color, coords, last = None, [], None
        for k in range(0, n, step):
            bucket = [lv for lv in levels[k:k + step] if lv is not None]
            lv = max(bucket) if bucket else None
            pt_color = c[lv] if lv is not None else c["plain"]
            x, y = x_of(times[k]), y_of(values[k])
            if last is not None and times[k] - last[2] > gap * step:
                runs.append((color, coords))           # a blank stretch: no line
                color, coords = pt_color, [x, y]
            elif color is None:
                color, coords = pt_color, [x, y]
            elif pt_color == color:
                coords += [x, y]
            else:
                runs.append((color, coords))
                color, coords = pt_color, [last[0], last[1], x, y]
            last = (x, y, times[k])
        runs.append((color, coords))
        for col, pts in runs:
            if len(pts) >= 4:
                cv.create_line(*pts, fill=col, width=lw)
            elif len(pts) == 2:
                cv.create_oval(pts[0] - lw, pts[1] - lw, pts[0] + lw, pts[1] + lw,
                               fill=col, outline="")

    def _placeholder(self, cv, width, s, message: str) -> None:
        cv.configure(height=int(ROW_H * 3 * s))
        cv.create_text(width / 2, ROW_H * 1.5 * s, text=message,
                       fill=self._apply_appearance_mode(T.TEXT_MUTED),
                       font=T.scale_font(T.FONT_BODY, s))

    # ------------------------------------------------------------------
    #  why each reading is judged as it is, under the pointer
    # ------------------------------------------------------------------

    def _hovered(self, event) -> None:
        geom = self._geom
        if not geom:
            return
        s = geom["s"]
        i = int(event.y // (ROW_H * s))
        over = event.x < geom["x0"] and 0 <= i < len(geom["columns"])
        want = (geom["columns"][i], event.x, event.y) if over else None
        if (want and self._hover and want[0] == self._hover[0]
                and abs(want[1] - self._hover[1]) < 30 * s
                and abs(want[2] - self._hover[2]) < 30 * s):
            return
        self._hover = want
        self._draw_tip()

    def _unhovered(self, _event=None) -> None:
        self._hover = None
        self._draw_tip()

    def _draw_tip(self) -> None:
        cv = self.canvas
        cv.delete("tip")
        if not self._hover or not self._geom:
            return
        column, x, y = self._hover
        s = self._geom["s"]
        lines = [column] + bands.why(column)
        if getattr(self.app, "simulating", False) and column in NOT_ON_FIELD_LAPTOP:
            lines.append("Simulated here: L342-D does not publish this reading, "
                         "and leaves the strip blank.")
        if len(lines) == 1:
            lines.append("No thresholds: this reading is not judged.")
        f_tip = T.scale_font(FONT_TIP, s)
        c = self._colors()
        tx, ty = x + 16 * s, y + 12 * s
        text_id = cv.create_text(tx + 10 * s, ty + 8 * s, anchor="nw",
                                 width=int(360 * s), fill=c["text"], font=f_tip,
                                 tags="tip", text="\n\n".join(lines))
        x_a, y_a, x_b, y_b = cv.bbox(text_id)
        dx = min(0.0, self._geom["width"] - 6 * s - (x_b + 10 * s))
        dy = min(0.0, int(cv.cget("height")) - 6 * s - (y_b + 8 * s))
        cv.move(text_id, dx, dy)
        rect = cv.create_rectangle(x_a - 10 * s + dx, y_a - 8 * s + dy,
                                   x_b + 10 * s + dx, y_b + 8 * s + dy,
                                   fill=c["tip_bg"], outline=c["tip_edge"], tags="tip")
        cv.tag_raise(text_id, rect)


def _time_ticks(span: float) -> list[float]:
    """Seconds-ago for each labelled tick on the time axis, newest first."""
    step = {120: 30.0, 600: 120.0, 1800: 300.0}.get(int(span), span / 4)
    out, k = [], 0.0
    while k <= span + 1e-6:
        out.append(k)
        k += step
    return out


def _ago_text(ago: float, span: float) -> str:
    """A time-axis label: seconds on the two-minute window, minutes otherwise."""
    if ago < 0.5:
        return "now"
    if span <= 120:
        return f"−{ago:.0f} s"
    return f"−{ago / 60:g} min"


def _pretty(value, column: str = "") -> str:
    """A reading, short enough to sit beside its own line."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    unit = laptop.UNITS.get(column, "")
    if unit == "bytes":
        for size, suffix in ((2 ** 30, "GiB"), (2 ** 20, "MiB"), (2 ** 10, "KiB")):
            if abs(v) >= size:
                return f"{v / size:,.2f} {suffix}"
        return f"{v:,.0f} B"
    if column == "system_uptime_s":
        if v >= 86_400:
            return f"{v / 86_400:.1f} d"
        if v >= 3600:
            return f"{v / 3600:.1f} h"
        return f"{v / 60:.0f} min"
    if v == int(v) and abs(v) < 1e6:
        return f"{int(v):,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"
