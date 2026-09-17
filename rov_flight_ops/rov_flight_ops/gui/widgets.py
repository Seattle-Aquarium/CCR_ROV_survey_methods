"""Reusable GUI pieces: section cards, and the site / transect editors."""

from __future__ import annotations

import tkinter
from collections.abc import Callable
from datetime import date as _date

import customtkinter as ctk

from ..survey import Pause, Site, Transect
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


class Repaint:
    """Runs a redraw at most once a frame, and once more when it settles.

    Tk delivers ``<Configure>`` as fast as the window manager can move the
    edge -- far faster than a canvas full of items can be rebuilt, and faster
    than a 60 Hz display can show. Doing the work on every one of them is what
    made dragging a window edge lag a second behind the pointer: each event's
    repaint delayed the next event, which was already stale by the time it was
    drawn.

    So the work runs on the next frame boundary and everything asked for in
    between is dropped -- the intermediate sizes were never going to be seen.
    One more pass is then scheduled for after the last event, so the final
    size is drawn from the size it actually ended at rather than from whichever
    event happened to land on a frame boundary. That last pass is the one that
    has to be right; the ones during the drag only have to be quick.
    """

    #: One frame at 60 Hz. Asking for more than this cannot reach the screen.
    FRAME_MS = 16
    #: How long after the last event the accurate pass runs. Long enough to
    #: sit out the gaps between a window manager's events, short enough that
    #: letting go of the edge feels like it finished immediately.
    SETTLE_MS = 90

    def __init__(self, widget, work: Callable[[], None], *,
                 frame_ms: int | None = None, settle_ms: int | None = None):
        self._widget = widget
        self._work = work
        self.frame_ms = self.FRAME_MS if frame_ms is None else frame_ms
        self.settle_ms = self.SETTLE_MS if settle_ms is None else settle_ms
        self._frame_job = None
        self._settle_job = None
        #: True while a burst of events is still arriving, for work that can
        #: be done cheaply now and properly at the end.
        self.busy = False

    def ask(self, _event=None) -> None:
        """Something changed. Draw soon, and again once it has settled."""
        self.busy = True
        if self._frame_job is None:
            self._frame_job = self._after(self.frame_ms, self._frame)
        self._cancel(self._settle_job)
        self._settle_job = self._after(self.settle_ms, self._settle)

    def now(self) -> None:
        """Draw immediately, dropping anything already scheduled."""
        self._cancel(self._frame_job)
        self._cancel(self._settle_job)
        self._frame_job = self._settle_job = None
        self.busy = False
        self._work()

    def cancel(self) -> None:
        self._cancel(self._frame_job)
        self._cancel(self._settle_job)
        self._frame_job = self._settle_job = None
        self.busy = False

    # ---- internals -----------------------------------------------------

    def _frame(self) -> None:
        self._frame_job = None
        self._work()

    def _settle(self) -> None:
        self._settle_job = None
        self.busy = False
        self._work()

    def _after(self, ms: int, fn):
        try:
            return self._widget.after(ms, fn)
        except Exception:
            return None                   # the window is going away

    def _cancel(self, job) -> None:
        if job is None:
            return
        try:
            self._widget.after_cancel(job)
        except Exception:
            pass                          # already run, or the window is gone


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


