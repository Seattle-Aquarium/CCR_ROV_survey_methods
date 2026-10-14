"""
Lights: the four DeepSea lights on Lutris, from the BlueOS Light Monitor.

The page reads the application's `LightsClient` (``lights/client.py``), which
reads the extension's ``/api/v1/state`` -- the same document the extension's
own page draws -- so the two always agree. Nothing here talks to the light
controller; the extension on the Pi is the only thing that does.

    +---------------------------------------------------------------+
    |  1. The Light Monitor          (where, what, what is recorded) |
    +----------------+---------------------------+------------------+
    |  1 front left  |                           |  4 front right   |
    +----------------+    Lutris from above,     +------------------+
    |  2 rear left   |    a ring per light       |  3 rear right    |
    +----------------+---------------------------+------------------+
    |  Power         |  Controller               |  Events          |
    +----------------+---------------------------+------------------+

**Measured or expected.** Each light's level, temperatures, power-on count
and flags are the light's own answers. Per-light watts and amps are not
measured -- one monitor's worth of wiring serves all four lights, and Lutris
has no monitor fitted (13 October 2026) -- so they are shown with "≈" as what
the light should draw at the level it reports. The vehicle's voltage and
current are ArduSub's measurement.

`LightsStrip` at the bottom is the one-row version that sits under the map on
Navigation.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import tkinter
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from .. import brand, diagnostics
from ..lights import lights_folder
from . import theme as T
from .widgets import Card, Repaint, button, fit_wrap, output_box, say

log = logging.getLogger(__name__)

TICK_MS = 500
SCHEMATIC_H = 470
#: Where each light is drawn, as a fraction of the drawing, and its name.
LAMPS = {1: (-1, -1, "Front left"), 2: (-1, 1, "Rear left"),
         3: (1, 1, "Rear right"), 4: (1, -1, "Front right")}
BAD = {"not_answering", "stale", "fault", "mismatch", "standby"}

#: Fills for a light's badge, by kind. Bright colors carry Fathom type and are
#: used only as fills, never as type on a light ground.
BADGE = {
    "on": (brand.ALGAE, brand.FATHOM),
    "bad": (brand.CORAL, brand.FATHOM),
}

FONT_LEVEL = (T.FAMILY_SEMIBOLD, 30)
FONT_BADGE = (T.FAMILY_SEMIBOLD, 13)
FONT_CANVAS = (T.FAMILY_SEMIBOLD, 10)
FONT_CANVAS_NUM = (T.FAMILY_SEMIBOLD, 13)


def kind_of(state: str | None) -> str:
    if state in BAD:
        return "bad"
    if state in ("on", "off"):
        return state
    return "unknown"


def fmt(value, nd: int = 1, unit: str = "") -> str:
    """A number for a label, or a dash -- never a zero standing in for unknown."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        text = f"{value:,.{nd}f}"
        if text.startswith("-") and not text.strip("-0.,"):
            text = text[1:]
        return f"{text} {unit}".rstrip()
    return str(value)


def ago(seconds) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _set(widget, **kw) -> None:
    """Configure a label only when something changed -- redraws are not free."""
    changed = {}
    for k, v in kw.items():
        try:
            if widget.cget(k) != v:
                changed[k] = v
        except Exception:
            changed[k] = v
    if changed:
        widget.configure(**changed)


def light_line(lt: dict) -> tuple[str, str]:
    """(value, note) for a light in one short line each."""
    k = kind_of(lt.get("state"))
    if lt.get("answering") is False or k == "bad" and lt.get("level_pct") is None:
        return lt.get("state_text", "—").split(" (")[0], ""
    lvl = lt.get("level_pct")
    value = "—" if lvl is None else f"{lvl:.0f}%"
    note = (f"≈{fmt(lt.get('expected_w'), 0, 'W')} · ≈{fmt(lt.get('expected_a'), 1, 'A')}"
            if lt.get("expected_w") is not None else "")
    return value, note


# ==========================================================================
#  one light's card
# ==========================================================================


