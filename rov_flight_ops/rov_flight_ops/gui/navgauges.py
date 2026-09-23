"""
The two vertical gauges, drawn on a Tk canvas.

Same reasoning as the Monitoring charts: a handful of canvas items redrawn a
few times a second costs well under a millisecond, where a plotting library
would bring a figure pipeline and a redraw measured in tens. These run for a
whole survey day on a field laptop next to Cockpit.

Both gauges follow the same rules, which come from what an operator can
actually read at arm's length on a Rugged laptop in daylight:

* the **number is the instrument** and the graphic is the context. The number
  is large, high-contrast and always carries its unit;
* nothing is ever drawn at a healthy-looking zero. An invalid or stale reading
  grays the icon, marks the number and says why;
* color never carries meaning alone -- every state that uses color also
  changes a glyph or a label, because a daylight-washed screen and color
  vision deficiency both eat the color first.
"""

from __future__ import annotations

import tkinter

import customtkinter as ctk

from ..nav import power as P
from ..nav.model import NO_VALUE, Quality, Reading
from . import theme as T

# --------------------------------------------------------------------------
#  Altitude
# --------------------------------------------------------------------------

#: At or below this the gauge locks to the fixed survey scale.
SURVEY_ENTER_M = 1.5
#: And only comes back out above this, after `HYSTERESIS_HOLD_S` of it.
SURVEY_EXIT_M = 1.7
HYSTERESIS_HOLD_S = 1.5

#: The altitude these surveys are flown at. A fixed reference mark, and
#: deliberately *not* the Surftrak setpoint -- the vehicle's actual target is
#: whatever it captured on engagement, drawn separately.
SURVEY_REFERENCE_M = 0.8
SURVEY_TOP_M = 1.5

#: Approach mode picks the smallest of these that fits the reading with
#: headroom, so the axis steps rather than creeping. Dense enough that a
#: reading sits in the upper half of its axis rather than halfway down: at
#: 10 m the axis is 12 m and the vehicle draws at 83% of the height, which is
#: what "approaching the bottom from above" should look like.
APPROACH_STOPS = (2.0, 3.0, 5.0, 8.0, 12.0, 16.0, 20.0, 30.0, 40.0, 50.0)
#: Headroom above the reading. 20% keeps the number off the top edge without
#: pushing it down into the middle of the gauge.
APPROACH_HEADROOM = 1.2
#: How long a reading must call for a smaller scale before the axis shrinks.
#: Growing is immediate -- a rising altitude must never be clipped.
RESCALE_DOWN_HOLD_S = 3.0


def survey_fraction(h: float) -> float:
    """Height on the fixed survey scale, 0 at the seabed to 1 at 1.5 m.

    A linear 0–1.5 m axis has its midpoint at 0.75 m, and the altitude these
    surveys are flown at is 0.8. Rather than mislabel the axis or move the
    reference off center, the scale is split at 0.8 and each half gets half
    the height:

        u = 0.5 * h / 0.8                 for 0 <= h <= 0.8
        u = 0.5 + 0.5 * (h - 0.8) / 0.7   for 0.8 < h <= 1.5

    Equal screen distances therefore do **not** represent equal altitude
    increments across the midpoint -- 1 mm below the middle is a smaller
    change than 1 mm above it. That is a real cost, and it is paid knowingly:
    the ticks are labeled with their true values, the two intervals are named
    in the tooltip, and the numeric readout is exact. What is bought is that
    the one altitude that matters sits exactly halfway up, where the eye finds
    it without reading anything.
    """
    if h <= 0.0:
        return 0.0
    if h <= SURVEY_REFERENCE_M:
        return 0.5 * h / SURVEY_REFERENCE_M
    if h >= SURVEY_TOP_M:
        return 1.0
    return 0.5 + 0.5 * (h - SURVEY_REFERENCE_M) / (SURVEY_TOP_M - SURVEY_REFERENCE_M)


def approach_top(value: float, current: float = 0.0) -> float:
    """The axis top for an approach reading: the smallest stop that fits."""
    want = max(value * APPROACH_HEADROOM, value + 0.4)
    for stop in APPROACH_STOPS:
        if stop >= want:
            return stop
    return APPROACH_STOPS[-1]


