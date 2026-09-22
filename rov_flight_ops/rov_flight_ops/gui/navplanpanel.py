"""
The survey-plan controls: a tool palette, a feature list and an inspector.

Everything an operator needs to draw a plan and then say exactly what it is:
"30 m at 125 true", "30 by 20 rotated 37, lanes every 2 m from the north-west
corner". The map handles pointing; this handles typing, which is how a plan
stops being approximately right.

The inspector is the same numbers the map draws on the shape's edges, from the
same `measurements()` call, so the two cannot disagree.
"""

from __future__ import annotations

import customtkinter as ctk

from ..nav import plan as P
from . import navdraw
from . import theme as T
from .widgets import button, entry, label

#: Tools offered, in the order they are used.
PALETTE = ("pan", "line", "polyline", "rect", "grid", "circle", "polygon")

GLYPH = {"pan": "✋", "line": "╱", "polyline": "⌁", "rect": "▭",
         "grid": "▦", "circle": "◯", "polygon": "⬠"}


def _set(widget, **kw) -> None:
    """`configure`, but only for the values that actually changed.

    CustomTkinter redraws a widget -- and, for a frame, its children -- on
    every `configure`, whatever was passed. On a panel refreshed at telemetry
    rate that turns an unchanged list into the most expensive thing on the
    page, so the comparison is worth the lines.
    """
    changed = {}
    for key, value in kw.items():
        try:
            if widget.cget(key) != value:
                changed[key] = value
        except Exception:
            changed[key] = value
    if changed:
        widget.configure(**changed)


