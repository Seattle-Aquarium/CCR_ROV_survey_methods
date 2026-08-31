"""
Bringing imagery into a flight's transect folders.

One page covers both routes, because they are the same job with a different
source:

* a **GoPro card** -- frames are *copied*, so the card keeps its originals
  until the operator chooses to reformat it;
* the flight's own **photos/GPR and photos/JPG** -- frames are *moved*, because
  they are already inside the flight and a second copy is waste.

Which one applies is decided by where the source sits, not by a toggle, so the
safe behaviour cannot be turned off by accident.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import ingest, layout
from . import theme as T
from .widgets import Card, button, entry, label


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
        self.found = ctk.CTkTextbox(c1.body, height=120, font=T.FONT_MONO,
                                    fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                    border_width=1, border_color=T.BORDER,
                                    corner_radius=6, wrap="none")
        self.found.grid(row=3, column=0, sticky="ew", pady=(10, 0))
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

        # ---- go ------------------------------------------------------
        c3 = Card(body, "3.  Import", "")
        c3.grid(row=2, column=0, sticky="ew")
        c3.body.grid_columnconfigure(0, weight=1)
        self.plan_note = ctk.CTkLabel(c3.body, text="Scan a source first.",
                                      font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                                      anchor="w", justify="left")
        self.plan_note.grid(row=0, column=0, sticky="w")
        button(c3.body, "Import now", self._go, "primary", width=150
               ).grid(row=1, column=0, sticky="w", pady=(10, 0))

    # ------------------------------------------------------------------

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
                                "Select a flight folder on the first page.")
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
        from ..pipeline import plan_windows
        plan = self.app._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(self.app.title(),
                                 "Fix the transects first:\n\n• "
                                 + "\n• ".join(errs[:8]))
            return None, None
        return plan, plan_windows(plan)

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
        self._say("\n".join(text))
        self._recount()

    def _import_options(self) -> ingest.ImportOptions:
        return ingest.ImportOptions(
            copy_gpr=bool(self.v_gpr.get()),
            copy_jpg=bool(self.v_jpg.get()),
            banner_previews=bool(self.v_banner.get()),
            include_off_transect=bool(self.v_off.get()),
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

    def _go(self) -> None:
        from .. import sorting
        from ..pipeline import ensure_telemetry

        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(),
                                "Select a flight folder on the first page.")
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

        p = ingest.plan_import(self.scan, windows, opts)
        moving = self._in_flight()
        verb = "Move" if moving else "Copy"
        extra = ("" if opts.include_off_transect else
                 f"\n\n{p.off_transect} off-transect frame(s) will NOT be "
                 f"brought across.")
        if not messagebox.askyesno(
            self.app.title(),
            f"{verb} {p.on_transect} on-transect frame(s) into "
            f"{len([n for n, c in p.per_transect.items() if c])} transect "
            f"folder(s)?\n\n{p.copy_bytes/1e9:.2f} GB{extra}"
        ):
            return

        flight, cfg, scan = self.app.flight_dir, self.app.cfg, self.scan
        style = None

        def work(progress, cancel):
            store = None
            if opts.banner_previews and opts.copy_jpg:
                progress(0.0, "reading telemetry…")
                store, _w = ensure_telemetry(
                    flight, cfg, windows=[(a, b) for _n, a, b in windows],
                    progress=lambda f, m="": progress(f * 0.2, m))
            sub = lambda f, m="": progress(0.2 + f * 0.8, m)
            if moving:
                return sorting.sort_flight(
                    flight, windows, store=store,
                    options=sorting.SortOptions(
                        move_gpr=opts.copy_gpr, move_jpg=opts.copy_jpg,
                        banner_previews=opts.banner_previews,
                        off_transect_gpr="move" if opts.include_off_transect else "keep",
                        off_transect_jpg="move" if opts.include_off_transect else "keep"),
                    progress=sub, cancel=cancel)
            return ingest.import_photos(scan, flight, windows, store=store,
                                        options=opts, style=style,
                                        progress=sub, cancel=cancel)

        self.app.submit(work, f"{verb}ing {p.on_transect} frame(s)…")
