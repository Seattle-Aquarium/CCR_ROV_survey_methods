"""
Two corrections to CustomTkinter, applied once when the window is built.

Both are about the same thing: dragging a window edge used to leave the
interface visibly catching up in small stuttered jumps, a second or more behind
the pointer. Profiling a drag across 320 pixels on the Monitoring tab showed
where the time went, and the two largest entries were not this program's code.

They are kept here, apart from everything else, because they reach into another
library's private methods. That is a thing worth being able to find and delete
in one place the day CustomTkinter fixes it upstream. Everything here checks
that what it is patching still looks the way it expects and does nothing at all
if it does not, so a newer CustomTkinter cannot be broken by it -- only
un-helped.

Measured on the station this was written on, CustomTkinter 6.0.0.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_applied = False


def apply() -> bool:
    """Apply both corrections. True if they went on. Safe to call twice."""
    global _applied
    if _applied:
        return True
    ok = _quieten_scrollbars()
    _applied = True
    return ok


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
