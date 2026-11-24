"""
Corrections to CustomTkinter, applied once when the window is built.

The scrollbar's two are about the same thing: dragging a window edge used to
leave the interface visibly catching up in small stuttered jumps, a second or
more behind the pointer. Profiling a drag across 320 pixels on the Monitoring
tab showed where the time went, and the two largest entries were not this
program's code. The scrollable frame's is about width: a scrolling tab's
content could end up wider than the window it scrolls in, with its right-hand
cards cut off at the edge.

The rest came from profiling the field laptop (a Dell Rugged, 1920 x 1080 at
150 %) in November 2026, with the simulator flying:

* **every `configure` read every option back.** `CTkBaseClass.configure` ends
  by passing what is left of its arguments to the Tk frame underneath -- which
  is nothing at all, nearly always, and `Frame.configure()` with nothing asks
  Tk for *every* option of the frame and builds a dictionary of them. That
  was most of what a label's `configure(text=...)` cost.
* **two polls that never stop.** CustomTkinter checks the display's DPI ten
  times a second and the system's light/dark setting thirty-three times a
  second, for the life of the window. The first only matters when the window
  is dragged to a monitor with a different scaling, the second not at all --
  this program always sets its own mode. Once a second is plenty for both.
* **a layout flush per widget on a theme change.** Every widget ended its
  light/dark repaint with `update_idletasks()`, so switching the appearance
  laid the whole window out once per widget -- several hundred times.
* **the root window's `<Configure>`** is bound by CustomTkinter on the root's
  binding tag, which every widget in the window carries, so it ran -- in
  Python, with Tk's whole event record decoded -- for every widget that
  moved or resized anywhere: over a thousand times for one tab change. See
  `quiet_root_configure`.

They are kept here, apart from everything else, because they reach into another
library's private methods. That is a thing worth being able to find and delete
in one place the day CustomTkinter fixes it upstream. Everything here checks
that what it is patching still looks the way it expects and does nothing at all
if it does not, so a newer CustomTkinter cannot be broken by it -- only
un-helped.

Measured on the station this was written on, CustomTkinter 6.0.0.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

_applied = False

#: How often CustomTkinter's DPI and appearance polls run, in milliseconds,
#: instead of its own 100 and 30.
POLL_MS = 1000


def apply() -> bool:
    """Apply the corrections. True if they all went on. Safe to call twice."""
    global _applied
    if _applied:
        return True
    ok = _quieten_scrollbars()
    ok = _hold_scrollable_width() and ok
    ok = _lean_configure() and ok
    ok = _slow_polls() and ok
    ok = _one_flush_per_theme_change() and ok
    _applied = True
    return ok


def _source(fn) -> str:
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):                      # pragma: no cover
        return ""                                     # a frozen build, say


def _lean_configure() -> bool:
    """Stop `configure` asking Tk for every option of the frame underneath.

    CustomTkinter 6.0.0's ``CTkBaseClass.configure`` hands the Tk frame what
    is left of its keyword arguments -- normally nothing -- and a Tk
    ``configure()`` with nothing reads back the frame's whole configuration.
    This is the same method, with that one call made only when there is
    something to pass. It is replaced only if its source still contains the
    line it replaces, so a CustomTkinter that has changed it is left alone.
    """
    try:
        from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import (
            CTkBaseClass,
        )
        from customtkinter.windows.widgets.utility.utility_functions import (
            check_kwargs_empty,
            pop_from_dict_by_set,
        )
    except Exception:                                 # pragma: no cover
        log.warning("CustomTkinter's base class could not be found; configure "
                    "was not made lean")
        return False
    original = CTkBaseClass.configure
    if getattr(original, "_ccr_tuned", False):
        return True
    src = _source(original)
    expected = ("super().configure(**pop_from_dict_by_set(kwargs, "
                "self._valid_tk_frame_attributes))")
    if expected not in src or "_set_dimensions" not in src:
        log.warning("CustomTkinter's configure has changed shape; it was not "
                    "made lean")
        return False

    import tkinter

    def configure(self, require_redraw=False, **kwargs):
        if "width" in kwargs:
            self._set_dimensions(width=kwargs.pop("width"))
        if "height" in kwargs:
            self._set_dimensions(height=kwargs.pop("height"))
        if "bg_color" in kwargs:
            new_bg_color = self._check_color_type(kwargs.pop("bg_color"),
                                                  transparency=True)
            if new_bg_color == "transparent":
                self._bg_color = self._detect_color_of_master()
            else:
                self._bg_color = self._check_color_type(new_bg_color)
            require_redraw = True
        frame_kw = pop_from_dict_by_set(kwargs, self._valid_tk_frame_attributes)
        if frame_kw:                                  # the whole change
            tkinter.Frame.configure(self, **frame_kw)
        check_kwargs_empty(kwargs, raise_error=True)
        if require_redraw:
            self._draw()

    configure._ccr_tuned = True                       # type: ignore[attr-defined]
    CTkBaseClass.configure = configure                # type: ignore[assignment]
    log.info("CustomTkinter configure made lean")
    return True


def _slow_polls() -> bool:
    """CustomTkinter's DPI check and appearance check, once a second."""
    try:
        from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import (
            AppearanceModeTracker,
        )
        from customtkinter.windows.widgets.scaling.scaling_tracker import (
            ScalingTracker,
        )
    except Exception:                                 # pragma: no cover
        log.warning("CustomTkinter's trackers could not be found; their polls "
                    "were not slowed")
        return False
    ok = True
    for cls in (ScalingTracker, AppearanceModeTracker):
        if isinstance(getattr(cls, "update_loop_interval", None), int):
            cls.update_loop_interval = max(cls.update_loop_interval, POLL_MS)
        else:                                         # pragma: no cover
            ok = False
    return ok


