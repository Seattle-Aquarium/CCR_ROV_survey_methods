"""Reusable GUI pieces: section cards, and the site / transect editors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as _date

import customtkinter as ctk

from ..survey import Site, Transect
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
        self._subtitle = None
        if subtitle:
            self._subtitle = ctk.CTkLabel(head, text=subtitle, font=T.FONT_SMALL,
                                          text_color=T.TEXT_MUTED,
                                          justify="left", anchor="w")
            self._subtitle.grid(row=1, column=0, columnspan=2, sticky="ew",
                                pady=(2, 0))
            # A label will not wrap unless it is given a width, and the card's
            # width is not known until it has been laid out. Without this a long
            # subtitle runs off the right edge of the window instead of flowing
            # onto a second line -- and the end of the sentence is simply lost.
            self.bind("<Configure>", self._fit_subtitle, add="+")

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=T.PAD, pady=(4, T.PAD))
        self.body.grid_columnconfigure(0, weight=1)

    def _fit_subtitle(self, event) -> None:
        """Keep the subtitle wrapped to the card's current width.

        The width has to be divided by the display scaling before it is handed
        over: CustomTkinter multiplies wraplength by the same factor on its way
        to the underlying label. On a 150% display, passing the measured pixel
        width asks for a wrap point half again wider than the card, so the
        longest subtitles never wrapped at all.
        """
        if self._subtitle is None:
            return
        # The head frame is inset by T.PAD on each side, and a few pixels
        # more go to the card's own border and rounding. Erring narrow
        # costs nothing; erring wide clips the last word of the line.
        width = event.width - 2 * T.PAD - 16
        if width < 120:
            return
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            scaling = 1.0
        target = int(width / (scaling or 1.0))

        # Re-wrapping changes the label's height, which fires <Configure> again;
        # ignoring changes of a few pixels stops that becoming a loop.
        try:
            current = int(self._subtitle.cget("wraplength"))
        except (TypeError, ValueError):
            current = 0
        if abs(current - target) > 8:
            self._subtitle.configure(wraplength=target)


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

    def __init__(self, master, on_remove: Callable[[TransectRow], None],
                 name: str = "T1", start: str = "", end: str = ""):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(5, weight=1)
        self._on_remove = on_remove

        self.name = entry(self, "T1", width=70)
        self.name.insert(0, name)
        self.name.grid(row=0, column=0, padx=(0, 8), pady=3)

        label(self, "start", muted=True).grid(row=0, column=1, padx=(0, 4))
        self.start = TimeEntry(self, width=110)
        self.start.set(start)
        self.start.grid(row=0, column=2, padx=(0, 10))

        label(self, "end", muted=True).grid(row=0, column=3, padx=(0, 4))
        self.end = TimeEntry(self, width=110)
        self.end.set(end)
        self.end.grid(row=0, column=4, padx=(0, 10))

        self.status = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=0, column=5, sticky="ew", padx=(4, 8))

        button(self, "Remove", lambda: on_remove(self), "danger", width=80
               ).grid(row=0, column=6)

        # Wire the callbacks only now: every widget refresh() touches exists.
        for e in (self.start, self.end):
            e.set_on_change(self.refresh)

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

    def __init__(self, master, on_remove: Callable[[SiteFrame], None],
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


class TimeEntry(ctk.CTkEntry):
    """hh:mm:ss typed as six digits, with the colons written for you.

    A transect is four numbers a day, typed in the field on a laptop lid, and
    reaching for ':' twice per time is most of the effort. Every component is
    zero-padded to two digits, so six keystrokes is always the whole time and
    the separators can be inserted as you go.

    Paste and editing still work: the text is re-derived from whatever digits
    the box ends up containing, rather than from keystrokes, so a pasted
    "12:25:45" or a mid-string correction both settle on the same result.
    """

    def __init__(self, master, width: int = 110, on_change=None, **kw):
        self._var = ctk.StringVar()
        super().__init__(
            master, textvariable=self._var, placeholder_text="hh:mm:ss",
            width=width, font=T.FONT_BODY, text_color=T.TEXT,
            fg_color=T.FIELD_BG, border_color=T.FIELD_BORDER, border_width=1,
            corner_radius=6, **kw
        )
        self._on_change = on_change
        self._guard = False
        self._var.trace_add("write", self._reformat)

    # ---- helpers -----------------------------------------------------

    @staticmethod
    def _digits(text: str) -> str:
        return "".join(c for c in str(text) if c.isdigit())[:6]

    @staticmethod
    def _format(digits: str) -> str:
        parts = [digits[i:i + 2] for i in range(0, len(digits), 2)]
        return ":".join(p for p in parts if p)

    @staticmethod
    def _caret_after_digits(text: str, n: int) -> int:
        """Index just past the nth digit of `text` (0 -> start of string).

        The caret is tracked by *digit count* rather than character offset,
        because inserting a colon shifts every offset after it.
        """
        if n <= 0:
            return 0
        seen = 0
        for i, c in enumerate(text):
            if c.isdigit():
                seen += 1
                if seen == n:
                    return i + 1
        return len(text)

    def _place_caret(self, pos: int) -> None:
        try:
            self.icursor(pos)
        except Exception:
            pass                          # widget went away mid-edit

    def _reformat(self, *_a) -> None:
        if self._guard:
            return
        raw = self._var.get()
        try:
            caret = self.index("insert")
        except Exception:
            caret = len(raw)
        # How many digits sit left of the caret? That survives reformatting;
        # a character offset does not.
        digits_left = sum(1 for c in raw[:caret] if c.isdigit())

        want = self._format(self._digits(raw))
        if want != raw:
            self._guard = True
            self._var.set(want)
            self._guard = False
            # Restore the caret on the next idle cycle. Setting it here is
            # discarded: this runs inside the variable's write trace, before Tk
            # has finished applying the new text to the widget. That was the
            # bug that turned "123456" into "12:45:63" -- the caret stayed left
            # of the inserted colon, so every later digit landed before it.
            self.after_idle(self._place_caret,
                            self._caret_after_digits(want, digits_left))
        if self._on_change:
            self._on_change()

    # ---- public ------------------------------------------------------

    def get(self) -> str:
        return self._var.get()

    def set(self, text: str) -> None:
        self._var.set(self._format(self._digits(text)))

    def clear(self) -> None:
        self._var.set("")

    def set_on_change(self, cb) -> None:
        """Attach the callback after the owner is fully built.

        Setting an initial value fires the callback, and during __init__ the
        widgets it wants to update do not exist yet.
        """
        self._on_change = cb

    @property
    def complete(self) -> bool:
        return len(self._digits(self._var.get())) == 6
