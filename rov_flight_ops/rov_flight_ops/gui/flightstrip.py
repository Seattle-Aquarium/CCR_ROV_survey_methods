"""
The flight numbers under the Navigation map: big enough to read at a glance.

Three groups on one row. Left, the flying: altitude above the bottom with
Surftrak's setpoint under it, depth, speed along the heading, and the pilot's
thruster gain. Middle, the time the transects are written down in: TC-25,
exactly as the GoPro Labs precision-time page shows it (`tc25`). Right, the
power: volts, amps and watts at the busbar. Every cell has the same gap to
its neighbours, so the two groups are spaced alike, and the time sits
centred between them.

Each number is the collector's own reading, so a stale value is drawn muted
and a missing one as a dash -- never a zero.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import board as B
from .. import tc25 as TC
from ..nav.model import Quality
from . import theme as T

FONT_VALUE = (T.MONO, 20, "bold")
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


class FlightNumbers(ctk.CTkFrame):

    def __init__(self, master, *, clock: TC.ClockCheck | None = None, **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.clock = clock or TC.ClockCheck()
        self.cells: dict[str, tuple] = {}
        # flying | gap | time | gap | power, in one grid.
        layout = [*(k for k, _h in FLYING), None, "tc25", None, *(k for k, _h in POWER)]
        heads = dict(FLYING + POWER + (("tc25", "TC-25"),))
        for col, key in enumerate(layout):
            if key is None:
                self.grid_columnconfigure(col, weight=1)
                # A thin rule in the middle of each gap. height=1: a
                # childless CTkFrame asks for 200 pixels, which took the
                # height from the map.
                ctk.CTkFrame(self, width=1, height=1, fg_color=T.BORDER).grid(
                    row=0, column=col, sticky="ns", pady=8)
                continue
            cell = ctk.CTkFrame(self, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="nsew", padx=CELL_PAD, pady=4)
            cell.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(cell, text=heads[key], font=FONT_HEAD, height=14,
                         text_color=T.TEXT_MUTED).grid(row=0, column=0)
            value = ctk.CTkLabel(cell, text="—", font=FONT_VALUE, height=28,
                                 text_color=T.TEXT_MUTED)
            value.grid(row=1, column=0)
            sub = ctk.CTkLabel(cell, text="", font=T.FONT_SMALL, height=14,
                               text_color=T.TEXT_MUTED)
            sub.grid(row=2, column=0)
            self.cells[key] = (value, sub)
        self.after(200, self._tick_clock)

    # -- the time, every frame, while on screen ------------------------------

    def _tick_clock(self) -> None:
        try:
            if self.winfo_ismapped():
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
