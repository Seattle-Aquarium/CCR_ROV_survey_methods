"""
System at a glance: one line per component, in two columns, and the power.

Painted from a `board.Board` once a second while the Monitoring tab is on
screen. Deliberately quiet: the same type size as the rest of the tab, a
coloured mark per row, and one summary line -- the Live monitoring charts
under it are what the tab is for, so this stays a few lines tall.

Labels are only reconfigured when their text or colour changes:
CustomTkinter reads every option back on `configure`, and some forty labels,
once a second, all day, is not free. A detail too long for its column is cut
with an ellipsis rather than wrapped, so a row is always one line.
"""

from __future__ import annotations

import tkinter.font as tkfont

import customtkinter as ctk

from .. import board as B
from . import theme as T
from .widgets import Card

FONT_ROW = T.FONT_BODY
FONT_DETAIL = T.FONT_SMALL
FONT_MARK = ("Segoe UI Symbol", 12)
#: Rows per column: the board is two columns of this many.
COLUMNS = 2


def _set(widget, **kw) -> None:
    cache = getattr(widget, "_board_cache", None)
    if cache is None:
        cache = widget._board_cache = {}
    changes = {k: v for k, v in kw.items() if cache.get(k) != v}
    if changes:
        cache.update(changes)
        widget.configure(**changes)


#: The summary line's words, by verdict.
SUMMARY = {None: "Nothing connected yet", B.GO: "Everything working",
           B.CHECK: "Check", B.NOGO: "Not working"}


class GoBoard(Card):
    def __init__(self, master, title: str = "1.  System at a glance"):
        super().__init__(master, title)
        body = self.body
        body.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        top.grid_columnconfigure(1, weight=1)
        self.verdict_mark = ctk.CTkLabel(top, text="○", font=FONT_MARK, width=18,
                                         text_color=T.TEXT_MUTED, height=20)
        self.verdict_mark.grid(row=0, column=0, sticky="w")
        self.headline = ctk.CTkLabel(top, text="", font=FONT_ROW, text_color=T.TEXT,
                                     anchor="w", height=20)
        self.headline.grid(row=0, column=1, sticky="ew", padx=(4, 8))
        self.power_label = ctk.CTkLabel(top, text="", font=FONT_ROW,
                                        text_color=T.TEXT, anchor="e", height=20)
        self.power_label.grid(row=0, column=2, sticky="e")

        self.rows_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.rows_frame.grid(row=1, column=0, sticky="ew")
        for c in range(COLUMNS):
            # mark, name, detail -- and a gap before the next column.
            self.rows_frame.grid_columnconfigure(c * 3 + 2, weight=1, uniform="detail")
        self._rows: dict[str, tuple] = {}
        self._per_column = 7
        self._fonts: dict = {}
        self._detail_px = 0
        self.rows_frame.bind("<Configure>", self._measure, add="+")

    # -- layout ------------------------------------------------------------

    def _row(self, key: str, index: int):
        got = self._rows.get(key)
        if got is not None:
            return got
        f = self.rows_frame
        col, r = divmod(index, self._per_column)
        base = col * 3
        mark = ctk.CTkLabel(f, text="○", font=FONT_MARK, width=16,
                            text_color=T.TEXT_MUTED, height=20)
        mark.grid(row=r, column=base, sticky="w", padx=(16 if col else 0, 0))
        name = ctk.CTkLabel(f, text="", font=FONT_ROW, text_color=T.TEXT,
                            anchor="w", height=20)
        name.grid(row=r, column=base + 1, sticky="w", padx=(4, 10))
        detail = ctk.CTkLabel(f, text="", font=FONT_DETAIL, text_color=T.TEXT_MUTED,
                              anchor="w", height=20)
        detail.grid(row=r, column=base + 2, sticky="ew")
        got = (mark, name, detail)
        self._rows[key] = got
        return got

    def _measure(self, _event=None) -> None:
        """How wide a detail may be before it is cut, in screen pixels."""
        rows = list(self._rows.values())
        if not rows:
            return
        try:
            self._detail_px = max(40, rows[0][2].winfo_width() - 4)
        except Exception:
            self._detail_px = 0

    def _font(self, spec) -> tkfont.Font:
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:
            scaling = 1.0
        key = (spec, scaling)
        f = self._fonts.get(key)
        if f is None:
            # As CustomTkinter sizes its own: the size in pixels, scaled.
            f = self._fonts[key] = tkfont.Font(family=spec[0],
                                               size=-int(round(spec[1] * scaling)))
        return f

    def _fit(self, text: str) -> str:
        room = self._detail_px
        if not text or room <= 0:
            return text
        font = self._font(FONT_DETAIL)
        if font.measure(text) <= room:
            return text
        while text and font.measure(text + "…") > room:
            text = text[:-1]
        return text.rstrip(" ·,") + "…"

    # -- painting ------------------------------------------------------------

    def update_from(self, board: B.Board) -> None:
        self._per_column = max(1, -(-len(board.rows) // COLUMNS))
        color = B.COLORS.get(board.verdict, T.TEXT_MUTED)
        _set(self.verdict_mark, text=B.GLYPHS[board.verdict], text_color=color)
        word, _sep, rest = board.headline.partition(" — ")
        problems = [r.name for r in board.rows
                    if board.verdict in (B.CHECK, B.NOGO) and r.level == board.verdict]
        if board.verdict in (B.CHECK, B.NOGO):
            text = f"{SUMMARY[board.verdict]}: " + ", ".join(problems)
        else:
            text = SUMMARY.get(board.verdict, rest)
        _set(self.headline, text=text)
        bits = []
        for value, unit, fmt in ((board.volts, "V", "{:.1f}"),
                                 (board.amps, "A", "{:.1f}"),
                                 (board.watts, "W", "{:.0f}")):
            bits.append(("—" if value is None else fmt.format(value)) + f" {unit}")
        _set(self.power_label, text="   ".join(bits))
        for i, row in enumerate(board.rows):
            mark, name, detail = self._row(row.key, i)
            rcolor = B.COLORS.get(row.level, T.TEXT_MUTED)
            _set(mark, text=B.GLYPHS[row.level], text_color=rcolor)
            _set(name, text=row.name)
            chips = "  " + " ".join(f"{label}{B.GLYPHS[lv]}" for label, lv in row.chips) \
                if row.chips else ""
            _set(detail, text=self._fit(row.detail + chips),
                 text_color=T.TEXT if row.level in (B.CHECK, B.NOGO) else T.TEXT_MUTED)
        if not self._detail_px:
            self.after_idle(self._measure)

    def fit(self, width_px: float) -> None:
        """Kept for callers; the rows fit themselves on <Configure>."""
        self._measure()