class OutputBox(ctk.CTkTextbox):
    """A report box whose scrollbars settle instead of flickering.

    CustomTkinter re-decides five times a second, forever, whether each
    scrollbar is needed, and it asks the question of the lines the widget is
    *displaying*. In a box one line tall that is a loop: showing the
    horizontal bar costs the text a line of height, the longest line scrolls
    out of the display, Tk then reports nothing left to scroll sideways, the
    bar goes away, the line comes back, and around again. The first thing
    anyone saw on opening the window was the path-to-the-vehicle box twitching
    -- measured at 4.8 changes a second, which is exactly CustomTkinter's
    200 ms poll interval.

    So the poll is stopped and the sideways question is asked of the *text*
    instead: the width of its longest line against the width of the text area,
    which does not change when a scrollbar appears. The vertical bar is still
    decided from what Tk reports, which is safe once the height is stable --
    narrowing a box can only add wrapped lines, never remove them, so showing
    the bar can never un-justify itself.

    Decisions are made when something has actually changed -- new text, a
    resize, a drag of the grip -- and coalesced to one per idle cycle.
    """

    #: Slack before the text counts as too wide, in pixels. A line that ends
    #: within a couple of pixels of the edge does not earn a scrollbar.
    SLACK_PX = 4
    #: How many lines are measured. The longest few by character count, not
    #: all of them: a day's log is thousands, and measuring them on every
    #: settle would cost more than the flicker did.
    MEASURE_LINES = 8

    def __init__(self, master, **kw):
        self._settle_pending = False
        self._settling = False
        #: The widest line in pixels, and the font it was measured with.
        #: Both are worked out once per change of text rather than once per
        #: settle: fetching the whole text across the Tcl boundary and asking
        #: the font system to resolve a family are each dear enough to show
        #: up while a window edge is being dragged, and neither answer
        #: changes when only the width does.
        self._widest_px: int | None = None
        self._measure_font: object | None = None
        super().__init__(master, **kw)
        # The outer frame's own <Configure>. CTkTextbox.bind sends bindings to
        # the inner Text widget, which is the one the scrollbars resize, so
        # this goes on the frame directly rather than through it.
        tkinter.Frame.bind(self, "<Configure>", self.settle, add="+")

    # ---- replacing CustomTkinter's poll --------------------------------

    def _check_if_scrollbars_needed(self, event=None, continue_loop: bool = False):
        """CustomTkinter's hook, which normally reschedules itself forever.

        Deliberately does not reschedule: `settle` is called when something
        has changed instead.
        """
        self.settle()

    def settle(self, _event=None) -> None:
        """Re-decide the scrollbars, once, after the current work finishes."""
        if self._settle_pending:
            return
        self._settle_pending = True
        try:
            self.after_idle(self._settle_now)
        except Exception:                     # no event loop yet, or gone
            self._settle_pending = False

    def _settle_now(self) -> None:
        self._settle_pending = False
        if self._settling:
            return
        self._settling = True
        try:
            for want, flag, kw in ((self._wants_x(), "_hide_x_scrollbar",
                                    "re_grid_x_scrollbar"),
                                   (self._wants_y(), "_hide_y_scrollbar",
                                    "re_grid_y_scrollbar")):
                if getattr(self, flag) is want:          # hidden when wanted
                    setattr(self, flag, not want)
                    self._create_grid_for_text_and_scrollbars(**{kw: True})
        except Exception:
            pass                              # a settled scrollbar is nobody's
        finally:                              # reason to lose a report
            self._settling = False

    # ---- the two questions ---------------------------------------------

    def _wants_x(self) -> bool:
        """Is the widest line wider than the text area?

        Measured from the text, not from what is on screen, because what is on
        screen is what the answer changes -- which is the whole bug.
        """
        try:
            if str(self._textbox.cget("wrap")) != "none":
                return False                  # wrapped text never runs wide
            width = self._textbox.winfo_width()
            if width <= 1:
                return False
            return self._widest() > width - self.SLACK_PX
        except Exception:
            return False

    def _widest(self) -> int:
        """The widest line, in pixels. Re-measured only when the text changes."""
        if self._widest_px is not None:
            return self._widest_px
        lines = self._textbox.get("1.0", "end-1c").split("\n")
        if self._measure_font is None:
            from tkinter import font as tkfont
            self._measure_font = tkfont.Font(font=self._textbox.cget("font"))
        longest = sorted(lines, key=len, reverse=True)[:self.MEASURE_LINES]
        self._widest_px = max(
            (self._measure_font.measure(x) for x in longest), default=0)
        return self._widest_px

    def _wants_y(self) -> bool:
        try:
            return tuple(self._textbox.yview()) != (0.0, 1.0)
        except Exception:
            return False

    # ---- anything that changes the answer -------------------------------

    def configure(self, require_redraw=False, **kw):
        out = super().configure(require_redraw=require_redraw, **kw)
        if "font" in kw:
            self._measure_font = None
            self._widest_px = None
        if "height" in kw or "wrap" in kw or "font" in kw:
            self.settle()
        return out

    def insert(self, index, text, tags=None):
        out = super().insert(index, text, tags)
        self._widest_px = None
        self.settle()
        return out

    def delete(self, index1, index2=None):
        out = super().delete(index1, index2)
        self._widest_px = None
        self.settle()
        return out


def output_box(master, *, wrap: str = "word", muted: bool = True,
               height: int | None = None) -> OutputBox:
    """A read-only report box that opens at its smallest, one line tall."""
    box = OutputBox(master, height=height or one_line_height(wrap),
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


class PauseCell(ctk.CTkFrame):
    """One pause inside a transect: its own start, end, and a way to drop it.

    Sits to the right of the transect's own times, because it belongs to that
    transect rather than being a row of its own -- a pause is not a second
    transect, and laying it out as one invited exactly that reading.
    """

    def __init__(self, master, index: int, on_remove: Callable[[PauseCell], None],
                 on_change: Callable[[], None], start: str = "", end: str = ""):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=6,
                         border_width=1, border_color=T.BORDER)
        self._on_remove = on_remove
        self.index_label = ctk.CTkLabel(self, text=f"pause {index}",
                                        font=T.FONT_SMALL,
                                        text_color=T.TEXT_MUTED, anchor="w")
        self.index_label.grid(row=0, column=0, padx=(8, 6), pady=3)
        self.start = TimeEntry(self, width=100)
        self.start.set(start)
        self.start.grid(row=0, column=1, padx=(0, 4), pady=3)
        label(self, "to", muted=True).grid(row=0, column=2, padx=(0, 4))
        self.end = TimeEntry(self, width=100)
        self.end.set(end)
        self.end.grid(row=0, column=3, padx=(0, 4), pady=3)
        ctk.CTkButton(self, text="✕", width=24, height=24, corner_radius=6,
                      font=T.FONT_SMALL, fg_color="transparent",
                      hover_color=T.SURFACE_ALT, text_color=T.TEXT_MUTED,
                      border_width=0, command=lambda: on_remove(self)
                      ).grid(row=0, column=4, padx=(0, 6))
        for e in (self.start, self.end):
            e.set_on_change(on_change)

    def renumber(self, index: int) -> None:
        self.index_label.configure(text=f"pause {index}")

    def to_pause(self) -> Pause:
        return Pause(self.start.get().strip(), self.end.get().strip())