class PlanPanel(ctk.CTkFrame):
    """Tools on top, then the feature list, then the inspector."""

    def __init__(self, master, page, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.page = page
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._rows: dict[str, ctk.CTkFrame] = {}
        self._fields: dict[str, ctk.CTkEntry] = {}
        self._showing: str | None = None

        self._build_tools()
        self._build_list()
        self._build_inspector()

    # ------------------------------------------------------------------

    def _build_tools(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.tool_buttons: dict[str, ctk.CTkButton] = {}
        for i, tool in enumerate(PALETTE):
            b = ctk.CTkButton(
                bar, text=f"{GLYPH[tool]}", width=34, height=30,
                font=T.FONT_BODY, corner_radius=6,
                fg_color="transparent", hover_color=T.SURFACE_ALT,
                text_color=T.TEXT, border_width=1, border_color=T.BORDER,
                command=lambda k=tool: self.page.set_tool(k))
            b.grid(row=0, column=i, padx=(0, 3))
            self.tool_buttons[tool] = b
        bar.grid_columnconfigure(len(PALETTE), weight=1)
        self.undo_btn = button(bar, "↶", self.page.undo, "ghost", width=32)
        self.undo_btn.grid(row=0, column=len(PALETTE) + 1, padx=(6, 2))
        self.redo_btn = button(bar, "↷", self.page.redo, "ghost", width=32)
        self.redo_btn.grid(row=0, column=len(PALETTE) + 2, padx=(0, 2))

        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        for i, (text, fn) in enumerate((
                ("New", self.page.new_plan), ("Open…", self.page.open_plan),
                ("Save as…", self.page.save_plan_as),
                ("Export…", self.page.export_plan))):
            button(row2, text, fn, "ghost", width=74).grid(row=0, column=i,
                                                           padx=(0, 4))
        row2.grid_columnconfigure(4, weight=1)
        self.hint = label(row2, "", muted=True)
        self.hint.configure(anchor="e")
        self.hint.grid(row=0, column=5, sticky="ew")

    def _build_list(self) -> None:
        self.listbox = ctk.CTkScrollableFrame(self, fg_color=T.FIELD_BG,
                                              height=110, corner_radius=6)
        self.listbox.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        self.listbox.grid_columnconfigure(0, weight=1)

    def _build_inspector(self) -> None:
        box = ctk.CTkFrame(self, fg_color=T.SURFACE_ALT, corner_radius=6)
        box.grid(row=3, column=0, sticky="ew")
        box.grid_columnconfigure(0, weight=1)
        self.inspector = box
        self.title = ctk.CTkLabel(box, text="Nothing selected", font=T.FONT_H2,
                                  text_color=T.HEADING, anchor="w")
        self.title.grid(row=0, column=0, sticky="ew", padx=8, pady=(7, 2))
        self.summary = ctk.CTkLabel(box, text="", font=T.FONT_SMALL,
                                    text_color=T.TEXT_MUTED, anchor="w",
                                    justify="left")
        self.summary.grid(row=1, column=0, sticky="ew", padx=8)
        self.body = ctk.CTkFrame(box, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        self.body.grid_columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    #  refreshing
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        ed = self.page.editor
        for tool, b in self.tool_buttons.items():
            on = ed is not None and ed.tool == tool
            _set(b, border_color=T.ACCENT if on else T.BORDER,
                 border_width=2 if on else 1,
                 text_color=T.ACCENT if on else T.TEXT)
        _set(self.hint, text=(ed.status if ed else "")[:64])
        hist = self.page.history
        _set(self.undo_btn, state="normal" if hist and hist.can_undo
             else "disabled")
        _set(self.redo_btn, state="normal" if hist and hist.can_redo
             else "disabled")
        self._refresh_list()
        self._refresh_inspector()

    def _refresh_list(self) -> None:
        plan = self.page.plan
        want = [] if plan is None else [f.id for f in plan.features]
        if list(self._rows) == want:
            for fid, row in self._rows.items():
                f = plan.get(fid)
                on = self.page.editor and self.page.editor.selected_id == fid
                # Only when it has actually changed. `CTkFrame.configure`
                # redraws the frame *and* every CustomTkinter child that
                # inherits its background, so reconfiguring forty unchanged
                # rows on every tick cost about 150 ms a redraw -- more than
                # everything on the map put together.
                _set(row, fg_color=T.SURFACE if on else "transparent")
                _set(row._name,
                     text=f"{navdraw.KIND_GLYPH.get(f.kind, '•')} {f.name}",
                     text_color=T.TEXT_MUTED if f.hidden else T.TEXT)
            return
        for row in self._rows.values():
            row.destroy()
        self._rows.clear()
        if plan is None:
            return
        for i, f in enumerate(plan.features):
            row = ctk.CTkFrame(self.listbox, fg_color="transparent",
                               corner_radius=4)
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            name = ctk.CTkLabel(
                row, text=f"{navdraw.KIND_GLYPH.get(f.kind, '•')} {f.name}",
                font=T.FONT_SMALL, anchor="w", cursor="hand2")
            name.grid(row=0, column=0, sticky="ew", padx=(6, 0))
            name.bind("<Button-1>", lambda _e, k=f.id: self.page.select(k))
            row._name = name
            for j, (text, fn) in enumerate((
                    ("👁", lambda k=f.id: self.page.toggle_hidden(k)),
                    ("🔒", lambda k=f.id: self.page.toggle_locked(k)),
                    ("✕", lambda k=f.id: self.page.delete_feature(k)))):
                ctk.CTkButton(row, text=text, width=24, height=20,
                              font=T.FONT_SMALL, fg_color="transparent",
                              hover_color=T.SURFACE, text_color=T.TEXT_MUTED,
                              command=fn).grid(row=0, column=1 + j)
            self._rows[f.id] = row

    def _refresh_inspector(self) -> None:
        f = self.page.editor.selected() if self.page.editor else None
        key = None if f is None else f"{f.id}:{f.kind}"
        if key != self._showing:
            self._showing = key
            for w in self.body.winfo_children():
                w.destroy()
            self._fields.clear()
            if f is not None:
                self._build_fields(f)
        if f is None:
            _set(self.title, text="Nothing selected")
            _set(self.summary, text="Pick a tool above, or click a feature "
                                    "on the map.")
            return
        _set(self.title, text=f.name or f.kind)
        _set(self.summary, text=navdraw._describe(f, f.measurements()))

    # ------------------------------------------------------------------
    #  the editable fields, per kind
    # ------------------------------------------------------------------

    def _field(self, row: int, name: str, key: str, value, width: int = 84,
               unit: str = "") -> None:
        label(self.body, name, muted=True).grid(row=row, column=0, sticky="w",
                                                pady=1)
        holder = ctk.CTkFrame(self.body, fg_color="transparent")
        holder.grid(row=row, column=1, sticky="w")
        e = entry(holder, "", width=width)
        e.insert(0, f"{value:g}" if isinstance(value, (int, float))
                 else str(value))
        e.grid(row=0, column=0)
        e.bind("<Return>", lambda _e: self.page.apply_fields())
        self._fields[key] = e
        if unit:
            label(holder, unit, muted=True).grid(row=0, column=1, padx=(4, 0))

    def _build_fields(self, f) -> None:
        r = 0
        self._field(r, "Name", "name", f.name, width=150)
        r += 1
        if isinstance(f, P.Line):
            m = f.measurements()
            if len(f.points) == 2:
                self._field(r, "Length", "length_m", round(m["length_m"], 2),
                            unit="m")
                r += 1
                self._field(r, "Bearing", "bearing_deg",
                            round(m["bearing_deg"] or 0, 1), unit="°T")
                r += 1
                self._pick(r, "Hold", "fix", ("start", "end"))
                r += 1
            button(self.body, "Reverse", self.page.reverse_line, "ghost",
                   width=88).grid(row=r, column=0, pady=(6, 0), sticky="w")
            button(self.body, "Parallels…", self.page.make_parallels, "ghost",
                   width=100).grid(row=r, column=1, pady=(6, 0), sticky="w")
        elif isinstance(f, (P.Rect, P.Grid)):
            self._field(r, "Length", "length_m", round(f.length_m, 2), unit="m")
            r += 1
            self._field(r, "Width", "width_m", round(f.width_m, 2), unit="m")
            r += 1
            self._field(r, "Rotation", "rotation_deg", round(f.rotation_deg, 1),
                        unit="°T")
            r += 1
            self._pick(r, "Anchor", "anchor_corner",
                       ("centre", "1", "2", "3", "4"))
            r += 1
            if isinstance(f, P.Grid):
                self._field(r, "Lane spacing", "spacing_m",
                            round(f.spacing_m, 2), unit="m")
                r += 1
                self._pick(r, "Lanes along", "lane_axis", ("length", "width"))
                r += 1
                self._pick(r, "Start corner", "start_corner",
                           ("1", "2", "3", "4"))
                r += 1
                self._check(r, "Back and forth", "boustrophedon",
                            f.boustrophedon)
                r += 1
                self._check(r, "Second pass at 90°", "second_pass",
                            f.second_pass)
                r += 1
                self._field(r, "Swath (optional)", "swath_m",
                            round(self.page.swath_m, 2) if self.page.swath_m
                            else "", unit="m")
                r += 1
                button(self.body, "Fly this grid", self.page.fly_selected,
                       "primary", width=120).grid(row=r, column=0,
                                                  columnspan=2, pady=(6, 0),
                                                  sticky="w")
                r += 1
        elif isinstance(f, P.Circle):
            self._field(r, "Radius", "radius_m", round(f.radius_m, 2), unit="m")
            r += 1
        if isinstance(f, P.Line):
            button(self.body, "Fly this line", self.page.fly_selected,
                   "primary", width=120).grid(row=r + 1, column=0,
                                              columnspan=2, pady=(6, 0),
                                              sticky="w")
        apply_row = r + 2
        button(self.body, "Apply", self.page.apply_fields, "primary",
               width=80).grid(row=apply_row, column=0, pady=(8, 0), sticky="w")
        label(self.body, "Enter applies", muted=True).grid(
            row=apply_row, column=1, sticky="w", pady=(8, 0))

    def _pick(self, row: int, name: str, key: str, values) -> None:
        label(self.body, name, muted=True).grid(row=row, column=0, sticky="w",
                                                pady=1)
        m = ctk.CTkOptionMenu(self.body, width=110, font=T.FONT_SMALL,
                              values=list(values))
        m.grid(row=row, column=1, sticky="w")
        self._fields[key] = m

    def _check(self, row: int, name: str, key: str, value: bool) -> None:
        var = ctk.BooleanVar(value=value)
        cb = ctk.CTkCheckBox(self.body, text=name, variable=var,
                             font=T.FONT_SMALL, width=150)
        cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=1)
        cb._var = var
        self._fields[key] = cb

    def values(self) -> dict:
        """What is typed in the inspector right now."""
        out = {}
        for key, w in self._fields.items():
            try:
                if isinstance(w, ctk.CTkCheckBox):
                    out[key] = bool(w._var.get())
                else:
                    out[key] = w.get()
            except Exception:
                pass
        return out