class AltitudeGauge(ctk.CTkFrame):
    """Height above the seabed: a moving ROV on a scale that changes mode.

    Two modes, chosen by the reading itself and held across the boundary by
    hysteresis so a vehicle hovering at 1.5 m does not make the axis flicker:

    **Approach** for anything above the threshold. A labeled linear scale
    with sensible stops and headroom, so ten meters reads near the top and
    zero is on screen.

    **Survey** at or below 1.5 m. The ticks stop moving entirely -- a fixed
    0–1.5 m axis with 0.8 m at the exact vertical center -- and only the
    vehicle moves. That is the mode the whole dive is flown in, and an axis
    that rescales underneath a pilot holding 0.8 m is worse than useless.
    """

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tkinter.Canvas(self, highlightthickness=0, bd=0,
                                     bg=_hex(T.SURFACE))
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self._mode = "approach"
        self._top = 10.0
        self._want_smaller_since: float | None = None
        self._above_exit_since: float | None = None
        self.reading: Reading | None = None
        self.target: Reading | None = None
        self._last_good: float | None = None

    # -- state ------------------------------------------------------------

    def update_reading(self, altitude: Reading, target: Reading | None = None,
                       *, now: float = 0.0) -> None:
        """Take a new altitude, and decide what the axis should be.

        An invalid or stale reading **cannot move the axis**. A rescale is a
        statement about where the vehicle is, and a reading that does not know
        where the vehicle is has no business making one.
        """
        self.reading = altitude
        self.target = target
        v = altitude.number()
        if v is None:
            self.draw()
            return
        self._last_good = v

        if self._mode == "approach":
            if v <= SURVEY_ENTER_M:
                self._mode = "survey"
                self._above_exit_since = None
            else:
                self._choose_top(v, now)
        else:
            if v > SURVEY_EXIT_M:
                if self._above_exit_since is None:
                    self._above_exit_since = now
                elif now - self._above_exit_since >= HYSTERESIS_HOLD_S:
                    self._mode = "approach"
                    self._top = approach_top(v)
                    self._above_exit_since = None
            else:
                self._above_exit_since = None
        self.draw()

    def _choose_top(self, v: float, now: float) -> None:
        want = approach_top(v)
        if want > self._top:
            # Growing is immediate: a climbing vehicle must never be clipped.
            self._top = want
            self._want_smaller_since = None
        elif want < self._top:
            # Shrinking waits, so noise around a stop does not pump the axis.
            if self._want_smaller_since is None:
                self._want_smaller_since = now
            elif now - self._want_smaller_since >= RESCALE_DOWN_HOLD_S:
                self._top = want
                self._want_smaller_since = None
        else:
            self._want_smaller_since = None

    @property
    def mode(self) -> str:
        return self._mode

    def fraction(self, h: float) -> float:
        """Where `h` sits on the current axis, 0 at the bottom to 1 at the top."""
        if self._mode == "survey":
            return survey_fraction(h)
        return max(0.0, min(1.0, h / self._top)) if self._top else 0.0

    # -- drawing ------------------------------------------------------------

    def draw(self) -> None:
        c = self.canvas
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except tkinter.TclError:
            return
        if w < 10 or h < 10:
            return
        c.delete("all")
        c.configure(bg=_hex(T.SURFACE))

        # The gauge column is narrow -- a HUD is a quarter of the page and
        # the gauge is a third of that, so about 180 px at 1920. The axis
        # therefore sits well left of center, tick values go on its left and
        # markers on its right, and nothing is written twice. An earlier
        # version put "0.8 survey" beside a tick already labeled 0.8 and the
        # two collided with the Surftrak target at survey altitude, which is
        # exactly when the gauge has to be readable.
        pad_top, pad_bot = 20, 40
        axis_x = int(w * 0.40)
        top_y, bot_y = pad_top, h - pad_bot
        span = max(1, bot_y - top_y)

        def y_of(frac: float) -> float:
            return bot_y - frac * span

        r = self.reading
        usable = r is not None and r.number() is not None
        ink = _hex(T.TEXT) if usable else _hex(T.TEXT_MUTED)
        muted = _hex(T.TEXT_MUTED)

        # The water column, and the seabed at zero.
        c.create_rectangle(axis_x - 9, top_y, axis_x + 9, bot_y,
                           fill=_hex(T.FIELD_BG), outline=_hex(T.BORDER))
        c.create_line(axis_x - 20, bot_y, w - 4, bot_y, fill=_hex(T.WARN),
                      width=3)
        c.create_text(axis_x + 14, bot_y - 7, text="seabed", anchor="w",
                      fill=_hex(T.WARN), font=T.FONT_SMALL)

        # Ticks.
        if self._mode == "survey":
            ticks = [(0.25, ""), (SURVEY_REFERENCE_M, "survey"), (1.0, ""),
                     (1.25, ""), (SURVEY_TOP_M, "")]
            c.create_text(4, 10, text="SURVEY  0–1.5 m  (split at 0.8)",
                          anchor="w", fill=muted, font=T.FONT_SMALL)
        else:
            step = self._top / 5.0
            ticks = [(step * i, "") for i in range(1, 6)]
            c.create_text(4, 10, text=f"APPROACH  0–{self._top:g} m",
                          anchor="w", fill=muted, font=T.FONT_SMALL)

        for value, tag in ticks:
            y = y_of(self.fraction(value))
            is_ref = tag == "survey"
            c.create_line(axis_x - 15, y, axis_x + 15 if is_ref else axis_x + 9,
                          y, fill=_hex(T.ACCENT) if is_ref else _hex(T.BORDER),
                          width=2 if is_ref else 1)
            c.create_text(axis_x - 18, y, text=f"{value:g}", anchor="e",
                          fill=_hex(T.ACCENT) if is_ref else muted,
                          font=T.FONT_SMALL)
            if is_ref:
                # The tick already reads 0.8; the diamond says which one it is
                # without repeating the number into the target's space.
                c.create_text(axis_x + 13, y, text="◆", anchor="w",
                              fill=_hex(T.ACCENT), font=T.FONT_SMALL)

        # The Surftrak target, when there genuinely is one. Visually distinct
        # from the fixed 0.8 reference, because they are different things and
        # confusing them would have a pilot fly to a number the vehicle is not
        # holding.
        tv = self.target.number() if self.target is not None else None
        if tv is not None and tv > 0:
            if tv > (SURVEY_TOP_M if self._mode == "survey" else self._top):
                c.create_text(w - 6, top_y + 12,
                              text=f"▲ target {tv:.2f} m (above scale)",
                              anchor="e", fill=_hex(T.OK), font=T.FONT_SMALL)
            else:
                ty = y_of(self.fraction(tv))
                c.create_line(axis_x - 16, ty, axis_x + 16, ty,
                              fill=_hex(T.OK), width=2, dash=(5, 3))
                c.create_text(axis_x + 26, ty, text=f"▲{tv:.2f}", anchor="w",
                              fill=_hex(T.OK), font=T.FONT_SMALL)

        # The vehicle.
        v = r.number() if r is not None else None
        shown = v if v is not None else (r.held() if r is not None else None)
        if shown is not None:
            above = shown > (SURVEY_TOP_M if self._mode == "survey" else self._top)
            frac = self.fraction(shown)
            y = y_of(frac)
            color = (_hex(T.ACCENT) if usable else _hex(T.TEXT_MUTED))
            if above:
                # Never plot a false in-range position: an arrow at the top
                # and the true number beside it.
                c.create_polygon(axis_x, top_y - 6, axis_x - 8, top_y + 6,
                                 axis_x + 8, top_y + 6, fill=color,
                                 outline="")
                c.create_text(axis_x + 14, top_y + 4,
                              text=f"▲ {shown:.2f} m above scale", anchor="w",
                              fill=color, font=T.FONT_SMALL)
            else:
                c.create_oval(axis_x - 13, y - 7, axis_x + 13, y + 7,
                              fill=color, outline="")
                c.create_line(axis_x - 13, y, axis_x - 26, y, fill=color,
                              width=2)
                if not usable:
                    c.create_text(axis_x + 30, y, text="✕", anchor="w",
                                  fill=_hex(T.WARN), font=T.FONT_SMALL)

        # The number.
        big = T.scale_font(T.FONT_TITLE, 1.4)
        if usable:
            text = f"{v:.2f}"
            sub = "m above bottom"
            color = ink
        elif r is not None and r.quality is Quality.STALE and shown is not None:
            text, sub, color = f"{shown:.2f}", "STALE · " + (r.note or ""), _hex(T.WARN)
        else:
            text = NO_VALUE
            sub = (r.note if r is not None and r.note else "no bottom range")
            color = _hex(T.WARN)
        c.create_text(4, h - 34, text=text, anchor="w", fill=color, font=big)
        c.create_text(4, h - 11, text=sub[:40], anchor="w", fill=muted,
                      font=T.FONT_SMALL)

    def refresh_theme(self) -> None:
        self.draw()


