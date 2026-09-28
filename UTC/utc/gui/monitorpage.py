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

import tkinter
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import brand, laptop, netdiag, nettrace
from . import theme as T
from .widgets import Card, button

#: How far back the charts look. A survey transect is a few minutes, so the
#: default shows one whole transect and its approach.
WINDOWS = {"2 min": 120, "10 min": 600, "30 min": 1800}
DEFAULT_WINDOW = "10 min"

#: One strip per reading. Tall enough for a label, a value and a line that can
#: be read at arm's length on a laptop in daylight.
ROW_H = 58
#: Room at the left for the reading's name, and for its current value.
NAME_W = 210
VALUE_W = 110
#: Kept clear on the right for the range the strip is scaled to.
RANGE_W = 96

#: Series colors, in order, from the brand palette. Seven, so a group never
#: has to reuse one.
SERIES_COLORS = (
    brand.SEAFOAM, brand.ALGAE, brand.CORAL, brand.MEDITERRANEAN,
    brand.PURPLE_STAR, "#FFC24D", "#9FB4C7",
)


class MonitorPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._recorder = None
        self._group = next(iter(laptop.GROUPS))
        self._window = DEFAULT_WINDOW
        self._ticking = False

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        self._build_recorder_card(body)
        self._build_network_card(body)
        self._build_chart_card(body)

    # ------------------------------------------------------------------
    #  the recorder
    # ------------------------------------------------------------------

    def _build_recorder_card(self, body) -> None:
        c = Card(body, "1.  Recording this flight",
                 "Starts itself when the ROV arms and closes when it has been "
                 "disarmed for 90 seconds, so a surface interval between "
                 "transects does not split one dive into several files. "
                 "Everything lands in the flight folder's logs folder.")
        c.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)

        r = ctk.CTkFrame(c.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="w")
        self.watch_btn = button(r, "Start monitoring", self._toggle_watch,
                                "primary", width=170)
        self.watch_btn.grid(row=0, column=0)
        self.manual_btn = button(r, "Record now", self._toggle_manual, "ghost",
                                 width=130)
        self.manual_btn.grid(row=0, column=1, padx=(8, 0))
        button(r, "Open logs folder", self._open_logs, "ghost", width=150
               ).grid(row=0, column=2, padx=(8, 0))

        self.state_label = ctk.CTkLabel(
            c.body, text="Not watching.", font=T.FONT_BODY,
            text_color=T.TEXT_MUTED, anchor="w", justify="left",
            wraplength=880)
        self.state_label.grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.detail = ctk.CTkLabel(
            c.body, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
            anchor="w", justify="left", wraplength=880)
        self.detail.grid(row=2, column=0, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------------
    #  the path to the vehicle
    # ------------------------------------------------------------------

    def _build_network_card(self, body) -> None:
        c = Card(body, "2.  The path to the vehicle",
                 "Read this on deck, before the dive. It says which adapter "
                 "actually carries the tether, whether that adapter is a "
                 "bridge with a real one underneath it, and whether Windows "
                 "is allowed to power any of them down. A flight recorded "
                 "without knowing those is a flight whose network column "
                 "cannot be interpreted afterwards.")
        c.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)

        r = ctk.CTkFrame(c.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="w")
        button(r, "Check the network", self._check_network, "primary",
               width=170).grid(row=0, column=0)
        button(r, "Measure the link", self._calibrate, "ghost", width=150
               ).grid(row=0, column=1, padx=(8, 0))
        button(r, "Save to flight folder", self._save_network, "ghost",
               width=180).grid(row=0, column=2, padx=(8, 0))

        self.net_box = ctk.CTkTextbox(c.body, height=190, font=T.FONT_MONO,
                                      fg_color=T.FIELD_BG,
                                      text_color=T.TEXT_MUTED, border_width=1,
                                      border_color=T.BORDER, corner_radius=6,
                                      wrap="none")
        self.net_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._net_text = ""
        self._say(self.net_box,
                  "Not checked yet.\n\n"
                  "Check the network reads the adapters and their settings — "
                  "about four seconds, most of it waiting on ARP.\n"
                  "Measure the link watches the counters for four seconds "
                  "with the tether live, and reports whether they move fast "
                  "enough for the fast trace to mean anything on this "
                  "laptop.")

    @staticmethod
    def _say(box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _check_network(self) -> None:
        host = self._host()
        self._say(self.net_box, "Reading the adapters…")

        def work(progress, cancel):
            return netdiag.report(host)

        self.app.submit(work, "Reading the topside network…",
                        on_done=self._network_done)

    def _calibrate(self) -> None:
        host = self._host()
        self._say(self.net_box,
                  "Watching the counters for four seconds — leave the video "
                  "running while this runs.")

        def work(progress, cancel):
            return nettrace.calibration_text(nettrace.calibrate(host))

        self.app.submit(work, "Measuring the link…",
                        on_done=self._network_done)

    def _network_done(self, result) -> None:
        if result is None or isinstance(result, Exception):
            self._say(self.net_box, f"Could not read the network: {result}")
            return
        self._net_text = str(result)
        self._say(self.net_box, self._net_text)

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
                "Choose a flight folder on Flight & transects first — this "
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
        messagebox.showinfo("Saved", f"Written to logs\\{name}")

    def _logs_folder(self) -> Path | None:
        rec = self.recorder
        if rec is None or not getattr(rec, "flight_dir", None):
            return None
        return Path(rec.flight_dir) / "logs"

    def _build_chart_card(self, body) -> None:
        c = Card(body, "3.  Live",
                 "One group at a time, each reading on its own scale. A blank "
                 "strip is a sensor this laptop does not publish — the list "
                 "beside the CSV says which, and why.")
        c.grid(row=2, column=0, sticky="ew")
        c.body.grid_columnconfigure(0, weight=1)

        strip = ctk.CTkFrame(c.body, fg_color="transparent")
        strip.grid(row=0, column=0, sticky="ew")
        self.group_pick = ctk.CTkSegmentedButton(
            strip, values=list(laptop.GROUPS), command=self._pick_group,
            font=T.FONT_SECTION, fg_color=T.SURFACE_ALT,
            selected_color=T.ACCENT, selected_hover_color=T.ACCENT_HOVER,
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

        self.canvas = tkinter.Canvas(
            c.body, highlightthickness=1, borderwidth=0,
            height=ROW_H * 7,
            background=self._apply_appearance_mode(T.FIELD_BG),
            highlightbackground=self._apply_appearance_mode(T.BORDER))
        self.canvas.grid(row=2, column=0, sticky="ew")
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

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
        return rec

    def _host(self) -> str:
        page = getattr(self.app, "pages", {}).get("Vehicle & files")
        typed = ""
        if page is not None:
            try:
                typed = page.host.get().strip()
            except Exception:
                typed = ""
        return typed or "192.168.2.2"

    def _toggle_watch(self) -> None:
        rec = self.recorder
        if rec.watching:
            rec.stop_watching()
        else:
            if self.app.flight_dir is None:
                messagebox.showinfo(
                    self.app.title(),
                    "Choose a flight folder first, on Flight & transects.\n\n"
                    "The monitor writes into that folder's logs folder and "
                    "will not guess one, so a flight is never filed somewhere "
                    "nobody looks.")
                return
            rec.host = self._host()
            rec.start_watching()
        self._sync_buttons()

    def _toggle_manual(self) -> None:
        rec = self.recorder
        if rec.status.state == "recording":
            rec.stop_manually()
        else:
            if self.app.flight_dir is None:
                messagebox.showinfo(self.app.title(),
                                    "Choose a flight folder first.")
                return
            rec.host = self._host()
            if not rec.watching:
                rec.start_watching()
            rec.start_manually()
        self._sync_buttons()

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

    def _pick_group(self, name: str) -> None:
        self._group = name
        self.group_note.configure(text=laptop.GROUP_NOTES.get(name, ""))
        self._redraw()

    def _pick_window(self, name: str) -> None:
        self._window = name
        self._redraw()

    def _sync_buttons(self) -> None:
        rec = getattr(self.app, "recorder", None)
        watching = bool(rec and rec.watching)
        self.watch_btn.configure(
            text="Stop monitoring" if watching else "Start monitoring")
        recording = bool(rec and rec.status.state == "recording")
        self.manual_btn.configure(text="Stop" if recording else "Record now")

    def _tick(self) -> None:
        """Once a second, matching the data. Reschedules itself forever.

        Redrawing is skipped while the page is not on screen. The recorder
        goes on recording either way -- it is on its own thread and knows
        nothing about this page -- but repainting seven strips onto a canvas
        nobody is looking at, for the length of a survey day, is exactly the
        kind of cost this tool is supposed not to have.
        """
        try:
            if self.winfo_ismapped():
                self._update_state()
                self._redraw()
        except Exception:
            pass
        self.after(1000, self._tick)

    def _update_state(self) -> None:
        rec = getattr(self.app, "recorder", None)
        if rec is None:
            self.state_label.configure(text="Not watching.",
                                       text_color=T.TEXT_MUTED)
            self.detail.configure(text="")
            return
        st = rec.status
        color = T.WARN if st.problem else (
            T.OK if st.state == "recording" else T.TEXT_MUTED)
        self.state_label.configure(text=st.line(), text_color=color)

        bits = []
        if st.csv_path is not None and st.state == "recording":
            bits.append(f"writing {st.csv_path.name}")
        if rec.flight_dir:
            bits.append(f"logs in {Path(rec.flight_dir) / 'logs'}")
        missing = [k for k, v in (rec.capabilities or {}).items()
                   if v.startswith("unavailable")]
        if missing:
            bits.append("not readable on this laptop: " + ", ".join(missing))
        self.detail.configure(text="     ".join(bits))
        self._sync_buttons()

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        # Not `_draw`: CTkFrame has one of its own, with a different signature,
        # and overriding it stops the frame being constructed at all.
        cv = self.canvas
        cv.delete("all")
        width = cv.winfo_width()
        if width < 50:
            return
        rec = getattr(self.app, "recorder", None)
        columns = laptop.GROUPS.get(self._group, ())
        cv.configure(height=ROW_H * max(1, len(columns)))

        if rec is None:
            self._placeholder(cv, width, "Press Start monitoring to begin.")
            return
        latest = rec.history.latest()
        if not latest:
            self._placeholder(
                cv, width,
                "Waiting for the first sample — nothing is recorded until the "
                "ROV arms." if rec.watching else
                "Press Start monitoring to begin.")
            return

        span = WINDOWS[self._window]
        now = latest.get("elapsed_time_s") or 0.0
        muted = self._apply_appearance_mode(T.TEXT_MUTED)
        text = self._apply_appearance_mode(T.TEXT)
        grid = self._apply_appearance_mode(T.BORDER)

        for i, column in enumerate(columns):
            top = i * ROW_H
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            if i:
                cv.create_line(8, top, width - 8, top, fill=grid)
            cv.create_text(12, top + ROW_H / 2 - 8, anchor="w", text=column,
                           fill=text, font=T.FONT_MONO)
            unit = laptop.UNITS.get(column, "")
            if unit:
                cv.create_text(12, top + ROW_H / 2 + 10, anchor="w",
                               text=unit, fill=muted, font=T.FONT_SMALL)

            series = [(t, v) for t, v in rec.history.series(column)
                      if t >= now - span]
            value = latest.get(column)
            cv.create_text(NAME_W + VALUE_W - 14, top + ROW_H / 2, anchor="e",
                           text=_pretty(value), fill=color,
                           font=(T.MONO, 15, "bold"))
            self._strip(cv, series, top, width, color, muted, now, span)

    def _strip(self, cv, series, top, width, color, muted, now, span) -> None:
        """One reading's line, scaled to its own range over the window."""
        x0 = NAME_W + VALUE_W
        x1 = width - RANGE_W - 10
        pad = 9
        y0, y1 = top + pad, top + ROW_H - pad
        if x1 - x0 < 30:
            return
        if len(series) < 2:
            cv.create_text((x0 + x1) / 2, top + ROW_H / 2, text="—",
                           fill=muted, font=T.FONT_SMALL)
            return

        lo = min(v for _t, v in series)
        hi = max(v for _t, v in series)
        # A flat line belongs in the middle of its strip, not pinned to an
        # edge by a zero-height range.
        if hi - lo < 1e-9:
            lo, hi = lo - 0.5, hi + 0.5
        t_lo = now - span

        # Decimate to the pixel width. An hour of samples through a 600-pixel
        # strip is 3,600 points for 600 columns; drawing them all costs six
        # times as much and looks identical.
        pixels = max(1, int(x1 - x0))
        step = max(1, len(series) // pixels)
        points: list[float] = []
        for t, v in series[::step]:
            fx = (t - t_lo) / span if span else 1.0
            points.append(x0 + max(0.0, min(1.0, fx)) * (x1 - x0))
            points.append(y1 - (v - lo) / (hi - lo) * (y1 - y0))
        if len(points) >= 4:
            cv.create_line(*points, fill=color, width=2, smooth=False)

        cv.create_text(width - 12, top + pad + 2, anchor="ne",
                       text=_pretty(hi), fill=muted, font=T.FONT_SMALL)
        cv.create_text(width - 12, top + ROW_H - pad - 2, anchor="se",
                       text=_pretty(lo), fill=muted, font=T.FONT_SMALL)

    def _placeholder(self, cv, width, message: str) -> None:
        cv.configure(height=ROW_H * 3)
        cv.create_text(width / 2, ROW_H * 1.5, text=message,
                       fill=self._apply_appearance_mode(T.TEXT_MUTED),
                       font=T.FONT_BODY)


def _pretty(value) -> str:
    """A reading, short enough to sit beside its own line."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == int(v) and abs(v) < 1e6:
        return f"{int(v):,}"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"