def _one_flush_per_theme_change() -> bool:
    """Repaint every widget for a new appearance, and lay out once, not once
    per widget. The caller (`Shell._toggle_theme`) flushes at the end."""
    try:
        from customtkinter.windows.widgets.appearance_mode import (
            CTkAppearanceModeBaseClass,
        )
        from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import (
            CTkBaseClass,
        )
    except Exception:                                 # pragma: no cover
        return False
    original = CTkBaseClass._set_appearance_mode
    if getattr(original, "_ccr_tuned", False):
        return True
    src = _source(original)
    if "update_idletasks" not in src or "self._draw()" not in src:
        log.warning("CustomTkinter's appearance change has changed shape; it "
                    "was left as it is")
        return False

    def _set_appearance_mode(self, mode_string):
        CTkAppearanceModeBaseClass._set_appearance_mode(self, mode_string)
        self._draw()

    _set_appearance_mode._ccr_tuned = True            # type: ignore[attr-defined]
    CTkBaseClass._set_appearance_mode = _set_appearance_mode  # type: ignore[assignment]
    return True


def quiet_root_configure(root, handler) -> bool:
    """Run `handler()` when the root window itself is configured, and only then.

    CustomTkinter binds ``<Configure>`` on the root window, and in Tk a binding
    on a toplevel is a binding on a tag that every widget inside it carries:
    the handler ran for every label, frame and canvas that changed size or
    place, in Python, after tkinter had decoded all nineteen fields of Tk's
    event record into an object. Opening one tab did it over a thousand times.

    This replaces the root's ``<Configure>`` binding with a line of Tcl that
    compares the window the event is for with the root, and calls into Python
    only when they match. `handler` takes no arguments; it is expected to call
    CustomTkinter's own ``_update_dimensions_event`` among whatever else the
    window does when it is resized.
    """
    try:
        cmd = root.register(lambda *_a: handler())
        path = str(root)
        root.tk.call("bind", path, "<Configure>",
                     f'if {{"%W" eq "{path}"}} {{{cmd}}}')
        return True
    except Exception:                                 # pragma: no cover
        log.warning("the root window's <Configure> could not be narrowed",
                    exc_info=True)
        return False


