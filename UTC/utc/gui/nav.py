"""
A vertical page rail.

CustomTkinter's tab view only puts its buttons in a horizontal strip, so a
left-hand rail is built here from a fixed-width column of buttons plus a
content area that raises one page at a time.

The rail also scales better than tabs did: pages are stacked vertically, so
adding a fifth or sixth costs no horizontal room and never truncates a label.

Accessibility note. The selected and unselected states differ by *fill*, not by
type colour, because one text colour has to read on both. An accent fill would
need dark type, which then sits near 1.1:1 on the unselected rows -- the same
trap the horizontal tabs fell into. Body text on surface-vs-ground clears
6.7:1 in both themes, and an accent stripe marks the selection instead.
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from . import theme as T

RAIL_WIDTH = 190
STRIPE_WIDTH = 4


class Navigator(ctk.CTkFrame):
    """A left rail of page names beside a single-page content area."""

    def __init__(self, master, on_select: Callable[[str], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self._on_select = on_select
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._rows: dict[str, tuple[ctk.CTkFrame, ctk.CTkFrame, ctk.CTkButton]] = {}
        self._current: str | None = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.rail = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0,
                                 width=RAIL_WIDTH)
        self.rail.grid(row=0, column=0, sticky="nsw")
        self.rail.grid_propagate(False)
        self.rail.grid_columnconfigure(0, weight=1)
        # Park all the slack in one row below the last entry. Without this the
        # rail shares its leftover height out among the rows, which pulls each
        # label away from its own subtitle and pushes the last page off-screen.
        self._SPACER_ROW = 900
        ctk.CTkFrame(self.rail, fg_color="transparent", height=1).grid(
            row=self._SPACER_ROW, column=0, sticky="nsew")
        self.rail.grid_rowconfigure(self._SPACER_ROW, weight=1)

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    # ------------------------------------------------------------------

    def add(self, name: str, subtitle: str = "") -> ctk.CTkFrame:
        """Register a page and return the frame to build it into."""
        row = len(self._rows)

        holder = ctk.CTkFrame(self.rail, fg_color="transparent",
                              corner_radius=0)
        holder.grid(row=row, column=0, sticky="ew", pady=(6 if row == 0 else 1, 1))
        holder.grid_columnconfigure(1, weight=1)

        # height=1 matters: a CTkFrame defaults to 200px, and with propagation
        # off it keeps that -- which silently made every rail row 200px tall
        # (351 once display scaling was applied) and pushed the last page off
        # the bottom. The stripe should take its height from the row, not set it.
        stripe = ctk.CTkFrame(holder, fg_color="transparent",
                              width=STRIPE_WIDTH, height=1, corner_radius=0)
        stripe.grid(row=0, column=0, rowspan=2, sticky="ns")
        stripe.grid_propagate(False)

        btn = ctk.CTkButton(
            holder, text=name, command=lambda n=name: self.select(n),
            font=T.FONT_BODY, anchor="w", height=40, corner_radius=0,
            fg_color="transparent", hover_color=T.SURFACE_ALT,
            text_color=T.TEXT,
        )
        btn.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        if subtitle:
            ctk.CTkLabel(holder, text=subtitle, font=T.FONT_SMALL,
                         text_color=T.TEXT_MUTED, anchor="w", justify="left"
                         ).grid(row=1, column=1, sticky="w", padx=(8, 8),
                                pady=(0, 6))

        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        page.lower()

        self._rows[name] = (holder, stripe, btn)
        self._pages[name] = page
        if self._current is None:
            self.select(name)
        return page

    def select(self, name: str) -> None:
        if name not in self._pages:
            return
        for n, (holder, stripe, btn) in self._rows.items():
            on = n == name
            stripe.configure(fg_color=T.ACCENT if on else "transparent")
            holder.configure(fg_color=T.SURFACE_ALT if on else "transparent")
            btn.configure(fg_color=T.SURFACE_ALT if on else "transparent",
                          font=T.FONT_H2 if on else T.FONT_BODY)
        self._pages[name].lift()
        self._current = name
        if self._on_select:
            self._on_select(name)

    @property
    def current(self) -> str | None:
        return self._current

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Grey a page out. Used to keep the flow honest -- importing imagery
        makes no sense before a flight folder and transects exist."""
        row = self._rows.get(name)
        if row:
            row[2].configure(state="normal" if enabled else "disabled")
