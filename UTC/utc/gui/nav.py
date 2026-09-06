"""
The rail: four chapters, in the order a survey day happens.

Aboard ROV, then Flight report, then Photos, then Video. That is the sequence
of a field day and then of the work that follows it, and the rail exists to
show it -- so it names the four and nothing else. The tools inside a chapter
are reached by a strip along the top of the chapter's own page, which keeps the
rail four items long however many tools accumulate.

Two things here are unusual for CustomTkinter.

**The rail is a canvas, not a column of buttons.** It is flat -- a gradient
behind four rows gives each of them a different ground, which reads as four
states rather than one control -- but a canvas still buys exact control over
type size, the marker and hover, none of which a CTkButton gives up willingly.
The section strip's underline is drawn, and that needs a canvas outright.

**Sizes are measured, not assumed.** Row height and rail width come from the
rendered font, so a laptop at 150% display scaling gets a rail that fits its
own type rather than one sized for somebody else's screen. An earlier version
of this file hard-coded a row height and pushed the last page off the bottom of
the rail on exactly such a machine.
"""

from __future__ import annotations

import tkinter as tkinter_mod
from collections.abc import Callable
from dataclasses import dataclass, field

import customtkinter as ctk
from PIL import ImageTk

from . import gradients as G
from . import theme as T

#: Space around a chapter name inside its row.
ROW_PAD_Y = 11
#: Left inset for the numeral, and for the name after it.
NUM_X = 20
NAME_X = 46
#: The selected chapter's marker.
STRIPE_W = 4
#: Never narrower than this, however short the chapter names get.
MIN_RAIL_W = 176


@dataclass
class Chapter:
    """One rail entry, and the tools that live under it."""

    name: str
    index: int
    nav: Navigator
    page: ctk.CTkFrame
    strip: SectionStrip | None = None
    holder: ctk.CTkFrame | None = None
    sections: list[str] = field(default_factory=list)

    def add(self, section: str) -> ctk.CTkFrame:
        """Register a tool in this chapter and return its frame."""
        return self.nav._add_section(self, section)


class SectionStrip(ctk.CTkFrame):
    """The tools within one chapter, as a row of text with an active marker.

    Only built when a chapter has more than one -- a strip with a single entry
    is a label pretending to be a control.
    """

    def __init__(self, master, on_select: Callable[[str], None]):
        super().__init__(master, fg_color="transparent")
        self._on_select = on_select
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._marks: dict[str, tkinter_mod.Canvas] = {}
        self._current: str | None = None

    def add(self, name: str) -> None:
        col = len(self._buttons)
        btn = ctk.CTkButton(
            self, text=name, font=T.FONT_BODY, height=28, corner_radius=0,
            fg_color="transparent", hover_color=T.SURFACE,
            text_color=T.TEXT_MUTED,
            command=lambda n=name: self._on_select(n),
        )
        btn.grid(row=0, column=col, sticky="ew", padx=(0, 4))

        # A canvas because the underline is a gradient, and a CTkLabel
        # holding an image is the wrong tool for it.
        # A first attempt did use one and dropped the PhotoImage as soon as the
        # tool was deselected, which left Tk holding a handle to an image
        # Python had already freed -- "image pyimage9 doesn't exist".
        mark = tkinter_mod.Canvas(self, height=int(T.RULE_HEIGHT * T.scale_of(self)),
                                  highlightthickness=0, borderwidth=0)
        mark.grid(row=1, column=col, sticky="ew", padx=(0, 4))
        mark.bind("<Configure>", lambda _e, n=name: self._paint_mark(n))
        self._buttons[name] = btn
        self._marks[name] = mark

    def select(self, name: str) -> None:
        self._current = name
        for n, btn in self._buttons.items():
            on = n == name
            btn.configure(text_color=T.HEADING if on else T.TEXT_MUTED,
                          font=T.FONT_H2 if on else T.FONT_BODY)
            self._paint_mark(n)

    def _paint_mark(self, name: str) -> None:
        """The open tool gets a bright-gradient underline.

        Repainted on <Configure> as well as on selection, so it arrives at the
        right width whenever the strip is finally laid out -- there is no
        retry timer to leak.
        """
        mark = self._marks[name]
        mark.delete("all")
        ground = self._apply_appearance_mode(T.BG)
        mark.configure(background=ground)
        if name != self._current:
            mark._photo = None          # noqa: SLF001 -- the reference Tk needs
            return
        w = max(1, mark.winfo_width())
        h = max(1, mark.winfo_height())
        img = G.render((w, h), T.STRIPE_GRADIENT, angle=0.0)
        # Held on the widget: Tk keeps no reference of its own, and a photo
        # collected while the canvas still shows it is a hard error.
        mark._photo = ImageTk.PhotoImage(img)      # noqa: SLF001
        mark.create_image(0, 0, image=mark._photo, anchor="nw")

    def set_enabled(self, enabled: bool) -> None:
        for btn in self._buttons.values():
            btn.configure(state="normal" if enabled else "disabled")

    def repaint(self) -> None:
        """After an appearance-mode change, when the ground beneath moves."""
        for name in self._marks:
            self._paint_mark(name)


