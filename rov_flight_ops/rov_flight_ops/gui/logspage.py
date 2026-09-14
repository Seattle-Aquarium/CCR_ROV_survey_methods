"""
BlueOS logs: see what is on the Pi, download it, and clear it off.

Three sections, one job each:

1. **Files** -- search the vehicle for the types ticked, and see how many of
   each there are and how much room they take. "List individual files" opens
   the totals up into the files themselves. Anything clicked -- a whole type,
   a folder, or single files -- is selected, and the selection is what
   "Manual selection" below means.
2. **Download files** -- file types (A) crossed with a time period (B), plus
   anything selected above, copied into the flight folder: logs/mcap,
   logs/mcap_video, logs/BIN, logs/tlog, and photos/C3.
3. **Clean the Pi** -- the same two choices, deleting from the vehicle instead.

The two actions resolve their choices the same way, and say what they will
touch before they touch it -- a live line under each panel, and a
confirmation that names the vehicle. Deleting is refused while the ROV is
armed, never touches a file modified in the last couple of minutes, and keeps a
record in the flight's logs folder. See `pifiles` for why deleting is here at
all.
"""

from __future__ import annotations

import tkinter
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

from .. import pifiles as PF
from . import theme as T
from .widgets import Card, button, checkbox, entry, fit_wrap, label

PERIODS = (("All files", PF.PERIOD_ALL), ("Transects only", PF.PERIOD_TRANSECTS),
           ("Manual selection", PF.PERIOD_MANUAL))

COLUMNS = (("files", "Files", 70, "e"), ("size", "Size", 90, "e"),
           ("recorded", "Recorded (vehicle clock)", 250, "w"),
           ("covers", "Transects", 150, "w"),
           ("copied", "In flight folder", 120, "w"))

DEFAULT_ROWS = 10


def _gib(n: int) -> str:
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:,.2f} GiB"
    if n >= 2 ** 20:
        return f"{n / 2 ** 20:,.1f} MiB"
    return f"{n / 1024:,.0f} KiB"


