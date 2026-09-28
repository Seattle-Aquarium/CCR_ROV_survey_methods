"""
Adding the telemetry banner to the folders of stills an import has just made.

The third section of Import photos rather than a tab of its own. Bannering is
a step in importing -- it acts on the folders the two sections above it have
filled, inside the flight the whole tab is already pointed at -- so it reads
the flight folder chosen on Flight & transects and lists every image folder
underneath. There is nothing left to choose a location for, which is the whole
reason the separate tab went: it asked for the same folder a second time and
then let you get it wrong.

The rule that shapes this section: **JPG_edited is never written to.** Those
frames feed downstream ML and must stay byte-for-byte as exported, so their
banner versions go to a JPG_edited_banner sibling. There is deliberately no
"remove banner": the originals in JPG_edited were never touched, so the
un-bannered version already exists and a generated JPG_edited_banner folder
can simply be deleted. Stripping would mean a second JPEG generation (a
stamp-then-strip round trip measures ~43 dB against ~53 dB for one pass) to
recreate a file that is already sitting next to it.

**The list is as tall as the list.** Walking a flight's image folders is a
disk read that can take seconds on a synced folder, so it runs off the window
thread; what comes back is drawn as rows in the section itself, which grows to
hold them and no further. A fixed-height box with its own scrollbar is what
was here before, and on the usual flight -- three transects, half a dozen
folders -- five sixths of it was empty, while the button underneath had been
pushed off the screen to make room for the emptiness.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import layout
from .. import photos as ph
from . import theme as T
from .widgets import Card, button, label


class BannerSection(Card):
    """Section 3 of Import photos: what is there to banner, and stamp it."""

    def __init__(self, master, app):
        super().__init__(
            master, "3.  Banner tools",
            "Every image folder in the flight folder chosen on Flight & "
            "transects. Tick the ones to act on. JPG_edited is written to a "
            "JPG_edited_banner copy so the originals stay untouched.")
        self.app = app
        self.root_dir: Path | None = None
        self.folders: list[layout.ImageFolder] = []
        self._checks: list[tuple[ctk.BooleanVar, layout.ImageFolder]] = []
        #: The flight the list on screen was built for, so showing the tab
        #: again does not walk the disk for an answer already displayed.
        self._scanned_for: Path | None = None
        self._scanning = False
        self.body.grid_columnconfigure(0, weight=1)

        sel = ctk.CTkFrame(self.body, fg_color="transparent")
        sel.grid(row=0, column=0, sticky="w")
        button(sel, "Rescan", self.rescan, "ghost", width=90
               ).grid(row=0, column=0)
        button(sel, "Select all", lambda: self._set_all(True), "ghost", width=90
               ).grid(row=0, column=1, padx=(8, 0))
        button(sel, "None", lambda: self._set_all(False), "ghost", width=70
               ).grid(row=0, column=2, padx=(8, 0))
        button(sel, "Only JPG_edited", lambda: self._only(layout.JPG_EDITED),
               "ghost", width=140).grid(row=0, column=3, padx=(8, 0))
        button(sel, "Only JPG_preview", lambda: self._only(layout.JPG_PREVIEW),
               "ghost", width=150).grid(row=0, column=4, padx=(8, 0))

        self.found_note = ctk.CTkLabel(
            self.body, text="Nothing scanned yet.", font=T.FONT_SMALL,
            text_color=T.TEXT_MUTED, anchor="w", justify="left")
        self.found_note.grid(row=1, column=0, sticky="ew", pady=(8, 6))

        # A plain frame, not a scrolling one: it is as tall as its rows, the
        # tab it sits on scrolls, and a scrollable frame nested inside another
        # one swallows the wheel over whichever of them has nothing to scroll.
        self.list_frame = ctk.CTkFrame(self.body, fg_color=T.FIELD_BG,
                                       border_width=1, border_color=T.BORDER,
                                       corner_radius=6)
        self.list_frame.grid(row=2, column=0, sticky="ew")
        self.list_frame.grid_columnconfigure(0, weight=1)
        label(self.list_frame, "Nothing scanned yet.", muted=True
              ).grid(row=0, column=0, sticky="w", padx=8, pady=8)

        self.sel_note = ctk.CTkLabel(self.body, text="Nothing ticked.",
                                     font=T.FONT_SMALL,
                                     text_color=T.TEXT_MUTED, anchor="w",
                                     justify="left")
        self.sel_note.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        self.add_btn = button(self.body, "Add banner", self._add, "primary",
                              width=140)
        self.add_btn.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.add_btn.configure(state="disabled")
        note = ctk.CTkLabel(
            self.body,
            text="Adding needs the flight's telemetry, so preview the "
                 "transects on Flight & transects once before stamping a "
                 "flight whose recordings have not been read yet.",
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
            justify="left", wraplength=900)
        note.grid(row=5, column=0, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------
    #  finding the folders
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rescan when the flight has changed, and not otherwise.

        Called every time the tab is shown as well as when a folder is
        chosen, and walking a flight folder is not free.
        """
        flight = getattr(self.app, "flight_dir", None)
        flight = Path(flight) if flight else None
        if flight != self._scanned_for:
            self.rescan()

    def rescan(self) -> None:
        flight = getattr(self.app, "flight_dir", None)
        if not flight:
            self.root_dir = None
            self._scanned_for = None
            self.folders = []
            self._render()
            self.found_note.configure(
                text="Choose a flight folder on Flight & transects.",
                text_color=T.TEXT_MUTED)
            return
        root = Path(flight)
        self.root_dir = root
        self._scanned_for = root
        self._scanning = True
        self.found_note.configure(text=f"Looking through {root.name} …",
                                  text_color=T.TEXT_MUTED)

        # Off the window thread: a flight folder on a synced drive holds
        # thousands of files, and walking it froze the window for seconds.
        def work():
            found = layout.find_image_folders(root)
            for f in found:
                # Everything under the flight folder belongs to this flight,
                # whether or not it sits in the photos/transects layout the
                # walker recognizes. Without this, a stray folder is reported
                # as outside a flight and then refused telemetry.
                f.flight = f.flight or root
            return found

        def shown(found) -> None:
            self._scanning = False
            if isinstance(found, Exception):
                self.folders = []
                self._render()
                self.found_note.configure(text=f"Could not read it: {found}",
                                          text_color=T.WARN)
                return
            self.folders = found
            self._render()

        self.app.background("banner-folders", work, shown)

    # ------------------------------------------------------------------
    #  the list
    # ------------------------------------------------------------------

    def _render(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._checks.clear()
        if not self.folders:
            label(self.list_frame,
                  "No JPG_preview, JPG_edited or JPG_edited_banner folders "
                  "in this flight yet.", muted=True
                  ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
            if self.root_dir is not None and not self._scanning:
                self.found_note.configure(
                    text=f"Nothing to banner in {self.root_dir.name}.",
                    text_color=T.TEXT_MUTED)
            self._update_selection()
            return

        flight_shown: str | None = None
        r = 0
        for f in self.folders:
            fl = str(f.flight) if f.flight else "(outside a flight folder)"
            if fl != flight_shown:
                flight_shown = fl
                name = Path(fl).name if f.flight else fl
                ctk.CTkLabel(self.list_frame, text=name, font=T.FONT_H2,
                             text_color=T.HEADING, anchor="w"
                             ).grid(row=r, column=0, sticky="w", padx=8,
                                    pady=(10 if r else 6, 2))
                r += 1
            v = ctk.BooleanVar(value=f.count > 0)
            text = f.label + ("   [protected — writes to a copy]"
                              if f.protected else "")
            ctk.CTkCheckBox(
                self.list_frame, text=text, variable=v, font=T.FONT_SMALL,
                text_color=T.TEXT if f.count else T.TEXT_MUTED,
                fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
                checkmark_color=T.ACCENT_TEXT, border_color=T.FIELD_BORDER,
                corner_radius=4, state="normal" if f.count else "disabled",
                command=self._update_selection,
            ).grid(row=r, column=0, sticky="w", padx=(24, 8), pady=1)
            self._checks.append((v, f))
            r += 1
        # Room under the last row, so it does not sit on the border.
        self.list_frame.grid_rowconfigure(r, minsize=8)

        transects = {f.transect for f in self.folders if f.transect}
        images = sum(f.count for f in self.folders)
        where = f" in {self.root_dir.name}" if self.root_dir else ""
        across = f" across {len(transects)} transect(s)" if transects else ""
        self.found_note.configure(
            text=f"{len(self.folders)} image folder(s){where}{across} "
                 f"— {images:,} image(s).",
            text_color=T.TEXT)
        self._update_selection()

    def _update_selection(self) -> None:
        """Say what Add banner would act on, before it is pressed."""
        chosen = self._selected()
        if not chosen:
            self.sel_note.configure(text="Nothing ticked.",
                                    text_color=T.TEXT_MUTED)
            self.add_btn.configure(state="disabled")
            return
        images = sum(f.count for f in chosen)
        copies = sum(1 for f in chosen if f.protected)
        extra = (f"; {copies} of them write to a JPG_edited_banner copy"
                 if copies else "")
        self.sel_note.configure(
            text=f"{len(chosen)} folder(s) ticked — "
                 f"{images:,} image(s){extra}.",
            text_color=T.TEXT)
        self.add_btn.configure(state="normal")

    def _set_all(self, on: bool) -> None:
        for v, f in self._checks:
            if f.count:
                v.set(on)
        self._update_selection()

    def _only(self, kind: str) -> None:
        for v, f in self._checks:
            v.set(f.count > 0 and f.kind == kind)
        self._update_selection()

    def _selected(self) -> list[layout.ImageFolder]:
        return [f for v, f in self._checks if v.get()]

    def refresh_theme(self) -> None:
        self.list_frame.configure(fg_color=T.FIELD_BG, border_color=T.BORDER)

    # ------------------------------------------------------------------

    def _tz_for(self, folder: layout.ImageFolder) -> str:
        """The timezone to read an edited frame's filename against.

        Edited exports lose their EXIF, so the name is the only clock they
        carry, and a bare local time needs a zone before it means anything. The
        flight's own plan is the authority; the configured default covers a
        folder that is not inside one.
        """
        from ..config import SyncConfig
        from ..survey import SurveyPlan, plan_path
        if folder.flight is not None:
            try:
                pp = plan_path(folder.flight)
                if pp and pp.is_file():
                    return SurveyPlan.load(pp).timezone
            except Exception:
                pass
        return SyncConfig().timezone

    def _store_for(self, folder: layout.ImageFolder):
        """Telemetry for the flight that owns this folder, or None.

        Goes through `telemetry_csv_for` rather than reaching into the cache,
        so a flight the operator switched to its autopilot log gets bannered
        from that log and not from the mcap it was switched away from.
        """
        from ..config import AppConfig
        from ..pipeline import telemetry_csv_for
        from ..telemetry import TelemetryStore
        if folder.flight is None:
            return None
        csv, source = telemetry_csv_for(folder.flight, AppConfig().cache_root)
        if csv is None:
            return None
        self._last_source = source
        return TelemetryStore.load(csv)

    def _add(self) -> None:
        chosen = self._selected()
        if not chosen:
            messagebox.showinfo(self.app.title(), "Tick at least one folder.")
            return

        jobs = []
        missing: list[str] = []
        for f in chosen:
            store = self._store_for(f)
            if store is None:
                missing.append(f.label)
                continue
            jobs.append((f, store, layout.banner_target(f)))
        if missing:
            messagebox.showwarning(
                self.app.title(),
                "No telemetry cache for:\n  " + "\n  ".join(missing[:6]) +
                "\n\nPreview the transects on Flight & transects once so the "
                "recordings are read, then try again.")
        if not jobs:
            return

        copies = [f.label for f, _, t in jobs if t != f.path]
        note = ("\n\nWriting copies to a sibling folder for:\n  "
                + "\n  ".join(copies)) if copies else ""
        # Name the source. The banner is irreversible, and the difference
        # between an mcap and the autopilot log is exactly what an operator
        # who switched sources is trying to confirm.
        src = getattr(self, "_last_source", None)
        if src:
            note = f"\n\nTelemetry source: {src}" + note
        if not messagebox.askyesno(
            self.app.title(),
            f"Add the telemetry banner to {len(jobs)} folder(s)?{note}"
        ):
            return

        def work(progress, cancel):
            out = []
            for i, (f, store, target) in enumerate(jobs):
                out.append(ph.banner_folder(
                    f.path, store, out_dir=None if target == f.path else target,
                    tz_name=self._tz_for(f),
                    progress=lambda fr, m="", i=i: progress(
                        (i + fr) / len(jobs), m),
                    cancel=cancel))
            return out

        if self.app.submit(work, f"Adding banner to {len(jobs)} folder(s)…"):
            self.app.after(400, self.rescan)
