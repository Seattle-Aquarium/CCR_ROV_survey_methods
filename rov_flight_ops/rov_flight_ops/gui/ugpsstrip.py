"""
The UGPS receivers row under the Navigation map.

One cell per receiver of the G2 antenna: its signal-to-noise now, its noise
floor, and a minute of signal-to-noise as a sparkline, all drawn on the same
0-60 dB scale so a receiver that sits low is visible at a glance. A cell at the
left says, in words, whether any receiver stands out from the other three
(`ugps.health`). Read from the application's UGPS capture, which talks to the
G2 topside directly, so the row keeps working when the Pi drops off the tether.
"""

from __future__ import annotations

import time
import tkinter as tk

import customtkinter as ctk

from ..ugps import health as H
from . import theme as T
from .widgets import Label, fit_wrap


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

#: The sparklines' scale. Healthy receivers read roughly 30-45 dB.
SNR_TOP_DB = 60.0
SPARK_H = 24


class ReceiversStrip(ctk.CTkFrame):
    """Each UGPS receiver's signal-to-noise, side by side."""

    def __init__(self, master, app, n: int = 4, *, compact: bool = False, **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.app = app
        #: `compact` puts the verdict on a line of its own above the four
        #: receivers, for a column too narrow to hold all five side by side.
        self.compact = compact
        self._span = n if compact else n + 1
        if compact:
            self.grid_columnconfigure(tuple(range(n)), weight=1, uniform="ustrip")
        else:
            self.grid_columnconfigure(0, weight=5, uniform="ustrip")
            self.grid_columnconfigure(tuple(range(1, n + 1)), weight=4,
                                      uniform="ustrip")
        self._frames: list[ctk.CTkFrame] = []

        f = ctk.CTkFrame(self, fg_color="transparent")
        if compact:
            f.grid(row=0, column=0, columnspan=n, sticky="nsew", padx=8, pady=(4, 0))
        else:
            f.grid(row=0, column=0, sticky="nsew", padx=8, pady=5)
        f.grid_columnconfigure(0, weight=1)
        self._frames.append(f)
        self.title = Label(f, text="UGPS receivers", font=T.FONT_SMALL,
                                  text_color=T.TEXT_MUTED, anchor="w", height=16)
        self.title.grid(row=0, column=0, sticky="ew")
        self.verdict = Label(f, text="—", font=T.FONT_SMALL if compact else T.FONT_BODY,
                             text_color=T.TEXT, anchor="w", justify="left",
                             wraplength=260)
        if compact:
            # Title and verdict on one line: "UGPS receivers  ·  all four agree".
            f.grid_columnconfigure(0, weight=0)
            f.grid_columnconfigure(1, weight=1)
            self.verdict.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        else:
            self.verdict.grid(row=1, column=0, sticky="ew")
        # Wrapped to its own column, which on the field laptop is narrower
        # than the fixed 260 it had, so the end of the sentence was cut off.
        fit_wrap(f, self.verdict, margin=4)

        self.cells = []
        for i in range(n):
            c = ctk.CTkFrame(self, fg_color="transparent")
            if compact:
                c.grid(row=1, column=i, sticky="nsew", padx=(8 if i == 0 else 4, 4),
                       pady=(0, 4))
            else:
                c.grid(row=0, column=i + 1, sticky="nsew", padx=6, pady=5)
            c.grid_columnconfigure(0, weight=1)
            self._frames.append(c)
            head = Label(c, text=f"Rx {i}", font=T.FONT_SMALL,
                                text_color=T.TEXT_MUTED, anchor="w", height=16)
            head.grid(row=0, column=0, sticky="ew")
            value = Label(c, text="—", font=T.FONT_SMALL if compact else T.FONT_BODY,
                          text_color=T.TEXT, anchor="w", height=18)
            value.grid(row=1, column=0, sticky="ew")
            # A plain Tk canvas is not scaled with the rest of the window, so
            # its height is scaled here to match the labels around it.
            spark = tk.Canvas(c, height=round(SPARK_H * self._get_widget_scaling()),
                              highlightthickness=0, borderwidth=0,
                              background=self._apply_appearance_mode(T.SURFACE))
            spark.grid(row=2, column=0, sticky="ew", pady=(2, 0))
            self.cells.append((head, value, spark))
        self.message = Label(self, text="", font=T.FONT_SMALL,
                                    text_color=T.TEXT_MUTED, anchor="w")

    # ------------------------------------------------------------------

    def update_from(self, snap: dict | None) -> None:
        if snap is None or (not snap.get("connected") and not snap.get("history")):
            self._show_message(
                "UGPS receivers — the G2 is not answering"
                + (f" at {snap['host']}" if snap else "")
                + (f" ({snap['error']})" if snap and snap.get("error") else ""))
            return
        self._hide_message()
        judged = H.judge(snap.get("history") or [], time.monotonic())
        stale = "" if snap.get("connected") else "  (G2 not answering; last minute shown)"
        _set(self.title, text="UGPS receivers" + stale)
        _set(self.verdict, text=judged.verdict[:160],
             text_color=T.WARN if judged.flagged else T.TEXT)
        # Not strict: an antenna answering with other than four receivers
        # fills the cells it has.
        for rh, (head, value, spark) in zip(judged.receivers, self.cells, strict=False):
            bad = bool(rh.flags)
            low_valid = (rh.valid_fraction is not None and rh.valid_fraction < 0.95)
            tail = (" · " + _tag(rh.flags[0]) if bad else
                    f" · {rh.valid_fraction:.0%} valid" if self.compact and low_valid
                    else "")
            _set(head, text=f"Rx {rh.index}" + tail,
                 text_color=T.WARN if bad else T.TEXT_MUTED)
            if rh.snr_db is None and rh.nsd_db is None:
                _set(value, text="no solution", text_color=T.TEXT_MUTED)
            else:
                unit = "" if self.compact else " dB"
                snr = (f"SNR {rh.snr_db:.0f}{unit}" if rh.snr_db is not None
                       else "SNR —")
                word = "nf" if self.compact else "noise"
                nsd = f" · {word} {rh.nsd_db:.0f}" if rh.nsd_db is not None else ""
                # Compact: the valid share is in the cell's heading instead.
                valid = (f" · {rh.valid_fraction:.0%} valid"
                         if low_valid and not self.compact else "")
                _set(value, text=snr + nsd + valid, text_color=T.WARN if bad else T.TEXT)
            self._sparkline(spark, rh.trace[-120:], bad)

    def _sparkline(self, cv: tk.Canvas, trace: list[float], bad: bool) -> None:
        m = self._apply_appearance_mode
        cv.configure(background=m(T.SURFACE))
        cv.delete("all")
        w = max(cv.winfo_width(), 20)
        h = max(cv.winfo_height(), SPARK_H)
        # A faint line at 30 dB, roughly where a healthy receiver starts.
        y30 = h - 2 - (30 / SNR_TOP_DB) * (h - 4)
        cv.create_line(0, y30, w, y30, fill=m(T.BORDER), dash=(2, 3))
        if len(trace) < 2:
            return
        n = len(trace)
        pts = []
        for k, v in enumerate(trace):
            v = min(max(v, 0.0), SNR_TOP_DB)
            pts += [k * (w - 1) / (n - 1), h - 2 - (v / SNR_TOP_DB) * (h - 4)]
        cv.create_line(*pts, fill=m(T.WARN if bad else T.ACCENT), width=2)

    def _show_message(self, text: str) -> None:
        if not self.message.winfo_manager():
            for f in self._frames:
                f.grid_remove()
            self.message.grid(row=0, column=0, columnspan=self._span,
                              sticky="ew", padx=10, pady=5)
        _set(self.message, text=text)

    def _hide_message(self) -> None:
        if self.message.winfo_manager():
            self.message.grid_remove()
            for f in self._frames:
                f.grid()


def _tag(flag: str) -> str:
    """A flag from `ugps.health` as one or two words for a cell heading; the
    whole sentence is in the verdict beside it."""
    if flag.startswith("SNR"):
        return "low SNR"
    if flag.startswith("noise"):
        return "noisy"
    if flag.startswith("valid"):
        return "dropping out"
    return flag