def _when(t: float | None, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(t).strftime(fmt) if t else "?"


class LogsPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.inv: PF.Inventory | None = None
        self._nodes: dict[str, tuple[str, str, str]] = {}   # iid -> (kind, cat, prefix/path)
        self._by_path: dict[str, PF.PiFile] = {}
        self._individual = False
        #: Copy states from the last background check, and what they were for.
        self._states: dict[str, str] = {}
        self._states_for: tuple | None = None
        #: Bumped by every rebuild, so batches of rows for an old tree stop.
        self._tree_gen = 0
        self._keep: set[str] = set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self._build_files(body)
        left, right = app.pair(body, row=1, pady=(0, 4))
        self.download = ActionPanel(
            left, self, "2.  Download files",
            "Copies into the flight folder chosen on Monitoring, each type "
            "into its own folder: logs/mcap, logs/mcap_video, logs/BIN, "
            "logs/tlog, photos/C3. Files already there are skipped.",
            "Download files", danger=False, command=self._download)
        self.clean = ActionPanel(
            right, self, "3.  Clean the Pi",
            "Deletes from the vehicle. Refused while the ROV is armed; files "
            "modified in the last two minutes are left alone; a record of "
            "every deletion is kept in the flight's logs folder. It cannot be "
            "undone.",
            "Delete files", danger=True, command=self._delete)
        self._update_previews()

    # ------------------------------------------------------------------
    #  1. files
    # ------------------------------------------------------------------

    def _build_files(self, body) -> None:
        c = Card(body, "1.  Files",
                 "Tick the types, then search. Totals come first; open them "
                 "up with List individual files. Click a type, a folder or a "
                 "file to select it — click again to unselect, Shift-click for "
                 "a range. What is selected is the Manual selection below.")
        c.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)
        self.card = c

        kinds = ctk.CTkFrame(c.body, fg_color="transparent")
        kinds.grid(row=0, column=0, sticky="w")
        self.v_all = ctk.BooleanVar(value=False)
        checkbox(kinds, "All files", self.v_all, self._tick_all
                 ).grid(row=0, column=0, padx=(0, 22))
        self.v_kind: dict[str, ctk.BooleanVar] = {}
        for i, cat in enumerate(PF.CATEGORIES, start=1):
            v = ctk.BooleanVar(value=cat.key in ("mcap", "bin"))
            self.v_kind[cat.key] = v
            checkbox(kinds, cat.label, v, self._tick_one
                     ).grid(row=0, column=i, padx=(0, 22))

        acts = ctk.CTkFrame(c.body, fg_color="transparent")
        acts.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        acts.grid_columnconfigure(4, weight=1)
        button(acts, "Search for files", self._search, "primary", width=160
               ).grid(row=0, column=0)
        self.list_btn = button(acts, "List individual files", self._toggle_individual,
                               "ghost", width=170)
        self.list_btn.grid(row=0, column=1, padx=(8, 0))
        button(acts, "Clear selection", self._clear_selection, "ghost", width=130
               ).grid(row=0, column=2, padx=(8, 0))
        label(acts, "C3 folder on the Pi", muted=True).grid(row=0, column=3,
                                                          padx=(22, 8))
        self.c3_entry = entry(acts, "blank = search the vehicle for it", width=300)
        self.c3_entry.grid(row=0, column=4, sticky="ew")
        if self.app.settings.get("c3_folder"):
            self.c3_entry.insert(0, self.app.settings["c3_folder"])

        self.found_note = ctk.CTkLabel(c.body, text="Nothing searched yet.",
                                       font=T.FONT_SMALL, text_color=T.TEXT_MUTED,
                                       anchor="w", justify="left", wraplength=900)
        self.found_note.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        fit_wrap(c.body, self.found_note)

        holder = tkinter.Frame(c.body, highlightthickness=0, borderwidth=0)
        holder.grid(row=3, column=0, sticky="ew")
        holder.grid_columnconfigure(0, weight=1)
        self._tree_holder = holder
        self._style_tree()
        self.tree = ttk.Treeview(holder, style="Pi.Treeview",
                                 columns=[k for k, *_ in COLUMNS],
                                 height=DEFAULT_ROWS, selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w")
        s = T.scale_of(self)
        self.tree.column("#0", width=int(330 * s), stretch=True, anchor="w")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=int(width * s), stretch=False, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="ew")
        sb = ctk.CTkScrollbar(holder, command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewOpen>>", self._opened)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_previews())
        self.tree.bind("<Button-1>", self._click)
        # The tab scrolls as a whole; over the list, the wheel scrolls the list.
        self.tree.bind("<MouseWheel>", self._wheel)
        c.add_grip(self.tree, 4 * self._row_px_unscaled(),
                   get_height=lambda: int(self.tree.cget("height")) * self._row_px_unscaled(),
                   set_height=lambda h: self.tree.configure(
                       height=max(4, round(h / self._row_px_unscaled()))))

        self.sel_note = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                     text_color=T.TEXT, anchor="w")
        self.sel_note.grid(row=4, column=0, sticky="ew", pady=(6, 0))

    def _row_px_unscaled(self) -> int:
        return 24

    def _style_tree(self) -> None:
        mode = self._apply_appearance_mode
        s = T.scale_of(self)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")        # the one theme that honours colours
        except tkinter.TclError:
            pass
        font = (T.FAMILY, -max(1, round(12 * s)))
        style.configure("Pi.Treeview", background=mode(T.FIELD_BG),
                        fieldbackground=mode(T.FIELD_BG),
                        foreground=mode(T.TEXT), font=font, borderwidth=0,
                        rowheight=int(self._row_px_unscaled() * s))
        style.map("Pi.Treeview",
                  background=[("selected", mode(T.ACCENT))],
                  foreground=[("selected", mode(T.ACCENT_TEXT))])
        style.configure("Pi.Treeview.Heading", background=mode(T.SURFACE_ALT),
                        foreground=mode(T.TEXT),
                        font=(T.FAMILY_SEMIBOLD, -max(1, round(12 * s))),
                        relief="flat")
        self._tree_holder.configure(background=mode(T.FIELD_BG))

    def refresh_theme(self) -> None:
        self._style_tree()

    def refresh(self) -> None:
        if self.inv is not None:
            self._rebuild()
        self._update_previews()

    def forget_vehicle(self) -> None:
        """Drop a listing taken from a vehicle the address no longer points at."""
        if self.inv is None:
            return
        self.inv = None
        self._tree_gen += 1
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        self._by_path = {}
        self.found_note.configure(
            text="The vehicle address changed — search again to list this vehicle.",
            text_color=T.WARN)
        self._update_previews()

    def _tick_all(self) -> None:
        for v in self.v_kind.values():
            v.set(self.v_all.get())

    def _tick_one(self) -> None:
        self.v_all.set(all(v.get() for v in self.v_kind.values()))

    def _wheel(self, event):
        self.tree.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _click(self, event):
        """A click toggles a row rather than replacing the selection.

        Picking three files out of forty should not need Ctrl held the whole
        way; Shift-click still selects a range, and the expander still opens.
        """
        if event.state & 0x0001:                          # Shift: default range
            return None
        if self.tree.identify_element(event.x, event.y) in (
                "Treeitem.indicator", "indicator"):
            return None
        iid = self.tree.identify_row(event.y)
        if not iid or iid.endswith("::more"):
            return None
        self.tree.selection_toggle(iid)
        self.tree.focus(iid)
        return "break"

    def _clear_selection(self) -> None:
        self.tree.selection_set(())

    def _windows(self):
        from ..telemetry_cache import plan_windows
        try:
            plan = self.app._plan()
            return [] if plan.validate() else plan_windows(plan)
        except Exception:
            return []

    def _c3_folder(self) -> str:
        typed = self.c3_entry.get().strip()
        if typed != self.app.settings.get("c3_folder", ""):
            self.app.settings["c3_folder"] = typed
            self.app.save_settings()
        return typed

    def _search(self, kinds=None, then=None) -> None:
        kinds = list(kinds or [k for k, v in self.v_kind.items() if v.get()])
        if not kinds:
            messagebox.showinfo(self.app.APP_NAME, "Tick at least one file type.")
            return
        host, c3 = self.app.vehicle_host(), self._c3_folder()
        self.found_note.configure(text="Asking the vehicle…", text_color=T.TEXT_MUTED)

        def work(progress, cancel):
            return _Search(PF.search(host, kinds, c3_folder=c3,
                                     progress=progress, cancel=cancel))

        def done(res):
            if not isinstance(res, _Search):
                self.found_note.configure(text=f"Could not list the vehicle: {res}",
                                          text_color=T.WARN)
                return
            if self.inv is None or self.inv.host != res.inv.host:
                self.inv = res.inv
            else:
                self.inv.merge(res.inv)
            found_c3 = res.inv.roots.get("c3")
            if found_c3 and len(found_c3) == 1 and not self.c3_entry.get().strip():
                self.c3_entry.insert(0, found_c3[0])
                self._c3_folder()
            self._rebuild()
            if then is not None:
                self.after(50, then)

        self.app.submit(work, f"Listing {', '.join(PF.BY_KEY[k].label for k in kinds)} "
                              f"on the vehicle…", on_done=done)

    # ---- the tree ------------------------------------------------------

    def _rebuild(self) -> None:
        inv = self.inv
        if inv is None:
            return
        PF.match(inv.all_files(), self._windows())
        keep = {self._nodes.get(i, ("", "", ""))[2] for i in self.tree.selection()}
        self._keep = keep
        self._tree_gen += 1
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        self._by_path = {f.path: f for f in inv.all_files()}
        for cat in PF.CATEGORIES:
            if cat.key not in inv.files:
                continue
            iid = f"cat:{cat.key}"
            files = inv.files[cat.key]
            self._nodes[iid] = ("cat", cat.key, "")
            self.tree.insert("", "end", iid=iid, text=cat.label,
                             values=self._agg(files), open=False)
            if files:
                self.tree.insert(iid, "end", iid=f"{iid}::more", text="…")
            if self._individual and files:
                self._populate(iid)
                self.tree.item(iid, open=True)
        for iid, (_k, _c, key) in list(self._nodes.items()):
            if key and key in keep:
                self.tree.selection_add(iid)

        bits = [f"{inv.vehicle or 'Vehicle'} at {inv.host}, listed "
                f"{datetime.fromtimestamp(inv.listed_at):%H:%M:%S}"]
        if inv.skew is not None:
            bits.append(f"vehicle clock {inv.skew:+.1f} s against this laptop"
                        + ("  — WRONG, times below will not match transects"
                           if abs(inv.skew) > 120 else ""))
        text = "  ·  ".join(bits)
        for key, notes in inv.notes.items():
            for n in notes:
                text += f"\n{PF.BY_KEY[key].label}: {n}"
        if not self._windows():
            text += "\nNo valid transects on the Transects tab yet, so nothing can match 'Transects only'."
        if any(f.end_estimated for f in inv.all_files()):
            text += ("\n≈ marks an end time estimated from the file's size (a "
                     "recording with no summary). Those are never picked by "
                     "'Transects only' when cleaning the Pi.")
        self.found_note.configure(text=text, text_color=T.TEXT_MUTED)
        self._update_previews()
        self._check_copies()

    # ---- what is already in the flight folder ---------------------------

    def _check_copies(self) -> None:
        """Work out, off the window's thread, which files are already copied.

        One stat per file -- thousands for a C3 folder, on a drive that may be
        synchronising -- used to run inside every rebuild, which is every time
        this tab is shown. The tree shows "checking…" until the answer comes
        back, and an answer for a flight folder or a listing that has since
        been replaced is dropped.
        """
        inv, flight = self.inv, self.app.flight_dir
        if inv is None or not flight:
            return
        files = list(inv.all_files())

        def work() -> dict[str, str]:
            manifest = PF.load_manifest(flight)
            return {f.path: PF.copy_state(f, flight, manifest) for f in files}

        def shown(states) -> None:
            if isinstance(states, Exception):
                return
            if inv is not self.inv or flight != self.app.flight_dir:
                return
            self._states = states
            self._states_for = (flight, inv)
            self._fill_copied()

        self.app.background("logs-copies", work, shown)

    def _fill_copied(self) -> None:
        if self.inv is None:
            return
        column = len(COLUMNS) - 1
        for iid, (kind, cat, key) in list(self._nodes.items()):
            if not self.tree.exists(iid):
                continue
            if kind == "file":
                f = self._by_path.get(key)
                value = self._file_values(f)[column] if f is not None else "—"
            else:
                files = [f for f in self.inv.files.get(cat, []) if f.rel.startswith(key)]
                value = self._agg(files)[column]
            self.tree.set(iid, "copied", value)

    def _agg(self, files) -> tuple:
        size = sum(f.size for f in files)
        starts = [f.start for f in files if f.start]
        ends = [f.end for f in files if f.end]
        rec = (f"{_when(min(starts))}  →  {_when(max(ends))}" if starts and ends
               else "—")
        if any(f.end_estimated for f in files):
            rec = "≈ " + rec
        covers = sorted({c for f in files for c in f.covers})
        flight = self.app.flight_dir
        states = [self._copy_state(f) for f in files] if flight else []
        if not flight:
            copied = "—"
        elif None in states:
            copied = "checking…"
        else:
            verified = states.count("verified")
            same = states.count("same size")
            if files and verified == len(files):
                copied = "all verified"
            else:
                copied = f"{verified} verified" + (f", {same} same size" if same else "")
                copied += f" of {len(files)}"
        return (f"{len(files):,}", _gib(size), rec,
                ", ".join(covers) or "—", copied)

    def _copy_state(self, f: PF.PiFile) -> str | None:
        """The last background answer for this file, or None if there is none
        yet for this flight folder and this listing."""
        made_for = self._states_for
        # By identity: comparing two listings field by field would walk every
        # file in them, which is the cost this cache exists to avoid.
        if (made_for is None or made_for[0] != self.app.flight_dir
                or made_for[1] is not self.inv):
            return None
        return self._states.get(f.path)

    def _file_values(self, f: PF.PiFile) -> tuple:
        rec = (f"{_when(f.start, '%m-%d %H:%M:%S')} – "
               f"{'≈' if f.end_estimated else ''}{_when(f.end, '%H:%M:%S')}"
               if f.span_known else "time unknown")
        if f.category == "c3":
            rec = _when(f.modified, "%m-%d %H:%M:%S")
        state = self._copy_state(f)
        return ("", _gib(f.size), rec, ", ".join(f.covers) or "—",
                "checking…" if state is None and self.app.flight_dir else
                {"verified": "verified", "same size": "same size (unverified)",
                 "differs": "DIFFERENT SIZE"}.get(state, "—"))

    def _opened(self, _event=None) -> None:
        iid = self.tree.focus()
        if iid:
            self._populate(iid)

    def _populate(self, iid: str) -> None:
        """Insert a node's children the first time it is opened.

        Lazily, because a C3 folder is thousands of images and inserting them
        all to show three folder names would make the list crawl.
        """
        more = f"{iid}::more"
        if not self.tree.exists(more):
            return
        self.tree.delete(more)
        kind, cat, prefix = self._nodes[iid]
        files = [f for f in (self.inv.files.get(cat, []) if self.inv else [])
                 if f.rel.startswith(prefix)]
        dirs: dict[str, list[PF.PiFile]] = {}
        leaves: list[PF.PiFile] = []
        for f in files:
            rest = f.rel[len(prefix):]
            if "/" in rest:
                dirs.setdefault(rest.split("/", 1)[0], []).append(f)
            else:
                leaves.append(f)
        for name in sorted(dirs):
            child = f"dir:{cat}:{prefix}{name}/"
            self._nodes[child] = ("dir", cat, f"{prefix}{name}/")
            self.tree.insert(iid, "end", iid=child, text=f"{name}/",
                             values=self._agg(dirs[name]))
            self.tree.insert(child, "end", iid=f"{child}::more", text="…")
        self._insert_leaves(iid, cat, leaves, self._tree_gen)

    #: Rows inserted per turn of the event loop when a folder is opened.
    LEAF_BATCH = 300

    def _insert_leaves(self, iid: str, cat: str, leaves: list, gen: int) -> None:
        """A folder's files, a batch at a time, so thousands do not freeze it.

        A rebuild in the meantime makes the rest of the batches stale, and
        they stop.
        """
        if gen != self._tree_gen or not self.tree.exists(iid):
            return
        batch, rest = leaves[:self.LEAF_BATCH], leaves[self.LEAF_BATCH:]
        keep = getattr(self, "_keep", set())
        for f in batch:
            child = f"file:{f.path}"
            if self.tree.exists(child):
                continue
            self._nodes[child] = ("file", cat, f.path)
            self.tree.insert(iid, "end", iid=child, text=f.name,
                             values=self._file_values(f))
            if f.path in keep:
                self.tree.selection_add(child)
        if rest:
            self.after(1, lambda: self._insert_leaves(iid, cat, rest, gen))

    def _toggle_individual(self) -> None:
        self._individual = not self._individual
        self.list_btn.configure(text="Show totals only" if self._individual
                                else "List individual files")
        if self.inv is None:
            return
        for cat in PF.CATEGORIES:
            iid = f"cat:{cat.key}"
            if not self.tree.exists(iid):
                continue
            if self._individual:
                self._populate(iid)
            self.tree.item(iid, open=self._individual)

    def picked(self) -> list[PF.PiFile]:
        """Every file the current selection stands for."""
        if self.inv is None:
            return []
        out: dict[str, PF.PiFile] = {}
        for iid in self.tree.selection():
            node = self._nodes.get(iid)
            if node is None:
                continue
            kind, cat, key = node
            if kind == "file":
                f = self._by_path.get(key)
                if f is not None:
                    out[f.path] = f
            else:
                for f in self.inv.files.get(cat, []):
                    if f.rel.startswith(key):
                        out[f.path] = f
        return list(out.values())

    # ------------------------------------------------------------------
    #  2 and 3
    # ------------------------------------------------------------------

    def _update_previews(self) -> None:
        picked = self.picked()
        self.sel_note.configure(
            text=(f"Selected: {len(picked):,} file(s), "
                  f"{_gib(sum(f.size for f in picked))}") if picked else
            "Nothing selected.")
        for panel in (getattr(self, "download", None), getattr(self, "clean", None)):
            if panel is not None:
                panel.update_preview(self.inv, picked)

    def _resolve(self, panel: ActionPanel, retry, retried: bool) -> PF.Choice | None:
        """What a panel's choices come to, listing any type not yet listed.

        A type that has not been searched is searched first and the action
        comes back here once -- only once, so a search that was stopped or
        failed ends with a message rather than going round again.
        """
        kinds, period = panel.kinds(), panel.period()
        if period == PF.PERIOD_TRANSECTS and not self._windows():
            messagebox.showinfo(self.app.APP_NAME,
                                "'Transects only' needs the transect times — "
                                "fill them in and save them on the Transects "
                                "tab first.")
            return None
        if self.inv is not None:
            PF.match(self.inv.all_files(), self._windows())
        choice = PF.choose(self.inv, kinds, period, self.picked(),
                           destructive=panel is self.clean)
        if choice.not_listed:
            if retried:
                messagebox.showinfo(
                    self.app.APP_NAME,
                    "Could not list " + ", ".join(PF.BY_KEY[k].label
                                                  for k in choice.not_listed)
                    + " on the vehicle, so nothing was done. The log at the "
                      "foot of the window says why.")
                return None
            self._search(choice.not_listed, then=retry)
            return None
        if not choice.files:
            messagebox.showinfo(self.app.APP_NAME,
                                "Nothing to act on. " + (choice.note.capitalize()
                                                         if choice.note else
                                                         "Select files above, or "
                                                         "choose file types and a "
                                                         "time period."))
            return None
        return choice

    def _download(self, retried: bool = False) -> None:
        from .. import rovfetch

        flight = self.app.flight_dir
        if not flight:
            messagebox.showinfo(self.app.APP_NAME,
                                "Choose a flight folder on Monitoring first — "
                                "downloads are filed inside it.")
            return
        choice = self._resolve(self.download, lambda: self._download(True),
                               retried)
        if choice is None:
            return
        manifest = PF.load_manifest(flight)
        todo = [f for f in choice.files
                if PF.copy_state(f, flight, manifest) not in ("verified", "same size")]
        if not todo:
            messagebox.showinfo(self.app.APP_NAME,
                                f"All {len(choice.files)} file(s) are already in "
                                f"the flight folder.")
            return
        need = sum(f.size for f in todo)
        dest = rovfetch.inspect_destination(
            Path(flight), need_bytes=need,
            largest_file=max((f.size for f in todo), default=0))
        if dest.problems:
            messagebox.showerror(self.app.APP_NAME, "The flight folder's drive is "
                                 "not ready:\n\n• " + "\n• ".join(dest.problems))
            return
        inv = self.inv
        skip = len(choice.files) - len(todo)
        if not messagebox.askyesno(
            self.app.APP_NAME,
            f"Download {len(todo)} file(s), {_gib(need)}, from "
            f"{inv.vehicle or inv.host} into\n\n{flight}\n\n{choice.breakdown()}"
            + (f"\n\n{skip} already in the flight folder will be skipped." if skip else "")
            + "\n\nNothing on the vehicle is changed."
        ):
            return

        def work(progress, cancel):
            return PF.download(todo, Path(flight), inv.host, inv.token,
                               progress=progress, cancel=cancel,
                               vehicle=inv.vehicle)

        def done(_res):
            # New recordings are on disk: the Flight summary and Analyze tabs
            # read the folder's file list, so it is taken again now.
            self.app.refresh_files()
            self._rebuild()

        self.app.submit(work, f"Downloading {len(todo)} file(s) from the vehicle…",
                        on_done=done)

    def _delete(self, retried: bool = False) -> None:
        choice = self._resolve(self.clean, lambda: self._delete(True), retried)
        if choice is None:
            return
        inv = self.inv
        flight = self.app.flight_dir
        busy = {f.path for f in PF.protected(choice.files, inv.skew)}
        lines = [f"Permanently delete up to {len(choice.files) - len(busy)} file(s), "
                 f"{_gib(sum(f.size for f in choice.files if f.path not in busy))}, "
                 f"from {inv.vehicle or 'the vehicle'} at {inv.host}?", "",
                 choice.breakdown()]
        if flight:
            manifest = PF.load_manifest(flight)
            unverified = [f for f in choice.files
                          if PF.copy_state(f, flight, manifest) != "verified"]
            if unverified:
                lines += ["", f"{len(unverified)} of these have NO VERIFIED COPY in "
                              f"this flight folder ({Path(flight).name})."]
        else:
            lines += ["", "No flight folder is chosen, so none of these can be "
                          "checked against a downloaded copy."]
        if busy:
            lines += ["", f"{len(busy)} were modified in the last two minutes or "
                          f"have no time, and will be left alone."]
        if choice.estimated:
            lines += ["", f"{choice.estimated} recording(s) with estimated end times "
                          f"were left out of 'Transects only'."]
        lines += ["", "Before deleting, the ROV must be confirmed disarmed by a "
                      "current heartbeat, and every file is checked again on the "
                      "vehicle. This cannot be undone."]
        if not messagebox.askyesno(self.app.APP_NAME, "\n".join(lines),
                                   icon="warning", default="no"):
            return
        record = Path(flight) / "logs" if flight else None

        def work(progress, cancel):
            return PF.delete(choice.files, inv, progress=progress, cancel=cancel,
                             record_dir=record)

        def done(res):
            if isinstance(res, PF.Refused):
                messagebox.showerror(self.app.APP_NAME, str(res))
                return
            if isinstance(res, Exception):
                messagebox.showerror(self.app.APP_NAME,
                                     f"Deleting stopped with an error: {res}")
                return
            if isinstance(res, PF.TransferReport):
                gone = {f.path for f in res.done}
                for key in list(inv.files):
                    inv.files[key] = [f for f in inv.files[key] if f.path not in gone]
                self._rebuild()
                if res.stopped or res.failed or res.skipped:
                    messagebox.showwarning(self.app.APP_NAME,
                                           res.summary())

        self.app.submit(work, f"Deleting {len(choice.files)} file(s) from the "
                              f"vehicle…", on_done=done)


