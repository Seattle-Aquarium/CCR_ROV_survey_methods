"""
DVL: the Water Linked DVL A50, beam by beam, and whether what it says arrives.

The page reads the application's DVL capture (`dvl.recorder`) and draws it.
Nothing here drives the capture beyond starting a live view when the tab is
opened with no flight folder chosen: the capture records whichever tab is
open, because the operator is flying and not looking at this.

Seven sections:

1. **The DVL and this capture** -- where the DVL is, whether each of its three
   streams is connected, and what is being written where.
2. **Did it reach the autopilot?** -- what the DVL emitted, side by side with
   what mavlink2rest counted arriving from the BlueOS extension, because that
   is where the dropped messages are seen: in the mcaps, Cockpit and QGC, and
   not on the DVL's own web page.
3. **The four beams** -- distance, signal, noise and validity, each drawn where
   its transducer points on the vehicle.
4. **Stream health** -- the cadence, every gap and which side of the link it
   came from, latency, the DVL's clock and temperature.
5. **Live charts** -- the same over the last 2, 10 or 30 minutes, with invalid
   stretches shaded and gaps marked.
6. **Echo and spectrum** -- the latest acoustic snapshot, and how often they
   are taken.
7. **Water Linked diagnostic log** -- collected only on request, only with the
   vehicle confirmed disarmed.

**Drawn on Tk canvases** for the reason the Monitoring charts are: a survey
day on a field laptop. Everything is decimated to the pixel width and redrawn
only while the tab is on screen. A raw canvas is not scaled by CustomTkinter,
so every size drawn on one here is multiplied by the display's scaling -- on
the 250% screen this was first run on, unscaled tiles came out a third of the
size of the text around them.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
import tkinter
import webbrowser
from dataclasses import asdict
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import brand, diagnostics
from ..dvl import capture as CAP
from ..dvl import diagnostic as DIAG
from ..dvl import protocol as P
from ..dvl import webapi
from ..dvl.recorder import DvlRecorder, dvl_folder
from . import theme as T
from .widgets import Card, Repaint, button, entry, fit_wrap, label, output_box, say

#: One colour per transducer, id 0..3, kept everywhere on the page.
BEAM_COLORS = (brand.SEAFOAM, brand.CORAL, "#FFC24D", brand.PURPLE_STAR)

WINDOWS = {"2 min": 120, "10 min": 600, "30 min": 1800}
DEFAULT_WINDOW = "2 min"

SNAPSHOT_CHOICES = {"Off": 0.0, "1 /s": 1.0, "2 /s": 2.0, "5 /s": 5.0, "10 /s": 10.0}
DURATION_CHOICES = {"15 s": 15, "30 s": 30, "1 min": 60, "5 min": 300}

#: The colours of the gap classes on the charts.
GAP_COLORS = {"dvl_quiet": "#9FB4C7", "missing_reports": brand.CORAL,
              "delivery_stall": "#FFC24D", "clock_step": brand.PURPLE_STAR}
GAP_WORDS = {"dvl_quiet": "DVL quiet", "missing_reports": "missing reports",
             "delivery_stall": "delivery stalls", "clock_step": "clock steps"}

#: Sizes on the canvases, in unscaled units; multiplied by the display scale.
STRIP_H = 64
NAME_W = 132
BEAMS_H = 280
ACOUSTIC_H = 210

#: The page refreshes its text this often while it is on screen...
TICK_MS = 500
#: ...and redraws its charts this often.
CHART_EVERY_S = 1.0

FONT_TILE_TITLE = (T.FAMILY_SEMIBOLD, 10)
FONT_BIG = (T.MONO, 15, "bold")
FONT_HUGE = (T.MONO, 18, "bold")
FONT_FLAG = (T.MONO, 11, "bold")
FONT_CANVAS = (T.MONO, 9)
FONT_CANVAS_SMALL = (T.FAMILY, 9)


def _f(value, nd: int = 2, unit: str = "") -> str:
    """A number for a label, or a dash -- never a zero standing in for unknown."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        text = f"{value:,.{nd}f}" if isinstance(value, float) else f"{value:,}"
        if text.startswith("-") and not text.strip("-0.,"):
            text = text[1:]                  # "-0" is a rounding, not a sign
        return f"{text} {unit}".rstrip()
    return str(value)


def _rate(snap, kind: str) -> str:
    return f"{snap.rates.get(kind, 0.0):.1f}/s"


def _segmented(master, values, command=None):
    return ctk.CTkSegmentedButton(
        master, values=values, command=command, font=T.FONT_SMALL,
        fg_color=T.SURFACE_ALT, selected_color=T.ACCENT,
        selected_hover_color=T.ACCENT_HOVER, unselected_color=T.SURFACE_ALT,
        unselected_hover_color=T.BORDER, text_color=T.TEXT)


