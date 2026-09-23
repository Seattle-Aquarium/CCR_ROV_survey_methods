"""
Bringing imagery into a flight's transect folders.

One page covers both routes, because they are the same job with a different
source:

* a **GoPro card** -- frames are *copied*, so the card keeps its originals
  until the operator chooses to reformat it;
* the flight's own **photos/GPR and photos/JPG** -- frames are *moved*, because
  they are already inside the flight and a second copy is waste.

Which one applies is decided by where the source sits, not by a toggle, so the
safe behavior cannot be turned off by accident.

Bannering the folders an import has just made is the third section,
`BannerSection`, rather than a tab of its own -- see `bannertools`.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import ingest, layout
from .. import metermark as mm
from . import theme as T
from .bannertools import BannerSection
from .widgets import Card, button, entry, label, output_box, transect_error


def _zone(name: str | None):
    """The plan's timezone, or None to leave times in UTC."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


class ImportPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.scan: ingest.CardScan | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # ---- source --------------------------------------------------
        c1 = Card(body, "1.  Where are the photos?",
                  "A GoPro card, or this flight's own photos/GPR and "
                  "photos/JPG. Files on a card are copied; files already in "
                  "the flight are moved.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)

        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.src_entry = entry(row, "No source selected", width=640)
        self.src_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick, "primary", width=110
               ).grid(row=0, column=1)
        button(row, "Use this flight", self._use_flight_photos, "ghost", width=130
               ).grid(row=0, column=2, padx=(8, 0))

        drow = ctk.CTkFrame(c1.body, fg_color="transparent")
        drow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        label(drow, "Removable drives:", muted=True).grid(row=0, column=0,
                                                          padx=(0, 8))
        self.drive_row = ctk.CTkFrame(drow, fg_color="transparent")
        self.drive_row.grid(row=0, column=1, sticky="w")
        button(drow, "Refresh", self._refresh_drives, "ghost", width=90
               ).grid(row=0, column=2, padx=(10, 0))
        self._refresh_drives()

        button(c1.body, "Scan source", self._scan, "primary", width=140
               ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.found = output_box(c1.body, wrap="none")
        self.found.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        c1.add_grip(self.found, self.found.min_height)
        self._say("Nothing scanned yet.")

        # ---- options -------------------------------------------------
        c2 = Card(body, "2.  What to bring across", "")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)

        opts = ctk.CTkFrame(c2.body, fg_color="transparent")
        opts.grid(row=0, column=0, sticky="w")
        self.v_gpr = ctk.BooleanVar(value=True)
        self.v_jpg = ctk.BooleanVar(value=True)
        self.v_banner = ctk.BooleanVar(value=True)
        self.v_off = ctk.BooleanVar(value=True)
        for i, (txt, var) in enumerate((
            ("GPR raws", self.v_gpr),
            ("JPG previews", self.v_jpg),
            ("Banner the previews", self.v_banner),
        )):
            ctk.CTkCheckBox(opts, text=txt, variable=var, font=T.FONT_BODY,
                            text_color=T.TEXT, fg_color=T.ACCENT,
                            hover_color=T.ACCENT_HOVER,
                            checkmark_color=T.ACCENT_TEXT,
                            border_color=T.FIELD_BORDER, corner_radius=4,
                            command=self._recount
                            ).grid(row=0, column=i, padx=(0, 22))

        ctk.CTkCheckBox(c2.body,
                        text="Also bring frames outside every transect "
                             "(into off_transect/)",
                        variable=self.v_off, font=T.FONT_BODY,
                        text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER,
                        checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4,
                        command=self._recount
                        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ctk.CTkLabel(c2.body,
                     text="Leave this on while the card is still your only "
                          "copy — it is what makes a mistyped transect time "
                          "recoverable.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=2, column=0, sticky="w",
                                          padx=(26, 0), pady=(2, 0))

        # ---- meter marks ---------------------------------------------
        self.v_marks = ctk.BooleanVar(value=False)
        self.v_marks_csv = ctk.BooleanVar(value=True)
        self.v_marks_png = ctk.BooleanVar(value=True)
        self.v_marks_jpg = ctk.BooleanVar(value=False)

        self._check(c2.body, "Work out which frame sits at each meter",
                    self.v_marks, self._toggle_marks
                    ).grid(row=3, column=0, sticky="w", pady=(14, 0))
        ctk.CTkLabel(c2.body,
                     text="Measures each transect from the telemetry and gives "
                          "every meter its own frame — no frame used twice. A "
                          "pause adds no distance.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=4, column=0, sticky="w",
                                          padx=(26, 0), pady=(2, 0))

        self.marks_opts = ctk.CTkFrame(c2.body, fg_color="transparent")
        self.marks_opts.grid(row=5, column=0, sticky="w", padx=(26, 0),
                             pady=(6, 0))
        label(self.marks_opts, "Every", muted=True).grid(row=0, column=0)
        self.interval = entry(self.marks_opts, "1.0", width=60)
        self.interval.insert(0, "1.0")
        self.interval.grid(row=0, column=1, padx=(6, 4))
        label(self.marks_opts, "m", muted=True).grid(row=0, column=2,
                                                     padx=(0, 18))
        self._check(self.marks_opts, "Marks CSV", self.v_marks_csv
                    ).grid(row=0, column=3, padx=(0, 18))
        self._check(self.marks_opts, "Track PNG", self.v_marks_png
                    ).grid(row=0, column=4)
        self._check(self.marks_opts, "Also file edited JPGs",
                    self.v_marks_jpg).grid(row=0, column=5, padx=(18, 0))
        self.marks_run = button(self.marks_opts, "Run on this flight",
                                self._run_marks, "ghost", width=150)
        self.marks_run.grid(row=0, column=6, padx=(22, 0))
        ctk.CTkLabel(c2.body,
                     text="Run on this flight works on imagery that is already "
                          "sorted — nothing is imported and no frame is "
                          "re-copied.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=6, column=0, sticky="w",
                                          padx=(26, 0), pady=(4, 0))
        self._toggle_marks()

        # What the choices above add up to, then the button that acts on
        # them. A section of its own for one button was a section the eye had
        # to travel to after it had already finished deciding.
        self.plan_note = ctk.CTkLabel(c2.body, text="Scan a source first.",
                                      font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                                      anchor="w", justify="left")
        self.plan_note.grid(row=7, column=0, sticky="w", pady=(12, 0))
        button(c2.body, "Import now", self._go, "primary", width=150
               ).grid(row=8, column=0, sticky="w", pady=(10, 0))

        # ---- banner --------------------------------------------------
        self.banner = BannerSection(body, app)
        self.banner.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """The flight folder changed, or the tab was opened."""
        self.banner.refresh()

    def refresh_theme(self) -> None:
        self.banner.refresh_theme()

    def _check(self, master, text, var, command=None) -> ctk.CTkCheckBox:
        """A checkbox in the app's palette."""
        return ctk.CTkCheckBox(
            master, text=text, variable=var, font=T.FONT_BODY,
            text_color=T.TEXT, fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
            checkmark_color=T.ACCENT_TEXT, border_color=T.FIELD_BORDER,
            corner_radius=4, command=command or self._recount)

    def _toggle_marks(self) -> None:
        """Gray the mark settings out until marks are switched on."""
        state = "normal" if self.v_marks.get() else "disabled"
        for w in self.marks_opts.winfo_children():
            if w is getattr(self, "marks_run", None):
                continue      # always live: it is the only way to mark a
                              # flight that was sorted on an earlier day
            try:
                w.configure(state=state)
            except Exception:
                pass
        self._recount()

    def _interval_m(self) -> float | None:
        """The mark spacing, or None when what was typed is not usable."""
        try:
            v = float(self.interval.get().strip())
        except ValueError:
            return None
        return v if v > 0 else None

    def _say(self, text: str) -> None:
        self.found.configure(state="normal")
        self.found.delete("1.0", "end")
        self.found.insert("1.0", text)
        self.found.configure(state="disabled")

    def _refresh_drives(self) -> None:
        for w in self.drive_row.winfo_children():
            w.destroy()
        drives = ingest.list_drives(removable_only=True)
        if not drives:
            label(self.drive_row, "none detected", muted=True).grid(row=0, column=0)
            return
        for i, d in enumerate(drives):
            button(self.drive_row, d.caption, lambda p=d.path: self._set_src(p),
                   "ghost", width=260).grid(row=0, column=i, padx=(0, 8))

    def _set_src(self, path: Path) -> None:
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, str(path))
        self._scan()

    def _pick(self) -> None:
        chosen = filedialog.askdirectory(title="Card or folder holding the photos")
        if chosen:
            self._set_src(Path(chosen))

    def _use_flight_photos(self) -> None:
        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(),
                                "Choose a flight folder on the Flight & transects tab.")
            return
        self._set_src(layout.photos_dir(self.app.flight_dir))

    @property
    def source(self) -> Path | None:
        t = self.src_entry.get().strip()
        return Path(t) if t else None

    def _in_flight(self) -> bool:
        """True when the source already lives inside the current flight."""
        src, flight = self.source, self.app.flight_dir
        if not src or not flight:
            return False
        try:
            src.resolve().relative_to(Path(flight).resolve())
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------

    def _windows(self):
        """(plan, the windows imagery is filed by).

        Pauses are cut out of these: a frame taken during one falls inside no
        window and is handled as off-transect, which is what typing the pause
        was for. Reading telemetry is a separate question and uses the whole
        span -- see `_read_windows`.
        """
        from ..pipeline import plan_windows
        plan = self.app._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(self.app.title(), transect_error(errs))
            return None, None
        return plan, plan_windows(plan, exclude_pauses=True)

    @staticmethod
    def _read_windows(plan):
        """The spans to read telemetry over: whole transects, pauses included.

        Which recordings to open is a question about when the flight happened,
        not about what was surveyed, and a banner drawn for a frame either
        side of a pause still needs the telemetry that spans it.
        """
        from ..pipeline import plan_windows
        return [(a, b) for _n, a, b in plan_windows(plan)]

    def _scan(self) -> None:
        src = self.source
        if not src or not src.is_dir():
            messagebox.showinfo(self.app.title(), f"Not a folder: {src}")
            return
        plan, windows = self._windows()
        if plan is None:
            return
        self._say(f"Scanning {src} …")
        self.update_idletasks()
        try:
            self.scan = ingest.scan_card(src, tz_name=plan.timezone)
        except Exception as ex:
            messagebox.showerror(self.app.title(), f"Could not read it: {ex}")
            return
        text = [self.scan.summary()]
        text.append("  files are "
                    + ("MOVED (source is inside this flight)" if self._in_flight()
                       else "COPIED (source is outside the flight)"))
        for w in self.scan.warnings:
            text.append(f"  WARNING: {w}")
        # The banner's card lamp is whatever the last scan on this tab or on
        # Video actually found, so it can never be more hopeful than that.
        self.app.note_source(src, len(self.scan.frames) + len(self.scan.videos))
        self._say("\n".join(text))
        self._recount()

    def _import_options(self) -> ingest.ImportOptions:
        return ingest.ImportOptions(
            copy_gpr=bool(self.v_gpr.get()),
            copy_jpg=bool(self.v_jpg.get()),
            banner_previews=bool(self.v_banner.get()),
            include_off_transect=bool(self.v_off.get()),
            meter_marks=bool(self.v_marks.get()),
            meter_interval_m=self._interval_m() or 1.0,
            marks_csv=bool(self.v_marks_csv.get()),
            marks_png=bool(self.v_marks_png.get()),
            marks_file_jpg=bool(self.v_marks_jpg.get()),
        )

    def _recount(self) -> None:
        if self.scan is None:
            return
        plan, windows = self._windows()
        if plan is None:
            return
        p = ingest.plan_import(self.scan, windows, self._import_options())
        bits = [f"{n}: {c}" for n, c in p.per_transect.items()]
        bits.append(f"off-transect: {p.off_transect}"
                    + ("" if self.v_off.get() else " (will be left behind)"))
        self.plan_note.configure(
            text="     ".join(bits) + f"\nto copy: {p.copy_bytes/1e9:.2f} GB"
                 + (f"   skipped: {p.skip_bytes/1e9:.2f} GB" if p.skip_bytes else ""),
            text_color=T.TEXT)

    # ------------------------------------------------------------------

    def _run_marks(self) -> None:
        """Work out the marks for a flight whose imagery is already in place.

        The import path runs this at the end of a move or copy, which is no
        use to a flight that was sorted weeks ago: there is nothing left to
        import, so nothing would trigger it.
        """
        from ..pipeline import ensure_telemetry

        if not self.app.flight_dir:
            messagebox.showinfo(
                self.app.title(),
                "Choose a flight folder on the Flight & transects tab.")
            return
        plan, windows = self._windows()
        if plan is None:
            return
        if self._interval_m() is None:
            messagebox.showinfo(self.app.title(),
                                "The mark spacing must be a number of meters "
                                "greater than zero.")
            return

        opts = self._import_options()
        marked = [n for n, _a, _b in windows
                  if layout.transect_dir(self.app.flight_dir, n).is_dir()]
        if not marked:
            messagebox.showinfo(
                self.app.title(),
                "No transect folders were found under photos/transects. "
                "Import the imagery first.")
            return

        flight, cfg = self.app.flight_dir, self.app.cfg
        read_windows = self._read_windows(plan)
        tz_name = plan.timezone
        options = mm.MarkOptions(
            enabled=True,
            interval_m=opts.meter_interval_m,
            write_csv=opts.marks_csv,
            write_png=opts.marks_png,
            move_gpr=opts.marks_file_gpr,
            move_jpg=opts.marks_file_jpg,
        )

        def work(progress, cancel):
            progress(0.0, "reading telemetry…")
            store, _w = ensure_telemetry(
                flight, cfg, windows=read_windows,
                progress=lambda f, m="": progress(f * 0.3, m), cancel=cancel)
            return mm.run_for_flight(
                flight, windows, store, options, tz_name=tz_name,
                tz=_zone(tz_name),
                progress=lambda f, m="": progress(0.3 + f * 0.7, m),
                cancel=cancel)

        self.app.submit(work, f"Marking {len(marked)} transect(s)…")

    # ------------------------------------------------------------------

    def _go(self) -> None:
        from .. import sorting
        from ..pipeline import ensure_telemetry

        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(),
                                "Choose a flight folder on the Flight & transects tab.")
            return
        if self.scan is None:
            messagebox.showinfo(self.app.title(), "Scan a source first.")
            return
        plan, windows = self._windows()
        if plan is None:
            return
        opts = self._import_options()
        if not (opts.copy_gpr or opts.copy_jpg):
            messagebox.showinfo(self.app.title(), "Choose GPR, JPG, or both.")
            return
        if self.v_marks.get() and self._interval_m() is None:
            messagebox.showinfo(self.app.title(),
                                "The mark spacing must be a number of meters "
                                "greater than zero.")
            return

        p = ingest.plan_import(self.scan, windows, opts)
        moving = self._in_flight()
        verb = "Move" if moving else "Copy"
        # Spelled out rather than verb + "ing": "Move" + "ing" is "Moveing".
        doing = "Moving" if moving else "Copying"
        extra = ("" if opts.include_off_transect else
                 f"\n\n{p.off_transect} off-transect frame(s) will NOT be "
                 f"brought across.")
        if opts.meter_marks:
            outs = [n for n, on in (("a CSV", opts.marks_csv),
                                    ("a PNG", opts.marks_png)) if on]
            extra += (f"\n\nEvery {opts.meter_interval_m:g} m will then be "
                      f"given its own frame"
                      + (f", writing {' and '.join(outs)} per transect."
                         if outs else ", writing no files."))
        if not messagebox.askyesno(
            self.app.title(),
            f"{verb} {p.on_transect} on-transect frame(s) into "
            f"{len([n for n, c in p.per_transect.items() if c])} transect "
            f"folder(s)?\n\n{p.copy_bytes/1e9:.2f} GB{extra}"
        ):
            return

        flight, cfg, scan = self.app.flight_dir, self.app.cfg, self.scan
        style = None

        read_windows = self._read_windows(plan)

        def work(progress, cancel):
            store = None
            if (opts.banner_previews and opts.copy_jpg) or opts.meter_marks:
                progress(0.0, "reading telemetry…")
                store, _w = ensure_telemetry(
                    flight, cfg, windows=read_windows,
                    progress=lambda f, m="": progress(f * 0.2, m), cancel=cancel)
            sub = lambda f, m="": progress(0.2 + f * 0.8, m)
            if moving:
                return sorting.sort_flight(
                    flight, windows, store=store,
                    options=sorting.SortOptions(
                        move_gpr=opts.copy_gpr, move_jpg=opts.copy_jpg,
                        banner_previews=opts.banner_previews,
                        off_transect_gpr="move" if opts.include_off_transect else "keep",
                        off_transect_jpg="move" if opts.include_off_transect else "keep",
                        meter_marks=opts.meter_marks,
                        meter_interval_m=opts.meter_interval_m,
                        marks_csv=opts.marks_csv, marks_png=opts.marks_png,
                        marks_file_gpr=opts.marks_file_gpr,
                        marks_file_jpg=opts.marks_file_jpg),
                    progress=sub, cancel=cancel)
            return ingest.import_photos(scan, flight, windows, store=store,
                                        options=opts, style=style,
                                        progress=sub, cancel=cancel)

        self.app.submit(work, f"{doing} {p.on_transect} frame(s)…")
