"""
Recording health: is the file broken, or was the dive broken?

Both failures reach the operator as "this transect has no depth data", and the
right response is opposite in each case:

* a recording the vehicle never closed can be repaired, and UTC already reads
  it -- but other tools (Foxglove) cannot, so a repaired copy is worth having;
* a recording the vehicle stopped filling cannot be repaired at all, and the
  only useful thing UTC can do is say so plainly instead of leaving a blank
  transect for someone to puzzle over.

The screen is two passes on purpose. The structural check reads nine bytes per
record and answers instantly, so it can run the moment a flight is opened.
Finding out *what is inside* costs a full read (about 18 s per 5 GB), so it is
a separate button rather than something the page does on its own.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import binlog, discovery
from .. import mcap_health as H
from ..ffmpeg_tools import CancelledError
from ..pipeline import cache_dir_for, plan_windows
from . import theme as T
from .widgets import Card, button, entry, label

_STATUS_COLOUR = {
    "ok": T.TEXT_MUTED,
    "truncated": T.WARN,
    "unreadable": T.WARN,
    "empty": T.TEXT_MUTED,
}


def _clock(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "--:--:--"


class HealthPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.reports: list[H.RecordingReport] = []
        self.bins: list[binlog.BinInfo] = []
        self.alignments: dict[Path, binlog.BinAlignment] = {}
        self.folder: Path | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        c1 = Card(body, "Recordings",
                  "The logs folder of a flight. UTC checks each .mcap for "
                  "damage without reading it end to end.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.path_entry = entry(row, "No folder selected", width=640)
        self.path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick, "ghost", 100).grid(row=0, column=1)
        button(row, "Check", self._quick, "primary", 100
               ).grid(row=0, column=2, padx=(8, 0))

        c2 = Card(body, "What UTC found", "")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)
        self.table = ctk.CTkFrame(c2.body, fg_color="transparent")
        self.table.grid(row=0, column=0, sticky="ew")
        self.table.grid_columnconfigure(0, weight=1)
        self.empty = label(self.table, "Choose a folder and press Check.", True)
        self.empty.grid(row=0, column=0, sticky="w")

        acts = ctk.CTkFrame(c2.body, fg_color="transparent")
        acts.grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.deep_btn = button(acts, "Check telemetry coverage", self._deep,
                               "ghost", 220)
        self.deep_btn.grid(row=0, column=0)
        self.repair_btn = button(acts, "Write repaired copies", self._repair,
                                 "primary", 200)
        self.repair_btn.grid(row=0, column=1, padx=(10, 0))
        self.hint = label(acts, "", True)
        self.hint.grid(row=0, column=2, padx=(14, 0))
        self.deep_btn.configure(state="disabled")
        self.repair_btn.configure(state="disabled")

        c4 = Card(body, "Autopilot logs (.BIN)",
                  "The flight controller writes these itself, so they survive "
                  "a companion-computer failure. Use them when a transect has "
                  "no telemetry in the mcap.")
        c4.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        c4.body.grid_columnconfigure(0, weight=1)
        self.bins_frame = ctk.CTkFrame(c4.body, fg_color="transparent")
        self.bins_frame.grid(row=0, column=0, sticky="ew")
        self.bins_frame.grid_columnconfigure(0, weight=1)
        label(self.bins_frame, "Press Check to look for .BIN logs.", True
              ).grid(row=0, column=0, sticky="w")
        bacts = ctk.CTkFrame(c4.body, fg_color="transparent")
        bacts.grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.bin_btn = button(bacts, "Use BIN for telemetry", self._use_bin,
                              "primary", 200)
        self.bin_btn.grid(row=0, column=0)
        self.bin_clear = button(bacts, "Back to mcap", self._clear_bin,
                                "ghost", 140)
        self.bin_clear.grid(row=0, column=1, padx=(10, 0))
        self.bin_hint = label(bacts, "", True)
        self.bin_hint.grid(row=0, column=2, padx=(14, 0))
        self.bin_btn.configure(state="disabled")
        self.bin_clear.configure(state="disabled")

        c3 = Card(body, "Transects",
                  "Whether the telemetry each transect needs actually exists. "
                  "Needs the coverage check above.")
        c3.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        c3.body.grid_columnconfigure(0, weight=1)
        self.verdicts = ctk.CTkFrame(c3.body, fg_color="transparent")
        self.verdicts.grid(row=0, column=0, sticky="ew")
        self.verdicts.grid_columnconfigure(0, weight=1)
        label(self.verdicts, "No coverage check run yet.", True
              ).grid(row=0, column=0, sticky="w")

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Point at the current flight's logs folder when one is opened."""
        if not self.app.flight_dir or self.path_entry.get().strip():
            return
        d = self.app.flight_dir
        disc = getattr(self.app, "discovery", None)
        guess = getattr(disc, "mcap_dir", None) or (d / "logs")
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, str(guess))

    def _pick(self) -> None:
        start = self.path_entry.get().strip() or str(self.app.flight_dir or "")
        d = filedialog.askdirectory(title="Folder of .mcap recordings",
                                    initialdir=start or None)
        if d:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, d)
            self._quick()

    def _folder(self) -> Path | None:
        raw = self.path_entry.get().strip()
        if not raw:
            messagebox.showinfo(self.app.title(), "Choose a folder first.")
            return None
        p = Path(raw)
        if not p.is_dir():
            messagebox.showinfo(self.app.title(), f"Not a folder:\n{p}")
            return None
        return p

    # ------------------------------------------------------------------

    def _quick(self) -> None:
        folder = self._folder()
        if folder is None:
            return
        paths = sorted(folder.glob("*.mcap"))
        if not paths:
            # A flight folder rather than its logs folder is the likely slip.
            paths = sorted(discovery.discover(folder).mcaps)
        if not paths:
            messagebox.showinfo(self.app.title(),
                                f"No .mcap files under:\n{folder}")
            return
        self.folder = folder
        self.reports = H.quick_scan(paths)
        self.bins = binlog.probe_bins(binlog.list_bins(folder))
        self._render()
        self._render_bins()

    def _deep(self) -> None:
        if not self.reports:
            return
        reports = [r for r in self.reports if r.readable]
        total = sum(max(1, r.size) for r in reports)
        self.app.submit(
            lambda progress, cancel: self._deep_work(reports, total,
                                                     progress, cancel),
            f"Reading {len(reports)} recording(s) to see what is inside…",
            on_done=lambda _res: self._render(),
        )

    @staticmethod
    def _deep_work(reports, total, progress, cancel):
        done = 0
        for r in reports:
            H.deep_scan(
                r, cancel=cancel,
                progress=lambda f, m="", d=done, s=max(1, r.size):
                    progress((d + f * s) / total, m))
            done += max(1, r.size)
        return _CoverageReport(reports)

    def _repair(self) -> None:
        todo = [r for r in self.reports if r.repairable]
        if not todo:
            return
        need = sum(r.health.good_end for r in todo if r.health)
        names = "\n".join(f"  {H.repaired_name(r.path)}" for r in todo)
        if not messagebox.askyesno(
            self.app.title(),
            f"Write {len(todo)} repaired copy(ies), using "
            f"{need / 1e9:.2f} GB?\n\n{names}\n\n"
            f"The original recordings are not modified."
        ):
            return
        self.app.submit(
            lambda progress, cancel: self._repair_work(todo, need,
                                                       progress, cancel),
            f"Repairing {len(todo)} recording(s)…",
            on_done=lambda _res: self._quick(),
        )

    @staticmethod
    def _repair_work(todo, need, progress, cancel):
        written, errors = [], []
        done = 0
        for r in todo:
            span = max(1, r.health.good_end if r.health else 1)
            try:
                written.append(H.repair_copy(
                    r, cancel=cancel,
                    progress=lambda f, m="", d=done, s=span:
                        progress((d + f * s) / max(1, need), m)))
            except CancelledError:
                raise
            except Exception as ex:
                errors.append(f"{r.path.name}: {ex}")
            done += span
        return _RepairReport(written, errors)


    # ------------------------------------------------------------------
    #  autopilot logs
    # ------------------------------------------------------------------

    def _cache(self) -> Path | None:
        if not self.app.flight_dir:
            return None
        return cache_dir_for(Path(self.app.flight_dir), self.app.cfg.cache_root)

    def _render_bins(self) -> None:
        for w in self.bins_frame.winfo_children():
            w.destroy()
        cache = self._cache()
        active = binlog.override_active(cache) if cache else None
        self.bin_clear.configure(state="normal" if active else "disabled")
        if not self.bins:
            label(self.bins_frame, "No .BIN logs in this folder.", True
                  ).grid(row=0, column=0, sticky="w")
            self.bin_btn.configure(state="disabled")
            self.bin_hint.configure(
                text="in use: " + Path(active["source"]).name if active else "")
            return
        for i, b in enumerate(self.bins):
            row = ctk.CTkFrame(self.bins_frame, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            for c, wd in enumerate((190, 90, 110, 320)):
                row.grid_columnconfigure(c, minsize=wd)
            label(row, b.path.name).grid(row=0, column=0, sticky="w")
            label(row, f"{b.size / 1e6:.0f} MB", True).grid(row=0, column=1, sticky="w")
            label(row, f"{b.duration / 60:.0f} min", True).grid(row=0, column=2, sticky="w")
            al = self.alignments.get(b.path)
            if al is None:
                note = "clock not yet worked out -- press Align"
            elif al.trustworthy:
                note = (f"{_clock(b.boot_first + al.offset)}"
                        f"–{_clock(b.boot_last + al.offset)}  ({al.note})")
            else:
                note = f"NOT VERIFIED -- {al.note or 'no overlapping recording'}"
            ctk.CTkLabel(row, text=note, font=T.FONT_SMALL, anchor="w",
                         text_color=T.TEXT_MUTED if (al and al.trustworthy) else T.WARN
                         ).grid(row=0, column=3, sticky="w")
        ready = [b for b in self.bins
                 if (a := self.alignments.get(b.path)) and a.trustworthy]
        self.bin_btn.configure(
            text="Use BIN for telemetry" if ready else "Align to the clock",
            state="normal" if self.bins else "disabled")
        self.bin_hint.configure(
            text=("in use: " + Path(active["source"]).name) if active
            else (f"{len(ready)} log(s) placed on the clock" if ready else ""))

    def _use_bin(self) -> None:
        ready = [b for b in self.bins
                 if (a := self.alignments.get(b.path)) and a.trustworthy]
        if not ready:
            # Align first, then carry straight on into the same action. Making
            # the operator press the button twice reads as "nothing happened".
            self._use_after_align = True
            self._align_bins()
            return
        self._use_after_align = False
        cache = self._cache()
        if cache is None:
            messagebox.showinfo(self.app.title(), "Open a flight folder first.")
            return
        best = max(ready, key=lambda b: b.duration)
        al = self.alignments[best.path]
        if not messagebox.askyesno(
            self.app.title(),
            f"Use {best.path.name} as this flight's telemetry?\n\n"
            f"{al.note}\n\n"
            f"Depth, altitude, lights, power and mode will come from the "
            f"autopilot's own log instead of the mcap. The recordings are "
            f"not modified, and 'Back to mcap' undoes this."
        ):
            return
        self.app.submit(
            lambda progress, cancel: _OverrideReport(
                binlog.write_override(cache, [best.path], al,
                                      progress=progress, cancel=cancel)),
            f"Building telemetry from {best.path.name}…",
            on_done=lambda _r: self._render_bins(),
        )

    def _align_bins(self) -> None:
        mcaps = [r.path for r in self.reports if r.readable]
        bins = [b.path for b in self.bins if b.usable]
        if not bins:
            return
        self.app.submit(
            lambda progress, cancel: self._align_work(bins, mcaps, progress),
            f"Placing {len(bins)} autopilot log(s) on the clock…",
            on_done=self._align_done,
        )

    @staticmethod
    def _align_work(bins, mcaps, progress):
        out = {}
        for i, b in enumerate(bins):
            progress(i / max(1, len(bins)), f"aligning {b.name}…")
            out[b] = binlog.align(b, mcaps)
        progress(1.0, "aligned")
        return _AlignReport(out)

    def _align_done(self, res) -> None:
        if isinstance(res, _AlignReport):
            self.alignments.update({k: v for k, v in res.found.items() if v})
        self._render_bins()
        if getattr(self, "_use_after_align", False):
            self._use_after_align = False
            ready = any((a := self.alignments.get(b.path)) and a.trustworthy
                        for b in self.bins)
            if ready:
                self.app.after(50, self._use_bin)
            else:
                messagebox.showwarning(
                    self.app.title(),
                    "None of the .BIN logs could be placed on the clock with "
                    "confidence.\n\nThis needs an mcap that overlaps the log "
                    "and recorded at least some autopilot telemetry, so the "
                    "two clocks can be matched. Check the log above for what "
                    "each file scored.")

    def _clear_bin(self) -> None:
        cache = self._cache()
        if cache is None:
            return
        if not messagebox.askyesno(
            self.app.title(),
            "Go back to reading telemetry from the mcap?\n\n"
            "The converted BIN telemetry is deleted; the .BIN files "
            "themselves are untouched."
        ):
            return
        binlog.clear_override(cache)
        self._render_bins()

    # ------------------------------------------------------------------

    def _render(self) -> None:
        for w in self.table.winfo_children():
            w.destroy()
        hdr = ctk.CTkFrame(self.table, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew")
        for i, (txt, wd) in enumerate((("recording", 300), ("size", 80),
                                       ("covers", 150), ("state", 100),
                                       ("telemetry", 220))):
            label(hdr, txt, True, font=T.FONT_SMALL).grid(
                row=0, column=i, sticky="w", padx=(0, 12))
            hdr.grid_columnconfigure(i, minsize=wd)

        for n, r in enumerate(self.reports, start=1):
            row = ctk.CTkFrame(self.table, fg_color="transparent")
            row.grid(row=n, column=0, sticky="ew", pady=1)
            gb = f"{r.size / 1e9:.2f} GB" if r.size else "--"
            h = r.health
            span = (f"{_clock(h.first_time)}–{_clock(h.last_time)}"
                    if h and h.first_time else "--")
            label(row, r.path.name).grid(row=0, column=0, sticky="w", padx=(0, 12))
            label(row, gb, True).grid(row=0, column=1, sticky="w", padx=(0, 12))
            label(row, span, True).grid(row=0, column=2, sticky="w", padx=(0, 12))
            st = ctk.CTkLabel(row, text=r.status, font=T.FONT_BODY, anchor="w",
                              text_color=_STATUS_COLOUR.get(r.status, T.TEXT))
            st.grid(row=0, column=3, sticky="w", padx=(0, 12))
            label(row, self._telemetry_cell(r), True).grid(
                row=0, column=4, sticky="w")
            for i, wd in enumerate((300, 80, 150, 100, 220)):
                row.grid_columnconfigure(i, minsize=wd)
            if r.status != "ok":
                label(row, f"     {r.headline()}", True, font=T.FONT_SMALL
                      ).grid(row=1, column=0, columnspan=5, sticky="w")

        n_bad = sum(1 for r in self.reports if r.repairable)
        self.deep_btn.configure(
            state="normal" if any(r.readable for r in self.reports) else "disabled")
        self.repair_btn.configure(state="normal" if n_bad else "disabled")
        self.hint.configure(
            text=(f"{n_bad} recording(s) can be repaired" if n_bad
                  else "nothing needs repairing"))
        self._render_verdicts()

    @staticmethod
    def _telemetry_cell(r: H.RecordingReport) -> str:
        if not r.deep:
            return "not checked"
        tel = r.groups.get(H.TELEMETRY)
        if not tel or not tel.count:
            return "none in this file"
        gap = r.telemetry_ended_early_by()
        if gap > 60:
            return f"stops {gap / 60:.0f} min early ({_clock(tel.last)})"
        return f"{_clock(tel.first)}–{_clock(tel.last)}"

    def _render_verdicts(self) -> None:
        for w in self.verdicts.winfo_children():
            w.destroy()
        if not any(r.deep for r in self.reports):
            label(self.verdicts, "No coverage check run yet.", True
                  ).grid(row=0, column=0, sticky="w")
            return
        try:
            windows = plan_windows(self.app._plan())
        except Exception:
            windows = []
        if not windows:
            label(self.verdicts, "No transects in the plan yet.", True
                  ).grid(row=0, column=0, sticky="w")
            return
        for i, v in enumerate(H.judge_windows(self.reports, windows)):
            row = ctk.CTkFrame(self.verdicts, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, minsize=70)
            row.grid_columnconfigure(1, minsize=110)
            label(row, v.name).grid(row=0, column=0, sticky="w")
            label(row, f"{_clock(v.start)}–{_clock(v.end)}", True).grid(
                row=0, column=1, sticky="w", padx=(0, 12))
            ctk.CTkLabel(row, text=v.explain(), font=T.FONT_BODY, anchor="w",
                         text_color=T.TEXT_MUTED if v.ok else T.TEXT
                         ).grid(row=0, column=2, sticky="w")


class _CoverageReport:
    """Whatever the log should say after a coverage check."""

    def __init__(self, reports):
        self.reports = reports
        self.warnings: list[str] = []
        for r in reports:
            gap = r.telemetry_ended_early_by()
            if gap > 60:
                self.warnings.append(
                    f"{r.path.name}: telemetry stops at {_clock(r.groups[H.TELEMETRY].last)} "
                    f"but the recording runs {gap / 60:.0f} min longer -- the "
                    f"vehicle stopped reporting; those numbers do not exist")
        self.errors = [f"{r.path.name}: {r.error}" for r in reports if r.error]

    def summary(self) -> str:
        out = []
        for r in self.reports:
            tel = r.groups.get(H.TELEMETRY)
            vid = r.groups.get(H.VIDEO)
            out.append(
                f"{r.path.name}: telemetry "
                + (f"{_clock(tel.first)}–{_clock(tel.last)} ({tel.count:,} msg)"
                   if tel and tel.count else "none")
                + (f", video {_clock(vid.first)}–{_clock(vid.last)}"
                   if vid and vid.count else ", no video"))
        return "\n".join(out)


class _RepairReport:
    def __init__(self, written, errors):
        self.written = written
        self.errors = errors
        self.warnings: list[str] = []
        self.target = written[0].parent if written else None

    def summary(self) -> str:
        if not self.written:
            return "No repaired copies were written."
        return "\n".join(
            [f"Wrote {len(self.written)} repaired copy(ies); the originals "
             f"were not modified."]
            + [f"  {p.name}  ({p.stat().st_size / 1e9:.2f} GB)"
               for p in self.written])


class _AlignReport:
    def __init__(self, found):
        self.found = found
        self.warnings = [
            f"{p.name}: clock not verified -- {(a.note or 'no overlapping recording')}"
            for p, a in found.items() if a and not a.trustworthy]
        self.warnings += [f"{p.name}: could not be placed on the clock"
                          for p, a in found.items() if a is None]
        self.errors: list[str] = []

    def summary(self) -> str:
        out = []
        for p, a in self.found.items():
            if a is None:
                out.append(f"{p.name}: no way to place it on the clock")
                continue
            out.append(f"{p.name}: {a.method} alignment, {a.note}"
                       + ("" if a.trustworthy else "  [UNVERIFIED]"))
        return "\n".join(out)


class _OverrideReport:
    def __init__(self, meta):
        self.meta = meta
        self.warnings = ["telemetry now comes from the autopilot log, not the "
                         "mcap -- use 'Back to mcap' to undo"]
        self.errors: list[str] = []

    def summary(self) -> str:
        m = self.meta
        return (f"Built {m['rows']:,} telemetry rows from "
                f"{Path(m['source']).name} ({m['method']} alignment"
                + (f", r={m['depth_agreement']:.4f}"
                   if m.get("depth_agreement") is not None else "") + ").")