class LightCard(ctk.CTkFrame):

    ROWS = (("expected", "Expected draw"), ("temps", "Temperatures"),
            ("max", "Highest recorded"), ("humid", "Humidity"),
            ("power_ons", "Power-ons"), ("runtime", "Run time"),
            ("flags", "Fault flags"), ("last", "Last answer"),
            ("serial", "Serial · firmware"))

    def __init__(self, master, light_id: int, name: str):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER)
        self.light_id = light_id
        self.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=T.PAD, pady=(T.PAD, 2))
        head.grid_columnconfigure(1, weight=1)
        self.badge = ctk.CTkLabel(head, text=str(light_id), width=30, height=30,
                                  corner_radius=15, font=FONT_BADGE,
                                  fg_color=T.SURFACE_ALT, text_color=T.TEXT)
        self.badge.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.name = ctk.CTkLabel(head, text=name, font=T.FONT_H1, text_color=T.HEADING,
                                 anchor="w")
        self.name.grid(row=0, column=1, sticky="w")
        self.state = ctk.CTkLabel(head, text="Waiting for the Light Monitor",
                                  font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w")
        self.state.grid(row=1, column=1, sticky="w")

        lv = ctk.CTkFrame(self, fg_color="transparent")
        lv.grid(row=1, column=0, sticky="ew", padx=T.PAD)
        self.level = ctk.CTkLabel(lv, text="—", font=FONT_LEVEL, text_color=T.TEXT)
        self.level.grid(row=0, column=0, sticky="w")
        self.told = ctk.CTkLabel(lv, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED)
        self.told.grid(row=0, column=1, sticky="sw", padx=(10, 0), pady=(0, 6))
        self.bar = ctk.CTkProgressBar(self, height=8, corner_radius=4,
                                      fg_color=T.BORDER,
                                      progress_color=(brand.MEDITERRANEAN, brand.SEAFOAM))
        self.bar.set(0)
        self.bar.grid(row=2, column=0, sticky="ew", padx=T.PAD, pady=(2, 8))

        kv = ctk.CTkFrame(self, fg_color="transparent")
        kv.grid(row=3, column=0, sticky="ew", padx=T.PAD, pady=(0, T.PAD))
        kv.grid_columnconfigure(1, weight=1)
        self.values: dict[str, ctk.CTkLabel] = {}
        for i, (key, text) in enumerate(self.ROWS):
            ctk.CTkLabel(kv, text=text, font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                         anchor="w").grid(row=i, column=0, sticky="w", padx=(0, 12))
            v = ctk.CTkLabel(kv, text="—", font=T.FONT_SMALL, text_color=T.TEXT, anchor="e")
            v.grid(row=i, column=1, sticky="e")
            self.values[key] = v

    def show(self, lt: dict | None) -> None:
        if lt is None:
            return
        k = kind_of(lt.get("state"))
        fill, ink = BADGE.get(k, (T.SURFACE_ALT, T.TEXT))
        _set(self.badge, fg_color=fill, text_color=ink)
        _set(self.name, text=lt.get("name") or self.name.cget("text"))
        _set(self.state, text=lt.get("state_text") or "—",
             text_color=T.WARN if k == "bad" else T.TEXT_MUTED)
        _set(self, border_color=T.WARN if k == "bad" else T.BORDER)
        # A light that has stopped answering has no current level: its last
        # report is history, not "100%" for a light that may be dark.
        lvl = lt.get("level_pct")
        silent = lt.get("answering") is False
        _set(self.level, text="—" if silent or lvl is None else f"{lvl:.0f}%")
        told = lt.get("commanded_pct")
        told_text = "" if told is None else f"told {told:.0f}%"
        if silent and lvl is not None:
            told_text = f"last reported {lvl:.0f}%" + (f" · {told_text}" if told_text else "")
        _set(self.told, text=told_text)
        self.bar.set(0.0 if silent else max(0.0, min(1.0, (lvl or 0) / 100.0)))
        temps = [t for t in (lt.get("temp2_c"), lt.get("temp3_c")) if t is not None]
        restarts = lt.get("restarts_this_session") or 0
        info = lt.get("info") or {}
        pc = lt.get("power_cycles")
        rows = {
            "expected": (f"≈ {fmt(lt.get('expected_w'), 0, 'W')} · "
                         f"≈ {fmt(lt.get('expected_a'), 1, 'A')}"
                         if lt.get("expected_w") is not None else "—", False),
            "temps": (" · ".join(fmt(t, 1) for t in temps) + " °C" if temps else "—", False),
            "max": (fmt(lt.get("max_temp_c"), 1, "°C"), False),
            "humid": (fmt(lt.get("humidity_pct"), 0, "%"), False),
            "power_ons": ("—" if pc is None else
                          f"{pc}" + (f"  (+{restarts} this session)" if restarts else ""),
                          bool(restarts)),
            "runtime": (fmt(lt.get("runtime_h"), 1, "h"), False),
            "flags": (lt.get("flags_hex") or "—", bool(lt.get("flags"))),
            "last": (ago(lt.get("last_answer_age_s")), False),
            "serial": (f"{info.get('serial')} · {info.get('firmware') or '?'}"
                       if info.get("serial") else "—", False),
        }
        for key, (text, warn) in rows.items():
            _set(self.values[key], text=text, text_color=T.WARN if warn else T.TEXT)


# ==========================================================================
#  the page
# ==========================================================================


class LightsPage(ctk.CTkFrame):

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._ticking = False
        self._events_shown = None
        self._state: dict | None = None

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self.body = body

        self._build_connection(body, 0)
        stage = ctk.CTkFrame(body, fg_color="transparent")
        stage.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        stage.grid_columnconfigure((0, 2), weight=3, uniform="lights")
        stage.grid_columnconfigure(1, weight=2, uniform="lights")
        self.cards: dict[int, LightCard] = {}
        for lid, (sx, sy, name) in LAMPS.items():
            card = LightCard(stage, lid, name)
            card.grid(row=0 if sy < 0 else 1, column=0 if sx < 0 else 2, sticky="nsew",
                      padx=(0, 6) if sx < 0 else (6, 0), pady=(0, 6) if sy < 0 else (6, 0))
            self.cards[lid] = card
        self.schematic = tkinter.Canvas(
            stage, height=int(SCHEMATIC_H * self._scale()), highlightthickness=0,
            borderwidth=0, background=self._apply_appearance_mode(T.BG))
        self.schematic.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=6)
        self._repaint = Repaint(self, self._draw_schematic)
        self.schematic.bind("<Configure>", self._repaint.ask)

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew")
        row.grid_columnconfigure((0, 1, 2), weight=1, uniform="lrow")
        self._build_power(row, 0)
        self._build_controller(row, 1)
        self._build_events(row, 2)

    # -- building -----------------------------------------------------------

    def _scale(self) -> float:
        return T.scale_of(self) or 1.0

    def _build_connection(self, parent, row: int) -> None:
        c = Card(parent, "1.  The Light Monitor",
                 "The BlueOS extension that reads the light controller on the Pi's USB. "
                 "This tab reads the extension, never the controller, and keeps a copy "
                 "of the extension's recording in the flight folder's logs\\lights.")
        c.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)
        self.conn_label = ctk.CTkLabel(c.body, text="", font=T.FONT_BODY, text_color=T.TEXT,
                                       anchor="w", justify="left")
        self.conn_label.grid(row=0, column=0, sticky="ew")
        self.copy_label = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                       text_color=T.TEXT_MUTED, anchor="w", justify="left")
        self.copy_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        b = ctk.CTkFrame(c.body, fg_color="transparent")
        b.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        button(b, "Open the extension", self._open_web, "ghost", width=150
               ).grid(row=0, column=0)
        button(b, "Open lights folder", self._open_folder, "ghost", width=150
               ).grid(row=0, column=1, padx=(8, 0))
        fit_wrap(c.body, self.conn_label, self.copy_label, margin=340)

    def _kv(self, parent, rows) -> dict[str, ctk.CTkLabel]:
        out = {}
        parent.grid_columnconfigure(1, weight=1)
        for i, (key, text) in enumerate(rows):
            ctk.CTkLabel(parent, text=text, font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                         anchor="w").grid(row=i, column=0, sticky="w", padx=(0, 12), pady=1)
            v = ctk.CTkLabel(parent, text="—", font=T.FONT_SMALL, text_color=T.TEXT,
                             anchor="e", justify="right")
            v.grid(row=i, column=1, sticky="e", pady=1)
            out[key] = v
        return out

    def _build_power(self, parent, col: int) -> None:
        c = Card(parent, "Power",
                 "Vehicle: ArduSub's measurement. Lights: what they should draw at "
                 "the levels they report (≈), unless a power monitor is fitted.")
        c.grid(row=0, column=col, sticky="nsew", padx=(0, 6))
        self.power = self._kv(c.body, (
            ("veh_v", "Vehicle supply"), ("veh_a", "Vehicle current"),
            ("veh_w", "Vehicle power"), ("rail", "Light supply"),
            ("exp_w", "Lights, expected"), ("exp_a", "Lights, expected current"),
            ("pilot", "Pilot asked · gain")))

    def _build_controller(self, parent, col: int) -> None:
        c = Card(parent, "Controller",
                 "The QT Py (or Pico) in the light box, as the extension hears it.")
        c.grid(row=0, column=col, sticky="nsew", padx=6)
        self.ctl = self._kv(c.body, (
            ("port", "Port"), ("board", "Board"), ("fw", "Firmware"),
            ("mpy", "MicroPython"), ("up", "Up for"), ("pwm", "PWM in"),
            ("cmd", "Commanding"), ("lines", "Lines"), ("lost", "Lost lines"),
            ("bus", "Light bus"), ("restarts", "Restarts seen"),
            ("mav", "MAVLink")))

    def _build_events(self, parent, col: int) -> None:
        c = Card(parent, "Events", "From the extension, newest first. Warnings also go "
                 "to Cockpit and QGC as STATUSTEXT, and into the tlog / mcap.")
        c.grid(row=0, column=col, sticky="nsew", padx=(6, 0))
        # One line tall like every report box in the program; its handle
        # drags it open.
        self.events_box = output_box(c.body)
        self.events_box.grid(row=0, column=0, sticky="nsew")
        c.add_grip(self.events_box, self.events_box.min_height)
        say(self.events_box, "Nothing yet.")

    # -- the drawing ----------------------------------------------------------

    def _draw_schematic(self) -> None:
        cv = self.schematic
        cv.delete("all")
        s = self._scale()
        w = cv.winfo_width()
        h = int(SCHEMATIC_H * s)
        if int(cv.cget("height")) != h:
            cv.configure(height=h)
        if w < 120 * s:
            return
        m = self._apply_appearance_mode
        text, muted, border = m(T.TEXT), m(T.TEXT_MUTED), m(T.BORDER)
        surface, alt = m(T.SURFACE), m(T.SURFACE_ALT)
        F = lambda font: T.scale_font(font, s)                    # noqa: E731
        cx, cy = w / 2, h / 2
        fw, fh = min(w * 0.42, 150 * s), 250 * s
        # forward
        cv.create_polygon(cx, cy - fh / 2 - 46 * s, cx + 9 * s, cy - fh / 2 - 32 * s,
                          cx - 9 * s, cy - fh / 2 - 32 * s, fill=muted, outline="")
        cv.create_text(cx, cy - fh / 2 - 20 * s, text="Forward", fill=muted,
                       font=F(FONT_CANVAS))
        # frame, tubes, thrusters
        x0, y0, x1, y1 = cx - fw / 2, cy - fh / 2, cx + fw / 2, cy + fh / 2
        cv.create_rectangle(x0, y0, x1, y1, fill=alt, outline=border, width=max(1, int(2 * s)))
        tw = fw * 0.38
        cv.create_rectangle(cx - tw / 2, y0 + 14 * s, cx + tw / 2, y0 + fh * 0.58,
                            fill=surface, outline=border, width=max(1, int(2 * s)))
        cv.create_rectangle(cx - tw * 0.42, y0 + fh * 0.64, cx + tw * 0.42, y1 - 14 * s,
                            fill=surface, outline=border, width=max(1, int(2 * s)))
        r = 9 * s
        for tx, ty in ((x0 + 14 * s, y0 + 16 * s), (x1 - 14 * s, y0 + 16 * s),
                       (x0 + 14 * s, y1 - 16 * s), (x1 - 14 * s, y1 - 16 * s),
                       (x0 + 16 * s, cy - 30 * s), (x1 - 16 * s, cy - 30 * s),
                       (x0 + 16 * s, cy + 30 * s), (x1 - 16 * s, cy + 30 * s)):
            cv.create_oval(tx - r, ty - r, tx + r, ty + r, fill=surface, outline=border,
                           width=max(1, int(2 * s)))
        # the lights
        lights = {lt["id"]: lt for lt in (self._state or {}).get("lights", [])}
        R, ring = 18 * s, 25 * s
        for lid, (sx, sy, name) in LAMPS.items():
            lx = cx + sx * (fw / 2 + 34 * s)
            ly = cy + sy * (fh / 2 - 34 * s)
            lt = lights.get(lid, {})
            k = kind_of(lt.get("state"))
            lvl = lt.get("level_pct") if lt.get("answering") is not False else 0
            cv.create_oval(lx - ring, ly - ring, lx + ring, ly + ring, outline=border,
                           width=max(2, int(5 * s)))
            frac = max(0.0, min(1.0, (lvl or 0) / 100.0))
            if frac > 0:
                cv.create_arc(lx - ring, ly - ring, lx + ring, ly + ring, start=90,
                              extent=-359.9 * frac, style="arc",
                              outline=brand.SEAFOAM, width=max(2, int(5 * s)))
            fill = {"on": brand.ALGAE, "bad": brand.CORAL}.get(k, surface)
            ring_col = {"on": brand.ALGAE, "bad": brand.CORAL, "off": brand.SEAFOAM}.get(k, border)
            ink = brand.FATHOM if k in ("on", "bad") else text
            cv.create_oval(lx - R, ly - R, lx + R, ly + R, fill=fill, outline=ring_col,
                           width=max(1, int(2 * s)))
            cv.create_text(lx, ly, text=str(lid), fill=ink, font=F(FONT_CANVAS_NUM))
            sub = (lt.get("state_text", "").split(" (")[0] if k == "bad" else
                   "—" if lt.get("level_pct") is None else f"{lt['level_pct']:.0f}%")
            tone = m(T.WARN) if k == "bad" else muted
            # Name then level, stacked away from the ring: above the front
            # lights (anchored by their bottom edge), below the rear ones.
            gap, line = 8 * s, 17 * s
            if sy < 0:
                cv.create_text(lx, ly - ring - gap, text=sub, fill=tone,
                               font=F(FONT_CANVAS), anchor="s")
                cv.create_text(lx, ly - ring - gap - line, text=name, fill=text,
                               font=F(FONT_CANVAS), anchor="s")
            else:
                cv.create_text(lx, ly + ring + gap, text=name, fill=text,
                               font=F(FONT_CANVAS), anchor="n")
                cv.create_text(lx, ly + ring + gap + line, text=sub, fill=tone,
                               font=F(FONT_CANVAS), anchor="n")
        cv.create_text(cx, h - 8 * s, anchor="s", fill=muted, font=F(FONT_CANVAS),
                       text="Ring = the level each light reports")

    # -- actions -----------------------------------------------------------------

    def _open_web(self) -> None:
        client = self.app.lights_client()
        webbrowser.open(client.base + "/")

    def _open_folder(self) -> None:
        folder = lights_folder(self.app.flight_dir)
        if folder is None:
            messagebox.showinfo(self.app.title(), "No flight folder chosen — the lights "
                                "are shown live and nothing is copied. Choose one on "
                                "Monitoring.")
            return
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)                                 # noqa: S606
        except Exception:
            pass

    # -- wiring ----------------------------------------------------------------

    def refresh(self) -> None:
        """Called when the tab is shown and when a flight folder is chosen."""
        try:
            self.app.lights_client()
        except Exception:
            diagnostics.log_exception("starting the lights client", *sys.exc_info())
        if not self._ticking:
            self._ticking = True
            self._tick()

    def refresh_theme(self) -> None:
        self.schematic.configure(background=self._apply_appearance_mode(T.BG))
        self._draw_schematic()

    def _tick(self) -> None:
        try:
            if self.schematic.winfo_ismapped():
                self._update()
        except Exception:
            diagnostics.log_exception("Lights page", *sys.exc_info(), level=logging.WARNING)
        self.after(TICK_MS, self._tick)

    def _update(self) -> None:
        client = self.app.lights_client()
        state, st = client.snapshot()
        self._state = state
        if state is None:
            _set(self.conn_label, text=f"○ Light Monitor at {st.url}: "
                                       f"{st.error or 'not read yet'}", text_color=T.WARN
                 if st.failures > 2 else T.TEXT)
        else:
            link = state.get("link", {})
            src = {"data": "telemetry port", "console": "console, read-only",
                   "simulator": "SIMULATED controller", "released": "port released",
                   "none": "controller not found"}.get(link.get("source"), link.get("source"))
            ext = state.get("extension", {})
            fresh = st.connected
            _set(self.conn_label,
                 text=(f"{'●' if fresh else '○'} Light Monitor {ext.get('version', '')} at "
                       f"{st.url} · controller: {src}"
                       + (f" · {link.get('note')}" if link.get("note") else "")),
                 text_color=T.OK if fresh and link.get("connected") else T.WARN)
        rec = (state or {}).get("recording", {})
        copy = (f"Topside copy: {st.copy_path} ({st.copied:,} records"
                + (f", {st.gaps} gap(s)" if st.gaps else "") + ")"
                if st.copy_path else "No flight folder chosen: shown live, nothing copied."
                if self.app.flight_dir is None else "Topside copy starts with the first record.")
        pi = (f"On the Pi: {rec.get('file')} in {rec.get('dir')}"
              + (f" — {rec.get('paused')}" if rec.get("paused") else "")) if rec.get("file") else ""
        _set(self.copy_label, text=copy + ("\n" + pi if pi else ""))
        if state is None:
            return
        for lt in state.get("lights", []):
            card = self.cards.get(lt.get("id"))
            if card is not None:
                card.show(lt)
        self._draw_schematic()
        self._update_power(state)
        self._update_controller(state)
        self._update_events(state)

    def _update_power(self, s: dict) -> None:
        p = s.get("power", {})
        veh = p.get("vehicle") or {}
        warn_v = p.get("vehicle_warn_v") or 10.5
        v = veh.get("v")
        P = self.power
        _set(P["veh_v"], text=fmt(v, 2, "V"),
             text_color=T.WARN if isinstance(v, (int, float)) and v < warn_v else T.TEXT)
        _set(P["veh_a"], text=fmt(veh.get("a"), 1, "A"))
        _set(P["veh_w"], text=fmt(veh.get("w"), 0, "W"))
        rail = p.get("rail")
        _set(P["rail"], text=(f"{fmt(rail.get('v'), 2, 'V')} · {fmt(rail.get('a'), 2, 'A')} · "
                              f"{fmt(rail.get('w'), 0, 'W')}" if rail else
                              "no power monitor fitted"),
             text_color=T.WARN if rail and rail.get("uv") else T.TEXT if rail else T.TEXT_MUTED)
        _set(P["exp_w"], text=f"≈ {fmt(p.get('lights_expected_w'), 0, 'W')}")
        _set(P["exp_a"], text=f"≈ {fmt(p.get('lights_expected_a'), 1, 'A')}")
        ard = p.get("ardusub") or {}
        _set(P["pilot"], text=f"{fmt(ard.get('lights_pct'), 0, '%')} · "
                              f"{fmt(ard.get('pilot_gain_pct'), 0, '%')}")

    def _update_controller(self, s: dict) -> None:
        c, link, mav = s.get("controller", {}), s.get("link", {}), s.get("mavlink", {})
        st = c.get("stats") or {}
        C = self.ctl
        _set(C["port"], text={"data": "telemetry port", "console": "console, read-only",
                              "simulator": "simulated", "released": "released",
                              "none": "not found"}.get(link.get("source"), "—"))
        _set(C["board"], text=c.get("machine") or c.get("board") or
             ("debug firmware" if link.get("legacy_firmware") else "—"))
        _set(C["fw"], text=c.get("fw") or "—")
        _set(C["mpy"], text=c.get("micropython") or "—")
        up = c.get("uptime_s")
        _set(C["up"], text="—" if up is None else f"{up / 60:.1f} min")
        sig = c.get("signal")
        _set(C["pwm"], text=(f"{c.get('pwm_us')} µs" if c.get("pwm_us") else "—")
             + (" · no signal" if sig is False else ""),
             text_color=T.WARN if sig is False else T.TEXT)
        _set(C["cmd"], text=fmt(c.get("commanded_pct"), 0, "%"))
        _set(C["lines"], text=f"{fmt(link.get('lines_per_s'), 1)}/s · "
                              f"{link.get('lines', 0):,} total")
        lost = link.get("lost_lines") or 0
        _set(C["lost"], text=f"{lost:,}", text_color=T.WARN if lost else T.TEXT)
        _set(C["bus"], text=(f"{st.get('answered')}/{st.get('asked')} answered · "
                             f"{st.get('failed')} failed" if st.get("asked") else "—"))
        restarts = c.get("restarts") or 0
        _set(C["restarts"], text=str(restarts), text_color=T.WARN if restarts else T.TEXT)
        _set(C["mav"], text=("off" if not mav.get("enabled") else
                             f"{'publishing' if mav.get('publishing') else 'waiting'} as "
                             f"{mav.get('system')}/{mav.get('component')} · "
                             f"{mav.get('sent', 0):,} sent · {mav.get('errors', 0)} errors"))

    def _update_events(self, s: dict) -> None:
        ev = s.get("events") or []
        key = (len(ev), ev[-1]["t"] if ev else None)
        if key == self._events_shown:
            return
        self._events_shown = key
        lines = [time.strftime("%H:%M:%S", time.localtime(e["t"]))
                 + ("  ⚠ " if e.get("lvl") == "warn" else "    ") + e.get("text", "")
                 for e in reversed(ev[-60:])]
        say(self.events_box, "\n".join(lines) or "Nothing yet.")