# --------------------------------------------------------------------------
#  Power
# --------------------------------------------------------------------------


class PowerGauge(ctk.CTkFrame):
    """Busbar watts on a fixed 0–1,000 W scale.

    **It never rescales.** An operator learns where 700 W sits on this dial,
    and a gauge that re-ranges itself destroys that at exactly the moment it
    matters. Over-range keeps the true number and adds a mark; it does not
    move the axis.

    The 900 W band is red *and* hatched *and* labeled, because on a sunlit
    screen the color is the first thing to go.
    """

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tkinter.Canvas(self, highlightthickness=0, bd=0,
                                     bg=_hex(T.SURFACE))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.reading: Reading | None = None
        self.peak: Reading | None = None

    def update_reading(self, watts: Reading, peak: Reading | None = None) -> None:
        self.reading = watts
        self.peak = peak
        self.draw()

    def draw(self) -> None:
        c = self.canvas
        try:
            w, h = c.winfo_width(), c.winfo_height()
        except tkinter.TclError:
            return
        if w < 10 or h < 10:
            return
        c.delete("all")
        c.configure(bg=_hex(T.SURFACE))

        pad_top, pad_bot = 20, 40
        axis_x = int(w * 0.46)
        top_y, bot_y = pad_top, h - pad_bot
        span = max(1, bot_y - top_y)
        y_of = lambda frac: bot_y - frac * span       # noqa: E731

        muted = _hex(T.TEXT_MUTED)
        c.create_text(4, 10, text="0–1,000 W  fixed", anchor="w", fill=muted,
                      font=T.FONT_SMALL)

        c.create_rectangle(axis_x - 9, top_y, axis_x + 9, bot_y,
                           fill=_hex(T.FIELD_BG), outline=_hex(T.BORDER))

        # The high-load band: filled, hatched and labeled.
        band_y = y_of(P.HIGH_LOAD_W / P.GAUGE_MAX_W)
        c.create_rectangle(axis_x - 9, top_y, axis_x + 9, band_y,
                           fill=_hex(T.ERROR), outline="", stipple="gray50")
        c.create_line(axis_x - 16, band_y, axis_x + 16, band_y,
                      fill=_hex(T.ERROR), width=2)
        c.create_text(axis_x + 13, band_y - 8, text="⚠900", anchor="w",
                      fill=_hex(T.ERROR), font=T.FONT_SMALL)

        for wv in (200, 400, 600, 800, 1000):
            y = y_of(wv / P.GAUGE_MAX_W)
            c.create_line(axis_x - 13, y, axis_x + 9, y, fill=_hex(T.BORDER))
            c.create_text(axis_x - 16, y, text=f"{wv:,}", anchor="e",
                          fill=muted, font=T.FONT_SMALL)

        r = self.reading
        v = r.number() if r is not None else None
        shown = v if v is not None else (r.held() if r is not None else None)
        usable = v is not None

        # The observed peak, as a chevron. Kept visually separate from the
        # instantaneous marker and from the threshold.
        pk = self.peak.number() if self.peak is not None else None
        if pk and pk > 0:
            py = y_of(min(1.0, pk / P.GAUGE_MAX_W))
            c.create_polygon(axis_x - 20, py, axis_x - 12, py - 5,
                             axis_x - 12, py + 5, fill=_hex(T.HEADING),
                             outline="")
            c.create_text(axis_x + 13, py + 8, text=f"pk {pk:,.0f}", anchor="w",
                          fill=_hex(T.HEADING), font=T.FONT_SMALL)

        if shown is not None:
            over = P.over_range(shown)
            frac = P.gauge_fraction(shown)
            y = y_of(frac)
            hot = P.high_load(shown)
            color = (_hex(T.ERROR) if hot else
                      _hex(T.ACCENT) if usable else _hex(T.TEXT_MUTED))
            c.create_oval(axis_x - 13, y - 7, axis_x + 13, y + 7, fill=color,
                          outline="")
            if over:
                c.create_text(axis_x, top_y - 12, text="▲ OVER", anchor="c",
                              fill=_hex(T.ERROR), font=T.FONT_SMALL)

        big = T.scale_font(T.FONT_TITLE, 1.4)
        if usable:
            hot = P.high_load(v)
            text = f"{v:,.0f}"
            sub = "HIGH LOAD — OTPS 1,000 W" if hot else "W  busbar"
            color = _hex(T.ERROR) if hot else _hex(T.TEXT)
        elif r is not None and r.quality is Quality.STALE and shown is not None:
            text, sub = f"{shown:,.0f}", "STALE · " + (r.note or "")
            color = _hex(T.WARN)
        else:
            text = NO_VALUE
            sub = (r.note if r is not None and r.note else "no power reading")
            color = _hex(T.WARN)
        c.create_text(4, h - 34, text=text, anchor="w", fill=color, font=big)
        c.create_text(4, h - 11, text=sub[:40], anchor="w", fill=muted,
                      font=T.FONT_SMALL)

    def refresh_theme(self) -> None:
        self.draw()


def _hex(color) -> str:
    """A theme color as a plain string a Tk canvas will take.

    `theme` stores every color as a (light, dark) pair for CustomTkinter,
    which resolves them itself. A raw Tk canvas does not, so the current mode's
    half has to be picked here.
    """
    if isinstance(color, (tuple, list)):
        mode = ctk.get_appearance_mode()
        return color[1] if str(mode).lower() == "dark" else color[0]
    return color
