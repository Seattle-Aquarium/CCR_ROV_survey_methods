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

    def add_grip(self, target, minimum: int, *, maximum: int | None = None,
                 get_height=None, set_height=None) -> Grip:
        """Let the card's bottom edge be dragged to resize `target`.

        The handle sits on the card's own lower border, so what gets dragged is
        the section itself; only `target` changes size. It stops at `minimum`,
        which for an output box is one line of text.
        """
        get_height = get_height or (lambda: float(target.cget("height")))
        set_height = set_height or (lambda h: target.configure(height=h))
        self.body.grid_configure(pady=(4, 0))
        grip = Grip(self, get_height, set_height, minimum=minimum,
                    maximum=maximum)
        grip.grid(row=2, column=0, sticky="ew", padx=T.RADIUS, pady=(0, 1))
        self._grip = grip
        return grip


class Grip(ctk.CTkFrame):
    """A thin handle that resizes one widget when dragged up or down.

    Heights are CustomTkinter's unscaled units, the ones `configure(height=)`
    takes, so the pointer's movement is divided by the display scaling on the
    way in -- otherwise a 250% laptop would move the edge two and a half times
    faster than the mouse.

    Double-click jumps between the smallest size and a comfortable one, for
    the times dragging is more effort than the glance is worth.
    """

    CURSOR = "sb_v_double_arrow"

    def __init__(self, master, get_height, set_height, *, minimum: int,
                 maximum: int | None = None, grows_downward: bool = True,
                 expanded: int = 240, thickness: int = 10):
        super().__init__(master, fg_color="transparent", height=thickness,
                         corner_radius=0, cursor=self.CURSOR)
        self._get, self._set = get_height, set_height
        self.minimum, self.maximum = int(minimum), maximum
        self._sign = 1 if grows_downward else -1
        self._expanded = max(expanded, int(minimum) * 3)
        self._y0 = 0
        self._h0 = 0.0
        self._pending: int | None = None
        self._scheduled = False
        self.bar = ctk.CTkFrame(self, width=46, height=4, corner_radius=2,
                                fg_color=T.BORDER, cursor=self.CURSOR)
        self.bar.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self, self.bar):
            w.bind("<ButtonPress-1>", self._press, add="+")
            w.bind("<B1-Motion>", self._drag, add="+")
            w.bind("<Double-Button-1>", self._toggle, add="+")
            w.bind("<Enter>", lambda _e: self.bar.configure(fg_color=T.ACCENT),
                   add="+")
            w.bind("<Leave>", lambda _e: self.bar.configure(fg_color=T.BORDER),
                   add="+")

    def _scaling(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
        except Exception:
            return 1.0

    def clamp(self, h: float) -> int:
        h = max(self.minimum, h)
        if self.maximum is not None:
            h = min(self.maximum, h)
        return int(h)

    def _press(self, event) -> None:
        self._y0 = event.y_root
        try:
            self._h0 = float(self._get())
        except Exception:
            self._h0 = float(self.minimum)

    def _drag(self, event) -> None:
        moved = (event.y_root - self._y0) / self._scaling()
        self._pending = self.clamp(self._h0 + self._sign * moved)
        # Motion arrives far faster than a textbox can re-lay itself out, so
        # only the latest height is applied, once per frame or so.
        if not self._scheduled:
            self._scheduled = True
            self.after(16, self._apply)

    def _apply(self) -> None:
        self._scheduled = False
        if self._pending is not None:
            try:
                self._set(self._pending)
            except Exception:
                pass

    def _toggle(self, _event=None) -> None:
        try:
            now = float(self._get())
        except Exception:
            now = self.minimum
        self._set(self.clamp(self._expanded if now <= self.minimum + 4
                             else self.minimum))


def fit_wrap(container, *labels, margin: int = 24) -> None:
    """Keep labels wrapped to their container's width as it changes.

    A fixed wraplength suits a full-width section and overflows a half-width
    one; sections now sit side by side, so the width is followed instead.
    """
    def fit(event) -> None:
        try:
            scaling = float(ctk.ScalingTracker.get_widget_scaling(container)) or 1.0
        except Exception:
            scaling = 1.0
        target = int(max(120, event.width - margin) / scaling)
        for lab in labels:
            try:
                if abs(int(lab.cget("wraplength")) - target) > 8:
                    lab.configure(wraplength=target)
            except Exception:
                pass

    container.bind("<Configure>", fit, add="+")


def one_line_height(wrap: str = "word") -> int:
    """The smallest an output box may get and still show a line of text.

    Measured from the monospace font rather than assumed, in the unscaled
    units a CTkTextbox takes. A box that does not wrap also has to leave room
    for its horizontal scrollbar, which would otherwise sit on the one line.
    """
    from tkinter import font as tkfont
    try:
        # CustomTkinter sets a tuple font's size as pixels, so ask for pixels.
        line = tkfont.Font(family=T.MONO, size=-abs(T.FONT_MONO[1])
                           ).metrics("linespace")
    except Exception:
        line = 15
    base = line + 18
    return base + (16 if wrap == "none" else 0)


def output_box(master, *, wrap: str = "word", muted: bool = True,
               height: int | None = None) -> ctk.CTkTextbox:
    """A read-only report box that opens at its smallest, one line tall."""
    box = ctk.CTkTextbox(master, height=height or one_line_height(wrap),
                         font=T.FONT_MONO, fg_color=T.FIELD_BG,
                         text_color=T.TEXT_MUTED if muted else T.TEXT,
                         border_width=1, border_color=T.BORDER,
                         corner_radius=6, wrap=wrap)
    box.min_height = one_line_height(wrap)          # type: ignore[attr-defined]
    return box


def say(box, text: str) -> None:
    """Replace a read-only textbox's contents."""
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


def entry(master, placeholder: str = "", width: int = 140, **kw) -> ctk.CTkEntry:
    return ctk.CTkEntry(
        master, placeholder_text=placeholder, width=width,
        font=T.FONT_BODY, text_color=T.TEXT, fg_color=T.FIELD_BG,
        border_color=T.FIELD_BORDER, border_width=1, corner_radius=6, **kw
    )


def checkbox(master, text: str, variable, command=None) -> ctk.CTkCheckBox:
    return ctk.CTkCheckBox(master, text=text, variable=variable,
                           font=T.FONT_BODY, text_color=T.TEXT,
                           fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
                           checkmark_color=T.ACCENT_TEXT,
                           border_color=T.FIELD_BORDER, corner_radius=4,
                           command=command)


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
