"""
Adding and removing the telemetry banner on folders of stills.

Point at a transect, a flight, or a whole parent of flights; UTC finds every
JPG_preview / JPG_edited / JPG_edited_banner folder underneath and lets you
choose which to act on.

The rule that shapes this screen: **JPG_edited is never written to.** Those
frames feed downstream ML and must stay byte-for-byte as exported, so their
banner versions go to a JPG_edited_banner sibling. There is deliberately no "remove banner": the originals in JPG_edited were
never touched, so the un-bannered version already exists and a generated
JPG_edited_banner folder can simply be deleted. Stripping would mean a second
JPEG generation (a stamp-then-strip round trip measures ~43 dB against ~53 dB
for one pass) to recreate a file that is already sitting next to it.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import layout
from .. import photos as ph
from . import theme as T
from .widgets import Card, button, entry, label


class BannerToolsTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.root_dir: Path | None = None
        self.folders: list[layout.ImageFolder] = []
        self._checks: list[tuple[ctk.BooleanVar, layout.ImageFolder]] = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        c1 = Card(self, "Choose where to look",
                  "A transect folder, a flight folder, or a folder of flights. "
                  "UTC lists every image folder it finds underneath.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.path_entry = entry(row, "No folder selected", width=680)
        self.path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick, "primary", width=110
               ).grid(row=0, column=1)
        button(row, "Rescan", self._scan, "ghost", width=90
               ).grid(row=0, column=2, padx=(8, 0))

        c2 = Card(self, "Folders found",
                  "Tick the ones to act on. JPG_edited is written to a "
                  "JPG_edited_banner copy so the originals stay untouched.")
        c2.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)
        c2.body.grid_rowconfigure(1, weight=1)

        sel = ctk.CTkFrame(c2.body, fg_color="transparent")
        sel.grid(row=0, column=0, sticky="w", pady=(0, 6))
        button(sel, "Select all", lambda: self._set_all(True), "ghost", width=90
               ).grid(row=0, column=0)
        button(sel, "None", lambda: self._set_all(False), "ghost", width=70
               ).grid(row=0, column=1, padx=(8, 0))
        button(sel, "Only JPG_edited", lambda: self._only(layout.JPG_EDITED),
               "ghost", width=140).grid(row=0, column=2, padx=(8, 0))
        button(sel, "Only JPG_preview", lambda: self._only(layout.JPG_PREVIEW),
               "ghost", width=150).grid(row=0, column=3, padx=(8, 0))

        self.list_frame = ctk.CTkScrollableFrame(
            c2.body, fg_color=T.FIELD_BG, border_width=1,
            border_color=T.BORDER, corner_radius=6, height=260)
        self.list_frame.grid(row=1, column=0, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.empty = label(self.list_frame, "Nothing scanned yet.", muted=True)
        self.empty.grid(row=0, column=0, sticky="w", padx=8, pady=8)

        c3 = Card(self, "Action", "")
        c3.grid(row=2, column=0, sticky="ew")
        arow = ctk.CTkFrame(c3.body, fg_color="transparent")
        arow.grid(row=0, column=0, sticky="w")
        button(arow, "Add banner", self._add, "primary", width=140
               ).grid(row=0, column=0)
        self.note = ctk.CTkLabel(
            c3.body,
            text="Adding needs the flight's telemetry, so the folders must sit "
                 "inside a flight whose mcap UTC has already read.",
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w", justify="left")
        self.note.grid(row=1, column=0, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------

    def _pick(self) -> None:
        chosen = filedialog.askdirectory(title="Folder to scan for images")
        if not chosen:
            return
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, chosen)
        self._scan()

    def _scan(self) -> None:
        text = self.path_entry.get().strip()
        if not text:
            return
        root = Path(text)
        if not root.is_dir():
            messagebox.showinfo(self.app.title(), f"Not a folder: {root}")
            return
        self.root_dir = root
        self.folders = layout.find_image_folders(root)
        self._render()

    def _render(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._checks.clear()
        if not self.folders:
            label(self.list_frame,
                  "No JPG_preview, JPG_edited or JPG_edited_banner folders "
                  "found here.", muted=True
                  ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
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
            ).grid(row=r, column=0, sticky="w", padx=(24, 8), pady=1)
            self._checks.append((v, f))
            r += 1

    def _set_all(self, on: bool) -> None:
        for v, f in self._checks:
            if f.count:
                v.set(on)

    def _only(self, kind: str) -> None:
        for v, f in self._checks:
            v.set(f.count > 0 and f.kind == kind)

    def _selected(self) -> list[layout.ImageFolder]:
        return [f for v, f in self._checks if v.get()]

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
                "\n\nRun the flight through 'Sort & composite' once so UTC "
                "reads its mcap, then try again.")
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
            self.app.after(400, self._scan)