class TransectRow(ctk.CTkFrame):
    """One transect: name, TC-25 start, TC-25 end, and any pauses inside it."""

    #: Pauses laid out per line before wrapping onto the next. Two fit beside
    #: the transect's own times on a field laptop; a third pushes the status
    #: text off the edge.
    PAUSES_PER_LINE = 2

    def __init__(self, master, on_remove: Callable[[TransectRow], None],
                 name: str = "T1", start: str = "", end: str = "",
                 pauses: list[Pause] | None = None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(6, weight=1)
        self._on_remove = on_remove
        self._pauses: list[PauseCell] = []

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

        # The pauses live in their own frame beside the times, so adding one
        # cannot shuffle the columns the operator's eye has already found.
        # It is taken out of the grid while it is empty: a CustomTkinter frame
        # with nothing in it keeps its default 200x200, which left a gap the
        # height of a transect row under every transect that had no pauses.
        self.pauses_frame = ctk.CTkFrame(self, fg_color="transparent",
                                         width=0, height=0)
        self.pauses_frame.grid(row=0, column=5, rowspan=2, sticky="w",
                               padx=(0, 10))
        self.pauses_frame.grid_remove()

        self.status = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left")
        self.status.grid(row=0, column=6, sticky="ew", padx=(4, 8))

        button(self, "Remove", lambda: on_remove(self), "danger", width=80
               ).grid(row=0, column=7)

        self.add_pause_btn = button(self, "+ Add pause", self.add_pause,
                                    "ghost", width=110)
        self.add_pause_btn.grid(row=1, column=3, columnspan=2, sticky="w",
                                pady=(0, 3))

        # Wire the callbacks only now: every widget refresh() touches exists.
        for e in (self.start, self.end):
            e.set_on_change(self.refresh)
        for p in pauses or []:
            self.add_pause(p.start_tc, p.end_tc)
        self.refresh()

    # ---- pauses --------------------------------------------------------

    def add_pause(self, start: str = "", end: str = "") -> None:
        cell = PauseCell(self.pauses_frame, len(self._pauses) + 1,
                         self._remove_pause, self.refresh, start, end)
        self._pauses.append(cell)
        self._lay_out_pauses()
        self.refresh()

    def _remove_pause(self, cell: PauseCell) -> None:
        if cell not in self._pauses:
            return
        self._pauses.remove(cell)
        cell.destroy()
        self._lay_out_pauses()
        self.refresh()

    def _lay_out_pauses(self) -> None:
        for i, cell in enumerate(self._pauses):
            cell.renumber(i + 1)
            cell.grid(row=i // self.PAUSES_PER_LINE,
                      column=i % self.PAUSES_PER_LINE,
                      padx=(0, 6), pady=2, sticky="w")
        if self._pauses:
            self.pauses_frame.grid()
        else:
            self.pauses_frame.grid_remove()

    # ---- the transect it stands for --------------------------------------

    def to_transect(self) -> Transect:
        return Transect(self.name.get().strip() or "T?",
                        self.start.get().strip(), self.end.get().strip(),
                        pauses=[c.to_pause() for c in self._pauses])

    def refresh(self) -> None:
        t = self.to_transect()
        if not t.start_tc and not t.end_tc:
            self.status.configure(text="", text_color=T.TEXT_MUTED)
            return
        errs = t.validate()
        if errs:
            self.status.configure(text=errs[0].split(": ", 1)[-1], text_color=T.WARN)
            return
        paused = t.paused_s()
        if paused <= 0:
            self.status.configure(text=f"{t.duration_s() / 60.0:.1f} min",
                                  text_color=T.OK)
            return
        self.status.configure(
            text=f"{t.active_s() / 60.0:.1f} min surveying  ·  "
                 f"{paused / 60.0:.1f} min paused",
            text_color=T.OK)


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
                self.add_transect(t)
        else:
            self.project.insert(0, default_project)
            self.date.insert(0, default_date or _date.today().isoformat())
            self.add_transect()

    # ---- transects -----------------------------------------------------

    def add_transect(self, transect: Transect | None = None) -> None:
        """Add a row, either blank (the button) or filled from a saved plan."""
        n = (transect.name if transect else "") or f"T{len(self._rows) + 1}"
        row = TransectRow(
            self.rows_frame, self._remove_row, n,
            transect.start_tc if transect else "",
            transect.end_tc if transect else "",
            pauses=list(transect.pauses) if transect else None)
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
