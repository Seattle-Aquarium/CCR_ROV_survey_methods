"""
The Underwater Telemetry Compositing (UTC) desktop application.

Layout follows the order of the job: pick the flight folder, confirm what was
found, describe the sites and transects, choose output sizes, run.

The pipeline runs on a worker thread and reports progress through a queue that
the Tk main loop drains on a timer. Tk is not thread-safe, so no worker ever
touches a widget directly.
"""

from __future__ import annotations

import queue
import threading
import traceback
from datetime import date as _date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import discovery
from ..config import AppConfig
from ..pipeline import RunResult
from ..survey import (
    PLAN_FILENAME,
    Site,
    SurveyPlan,
    plan_path,
)
from . import gradients as G
from . import theme as T
from .widgets import Card, SiteFrame, button, entry

APP_NAME = "Underwater Telemetry Compositing"
#: Short form, for window chrome and generated file names.
APP_ABBREV = "UTC"

#: What the banner says. Deliberately separate from APP_NAME, which still names
#: the window, the dialogs and the files this writes -- renaming the programme
#: is a decision for later, and nothing on disk should move in the meantime.
DISPLAY_TITLE = "Program title TBD"

#: One line per chapter, in the rail's order, and numbered to match it. Drawn
#: with a coloured badge carrying the number -- the same colour that chapter's
#: button wears -- so the banner reads as the roadmap for the rail rather than
#: as a sentence that happens to list four things.
CHAPTER_BLURBS = (
    "Connect to ROV, monitor vehicle health and fetch files",
    "ROV telemetry, message and log health",
    "Import, process and export polished photos",
    "Import, assemble and export videos",
)

#: Set apart from the roadmap by a wider gap and a quieter ink: this is who
#: made it, not what it does. "Seattle Aquarium" is deliberately absent -- the
#: logo two inches to the left already says it.
ATTRIBUTION = ("Conservation Programs and Partnerships  ·  "
               "Coastal Climate Resilience")
# Plan filename and legacy fallback live in survey.py, so the CLI and the
# GUI cannot drift apart on which file they read.


