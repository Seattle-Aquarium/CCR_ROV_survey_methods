"""
Vehicle & files: pull the right recordings onto a drive you can carry home.

Three field failures came from choosing files by hand — a flight whose
covering recording was never downloaded, a file from a previous day that
looked current because BlueOS had rewritten its modification time, and a stray
from six weeks earlier. UTC already knows the transect times, so it can judge
each recording on its **recorded span** and say which transects it covers.

The page works in two halves that can be used independently. The vehicle half
asks BlueOS what it has; the destination half checks the drive is actually
able to take it, which is where the FAT32 four-gigabyte limit gets caught
before an hour is spent discovering it.

Until the vehicle's file API is confirmed, a folder can stand in as the source
— a mounted share, or last dive's logs — so the whole path is usable now.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import theme as T
from .widgets import Card, button, entry, label


class RovPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._recordings: list = []

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. the vehicle ------------------------------------------
        c1 = Card(body, "1.  The vehicle",
                  "Asks BlueOS what it is and what it has. Read-only — "
                  "nothing on the ROV is changed or deleted.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)
        r = ctk.CTkFrame(c1.body, fg_color="transparent")
        r.grid(row=0, column=0, sticky="ew")
        r.grid_columnconfigure(1, weight=1)
        label(r, "address", muted=True).grid(row=0, column=0, padx=(0, 8))
        self.host = entry(r, "leave blank to search 192.168.2.2, blueos.local",
                          width=420)
        self.host.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        button(r, "Connect", self._connect, "primary", width=110
               ).grid(row=0, column=2)
        self.vstate = ctk.CTkTextbox(c1.body, height=110, font=T.FONT_MONO,
                                     fg_color=T.FIELD_BG,
                                     text_color=T.TEXT_MUTED, border_width=1,
                                     border_color=T.BORDER, corner_radius=6,
                                     wrap="none")
        self.vstate.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._say(self.vstate, "Not connected.")

        # ---- 2. before the dive --------------------------------------
        c2a = Card(body, "2.  Before the dive",
                   "Two things worth knowing while the ROV is still on deck: "
                   "whether there is room on it for the dive you have planned, "
                   "and whether the Pi is already throttling. A recorder that "
                   "fills mid-transect does not warn anyone — it stops.")
        c2a.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2a.body.grid_columnconfigure(0, weight=1)
        r2a = ctk.CTkFrame(c2a.body, fg_color="transparent")
        r2a.grid(row=0, column=0, sticky="w")
        label(r2a, "planning to record", muted=True).grid(row=0, column=0,
                                                          padx=(0, 8))
        self.planned = entry(r2a, "e.g. 45", width=90)
        self.planned.grid(row=0, column=1)
        label(r2a, "minutes", muted=True).grid(row=0, column=2, padx=(8, 18))
        button(r2a, "Check the vehicle", self._check_ready, "primary",
               width=160).grid(row=0, column=3)
        button(r2a, "Save a snapshot", self._snapshot, "ghost", width=150
               ).grid(row=0, column=4, padx=(8, 0))
        self.rstate = ctk.CTkLabel(c2a.body, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left", wraplength=880)
        self.rstate.grid(row=1, column=0, sticky="w", pady=(10, 0))

        # ---- 3. where it goes ----------------------------------------
        c2 = Card(body, "3.  Where the recordings go",
                  "A portable SSD is the fastest route home: it skips the "
                  "upload and the re-download entirely. The folder is laid "
                  "out as flights/<date>_<site>/logs so it drops straight "
                  "into Dropbox later.")
        c2.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)
        r2 = ctk.CTkFrame(c2.body, fg_color="transparent")
        r2.grid(row=0, column=0, sticky="ew")
        r2.grid_columnconfigure(1, weight=1)
        label(r2, "drive", muted=True).grid(row=0, column=0, padx=(0, 8))
        self.dest = entry(r2, "the root of the SSD, e.g. E:\\", width=420)
        self.dest.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        button(r2, "Browse…", self._pick_dest, "ghost", width=100
               ).grid(row=0, column=2)
        button(r2, "Check", self._check_dest, "primary", width=90
               ).grid(row=0, column=3, padx=(8, 0))
        self.dstate = ctk.CTkLabel(c2.body, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left", wraplength=880)
        self.dstate.grid(row=1, column=0, sticky="w", pady=(8, 0))

        # ---- 4. which recordings -------------------------------------
        c3 = Card(body, "4.  Which recordings",
                  "Judged on the span actually recorded, never on the file "
                  "name or its date — BlueOS rewrites old recordings when it "
                  "repairs them, which is how a previous day's file comes to "
                  "look like today's.")
        c3.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(0, weight=1)
        r3 = ctk.CTkFrame(c3.body, fg_color="transparent")
        r3.grid(row=0, column=0, sticky="w")
        button(r3, "List from vehicle", self._list_vehicle, "primary", width=160
               ).grid(row=0, column=0)
        button(r3, "…or from a folder", self._list_folder, "ghost", width=150
               ).grid(row=0, column=1, padx=(8, 0))
        self.only_transects = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r3, text="only those covering my transects",
                        variable=self.only_transects, font=T.FONT_BODY,
                        text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER,
                        checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4,
                        command=self._render
                        ).grid(row=0, column=2, padx=(18, 0))
        self.listing = ctk.CTkTextbox(c3.body, height=190, font=T.FONT_MONO,
                                      fg_color=T.FIELD_BG,
                                      text_color=T.TEXT_MUTED, border_width=1,
                                      border_color=T.BORDER, corner_radius=6,
                                      wrap="none")
        self.listing.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._say(self.listing, "Nothing listed yet.")

        # ---- 5. fetch -------------------------------------------------
        c4 = Card(body, "5.  Copy", "")
        c4.grid(row=4, column=0, sticky="ew")
        button(c4.body, "Copy to the drive", self._fetch, "primary", width=180
               ).grid(row=0, column=0, sticky="w")
        self.fstate = ctk.CTkLabel(c4.body, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left", wraplength=880)
        self.fstate.grid(row=1, column=0, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------

    @staticmethod
    def _say(box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def refresh(self) -> None:
        if self.app.flight_dir and not self.dest.get().strip():
            return

    # ------------------------------------------------------------------
    #  the vehicle
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        from .. import blueos

        host = self.host.get().strip() or None
        self._say(self.vstate, "Looking for the vehicle…")
        self.update_idletasks()

        def work(progress, cancel):
            return blueos.probe(host=host, progress=progress)

        self.app.submit(work, "Asking the ROV what it has…",
                        on_done=self._connected)

    def _connected(self, rep) -> None:
        if rep is None or isinstance(rep, Exception):
            self._say(self.vstate, f"Could not reach the vehicle: {rep}")
            return
        self._probe = rep
        self._say(self.vstate, rep.report())

    # ------------------------------------------------------------------
    #  before the dive
    # ------------------------------------------------------------------

    def _planned_seconds(self) -> float:
        """How long the dive is expected to record.

        Typed minutes win; otherwise the plan's own span from the first
        transect to the last, which is roughly what arming to disarming
        writes.
        """
        raw = self.planned.get().strip()
        if raw:
            try:
                return max(0.0, float(raw)) * 60
            except ValueError:
                pass
        wins = self._windows()
        if not wins:
            return 0.0
        return max(w[2] for w in wins) - min(w[1] for w in wins)

    def _check_ready(self) -> None:
        from .. import blueos

        host = self.host.get().strip() or None
        secs = self._planned_seconds()
        self.rstate.configure(text="Asking the vehicle…",
                              text_color=T.TEXT_MUTED)

        def work(progress, cancel):
            if progress:
                progress(0.3, "reading free space and the Pi's state…")
            return blueos.check_readiness(host=host, planned_seconds=secs)

        self.app.submit(work, "Checking the vehicle…", on_done=self._ready)

    def _ready(self, r) -> None:
        if r is None or isinstance(r, Exception):
            self.rstate.configure(text=f"Could not check the vehicle: {r}",
                                  text_color=T.WARN)
            return
        self.rstate.configure(text="\n".join(r.lines()),
                              text_color=T.TEXT_MUTED if r.ok else T.WARN)

    def _snapshot(self) -> None:
        """Write the vehicle's configuration beside the flight it flew."""
        from .. import blueos

        flight = self.app.flight_dir
        if not flight:
            messagebox.showinfo(self.app.title(),
                                "Choose a flight folder first — the snapshot "
                                "is written into its logs folder.")
            return
        host = self.host.get().strip() or None
        secs = self._planned_seconds()

        def work(progress, cancel):
            return blueos.save_snapshot(Path(flight), host,
                                        planned_seconds=secs)

        self.app.submit(work, "Recording what the vehicle is…",
                        on_done=self._snapshotted)

    def _snapshotted(self, path) -> None:
        if path is None or isinstance(path, Exception):
            self.rstate.configure(text=f"Snapshot failed: {path}",
                                  text_color=T.WARN)
            return
        self.rstate.configure(text=f"Written to {path}",
                              text_color=T.TEXT_MUTED)

    # ------------------------------------------------------------------
    #  the drive
    # ------------------------------------------------------------------

    def _pick_dest(self) -> None:
        d = filedialog.askdirectory(title="The drive to copy recordings onto")
        if d:
            self.dest.delete(0, "end")
            self.dest.insert(0, d)
            self._check_dest()

    def _check_dest(self):
        from .. import rovfetch as rf

        raw = self.dest.get().strip()
        if not raw:
            self.dstate.configure(text="Choose a drive first.", text_color=T.WARN)
            return None
        need = sum(r.size for r in self._selected())
        largest = max((r.size for r in self._selected()), default=0)
        d = rf.inspect_destination(Path(raw), need_bytes=need,
                                   largest_file=largest)
        lines = [d.summary()]
        lines += [f"PROBLEM: {p}" for p in d.problems]
        lines += [f"note: {n}" for n in d.notes]
        self.dstate.configure(text="\n".join(lines),
                              text_color=T.WARN if d.problems else T.TEXT_MUTED)
        return d

    # ------------------------------------------------------------------
    #  the recordings
    # ------------------------------------------------------------------

    def _windows(self):
        from ..pipeline import plan_windows
        plan = self.app._plan()
        return plan_windows(plan) if plan else []

    def _list_vehicle(self) -> None:
        messagebox.showinfo(
            self.app.title(),
            "Listing straight from the vehicle needs the file endpoint that "
            "BlueOS actually serves, which the probe in step 1 reports.\n\n"
            "Connect to the ROV, press Connect, and send me the report — the "
            "transport drops in behind this button.\n\n"
            "In the meantime '…or from a folder' does the same job for a "
            "mounted share or a folder of recordings.")

    def _list_folder(self) -> None:
        from .. import mcap_extract
        from .. import rovfetch as rf

        d = filedialog.askdirectory(title="Folder of .mcap recordings")
        if not d:
            return
        folder = Path(d)
        paths = sorted(folder.glob("*.mcap"))
        if not paths:
            self._say(self.listing, f"No .mcap files in {folder}")
            return
        self._say(self.listing, f"Reading {len(paths)} header(s)…")
        self.update_idletasks()

        recs = []
        for info in mcap_extract.probe_mcaps(paths):
            recs.append(rf.Recording(
                name=info.path.name, size=info.path.stat().st_size,
                start=info.start, end=info.end, ref=str(info.path)))
        rf.match_transects(recs, self._windows())
        self._recordings = recs
        self._source_folder = folder
        self._render()

    def _selected(self):
        if not self._recordings:
            return []
        if not self.only_transects.get():
            return list(self._recordings)
        return [r for r in self._recordings if r.covers]

    def _render(self) -> None:
        from datetime import datetime

        if not self._recordings:
            self._say(self.listing, "Nothing listed yet.")
            return
        chosen = {r.name for r in self._selected()}
        rows = []
        for r in self._recordings:
            when = ("            ?" if not r.span_known else
                    f"{datetime.fromtimestamp(r.start):%m-%d %H:%M:%S}"
                    f"..{datetime.fromtimestamp(r.end):%H:%M:%S}")
            mark = "[x]" if r.name in chosen else "[ ]"
            cov = ", ".join(r.covers) if r.covers else "-"
            rows.append(f"{mark} {r.name:34s} {r.size / 2 ** 30:6.2f} GiB  "
                        f"{when}   {cov}")
        take = self._selected()
        rows.append("")
        rows.append(f"{len(take)} of {len(self._recordings)} selected, "
                    f"{sum(r.size for r in take) / 2 ** 30:,.2f} GiB")
        self._say(self.listing, "\n".join(rows))
        self._check_dest()

    # ------------------------------------------------------------------
    #  copying
    # ------------------------------------------------------------------

    def _fetch(self) -> None:
        from .. import rovfetch as rf

        take = self._selected()
        if not take:
            messagebox.showinfo(self.app.title(), "Nothing selected to copy.")
            return
        d = self._check_dest()
        if d is None or not d.ok:
            messagebox.showerror(
                self.app.title(),
                "The drive is not ready:\n\n• " + "\n• ".join(d.problems)
                if d else "Choose a drive first.")
            return

        plan = self.app._plan()
        site = plan.sites[0] if plan and plan.sites else None
        out = rf.flight_logs_dir(d.root,
                                 site.date if site else "",
                                 site.name if site else "")
        if not messagebox.askyesno(
            self.app.title(),
            f"Copy {len(take)} recording(s), "
            f"{sum(r.size for r in take) / 2 ** 30:,.1f} GiB, to:\n\n{out}\n\n"
            f"Nothing on the ROV is changed."
        ):
            return

        folder = getattr(self, "_source_folder", None)
        opener = rf.local_opener(folder) if folder else None
        if opener is None:
            messagebox.showinfo(self.app.title(),
                                "List the recordings first.")
            return

        def work(progress, cancel):
            return rf.fetch(take, opener, out, progress=progress, cancel=cancel)

        self.app.submit(work, f"Copying {len(take)} recording(s)…",
                        on_done=self._fetched)

    def _fetched(self, rep) -> None:
        if rep is None or isinstance(rep, Exception):
            return
        lines = [rep.summary()]
        lines += [f"FAILED {i.recording.name}: {i.detail}" for i in rep.failed]
        lines += [f"note: {w}" for w in rep.warnings]
        self.fstate.configure(text="\n".join(lines),
                              text_color=T.WARN if rep.failed else T.TEXT_MUTED)