def _quieten_scrollbars() -> bool:
    """Stop every scrollbar redraw flushing the whole layout.

    ``CTkScrollbar._draw`` ends with ``self._canvas.update_idletasks()``.
    Outside a resize that is merely unnecessary -- the scrollbar is on screen
    by the end of the idle cycle either way. Inside one it is re-entrant: the
    flush runs every pending ``<Configure>`` handler, several of which move a
    scrollbar, which draws, which flushes again. In the profiled drag, 808
    scrollbar redraws accounted for ten of the seventy-six seconds it took --
    the largest single entry in the window, and none of it work anybody asked
    for.

    So the redraw stays and the flush is dropped, by hiding the canvas's own
    ``update_idletasks`` for the duration of the call. Nothing else in the
    program calls it on a scrollbar's canvas, and a scrollbar drawn without it
    is identical -- one idle cycle later, which is the same frame.

    ``set`` gets the other half: it redraws unconditionally, and most of the
    calls during a resize hand it the values it already has.
    """
    try:
        from customtkinter.windows.widgets.ctk_scrollbar import CTkScrollbar
    except Exception:                                 # pragma: no cover
        log.warning("CustomTkinter's scrollbar could not be found; the "
                    "resize tuning was not applied")
        return False

    original_draw = getattr(CTkScrollbar, "_draw", None)
    original_set = getattr(CTkScrollbar, "set", None)
    if original_draw is None or original_set is None:  # pragma: no cover
        log.warning("CustomTkinter's scrollbar has changed shape; the resize "
                    "tuning was not applied")
        return False
    if getattr(original_draw, "_ccr_tuned", False):
        return True

    def _nothing() -> None:
        """What update_idletasks does inside a scrollbar redraw."""

    def draw(self, no_color_updates: bool = False):
        canvas = getattr(self, "_canvas", None)
        if canvas is None or getattr(canvas, "_ccr_quiet", False):
            return original_draw(self, no_color_updates)
        real = canvas.update_idletasks
        canvas.update_idletasks = _nothing
        canvas._ccr_quiet = True
        try:
            return original_draw(self, no_color_updates)
        finally:
            canvas.update_idletasks = real
            canvas._ccr_quiet = False

    def set_(self, start_value: float, end_value: float):
        start, end = float(start_value), float(end_value)
        # A scroll command fires on every <Configure>, and during a resize it
        # is usually reporting the same view it reported last time.
        if (start, end) == (self._start_value, self._end_value):
            return
        self._start_value, self._end_value = start, end
        self._draw()

    draw._ccr_tuned = True                            # type: ignore[attr-defined]
    set_._ccr_tuned = True                            # type: ignore[attr-defined]
    CTkScrollbar._draw = draw                         # type: ignore[assignment]
    CTkScrollbar.set = set_                           # type: ignore[assignment]
    log.info("CustomTkinter scrollbars tuned for resizing")
    return True


def _hold_scrollable_width() -> bool:
    """Keep a scrolling frame's content as wide as the canvas it scrolls in.

    ``CTkScrollableFrame`` gives its content the canvas's width by setting the
    width of the canvas's window item, each time the canvas is configured. Tk
    does not always hold the content to it. On the Lights and BlueOS logs tabs
    the content was measured at its own *requested* width -- 2,649 pixels in a
    2,168-pixel canvas -- while the item still reported 2,168, so the cards on
    the right ran off the edge of the window and were cut off. Setting the
    same width again put it right at once.

    So each scrollable frame checks, whenever its content or its canvas is
    configured or the canvas is mapped, whether the two disagree, and if they
    do sets the width again -- at most once per idle cycle, and never while
    the canvas has no size yet. Watching the content alone is not enough: it
    takes its wide size while the canvas is still 1 pixel, and when the canvas
    then gets its width it is the width Tk fails to pass on, so the content
    never changes and never says so. A horizontal frame is held to the
    canvas's height the same way.
    """
    try:
        from customtkinter.windows.widgets.ctk_scrollable_frame import (
            CTkScrollableFrame,
        )
    except Exception:                                 # pragma: no cover
        log.warning("CustomTkinter's scrollable frame could not be found; "
                    "the width correction was not applied")
        return False

    original_init = CTkScrollableFrame.__init__
    if getattr(original_init, "_ccr_tuned", False):
        return True

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        canvas = getattr(self, "_parent_canvas", None)
        item = getattr(self, "_create_window_id", None)
        if canvas is None or item is None:            # pragma: no cover
            return                                    # a different shape now
        pending = [False]

        def hold() -> None:
            pending[0] = False
            try:
                if getattr(self, "_orientation", "vertical") == "horizontal":
                    want, have, option = (canvas.winfo_height(),
                                          self.winfo_height(), "height")
                else:
                    want, have, option = (canvas.winfo_width(),
                                          self.winfo_width(), "width")
                if want > 1 and have != want:
                    canvas.itemconfigure(item, **{option: want})
            except Exception:
                pass                                  # the window is going away

        def ask(_event=None) -> None:
            if not pending[0]:
                pending[0] = True
                try:
                    self.after_idle(hold)
                except Exception:
                    pending[0] = False

        self.bind("<Configure>", ask, add="+")
        canvas.bind("<Configure>", ask, add="+")
        canvas.bind("<Map>", ask, add="+")
        self._ccr_hold_width = hold

    init._ccr_tuned = True                            # type: ignore[attr-defined]
    CTkScrollableFrame.__init__ = init                # type: ignore[assignment]
    log.info("CustomTkinter scrollable frames held to their canvas width")
    return True
