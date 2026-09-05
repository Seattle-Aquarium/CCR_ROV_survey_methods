"""
The desktop window.

Deliberately the same shape as tlog_to_csv.py's dialog -- add files, fill in the
site, list the transect windows, Run -- because that is the workflow the survey
team already knows. What is new is there because .mcap files forced it:

  * a recording is gigabytes rather than megabytes, so the run happens on a
    worker thread with a progress bar and a live log instead of freezing the
    window for a minute with no sign of life;
  * a folder holds a dozen recordings from one day, so each file is listed with
    the local clock time it actually covers -- which is also what the transect
    windows below have to be written in.

Built on tkinter/ttk from the standard library: nothing to install, and it
starts instantly whether run from a checkout or from the packaged .exe.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .mcap_read import PACIFIC_TZ, probe_mcaps
from .pipeline import TransectSpec, run
from .survey import load_plan
from .tide import STATIONS

log = logging.getLogger(__name__)

APP_TITLE = "MCAP to CSV - Transect Extractor"


def _config_path() -> Path:
    """Beside the user's other app data, not beside the code.

    A packaged build unpacks into a temporary directory that is deleted on exit,
    so anything written next to __file__ would be lost between runs.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "CCR_ROV" / "mcap_to_csv_config.json"


def load_config() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _icon_path() -> Path | None:
    """assets/app.ico, whether running from a checkout or a PyInstaller bundle."""
    roots = [Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parent.parent]
    for root in roots:
        if not str(root):
            continue
        p = root / "assets" / "app.ico"
        if p.is_file():
            return p
    return None


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable frame for a growing list of transect blocks."""

    def __init__(self, parent, height=220, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig(self.window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class TransectBlock:
    """One transect: an ID plus one or more start/end time windows."""

    def __init__(self, parent, index: int, on_remove):
        self.on_remove = on_remove
        self.window_rows: list[dict] = []

        self.frame = ttk.Frame(parent, relief="groove", borderwidth=1)
        self.frame.pack(fill="x", padx=4, pady=4)

        header = ttk.Frame(self.frame)
        header.pack(fill="x", padx=4, pady=(4, 0))
        self.label_var = tk.StringVar()
        ttk.Label(header, textvariable=self.label_var,
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Remove transect", command=self._remove_self).pack(side="right")

        id_row = ttk.Frame(self.frame)
        id_row.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(id_row, text="Transect ID:").pack(side="left")
        self.transect_id_var = tk.StringVar()
        ttk.Entry(id_row, textvariable=self.transect_id_var, width=30).pack(
            side="left", padx=(4, 0))
        ttk.Label(id_row, text="(used as the output CSV filename, e.g. EBM_S24_T4)",
                  foreground="grey").pack(side="left", padx=(6, 0))

        self.rows_frame = ttk.Frame(self.frame)
        self.rows_frame.pack(fill="x", padx=4)

        ttk.Button(self.frame, text="+ Add time window",
                   command=self.add_window_row).pack(anchor="w", padx=4, pady=(2, 6))

        self.set_index(index)
        self.add_window_row()

    def set_index(self, index: int) -> None:
        self.index = index
        self.label_var.set(f"Transect {index}")

    def add_window_row(self, start: str = "", end: str = "") -> None:
        row = ttk.Frame(self.rows_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Start (HH:MM:SS):").pack(side="left")
        start_var = tk.StringVar(value=start)
        ttk.Entry(row, textvariable=start_var, width=10).pack(side="left", padx=(2, 10))
        ttk.Label(row, text="End (HH:MM:SS):").pack(side="left")
        end_var = tk.StringVar(value=end)
        ttk.Entry(row, textvariable=end_var, width=10).pack(side="left", padx=(2, 10))

        entry = {"frame": row, "start_var": start_var, "end_var": end_var}

        def remove_row():
            if len(self.window_rows) <= 1:
                return                      # always keep one window row
            self.window_rows.remove(entry)
            row.destroy()

        ttk.Button(row, text="x", width=2, command=remove_row).pack(side="left")
        self.window_rows.append(entry)

    def _remove_self(self) -> None:
        self.frame.destroy()
        self.on_remove(self)

    def get_windows(self) -> list[tuple[str, str]]:
        out = []
        for w in self.window_rows:
            s, e = w["start_var"].get().strip(), w["end_var"].get().strip()
            if s and e:
                out.append((s, e))
        return out

    def get_transect_id(self) -> str:
        return self.transect_id_var.get().strip()


class App:
    def __init__(self, root: tk.Misc | None = None) -> None:
        """``root`` lets a caller supply the window.

        Only the tests use it, and for a reason worth recording: some Tcl builds
        (Anaconda's among them) cannot fully re-initialise a second interpreter
        after the first has been destroyed, so a test file that made one Tk root
        per test failed partway through with "invalid command name
        tcl_findLibrary". Handing each test a Toplevel of one shared root sides
        steps that entirely.
        """
        self.cfg = load_config()
        self.mcap_paths: list[str] = []
        self.last_dir: str = self.cfg.get("mcap_dir", "")
        self.transect_blocks: list[TransectBlock] = []
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.result = None
        #: Bumped whenever the file list changes, so a probe that finishes after
        #: the user has already moved on is discarded rather than overwriting
        #: the newer list.
        self.probe_token = 0

        self.root = tk.Tk() if root is None else root
        self.root.title(APP_TITLE)
        self.root.minsize(860, 720)
        icon = _icon_path()
        if icon:
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # One polling loop for the lifetime of the window, feeding both the file
        # probe and the run. Starting it per-task would drop whichever events
        # arrived while no loop happened to be scheduled.
        self.drain_after: str | None = self.root.after(80, self._drain)

    # -- layout ----------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 5}
        root = self.root
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text=APP_TITLE, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(14, 2), padx=14)
        ttk.Label(root,
                  text="Add .mcap file(s), fill in the site details, define transect ID(s) "
                       "and time window(s), then click Run.",
                  foreground="grey").grid(row=1, column=0, columnspan=3, pady=(0, 4))
        ttk.Separator(root, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=4)

        # ---- files ----
        ttk.Label(root, text="MCAP file(s):").grid(row=3, column=0, sticky="ne", **pad)
        files_frame = ttk.Frame(root)
        files_frame.grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        files_frame.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(files_frame, width=88, height=5, selectmode="extended",
                                  font=("Consolas", 9))
        self.listbox.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(files_frame, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

        btn_row = ttk.Frame(files_frame)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(btn_row, text="Add File(s)...",
                   command=self.add_files).pack(side="left", padx=(0, 4))
        ttk.Button(btn_row, text="Add Folder...", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Remove Selected",
                   command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Clear All", command=self.clear_files).pack(side="left", padx=4)

        self.span_var = tk.StringVar(value="")
        ttk.Label(files_frame, textvariable=self.span_var, foreground="#26568c").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Separator(root, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

        # ---- site info ----
        ttk.Label(root, text="Site name:").grid(row=5, column=0, sticky="e", **pad)
        self.site_var = tk.StringVar(value=self.cfg.get("site_name", ""))
        ttk.Entry(root, textvariable=self.site_var, width=30).grid(
            row=5, column=1, sticky="w", **pad)

        ttk.Label(root, text="Survey date (YYYYMMDD):").grid(row=6, column=0, sticky="e", **pad)
        self.date_var = tk.StringVar(value=self.cfg.get("survey_date", ""))
        ttk.Entry(root, textvariable=self.date_var, width=30).grid(
            row=6, column=1, sticky="w", **pad)

        ttk.Label(root, text="Tide station:").grid(row=7, column=0, sticky="e", **pad)
        self.station_var = tk.StringVar(value=self.cfg.get("station_display", STATIONS[0][0]))
        ttk.Combobox(root, textvariable=self.station_var, width=28, state="readonly",
                     values=[s[0] for s in STATIONS]).grid(row=7, column=1, sticky="w", **pad)

        ttk.Label(root, text="Save location (folder):").grid(row=8, column=0, sticky="e", **pad)
        self.save_var = tk.StringVar(value=self.cfg.get("save_location", ""))
        ttk.Entry(root, textvariable=self.save_var, width=52).grid(
            row=8, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self.browse_save).grid(row=8, column=2, **pad)

        self.map_var = tk.BooleanVar(value=self.cfg.get("make_map", True))
        ttk.Checkbutton(root, text="Also build a Leaflet map of these transects",
                        variable=self.map_var).grid(row=9, column=1, sticky="w", padx=10)

        ttk.Separator(root, orient="horizontal").grid(
            row=10, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

        # ---- transects ----
        ttk.Label(root, text="Transects", font=("Segoe UI", 11, "bold")).grid(
            row=11, column=0, columnspan=3, sticky="w", padx=14)
        ttk.Label(root,
                  text="Give each transect a unique ID (used as the output filename). A transect\n"
                       "may have more than one start/end window (e.g. paused and resumed later).\n"
                       "Times are local (Pacific), matching the file span shown above.",
                  foreground="grey", justify="left").grid(
            row=12, column=0, columnspan=3, sticky="w", padx=14)

        # Tall enough for two transect blocks without scrolling: a survey rarely
        # has just one, and having to scroll to see the second while typing the
        # first is where transposed time windows come from.
        self.scroll_area = ScrollableFrame(root, height=300)
        self.scroll_area.grid(row=13, column=0, columnspan=3, sticky="nsew", padx=10, pady=(4, 0))
        root.rowconfigure(13, weight=1)

        transect_buttons = ttk.Frame(root)
        transect_buttons.grid(row=14, column=0, columnspan=3, pady=(6, 4))
        ttk.Button(transect_buttons, text="+ Add Transect",
                   command=self.add_transect).pack(side="left", padx=6)
        ttk.Button(transect_buttons, text="Load plan (.json)...",
                   command=self.load_plan_file).pack(side="left", padx=6)
        self.add_transect()

        ttk.Separator(root, orient="horizontal").grid(
            row=15, column=0, columnspan=3, sticky="ew", padx=10, pady=6)

        # ---- progress + log ----
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=1000)
        self.progress.grid(row=16, column=0, columnspan=3, sticky="ew", padx=14)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.status_var, foreground="#444").grid(
            row=17, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 0))

        log_frame = ttk.Frame(root)
        log_frame.grid(row=18, column=0, columnspan=3, sticky="nsew", padx=14, pady=(4, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=6, wrap="none", font=("Consolas", 9),
                                state="disabled", background="#f6f7f9", relief="flat")
        self.log_text.grid(row=0, column=0, sticky="ew")
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set)

        btns = ttk.Frame(root)
        btns.grid(row=19, column=0, columnspan=3, pady=(8, 14))
        self.run_btn = ttk.Button(btns, text="  Run  ", command=self.on_run)
        self.run_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(btns, text="Open output folder",
                                   command=self.open_output, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        self.map_btn = ttk.Button(btns, text="Open map", command=self.open_map, state="disabled")
        self.map_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Close", command=self._on_close).pack(side="left", padx=8)

    # -- file list -------------------------------------------------------

    def refresh_listbox(self) -> None:
        """Show the file names at once, then fill in their spans in the
        background.

        Reading an mcap's summary means seeking to the end of the file, and a
        day's recordings are several gigabytes sitting in a synced folder that
        may still have to fetch them from the cloud. Doing that on the main
        thread froze the window for minutes the moment anyone picked a real logs
        directory.
        """
        self.probe_token += 1
        token = self.probe_token

        self.listbox.delete(0, tk.END)
        if not self.mcap_paths:
            self.span_var.set("")
            return
        for p in self.mcap_paths:
            self.listbox.insert(tk.END, f"{Path(p).name:<34} reading...")
        self.span_var.set(f"reading headers for {len(self.mcap_paths)} file(s)...")

        paths = [Path(p) for p in self.mcap_paths]
        threading.Thread(
            target=lambda: self.events.put(("probe", (token, probe_mcaps(paths)))),
            daemon=True,
        ).start()

    def _show_probe(self, infos: list) -> None:
        self.listbox.delete(0, tk.END)
        for info in infos:
            self.listbox.insert(tk.END, f"{info.path.name:<34} {info.local_span()}")

        good = [i for i in infos if i.usable]
        if not good:
            self.span_var.set("none of these files could be read")
            return

        start = min(i.start for i in good)
        end = max(i.end or i.start for i in good)
        a = datetime.fromtimestamp(start, timezone.utc).astimezone(PACIFIC_TZ)
        b = datetime.fromtimestamp(end, timezone.utc).astimezone(PACIFIC_TZ)
        self.span_var.set(
            f"Combined local span:  {a:%Y-%m-%d}  {a:%H:%M:%S} - {b:%H:%M:%S}   "
            f"({(b - a).total_seconds() / 60:.0f} min across {len(good)} file(s))"
        )
        if not self.date_var.get().strip():
            self.date_var.set(f"{a:%Y%m%d}")

    def add_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="Select .mcap file(s)",
            initialdir=self.last_dir or str(Path.home()),
            filetypes=[("MCAP recordings", "*.mcap"), ("All files", "*.*")],
        )
        if not files:
            return
        self.last_dir = str(Path(files[0]).parent)
        for f in files:
            if f not in self.mcap_paths:
                self.mcap_paths.append(f)
        self.refresh_listbox()

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="Select folder containing .mcap files",
            initialdir=self.last_dir or str(Path.home()))
        if not folder:
            return
        self.last_dir = folder
        found = sorted(glob.glob(os.path.join(folder, "*.mcap")))
        if not found:
            messagebox.showwarning("No mcap files", f"No .mcap files found in:\n{folder}")
            return
        for f in found:
            if f not in self.mcap_paths:
                self.mcap_paths.append(f)
        self.refresh_listbox()

    def remove_selected(self) -> None:
        for i in reversed(list(self.listbox.curselection())):
            del self.mcap_paths[i]
        self.refresh_listbox()

    def clear_files(self) -> None:
        self.mcap_paths.clear()
        self.refresh_listbox()

    def browse_save(self) -> None:
        p = filedialog.askdirectory(title="Select save location",
                                    initialdir=self.save_var.get().strip() or str(Path.home()))
        if p:
            self.save_var.set(p)

    # -- transect blocks -------------------------------------------------

    def add_transect(self) -> None:
        block = TransectBlock(self.scroll_area.inner, len(self.transect_blocks) + 1,
                              self.remove_block)
        self.transect_blocks.append(block)

    def remove_block(self, block: TransectBlock) -> None:
        self.transect_blocks.remove(block)
        for idx, b in enumerate(self.transect_blocks, start=1):
            b.set_index(idx)

    def load_plan_file(self) -> None:
        """Fill the form from a survey plan, rather than retyping it.

        This is the same JSON the UTC compositing tool reads, so one file drives
        both and the transect windows cannot drift apart between them.
        """
        path = filedialog.askopenfilename(
            title="Select a survey plan",
            initialdir=self.last_dir or str(Path.home()),
            filetypes=[("Survey plan", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            plan = load_plan(path)
        except ValueError as ex:
            messagebox.showerror("Could not read the plan", str(ex))
            return

        site = plan.sites[0]
        if len(plan.sites) > 1:
            names = ", ".join(s.name for s in plan.sites)
            if not messagebox.askyesno(
                    "Several sites in this plan",
                    f"The plan covers {len(plan.sites)} sites ({names}).\n\n"
                    f"This window handles one site at a time. Load {site.name!r}?"):
                return

        self.site_var.set(site.name)
        self.date_var.set(site.survey_date)

        for block in list(self.transect_blocks):
            block._remove_self()
        for spec in site.transects:
            self.add_transect()
            block = self.transect_blocks[-1]
            block.transect_id_var.set(spec.transect_id)
            first, *rest = spec.windows
            block.window_rows[0]["start_var"].set(first[0])
            block.window_rows[0]["end_var"].set(first[1])
            for start, end in rest:
                block.add_window_row(start, end)
        if not self.transect_blocks:
            self.add_transect()

        notes = "\n".join(f"- {w}" for w in plan.warnings)
        message = f"Loaded {site}."
        if notes:
            messagebox.showwarning("Plan loaded with notes", f"{message}\n\n{notes}")
        else:
            self.status_var.set(message)

    # -- validation ------------------------------------------------------

    def _collect(self) -> dict | None:
        if not self.mcap_paths:
            messagebox.showerror("Missing input", "Please add at least one .mcap file.")
            return None
        for p in self.mcap_paths:
            if not Path(p).is_file():
                messagebox.showerror("File not found", f"MCAP file not found:\n{p}")
                return None

        site_name = self.site_var.get().strip()
        if not site_name:
            messagebox.showerror("Missing input", "Please enter a site name.")
            return None

        survey_date = self.date_var.get().strip()
        try:
            datetime.strptime(survey_date, "%Y%m%d")
        except ValueError:
            messagebox.showerror("Invalid date", "Survey date must be in YYYYMMDD format.")
            return None

        station_id = dict(STATIONS).get(self.station_var.get())
        if not station_id:
            messagebox.showerror("Missing input", "Please select a tide station.")
            return None

        save_location = self.save_var.get().strip()
        if not save_location:
            messagebox.showerror("Missing input", "Please choose a save location.")
            return None

        specs: list[TransectSpec] = []
        for block in self.transect_blocks:
            windows = block.get_windows()
            if not windows:
                continue
            for s, e in windows:
                try:
                    t_start = datetime.strptime(s, "%H:%M:%S").time()
                    t_end = datetime.strptime(e, "%H:%M:%S").time()
                except ValueError:
                    messagebox.showerror(
                        "Invalid time",
                        f"Transect {block.index}: times must be in HH:MM:SS format.\n"
                        f"Got start='{s}', end='{e}'.")
                    return None
                if t_start >= t_end:
                    messagebox.showerror(
                        "Invalid time window",
                        f"Transect {block.index}: start time must be before end time.\n"
                        f"Got start='{s}', end='{e}'.")
                    return None
            transect_id = block.get_transect_id()
            if not transect_id:
                messagebox.showerror("Missing input",
                                     f"Transect {block.index}: please enter a Transect ID.")
                return None
            specs.append(TransectSpec(transect_id, windows))

        seen: set[str] = set()
        for spec in specs:
            if spec.transect_id in seen:
                messagebox.showerror(
                    "Duplicate Transect ID",
                    f"Transect ID '{spec.transect_id}' is used more than once.\n"
                    "Each transect needs a unique ID.")
                return None
            seen.add(spec.transect_id)

        if not specs and not messagebox.askyesno(
                "No transects defined",
                "No transect time windows were entered.\n\n"
                "Process the entire recording as a single transect?"):
            return None

        save_config({
            "mcap_dir": self.last_dir,
            "site_name": site_name,
            "survey_date": survey_date,
            "station_display": self.station_var.get(),
            "save_location": save_location,
            "make_map": bool(self.map_var.get()),
        })

        return {
            "mcap_paths": list(self.mcap_paths),
            "site_name": site_name,
            "survey_date": survey_date,
            "station_id": station_id,
            "save_location": save_location,
            "transects": specs,
            "make_map": bool(self.map_var.get()),
        }

    # -- running ---------------------------------------------------------

    def on_run(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        args = self._collect()
        if args is None:
            return

        self._clear_log()
        self.run_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.map_btn.configure(state="disabled")
        self.result = None
        self.progress["value"] = 0

        def work() -> None:
            try:
                result = run(
                    args["mcap_paths"],
                    site_name=args["site_name"],
                    survey_date=args["survey_date"],
                    station_id=args["station_id"],
                    save_location=args["save_location"],
                    transects=args["transects"],
                    make_map=args["make_map"],
                    progress=lambda f, m: self.events.put(("progress", (f, m))),
                    on_log=lambda m: self.events.put(("log", m)),
                )
                self.events.put(("done", result))
            except Exception as ex:                       # noqa: BLE001 - shown to the user
                log.exception("run failed")
                self.events.put(("error", ex))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    frac, msg = payload
                    self.progress["value"] = max(0, min(1000, int(frac * 1000)))
                    self.status_var.set(msg)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "probe":
                    token, infos = payload
                    if token == self.probe_token:   # else the list moved on
                        self._show_probe(infos)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.drain_after = self.root.after(80, self._drain)

    def _finish(self, result) -> None:
        self.result = result
        self.progress["value"] = 1000
        self.run_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        if result.map_path:
            self.map_btn.configure(state="normal")
        self.status_var.set(
            f"Done - {len(result.saved)} of {len(result.results)} transect(s) written.")

        text = "\n".join(result.summary_lines())
        if result.skipped or not result.tide_ok or result.warnings or result.read.warnings:
            messagebox.showwarning("MCAP to CSV - finished with notes", text)
        else:
            messagebox.showinfo("MCAP to CSV - complete", text)

    def _fail(self, ex: Exception) -> None:
        self.run_btn.configure(state="normal")
        self.progress["value"] = 0
        self.status_var.set("Failed.")
        self._append_log(f"ERROR: {type(ex).__name__}: {ex}")
        messagebox.showerror("MCAP to CSV - error", f"{type(ex).__name__}: {ex}")

    def open_output(self) -> None:
        if self.result:
            webbrowser.open(self.result.transects_folder.as_uri())

    def open_map(self) -> None:
        if self.result and self.result.map_path:
            webbrowser.open(self.result.map_path.as_uri())

    # -- log pane --------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def close(self) -> None:
        """Tear the window down, cancelling the polling loop first.

        Tk keeps a scheduled ``after`` callback registered against the
        interpreter, so destroying the window without cancelling leaves a timer
        pointing at a command that no longer exists -- which surfaces later as
        an "invalid command name ..._drain" error from Tcl.
        """
        if self.drain_after is not None:
            try:
                self.root.after_cancel(self.drain_after)
            except tk.TclError:
                pass
            self.drain_after = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive() and not messagebox.askyesno(
                "Still running", "A run is in progress. Close anyway?"):
            return
        self.close()

    def mainloop(self) -> None:
        self.root.mainloop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    App().mainloop()


if __name__ == "__main__":
    main()
