"""
The flight numbers on the Navigation tab: big enough to read at a glance.

The pilot's heads-up display, at the top of the right-hand column. Two rows:

* the flying -- altitude above the bottom with Surftrak's setpoint under it,
  depth, speed along the heading, and the pilot's thruster gain;
* the time the transects are written down in -- TC-25, exactly as the GoPro
  Labs precision-time page shows it (`tc25`) -- beside the power at the
  busbar: volts, amps and watts.

`stacked=False` lays the same cells out in one row of three groups, as they
were when they sat under the map.

Each number is the collector's own reading, so a stale value is drawn muted
and a missing one as a dash -- never a zero.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import board as B
from .. import tc25 as TC
from ..nav.model import Quality
from . import theme as T
from .widgets import Label, shown

FONT_VALUE = (T.MONO, 20, "bold")
#: The flying numbers, a size up when they have a row of their own.
FONT_VALUE_BIG = (T.MONO, 22, "bold")
#: The gap either side of each cell, so both groups are spaced alike.
CELL_PAD = 9
FONT_HEAD = (T.FAMILY_MEDIUM, 10)
#: How often the time is redrawn: once a frame at 25 frames a second.
CLOCK_MS = 40


def _set(widget, **kw) -> None:
    cache = getattr(widget, "_cache", None)
    if cache is None:
        cache = widget._cache = {}
    changes = {k: v for k, v in kw.items() if cache.get(k) != v}
    if changes:
        cache.update(changes)
        widget.configure(**changes)


def show(reading, fmt: str, scale: float = 1.0) -> tuple[str, tuple]:
    """(text, colour) for one reading: live, stale (muted), or a dash."""
    if reading is None or not reading.quality.has_value:
        return "—", T.TEXT_MUTED
    v = reading.held()
    if v is None:
        return "—", T.TEXT_MUTED
    text = fmt.format(v * scale)
    return text, (T.TEXT if reading.quality is Quality.OK else T.TEXT_MUTED)


#: (key, heading) for each group.
FLYING = (("altitude", "ALTITUDE"), ("depth", "DEPTH"),
          ("forward", "FORWARD"), ("gain", "GAIN"))
POWER = (("volts", "VOLTS"), ("amps", "AMPS"), ("watts", "WATTS"))
HEADS = dict(FLYING + POWER + (("tc25", "TC-25"),))


class FlightNumbers(ctk.CTkFrame):

    def __init__(self, master, *, clock: TC.ClockCheck | None = None,
                 stacked: bool = False, **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.clock = clock or TC.ClockCheck()
        self.stacked = stacked
        self.cells: dict[str, tuple] = {}
        if stacked:
            self._build_stacked()
        else:
            self._build_row()
        self.after(200, self._tick_clock)

    # -- layout ----------------------------------------------------------------

    def _cell(self, parent, key: str, column: int, font=FONT_VALUE,
              row: int = 0, pad: int = CELL_PAD) -> None:
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=column, sticky="nsew", padx=pad, pady=4)
        cell.grid_columnconfigure(0, weight=1)
        Label(cell, text=HEADS[key], font=FONT_HEAD, height=14,
              text_color=T.TEXT_MUTED).grid(row=0, column=0)
        value = Label(cell, text="—", font=font, height=30 if font is FONT_VALUE_BIG
                      else 28, text_color=T.TEXT_MUTED)
        value.grid(row=1, column=0)
        sub = Label(cell, text="", font=T.FONT_SMALL, height=14,
                    text_color=T.TEXT_MUTED)
        sub.grid(row=2, column=0)
        self.cells[key] = (value, sub)

    def _rule(self, parent, column: int, row: int = 0) -> None:
        # height=1: a childless CTkFrame asks for 200 pixels.
        ctk.CTkFrame(parent, width=1, height=1, fg_color=T.BORDER).grid(
            row=row, column=column, sticky="ns", pady=8)

    def _build_row(self) -> None:
        """Flying | gap | time | gap | power, in one grid."""
        layout = [*(k for k, _h in FLYING), None, "tc25", None, *(k for k, _h in POWER)]
        for col, key in enumerate(layout):
            if key is None:
                self.grid_columnconfigure(col, weight=1)
                self._rule(self, col)
                continue
            self._cell(self, key, col)

    def _build_stacked(self) -> None:
        """Two rows, each its own grid so each can space its own cells.

        The four flying numbers share the width evenly. Underneath, TC-25 --
        eleven characters -- takes two parts of the width to each power
        number's one, so the time is never the cell that is cut short.
        """
        self.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 0))
        for i, (key, _h) in enumerate(FLYING):
            top.grid_columnconfigure(i, weight=1, uniform="flying")
            self._cell(top, key, i, font=FONT_VALUE_BIG, pad=4)
        ctk.CTkFrame(self, height=1, fg_color=T.BORDER).grid(
            row=1, column=0, sticky="ew", padx=10)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 2))
        bottom.grid_columnconfigure(0, weight=2, uniform="power")
        self._cell(bottom, "tc25", 0, pad=4)
        self._rule(bottom, 1)
        for i, (key, _h) in enumerate(POWER, start=2):
            bottom.grid_columnconfigure(i, weight=1, uniform="power")
            self._cell(bottom, key, i, pad=4)

    # -- the time, every frame, while on screen ------------------------------

    def _tick_clock(self) -> None:
        try:
            if shown(self):
                value, sub = self.cells["tc25"]
                _set(value, text=TC.tc25(), text_color=T.TEXT)
                self.clock.poll()
                off = self.clock.off_by()
                _set(sub, text=self.clock.words(),
                     text_color=(T.WARN if off is not None and abs(off) >= 1.0
                                 else T.TEXT_MUTED))
                self.after(CLOCK_MS, self._tick_clock)
                return
        except Exception:
            pass
        # Off screen: look again in a while, at no cost meanwhile.
        self.after(500, self._tick_clock)

    # -- the readings, from the collector ------------------------------------

    def update_from(self, s) -> None:
        if s is None:
            for key, (value, sub) in self.cells.items():
                if key != "tc25":
                    _set(value, text="—", text_color=T.TEXT_MUTED)
                    _set(sub, text="")
            return
        target, _c = show(s.surftrak_target, "{:.2f} m")
        surftrak = f"Surftrak {target}" if target != "—" else "Surftrak not set"
        altitude = show(s.altitude, "{:.2f} m")
        if s.altitude.quality is Quality.INVALID:
            # Lost lock is a state worth seeing, not a blank.
            altitude = ("no lock", B.COLORS[B.CHECK])
        values = {
            "altitude": (altitude, surftrak),
            "depth": (show(s.depth, "{:.1f} m", -1.0), ""),
            "forward": (show(s.forward, "{:+.2f} m/s"), "along heading"),
            "gain": (show(s.pilot_gain, "{:.0f} %", 100.0), "pilot gain"),
            "volts": (show(s.voltage, "{:.1f}"), "V"),
            "amps": (show(s.current, "{:.1f}"), "A"),
            "watts": (show(s.watts, "{:.0f}"), "W"),
        }
        for key, ((text, color), sub_text) in values.items():
            value, sub = self.cells[key]
            _set(value, text=text, text_color=color)
            _set(sub, text=sub_text)
