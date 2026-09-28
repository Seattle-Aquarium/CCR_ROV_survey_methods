"""
Flight summary: the whole day, read back and put on one page.

The first tool in chapter 2, and the one to open when the boat is back on the
trailer. It reads everything the flight wrote -- the vehicle's recordings, the
topside row, the network trace, the parameter snapshots -- works out what
happened, and writes a PDF that travels with the flight folder.

**Nothing here touches the vehicle and nothing here writes into a recording.**
It is arithmetic over files that already exist, which is why it is allowed to
use the whole laptop: by the time anyone opens this page the ROV is on deck
and the 1 Hz budget that governs the monitoring page does not apply.

**The findings are shown before the PDF is opened.** A sheet that has to be
opened to find out whether anything went wrong is a sheet nobody opens on the
days it matters. The worst thing the day found goes on this page, in words,
as soon as the scan finishes.
"""

from __future__ import annotations

import os
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import flightreport, flightscan, tearsheet
from . import theme as T
from .widgets import Card, button, label, output_box


class SummaryPage(ctk.CTkFrame):
    def __init__(self, master, app, scroll: bool = True):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._report = None
        self._sheet = None

        # Built straight into this frame when a tab stacks several pages in
        # one scrolling body; a scroller inside a scroller cannot size itself.
        if scroll:
            body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
            body.grid(row=0, column=0, sticky="nsew")
        else:
            body = self
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. read the day ------------------------------------------
        c1 = Card(body, "1.  Read the day",
                  "Opens every recording, the topside logs and the parameter "
                  "snapshots in this flight's logs folder, and works out what "
                  "happened. A 5 GB day takes about a minute; nothing is "
                  "written to the vehicle and no recording is modified.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)

        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="w")
        self.run_btn = button(row, "Build the flight report", self._run,
                              "primary", width=210)
        self.run_btn.pack(side="left")
        self.open_btn = button(row, "Open the PDF", self._open_pdf, "ghost",
                               width=140)
        self.open_btn.pack(side="left", padx=8)
        self.open_btn.configure(state="disabled")
        button(row, "Read another flight…", self._pick_folder, "ghost",
               width=170).pack(side="left")

        self.note = label(c1.body, "Choose a flight folder on Monitoring, "
                                   "then press Build.", muted=True)
        self.note.grid(row=1, column=0, sticky="w", pady=(10, 0))

        # ---- 2. what it found ------------------------------------------
        c2 = Card(body, "2.  What the logs say",
                  "Ranked by weight. Everything here is in the PDF too, with "
                  "the timestamps and readings each was worked out from.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)
        self.findings = output_box(c2.body, wrap="word")
        self.findings.grid(row=0, column=0, sticky="ew")
        c2.add_grip(self.findings, self.findings.min_height)
        self._say("Nothing read yet.")

    # ------------------------------------------------------------------

    @staticmethod
    def _set(box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _say(self, text: str) -> None:
        self._set(self.findings, text)

    def _folder(self) -> Path | None:
        if getattr(self, "_chosen", None):
            return self._chosen
        return Path(self.app.flight_dir) if self.app.flight_dir else None

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose a flight folder, or its logs folder")
        if not chosen:
            return
        self._chosen = Path(chosen)
        self.note.configure(text=f"Will read {self._chosen}")

    # ------------------------------------------------------------------

    def _run(self) -> None:
        folder = self._folder()
        if folder is None:
            messagebox.showinfo(
                "No flight folder",
                "Choose a flight folder on Monitoring first, or use "
                "Read another flight to point at one.")
            return
        self._say("Reading…")
        self.note.configure(text=f"Reading {folder}…")

        def work(progress, cancel):
            day = flightscan.scan(folder, progress=progress)
            progress(0.97, "Working out what it means")
            report = flightreport.analyze(day)
            progress(0.99, "Drawing the sheet")
            return report, tearsheet.build(report)

        if not self.app.submit(work, "Reading the flight…",
                               on_done=self._done):
            messagebox.showinfo("Busy", "Another job is running.")

    def _done(self, result) -> None:
        if result is None or isinstance(result, Exception):
            self._say(f"Could not read the flight: {result}")
            self.note.configure(text="Nothing was written.")
            return
        report, sheet = result
        self._report, self._sheet = report, sheet
        self.open_btn.configure(state="normal")
        day = report.day
        self.note.configure(
            text=f"{sheet.path.name} — {sheet.pages} pages, written into "
                 f"{day.folder.name} in {sheet.seconds:.1f} s.")

        lines = [report.headline, ""]
        for finding in report.sorted_findings:
            mark = {flightreport.CRITICAL: "!!", flightreport.WARNING: " !",
                    flightreport.NOTE: "  ", flightreport.GOOD: " +"}.get(
                        finding.level, "  ")
            lines.append(f"{mark}  {finding.title}")
            if finding.detail:
                lines.append(f"      {finding.detail}")
            for evidence in finding.evidence[:4]:
                lines.append(f"        {evidence}")
            lines.append("")
        self._say("\n".join(lines))

    def _open_pdf(self) -> None:
        if self._sheet is None:
            return
        try:
            os.startfile(self._sheet.path)            # noqa: S606
        except OSError as ex:
            messagebox.showerror("Could not open it", str(ex))

    def refresh(self) -> None:
        folder = self._folder()
        if folder is not None and self._report is None:
            self.note.configure(text=f"Ready to read {folder}")