class Navigator(ctk.CTkFrame):
    """A rail of chapters beside a single-page content area."""

    def __init__(self, master, on_select: Callable[[str], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self._on_select = on_select
        self._chapters: dict[str, Chapter] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}       # section -> frame
        self._owner: dict[str, Chapter] = {}            # section -> chapter
        self._current: str | None = None                # current section
        self._current_chapter: str | None = None
        self._hover: str | None = None
        self._enabled: dict[str, bool] = {}
        self._scale = 1.0
        self._rail_w = MIN_RAIL_W
        self._row_h = 44
        self._top = ROW_PAD_Y
        self._name_x, self._num_x = NAME_X, NUM_X
        self._font_name = T.FONT_RAIL
        self._font_name_off = T.FONT_RAIL_SMALL
        self._font_num = T.FONT_SMALL

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # A raw canvas: highlightthickness and borderwidth both default to
        # non-zero and would draw a light frame around the gradient.
        self.rail = tkinter_mod.Canvas(self, width=self._rail_w, height=10,
                                       highlightthickness=0, borderwidth=0,
                                       background=self._apply_appearance_mode(
                                           T.RAIL_BG))
        self.rail.grid(row=0, column=0, sticky="nsw")
        self.rail.bind("<Configure>", lambda _e: self._redraw())
        self.rail.bind("<Button-1>", self._clicked)
        self.rail.bind("<Motion>", self._moved)
        self.rail.bind("<Leave>", lambda _e: self._set_hover(None))

        # The rail runs edge to edge, from the banner's rule down to the
        # footer: it is meant to read as one continuous pathway, and a gap of
        # window ground above or below it breaks that. The breathing room the
        # cards need therefore lives on the content side.
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(16, 16),
                          pady=(10, 8))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    # ------------------------------------------------------------------
    #  building
    # ------------------------------------------------------------------

    def add_chapter(self, name: str) -> Chapter:
        holder = ctk.CTkFrame(self.content, fg_color="transparent")
        holder.grid(row=0, column=0, sticky="nsew")
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(1, weight=1)
        holder.lower()

        ch = Chapter(name=name, index=len(self._chapters) + 1, nav=self,
                     page=holder, holder=holder)
        self._chapters[name] = ch
        self._measure()
        return ch

    def _add_section(self, ch: Chapter, section: str) -> ctk.CTkFrame:
        if ch.strip is None:
            ch.strip = SectionStrip(ch.holder, on_select=self.select)
            # Kept out of the layout until a second tool arrives.
            ch.strip.grid(row=0, column=0, sticky="w", pady=(0, 8))
            ch.strip.grid_remove()
        ch.strip.add(section)
        if len(ch.sections) >= 1:
            ch.strip.grid()

        page = ctk.CTkFrame(ch.holder, fg_color="transparent")
        page.grid(row=1, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        page.lower()

        ch.sections.append(section)
        self._pages[section] = page
        self._owner[section] = ch
        self._enabled[section] = True
        if self._current is None:
            self.select(section)
        return page

    def _measure(self) -> None:
        """Rail width and row height from the rendered font, not from a guess.

        Everything here is in real pixels: the canvas is raw Tk, which does not
        get CustomTkinter's display scaling, so the constants are scaled on the
        way in and the measured text already comes back scaled.
        """
        s = T.scale_of(self)
        self._scale = s
        self._font_name = T.scale_font(T.FONT_RAIL, s)
        self._font_name_off = T.scale_font(T.FONT_RAIL_SMALL, s)
        self._font_num = T.scale_font(T.FONT_SMALL, s)
        try:
            from tkinter import font as tkfont
            f = tkfont.Font(family=self._font_name[0], size=self._font_name[1])
            line = f.metrics("linespace")
            widest = max((f.measure(n) for n in self._chapters), default=0)
        except Exception:
            line, widest = int(20 * s), int(120 * s)
        self._top = int(ROW_PAD_Y * s)
        self._row_h = int(line + ROW_PAD_Y * 2 * s)
        self._name_x = int(NAME_X * s)
        self._num_x = int(NUM_X * s)
        self._rail_w = int(max(MIN_RAIL_W * s, self._name_x + widest + 22 * s))
        self.rail.configure(width=self._rail_w)

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def _mode(self) -> str:
        return ctk.get_appearance_mode().lower()

    def _redraw(self) -> None:
        """The rail, on one flat surface colour.

        Deliberately not a gradient. The rail is a list of four things that
        differ only by which one is open, and a gradient behind them makes each
        row sit on a slightly different ground -- which reads as four states
        rather than one control. The banner is a single object and can carry
        one; this cannot. It takes its distinctness from the surface colour
        instead, a step off the window ground in both modes.
        """
        c = self.rail
        w, h = max(1, c.winfo_width()), max(1, c.winfo_height())
        c.delete("all")

        ink = self._apply_appearance_mode
        ground = ink(T.RAIL_BG)
        c.configure(background=ground)
        c.create_rectangle(0, 0, w, h, fill=ground, outline="")

        stripe_w = max(1, int(STRIPE_W * self._scale))
        accent, text, muted = ink(T.ACCENT), ink(T.TEXT), ink(T.TEXT_MUTED)
        dim = ink(T.BORDER)

        for name, ch in self._chapters.items():
            y = self._top + (ch.index - 1) * self._row_h
            on = name == self._current_chapter
            live = self._chapter_enabled(name)
            if on:
                c.create_rectangle(0, y, stripe_w, y + self._row_h,
                                   fill=accent, outline="")
            if live:
                colour = text if (on or name == self._hover) else muted
            else:
                colour = dim
            c.create_text(self._num_x, y + self._row_h / 2, anchor="w",
                          text=str(ch.index), font=self._font_num,
                          fill=muted if live else dim,
                          tags=("row", f"row:{name}"))
            c.create_text(self._name_x, y + self._row_h / 2, anchor="w",
                          text=name,
                          font=self._font_name if on else self._font_name_off,
                          fill=colour, tags=("row", f"row:{name}"))

    def _chapter_at(self, y: int) -> str | None:
        for name, ch in self._chapters.items():
            y0 = self._top + (ch.index - 1) * self._row_h
            if y0 <= y < y0 + self._row_h:
                return name
        return None

    def _chapter_enabled(self, name: str) -> bool:
        ch = self._chapters.get(name)
        if ch is None:
            return True
        return any(self._enabled.get(s, True) for s in ch.sections)

    def _clicked(self, event) -> None:
        name = self._chapter_at(event.y)
        if name and self._chapter_enabled(name):
            self.select_chapter(name)

    def _moved(self, event) -> None:
        self._set_hover(self._chapter_at(event.y))

    def _set_hover(self, name: str | None) -> None:
        if name == self._hover:
            return
        self._hover = name
        self.rail.configure(cursor="hand2" if name else "")
        self._redraw()

    # ------------------------------------------------------------------
    #  selection
    # ------------------------------------------------------------------

    def select_chapter(self, name: str) -> None:
        ch = self._chapters.get(name)
        if ch is None or not ch.sections:
            return
        # Return to whichever tool was last open in this chapter.
        wanted = ch.sections[0]
        if self._current in ch.sections:
            wanted = self._current
        elif getattr(ch, "_last", None) in ch.sections:
            wanted = ch._last                                # type: ignore[attr-defined]
        self.select(wanted)

    def select(self, name: str) -> None:
        """Raise one tool, by its own name."""
        if name not in self._pages:
            return
        ch = self._owner[name]
        ch._last = name                                      # type: ignore[attr-defined]
        self._current = name
        self._current_chapter = ch.name

        ch.holder.lift()
        self._pages[name].lift()
        if ch.strip is not None:
            ch.strip.select(name)
        self._redraw()
        if self._on_select:
            self._on_select(name)

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def current_chapter(self) -> str | None:
        return self._current_chapter

    @property
    def sections(self) -> list[str]:
        return list(self._pages)

    @property
    def chapters(self) -> list[str]:
        return list(self._chapters)

    # ------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Grey out one tool. Accepts a chapter name or a tool name."""
        if name in self._chapters:
            for s in self._chapters[name].sections:
                self.set_enabled(s, enabled)
            return
        if name not in self._pages:
            return
        self._enabled[name] = enabled
        ch = self._owner[name]
        if ch.strip is not None:
            btn = ch.strip._buttons.get(name)
            if btn is not None:
                btn.configure(state="normal" if enabled else "disabled")
        self._redraw()

    def set_locked(self, locked: bool) -> None:
        """Freeze navigation entirely.

        Used while the Lightroom batch drives another application's window with
        synthetic keystrokes: a click that changes page mid-sequence sends the
        rest of the keys somewhere nobody intended.
        """
        for name in self._pages:
            self.set_enabled(name, not locked)

    def refresh_theme(self) -> None:
        """Redraw after the appearance mode changes."""
        self._measure()
        self._redraw()
        for ch in self._chapters.values():
            if ch.strip is not None:
                ch.strip.repaint()