class DvlPage(ctk.CTkFrame):

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._ticking = False
        self._window = DEFAULT_WINDOW
        self._last_chart = 0.0
        self._events_shown = None
        self._acoustic_shown = (None, None)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self.body = body

        pair = ctk.CTkFrame(body, fg_color="transparent")
        pair.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        pair.grid_columnconfigure((0, 1), weight=1, uniform="pair")
        left = ctk.CTkFrame(pair, fg_color="transparent")
        right = ctk.CTkFrame(pair, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for f in (left, right):
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(0, weight=1)
        self._build_connection(left)
        self._build_delivery(right)
        self._build_beams(body, 1)
        self._build_health(body, 2)
        self._build_charts(body, 3)
        self._build_acoustic(body, 4)
        self._build_diagnostic(body, 5)

    # ------------------------------------------------------------------
    #  drawing at display scale
    # ------------------------------------------------------------------

    def _scale(self) -> float:
        return T.scale_of(self) or 1.0

    def _make_canvas(self, parent, height: int) -> tkinter.Canvas:
        cv = tkinter.Canvas(parent, height=int(height * self._scale()),
                            highlightthickness=1, borderwidth=0,
                            background=self._apply_appearance_mode(T.FIELD_BG),
                            highlightbackground=self._apply_appearance_mode(T.BORDER))
        cv.base_height = height                      # type: ignore[attr-defined]
        return cv

    def _colors(self) -> dict:
        m = self._apply_appearance_mode
        return {"text": m(T.TEXT), "muted": m(T.TEXT_MUTED), "border": m(T.BORDER),
                "warn": m(T.WARN), "ok": m(T.OK), "shade": m(T.SURFACE_ALT)}

    # ------------------------------------------------------------------
    #  1. the DVL and this capture
    # ------------------------------------------------------------------

    def _build_connection(self, parent) -> None:
        c = Card(parent, "1.  The DVL and this capture",
                 "Everything the DVL sends is written to the flight folder's "
                 "logs\\dvl from the moment a folder is chosen until the "
                 "program closes — armed or not. Leave the address blank to "
                 "use the one the BlueOS DVL extension reports.")
        c.grid(row=0, column=0, sticky="nsew")
        c.body.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(c.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(1, weight=1)
        label(row, "DVL address", muted=True).grid(row=0, column=0, padx=(0, 8))
        self.host_entry = entry(row, "blank = ask the BlueOS DVL extension "
                                     "(e.g. 192.168.2.95)", width=260)
        self.host_entry.grid(row=0, column=1, sticky="ew")
        saved = self.app.settings.get("dvl_host", "")
        if saved:
            self.host_entry.insert(0, saved)
        self.host_entry.bind("<Return>", lambda _e: self._commit_host())
        self.host_entry.bind("<FocusOut>", lambda _e: self._commit_host())

        b = ctk.CTkFrame(c.body, fg_color="transparent")
        b.grid(row=1, column=0, sticky="w", pady=(8, 0))
        button(b, "Open DVL folder", self._open_folder, "ghost", width=150
               ).grid(row=0, column=0)
        button(b, "DVL web page", self._open_web, "ghost", width=130
               ).grid(row=0, column=1, padx=(8, 0))

        self.conn_label = ctk.CTkLabel(c.body, text="", font=T.FONT_BODY,
                                       text_color=T.TEXT, anchor="w", justify="left")
        self.conn_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.capture_label = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                          text_color=T.TEXT_MUTED, anchor="w",
                                          justify="left")
        self.capture_label.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.problem_label = ctk.CTkLabel(c.body, text="", font=T.FONT_BODY,
                                          text_color=T.WARN, anchor="w",
                                          justify="left")
        self.problem_label.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        fit_wrap(c.body, self.conn_label, self.capture_label, self.problem_label)

    def _commit_host(self) -> None:
        text = self.host_entry.get().strip()
        if text and webapi.Address.parse(text) is None:
            messagebox.showwarning(self.app.title(),
                                   f"“{text}” is not an address. Type an IP or "
                                   f"host name, optionally with :port for the "
                                   f"web side, or an https:// URL.")
            return
        if text == self.app.settings.get("dvl_host", ""):
            return
        self.app.settings["dvl_host"] = text
        self.app.save_settings()
        self.recorder.set_dvl_host(text)
        self.app._log(f"DVL address set to {text}." if text else
                      "DVL address: asking the BlueOS DVL extension.")

    def _open_folder(self) -> None:
        folder = self.recorder.folder or dvl_folder(self.app.flight_dir)
        if folder is None:
            messagebox.showinfo(self.app.title(), "No flight folder chosen — "
                                "the DVL is being shown live and nothing is "
                                "written. Choose one on Monitoring.")
            return
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)                       # noqa: S606
        except Exception:
            pass

    def _open_web(self) -> None:
        cap = self.recorder.capture
        addr = cap.address if cap is not None else None
        if addr is None:
            messagebox.showinfo(self.app.title(), "The DVL's address is not "
                                "known yet.")
            return
        webbrowser.open(addr.base_url)

    # ------------------------------------------------------------------
    #  2. did it reach the autopilot?
    # ------------------------------------------------------------------

    def _build_delivery(self, parent) -> None:
        c = Card(parent, "2.  Did it reach the autopilot?",
                 "What the DVL sent, beside what mavlink2rest counted arriving "
                 "from the BlueOS DVL extension. The stock extension forwards "
                 "only valid reports, so an invalid stretch on the left is a "
                 "gap on the right — and in the mcap.")
        c.grid(row=0, column=0, sticky="nsew")
        c.body.grid_columnconfigure(0, weight=0)
        c.body.grid_columnconfigure(1, weight=1)
        rows = [("dvl_vel", "DVL velocity reports"),
                ("dvl_dr", "DVL dead-reckoning reports"),
                ("ext_dist", "→ DISTANCE_SENSOR (extension)"),
                ("ext_gvpe", "→ GLOBAL_VISION_POSITION_ESTIMATE"),
                ("ext_vpd", "→ VISION_POSITION_DELTA"),
                ("ap_rf", "autopilot RANGEFINDER"),
                ("ext", "BlueOS DVL extension"),
                ("clients", "clients on the DVL's JSON output")]
        self.delivery: dict[str, ctk.CTkLabel] = {}
        for i, (key, name) in enumerate(rows):
            ctk.CTkLabel(c.body, text=name, font=T.FONT_SMALL,
                         text_color=T.TEXT_MUTED, anchor="w"
                         ).grid(row=i, column=0, sticky="w", padx=(0, 12))
            v = ctk.CTkLabel(c.body, text="—", font=T.FONT_MONO,
                             text_color=T.TEXT, anchor="w", justify="left")
            v.grid(row=i, column=1, sticky="w")
            self.delivery[key] = v

    # ------------------------------------------------------------------
    #  3. the beams
    # ------------------------------------------------------------------

    def _build_beams(self, parent, row: int) -> None:
        c = Card(parent, "3.  The four beams",
                 "Each transducer where it points on the vehicle (forward is "
                 "up), from Water Linked's transducer drawing and the DVL's "
                 "mounting offset. Confirm it once on the bench by covering one "
                 "transducer: its tile should go red.")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)
        self.beams = self._make_canvas(c.body, BEAMS_H)
        self.beams.grid(row=0, column=0, sticky="ew")
        self._beams_repaint = Repaint(self, self._draw_beams)
        self.beams.bind("<Configure>", self._beams_repaint.ask)

    def _draw_beams(self) -> None:
        cv = self.beams
        cv.delete("all")
        s = self._scale()
        w = cv.winfo_width()
        h = int(BEAMS_H * s)
        if int(cv.cget("height")) != h:
            cv.configure(height=h)
        if w < 200 * s:
            return
        snap = self.recorder.live.snapshot(history=False)
        c = self._colors()
        F = lambda font: T.scale_font(font, s)        # noqa: E731
        cx, cy = w / 2, h / 2
        bw, bh = 66 * s, 104 * s
        cv.create_rectangle(cx - bw, cy - bh, cx + bw, cy + bh, outline=c["border"],
                            width=max(1, int(2 * s)))
        cv.create_text(cx, cy - bh + 14 * s, text="▲ forward", fill=c["muted"],
                       font=F(FONT_CANVAS_SMALL))
        v = snap.velocity
        offset = snap.status.get("cfg_mounting_rotation_offset")
        off = offset if isinstance(offset, (int, float)) else 0.0
        valid = v.get("velocity_valid")
        if not v:
            cv.create_text(cx, cy, text="no report yet", fill=c["muted"],
                           font=F(FONT_CANVAS))
        else:
            cv.create_text(cx, cy - 50 * s, text=_f(v.get("altitude"), 2, "m"),
                           fill=c["text"], font=F(FONT_HUGE))
            cv.create_text(cx, cy - 30 * s, text="altitude", fill=c["muted"],
                           font=F(FONT_CANVAS_SMALL))
            cv.create_text(cx, cy - 8 * s,
                           text="VALID" if valid else "INVALID" if valid is False else "—",
                           fill=c["ok"] if valid else c["warn"], font=F(FONT_FLAG))
            cv.create_text(cx, cy + 14 * s,
                           text=f"vx {_f(v.get('vx'), 3)}  vy {_f(v.get('vy'), 3)}",
                           fill=c["text"], font=F(FONT_CANVAS))
            cv.create_text(cx, cy + 32 * s, text=f"vz {_f(v.get('vz'), 3)} m/s",
                           fill=c["text"], font=F(FONT_CANVAS))
            cv.create_text(cx, cy + 50 * s, text=f"FOM {_f(v.get('fom'), 4)}",
                           fill=c["muted"], font=F(FONT_CANVAS))
            if v.get("status_high_temperature"):
                cv.create_text(cx, cy + 70 * s, text="DVL HOT", fill=c["warn"],
                               font=F(FONT_FLAG))
        tile_w, tile_h = min(250 * s, w * 0.3), 92 * s
        for beam in P.BEAMS:
            az = math.radians(P.beam_azimuth_on_vehicle(beam, off))
            sx, sy = math.sin(az), -math.cos(az)
            bx = cx + w * 0.32 * (1 if sx > 1e-6 else -1 if sx < -1e-6 else 0)
            by = cy + h * 0.27 * (1 if sy > 1e-6 else -1 if sy < -1e-6 else 0)
            color = BEAM_COLORS[beam.id]
            tid = beam.id
            bvalid = v.get(f"t{tid}_valid")
            edge = c["warn"] if bvalid is False else color
            cv.create_line(cx + sx * bw * 0.85, cy + sy * bh * 0.85,
                           bx - (tile_w / 2) * (1 if sx > 0 else -1), by,
                           fill=color, width=max(1, int(2 * s)), dash=(4, 3))
            x0, y0 = bx - tile_w / 2, by - tile_h / 2
            cv.create_rectangle(x0, y0, x0 + tile_w, y0 + tile_h, outline=edge,
                                width=max(1, int((3 if bvalid is False else 2) * s)))
            pad = 9 * s
            cv.create_text(x0 + pad, y0 + 13 * s, anchor="w", fill=color,
                           font=F(FONT_TILE_TITLE),
                           text=f"T{beam.number} · id {tid} · {P.beam_position(beam, off)}")
            r = 5 * s
            cv.create_oval(x0 + tile_w - pad - 2 * r, y0 + 13 * s - r,
                           x0 + tile_w - pad, y0 + 13 * s + r,
                           fill=(c["ok"] if bvalid else c["warn"] if bvalid is False
                                 else c["border"]), outline="")
            cv.create_text(x0 + pad, y0 + 35 * s, anchor="w", fill=c["text"],
                           font=F(FONT_BIG), text=_f(v.get(f"t{tid}_distance"), 3, "m"))
            cv.create_text(x0 + pad, y0 + 57 * s, anchor="w", fill=c["text"],
                           font=F(FONT_CANVAS),
                           text=f"RSSI {_f(v.get(f't{tid}_rssi'), 1)}  "
                                f"NSD {_f(v.get(f't{tid}_nsd'), 1)} dBm")
            cv.create_text(x0 + pad, y0 + 76 * s, anchor="w", fill=c["muted"],
                           font=F(FONT_CANVAS),
                           text=f"SNR {_f(v.get(f't{tid}_snr_db'), 1)} dB  "
                                f"v {_f(v.get(f't{tid}_velocity'), 3)} m/s")
        note = (f"mounting offset {_f(offset, 0, '°')}" if offset is not None
                else "mounting offset not read yet (0° assumed)")
        cv.create_text(8 * s, h - 8 * s, anchor="sw", fill=c["muted"],
                       font=F(FONT_CANVAS_SMALL),
                       text=f"{note}  ·  T = transducer number on Water Linked's "
                            f"drawing; id = the protocol's number (T − 1)")

    # ------------------------------------------------------------------
    #  4. stream health
    # ------------------------------------------------------------------

    def _build_health(self, parent, row: int) -> None:
        c = Card(parent, "4.  Stream health",
                 "Gaps are read from the DVL's own clock and its own count of "
                 "milliseconds since its previous report, so each can be put on "
                 "one side of the link: the DVL made nothing (quiet), the DVL "
                 "made reports that never arrived (missing), or they arrived "
                 "late (stall).")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure((1, 3), weight=1)
        names = [("cadence", "report interval"), ("last", "last report"),
                 ("gaps", "gaps this capture"), ("invalid", "invalid velocity"),
                 ("latency", "DVL processing / network"), ("clock", "DVL clock vs laptop"),
                 ("cycling", "periodic cycling"), ("temp", "temperature / CPU"),
                 ("range", "range mode"), ("warnings", "DVL warnings"),
                 ("streams", "stream rates"), ("snaps", "acoustic snapshots")]
        self.health: dict[str, ctk.CTkLabel] = {}
        for i, (key, name) in enumerate(names):
            r, col = divmod(i, 2)
            ctk.CTkLabel(c.body, text=name, font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                         anchor="w").grid(row=r, column=col * 2, sticky="w",
                                          padx=(0 if col == 0 else 18, 10))
            v = ctk.CTkLabel(c.body, text="—", font=T.FONT_MONO, text_color=T.TEXT,
                             anchor="w", justify="left")
            v.grid(row=r, column=col * 2 + 1, sticky="w")
            self.health[key] = v
        self.events_box = output_box(c.body, wrap="none")
        self.events_box.grid(row=len(names) // 2 + 1, column=0, columnspan=4,
                             sticky="ew", pady=(10, 0))
        c.add_grip(self.events_box, self.events_box.min_height)
        say(self.events_box, "Events appear here as they happen, newest first; all "
                             "of them are in the capture's events file.")

    # ------------------------------------------------------------------
    #  5. live charts
    # ------------------------------------------------------------------

    def _build_charts(self, parent, row: int) -> None:
        c = Card(parent, "5.  Live charts",
                 "Shaded: velocity invalid (what the extension drops). Vertical "
                 "lines: gaps — grey the DVL was quiet, red reports went missing, "
                 "yellow they arrived late. Report interval is drawn on both "
                 "clocks: the DVL's (white) and this laptop's (red).")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)
        self.window_pick = _segmented(c.body, list(WINDOWS), self._pick_window)
        self.window_pick.set(self._window)
        self.window_pick.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.charts = self._make_canvas(c.body, STRIP_H * 6)
        self.charts.grid(row=1, column=0, sticky="ew")
        self._charts_repaint = Repaint(self, self._draw_charts)
        self.charts.bind("<Configure>", self._charts_repaint.ask)

    def _pick_window(self, name: str) -> None:
        self._window = name
        self._draw_charts()

    def _draw_charts(self) -> None:
        cv = self.charts
        cv.delete("all")
        s = self._scale()
        w = cv.winfo_width()
        if w < 200 * s:
            return
        F = lambda font: T.scale_font(font, s)        # noqa: E731
        span = WINDOWS[self._window]
        now = time.time()
        snap = self.recorder.live.snapshot(history_since=now - span)
        hist = snap.history
        c = self._colors()
        strip_h = STRIP_H * s
        x0, x1 = NAME_W * s, w - 60 * s
        pixels = max(1, int(x1 - x0))
        if self._charts_repaint.busy:
            pixels = max(1, pixels // 3)
        step = max(1, len(hist) // pixels)
        pts = hist[::step]

        def x_of(t):
            return x0 + max(0.0, min(1.0, (t - (now - span)) / span)) * (x1 - x0)

        # Invalid spans, as bands under every strip.
        bands, start = [], None
        for p in hist:
            if p.valid is False and start is None:
                start = p.t
            elif p.valid is not False and start is not None:
                bands.append((start, p.t))
                start = None
        if start is not None:
            bands.append((start, now))

        temps = [(t, deg) for t, deg in snap.temperature if t >= now - span]
        strips = [
            ("altitude / beams", "m",
             [(c["text"], [(p.t, p.altitude) for p in pts if p.valid])] +
             [(BEAM_COLORS[i], [(p.t, p.distance[i]) for p in pts]) for i in range(4)]),
            ("RSSI", "dBm", [(BEAM_COLORS[i], [(p.t, p.rssi[i]) for p in pts])
                             for i in range(4)]),
            ("NSD (noise)", "dBm", [(BEAM_COLORS[i], [(p.t, p.nsd[i]) for p in pts])
                                    for i in range(4)]),
            ("report interval", "ms: DVL / laptop",
             [(c["text"], [(p.t, p.d_tov_ms) for p in pts]),
              (brand.CORAL, [(p.t, p.d_rx_ms) for p in pts])]),
            ("FOM", "m/s", [(c["text"], [(p.t, p.fom) for p in pts if p.valid])]),
            ("DVL temperature", "°C", [(brand.CORAL, temps)]),
        ]
        total = int(strip_h * len(strips))
        if int(cv.cget("height")) != total:
            cv.configure(height=total)
        lw = max(1, int(1.5 * s))
        for i, (name, unit, series) in enumerate(strips):
            top = i * strip_h
            y0, y1 = top + 7 * s, top + strip_h - 7 * s
            if i:
                cv.create_line(6, top, w - 6, top, fill=c["border"])
            for a, b in bands:
                cv.create_rectangle(x_of(a), y0, x_of(b), y1, fill=c["shade"],
                                    outline="")
            cv.create_text(9 * s, top + strip_h / 2 - 8 * s, anchor="w", text=name,
                           fill=c["text"], font=F(FONT_CANVAS_SMALL))
            cv.create_text(9 * s, top + strip_h / 2 + 9 * s, anchor="w", text=unit,
                           fill=c["muted"], font=F(FONT_CANVAS_SMALL))
            values = [v for _c, ser in series for _t, v in ser
                      if isinstance(v, (int, float))]
            if len(values) < 2:
                cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="—",
                               fill=c["muted"], font=F(FONT_CANVAS_SMALL))
                continue
            lo, hi = min(values), max(values)
            if hi - lo < 1e-9:
                lo, hi = lo - 0.5, hi + 0.5
            for color, ser in series:
                coords: list[float] = []
                for t, v in ser:
                    if not isinstance(v, (int, float)):
                        if len(coords) >= 4:
                            cv.create_line(*coords, fill=color, width=lw)
                        coords = []
                        continue
                    coords += [x_of(t), y1 - (v - lo) / (hi - lo) * (y1 - y0)]
                if len(coords) >= 4:
                    cv.create_line(*coords, fill=color, width=lw)
            cv.create_text(w - 6 * s, y0, anchor="ne", text=_f(hi, 1),
                           fill=c["muted"], font=F(FONT_CANVAS_SMALL))
            cv.create_text(w - 6 * s, y1, anchor="se", text=_f(lo, 1),
                           fill=c["muted"], font=F(FONT_CANVAS_SMALL))
        for gap in snap.gaps:
            if gap.ended_unix < now - span:
                continue
            x = x_of(gap.ended_unix)
            cv.create_line(x, 0, x, total, fill=GAP_COLORS.get(gap.kind, c["text"]),
                           width=max(1, int(2 * s)))

    # ------------------------------------------------------------------
    #  6. echo and spectrum
    # ------------------------------------------------------------------

    def _build_acoustic(self, parent, row: int) -> None:
        c = Card(parent, "6.  Echo and spectrum",
                 "The DVL's own diagnostic views, read from its web API: each "
                 "beam's echo strength against range, and each beam's spectral "
                 "density (dBm) around the 1 MHz carrier. Each read is the "
                 "latest ping, written whole to the capture.")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=0)
        c.body.grid_columnconfigure(1, weight=1)
        label(c.body, "snapshots", muted=True).grid(row=0, column=0, padx=(0, 8),
                                                    sticky="w")
        self.snap_pick = _segmented(c.body, list(SNAPSHOT_CHOICES),
                                    self._pick_snapshots)
        try:
            hz = float(self.app.settings.get("dvl_snapshot_hz",
                                             CAP.DEFAULT_SNAPSHOT_HZ))
        except (TypeError, ValueError):
            hz = CAP.DEFAULT_SNAPSHOT_HZ
        self.snap_pick.set(next((k for k, v in SNAPSHOT_CHOICES.items() if v == hz),
                                "5 /s"))
        self.snap_pick.grid(row=0, column=1, sticky="w")
        self.acoustic = self._make_canvas(c.body, ACOUSTIC_H)
        self.acoustic.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._acoustic_repaint = Repaint(self, self._draw_acoustic)
        self.acoustic.bind("<Configure>", self._acoustic_repaint.ask)

    def _pick_snapshots(self, name: str) -> None:
        hz = SNAPSHOT_CHOICES.get(name, 0.0)
        self.app.settings["dvl_snapshot_hz"] = hz
        self.app.save_settings()
        self.recorder.set_snapshot_hz(hz)

    def _draw_acoustic(self) -> None:
        cv = self.acoustic
        cv.delete("all")
        s = self._scale()
        h = int(ACOUSTIC_H * s)
        if int(cv.cget("height")) != h:
            cv.configure(height=h)
        w = cv.winfo_width()
        if w < 200 * s:
            return
        snap = self.recorder.live.snapshot(history=False)
        half = w / 2
        self._panel(cv, 0, half, h, s, snap.echo, "echo strength", "range (m)")
        self._panel(cv, half, w, h, s, snap.spectrum, "spectral density",
                    "frequency (kHz)")

    def _panel(self, cv, left, right, h, s, data: dict, title, xname) -> None:
        c = self._colors()
        F = lambda font: T.scale_font(font, s)        # noqa: E731
        x0, x1 = left + 46 * s, right - 12 * s
        y0, y1 = 24 * s, h - 28 * s
        cv.create_text(left + 9 * s, 11 * s, anchor="w", text=f"{title} (dBm)",
                       fill=c["text"], font=F(FONT_CANVAS_SMALL))
        rows = data.get("data") if data else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
            cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, fill=c["muted"],
                           font=F(FONT_CANVAS_SMALL),
                           text="no snapshot yet" if self.recorder.snapshot_hz
                           else "snapshots are off")
            return
        xs, ys = float(data.get("x_scale") or 1), float(data.get("y_scale") or 1)
        xo, yo = float(data.get("x_offset") or 0), float(data.get("y_offset") or 0)
        n, width = len(rows), min(4, len(rows[0]))
        vals = [r[b] * ys + yo for r in rows for b in range(width)
                if isinstance(r[b], (int, float)) and r[b] > -1e9]
        if not vals:
            return
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        cv.create_rectangle(x0, y0, x1, y1, outline=c["border"])
        step = max(1, n // max(1, int(x1 - x0)))
        for b in range(width):
            coords: list[float] = []
            for k in range(0, n, step):
                v = rows[k][b]
                if not isinstance(v, (int, float)) or v < -1e9:
                    continue
                coords += [x0 + k / max(1, n - 1) * (x1 - x0),
                           y1 - ((v * ys + yo) - lo) / (hi - lo) * (y1 - y0)]
            if len(coords) >= 4:
                cv.create_line(*coords, fill=BEAM_COLORS[b], width=max(1, int(s)))
        small = F(FONT_CANVAS_SMALL)
        cv.create_text(x0 - 4 * s, y0, anchor="ne", text=_f(hi, 0), fill=c["muted"],
                       font=small)
        cv.create_text(x0 - 4 * s, y1, anchor="se", text=_f(lo, 0), fill=c["muted"],
                       font=small)
        cv.create_text(x0, y1 + 12 * s, anchor="w", text=_f(xo, 1), fill=c["muted"],
                       font=small)
        cv.create_text(x1, y1 + 12 * s, anchor="e", text=_f(xo + (n - 1) * xs, 1),
                       fill=c["muted"], font=small)
        age = time.time() - data["t"] if isinstance(data.get("t"), float) else None
        cv.create_text((x0 + x1) / 2, y1 + 12 * s, fill=c["muted"], font=small,
                       text=xname + (f"  ·  {age:.1f} s old" if age is not None else ""))

    # ------------------------------------------------------------------
    #  7. the diagnostic log
    # ------------------------------------------------------------------

    def _build_diagnostic(self, parent, row: int) -> None:
        c = Card(parent, "7.  Water Linked diagnostic log",
                 "The DVL records its own internal logs for a set time and "
                 "returns them as a file for Water Linked support — the one "
                 "thing it will not stream. Only with the vehicle confirmed "
                 "disarmed. The capture keeps running throughout, so it will "
                 "show whether the DVL's output changes while it records.")
        c.grid(row=row, column=0, sticky="ew")
        c.body.grid_columnconfigure(0, weight=0)
        c.body.grid_columnconfigure(1, weight=1)
        label(c.body, "what is happening", muted=True).grid(row=0, column=0,
                                                            padx=(0, 8), sticky="w")
        self.diag_desc = entry(c.body, "e.g. Nereo on deck, tank test: beam 2 "
                                       "flickers invalid", width=420)
        self.diag_desc.grid(row=0, column=1, sticky="ew")
        r = ctk.CTkFrame(c.body, fg_color="transparent")
        r.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.diag_pick = _segmented(r, list(DURATION_CHOICES))
        self.diag_pick.set("15 s")
        self.diag_pick.grid(row=0, column=0)
        button(r, "Collect diagnostic log", self._collect, "primary", width=200
               ).grid(row=0, column=1, padx=(10, 0))
        self.diag_label = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                       text_color=T.TEXT_MUTED, anchor="w",
                                       justify="left")
        self.diag_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        fit_wrap(c.body, self.diag_label)

    def armed_state(self) -> tuple[bool | None, bool]:
        """(armed, current), from the flight recorder's arm poll."""
        rec = getattr(self.app, "recorder", None)
        if rec is None or not rec.watching:
            return None, False
        st = rec.status
        return st.armed, bool(st.reachable)

    def _collect(self) -> None:
        cap = self.recorder.capture
        addr = cap.address if cap is not None else None
        if addr is None:
            messagebox.showinfo(self.app.title(), "The DVL's address is not known yet.")
            return
        seconds = DURATION_CHOICES.get(self.diag_pick.get(), 15)
        desc = self.diag_desc.get().strip()
        try:
            if not desc:
                raise DIAG.Refused("Type what is happening first — Water Linked's "
                                   "form needs a description.")
            DIAG.require_disarmed(self.armed_state)
        except DIAG.Refused as ex:
            messagebox.showwarning(self.app.title(), f"Not collected: {ex}")
            return
        folder = dvl_folder(self.app.flight_dir) or (
            Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "CCR_ROV" / "rov_flight_ops" / "dvl_diagnostic")
        if not messagebox.askyesno(
                self.app.title(),
                f"Ask the DVL at {addr} to record for {seconds} s and send its "
                f"diagnostic log?\n\nIt will be saved in\n{folder}\n\nKeep the "
                f"vehicle disarmed until it has finished."):
            return
        state = self.armed_state

        def work(progress, cancel):
            return DIAG.collect(addr, seconds=seconds, description=desc,
                                folder=folder, armed_state=state, cancel=cancel,
                                progress=progress)

        self.diag_label.configure(text=f"Collecting ({seconds} s, then packaging)…",
                                  text_color=T.TEXT_MUTED)
        self.app.submit(work, f"Collecting a {seconds} s DVL diagnostic log…",
                        on_done=self._collected)

    def _collected(self, res) -> None:
        if isinstance(res, BaseException) or res is None:
            self.diag_label.configure(text=f"Not collected: {res}", text_color=T.WARN)
            return
        cap = self.recorder.capture
        if cap is not None:
            try:
                cap.note_diagnostic(asdict(res))
            except Exception:
                diagnostics.log_exception("recording a DVL diagnostic log",
                                          *sys.exc_info())
        if res.ok:
            self.diag_label.configure(
                text=f"Saved {Path(res.path).name} — {res.bytes / 1e6:.2f} MB, "
                     f"SHA-256 {res.sha256[:12]}…", text_color=T.OK)
            self.app._log(f"DVL diagnostic log saved: {res.path}")
        else:
            self.diag_label.configure(text=f"Not collected: {res.error}",
                                      text_color=T.WARN)
            self.app._log(f"DVL diagnostic log failed: {res.error}")

    # ------------------------------------------------------------------
    #  wiring
    # ------------------------------------------------------------------

    @property
    def recorder(self) -> DvlRecorder:
        return self.app.dvl_recorder()

    def refresh(self) -> None:
        """Called when the tab is opened, and when a flight folder is chosen.

        Opened with no folder chosen, this starts a live view, so the tab
        shows the DVL before anything is being recorded -- on deck, before
        the dive. Only when the tab is actually the one on screen: choosing a
        folder refreshes every page, and the capture for a folder is started
        by the application (`App._arm_dvl`), not by whichever pages happen to
        be refreshed with it.
        """
        nav = getattr(self.app, "nav", None)
        if nav is None or nav.current == "DVL":
            try:
                self.recorder.ensure_running(dvl_folder(self.app.flight_dir))
            except Exception:
                diagnostics.log_exception("starting the DVL view", *sys.exc_info())
        if not self._ticking:
            self._ticking = True
            self._tick()

    def refresh_theme(self) -> None:
        for cv in (self.beams, self.charts, self.acoustic):
            cv.configure(background=self._apply_appearance_mode(T.FIELD_BG),
                         highlightbackground=self._apply_appearance_mode(T.BORDER))
        self._draw_beams()
        self._draw_charts()
        self._draw_acoustic()

    def _tick(self) -> None:
        """Twice a second while on screen; nothing at all while it is not."""
        try:
            if self.beams.winfo_ismapped():
                self._update_text()
                self._draw_beams()
                now = time.monotonic()
                if now - self._last_chart >= CHART_EVERY_S:
                    self._last_chart = now
                    self._draw_charts()
                key = (self.recorder.live.snapshot(history=False).echo.get("t"),
                       self.recorder.snapshot_hz)
                if key != self._acoustic_shown:
                    self._acoustic_shown = key
                    self._draw_acoustic()
        except Exception:
            diagnostics.log_exception("DVL page", *sys.exc_info(),
                                      level=logging.WARNING)
        self.after(TICK_MS, self._tick)

    def _update_text(self) -> None:
        rec = self.recorder
        s = rec.live.snapshot(history=False)
        # 1. connection
        lamp = lambda on: "●" if on else "○"           # noqa: E731
        addr = s.address or "address unknown"
        src = f"  ({s.address_source})" if s.address_source else ""
        tcp = (f"{lamp(s.tcp_connected)} TCP stream "
               + (f"port {s.tcp_port}, connection {s.tcp_connections}"
                  if s.tcp_connected else (s.tcp_error or "not connected")))
        ws = f"{lamp(s.ws_connected)} web stream " + ("" if s.ws_connected
                                                      else (s.ws_error or "not connected"))
        api_ok = not s.web_error and bool(s.status.get("reachable"))
        web = f"{lamp(api_ok)} web API " + (s.web_error or "")
        self.conn_label.configure(text=f"DVL {addr}{src}\n{tcp}\n{ws}\n{web}",
                                  text_color=T.OK if s.tcp_connected else T.TEXT)
        if s.capturing:
            mb = s.bytes_in / 1e6
            cap_text = (f"Recording capture {s.capture_id} into {s.folder}\n"
                        f"{s.lines_in:,} TCP lines · {mb:.1f} MB from the stream · "
                        f"{len(s.files)} files")
        elif rec.transition:
            cap_text = f"{rec.transition}…"
        else:
            cap_text = ("Live view only — nothing is written. Choose a flight "
                        "folder on Monitoring to record the DVL.")
        self.capture_label.configure(text=cap_text)
        problem = s.problem or ("; ".join(s.notes[-2:]) if s.notes else "")
        self.problem_label.configure(text=problem,
                                     text_color=T.WARN if s.problem else T.TEXT_MUTED)

        # 2. delivery
        d = self.delivery
        d["dvl_vel"].configure(text=f"{_rate(s, 'velocity')}  "
                                    f"(valid {_rate(s, 'velocity_valid')}, "
                                    f"invalid {_rate(s, 'velocity_invalid')})")
        d["dvl_dr"].configure(text=_rate(s, "position_local"))

        down = s.mavlink.get("_down") or ""

        def mav(key):
            m = s.mavlink.get(key) or {}
            if down and (not m or m.get("counter") is None):
                return f"mavlink2rest not answering ({down[:60]})"
            if not m:
                return "not read yet" if rec.vehicle else "vehicle not read"
            if m.get("counter") is None:
                return m.get("error") or "—"
            rate = m.get("rate")
            return ((f"{rate:.1f}/s" if isinstance(rate, (int, float)) else "…")
                    + f"  (count {m['counter']:,})")
        d["ext_dist"].configure(text=mav("255/0/DISTANCE_SENSOR"))
        d["ext_gvpe"].configure(text=mav("255/0/GLOBAL_VISION_POSITION_ESTIMATE"))
        d["ext_vpd"].configure(text=mav("255/0/VISION_POSITION_DELTA"))
        d["ap_rf"].configure(text=mav("1/1/RANGEFINDER"))
        st = s.status
        if st.get("ext_reachable"):
            d["ext"].configure(text=f"{st.get('ext_status') or '?'} · sends "
                                    f"{st.get('ext_should_send') or '?'} · rangefinder "
                                    f"{_f(st.get('ext_rangefinder'))}")
        else:
            d["ext"].configure(text="not answering" if st.get("ext_reachable") is False
                               else "—")
        clients = st.get("json_clients")
        d["clients"].configure(text=(f"{clients}  (this program is one of them)"
                                     if clients is not None else "—"))

        # 4. health
        h = self.health
        med = s.median_ms
        h["cadence"].configure(text=(f"median {med:.0f} ms ≈ {1000 / med:.1f} Hz"
                                     if med else "—"))
        h["last"].configure(text=(f"{s.last_rx_age_s:.1f} s ago"
                                  if s.last_rx_age_s is not None else "—"))
        cad = s.cadence or {}
        gaps = cad.get("gaps") or {}
        gap_ms = cad.get("gap_ms") or {}
        h["gaps"].configure(text=("  ·  ".join(
            f"{GAP_WORDS[k]} {gaps.get(k, 0)}"
            + (f" ({gap_ms.get(k, 0) / 1000:.1f} s)" if gaps.get(k) else "")
            for k in ("dvl_quiet", "missing_reports", "delivery_stall"))
            + (f"\n~{cad.get('missing_reports_estimate', 0)} report(s) made and never "
               f"received" if cad.get("missing_reports_estimate") else ""))
            if cad else "—")
        h["invalid"].configure(text=(f"{cad.get('invalid_reports', 0):,} reports in "
                                     f"{cad.get('invalid_spans', 0)} stretch(es), "
                                     f"{cad.get('invalid_ms', 0) / 1000:.1f} s"
                                     if cad else "—"))
        v = s.velocity
        h["latency"].configure(text=f"{_f(v.get('tx_minus_tov_ms'), 1, 'ms')} / "
                                    f"{_f(v.get('rx_minus_tx_ms'), 1, 'ms')}")
        h["clock"].configure(text=(f"{_f(st.get('dvl_clock_minus_laptop_ms'), 0, 'ms')} "
                                   f"± {_f(st.get('clock_uncertainty_ms'), 0)} · NTP "
                                   f"{'synced' if st.get('ntp_synchronized') else 'not synced'}"
                                   if st.get("dvl_clock_utc") else "—"))
        cyc = st.get("cfg_periodic_cycling_enabled")
        in_cycle = s.rates.get("ws_periodic_cycling", 0) > 0
        h["cycling"].configure(
            text=("ON — the DVL drops reports every 10 s" if cyc else
                  "off" if cyc is False else "—") + ("  (checking now)" if in_cycle else ""),
            text_color=T.WARN if cyc or in_cycle else T.TEXT)
        temp = st.get("temperature_c")
        h["temp"].configure(text=f"{_f(temp, 1, '°C')} / {_f(st.get('cpu_load'), 2)}",
                            text_color=T.WARN if isinstance(temp, (int, float))
                            and temp >= CAP.HOT_C else T.TEXT)
        run = s.ws_velocity.get("run_config")
        expected = P.RANGE_MODES.get(int(run)) if isinstance(run, (int, float)) else None
        h["range"].configure(text=f"setting {_f(st.get('cfg_range_mode'))} · running "
                                  f"{_f(run, 0)}"
                                  + (f" (Water Linked: {expected[2]})" if expected else ""))
        h["warnings"].configure(text=st.get("warnings") or ("none" if st else "—"),
                                text_color=T.WARN if st.get("warnings") else T.TEXT)
        h["streams"].configure(text=f"TCP {_rate(s, 'velocity')} vel · "
                                    f"web {_rate(s, 'ws')} msgs · "
                                    f"orientation {_rate(s, 'ws_roll_pitch_yaw')}")
        h["snaps"].configure(text=(f"{rec.snapshot_hz:g}/s asked · "
                                   f"{_rate(s, 'echo')} achieved"
                                   if rec.snapshot_hz else "off"))
        if s.events and s.events[-1] != self._events_shown:
            self._events_shown = s.events[-1]
            lines = [time.strftime("%H:%M:%S", time.localtime(when)) + "  " + text
                     for when, text in s.events[-40:]]
            say(self.events_box, "\n".join(reversed(lines)))
