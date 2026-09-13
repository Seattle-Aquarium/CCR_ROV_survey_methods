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
touches a widget.
"""

from __future__ import annotations

import queue
import threading
import tkinter
import traceback
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from . import gradients as G
from . import theme as T
from .widgets import Grip, button, one_line_height


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

#: The log pane may be dragged open to at most this share of the window.
LOG_MAX_SHARE = 0.6


class TabStrip(ctk.CTkFrame):
    """The program's tabs, left to right, the open one outlined.

    Keeps the small navigation API the pages already use -- ``select``,
    ``sections``, ``current``, ``set_enabled`` and ``set_locked`` -- so a page
    written against UTC's rail works unchanged on a tab.
    """

    def __init__(self, master, holder, on_select: Callable[[str], None]):
        super().__init__(master, fg_color="transparent")
        self._holder = holder
        self._on_select = on_select
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._current: str | None = None

    def add(self, name: str) -> ctk.CTkFrame:
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
        page.lower()
        self._pages[name] = page
        if self._current is None:
            self.select(name, notify=False)
        return page

    def select(self, name: str, notify: bool = True) -> None:
        if name not in self._pages:
            return
        self._current = name
        self._pages[name].lift()
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


class Shell(ctk.CTk):
    """Banner, tabs, shared output. Subclasses add the tabs."""

    #: Window chrome, dialogs and generated file names.
    APP_NAME = "ROV program"
    #: What the banner says.
    DISPLAY_TITLE = "Program Title"
    #: A short stem for per-program files under %LOCALAPPDATA%.
    SLUG = "rov_program"

    def __init__(self) -> None:
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
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._on_done = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_footer()
        self.build_tabs()
        if self.nav.sections:
            self.nav.select(self.nav.sections[0])
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    #  for subclasses
    # ------------------------------------------------------------------

    def build_tabs(self) -> None:
        """Add the program's tabs with `add_tab`. Called once, at startup."""

    def add_tab(self, name: str) -> ctk.CTkFrame:
        return self.nav.add(name)

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
        self.header.bind("<Configure>", lambda _e: self._paint_header())

        self.controls = ctk.CTkFrame(self.header, fg_color=T.HEADER_BG,
                                     corner_radius=0)
        self.theme_switch = ctk.CTkSwitch(
            self.controls, text="Dark mode", command=self._toggle_theme,
            font=T.FONT_SMALL, text_color=T.TEXT,
            progress_color=T.ACCENT, button_color=T.SURFACE_ALT,
            bg_color=T.HEADER_BG)
        self.theme_switch.select()
        self.theme_switch.grid(row=0, column=0, padx=(0, 10))
        self.fold_btn = ctk.CTkButton(
            self.controls, text="▲", width=34, height=28, corner_radius=6,
            font=("Segoe UI Symbol", 14), fg_color="transparent",
            hover_color=T.SURFACE_ALT,
            text_color=T.TEXT, border_width=1, border_color=T.BORDER,
            bg_color=T.HEADER_BG, command=self.toggle_banner)
        self.fold_btn.grid(row=0, column=1)
        self._fold_tip = "Hide the title banner"

        self._rule_photo = None
        self._logo_photo = None
        self._load_logo()

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
        self._paint_header()

    def _paint_header(self) -> None:
        """Logo, title and attribution, then the rule and the two controls.

        Folded, only the last three are drawn: the canvas shrinks to the
        height of the controls, and the rule and controls sit at the top of
        the window where the banner used to be.
        """
        from tkinter import font as tkfont

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
            f_title = tkfont.Font(**_font_kw(T.scale_font(T.title_font(), s)))
            f_sub = tkfont.Font(**_font_kw(T.scale_font(T.FONT_BANNER_SUB, s)))
            title_h = f_title.metrics("linespace")
            sub_h = f_sub.metrics("linespace")
            gap_title = int(10 * s)
            block_h = title_h + gap_title + sub_h * 2 + int(4 * s)
            h = pad + block_h + pad + rule_h
        else:
            block_h = 0
            h = int(6 * s) + ctl_h + int(6 * s) + rule_h

        if int(c.cget("height")) != h:
            c.configure(height=h)
        c.delete("all")
        c.configure(background=ground)
        c.create_rectangle(0, 0, w, h, fill=ground, outline="")

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

        # The one gradient in the application: a single object, no type on it.
        rule = G.render((w, rule_h), T.RULE_GRADIENT, angle=0.0)
        self._rule_photo = ImageTk.PhotoImage(rule)
        c.create_image(0, h - rule_h, image=self._rule_photo, anchor="nw")

        c.create_window(w - pad, (h - rule_h) // 2, window=self.controls,
                        anchor="e")

    def _toggle_theme(self) -> None:
        self.mode = "dark" if self.theme_switch.get() else "light"
        T.apply(ctk, self.mode)
        self.theme_switch.configure(text="Dark mode" if self.mode == "dark"
                                    else "Light mode")
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

        self.log = ctk.CTkTextbox(f, height=one_line_height("word"),
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
        second one -- two jobs moving the same files would race.
        """
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(self.APP_NAME, "A job is already running. Wait "
                                               "for it to finish, or press Stop.")
            return False
        self._cancel.clear()
        self._on_done = on_done
        self.cancel_btn.configure(state="normal")
        self.progress.set(0.0)
        if label:
            self._log("─" * 60)
            self._log(label)
            self.status.configure(text=label)

        def runner() -> None:
            try:
                out = work(lambda f, m="": self._queue.put(("progress", f, m)),
                           self._cancel)
                self._queue.put(("done", out))
            except Exception as ex:
                self._queue.put(("crash", traceback.format_exc(), ex))

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()
        return True

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _cancel_run(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self.status.configure(text="Stopping…")

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, frac, msg = item
                    self.progress.set(max(0.0, min(1.0, float(frac or 0))))
                    if msg:
                        self.status.configure(text=msg)
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "crash":
                    self._log("Unexpected error:\n" + item[1])
                    self.status.configure(text=f"Failed: {item[2]}")
                    self.cancel_btn.configure(state="disabled")
                    # A page waiting on its result is told, so it can put its
                    # own widgets back rather than saying "Reading…" forever.
                    cb, self._on_done = self._on_done, None
                    if cb is not None:
                        try:
                            cb(item[2])
                        except Exception:
                            self._log("on_done failed:\n" + traceback.format_exc())
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, res) -> None:
        """Report whatever the worker returned."""
        self.cancel_btn.configure(state="disabled")
        cb, self._on_done = self._on_done, None
        if cb is not None:
            try:
                cb(res)
            except Exception:
                self._log("on_done failed:\n" + traceback.format_exc())

        if self.finish_special(res):
            return
        self.progress.set(1.0)
        reports = res if isinstance(res, (list, tuple)) else [res]
        opened: Path | None = None
        for r in reports:
            if r is None:
                continue
            text = r.summary() if hasattr(r, "summary") else str(r)
            for line in str(text).splitlines():
                self._log(line)
            for w in getattr(r, "warnings", []) or []:
                self._log(f"WARNING: {w}")
            for e in getattr(r, "errors", []) or []:
                self._log(f"ERROR: {e}")
            opened = opened or getattr(r, "root", None) or getattr(r, "target", None)
        self.status.configure(text="Done.")
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
        # which is what a pane dragged down to one line shows.
        self.log.configure(state="normal")
        first = not self.log.get("1.0", "end-1c")
        self.log.insert("end", ("" if first else "\n") + text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(self.APP_NAME,
                                       "A job is in progress. Quit anyway?"):
                return
            self._cancel.set()
        if self.before_close():
            self.destroy()

    def before_close(self) -> bool:
        """A subclass's last word before the window closes. False = stay open."""
        return True
