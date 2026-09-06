"""
The rail: four chapters, in the order a survey day happens.

Aboard ROV, then Flight report, then Photos, then Video. That is the sequence
of a field day and then of the work that follows it, and the rail exists to
show it -- so it names the four and nothing else. The tools inside a chapter
are reached by a strip along the top of the chapter's own page, which keeps the
rail four items long however many tools accumulate.

Two things here are unusual for CustomTkinter.

**The four are buttons, and each carries its own brand colour.** They are the
roadmap, so they are drawn larger than a standard button, rounded, bordered and
spaced apart rather than stacked as a list. The type on each is chosen by
measuring against its own fill -- Seafoam takes dark type where Salish takes
White -- so the palette in `theme.CHAPTER_COLOURS` can be swapped without
anyone remembering to swap the type with it.

**The rail is a canvas, not a column of CTkButtons.** Tk has no rounded
rectangle and no anti-aliasing, so the buttons are drawn with PIL and placed as
images; that also buys exact control over type size, hover and the disabled
state. The section strip's gradient underline needs a canvas outright.

**Sizes are measured, not assumed.** Button height and rail width come from the
rendered font, so a laptop at 250% display scaling gets a rail that fits its
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

#: Where the step numeral sits inside a chapter button, and the clearance
#: kept between it and the centred name.
BTN_NUM_X = 16
BTN_PAD_X = 14
#: Never narrower than this, however short the chapter names get.
MIN_RAIL_W = 190


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
        style = T.SECTION_MARK_STYLE
        btn = ctk.CTkButton(
            self, text=name, font=T.FONT_SECTION, height=34,
            corner_radius=8 if style in ("outline", "pill") else 0,
            fg_color="transparent", hover_color=T.SURFACE,
            text_color=T.TEXT_MUTED,
            command=lambda n=name: self._on_select(n),
        )
        # "topline" puts the rule above the tab, so the two swap rows.
        rows = (1, 0) if style == "topline" else (0, 1)
        btn.grid(row=rows[0], column=col, sticky="ew", padx=(0, 4))

        # A canvas because the marker can be a gradient, and a CTkLabel holding
        # an image is the wrong tool for it. A first attempt did use one and
        # dropped the PhotoImage as soon as the tool was deselected, which left
        # Tk holding a handle to an image Python had already freed --
        # "image pyimage9 doesn't exist".
        mark = tkinter_mod.Canvas(
            self, height=max(1, int(T.SECTION_MARK_HEIGHT * T.scale_of(self))),
            highlightthickness=0, borderwidth=0)
        mark.grid(row=rows[1], column=col, sticky="ew", padx=(0, 4))
        mark.bind("<Configure>", lambda _e, n=name: self._paint_mark(n))
        if style in ("outline", "pill"):
            mark.grid_remove()          # the tab carries its own marking
        self._buttons[name] = btn
        self._marks[name] = mark

    def select(self, name: str) -> None:
        self._current = name
        style = T.SECTION_MARK_STYLE
        for n, btn in self._buttons.items():
            on = n == name
            btn.configure(text_color=T.HEADING if on else T.TEXT_MUTED,
                          font=T.FONT_SECTION_ON if on else T.FONT_SECTION)
            if style == "outline":
                btn.configure(border_width=2 if on else 0,
                              border_color=T.ACCENT, fg_color="transparent")
            elif style == "pill":
                btn.configure(fg_color=T.SURFACE if on else "transparent")
            self._paint_mark(n)

    def _paint_mark(self, name: str) -> None:
        """Mark the open tool, in whichever style the theme asks for.

        Repainted on <Configure> as well as on selection, so it arrives at the
        right width whenever the strip is finally laid out -- there is no
        retry timer to leak.
        """
        style = T.SECTION_MARK_STYLE
        mark = self._marks[name]
        mark.delete("all")
        ground = self._apply_appearance_mode(T.BG)
        mark.configure(background=ground)
        if name != self._current or style in ("outline", "pill"):
            mark._photo = None          # noqa: SLF001 -- the reference Tk needs
            return
        w = max(1, mark.winfo_width())
        h = max(1, mark.winfo_height())
        if style == "hairline":
            colour = self._apply_appearance_mode(T.ACCENT)
            mark.create_rectangle(0, h // 2, w, h, fill=colour, outline="")
            mark._photo = None          # noqa: SLF001
            return
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
        self._btn_h = T.CHAPTER_BTN_H
        self._gap = T.CHAPTER_BTN_GAP
        self._inset = T.CHAPTER_BTN_INSET
        self._radius = T.CHAPTER_BTN_RADIUS
        self._border = T.CHAPTER_BTN_BORDER
        self._border_on = T.CHAPTER_BTN_BORDER_ON
        self._top = T.CHAPTER_BTN_TOP
        self._num_x = BTN_NUM_X
        self._font_name = T.FONT_RAIL
        self._font_name_off = T.FONT_RAIL_SMALL
        self._font_num = T.FONT_RAIL_NUM
        #: Tk keeps no reference to a PhotoImage, so the rail holds them.
        self._photos: list[ImageTk.PhotoImage] = []

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
        """Button geometry from the rendered font, not from a guess.

        Everything here is in real pixels: the canvas is raw Tk, which does not
        get CustomTkinter's display scaling, so the constants are scaled on the
        way in and the measured text already comes back scaled.
        """
        s = T.scale_of(self)
        self._scale = s
        self._font_name = T.scale_font(T.FONT_RAIL, s)
        self._font_name_off = T.scale_font(T.FONT_RAIL_SMALL, s)
        self._font_num = T.scale_font(T.FONT_RAIL_NUM, s)
        widest = max((self._text_w(n, self._font_name) for n in self._chapters),
                     default=0)
        num_w = max((self._text_w(str(i + 1), self._font_num)
                     for i in range(len(self._chapters))), default=0)

        self._btn_h = int(T.CHAPTER_BTN_H * s)
        self._gap = int(T.CHAPTER_BTN_GAP * s)
        self._inset = int(T.CHAPTER_BTN_INSET * s)
        self._radius = int(T.CHAPTER_BTN_RADIUS * s)
        self._border = max(1, int(T.CHAPTER_BTN_BORDER * s))
        self._border_on = max(1, int(T.CHAPTER_BTN_BORDER_ON * s))
        self._top = int(T.CHAPTER_BTN_TOP * s)
        self._num_x = int(BTN_NUM_X * s)
        # The name is centred, so the room the numeral takes has to be
        # reserved on *both* sides of it -- otherwise the longest name grows
        # leftward until it collides with the number.
        side = self._num_x + num_w + int(BTN_PAD_X * s)
        self._rail_w = int(max(MIN_RAIL_W * s,
                               widest + side * 2 + self._inset * 2))
        self.rail.configure(width=self._rail_w)

    # ------------------------------------------------------------------
    #  drawing
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        """Four buttons, each in its own brand colour, on a flat surface.

        The rail is the roadmap through a survey day, so the four read as
        objects rather than as a list: rounded, bordered, larger than a
        standard button and spaced apart.

        Each carries its own colour, and the type on it is chosen by measuring
        against that colour rather than from a table -- Seafoam takes dark
        type, Salish takes White, and a palette can be swapped without anyone
        remembering to swap the type with it.
        """
        c = self.rail
        w, h = max(1, c.winfo_width()), max(1, c.winfo_height())
        c.delete("all")
        self._photos.clear()

        mode = self._apply_appearance_mode
        ground = mode(T.RAIL_BG)
        c.configure(background=ground)
        c.create_rectangle(0, 0, w, h, fill=ground, outline="")

        inset, btn_w = self._inset, max(1, w - self._inset * 2)

        for name, ch in self._chapters.items():
            y = self._btn_y(ch.index)
            on = name == self._current_chapter
            live = self._chapter_enabled(name)
            state = ("on" if on else
                     "hover" if name == self._hover else "off")

            fill, border, width, radius, bar, ink = self._button_look(
                self._colour_for(ch.index), state, live, mode)

            img = G.chip((btn_w, self._btn_h), fill=fill, border=border,
                         border_w=width, radius=radius, ground=ground)
            photo = ImageTk.PhotoImage(img)
            self._photos.append(photo)      # Tk keeps no reference of its own
            c.create_image(inset, y, image=photo, anchor="nw")

            if bar:
                # A colour bar down the leading edge, clipped to the button's
                # own corner radius so it does not poke out of the rounding.
                bw = max(1, int(T.CHAPTER_BTN_BAR * self._scale))
                c.create_rectangle(inset + width, y + radius // 2,
                                   inset + width + bw,
                                   y + self._btn_h - radius // 2,
                                   fill=bar, outline="")

            cy = y + self._btn_h / 2

            # The numeral sits at a fixed inset so the four line up as a
            # column -- they are the roadmap's step numbers, and centring them
            # with their names left them ragged. The name is centred; the rail
            # is sized so it can never reach back to the numeral.
            c.create_text(inset + self._num_x, cy, anchor="w",
                          text=str(ch.index), font=self._font_num, fill=ink,
                          tags=("row", f"row:{name}"))
            c.create_text(inset + btn_w / 2, cy, anchor="center", text=name,
                          font=self._font_name if on else self._font_name_off,
                          fill=ink, tags=("row", f"row:{name}"))

    def _button_look(self, colour: str, state: str, live: bool, mode):
        """Fill, border, border width, radius, leading bar and ink.

        One place for all six styles, so a variant is a table entry rather
        than a branch scattered through the drawing code.

        Two rules hold across every style. A disabled chapter loses its colour
        entirely -- a greyed-out button that keeps a saturated fill still looks
        pressable. And type is never set *in* Algae or Seafoam: on a light
        ground they measure 2.2:1 and 1.9:1, so the colour goes in a fill or a
        border and `ink_for` picks what sits on it.
        """
        style = T.CHAPTER_BTN_STYLE
        surface = mode(T.SURFACE_ALT)
        text, muted, edge = mode(T.TEXT), mode(T.TEXT_MUTED), mode(T.BORDER)
        b, b_on = self._border, self._border_on
        r = T.chapter_btn_radius(self._radius, self._btn_h)
        on, hover = state == "on", state == "hover"

        if not live:
            return surface, edge, 0, r, "", muted

        if style == "outline":
            fill = colour if on else surface
            return (fill, colour, b_on if (on or hover) else b, r, "",
                    T.ink_for(colour) if on else text)

        if style == "leftbar":
            return (surface, colour if on else edge, b_on if on else b, r,
                    colour, text if (on or hover) else muted)

        if style == "ghost":
            # Colour arrives only on the chapter you chose.
            fill = colour if on else surface
            return (fill, colour if (on or hover) else edge,
                    b_on if on else b, r, "",
                    T.ink_for(colour) if on else text)

        # "solid".
        if on:
            border, width = text, b_on
        elif hover:
            border, width = muted, b_on
        else:
            border, width = edge, b
        return colour, border, width, r, "", T.ink_for(colour)

    def _btn_y(self, index: int) -> int:
        """Top of the button for chapter `index` (1-based)."""
        return self._top + (index - 1) * (self._btn_h + self._gap)

    def _colour_for(self, index: int) -> str:
        palette = T.CHAPTER_COLOURS
        return palette[(index - 1) % len(palette)]

    def _text_w(self, text: str, font: tuple) -> int:
        try:
            from tkinter import font as tkfont
            return tkfont.Font(family=font[0], size=font[1]).measure(text)
        except Exception:
            return len(text) * font[1]

    def _chapter_at(self, y: int) -> str | None:
        """Which button, if any, is under `y`. The gaps between are dead."""
        for name, ch in self._chapters.items():
            y0 = self._btn_y(ch.index)
            if y0 <= y < y0 + self._btn_h:
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