# ==========================================================================
#  the Navigation row
# ==========================================================================


class LightsStrip(ctk.CTkFrame):
    """One row under the map: each light's level and expected draw, and the
    vehicle's supply. Two short lines per cell, so it costs the map little."""

    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.app = app
        self.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform="lstrip")
        self.cells: dict[str, tuple[ctk.CTkLabel, ctk.CTkLabel]] = {}
        #: The cells' frames, kept so they can be put back: grid_slaves() only
        #: lists what is currently gridded, not what was removed.
        self._frames: list[ctk.CTkFrame] = []
        order = [(1, "1 · Front left"), (4, "4 · Front right"), (2, "2 · Rear left"),
                 (3, "3 · Rear right")]
        for col, (lid, title) in enumerate(order):
            self.cells[str(lid)] = self._cell(col, title)
        self.cells["power"] = self._cell(4, "Vehicle supply")
        self.message = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                    text_color=T.TEXT_MUTED, anchor="w")

    def _cell(self, col: int, title: str) -> tuple[ctk.CTkLabel, ctk.CTkLabel]:
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=0, column=col, sticky="ew", padx=8, pady=5)
        f.grid_columnconfigure(0, weight=1)
        self._frames.append(f)
        t = ctk.CTkLabel(f, text=title, font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                         anchor="w", height=16)
        t.grid(row=0, column=0, sticky="ew")
        v = ctk.CTkLabel(f, text="—", font=T.FONT_BODY, text_color=T.TEXT, anchor="w",
                         height=18)
        v.grid(row=1, column=0, sticky="ew")
        return t, v

    def update_from(self, state: dict | None, status) -> None:
        if state is None:
            if not self.message.winfo_manager():
                for f in self._frames:
                    f.grid_remove()
                self.message.grid(row=0, column=0, columnspan=5, sticky="ew", padx=10, pady=5)
            _set(self.message, text=f"Lights — Light Monitor not answering at "
                                    f"{status.url}" + (f" ({status.error})"
                                                       if status.error else ""))
            return
        if self.message.winfo_manager():
            self.message.grid_remove()
            for f in self._frames:
                f.grid()
        for lt in state.get("lights", []):
            cell = self.cells.get(str(lt.get("id")))
            if cell is None:
                continue
            k = kind_of(lt.get("state"))
            value, note = light_line(lt)
            dot = "● " if k in ("on", "bad") else "○ "
            _set(cell[0], text=dot + f"{lt['id']} · {lt.get('name', '')}",
                 text_color=T.WARN if k == "bad" else T.TEXT_MUTED)
            _set(cell[1], text=value + (f"  {note}" if note else ""),
                 text_color=T.WARN if k == "bad" else T.TEXT)
        p = state.get("power", {})
        veh = p.get("vehicle") or {}
        v = veh.get("v")
        warn = isinstance(v, (int, float)) and v < (p.get("vehicle_warn_v") or 10.5)
        _set(self.cells["power"][0],
             text="Vehicle supply" + (f" · lights ≈{fmt(p.get('lights_expected_w'), 0, 'W')}"
                                      if p.get("lights_expected_w") else ""))
        _set(self.cells["power"][1],
             text=(f"{fmt(v, 1, 'V')} · {fmt(veh.get('a'), 0, 'A')} · {fmt(veh.get('w'), 0, 'W')}"
                   if v is not None else "not read"),
             text_color=T.WARN if warn else T.TEXT)