def _font_kw(font: tuple) -> dict:
    """A Tk font tuple as keyword arguments for tkinter.font.Font."""
    kw = {"family": font[0], "size": font[1]}
    if len(font) > 2 and "bold" in font[2:]:
        kw["weight"] = "bold"
    return kw


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x900")
        self.minsize(980, 700)

        self.mode = "dark"
        T.apply(ctk, self.mode)
        self.configure(fg_color=T.BG)

        self.cfg = AppConfig()
        self.flight_dir: Path | None = None
        self.discovery: discovery.Discovery | None = None
        self._sites: list[SiteFrame] = []
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._on_done = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_body()
        self._build_footer()
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    #  chrome
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        """The banner, drawn on a canvas over a brand gradient.

        A canvas rather than a frame of labels so the height can come from the
        rendered type: the banner sets three lines, and CustomTkinter scales
        fonts for the display where a fixed pixel height does not. It also
        makes the bright rule along its foot a two-line job.
        """
        import tkinter

        self.header = tkinter.Canvas(
            self, highlightthickness=0, borderwidth=0, height=104,
            background=self._apply_appearance_mode(T.HEADER_BG))
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.bind("<Configure>", lambda _e: self._paint_header())

        self.theme_switch = ctk.CTkSwitch(
            self.header, text="Dark mode", command=self._toggle_theme,
            font=T.FONT_SMALL, text_color=T.TEXT,
            progress_color=T.ACCENT, button_color=T.SURFACE_ALT,
        )
        self.theme_switch.select()
        self._header_photo = None
        self._rule_photo = None
        self._load_logo()

    def _load_logo(self) -> None:
        """The logo for this mode, at full resolution.

        Kept as a PIL image rather than the CTkImage the rest of the
        application uses, because the banner is a canvas. Full resolution
        because the banner's height comes from its own type, so the size it
        wants is not known until it draws.
        """
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

    def _wrap_roadmap(self, f_sub, badge: int, gap: int,
                      avail: int) -> list[list[int]]:
        """Fit the four chapter blurbs into lines of at most `avail` pixels.

        Returns the chapter indices per line. Flowing rather than fixed at two
        lines, because the window can be dragged from 980px to a wide monitor
        and the roadmap should use whatever it is given.
        """
        lines: list[list[int]] = [[]]
        used = 0
        for i, blurb in enumerate(CHAPTER_BLURBS):
            need = badge + gap // 2 + f_sub.measure(blurb)
            if lines[-1] and used + gap + need > avail:
                lines.append([])
                used = 0
            used += (gap if lines[-1] else 0) + need
            lines[-1].append(i)
        return lines

    def _paint_header(self) -> None:
        """Draw the banner at the current width.

        Laid out by measuring rather than by fixed offsets. Three things follow
        from that and none of them can be a constant: the roadmap flows into as
        many lines as the width needs, the banner is as tall as whatever that
        came to, and the logo is sized to span the title and roadmap together.

        The roadmap's numbers are badges in their own chapter's colour -- the
        same colour that chapter's button wears in the rail. It reads as the
        key to the rail rather than as a sentence, and it sidesteps the fact
        that Seafoam and Algae cannot be used as type on a light ground.
        """
        from tkinter import font as tkfont

        from PIL import ImageTk

        c = self.header
        s = T.scale_of(self)
        w = max(1, c.winfo_width())
        pad = int(16 * s)
        rule_h = max(1, int(T.RULE_HEIGHT * s))
        ground = self._apply_appearance_mode(T.HEADER_BG)

        f_title = tkfont.Font(**_font_kw(T.scale_font(T.FONT_BANNER, s)))
        f_sub = tkfont.Font(**_font_kw(T.scale_font(T.FONT_BANNER_SUB, s)))
        title_h = f_title.metrics("linespace")
        sub_h = f_sub.metrics("linespace")

        badge = int(sub_h * 1.25)
        line_h = badge + int(5 * s)
        gap_title = int(9 * s)
        gap_attrib = int(14 * s)         # the visible break: roadmap, then who
        seg_gap = int(24 * s)
        logo_gap = int(22 * s)

        # The logo's height depends on how many lines the roadmap takes, and
        # the space the roadmap has depends on how wide the logo is. Settle it
        # by laying out twice -- the second pass knows the real logo width.
        lines: list[list[int]] = [[0, 1], [2, 3]]
        x = logo_w = 0
        for _ in range(2):
            block_h = title_h + gap_title + len(lines) * line_h
            logo = self._logo_scaled(block_h)
            logo_w = logo.width if logo is not None else int(150 * s)
            x = pad + int(4 * s) + logo_w + logo_gap
            avail = max(int(200 * s), w - x - int(170 * s))
            lines = self._wrap_roadmap(f_sub, badge, seg_gap, avail)

        block_h = title_h + gap_title + len(lines) * line_h
        h = pad + block_h + gap_attrib + sub_h + pad + rule_h
        if int(c.cget("height")) != h:
            c.configure(height=h)

        c.delete("all")
        c.configure(background=ground)
        c.create_rectangle(0, 0, w, h, fill=ground, outline="")

        heading = self._apply_appearance_mode(T.HEADING)
        body = self._apply_appearance_mode(T.TEXT)
        muted = self._apply_appearance_mode(T.TEXT_MUTED)

        # The logo spans the title and the roadmap, top-aligned with both.
        logo = self._logo_scaled(block_h)
        if logo is not None:
            self._logo_photo = ImageTk.PhotoImage(logo)
            c.create_image(pad + int(4 * s), pad, image=self._logo_photo,
                           anchor="nw")
        else:
            c.create_text(pad + int(4 * s), pad + block_h // 2, anchor="w",
                          text="Seattle Aquarium",
                          font=T.scale_font(T.FONT_H2, s), fill=heading)

        y = pad
        c.create_text(x, y, anchor="nw", text=DISPLAY_TITLE,
                      font=T.scale_font(T.FONT_BANNER, s), fill=heading)
        y += title_h + gap_title

        self._badge_photos = []
        f_badge = T.scale_font(T.FONT_BANNER_SUB, s)
        for row in lines:
            bx = x
            for i in row:
                colour = T.CHAPTER_COLOURS[i % len(T.CHAPTER_COLOURS)]
                img = G.chip((badge, badge), fill=colour, radius=badge // 4,
                             ground=ground)
                photo = ImageTk.PhotoImage(img)
                self._badge_photos.append(photo)
                c.create_image(bx, y, image=photo, anchor="nw")
                c.create_text(bx + badge / 2, y + badge / 2, anchor="center",
                              text=str(i + 1), font=f_badge,
                              fill=T.ink_for(colour))
                bx += badge + seg_gap // 2
                c.create_text(bx, y + badge / 2, anchor="w",
                              text=CHAPTER_BLURBS[i], font=f_badge, fill=body)
                bx += f_sub.measure(CHAPTER_BLURBS[i]) + seg_gap
            y += line_h

        c.create_text(x, y + gap_attrib, anchor="nw", text=ATTRIBUTION,
                      font=T.scale_font(T.FONT_BANNER_SUB, s), fill=muted)

        # The one gradient in the application: a three-colour bright gradient
        # along the foot of the banner. It is a single object and carries no
        # type, which is exactly what p.19 sanctions a bright gradient for.
        rule = G.render((w, rule_h), T.RULE_GRADIENT, angle=0.0)
        self._rule_photo = ImageTk.PhotoImage(rule)
        c.create_image(0, h - rule_h, image=self._rule_photo, anchor="nw")

        self.theme_switch.configure(bg_color=ground)
        c.create_window(w - pad, (h - rule_h) // 2, window=self.theme_switch,
                        anchor="e")

    def _logo_scaled(self, height: int):
        """The logo at a pixel height, cached so a resize is not a resample.

        Sized to span the title and the roadmap beneath it. p.11 sets a
        minimum of 50px wide for digital and no maximum; the clear space it
        asks for -- the height of the "A" in AQUARIUM, about a sixth of the
        mark -- is what the banner's own padding provides.
        """
        if self._logo_pil is None:
            return None
        height = max(8, int(height))
        if getattr(self, "_logo_at", None) != (id(self._logo_pil), height):
            from PIL import Image
            w = max(1, int(self._logo_pil.width * height / self._logo_pil.height))
            self._logo_ready = self._logo_pil.resize((w, height), Image.LANCZOS)
            self._logo_at = (id(self._logo_pil), height)
        return self._logo_ready

    def _toggle_theme(self) -> None:
        self.mode = "dark" if self.theme_switch.get() else "light"
        T.apply(ctk, self.mode)
        self.theme_switch.configure(text="Dark mode" if self.mode == "dark"
                                    else "Light mode")
        self._load_logo()
        self._paint_header()
        if hasattr(self, "nav"):
            self.nav.refresh_theme()

    # ------------------------------------------------------------------
    #  body
    # ------------------------------------------------------------------

    def _build_body(self) -> None:
        """Four chapters, in the order a survey day happens.

        Aboard ROV is the boat: the flight and its transect times, the vehicle,
        and the copy onto the drive. Flight report is the desk afterwards --
        what the recordings say and whether they are sound. Then the two media
        chapters, which are the same shape as each other: bring it in, work it
        up, get it out.

        Grouping rather than one rail entry per tool. Eight entries had stopped
        describing anything; four describe the day, and a chapter can grow a
        fifth tool without the rail growing at all.
        """
        from .bannertools import BannerToolsTab
        from .healthpage import HealthPage
        from .importpage import ImportPage
        from .nav import Navigator
        from .processpage import ProcessPage
        from .rovpage import RovPage
        from .transectpage import TransectPage
        from .videopage import VideoPage

        # Pages that read the flight re-read it when opened: the folder and the
        # survey plan are both edited on Flight & transects, and a page built
        # once at startup would still show the state from then.
        nav = Navigator(self, on_select=self._page_shown)
        nav.grid(row=1, column=0, sticky="nsew")
        self.nav = nav
        self.pages = {}

        # 1. On the boat. The flight comes first even though the chapter is
        #    named for the vehicle: the site and date are what every folder
        #    downstream is named from, and the snapshot needs somewhere to land.
        aboard = nav.add_chapter("Aboard ROV")
        self._build_flight_page(aboard.add("Flight & transects"))
        self._mount(aboard, "Vehicle & files", RovPage)

        # 2. Back at the desk. Transects lead: the CSVs need only the plan and
        #    the mcaps, and the same windows go on to drive the video overlays.
        report = nav.add_chapter("Flight report")
        self._mount(report, "Transects", TransectPage)
        self._mount(report, "Recording health", HealthPage)

        # 3 and 4. Bring it in, work it up, get it out.
        photos = nav.add_chapter("Photos")
        self._mount(photos, "Import photos", ImportPage)
        self._mount(photos, "Process photos", ProcessPage)
        self._mount(photos, "Banner tools", BannerToolsTab)

        videos = nav.add_chapter("Videos")
        self._mount(videos, "Video", VideoPage)

        nav.select("Flight & transects")

    def _mount(self, chapter, name: str, cls) -> None:
        """Build one tool into its chapter and remember it by name."""
        page = cls(chapter.add(name), self)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[name] = page

    def _page_shown(self, name: str) -> None:
        """Let a page re-read the flight when the rail raises it."""
        page = getattr(self, "pages", {}).get(name)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def use_flight(self, path: Path) -> None:
        """Adopt a flight folder as the current one and rescan it.

        Creating a flight selects it, so the natural next step needs no
        re-navigation. Every page reads `self.flight_dir`, so one setter serves
        all of them.
        """
        self.flight_dir = Path(path)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan()

    def _build_flight_page(self, parent) -> None:
        body = ctk.CTkScrollableFrame(parent, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)


        # ---- 1. flight folder ----------------------------------------
        c1 = Card(body, "1.  Flight folder",
                  "Point at the folder for the dive. Expected inside: "
                  "logs/ (mcap), videos/downward/ (GoPro). Older layouts are "
                  "handled too, and forward/ video is ignored.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        r = ctk.CTkFrame(c1.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="ew")
        r.grid_columnconfigure(0, weight=1)
        self.folder_entry = entry(r, "No folder selected", width=700)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(r, "Browse…", self._pick_folder, "primary", width=110
               ).grid(row=0, column=1)

        self.found = ctk.CTkTextbox(c1.body, height=118, font=T.FONT_MONO,
                                    fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                    border_width=1, border_color=T.BORDER,
                                    corner_radius=6, wrap="none")
        self.found.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.found.insert("1.0", "Nothing selected yet.")
        self.found.configure(state="disabled")

        # ---- 2. sites & transects ------------------------------------
        c2 = Card(body, "2.  Sites and transects",
                  "Times are TC-25, as written down in the field — the clock the "
                  "GoPro shows after a GoPro Labs precision-time sync.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.sites_holder = ctk.CTkFrame(c2.body, fg_color="transparent")
        self.sites_holder.grid(row=0, column=0, sticky="ew")
        self.sites_holder.grid_columnconfigure(0, weight=1)

        srow = ctk.CTkFrame(c2.body, fg_color="transparent")
        srow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        button(srow, "+ Add site", self.add_site, "primary", width=120
               ).grid(row=0, column=0, sticky="w")
        button(srow, "Load…", self._load_plan, "ghost", width=90
               ).grid(row=0, column=1, padx=(8, 0))
        button(srow, "Save", self._save_plan, "ghost", width=90
               ).grid(row=0, column=2, padx=(8, 0))

        # ---- 3. preview -----------------------------------------------
        c3 = Card(body, "3.  Preview the transects",
                  "Reads the flight's mcap and draws the dive profile with your "
                  "transects marked. Worth doing before importing imagery — "
                  "especially if the card is about to be wiped.")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(0, weight=1)
        prow = ctk.CTkFrame(c3.body, fg_color="transparent")
        prow.grid(row=0, column=0, sticky="w")
        button(prow, "Preview transects", self._preview_transects, "primary",
               width=170).grid(row=0, column=0)
        button(prow, "Save plan", self._save_plan, "ghost", width=110
               ).grid(row=0, column=1, padx=(10, 0))
        self.preview_note = ctk.CTkLabel(
            c3.body, text="", font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
            anchor="w", justify="left")
        self.preview_note.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.preview_img = ctk.CTkLabel(c3.body, text="")
        self.preview_img.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._profile_img = None

        self.add_site()

    def _build_footer(self) -> None:
        """Progress, status and log, shared by every page.

        There is no global Run button: each page starts its own job, and one
        button would have to mean something different on each of them. Stop
        stays here because there is only ever one worker to stop.
        """
        f = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        f.grid(row=2, column=0, sticky="ew")
        f.grid_columnconfigure(0, weight=1)

        self.log = ctk.CTkTextbox(f, height=140, font=T.FONT_MONO,
                                  fg_color=T.FIELD_BG, text_color=T.TEXT,
                                  border_width=1, border_color=T.BORDER,
                                  corner_radius=6, wrap="word")
        self.log.grid(row=0, column=0, columnspan=2, sticky="ew",
                      padx=16, pady=(12, 8))
        self.log.configure(state="disabled")

        self.progress = ctk.CTkProgressBar(f, height=12, corner_radius=6,
                                           progress_color=T.ACCENT,
                                           fg_color=T.SURFACE_ALT)
        self.progress.set(0.0)
        self.progress.grid(row=1, column=0, sticky="ew", padx=(16, 12),
                           pady=(0, 6))

        self.status = ctk.CTkLabel(f, text="Ready.", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.cancel_btn = button(f, "Stop", self._cancel_run, "danger", 90)
        self.cancel_btn.grid(row=1, column=1, rowspan=2, padx=(0, 16),
                             pady=(0, 12))
        self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    #  actions
    # ------------------------------------------------------------------

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select the flight folder")
        if not d:
            return
        self.flight_dir = Path(d)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(self.flight_dir))
        self._scan()

    def _scan(self) -> None:
        if not self.flight_dir:
            return
        disc = discovery.discover(self.flight_dir)
        self.discovery = disc
        self._set_found(disc.summary())

        # adopt the project/date suggested by the folder path
        guess_project, guess_date = _guess_from_path(self.flight_dir)
        for sf in self._sites:
            if not sf.project.get().strip() and guess_project:
                sf.project.insert(0, guess_project)
            if guess_date and sf.date.get().strip() == _date.today().isoformat():
                sf.date.delete(0, "end")
                sf.date.insert(0, guess_date)

        saved = plan_path(self.flight_dir)
        if saved.is_file():
            try:
                self._apply_plan(SurveyPlan.load(saved))
                self._log(f"Loaded saved transects from {PLAN_FILENAME}")
            except Exception as ex:
                self._log(f"Could not read {PLAN_FILENAME}: {ex}")

        for page in getattr(self, "pages", {}).values():
            if hasattr(page, "refresh"):
                try:
                    page.refresh()
                except Exception as ex:
                    self._log(f"{type(page).__name__}.refresh failed: {ex}")

    def _set_found(self, text: str) -> None:
        self.found.configure(state="normal")
        self.found.delete("1.0", "end")
        self.found.insert("1.0", text)
        self.found.configure(state="disabled")

    def add_site(self, site: Site | None = None) -> None:
        gp, gd = _guess_from_path(self.flight_dir) if self.flight_dir else ("", "")
        sf = SiteFrame(self.sites_holder, self._remove_site,
                       index=len(self._sites) + 1, site=site,
                       default_project=gp, default_date=gd)
        sf.grid(row=len(self._sites), column=0, sticky="ew", pady=5)
        self._sites.append(sf)

    def _remove_site(self, sf: SiteFrame) -> None:
        if len(self._sites) == 1:
            messagebox.showinfo(APP_NAME, "At least one site is needed.")
            return
        self._sites.remove(sf)
        sf.destroy()
        for i, s in enumerate(self._sites):
            s.grid(row=i, column=0, sticky="ew", pady=5)

    def _plan(self) -> SurveyPlan:
        return SurveyPlan([sf.to_site() for sf in self._sites])

    def _apply_plan(self, plan: SurveyPlan) -> None:
        for sf in self._sites:
            sf.destroy()
        self._sites.clear()
        for s in plan.sites:
            self.add_site(s)
        if not self._sites:
            self.add_site()

    def _save_plan(self) -> None:
        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        try:
            self._plan().save(plan_path(self.flight_dir, for_writing=True))
            self._log(f"Saved transects to {PLAN_FILENAME}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not save: {ex}")

    def _load_plan(self) -> None:
        start = str(self.flight_dir) if self.flight_dir else None
        p = filedialog.askopenfilename(title="Load transects",
                                       initialdir=start,
                                       filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            self._apply_plan(SurveyPlan.load(p))
            self._log(f"Loaded transects from {Path(p).name}")
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Could not load: {ex}")

    # ------------------------------------------------------------------
    #  run
    # ------------------------------------------------------------------

    def _preview_transects(self) -> None:
        """Draw the dive profile with the transects marked.

        The sanity check before imagery is imported and a card is wiped: it
        shows the times landing on the part of the dive the user believes they
        describe, rather than asking them to trust six typed digits.
        """
        from .. import depthplot
        from ..pipeline import ensure_telemetry, plan_windows
        from ..survey import utc_offset_hours

        if not self.flight_dir:
            messagebox.showinfo(APP_NAME, "Select a flight folder first.")
            return
        plan = self._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(APP_NAME, "Please fix these first:\n\n• " +
                                 "\n• ".join(errs[:10]))
            return

        flight, cfg, mode = self.flight_dir, self.cfg, self.mode
        windows = plan_windows(plan)
        try:
            off = utc_offset_hours(plan.sites[0].date_obj(), plan.timezone)
        except Exception:
            off = -7.0

        def work(progress, cancel):
            store, warns = ensure_telemetry(
                flight, cfg, windows=[(a, b) for _n, a, b in windows],
                progress=progress)
            style = (depthplot.PlotStyle() if mode == "dark"
                     else depthplot.PlotStyle.light())
            img = depthplot.render_profile(store, windows, width=980, height=300,
                                           style=style, tz_offset_hours=off)
            return ("profile", img, depthplot.transect_stats(store, windows), warns)

        self.submit(work, "Reading telemetry for the transect preview…")

    def _show_profile(self, img, stats, warns) -> None:
        """Put the rendered profile on the Flight & transects page."""
        self._profile_img = ctk.CTkImage(light_image=img, dark_image=img,
                                         size=img.size)
        self.preview_img.configure(image=self._profile_img, text="")
        bits = []
        for r in stats:
            d = (f"  {r['depth_min']:.1f}–{r['depth_max']:.1f} m"
                 if "depth_min" in r else "  no depth data")
            bits.append(f"{r['name']}: {r['seconds']/60:.1f} min{d}")
        self.preview_note.configure(text="     ".join(bits), text_color=T.TEXT)
        for w in warns:
            self._log(f"note: {w}")

    # ------------------------------------------------------------------
    #  the one worker
    # ------------------------------------------------------------------

    def submit(self, work, label: str | None = None, on_done=None) -> bool:
        """Run ``work(progress, cancel)`` on the worker thread.

        Every tab goes through here, so there is exactly one worker, one queue,
        and one place that touches widgets. Returns False if a job is already
        running rather than starting a second one -- two jobs moving the same
        files would race.
        """
        if self._worker and self._worker.is_alive():
            messagebox.showinfo(APP_NAME, "A job is already running. Wait for "
                                          "it to finish, or press Stop.")
            return False

        self._cancel.clear()
        # Called on the main thread once the job lands, so a page can refresh
        # its own widgets without touching them from the worker.
        self._on_done = on_done
        self.cancel_btn.configure(state="normal")
        self.progress.set(0.0)
        if label:
            self._log("─" * 60)
            self._log(label)

        def runner() -> None:
            try:
                out = work(
                    lambda f, m="": self._queue.put(("progress", f, m)),
                    self._cancel,
                )
                self._queue.put(("done", out))
            except Exception:
                self._queue.put(("crash", traceback.format_exc()))

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
                    self.progress.set(frac)
                    if msg:
                        self.status.configure(text=msg)
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "crash":
                    self._log("Unexpected error:\n" + item[1])
                    self._reset_buttons()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, res) -> None:
        """Report whatever the worker returned.

        A full pipeline run gets the detailed treatment; the smaller jobs the
        other tabs submit just report their own summary, so one worker can
        serve every tab without each needing its own plumbing.
        """
        self._reset_buttons()
        cb, self._on_done = getattr(self, "_on_done", None), None
        if cb is not None:
            try:
                cb(res)
            except Exception:
                self._log("on_done failed:\n" + traceback.format_exc())

        if isinstance(res, RunResult):
            self.progress.set(1.0 if res.ok else self.progress.get())
            for line in res.summary().splitlines():
                self._log(line)
            if res.cancelled:
                self.status.configure(text="Cancelled.")
            elif res.errors:
                self.status.configure(text="Finished with errors — see the log.")
            else:
                self.status.configure(text=f"Done — {len(res.outputs)} file(s).")
                self._reveal(res.outputs[0].parent if res.outputs else None)
            return

        if isinstance(res, tuple) and res and res[0] == "profile":
            _tag, img, stats, warns = res
            self.progress.set(1.0)
            self._show_profile(img, stats, warns)
            self.status.configure(text="Preview drawn — check the transects.")
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
            for w in getattr(r, "warnings", []):
                self._log(f"WARNING: {w}")
            for e in getattr(r, "errors", []):
                self._log(f"ERROR: {e}")
            opened = opened or getattr(r, "root", None) or getattr(r, "target", None)
        self.status.configure(text="Done.")
        self._reveal(opened)

    def _reveal(self, path: Path | None) -> None:
        """Open a folder in Explorer, best effort."""
        if not path:
            return
        try:
            import os
            os.startfile(Path(path))          # noqa: S606
        except Exception:
            pass

    def _reset_buttons(self) -> None:
        self.cancel_btn.configure(state="disabled")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A run is in progress. Quit anyway?"):
                return
            self._cancel.set()
        self.destroy()


def _guess_from_path(p: Path | None) -> tuple[str, str]:
    """Infer project and date from a path like .../HSIL/2025/2025_09_27_Shaw_Island."""
    if p is None:
        return "", ""
    import re
    name = p.name
    m = re.match(r"(\d{4})[_-](\d{2})[_-](\d{2})", name)
    date_s = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    project = ""
    for parent in list(p.parents)[:3]:
        if parent.name.lower() == "flights":
            break
        if not re.fullmatch(r"\d{4}", parent.name):
            project = parent.name
    return project, date_s


def main() -> None:
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
