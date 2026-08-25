"""Reusable GUI pieces: section cards, and the site / transect editors."""

from __future__ import annotations

from datetime import date as _date
from typing import Callable

import customtkinter as ctk

from ..survey import Site, SurveyError, Transect, parse_hhmmss
from . import theme as T


class Card(ctk.CTkFrame):
    """A titled section panel."""

    def __init__(self, master, title: str, subtitle: str = "", **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=T.PAD, pady=(T.PAD, 4))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text=title, font=T.FONT_H1, text_color=T.HEADING
                     ).grid(row=0, column=0, sticky="w")
        if subtitle:
            ctk.CTkLabel(head, text=subtitle, font=T.FONT_SMALL,
                         text_color=T.TEXT_MUTED, justify="left", anchor="w"
                         ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=T.PAD, pady=(4, T.PAD))
        self.body.grid_columnconfigure(0, weight=1)


def entry(master, placeholder: str = "", width: int = 140, **kw) -> ctk.CTkEntry:
    return ctk.CTkEntry(
        master, placeholder_text=placeholder, width=width,
        font=T.FONT_BODY, text_color=T.TEXT, fg_color=T.FIELD_BG,
        border_color=T.FIELD_BORDER, border_width=1, corner_radius=6, **kw
    )


def label(master, text: str, muted: bool = False, font=None) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=font or T.FONT_BODY,
                        text_color=T.TEXT_MUTED if muted else T.TEXT, anchor="w")


def button(master, text, command, kind: str = "primary", width: int = 120):
    if kind == "primary":
        return ctk.CTkButton(master, text=text, command=command, width=width,
                             font=T.FONT_H2, fg_color=T.ACCENT,
                             hover_color=T.ACCENT_HOVER, text_color=T.ACCENT_TEXT,
                             corner_radius=6)
    if kind == "danger":
        return ctk.CTkButton(master, text=text, command=command, width=width,
                             font=T.FONT_BODY, fg_color="transparent",
                             hover_color=T.SURFACE_ALT, text_color=T.WARN,
                             border_width=1, border_color=T.BORDER, corner_radius=6)
    return ctk.CTkButton(master, text=text, command=command, width=width,
                         font=T.FONT_BODY, fg_color="transparent",
                         hover_color=T.SURFACE_ALT, text_color=T.TEXT,
                         border_width=1, border_color=T.BORDER, corner_radius=6)


class TransectRow(ctk.CTkFrame):
    """One transect: name, TC-25 start, TC-25 end."""

    def __init__(self, master, on_remove: Callable[["TransectRow"], None],
                 name: str = "T1", start: str = "", end: str = ""):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(5, weight=1)
        self._on_remove = on_remove

        self.name = entry(self, "T1", width=70)
        self.name.insert(0, name)
        self.name.grid(row=0, column=0, padx=(0, 8), pady=3)

        label(self, "start", muted=True).grid(row=0, column=1, padx=(0, 4))
        self.start = entry(self, "hh:mm:ss", width=110)
        self.start.insert(0, start)
        self.start.grid(row=0, column=2, padx=(0, 10))

        label(self, "end", muted=True).grid(row=0, column=3, padx=(0, 4))
        self.end = entry(self, "hh:mm:ss", width=110)
        self.end.insert(0, end)
        self.end.grid(row=0, column=4, padx=(0, 10))

        self.status = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=0, column=5, sticky="ew", padx=(4, 8))

        button(self, "Remove", lambda: on_remove(self), "danger", width=80
               ).grid(row=0, column=6)

        for e in (self.start, self.end):
            e.bind("<FocusOut>", lambda _e: self.refresh())
            e.bind("<KeyRelease>", lambda _e: self.refresh())

    def to_transect(self) -> Transect:
        return Transect(self.name.get().strip() or "T?",
                        self.start.get().strip(), self.end.get().strip())

    def refresh(self) -> None:
        t = self.to_transect()
        if not t.start_tc and not t.end_tc:
            self.status.configure(text="", text_color=T.TEXT_MUTED)
            return
        errs = t.validate()
        if errs:
            self.status.configure(text=errs[0].split(": ", 1)[-1], text_color=T.WARN)
            return
        mins = t.duration_s() / 60.0
        self.status.configure(text=f"{mins:.1f} min", text_color=T.OK)


class SiteFrame(ctk.CTkFrame):
    """A survey site and its transects."""

    def __init__(self, master, on_remove: Callable[["SiteFrame"], None],
                 index: int = 1, site: Site | None = None,
                 default_project: str = "", default_date: str = ""):
        super().__init__(master, fg_color=T.SURFACE_ALT, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER)
        self.grid_columnconfigure(0, weight=1)
        self._on_remove = on_remove
        self._rows: list[TransectRow] = []

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        head.grid_columnconfigure(6, weight=1)

        ctk.CTkLabel(head, text=f"Site {index}", font=T.FONT_H2,
                     text_color=T.HEADING).grid(row=0, column=0, padx=(0, 12))

        label(head, "name").grid(row=0, column=1, padx=(0, 4))
        self.name = entry(head, "e.g. Centennial", width=160)
        self.name.grid(row=0, column=2, padx=(0, 12))

        label(head, "project").grid(row=0, column=3, padx=(0, 4))
        self.project = entry(head, "e.g. HSIL", width=140)
        self.project.grid(row=0, column=4, padx=(0, 12))

        label(head, "date").grid(row=0, column=5, padx=(0, 4))
        self.date = entry(head, "YYYY-MM-DD", width=120)
        self.date.grid(row=0, column=6, sticky="w")

        button(head, "Remove site", lambda: on_remove(self), "danger", width=100
               ).grid(row=0, column=7, sticky="e")

        self.rows_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.rows_frame.grid(row=1, column=0, sticky="ew", padx=10)
        self.rows_frame.grid_columnconfigure(0, weight=1)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
        button(foot, "+ Add transect", self.add_transect, "ghost", width=130
               ).grid(row=0, column=0, sticky="w")

        if site:
            self.name.insert(0, site.name)
            self.project.insert(0, site.project)
            self.date.insert(0, site.date)
            for t in site.transects:
                self.add_transect(t.name, t.start_tc, t.end_tc)
        else:
            self.project.insert(0, default_project)
            self.date.insert(0, default_date or _date.today().isoformat())
            self.add_transect()

    # ---- transects -----------------------------------------------------

    def add_transect(self, name: str | None = None, start: str = "",
                     end: str = "") -> None:
        n = name or f"T{len(self._rows) + 1}"
        row = TransectRow(self.rows_frame, self._remove_row, n, start, end)
        row.grid(row=len(self._rows), column=0, sticky="ew", pady=1)
        self._rows.append(row)
        row.refresh()

    def _remove_row(self, row: TransectRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
            row.destroy()
            for i, r in enumerate(self._rows):
                r.grid(row=i, column=0, sticky="ew", pady=1)

    def to_site(self) -> Site:
        return Site(
            name=self.name.get().strip(),
            project=self.project.get().strip(),
            date=self.date.get().strip(),
            transects=[r.to_transect() for r in self._rows],
        )
