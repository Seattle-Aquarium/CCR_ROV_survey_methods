"""
Creating the folder structure for a new flight.

The first thing that happens after a dive: make somewhere consistent to put the
mcap and the imagery, so everything downstream can rely on the layout instead
of searching for it.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import layout
from . import theme as T
from .widgets import Card, button, entry, label


class NewFlightTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.parent_dir: Path | None = None
        self.grid_columnconfigure(0, weight=1)

        c = Card(self, "Create a new flight folder",
                 "Pick where it goes, name it, and UTC makes the empty "
                 "structure. Then drag the mcap and imagery into place.")
        c.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c.body.grid_columnconfigure(0, weight=1)

        # ---- where ---------------------------------------------------
        label(c.body, "Create inside", muted=True).grid(row=0, column=0, sticky="w")
        row = ctk.CTkFrame(c.body, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        row.grid_columnconfigure(0, weight=1)
        self.parent_entry = entry(row, "Navigate to your project folder", width=680)
        self.parent_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick_parent, "primary", width=110
               ).grid(row=0, column=1)

        # ---- name ----------------------------------------------------
        label(c.body, "Folder name", muted=True).grid(row=2, column=0, sticky="w")
        nrow = ctk.CTkFrame(c.body, fg_color="transparent")
        nrow.grid(row=3, column=0, sticky="ew", pady=(2, 4))
        nrow.grid_columnconfigure(0, weight=1)
        self.name_entry = entry(nrow, "2026_08_25_SiteName", width=520)
        self.name_entry.insert(0, layout.default_flight_name())
        self.name_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(nrow, "Today's date", self._reset_date, "ghost", width=120
               ).grid(row=0, column=1)

        self.status = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left")
        self.status.grid(row=4, column=0, sticky="w", pady=(2, 8))
        self.name_entry.bind("<KeyRelease>", lambda _e: self._refresh())

        # ---- preview -------------------------------------------------
        self.preview = ctk.CTkTextbox(c.body, height=150, font=T.FONT_MONO,
                                      fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                      border_width=1, border_color=T.BORDER,
                                      corner_radius=6, wrap="none")
        self.preview.grid(row=5, column=0, sticky="ew")
        self._set_preview()

        button(c.body, "Create folders", self._create, "primary", width=150
               ).grid(row=6, column=0, sticky="w", pady=(12, 0))
        self._refresh()

    # ------------------------------------------------------------------

    def _pick_parent(self) -> None:
        chosen = filedialog.askdirectory(title="Where should the flight folder go?")
        if not chosen:
            return
        self.parent_dir = Path(chosen)
        self.parent_entry.delete(0, "end")
        self.parent_entry.insert(0, str(self.parent_dir))
        self._refresh()

    def _reset_date(self) -> None:
        """Put today's date back, keeping whatever site name was typed."""
        current = layout.clean_flight_name(self.name_entry.get())
        rest = current[10:].strip("_-") if len(current) > 10 else ""
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, layout.default_flight_name() + rest)
        self._refresh()

    def _typed_parent(self) -> Path | None:
        text = self.parent_entry.get().strip()
        if text:
            return Path(text)
        return self.parent_dir

    def _refresh(self) -> None:
        name = self.name_entry.get()
        errs = layout.validate_flight_name(name)
        if errs:
            self.status.configure(text=errs[0], text_color=T.WARN)
        else:
            cleaned = layout.clean_flight_name(name)
            parent = self._typed_parent()
            if parent and (parent / cleaned).exists():
                self.status.configure(
                    text=f"{cleaned} already exists — missing folders will be "
                         f"added, nothing will be removed.",
                    text_color=T.TEXT_MUTED)
            else:
                self.status.configure(text=f"Will create: {cleaned}",
                                      text_color=T.OK)
        self._set_preview()

    def _set_preview(self) -> None:
        cleaned = layout.clean_flight_name(self.name_entry.get()) or "<name>"
        lines = [f"{cleaned}/"]

        # Build the real tree. Printing only each leaf name would show
        # photos/GPR as an indented "GPR/" directly under logs/ -- which is a
        # different structure from the one actually created, in the one place
        # the user is checking whether the structure is right.
        tree: dict[str, dict] = {}
        for rel in layout.BASE_DIRS:
            node = tree
            for part in rel.split("/"):
                node = node.setdefault(part, {})

        def walk(node: dict, depth: int) -> None:
            for name in node:
                lines.append("    " * (depth + 1) + name + "/")
                walk(node[name], depth + 1)

        walk(tree, 0)
        lines.append("")
        lines.append("  transects/ appears under photos/ when you sort imagery.")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", "\n".join(lines))
        self.preview.configure(state="disabled")

    def _create(self) -> None:
        parent = self._typed_parent()
        if not parent or not parent.is_dir():
            messagebox.showinfo(self.app.title(),
                                "Choose the folder to create the flight inside.")
            return
        name = self.name_entry.get()
        errs = layout.validate_flight_name(name)
        if errs:
            messagebox.showerror(self.app.title(), errs[0])
            return
        try:
            res = layout.scaffold(parent, name)
        except Exception as ex:
            messagebox.showerror(self.app.title(), f"Could not create it: {ex}")
            return

        for line in res.summary().splitlines():
            self.app._log(line)
        self.app.use_flight(res.root)
        self.app._reveal(res.root)
        self.status.configure(
            text="Created. Selected as the current flight folder.",
            text_color=T.OK)