class ActionPanel:
    """File types (A) and a time period (B), and the button that acts on them."""

    def __init__(self, parent, page: LogsPage, title: str, subtitle: str,
                 verb: str, *, danger: bool, command):
        self.page = page
        c = Card(parent, title, subtitle)
        c.grid(row=0, column=0, sticky="nsew")
        c.body.grid_columnconfigure(0, weight=1)

        label(c.body, "(A)  File types", font=T.FONT_H2).grid(row=0, column=0,
                                                              sticky="w")
        kinds = ctk.CTkFrame(c.body, fg_color="transparent")
        kinds.grid(row=1, column=0, sticky="w", pady=(4, 10))
        self.v_kind: dict[str, ctk.BooleanVar] = {}
        for i, cat in enumerate(PF.CATEGORIES):
            v = ctk.BooleanVar(value=False)
            self.v_kind[cat.key] = v
            checkbox(kinds, cat.label, v, page._update_previews
                     ).grid(row=i // 3, column=i % 3, sticky="w",
                            padx=(0, 18), pady=2)

        label(c.body, "(B)  Time period", font=T.FONT_H2).grid(row=2, column=0,
                                                               sticky="w")
        periods = ctk.CTkFrame(c.body, fg_color="transparent")
        periods.grid(row=3, column=0, sticky="w", pady=(4, 10))
        self.v_period = ctk.StringVar(value="")
        for i, (text, value) in enumerate(PERIODS):
            ctk.CTkRadioButton(periods, text=text, value=value,
                               variable=self.v_period, font=T.FONT_BODY,
                               text_color=T.TEXT, fg_color=T.ACCENT,
                               hover_color=T.ACCENT_HOVER,
                               border_color=T.FIELD_BORDER,
                               command=page._update_previews
                               ).grid(row=0, column=i, padx=(0, 18))

        self.note = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                 text_color=T.TEXT_MUTED, anchor="w",
                                 justify="left", wraplength=480)
        self.note.grid(row=4, column=0, sticky="ew")
        fit_wrap(c.body, self.note)
        button(c.body, verb, command, "danger" if danger else "primary",
               width=160).grid(row=5, column=0, sticky="w", pady=(10, 0))

    def kinds(self) -> list[str]:
        return [k for k, v in self.v_kind.items() if v.get()]

    def period(self) -> str:
        return self.v_period.get()

    def update_preview(self, inv, picked) -> None:
        choice = PF.choose(inv, self.kinds(), self.period(), picked,
                           destructive=self is getattr(self.page, "clean", None))
        bits = []
        if choice.files:
            bits.append(f"Will use {len(choice.files):,} file(s), {_gib(choice.size)}"
                        f" — {choice.breakdown()}")
            if choice.by_hand and choice.by_rule:
                bits.append(f"({choice.by_rule} from the choices above, "
                            f"{choice.by_hand} more selected in the list)")
            elif choice.by_hand:
                bits.append("(the files selected in the list)")
        else:
            bits.append("Nothing chosen yet: select files in the list, or pick "
                        "file types and a time period.")
        if choice.not_listed:
            bits.append("Not searched yet: "
                        + ", ".join(PF.BY_KEY[k].label for k in choice.not_listed)
                        + " — they will be listed first.")
        if choice.no_time:
            bits.append(f"{choice.no_time} file(s) have no readable recording time "
                        f"and are left out of 'Transects only'.")
        if choice.estimated:
            bits.append(f"{choice.estimated} recording(s) have only an estimated end "
                        f"time and are left out of 'Transects only' here.")
        if choice.note:
            bits.append(choice.note[0].upper() + choice.note[1:] + ".")
        self.note.configure(text="\n".join(bits))


class _Search:
    def __init__(self, inv: PF.Inventory):
        self.inv = inv
        self.warnings: list[str] = []

    def summary(self) -> str:
        parts = [f"{PF.BY_KEY[k].label}: {len(v)} ({_gib(sum(f.size for f in v))})"
                 for k, v in self.inv.files.items()]
        return (f"{self.inv.vehicle or self.inv.host}: " + ", ".join(parts)
                if parts else "Nothing listed.")
