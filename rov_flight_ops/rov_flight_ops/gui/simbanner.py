"""
The ROV flight simulator's place in the banner: its switch, and the fault line.

Left of Diagnostics, open or folded, so it is never out of sight: a simulator
that could be left on without anyone noticing would be a simulator that one
day gets mistaken for a vehicle. Engaged, the switch and its outline light up
in the simulator's own purple -- not a status colour, so it is never read as
a fault -- and the line under it (beside it, folded) counts the fault clock
down and offers to say what is wrong.

The lamps beside it are left alone: in simulation they show exactly what they
would show on a real flight, which is the point.
"""

from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from . import theme as T

#: The simulator's own colour, as (light, dark): 7.1:1 and 6.3:1 against the
#: banner in each mode.
SIM_COLOR = ("#5B3F8C", "#B9A2E6")
#: The fault line's tones, as (light, dark), each at least 4.5:1 on the banner.
TONES = {"fault": ("#8A5700", "#FAB219"), "green": ("#00795A", "#00C389"),
         "muted": T.TEXT_MUTED}
GLYPHS = {"fault": "▲ ", "green": "● ", "muted": ""}


class SimControls:
    """The switch and the fault line. `pairs()` is what the banner lays out."""

    def __init__(self, app, parent) -> None:
        self.app = app
        self.box = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=8,
                                border_width=2, border_color=T.HEADER_BG,
                                bg_color=T.HEADER_BG)
        self.switch = ctk.CTkSwitch(
            self.box, text="ROV flight simulator", command=self._switched,
            font=T.FONT_SMALL, text_color=T.TEXT, progress_color=SIM_COLOR,
            button_color=T.SURFACE_ALT, bg_color="transparent")
        self.switch.grid(row=0, column=0, padx=(8, 12), pady=3)
        self.line = ctk.CTkFrame(parent, fg_color="transparent", bg_color=T.HEADER_BG)
        self.fault = ctk.CTkLabel(self.line, text="", font=T.FONT_SMALL,
                                  text_color=T.TEXT_MUTED, anchor="w", height=22)
        self.fault.grid(row=0, column=0, sticky="w")
        self.reveal = ctk.CTkButton(
            self.line, text="Show what's wrong", width=128, height=22,
            corner_radius=6, font=T.FONT_SMALL, fg_color="transparent",
            hover_color=T.SURFACE_ALT, text_color=SIM_COLOR, border_width=1,
            border_color=SIM_COLOR, bg_color=T.HEADER_BG, command=self._reveal)
        self.reveal.grid(row=0, column=1, padx=(8, 0))
        self.reveal.grid_remove()
        self._shown = ("", "", "")
        self._checking = False

    def pairs(self) -> list[tuple]:
        return [(self.box, self.line)]

    # ------------------------------------------------------------------

    @property
    def sim(self):
        return self.app.simulator

    def _switched(self) -> None:
        want = bool(self.switch.get())
        if want and not self.sim.active:
            self.switch.deselect()            # on only once it is safe
            if self._checking:
                return
            blockers = self.sim.blockers()
            if blockers:
                messagebox.showinfo(
                    self.app.APP_NAME,
                    "The ROV flight simulator only runs with no vehicle "
                    "connected, so it can never be mistaken for one.\n\nNot "
                    "now: " + "; ".join(blockers) + ".")
                return
            self._begin_check()
        elif not want and self.sim.active:
            self.sim.disengage("switched off")
            self.tick(False)

    def _begin_check(self) -> None:
        """Ask the vehicle's address once, off the window's thread, then start."""
        from ..sim.controller import vehicle_answers

        self._checking = True
        self.switch.configure(text="ROV flight simulator (checking…)",
                              state="disabled")
        host = self.app.version_host()

        def done(answered) -> None:
            self._checking = False
            self.switch.configure(text="ROV flight simulator", state="normal")
            if answered is True:
                messagebox.showinfo(
                    self.app.APP_NAME,
                    f"A vehicle answered at {host}. The ROV flight simulator "
                    f"only runs with no vehicle connected.")
                return
            try:
                self.sim.engage()
            except Exception as ex:
                import sys

                from .. import diagnostics
                diagnostics.log_exception("starting the simulator", *sys.exc_info())
                try:
                    self.sim.disengage("it could not start")
                except Exception:
                    pass
                messagebox.showerror(self.app.APP_NAME,
                                     f"The simulator could not start: {ex}")
                return
            self.switch.select()
            self.tick(False)

        self.app.background("sim-check", lambda: vehicle_answers(host), done)

    def _reveal(self) -> None:
        self.sim.toggle_reveal(self.app.nav.current)
        self.tick(False)

    # ------------------------------------------------------------------

    def tick(self, connected: bool) -> None:
        """Once a second: the switch's light, and the fault line."""
        sim = self.sim
        if sim.active:
            why = sim.tick()
            if why:
                sim.disengage(why)
                self.switch.deselect()
                self._paint(False, connected)
                messagebox.showwarning(
                    self.app.APP_NAME,
                    f"The ROV flight simulator has been switched off: {why}.\n\n"
                    f"Everything is reading the real vehicle again.")
                return
        if bool(self.switch.get()) != sim.active and not self._checking:
            if sim.active:
                self.switch.select()
            else:
                self.switch.deselect()
        self._paint(sim.active, connected)

    def _paint(self, active: bool, connected: bool) -> None:
        self.box.configure(border_color=SIM_COLOR if active else T.HEADER_BG)
        self.switch.configure(text_color=SIM_COLOR if active else
                              (T.TEXT_MUTED if connected else T.TEXT))
        text, tone, button = self.sim.banner(self.app.nav.current) if active \
            else ("", "muted", "")
        shown = (text, tone, button)
        if shown == self._shown:
            return
        self._shown = shown
        self.fault.configure(text=(GLYPHS.get(tone, "") + text) if text else "",
                             text_color=TONES.get(tone, T.TEXT_MUTED))
        if button:
            self.reveal.configure(text=button)
            self.reveal.grid()
        else:
            self.reveal.grid_remove()
