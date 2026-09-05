"""
Video: trim the original 4K to the transects, and build composites.

Two jobs at very different speeds, deliberately on one page but chosen
separately:

* **Trim** is a stream copy -- no re-encode, nothing lost, seconds per
  transect. It gives you the untouched 4K for exactly the survey window.
* **Composite** decodes, overlays telemetry and re-encodes. Minutes to hours.

Either can be run now or months later by pointing at a flight whose footage is
already in ``videos/downward``, so a rushed field day can dump the card and
leave the slow work for a desk.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import sidebyside
from . import theme as T
from .widgets import Card, button, entry, label


class VideoPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # ---- source --------------------------------------------------
        c1 = Card(body, "1.  Where is the footage?",
                  "A GoPro card, or this flight's videos/downward. Nothing is "
                  "written to the source either way.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)
        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.src_entry = entry(row, "Defaults to this flight's videos/downward",
                               width=620)
        self.src_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick, "primary", width=110
               ).grid(row=0, column=1)
        button(row, "Use this flight", self._use_flight, "ghost", width=130
               ).grid(row=0, column=2, padx=(8, 0))

        button(c1.body, "Scan footage", self._scan, "primary", width=140
               ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.found = ctk.CTkTextbox(c1.body, height=120, font=T.FONT_MONO,
                                    fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                    border_width=1, border_color=T.BORDER,
                                    corner_radius=6, wrap="none")
        self.found.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._say("Nothing scanned yet.")

        # ---- what to make --------------------------------------------
        c2 = Card(body, "2.  What to make", "")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)

        self.v_trim = ctk.BooleanVar(value=True)
        self.v_comp = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(c2.body,
                        text="Trim the original 4K to each transect  "
                             "(videos/transects/T*/)",
                        variable=self.v_trim, font=T.FONT_BODY,
                        text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER,
                        checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4
                        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(c2.body,
                     text="Stream copy — no re-encode, nothing lost, seconds "
                          "per transect. Cuts land on the nearest keyframe, so "
                          "a clip can start up to about a second early.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=1, column=0, sticky="w",
                                          padx=(26, 0), pady=(2, 8))

        ctk.CTkCheckBox(c2.body,
                        text="Build telemetry composites  (videos/composites/)",
                        variable=self.v_comp, font=T.FONT_BODY,
                        text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER,
                        checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4,
                        command=self._toggle_comp
                        ).grid(row=2, column=0, sticky="w")

        rr = ctk.CTkFrame(c2.body, fg_color="transparent")
        rr.grid(row=3, column=0, sticky="w", padx=(26, 0), pady=(6, 0))
        self.res_vars: dict[str, ctk.BooleanVar] = {}
        self._res_boxes = []
        for i, key in enumerate(("4K", "1080p", "720p")):
            v = ctk.BooleanVar(value=(key == "1080p"))
            self.res_vars[key] = v
            b = ctk.CTkCheckBox(rr, text=key, variable=v, font=T.FONT_BODY,
                                text_color=T.TEXT, fg_color=T.ACCENT,
                                hover_color=T.ACCENT_HOVER,
                                checkmark_color=T.ACCENT_TEXT,
                                border_color=T.FIELD_BORDER, corner_radius=4)
            b.grid(row=0, column=i, padx=(0, 22))
            self._res_boxes.append(b)
        self.csv_var = ctk.BooleanVar(value=True)
        b = ctk.CTkCheckBox(rr, text="1 Hz telemetry CSV", variable=self.csv_var,
                            font=T.FONT_BODY, text_color=T.TEXT,
                            fg_color=T.ACCENT, hover_color=T.ACCENT_HOVER,
                            checkmark_color=T.ACCENT_TEXT,
                            border_color=T.FIELD_BORDER, corner_radius=4)
        b.grid(row=0, column=3, padx=(10, 0))
        self._res_boxes.append(b)
        ctk.CTkLabel(c2.body,
                     text="4K keeps 10-bit for analysis; 720p is 8-bit H.264 "
                          "for sharing. Long transects at 4K can take hours.",
                     font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
                     justify="left").grid(row=4, column=0, sticky="w",
                                          padx=(26, 0), pady=(6, 0))
        self._toggle_comp()

        # ---- go ------------------------------------------------------
        c3 = Card(body, "3.  Run", "")
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        button(c3.body, "Process video", self._go, "primary", width=160
               ).grid(row=0, column=0, sticky="w")

        self._build_clips(body)

    # ------------------------------------------------------------------
    #  short clips
    # ------------------------------------------------------------------

    def _build_clips(self, body) -> None:
        """Cutting a shareable moment out of one file.

        Separate from the transect work above because the times mean something
        different: an offset *into the chosen file*, not a TC-25 clock time.
        Mixing the two on one control would be a good way to cut the wrong
        fifteen seconds.
        """
        from .. import clips

        c = Card(body, "4.  Short clip from one video",
                 "For a talk or a post. Times are minutes:seconds into the "
                 "file you pick — not TC-25. Output goes to videos/clips/.")
        c.grid(row=3, column=0, sticky="ew")
        c.body.grid_columnconfigure(0, weight=1)

        row = ctk.CTkFrame(c.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.clip_src = entry(row, "Pick a folder of videos, or one file",
                              width=560)
        self.clip_src.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Folder…", self._clip_pick_dir, "primary", width=100
               ).grid(row=0, column=1)
        button(row, "File…", self._clip_pick_file, "ghost", width=90
               ).grid(row=0, column=2, padx=(8, 0))

        self.clip_menu = ctk.CTkOptionMenu(
            c.body, values=["— scan a folder first —"], width=560,
            font=T.FONT_BODY, text_color=T.TEXT, fg_color=T.FIELD_BG,
            button_color=T.SURFACE_ALT, button_hover_color=T.BORDER,
            dropdown_fg_color=T.SURFACE, dropdown_text_color=T.TEXT,
            command=self._clip_chosen)
        self.clip_menu.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._clip_sources: list = []

        trow = ctk.CTkFrame(c.body, fg_color="transparent")
        trow.grid(row=2, column=0, sticky="w", pady=(8, 0))
        label(trow, "start", muted=True).grid(row=0, column=0, padx=(0, 6))
        self.clip_start = entry(trow, "6:40", width=90)
        self.clip_start.grid(row=0, column=1, padx=(0, 14))
        label(trow, "end", muted=True).grid(row=0, column=2, padx=(0, 6))
        self.clip_end = entry(trow, "6:55", width=90)
        self.clip_end.grid(row=0, column=3, padx=(0, 14))
        label(trow, "name", muted=True).grid(row=0, column=4, padx=(0, 6))
        self.clip_label = entry(trow, "e.g. lingcod", width=160)
        self.clip_label.grid(row=0, column=5, padx=(0, 14))
        self.clip_note = ctk.CTkLabel(trow, text="", font=T.FONT_SMALL,
                                      text_color=T.TEXT_MUTED, anchor="w")
        self.clip_note.grid(row=0, column=6, sticky="w")
        for e in (self.clip_start, self.clip_end):
            e.bind("<KeyRelease>", lambda _e: self._clip_refresh())

        frow = ctk.CTkFrame(c.body, fg_color="transparent")
        frow.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.clip_vars: dict[str, ctk.BooleanVar] = {}
        for i, (key, fmt) in enumerate(clips.CLIP_FORMATS.items()):
            v = ctk.BooleanVar(value=(key == "1080p"))
            self.clip_vars[key] = v
            ctk.CTkCheckBox(frow, text=fmt.label, variable=v, font=T.FONT_BODY,
                            text_color=T.TEXT, fg_color=T.ACCENT,
                            hover_color=T.ACCENT_HOVER,
                            checkmark_color=T.ACCENT_TEXT,
                            border_color=T.FIELD_BORDER, corner_radius=4
                            ).grid(row=0, column=i, padx=(0, 18))
        ctk.CTkLabel(
            c.body,
            text="  •  ".join(f"{f.label}: {f.note}"
                              for f in clips.CLIP_FORMATS.values()),
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
            justify="left", wraplength=900
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))

        button(c.body, "Make clip", self._clip_go, "primary", width=140
               ).grid(row=5, column=0, sticky="w", pady=(12, 0))

        # ---- 5. two videos side by side ------------------------------
        c5 = Card(body, "5.  Two videos side by side",
                  "Compare two flights in one frame. Either side can be a "
                  "video file or a folder of mcaps (the ROV's forward "
                  "camera). Output goes to videos/composites/.")
        c5.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        c5.body.grid_columnconfigure(0, weight=1)

        self.sbs_rows = {}
        for i, which in enumerate(("left", "right")):
            r = ctk.CTkFrame(c5.body, fg_color="transparent")
            r.grid(row=i, column=0, sticky="ew", pady=(0, 6))
            r.grid_columnconfigure(1, weight=1)
            label(r, which, muted=True).grid(row=0, column=0, padx=(0, 8),
                                             sticky="w")
            src = entry(r, "a video file, or a folder of .mcap", width=460)
            src.grid(row=0, column=1, sticky="ew", padx=(0, 8))
            button(r, "Folder…", lambda w=which: self._sbs_pick(w, True),
                   "ghost", width=90).grid(row=0, column=2)
            button(r, "File…", lambda w=which: self._sbs_pick(w, False),
                   "ghost", width=80).grid(row=0, column=3, padx=(6, 0))
            label(r, "start", muted=True).grid(row=0, column=4, padx=(14, 6))
            start = entry(r, "10:02:27", width=100)
            start.grid(row=0, column=5)
            note = ctk.CTkLabel(r, text="", font=T.FONT_SMALL,
                                text_color=T.TEXT_MUTED, anchor="w")
            note.grid(row=0, column=6, sticky="w", padx=(10, 0))
            self.sbs_rows[which] = {"src": src, "start": start, "note": note}

        srow = ctk.CTkFrame(c5.body, fg_color="transparent")
        srow.grid(row=2, column=0, sticky="w", pady=(6, 0))
        label(srow, "seconds", muted=True).grid(row=0, column=0, padx=(0, 6))
        self.sbs_secs = entry(srow, "90", width=80)
        self.sbs_secs.grid(row=0, column=1, padx=(0, 16))
        self.sbs_labels = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(srow, text="caption each side", variable=self.sbs_labels,
                        font=T.FONT_BODY, text_color=T.TEXT, fg_color=T.ACCENT,
                        hover_color=T.ACCENT_HOVER,
                        checkmark_color=T.ACCENT_TEXT,
                        border_color=T.FIELD_BORDER, corner_radius=4
                        ).grid(row=0, column=2, padx=(0, 18))

        frow5 = ctk.CTkFrame(c5.body, fg_color="transparent")
        frow5.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.sbs_fmt = ctk.StringVar(value="1080p")
        for i, (key, fmt) in enumerate(sidebyside.SBS_FORMATS.items()):
            ctk.CTkRadioButton(frow5, text=fmt.label, value=key,
                               variable=self.sbs_fmt, font=T.FONT_BODY,
                               text_color=T.TEXT, fg_color=T.ACCENT,
                               hover_color=T.ACCENT_HOVER,
                               border_color=T.FIELD_BORDER
                               ).grid(row=0, column=i, padx=(0, 18))
        ctk.CTkLabel(
            c5.body,
            text=("Start is hh:mm:ss for a time of day, or m:ss for an offset "
                  "into the file. Each side keeps its own start, so two "
                  "different transects can be compared from their own "
                  "beginnings. A source with no trustworthy clock — a trim or "
                  "a composite — takes an offset only."),
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
            justify="left", wraplength=900
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))

        self.sbs_note = ctk.CTkLabel(c5.body, text="", font=T.FONT_SMALL,
                                     text_color=T.TEXT_MUTED, anchor="w",
                                     justify="left", wraplength=900)
        self.sbs_note.grid(row=5, column=0, sticky="w", pady=(6, 0))
        button(c5.body, "Build side by side", self._sbs_go, "primary",
               width=180).grid(row=6, column=0, sticky="w", pady=(12, 0))

    def _clip_pick_dir(self) -> None:
        d = filedialog.askdirectory(title="Folder of videos")
        if d:
            self._clip_scan(Path(d))

    def _clip_pick_file(self) -> None:
        f = filedialog.askopenfilename(
            title="Video file", filetypes=[("Video", "*.mp4 *.mov *.m4v")])
        if f:
            self._clip_scan(Path(f))

    def _clip_scan(self, where: Path) -> None:
        from .. import clips
        self.clip_src.delete(0, "end")
        self.clip_src.insert(0, str(where))
        self._clip_sources = clips.list_videos(where)
        if not self._clip_sources:
            self.clip_menu.configure(values=["— no video found here —"])
            self.clip_menu.set("— no video found here —")
            return
        names = [v.caption for v in self._clip_sources]
        self.clip_menu.configure(values=names)
        self.clip_menu.set(names[0])
        self._clip_refresh()

    def _clip_chosen(self, _value=None) -> None:
        self._clip_refresh()

    def _current_clip_source(self):
        cap = self.clip_menu.get()
        return next((v for v in self._clip_sources if v.caption == cap), None)

    def _clip_refresh(self) -> None:
        from .. import clips
        src = self._current_clip_source()
        if src is None:
            return
        a = clips.parse_offset(self.clip_start.get())
        b = clips.parse_offset(self.clip_end.get())
        if a is None or b is None:
            self.clip_note.configure(text="times read as m:ss or h:mm:ss",
                                     text_color=T.TEXT_MUTED)
            return
        errs = clips.validate(src, a, b)
        if errs:
            self.clip_note.configure(text=errs[0], text_color=T.WARN)
        else:
            self.clip_note.configure(text=f"{b - a:.0f}s clip", text_color=T.OK)

    def _clip_go(self) -> None:
        from .. import clips

        src = self._current_clip_source()
        if src is None:
            messagebox.showinfo(self.app.title(), "Choose a video first.")
            return
        a = clips.parse_offset(self.clip_start.get())
        b = clips.parse_offset(self.clip_end.get())
        if a is None or b is None:
            messagebox.showerror(self.app.title(),
                                 "Times are minutes:seconds into the file — "
                                 "for example 6:40.")
            return
        errs = clips.validate(src, a, b)
        if errs:
            messagebox.showerror(self.app.title(), "\n".join(errs))
            return
        chosen = [k for k, v in self.clip_vars.items() if v.get()]
        if not chosen:
            messagebox.showinfo(self.app.title(), "Choose at least one format.")
            return

        out = clips.clips_dir(src.path.parent)
        name = self.clip_label.get().strip()

        def work(progress, cancel):
            return clips.make_clip(src, a, b, out, chosen, label=name,
                                   progress=progress, cancel=cancel)

        self.app.submit(
            work,
            f"Cutting {clips.format_offset(a)}–{clips.format_offset(b)} "
            f"from {src.path.name} ({', '.join(chosen)})…")

    # ------------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.found.configure(state="normal")
        self.found.delete("1.0", "end")
        self.found.insert("1.0", text)
        self.found.configure(state="disabled")

    def _toggle_comp(self) -> None:
        state = "normal" if self.v_comp.get() else "disabled"
        for b in self._res_boxes:
            b.configure(state=state)

    def _pick(self) -> None:
        c = filedialog.askdirectory(title="Card or folder holding the video")
        if c:
            self.src_entry.delete(0, "end")
            self.src_entry.insert(0, c)
            self._scan()

    def _use_flight(self) -> None:
        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(),
                                "Select a flight folder on the first page.")
            return
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, str(Path(self.app.flight_dir) / "videos" / "downward"))
        self._scan()

    @property
    def source(self) -> Path | None:
        t = self.src_entry.get().strip()
        if t:
            return Path(t)
        if self.app.flight_dir:
            return Path(self.app.flight_dir) / "videos" / "downward"
        return None

    def _chapters(self):
        from ..pipeline import describe_chapters
        src = self.source
        if not src or not src.is_dir():
            return []
        vids = sorted(p for p in src.rglob("*")
                      if p.is_file() and p.suffix.lower() in (".mp4", ".mov"))
        # our own outputs are not source footage
        vids = [p for p in vids
                if "composites" not in p.parts and "transects" not in p.parts]
        return describe_chapters(vids)

    def _trims(self) -> dict[str, Path]:
        from .. import videoclip
        if not self.app.flight_dir:
            return {}
        return videoclip.find_trims(Path(self.app.flight_dir))

    @staticmethod
    def _describe_trims(trims: dict[str, Path]) -> str:
        """Say what will be composited, and why no timecode is shown.

        A trim is a stream copy, so it carries the source chapter's timecode --
        every trim from one recording reports the same start. They are matched
        to transects by folder name instead, which is why the timecode is not
        worth printing here.
        """
        from ..layout import transect_sort_key
        lines = [f"{len(trims)} per-transect trim(s) in videos/transects/ --"
                 f" composites will be built from these.",
                 "Matched to transects by folder name, not by timecode.", ""]
        for name in sorted(trims, key=transect_sort_key):
            f = trims[name]
            mb = f.stat().st_size / 1e6 if f.is_file() else 0
            lines.append(f"   {name:5s} {f.name}   {mb:,.0f} MB")
        return "\n".join(lines)

    def _scan(self) -> None:
        self._say("Reading timecode …")
        self.update_idletasks()
        try:
            ch = self._chapters()
        except Exception as ex:
            messagebox.showerror(self.app.title(), f"Could not read it: {ex}")
            return
        # Per-transect trims are a valid source in their own right, and a
        # flight whose full-length footage was never kept has nothing else.
        # Report them rather than saying there is no video: the composite step
        # prefers them anyway, so "nothing found" would be plainly wrong.
        trims = self._trims()
        if not ch:
            if trims:
                self._say(self._describe_trims(trims))
                return
            self._say("No video files found here, and no per-transect trims "
                      "in videos/transects/.")
            return
        if trims:
            self._say(self._describe_trims(trims))
            return
        from ..survey import format_hhmmss
        lines = [f"{len(ch)} file(s) in {self.source}"]
        for c in ch:
            tc = (format_hhmmss(c.tc_start_s) if c.tc_start_s is not None
                  else "no timecode")
            lines.append(f"   {c.path.name}  {c.duration/60:5.1f} min  "
                         f"{c.width}x{c.height}  starts {tc}")
        if any(c.tc_start_s is None for c in ch):
            lines.append("   WARNING: a file has no timecode and cannot be "
                         "placed against the transects.")
        self._say("\n".join(lines))


    # ------------------------------------------------------------------
    #  5. side by side
    # ------------------------------------------------------------------

    def _sbs_pick(self, which: str, want_dir: bool) -> None:
        row = self.sbs_rows[which]
        start = row["src"].get().strip() or str(self.app.flight_dir or "")
        if want_dir:
            p = filedialog.askdirectory(
                title=f"Folder of .mcap for the {which} side",
                initialdir=start or None)
        else:
            p = filedialog.askopenfilename(
                title=f"Video for the {which} side",
                initialdir=start or None,
                filetypes=[("Video", "*.mp4 *.mov *.mkv *.m4v"),
                           ("All files", "*.*")])
        if p:
            row["src"].delete(0, "end")
            row["src"].insert(0, p)
            row["note"].configure(text="")

    def _sbs_go(self) -> None:
        from .. import sidebyside as sbs
        from ..config import AppConfig

        picks = {}
        for which, row in self.sbs_rows.items():
            raw = row["src"].get().strip()
            if not raw:
                messagebox.showinfo(self.app.title(),
                                    f"Choose a source for the {which} side.")
                return
            picks[which] = (Path(raw), row["start"].get().strip())

        secs = sbs.parse_time(self.sbs_secs.get())
        if secs is None or secs[1] <= 0:
            messagebox.showerror(self.app.title(),
                                 "Length has to be a number of seconds, or "
                                 "m:ss — for example 90 or 1:30.")
            return
        seconds = secs[1]
        fmt = self.sbs_fmt.get()
        want_labels = bool(self.sbs_labels.get())
        cfg = AppConfig()
        flight = Path(self.app.flight_dir) if self.app.flight_dir else \
            picks["left"][0].parent
        out_dir = sbs.output_dir(flight)

        def work(progress, cancel):
            sides = {}
            for i, which in enumerate(("left", "right")):
                path, when = picks[which]
                # Probing an mcap folder builds a proxy, which is most of the
                # work; give each side half the bar.
                lo, hi = i * 0.45, i * 0.45 + 0.45

                def sub(f, m="", lo=lo, hi=hi):
                    progress(lo + (hi - lo) * f, m)

                sides[which] = sbs.probe_side(
                    path, label=_sbs_label(path, when),
                    cache_root=cfg.cache_root,
                    # A clock time lets the mcaps be narrowed before any are
                    # read, so a folder holding a whole day only decodes the
                    # recording that covers the window.
                    when=when, seconds=seconds,
                    progress=sub, cancel=cancel)
            left, right = sides["left"], sides["right"]
            in_l = left.in_point(picks["left"][1])
            in_r = right.in_point(picks["right"][1])
            return sbs.make_side_by_side(
                left, right, in_l, in_r, seconds, out_dir, fmt,
                labels=want_labels,
                progress=lambda f, m="": progress(0.9 + f * 0.1, m),
                cancel=cancel)

        self.app.submit(work, f"Building a {fmt} side-by-side…",
                        on_done=self._sbs_done)

    def _sbs_done(self, rep) -> None:
        if rep is None or isinstance(rep, Exception):
            return
        self.sbs_note.configure(text=rep.summary())

    # ------------------------------------------------------------------

    def _go(self) -> None:
        from .. import videoclip
        from ..pipeline import RunRequest, cache_dir_for
        from ..pipeline import run as run_pipeline
        from ..survey import resolve_plan

        if not self.app.flight_dir:
            messagebox.showinfo(self.app.title(),
                                "Select a flight folder on the first page.")
            return
        plan = self.app._plan()
        errs = plan.validate()
        if errs:
            messagebox.showerror(self.app.title(),
                                 "Fix the transects first:\n\n• "
                                 + "\n• ".join(errs[:8]))
            return
        do_trim = bool(self.v_trim.get())
        do_comp = bool(self.v_comp.get())
        if not (do_trim or do_comp):
            messagebox.showinfo(self.app.title(),
                                "Choose trimming, compositing, or both.")
            return
        rends = tuple(k for k, v in self.res_vars.items() if v.get())
        if do_comp and not rends:
            messagebox.showinfo(self.app.title(),
                                "Choose at least one composite resolution.")
            return

        flight, cfg = self.app.flight_dir, self.app.cfg
        write_csv = bool(self.csv_var.get())

        def work(progress, cancel):
            out = []
            share = 0.25 if (do_trim and do_comp) else 1.0
            if do_trim:
                ch = self._chapters()
                resolved = [r for r in resolve_plan(plan, ch) if r.segments]
                scratch = cache_dir_for(flight, cfg.cache_root) / "trim"
                out.append(videoclip.trim_flight(
                    flight, resolved, scratch,
                    progress=lambda f, m="": progress(f * share, m),
                    cancel=cancel))
            if do_comp:
                req = RunRequest(flight_dir=flight, plan=plan, renditions=rends,
                                 app=cfg, write_csv=write_csv)
                out.append(run_pipeline(
                    req,
                    progress=lambda f, m="": progress(share + f * (1 - share), m),
                    cancel=cancel))
            return out

        bits = []
        if do_trim:
            bits.append("trimming 4K to transects")
        if do_comp:
            bits.append(f"compositing {', '.join(rends)}")
        self.app.submit(work, " and ".join(bits).capitalize() + "…")


def _sbs_label(path: Path, when: str) -> str:
    """Name a pane after its source, so the caption says which ROV it is.

    A folder of mcaps is named by the folder (2026_08_31_mcap_Lutris ->
    Lutris); a video by its stem. The time is appended when one was given, so
    two panes from the same vehicle stay distinguishable.
    """
    base = path.stem if path.is_file() else path.name
    for chunk in ("_mcap_", "_mcap", "mcap_"):
        if chunk in base:
            base = base.split(chunk)[-1] or base
            break
    base = base.strip("_- ") or "source"
    return f"{base} {when}".strip() if when else base
