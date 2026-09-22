"""
The window: a banner that folds away, a row of tabs, and one shared output pane.

Both ROV programs are built on this. It replaces UTC's chapter rail: with one
program per job there are no chapters left to choose between, so the tabs that
used to sit inside a chapter are the navigation, and the width the rail took
goes back to the work.

Three things about it are deliberate.

**The banner folds, the rule does not.** The logo, title and attribution are
worth having and cost a tenth of a field laptop's height. Folding them leaves
the gradient rule, the appearance switch and the fold button exactly as they
were, risen to the top of the window -- so the controls that bring it back
never move out from under the pointer's memory of where they were.

**Output starts small and is dragged open.** The shared log pane at the foot
and every report box on every tab open one line tall. Most of the time a line
is all they have to say; when it is not, the handle on the pane's top edge (or
the section's bottom edge) opens them. Progress and status are never inside
the part that shrinks.

**One worker.** Every tab starts its jobs through `submit`, and a job reports
through a queue the Tk loop drains -- Tk is not thread-safe, so no worker ever
touches a widget. Each job has its own id, its own Stop signal and its own
callback, and stays the running job until its result has been handed over --
not merely until its thread ends -- so one job's result can never reach the
next job's callback. The drain survives a callback that raises, hands the
loop back after a bounded slice, and keeps only the newest progress update
from each batch.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter
import traceback
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import diagnostics
from . import ctk_tuning
from . import gradients as G
from . import theme as T
from .widgets import Grip, OutputBox, Repaint, button, one_line_height

log = logging.getLogger(__name__)
#: The shared output pane, copied into the diagnostics log.
output_log = logging.getLogger(diagnostics.PACKAGE + ".output")

#: A drain pass hands the event loop back after this many messages or this
#: long, whichever comes first, so a flood of progress cannot starve input.
DRAIN_EVERY_MS = 80
DRAIN_MAX_MESSAGES = 200
DRAIN_MAX_S = 0.05

#: Lines the shared output pane keeps. Older lines are dropped from the
#: screen in one block; the diagnostics log has the whole run.
LOG_MAX_LINES = 2000
LOG_TRIM_LINES = 500

#: The longest line, and the longest message, the output pane is given. Tk's
#: text widget lays a line out whole, and a single line of a few megabytes
#: takes it tens of minutes -- which is how the flight report froze the
#: window on 14 September 2026: its result was printed as one 4 MB line.
LOG_LINE_CHARS = 2000
LOG_MESSAGE_CHARS = 40_000
#: A job result without a summary() is printed only if its text is this
#: short; a larger one is for the page that asked for it, not the log.
RESULT_TEXT_CHARS = 500


def _bounded(text: str) -> str:
    """`text` cut to what the output pane can lay out without stalling."""
    if len(text) > LOG_MESSAGE_CHARS:
        text = (text[:LOG_MESSAGE_CHARS]
                + f"\n… {len(text) - LOG_MESSAGE_CHARS:,} more characters not shown")
    if len(text) <= LOG_LINE_CHARS:
        return text
    return "\n".join(
        line if len(line) <= LOG_LINE_CHARS else
        f"{line[:LOG_LINE_CHARS]}… ({len(line) - LOG_LINE_CHARS:,} more characters)"
        for line in text.split("\n"))


def _brief(result, job=None) -> str:
    """A job result that has no summary(), as text fit for the output pane.

    Short values -- a message, a path -- are printed as they always were. A
    large object is not: its page shows it, and printing its repr is what
    froze the window.
    """
    text = str(result)
    if len(text) <= RESULT_TEXT_CHARS:
        return text
    log.info("job %s: its %s result (%s characters) was not printed",
             getattr(job, "id", "?"), type(result).__name__, f"{len(text):,}")
    return ""


class JobStopped(BaseException):  # noqa: N818 - it is not an error
    """Raised inside a job's work to leave it at a checkpoint after Stop.

    A BaseException, deliberately: code the job calls into -- the transect
    extractor, for one -- recovers from an ``except Exception`` around each
    file, and a Stop must not be mistaken for a damaged file and skipped past.
    """


class _Job:
    """One submitted job: its id, its Stop signal and who wants its result."""

    def __init__(self, job_id: int, label: str, on_done) -> None:
        self.id = job_id
        self.label = label
        self.on_done = on_done
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = time.monotonic()


def _font_kw(font: tuple) -> dict:
    """A Tk font tuple as keyword arguments for tkinter.font.Font."""
    kw = {"family": font[0], "size": font[1]}
    if len(font) > 2 and "bold" in font[2:]:
        kw["weight"] = "bold"
    return kw


#: Who made it. "Seattle Aquarium" is deliberately absent -- the logo beside it
#: already says it.
ATTRIBUTION_LINES = ("Conservation Programs and Partnerships",
                     "Coastal Climate Resilience")

#: What is shown for a version nobody has been able to read yet.
UNKNOWN_VERSION = "—"

#: The three versions the banner carries, and what to call each. They are
#: what a question six months from now turns on -- "why does August look
#: different?" is a lookup rather than an argument only if the answer was on
#: screen at the time.
VERSION_KEYS = ("blueos", "ardusub", "cockpit")
VERSION_LABELS = {"blueos": "BlueOS", "ardusub": "ArduSub",
                  "cockpit": "Cockpit"}

#: Gradient rules kept as Tk images, by width. One drag across a screen passes
#: through a few dozen widths and comes back through the same ones; past that
#: the oldest goes.
RULE_CACHE_MAX = 48

#: How often the vehicle is asked for them. They change when somebody updates
#: the vehicle, which is not during a dive, so a minute is plenty -- and this
#: runs for a whole survey day.
VERSION_POLL_S = 60.0
#: And how often the lamps are refreshed. Fast enough that losing the tether
#: shows up as quickly as the recorder itself notices.
STATUS_POLL_MS = 1000


class Lamp(ctk.CTkFrame):
    """A small indicator: a dot, and the word for what it is watching.

    Three states rather than two, because there genuinely are three and
    flattening them would be a lie at exactly the moment it mattered. The
    recorder watches for the ROV to arm and only writes rows once it has, so
    between two transects the monitoring is up and the logging is not. A ring
    says "on and waiting", a filled dot says "happening now", and grey says
    neither -- which reads at a glance and survives being colour-blind, a
    laptop in daylight, and a screenshot in a report.
    """

    #: dot, colour attribute on the theme.
    OFF = ("○", "TEXT_MUTED")
    WAITING = ("○", "ACCENT")
    ON = ("●", "OK")

    def __init__(self, master, text: str, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._state = self.OFF
        self.dot = ctk.CTkLabel(self, text=self.OFF[0], width=14,
                                font=("Segoe UI Symbol", 14),
                                text_color=T.TEXT_MUTED)
        self.dot.grid(row=0, column=0, padx=(0, 4))
        self.caption = ctk.CTkLabel(self, text=text, font=T.FONT_SMALL,
                                    text_color=T.TEXT_MUTED, anchor="w")
        self.caption.grid(row=0, column=1, sticky="w")

    def set_state(self, state: tuple[str, str]) -> None:
        self._state = state
        self.refresh_theme()

    def refresh_theme(self) -> None:
        glyph, colour = self._state
        try:
            self.dot.configure(text=glyph, text_color=getattr(T, colour))
            self.caption.configure(
                text_color=T.TEXT if self._state is not self.OFF
                else T.TEXT_MUTED)
        except Exception:
            pass                       # the window is closing

#: The log pane may be dragged open to at most this share of the window.
LOG_MAX_SHARE = 0.6


class TabStrip(ctk.CTkFrame):
    """The program's tabs, left to right, the open one outlined.

    Keeps the small navigation API the pages already use -- ``select``,
    ``sections``, ``current``, ``set_enabled`` and ``set_locked`` -- so a page
    written against UTC's rail works unchanged on a tab.

    **Only the open tab is laid out.** The five pages share one grid cell, and
    the first version simply stacked them and raised the open one. They were
    all still *managed*, so every one of them was measured and re-laid-out
    every time the window changed width -- five tabs' worth of work to show
    one. Measured on the station this was written on, that was 120 ms per
    resize step where laying out the open tab alone is 45: it is where most of
    the stutter came from when a window edge was dragged. The four that are
    not being looked at are now taken out of the grid entirely and put back
    when they are chosen, which costs one layout pass on a tab change -- an
    action that happens a handful of times a day, against a resize that
    happens sixty times a second.
    """

    def __init__(self, master, holder, on_select: Callable[[str], None]):
        super().__init__(master, fg_color="transparent")
        self._holder = holder
        self._on_select = on_select
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._current: str | None = None
        #: The tab to come back to when the start-up warm-up has been round
        #: them all; None when it is not running.
        self._warm_home: str | None = None
        #: A stable identifier per tab, independent of its position and of
        #: its displayed name.
        #:
        #: Nothing persists a chosen tab today -- the window always opens on
        #: the first one -- and this exists so that if anything ever does, it
        #: cannot persist a *number*. Navigation was inserted at position 2 in
        #: September 2026 and pushed Transects to 3; a saved "tab 2" from
        #: before that would now reopen Navigation, which is the kind of
        #: silent migration bug that is only ever noticed by the person it
        #: confuses. Save `key_of(name)`, restore with `select_key`.
        self._keys: dict[str, str] = {}

    def add(self, name: str, key: str = "") -> ctk.CTkFrame:
        n = len(self._buttons) + 1
        btn = ctk.CTkButton(
            self, text=f"{n}  {name}", font=T.FONT_TAB, height=36,
            corner_radius=8, fg_color="transparent", hover_color=T.SURFACE,
            text_color=T.TEXT_MUTED, border_width=0, border_color=T.ACCENT,
            command=lambda: self.select(name))
        btn.grid(row=0, column=len(self._buttons), padx=(0, 6), sticky="w")
        self._buttons[name] = btn

        page = ctk.CTkFrame(self._holder, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        # Gridded and then removed, which keeps the options for when it is
        # chosen. Until then Tk has nothing to lay out for it.
        page.grid_remove()
        self._pages[name] = page
        self._keys[name] = key or name.lower().replace(" ", "_")
        if self._current is None:
            self.select(name, notify=False)
        return page

    def key_of(self, name: str) -> str:
        """The stable identifier for a tab. Save this, never the position."""
        return self._keys.get(name, "")

    def select_key(self, key: str) -> bool:
        """Open the tab with this identifier. False if it no longer exists.

        A key that has gone is not an error -- a chapter can be removed
        between releases -- and the caller falls back to the first tab rather
        than opening whatever now happens to sit where the old one did.
        """
        for name, k in self._keys.items():
            if k == key:
                self.select(name)
                return True
        return False

    def select(self, name: str, notify: bool = True) -> None:
        if name not in self._pages:
            return
        previous = self._current
        self._current = name
        if previous is not None and previous != name:
            page = self._pages.get(previous)
            if page is not None:
                page.grid_remove()
        self._pages[name].grid()
        for n, btn in self._buttons.items():
            on = n == name
            btn.configure(text_color=T.HEADING if on else T.TEXT_MUTED,
                          font=T.FONT_TAB_ON if on else T.FONT_TAB,
                          border_width=2 if on else 0)
        if notify:
            self._on_select(name)

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def sections(self) -> list[str]:
        return list(self._pages)

    @property
    def _page_frames(self) -> dict[str, ctk.CTkFrame]:
        return self._pages

    def set_enabled(self, name: str, enabled: bool) -> None:
        btn = self._buttons.get(name)
        if btn is not None:
            btn.configure(state="normal" if enabled else "disabled")

    def set_locked(self, locked: bool) -> None:
        """Freeze navigation -- used while another application is being driven
        with synthetic keystrokes, where a stray tab change sends the rest of
        the keys somewhere nobody intended."""
        for name in self._buttons:
            self.set_enabled(name, not locked)

    def refresh_theme(self) -> None:
        if self._current:
            self.select(self._current, notify=False)

    #: How long each tab is left open while it is warmed.
    WARM_HOLD_MS = 55

    def warm(self, index: int = 0) -> None:
        """Open every tab once, at start-up, so no first click has to wait.

        Keeping only the open tab in the grid is what makes resizing cheap,
        and the price is that a tab's first appearance pays for its whole
        layout in one go. Measured here: 242 ms the first time each tab was
        opened, against 43 ms before -- a hitch right when somebody has just
        clicked. Doing the work up front, while the window is idle and nobody
        is waiting on it, puts it where it cannot be felt.

        **It warms by opening each tab, exactly as a click does, one per turn
        of the event loop.** The obvious version -- grid a page, lay it out,
        take it back out again, without ever showing it -- does not work, and
        does not fail loudly either: the page is laid out but never finishes
        *mapping*, and every widget inside a canvas-embedded frame (which is
        every card on every tab, because each tab scrolls) is then left
        believing it was never shown. The tab opens, its geometry is right to
        the pixel, and it draws nothing at all. Going through the same path a
        click goes through cannot drift away from what a click does, which
        after that is the property worth having. The cost is that the tabs
        flick past once while the window is starting.

        `notify=False`: this is not somebody choosing a tab, and the pages'
        `refresh` hooks should not be run as though it were.
        """
        names = list(self._pages)
        if index == 0:
            if self._warm_home is not None:
                return                    # one is already going round
            self._warm_home = self._current
        if index >= len(names):
            if self._warm_home is not None:
                self.select(self._warm_home, notify=False)
            self._warm_home = None
            return
        try:
            self.select(names[index], notify=False)
            self.after(self.WARM_HOLD_MS, lambda: self.warm(index + 1))
        except Exception:
            self._warm_home = None        # the window is closing


class Shell(ctk.CTk):
    """Banner, tabs, shared output. Subclasses add the tabs."""

    #: Window chrome, dialogs and generated file names.
    APP_NAME = "ROV program"
    #: What the banner says.
    DISPLAY_TITLE = "Program Title"
    #: A short stem for per-program files under %LOCALAPPDATA%.
    SLUG = "rov_program"

    def __init__(self) -> None:
        # Before any widget exists: it reaches into CustomTkinter's scrollbar,
        # and a scrollbar already built would keep the method it was made with.
        ctk_tuning.apply()
        super().__init__()
        self.title(self.APP_NAME)
        self.geometry("1320x900")
        self.minsize(1000, 640)

        self.mode = "dark"
        self.banner_open = True
        T.apply(ctk, self.mode)
        self.configure(fg_color=T.BG)

        self.pages: dict[str, object] = {}
        #: tab name -> the pages built into it, each refreshed when it shows.
        self._tab_pages: dict[str, list] = {}
        self._queue: queue.Queue[tuple] = queue.Queue()
        #: The running job, until its result has been handed over.
        self._job: _Job | None = None
        self._job_seq = 0
        #: The last job's Stop signal, for anything that still reads it.
        self._cancel = threading.Event()
        #: key -> the newest background read asked for under that key.
        self._bg_gen: dict[str, int] = {}
        self._closing = False
        self._log_empty = True
        self._beat_due = time.monotonic()
        self._beat_seen = False
        self._late_logged = 0.0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_footer()
        self.build_tabs()
        if self.nav.sections:
            self.nav.select(self.nav.sections[0])
            # Once the window is up and before anyone has clicked anything.
            self.after(400, self.nav.warm)
        self.after(DRAIN_EVERY_MS, self._drain)
        # The watchdog starts counting from here: building the window is
        # start-up, however long it takes, not a stall.
        self._beat_due = time.monotonic() + diagnostics.HEARTBEAT_S
        self.after(int(diagnostics.HEARTBEAT_S * 1000), self._heartbeat)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    #  for subclasses
    # ------------------------------------------------------------------

    def build_tabs(self) -> None:
        """Add the program's tabs with `add_tab`. Called once, at startup."""

    def add_tab(self, name: str, key: str = "") -> ctk.CTkFrame:
        """Add a tab. `key` is its stable identifier -- see `TabStrip`."""
        return self.nav.add(name, key)

    def mount(self, tab: str, key: str, page) -> None:
        """Remember a page built into a tab, so showing the tab refreshes it."""
        self.pages[key] = page
        self._tab_pages.setdefault(tab, []).append(page)

    # ------------------------------------------------------------------
    #  the banner
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        """Drawn on a canvas so its height can come from its own type.

        CustomTkinter scales fonts for the display where a fixed pixel height
        does not, and the gradient rule along the foot is an image.
        """
        self.header = tkinter.Canvas(
            self, highlightthickness=0, borderwidth=0, height=104,
            background=self._apply_appearance_mode(T.HEADER_BG))
        self.header.grid(row=0, column=0, sticky="ew")
        # At most one repaint a frame while the window is being dragged, and
        # one more when it settles -- the banner is a full canvas rebuild
        # including a gradient, and a window manager delivers <Configure>
        # faster than that can be done or seen.
        self._header_paint = Repaint(self, self._paint_header)
        self.header.bind("<Configure>", self._header_configured)
        self._header_at: tuple[int, int] | None = None

        # bg_color as well as fg_color, and both as the (light, dark) pair.
        # CustomTkinter works a widget's bg_color out from its master, and a
        # plain Tk canvas can only answer with the one colour it is painted
        # right now -- which at start-up is the dark banner. The frame then
        # held that dark literal for the rest of the session, and at fractional
        # display scaling its rounded fill lands a pixel or two short of its
        # own canvas, so a hairline of it showed down the right edge and along
        # the foot in light mode. Every control inside the frame already
        # carries this for the same reason; the frame holding them did not.
        self.controls = ctk.CTkFrame(self.header, fg_color=T.HEADER_BG,
                                     bg_color=T.HEADER_BG, corner_radius=0)
        # Folded away, everything sits in one row reading left to right:
        # versions, lamps, then the three controls. Open, the versions move
        # up beside the title and the lamps drop under the two controls --
        # see `_lay_out_controls`.
        self.versions_row = ctk.CTkFrame(self.controls, fg_color="transparent")
        self.version_labels: dict[str, ctk.CTkLabel] = {}
        for i, key in enumerate(VERSION_KEYS):
            lab = ctk.CTkLabel(self.versions_row, text="", font=T.FONT_SMALL,
                               text_color=T.TEXT_MUTED, anchor="w")
            lab.grid(row=0, column=i, padx=(0, 12), sticky="w")
            self.version_labels[key] = lab

        self.lamps = {
            "vehicle": Lamp(self.controls, "Vehicle connected"),
            "logging": Lamp(self.controls, "Logging"),
        }

        # Where the diagnostics log is, one click away for whoever is asked
        # to send it after a problem.
        self.diag_btn = ctk.CTkButton(
            self.controls, text="Diagnostics", width=90, height=28,
            corner_radius=6, font=T.FONT_SMALL, fg_color="transparent",
            hover_color=T.SURFACE_ALT, text_color=T.TEXT_MUTED, border_width=0,
            bg_color=T.HEADER_BG, command=self.open_diagnostics)
        self.theme_switch = ctk.CTkSwitch(
            self.controls, text="Dark mode", command=self._toggle_theme,
            font=T.FONT_SMALL, text_color=T.TEXT,
            progress_color=T.ACCENT, button_color=T.SURFACE_ALT,
            bg_color=T.HEADER_BG)
        self.theme_switch.select()
        self.fold_btn = ctk.CTkButton(
            self.controls, text="▲", width=34, height=28, corner_radius=6,
            font=("Segoe UI Symbol", 14), fg_color="transparent",
            hover_color=T.SURFACE_ALT,
            text_color=T.TEXT, border_width=1, border_color=T.BORDER,
            bg_color=T.HEADER_BG, command=self.toggle_banner)
        self._fold_tip = "Hide the title banner"
        self._lay_out_controls()

        self._rule_photo = None
        self._rule_photos: dict[tuple, object] = {}
        self._logo_photo = None
        #: What the last full repaint was drawn for, and the items it left
        #: behind that a width change moves rather than redraws.
        self._painted: tuple | None = None
        self._ground_id = None
        self._rule_id = None
        self._controls_id = None
        #: Tk fonts, by (tuple, display scale). Building one asks the font
        #: system to resolve a family, which is not free and is the same
        #: answer every time the banner is painted.
        self._fonts: dict[tuple, object] = {}
        self._load_logo()
        self._versions: dict[str, str] = dict.fromkeys(VERSION_KEYS, "")
        self._versions_read_at = 0.0
        self._versions_reading = False
        self.after(1200, self._tick_status)

    # ---- the row (or block) of controls ---------------------------------

    def _lay_out_controls(self) -> None:
        """Arrange the controls for the banner's current state.

        Open, the two lamps sit directly under the two controls they line up
        with, and the fold button stands beside both rows. Folded, everything
        is one row: versions, lamps, Diagnostics, appearance, fold -- so the
        fold button is in the same place either way, which is the one thing
        about this banner that has always been deliberate.
        """
        for w in (self.versions_row, *self.lamps.values(), self.diag_btn,
                  self.theme_switch, self.fold_btn):
            w.grid_forget()
        if self.banner_open:
            self.diag_btn.grid(row=0, column=0, padx=(0, 10), sticky="w")
            self.theme_switch.grid(row=0, column=1, padx=(0, 10), sticky="w")
            self.fold_btn.grid(row=0, column=2, rowspan=2)
            self.lamps["vehicle"].grid(row=1, column=0, padx=(0, 10),
                                       pady=(6, 0), sticky="w")
            self.lamps["logging"].grid(row=1, column=1, padx=(0, 10),
                                       pady=(6, 0), sticky="w")
        else:
            self.versions_row.grid(row=0, column=0, padx=(0, 16), sticky="w")
            self.lamps["vehicle"].grid(row=0, column=1, padx=(0, 12))
            self.lamps["logging"].grid(row=0, column=2, padx=(0, 16))
            self.diag_btn.grid(row=0, column=3, padx=(0, 10))
            self.theme_switch.grid(row=0, column=4, padx=(0, 10))
            self.fold_btn.grid(row=0, column=5)

    def _font(self, font: tuple, scale: float):
        """A Tk font for a theme tuple at this display's scale, made once."""
        from tkinter import font as tkfont

        key = (tuple(font), round(scale, 3))
        got = self._fonts.get(key)
        if got is None:
            got = tkfont.Font(**_font_kw(T.scale_font(font, scale)))
            self._fonts[key] = got
        return got

    def _header_configured(self, event) -> None:
        """Repaint only when the banner's own size actually changed.

        Tk sends <Configure> for a good deal more than a resize -- a child
        being re-gridded, a scrollbar appearing, the window being raised --
        and repainting the banner for any of those is work nobody can see.
        """
        size = (int(event.width), int(event.height))
        if size == self._header_at:
            return
        self._header_at = size
        self._header_paint.ask()

    def _load_logo(self) -> None:
        self._logo_pil = None
        self._logo_at = None
        path = T.logo_for(self.mode)
        if not path:
            return
        try:
            from PIL import Image
            self._logo_pil = Image.open(path).convert("RGBA")
        except Exception:
            self._logo_pil = None

    def _logo_scaled(self, height: int):
        if self._logo_pil is None:
            return None
        height = max(8, int(height))
        if self._logo_at != (id(self._logo_pil), height):
            from PIL import Image
            w = max(1, int(self._logo_pil.width * height / self._logo_pil.height))
            self._logo_ready = self._logo_pil.resize((w, height), Image.LANCZOS)
            self._logo_at = (id(self._logo_pil), height)
        return self._logo_ready

    def toggle_banner(self) -> None:
        """Fold the banner away, or bring it back."""
        self.banner_open = not self.banner_open
        self.fold_btn.configure(text="▲" if self.banner_open else "▼")
        self._lay_out_controls()
        self._paint_header()

    # ------------------------------------------------------------------
    #  versions and lamps
    # ------------------------------------------------------------------

    def vehicle_status(self) -> tuple[bool, str]:
        """(is the vehicle answering, what the logging lamp should show).

        A subclass that has a recorder overrides this. The shell itself has
        no vehicle, so the lamps stay grey -- which is the truth for the
        imagery program, where there is nothing plugged in at all.
        """
        return False, "off"

    def _tick_status(self) -> None:
        """Lamps every second; versions every `VERSION_POLL_S` when connected."""
        if self._closing:
            return
        try:
            connected, logging_state = self.vehicle_status()
            self.lamps["vehicle"].set_state(
                Lamp.ON if connected else Lamp.OFF)
            self.lamps["logging"].set_state(
                {"on": Lamp.ON, "waiting": Lamp.WAITING}.get(
                    logging_state, Lamp.OFF))
            self._maybe_read_versions(connected)
        except Exception:
            diagnostics.log_exception("banner status", *sys.exc_info(),
                                      level=logging.WARNING)
        try:
            self.after(STATUS_POLL_MS, self._tick_status)
        except tkinter.TclError:
            pass

    def _maybe_read_versions(self, connected: bool) -> None:
        """Ask the vehicle what it is running, off the window's thread.

        Not through `submit`: that is the one worker, and it belongs to
        whatever the operator pressed. A banner poll must never be the reason
        a download will not start.
        """
        host = self.version_host()
        if not host or self._versions_reading:
            return
        due = time.monotonic() - self._versions_read_at
        if not connected and self._versions_read_at:
            return                       # nothing to ask; keep what we have
        if due < VERSION_POLL_S and self._versions_read_at:
            return
        self._versions_reading = True
        self._versions_read_at = time.monotonic()

        def read():
            from .. import blueos, laptop
            found = blueos.read_versions_brief(host)
            if not found.get("cockpit"):
                # Cockpit is normally flown from this laptop rather than
                # served off the vehicle, and the vehicle cannot see that.
                found["cockpit"] = laptop.cockpit_version()
                found["cockpit_from"] = "this laptop" if found["cockpit"] else ""
            return found

        def shown(found) -> None:
            self._versions_reading = False
            self.merge_versions(found)

        self.background("banner-versions", read, shown)

    def merge_versions(self, found) -> None:
        """Take what a read came back with, and show it.

        A blank answer does not erase a version already read. The tether comes
        and goes all day and the vehicle has not changed underneath it, so a
        banner that blanked itself every time a request timed out would be
        noise rather than information.
        """
        if not isinstance(found, dict):
            return                       # an exception, or nothing at all
        for key in VERSION_KEYS:
            if found.get(key):
                self._versions[key] = str(found[key])
        self._show_versions()

    def version_host(self) -> str | None:
        """The vehicle to ask. None in a program that has no vehicle."""
        return None

    def version_lines(self) -> list[str]:
        return [f"{VERSION_LABELS[k]} {self._versions.get(k) or UNKNOWN_VERSION}"
                for k in VERSION_KEYS]

    def _show_versions(self) -> None:
        for key, text in zip(VERSION_KEYS, self.version_lines(), strict=True):
            try:
                self.version_labels[key].configure(text=text)
            except Exception:
                return
        if self.banner_open:
            self._paint_header()         # they are drawn beside the title

    def _paint_header(self) -> None:
        """Logo, title and attribution, then the rule and the two controls.

        Folded, only the last three are drawn: the canvas shrinks to the
        height of the controls, and the rule and controls sit at the top of
        the window where the banner used to be.

        Widening the window changes three of these and none of the rest: the
        ground behind everything, the gradient rule along the foot, and where
        the controls sit. The logo, the title, the attribution and the version
        column are all pinned to the left edge and do not move at all. So a
        width-only change moves those three and leaves the rest of the canvas
        alone, rather than tearing the banner down and drawing it again --
        which was nine milliseconds of every frame of a drag.
        """
        from PIL import ImageTk

        c = self.header
        s = T.scale_of(self)
        w = max(1, c.winfo_width())
        pad = int(16 * s)
        rule_h = max(1, int(T.RULE_HEIGHT * s))
        ground = self._apply_appearance_mode(T.HEADER_BG)
        # Requested rather than laid-out height: this runs from <Configure>,
        # and forcing a layout pass from inside one re-enters it.
        ctl_h = max(self.controls.winfo_reqheight(), int(28 * s))

        if self.banner_open:
            f_title = self._font(T.title_font(), s)
            f_sub = self._font(T.FONT_BANNER_SUB, s)
            title_h = f_title.metrics("linespace")
            sub_h = f_sub.metrics("linespace")
            gap_title = int(10 * s)
            block_h = title_h + gap_title + sub_h * 2 + int(4 * s)
            # The controls are two rows tall now -- the lamps sit under
            # Diagnostics and the appearance switch -- so the banner has to
            # be at least as tall as they are, or they are clipped by the
            # gradient rule.
            h = max(block_h, ctl_h) + 2 * pad + rule_h
        else:
            block_h = 0
            h = int(6 * s) + ctl_h + int(6 * s) + rule_h

        # Everything a full repaint depends on except the width. The version
        # column is in here because it is drawn, so a version arriving has to
        # be able to redraw it.
        shape = (h, rule_h, pad, self.banner_open, self.mode, round(s, 3),
                 self.DISPLAY_TITLE, tuple(self.version_lines()))
        if shape == self._painted and self._ground_id is not None:
            self._restretch(w, h, rule_h, pad)
            return
        self._painted = shape

        if int(c.cget("height")) != h:
            c.configure(height=h)
        c.delete("all")
        c.configure(background=ground)
        self._ground_id = c.create_rectangle(0, 0, w, h, fill=ground,
                                             outline="")

        if self.banner_open:
            heading = self._apply_appearance_mode(T.HEADING)
            muted = self._apply_appearance_mode(T.TEXT_MUTED)
            x0 = pad + int(4 * s)
            logo = self._logo_scaled(block_h)
            if logo is None:
                c.create_text(x0, pad + block_h // 2, anchor="w",
                              text="Seattle Aquarium",
                              font=T.scale_font(T.FONT_H2, s), fill=heading)
                x1 = x0 + int(150 * s)
            else:
                self._logo_photo = ImageTk.PhotoImage(logo)
                c.create_image(x0, pad, image=self._logo_photo, anchor="nw")
                x1 = x0 + logo.width + int(22 * s)
            y = pad
            c.create_text(x1, y, anchor="nw", text=self.DISPLAY_TITLE,
                          font=T.scale_font(T.title_font(), s), fill=heading)
            y += title_h + gap_title
            for line in ATTRIBUTION_LINES:
                c.create_text(x1, y, anchor="nw", text=line,
                              font=T.scale_font(T.FONT_BANNER_SUB, s), fill=muted)
                y += sub_h + int(2 * s)

            # What the vehicle is running, in a column beside the title, at
            # the attribution's weight -- present without competing with it.
            # Drawn as canvas text rather than as the widget that carries the
            # same words when the banner is folded, for the same reason the
            # title is: this half of the banner is one drawn block.
            vx = x1 + f_title.measure(self.DISPLAY_TITLE) + int(40 * s)
            if vx < w - pad - int(320 * s):
                vy = pad + int(2 * s)
                for line in self.version_lines():
                    c.create_text(vx, vy, anchor="nw", text=line,
                                  font=T.scale_font(T.FONT_BANNER_SUB, s),
                                  fill=muted)
                    vy += sub_h + int(4 * s)

        # The one gradient in the application: a single object, no type on it.
        self._rule_id = c.create_image(0, h - rule_h, image=self._rule(w, rule_h),
                                       anchor="nw")
        self._controls_id = c.create_window(w - pad, (h - rule_h) // 2,
                                            window=self.controls, anchor="e")

    def _rule(self, w: int, rule_h: int):
        """The gradient along the banner's foot, as a Tk image.

        The bitmap is cached by size inside `gradients`; the Tk image made
        from it is cached here as well, because building one allocates and
        copies every pixel and a drag comes back through the same widths over
        and over. Kept small: the widths seen during one drag, no more.
        """
        from PIL import ImageTk

        key = (w, rule_h, self.mode)
        photo = self._rule_photos.get(key)
        if photo is None:
            photo = ImageTk.PhotoImage(
                G.render((w, rule_h), T.RULE_GRADIENT, angle=0.0))
            if len(self._rule_photos) >= RULE_CACHE_MAX:
                self._rule_photos.pop(next(iter(self._rule_photos)))
            self._rule_photos[key] = photo
        # Tk keeps no reference of its own to an image, so one the canvas is
        # showing must be held here or it is collected and the rule vanishes.
        self._rule_photo = photo
        return photo

    def _restretch(self, w: int, h: int, rule_h: int, pad: int) -> None:
        """The three things a width change moves, without redrawing the rest."""
        c = self.header
        try:
            c.coords(self._ground_id, 0, 0, w, h)
            c.itemconfigure(self._rule_id, image=self._rule(w, rule_h))
            c.coords(self._rule_id, 0, h - rule_h)
            c.coords(self._controls_id, w - pad, (h - rule_h) // 2)
        except tkinter.TclError:
            # An item went away under us -- draw the whole thing next time.
            self._painted = None

    def _toggle_theme(self) -> None:
        self.mode = "dark" if self.theme_switch.get() else "light"
        T.apply(ctk, self.mode)
        self.theme_switch.configure(text="Dark mode" if self.mode == "dark"
                                    else "Light mode")
        for lamp in self.lamps.values():
            lamp.refresh_theme()
        for lab in self.version_labels.values():
            lab.configure(text_color=T.TEXT_MUTED)
        self._load_logo()
        self._paint_header()
        self.nav.refresh_theme()
        for pages in self._tab_pages.values():
            for page in pages:
                fn = getattr(page, "refresh_theme", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    #  tabs and content
    # ------------------------------------------------------------------

    def _build_tabs(self) -> None:
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=2, column=0, sticky="nsew", padx=16, pady=(6, 6))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.nav = TabStrip(self, self.content, on_select=self._tab_shown)
        self.nav.grid(row=1, column=0, sticky="w", padx=14, pady=(8, 0))

    def _tab_shown(self, name: str) -> None:
        for page in self._tab_pages.get(name, []):
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as ex:
                    diagnostics.log_exception(f"{type(page).__name__}.refresh",
                                              *sys.exc_info())
                    self._log(f"{type(page).__name__}.refresh failed: {ex}")

    def scroll_body(self, parent) -> ctk.CTkScrollableFrame:
        """The scrolling column a tab's sections are laid into."""
        body = ctk.CTkScrollableFrame(parent, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        return body

    @staticmethod
    def pair(body, row: int, pady=(0, 12)) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        """Two equal columns, for sections small enough to sit side by side."""
        frame = ctk.CTkFrame(body, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=pady)
        frame.grid_columnconfigure((0, 1), weight=1, uniform="pair")
        frame.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(frame, fg_color="transparent")
        right = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for f in (left, right):
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(0, weight=1)
        return left, right

    # ------------------------------------------------------------------
    #  the shared output pane
    # ------------------------------------------------------------------

    def _build_footer(self) -> None:
        """Log, progress, status and Stop, shared by every tab.

        The handle along the top edge resizes the log and nothing else, so
        progress and status stay on screen however far it is closed.
        """
        f = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        f.grid(row=3, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)
        self.footer = f

        self.log = OutputBox(f, height=one_line_height("word"),
                             font=T.FONT_MONO, fg_color=T.FIELD_BG,
                             text_color=T.TEXT, border_width=1,
                             border_color=T.BORDER, corner_radius=6,
                             wrap="word")
        self.log_grip = Grip(
            f, lambda: float(self.log.cget("height")),
            lambda h: self.log.configure(height=h),
            minimum=one_line_height("word"), grows_downward=False,
            expanded=220)
        self.log_grip.grid(row=0, column=0, columnspan=2, sticky="ew",
                           padx=16, pady=(2, 0))
        self.log.grid(row=1, column=0, columnspan=2, sticky="ew",
                      padx=16, pady=(0, 6))
        self.log.configure(state="disabled")
        self.bind("<Configure>", self._cap_log, add="+")

        self.progress = ctk.CTkProgressBar(f, height=12, corner_radius=6,
                                           progress_color=T.ACCENT,
                                           fg_color=T.SURFACE_ALT)
        self.progress.set(0.0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=(16, 12),
                           pady=(0, 4))
        self.status = ctk.CTkLabel(f, text="Ready.", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.cancel_btn = button(f, "Stop", self._cancel_run, "danger", 90)
        self.cancel_btn.grid(row=2, column=1, rowspan=2, padx=(0, 16),
                             pady=(0, 8))
        self.cancel_btn.configure(state="disabled")

    def _cap_log(self, event) -> None:
        """Keep the log from being dragged taller than the window can spare."""
        if event.widget is not self:
            return
        try:
            s = T.scale_of(self)
            self.log_grip.maximum = max(self.log_grip.minimum,
                                        int(self.winfo_height() / s * LOG_MAX_SHARE))
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  the one worker
    # ------------------------------------------------------------------

    def submit(self, work, label: str | None = None, on_done=None) -> bool:
        """Run ``work(progress, cancel)`` on the worker thread.

        Returns False if a job is already running rather than starting a
        second one -- two jobs moving the same files would race. A job is
        running from here until its result (or its error) has been handed to
        `on_done`, so a finished job whose result is still queued still
        counts.
        """
        if self._job is not None:
            messagebox.showinfo(self.APP_NAME, "A job is already running. Wait "
                                               "for it to finish, or press Stop.")
            return False
        self._job_seq += 1
        job = _Job(self._job_seq, label or "", on_done)
        self._job = job
        self._cancel = job.cancel
        self.cancel_btn.configure(state="normal")
        self.progress.set(0.0)
        if label:
            self._log("─" * 60)
            self._log(label)
            self.status.configure(text=label)
        log.info("job %d started: %s", job.id, label or "(unlabelled)")
        diagnostics.note_activity("job", f"#{job.id} {label or ''}")
        put = self._queue.put

        def runner() -> None:
            try:
                out = work(lambda f, m="": put(("progress", job.id, f, m)),
                           job.cancel)
                put(("done", job.id, out))
            except BaseException as ex:          # noqa: BLE001 - reported, not lost
                if job.cancel.is_set():
                    # Whatever a job raises on its way out after Stop is the
                    # stop, not a fault, and is reported as one.
                    put(("stopped", job.id, ex))
                else:
                    put(("crash", job.id, traceback.format_exc(), ex))

        job.thread = threading.Thread(target=runner, daemon=True,
                                      name=f"job-{job.id}")
        job.thread.start()
        return True

    @property
    def busy(self) -> bool:
        return self._job is not None

    def _cancel_run(self) -> None:
        job = self._job
        if job is not None and not job.cancel.is_set():
            job.cancel.set()
            log.info("job %d: Stop pressed", job.id)
            self.status.configure(text="Stopping…")

    def background(self, key: str, work: Callable[[], object],
                   on_result: Callable[[object], None]) -> int:
        """Run a read-only ``work()`` off the Tk thread, beside the worker.

        For reading the disk -- folder scans, copy checks -- which must not
        wait behind a download and must not freeze the window either.
        `on_result` is called on the Tk thread with the result (or the
        exception), and only if no newer call has been made with the same
        `key` since: a scan of a folder the operator has already moved away
        from is dropped rather than shown.
        """
        gen = self._bg_gen.get(key, 0) + 1
        self._bg_gen[key] = gen
        put = self._queue.put

        def run() -> None:
            try:
                out = work()
            except Exception as ex:
                diagnostics.log_exception(f"background read {key}",
                                          *sys.exc_info(), level=logging.WARNING)
                out = ex
            put(("bg", key, gen, on_result, out))

        threading.Thread(target=run, daemon=True, name=f"bg-{key}").start()
        return gen

    def _drain(self) -> None:
        """Hand queued worker messages to the window, then come back.

        The next pass is scheduled in `finally`, so a callback that raises is
        logged and the queue is still serviced afterwards -- before, one
        exception here stopped every later job from ever reporting.
        """
        if self._closing:
            return
        try:
            self._drain_pass()
        except Exception:
            diagnostics.log_exception("output queue", *sys.exc_info())
        finally:
            if not self._closing:
                try:
                    self.after(DRAIN_EVERY_MS, self._drain)
                except tkinter.TclError:
                    pass                               # the window is gone

    def _drain_pass(self) -> None:
        deadline = time.monotonic() + DRAIN_MAX_S
        progress: dict[int, tuple] = {}
        for _ in range(DRAIN_MAX_MESSAGES):
            if time.monotonic() > deadline:
                break
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "progress":
                # Replaceable: only the newest of a batch is drawn.
                progress[item[1]] = item[2:]
                continue
            if item[0] in ("done", "crash", "stopped"):
                pending = progress.pop(item[1], None)
                if pending is not None:
                    self._show_progress(item[1], *pending)
            self._handle(item)
        for job_id, (frac, msg) in progress.items():
            self._show_progress(job_id, frac, msg)
        job = self._job
        if (job is not None and job.thread is not None
                and not job.thread.is_alive() and self._queue.empty()):
            # Every message a thread sends is queued before it exits, so a
            # thread already dead when the queue is then seen empty ended
            # without a result. (In that order: the other way round races.)
            self._retire(job, "ended without reporting a result")
            self.status.configure(text="The job ended without a result.")

    def _show_progress(self, job_id: int, frac, msg) -> None:
        if self._job is None or self._job.id != job_id:
            return
        self.progress.set(max(0.0, min(1.0, float(frac or 0))))
        if msg:
            self.status.configure(text=msg)

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        try:
            if kind == "bg":
                _, key, gen, on_result, out = item
                if self._bg_gen.get(key) == gen:
                    on_result(out)
                return
            job = self._job
            if job is None or job.id != item[1]:
                log.warning("dropped a %s from job %s, which is no longer the "
                            "running job", kind, item[1])
                return
            # Retired before its callback runs, so the callback can start the
            # next job, and a callback that fails still leaves Stop disabled.
            if kind == "done":
                self._retire(job, "stopped" if job.cancel.is_set() else "finished")
                self._finish(item[2], job)
            elif kind == "crash":
                self._retire(job, "failed")
                self._crashed(job, item[2], item[3])
            elif kind == "stopped":
                self._retire(job, "stopped part way")
                self._stopped(job, item[2])
        except Exception:
            diagnostics.log_exception(f"handling a {kind} message", *sys.exc_info())
            try:
                self._log("A result could not be shown; the details are in the "
                          "diagnostics log.")
            except Exception:
                pass

    def _retire(self, job: _Job, outcome: str) -> None:
        if self._job is job:
            self._job = None
        diagnostics.note_activity("job", None)
        log.info("job %d %s after %.1f s: %s", job.id, outcome,
                 time.monotonic() - job.started, job.label)
        try:
            self.cancel_btn.configure(state="disabled")
        except Exception:
            pass

    def _crashed(self, job: _Job, tb: str, ex) -> None:
        log.error("job %d raised:\n%s", job.id, tb)
        self._log("Unexpected error:\n" + tb)
        self.status.configure(text=f"Failed: {ex}")
        # A page waiting on its result is told, so it can put its own widgets
        # back rather than saying "Reading…" forever.
        if job.on_done is not None:
            try:
                job.on_done(ex)
            except Exception:
                self._log("on_done failed:\n" + traceback.format_exc())
                diagnostics.log_exception("on_done after a failure", *sys.exc_info())

    def _stopped(self, job: _Job, ex) -> None:
        log.info("job %d stopped at: %s: %s", job.id, type(ex).__name__, ex)
        self._log(f"Stopped before it finished ({ex})." if str(ex) else
                  "Stopped before it finished.")
        self.status.configure(text="Stopped before it finished.")
        if job.on_done is not None:
            try:
                job.on_done(ex)
            except Exception:
                self._log("on_done failed:\n" + traceback.format_exc())
                diagnostics.log_exception("on_done after a stop", *sys.exc_info())

    def _finish(self, res, job: _Job | None = None) -> None:
        """Report whatever the worker returned."""
        cb = job.on_done if job is not None else None
        if cb is not None:
            try:
                cb(res)
            except Exception:
                self._log("on_done failed:\n" + traceback.format_exc())
                diagnostics.log_exception("on_done", *sys.exc_info())

        if self.finish_special(res):
            return
        self.progress.set(1.0)
        reports = res if isinstance(res, (list, tuple)) else [res]
        opened: Path | None = None
        for r in reports:
            if r is None:
                continue
            text = r.summary() if hasattr(r, "summary") else _brief(r, job)
            for line in str(text).splitlines():
                self._log(line)
            for w in getattr(r, "warnings", []) or []:
                self._log(f"WARNING: {w}")
            for e in getattr(r, "errors", []) or []:
                self._log(f"ERROR: {e}")
            opened = opened or getattr(r, "root", None) or getattr(r, "target", None)
        stopped = job is not None and job.cancel.is_set()
        # A stopped job is not a finished one, and does not say so.
        self.status.configure(text="Stopped before it finished." if stopped
                              else "Done.")
        if not stopped:
            self._reveal(opened)

    def finish_special(self, res) -> bool:
        """A subclass's chance to report a result its own way. True = handled."""
        return False

    def _reveal(self, path: Path | None) -> None:
        """Open a folder in Explorer, best effort."""
        if not path:
            return
        try:
            import os
            os.startfile(Path(path))          # noqa: S606
        except Exception:
            pass

    def _log(self, text: str) -> None:
        # The newline goes before each message rather than after it, so the
        # last line of the log is the last message and not an empty line --
        # which is what a pane dragged down to one line shows. Whether the
        # pane is empty is remembered rather than read back out of it, and
        # the pane is trimmed in blocks: reading or keeping the whole text
        # made every message slower than the last over a long day.
        text = _bounded(str(text))
        output_log.info("%s", text)
        self.log.configure(state="normal")
        self.log.insert("end", ("" if self._log_empty else "\n") + text)
        self._log_empty = False
        try:
            lines = int(str(self.log.index("end-1c")).split(".")[0])
            if lines > LOG_MAX_LINES:
                self.log.delete("1.0", f"{lines - LOG_MAX_LINES + LOG_TRIM_LINES}.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------
    #  staying diagnosable
    # ------------------------------------------------------------------

    def report_callback_exception(self, exc, val, tb) -> None:   # noqa: D401
        """Tk's hook for an exception in a callback -- a button, a timer.

        Tk's own version prints to stderr, which does not exist under
        pythonw, so these used to vanish without trace.
        """
        diagnostics.log_exception("window callback", exc, val, tb)
        try:
            self.status.configure(
                text=f"Something went wrong: {val} — recorded in the "
                     f"diagnostics log.")
        except Exception:
            pass

    def _heartbeat(self) -> None:
        """Tells the diagnostics watchdog the window is still running."""
        if self._closing:
            return
        now = time.monotonic()
        late = max(0.0, now - self._beat_due)
        # The first beat can be late by however long mainloop took to start.
        diagnostics.beat(late if self._beat_seen else 0.0)
        if not self._beat_seen:
            self._beat_seen = True
            late = 0.0
        if late > 1.0 and now - self._late_logged > 60.0:
            self._late_logged = now
            log.warning("the window ran its heartbeat %.1f s late%s", late,
                        f" ({diagnostics.activity_text().strip()})"
                        if diagnostics.activity_text() else "")
        self._beat_due = now + diagnostics.HEARTBEAT_S
        try:
            self.after(int(diagnostics.HEARTBEAT_S * 1000), self._heartbeat)
        except tkinter.TclError:
            pass

    def open_diagnostics(self) -> None:
        where = diagnostics.open_folder()
        self._log(f"Diagnostics are in {where}")

    # ------------------------------------------------------------------
    #  closing
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        job = self._job
        if job is not None:
            if not messagebox.askyesno(self.APP_NAME,
                                       "A job is in progress. Quit anyway?"):
                return
            job.cancel.set()
        if self.before_close():
            self.close_now()

    def close_now(self) -> None:
        """Destroy the window. Queued worker messages are not delivered."""
        if self._closing:
            return
        self._closing = True
        log.info("window closed")
        try:
            self.destroy()
        except tkinter.TclError:
            pass

    def before_close(self) -> bool:
        """A subclass's last word before the window closes.

        False keeps the window open; a subclass that needs time to shut down
        returns False and calls `close_now` itself when it is done.
        """
        return True
