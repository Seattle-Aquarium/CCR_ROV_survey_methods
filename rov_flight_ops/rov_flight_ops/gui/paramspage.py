"""
Parameters: the vehicle's ArduSub parameters against its reference file.

    +--------------------------------------------------------------+
    |  1. Key parameters   MATCHES / DIFFERS · how many · the file  |
    |     EKF · DVL · GPS · origin  |  depth · failsafes · lights · |
    |                               |  gain · logging               |
    +--------------------------------------------------------------+
    |  2. Other differences  (empty when the rest matches)          |
    +-------------------------------+------------------------------+
    |  3. Navigation profile        |  4. Origin                   |
    |                               |  5. Message health           |
    +-------------------------------+------------------------------+

The vehicle's values are the ones the Navigation tab's collector reads from
the head of the autopilot's own dataflash log, so this tab sends the vehicle
nothing to fill itself. The one thing here that does write is "Set to
reference", and only with the ROV disarmed: each value is sent as a
PARAM_SET and read back, and the arming state is checked again before every
write.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import board as B
from .. import diagnostics, paramfile
from .. import paramcheck as PC
from ..nav import profiles as PR
from . import theme as T
from .widgets import Card, button, fit_wrap, label

log = logging.getLogger(__name__)

VEHICLE = "Nereo"
#: How often the tab re-reads while it is on screen.
REFRESH_MS = 2000
#: The key table's columns: mark, name, what it is, vehicle, reference.
KEY_WIDTHS = (22, 150, 0, 150, 150)
#: Groups before this go in the left column, the rest in the right.
KEY_SPLIT = 4

FONT_VERDICT = (T.FAMILY_SEMIBOLD, 22)
FONT_GROUP = (T.FAMILY_SEMIBOLD, 12)
FONT_NAME = (T.MONO, 11)
FONT_MARK = ("Segoe UI Symbol", 13)

#: (glyph, colour) for each state.
STATE_MARK = {
    PC.MATCH: ("●", B.COLORS[B.GO]),
    PC.DIFFERS: ("▲", B.COLORS[B.CHECK]),
    PC.MISSING: ("▲", B.COLORS[B.CHECK]),
    PC.SITE: ("◆", T.TEXT_MUTED),
    PC.DERIVED: ("=", T.TEXT_MUTED),
}


def _set(widget, **kw) -> None:
    """Configure only what changed: these tables are refreshed every two
    seconds, and CustomTkinter redraws a label on every configure."""
    cache = getattr(widget, "_cache", None)
    if cache is None:
        cache = widget._cache = {}
    changes = {k: v for k, v in kw.items() if cache.get(k) != v}
    if changes:
        cache.update(changes)
        widget.configure(**changes)


class Table(ctk.CTkFrame):
    """Rows of labels, built once per layout and only re-texted after that.

    Each row is a dict: `key`, `cells` [(text, font, colour)], and optionally
    `header` (one cell across the table), `button` (text, command, enabled)
    and `note` (text, colour) on a line of its own beneath.
    """

    def __init__(self, master, widths: tuple[int, ...]):
        super().__init__(master, fg_color="transparent")
        self.widths = widths
        # Columns as wide as their widest cell; the spare width goes to the
        # end, after the buttons' column, so values sit beside their names.
        for i, w in enumerate(widths):
            self.grid_columnconfigure(i, minsize=w)
        self.grid_columnconfigure(len(widths), weight=1)
        self._keys: list = []
        self._widgets: list = []
        self._wrap = 0
        self.bind("<Configure>", self._fit_notes, add="+")

    def _fit_notes(self, event=None) -> None:
        """Notes wrap to the table, less the mark column they are indented by."""
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:
            scaling = 1.0
        width = int(self.winfo_width() / scaling) - self.widths[0] - 12
        if width < 100 or abs(width - self._wrap) < 8:
            return
        self._wrap = width
        for made in self._widgets:
            if made["note"] is not None:
                made["note"].configure(wraplength=width)

    def show(self, rows: list[dict]) -> None:
        keys = [(r["key"], bool(r.get("header")), bool(r.get("button")),
                 bool(r.get("note"))) for r in rows]
        if keys != self._keys:
            for w in self.winfo_children():
                w.destroy()
            self._widgets = []
            self._keys = keys
            grid_row = 0
            for r in rows:
                made = {"cells": [], "button": None, "note": None}
                if r.get("header"):
                    lab = ctk.CTkLabel(self, text="", anchor="w", height=24)
                    lab.grid(row=grid_row, column=0, columnspan=len(self.widths) + 1,
                             sticky="ew", pady=(8, 1))
                    made["cells"].append(lab)
                else:
                    for i in range(len(r["cells"])):
                        lab = ctk.CTkLabel(self, text="", anchor="w", height=20)
                        lab.grid(row=grid_row, column=i, sticky="ew",
                                 padx=(0, 8))
                        made["cells"].append(lab)
                    if r.get("button"):
                        btn = button(self, "Set", None, "ghost", width=48)
                        btn.configure(height=20, font=T.FONT_SMALL)
                        btn.grid(row=grid_row, column=len(self.widths), sticky="w")
                        made["button"] = btn
                grid_row += 1
                if r.get("note"):
                    lab = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                       height=16, font=T.FONT_SMALL)
                    lab.grid(row=grid_row, column=1, columnspan=len(self.widths),
                             sticky="ew", pady=(0, 3))
                    made["note"] = lab
                    if self._wrap:
                        lab.configure(wraplength=self._wrap)
                    grid_row += 1
                self._widgets.append(made)
        for r, made in zip(rows, self._widgets, strict=True):
            if r.get("header"):
                text, font, color = r["cells"][0]
                _set(made["cells"][0], text=text, font=font, text_color=color)
                continue
            for lab, (text, font, color) in zip(made["cells"], r["cells"], strict=True):
                _set(lab, text=text, font=font, text_color=color)
            if made["button"] is not None:
                text, command, enabled = r["button"]
                _set(made["button"], text=text, command=command,
                     state="normal" if enabled else "disabled")
            if made["note"] is not None:
                text, color = r["note"]
                _set(made["note"], text=text, text_color=color)


class ParamsPage(ctk.CTkFrame):

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._ticking = False
        self._reference: paramfile.Reference | None = None
        self._reference_mtime = 0.0
        #: name -> (value, time): writes confirmed by read-back, shown until
        #: the vehicle's own log catches up with them.
        self._confirmed: dict[str, tuple[float, float]] = {}

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self.body = body

        self._build_key(body)
        self._build_others(body)
        left, right = app.pair(body, row=2, split_key="params_pair_share")
        right.grid_rowconfigure(0, weight=0)
        self._build_profile(left, 0)
        self._build_origin(right, 0)
        self._build_messages(right, 1)

    # ------------------------------------------------------------------
    #  building
    # ------------------------------------------------------------------

    def _build_key(self, body) -> None:
        c = Card(body, "1.  Key parameters",
                 "Vehicle, then reference.   ▲ differs   ◆ set per site   "
                 "= worked out from the others")
        c.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top = ctk.CTkFrame(c.body, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self.verdict = ctk.CTkLabel(top, text="—", font=FONT_VERDICT,
                                    text_color=T.TEXT_MUTED, anchor="w")
        self.verdict.grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.headline = ctk.CTkLabel(top, text="", font=T.FONT_BODY,
                                     text_color=T.TEXT, anchor="w")
        self.headline.grid(row=0, column=1, sticky="ew")
        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        button(buttons, "Open reference file", self._open_reference, "ghost",
               width=160).grid(row=0, column=0)
        button(buttons, "Save the vehicle's", self._save_vehicle, "ghost",
               width=150).grid(row=0, column=1, padx=(8, 0))
        self.key_all = button(buttons, "Set to reference",
                              lambda: self._set(self._key_settable()), "ghost",
                              width=170)
        self.key_all.grid(row=0, column=2, padx=(8, 0))
        self.detail = ctk.CTkLabel(top, text="", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w",
                                   justify="left")
        self.detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.write_state = ctk.CTkLabel(top, text="", font=T.FONT_SMALL,
                                        text_color=T.TEXT_MUTED, anchor="w")
        self.write_state.grid(row=2, column=0, columnspan=3, sticky="ew",
                              pady=(2, 0))
        fit_wrap(top, self.detail)

        # The groups in two columns, so the list is half as long.
        cols = ctk.CTkFrame(c.body, fg_color="transparent")
        cols.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        cols.grid_columnconfigure((0, 1), weight=1, uniform="key")
        self.key_tables = []
        for i in range(2):
            t = Table(cols, KEY_WIDTHS)
            t.grid(row=0, column=i, sticky="new",
                   padx=(0, 16) if i == 0 else (16, 0))
            self.key_tables.append(t)

    def _build_profile(self, parent, row: int) -> None:
        c = Card(parent, "3.  Navigation profile")
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 12))
        head = ctk.CTkFrame(c.body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        self.profile_menu = ctk.CTkOptionMenu(
            head, width=200, font=T.FONT_SMALL,
            values=[PR.PROFILES[k].label for k in PR.PROFILE_ORDER],
            command=self._pick_profile)
        self.profile_menu.grid(row=0, column=0, sticky="w")
        self.profile_summary = label(head, "not checked", muted=True)
        self.profile_summary.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.profile_table = Table(c.body, (22, 150, 0, 0))
        self.profile_table.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def _build_origin(self, parent, row: int) -> None:
        c = Card(parent, "4.  Origin")
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 12))
        self.origin_text = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                        text_color=T.TEXT, anchor="w",
                                        justify="left")
        self.origin_text.grid(row=0, column=0, sticky="ew")
        fit_wrap(c.body, self.origin_text)

    def _build_messages(self, parent, row: int) -> None:
        c = Card(parent, "5.  Message health",
                 "Age is since a new message, by mavlink2rest's counter.")
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 12))
        self.messages = ctk.CTkLabel(c.body, text="", font=T.FONT_MONO,
                                     text_color=T.TEXT, anchor="nw",
                                     justify="left")
        self.messages.grid(row=0, column=0, sticky="ew")
        self._messages_text = ""

    def _build_others(self, body) -> None:
        c = Card(body, "2.  Other differences",
                 "Everything else on the vehicle that is not as the file has "
                 "it.")
        c.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        head = ctk.CTkFrame(c.body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.others_summary = label(head, "", muted=True)
        self.others_summary.grid(row=0, column=0, sticky="w")
        self.others_all = button(head, "Set all to reference",
                                 lambda: self._set(self._others_settable()),
                                 "ghost", width=170)
        self.others_all.grid(row=0, column=1, sticky="e")
        self.others_table = Table(c.body, (22, 190, 0, 170, 170))
        self.others_table.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.ignored = ctk.CTkLabel(c.body, text="", font=T.FONT_SMALL,
                                    text_color=T.TEXT_MUTED, anchor="w",
                                    justify="left")
        self.ignored.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        fit_wrap(c.body, self.ignored)

    # ------------------------------------------------------------------
    #  reading
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        nav = self.app.pages.get("navigation")
        if nav is not None:
            nav.ensure_live()
        if not self._ticking:
            self._ticking = True
            self._tick()

    def _tick(self) -> None:
        try:
            if self.winfo_ismapped():
                self._update()
        except Exception:
            diagnostics.log_exception("Parameters tab", *sys.exc_info(),
                                      level=logging.WARNING)
        self.after(REFRESH_MS, self._tick)

    def reference(self) -> paramfile.Reference:
        path = paramfile.reference_path(VEHICLE)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._reference is None or mtime != self._reference_mtime:
            self._reference = paramfile.read(path)
            self._reference_mtime = mtime
        return self._reference

    def _nav(self):
        return self.app.pages.get("navigation")

    def _snapshot(self):
        nav = self._nav()
        col = getattr(nav, "collector", None)
        return col.snapshot() if col is not None else None

    def vehicle_params(self, s) -> dict[str, float]:
        params = dict((s.params if s is not None else None) or {})
        for name, (value, _t) in list(self._confirmed.items()):
            if name in params and PC.same(params[name], value):
                del self._confirmed[name]              # the log has caught up
            else:
                params[name] = value
        return params

    def _update(self) -> None:
        s = self._snapshot()
        ref = self.reference()
        cmp = PC.compare(self.vehicle_params(s), ref.params)
        self.comparison = cmp
        writable, why = self._writable(s)
        self._paint_summary(cmp, ref, s, writable, why)
        self._paint_key(cmp, writable)
        self._paint_others(cmp, writable)
        self._paint_profile()
        self._paint_origin()
        self._paint_messages(s)

    # ------------------------------------------------------------------
    #  painting
    # ------------------------------------------------------------------

    def _paint_summary(self, cmp, ref, s, writable: bool, why: str) -> None:
        word, rest = cmp.headline()
        color = (B.COLORS[B.GO] if word == "MATCHES" else
                 B.COLORS[B.CHECK] if word == "DIFFERS" else T.TEXT_MUTED)
        _set(self.verdict, text=word, text_color=color)
        _set(self.headline, text=rest)
        meta = ref.meta or {}
        bits = []
        if cmp.read:
            age = getattr(s, "params_age", None)
            bits.append(f"{cmp.read:,} read from the vehicle's log"
                        + (f" {age:.0f} s ago" if isinstance(age, (int, float)) else ""))
        where = ref.path.name if ref.path else "no reference file"
        src = " · ".join(x for x in (meta.get("firmware", ""),
                                     f"flight {meta['source_flight']}"
                                     if meta.get("source_flight") else "") if x)
        bits.append(f"reference {where}" + (f" ({src})" if src else ""))
        if cmp.ignored:
            bits.append(f"{len(cmp.ignored)} that change by themselves not compared")
        if ref.problems:
            bits.append(f"{len(ref.problems)} line(s) of the file could not be read")
        _set(self.detail, text="  ·  ".join(bits))
        _set(self.write_state, text=why,
             text_color=T.TEXT_MUTED if writable else T.TEXT_MUTED)

    def _row_cells(self, r: PC.Row, name_font=FONT_NAME) -> list:
        glyph, color = STATE_MARK.get(r.state, ("○", T.TEXT_MUTED))
        loud = r.state in (PC.DIFFERS, PC.MISSING) or r.derived_differs
        if r.derived_differs:
            color = B.COLORS[B.CHECK]
        confirmed = r.name in self._confirmed
        if confirmed:
            glyph, color = "●", B.COLORS[B.GO]
        return [(glyph, FONT_MARK, color),
                (r.name, name_font, T.TEXT if loud else T.TEXT_MUTED),
                (r.what, T.FONT_SMALL, T.TEXT_MUTED),
                (r.vehicle_text + ("  set ✓" if confirmed else ""), FONT_NAME,
                 T.TEXT if loud else T.TEXT_MUTED),
                (r.reference_text, FONT_NAME, T.TEXT_MUTED)]

    def _paint_key(self, cmp, writable: bool) -> None:
        halves: list[list] = [[], []]
        for gi, g in enumerate(cmp.groups):
            rows = halves[0 if gi < KEY_SPLIT else 1]
            n = sum(1 for r in g.rows if r.state in (PC.DIFFERS, PC.MISSING))
            tail = f"   {n} differ{'s' if n == 1 else ''}" if n else ""
            rows.append({"key": ("g", g.name), "header": True,
                         "cells": [(g.name + tail, FONT_GROUP,
                                    B.COLORS[B.CHECK] if n else T.HEADING)]})
            for r in g.rows:
                spec = {"key": ("r", r.name), "cells": self._row_cells(r)}
                if r.settable:
                    spec["button"] = ("Set", lambda r=r: self._set([r]), writable)
                if r.advice:
                    spec["note"] = (f"▲ {r.advice}", B.COLORS[B.CHECK])
                rows.append(spec)
        for table, rows in zip(self.key_tables, halves, strict=True):
            table.show(rows)
        n = len(self._key_settable())
        _set(self.key_all, text=f"Set {n} to reference" if n > 1 else
             "Set to reference", state="normal" if writable and n else "disabled")

    def _key_settable(self) -> list:
        cmp = getattr(self, "comparison", None)
        return [r for r in (cmp.key_rows if cmp else [])
                if r.settable and r.name not in self._confirmed]

    def _others_settable(self) -> list:
        cmp = getattr(self, "comparison", None)
        return [r for r in (cmp.others if cmp else [])
                if r.settable and r.name not in self._confirmed]

    def _paint_others(self, cmp, writable: bool) -> None:
        rows = []
        for r in cmp.others:
            cells = self._row_cells(r)
            rows.append({"key": ("o", r.name),
                         "cells": [cells[0], cells[1], ("", T.FONT_SMALL, T.TEXT_MUTED),
                                   cells[3], cells[4]],
                         "button": ("Set", lambda r=r: self._set([r]), writable)})
        self.others_table.show(rows)
        if not cmp.read:
            text = "Waiting for the vehicle's parameters."
        elif not cmp.others:
            text = "None — the rest of the vehicle matches the file."
        else:
            text = f"{len(cmp.others)} differ:"
        _set(self.others_summary, text=text)
        n = len(self._others_settable())
        _set(self.others_all, state="normal" if writable and n else "disabled")
        bits = []
        if cmp.only_vehicle:
            bits.append("Only on the vehicle: " + ", ".join(cmp.only_vehicle[:12])
                        + (" …" if len(cmp.only_vehicle) > 12 else ""))
        if cmp.only_reference:
            bits.append("Only in the file: " + ", ".join(cmp.only_reference[:12])
                        + (" …" if len(cmp.only_reference) > 12 else ""))
        if cmp.ignored:
            families = sorted({n.split("_")[0] + "_*" if n.startswith(("STAT_", "SR0_"))
                               else n for n in cmp.ignored})
            bits.append("Not compared, rewritten by the autopilot or Cockpit: "
                        + ", ".join(families) + ".  The origin is per site.")
        _set(self.ignored, text="\n".join(bits))

    def _paint_profile(self) -> None:
        nav = self._nav()
        if nav is None:
            return
        # Checked again only when there is something new to check: each
        # check is written to the navigation session's log.
        col = getattr(nav, "collector", None)
        key = (getattr(col, "params_mono", None), nav.profile_key,
               getattr(nav.origin_state, "active", None))
        if key != getattr(self, "_checked", None) or nav.check is None:
            self._checked = key
            try:
                nav._revalidate()
            except Exception:
                diagnostics.log_exception("profile check", *sys.exc_info(),
                                          level=logging.WARNING)
        label_text = PR.PROFILES[nav.profile_key].label
        if self.profile_menu.get() != label_text:
            self.profile_menu.set(label_text)
        check = nav.check
        if check is None:
            _set(self.profile_summary, text="not checked — no parameters yet",
                 text_color=T.TEXT_MUTED)
            self.profile_table.show([])
            return
        _set(self.profile_summary, text=check.summary(),
             text_color=B.COLORS[B.GO] if check.matches else B.COLORS[B.CHECK])
        rows = []
        for f in check.findings:
            if f.ok:
                glyph, color = "●", B.COLORS[B.GO]
            elif f.severity is PR.Severity.BLOCKER:
                glyph, color = "✖", B.COLORS[B.NOGO]
            else:
                glyph, color = "▲", B.COLORS[B.CHECK]
            spec = {"key": ("f", f.param),
                    "cells": [(glyph, FONT_MARK, color),
                              (f.param, FONT_NAME, T.TEXT if not f.ok else T.TEXT_MUTED),
                              (f.current_text(), FONT_NAME,
                               T.TEXT if not f.ok else T.TEXT_MUTED),
                              ("" if f.ok else f"wants {f.want_text()}", FONT_NAME,
                               T.TEXT_MUTED)]}
            if not f.ok:
                spec["note"] = (f.requirement.why, color)
            rows.append(spec)
        for i, (sev, text) in enumerate(check.notes):
            bad = sev is PR.Severity.BLOCKER
            rows.append({"key": ("n", i),
                         "cells": [("✖" if bad else "▲", FONT_MARK,
                                    B.COLORS[B.NOGO] if bad else B.COLORS[B.CHECK]),
                                   ("integration", FONT_NAME, T.TEXT_MUTED),
                                   ("", FONT_NAME, T.TEXT_MUTED),
                                   ("", FONT_NAME, T.TEXT_MUTED)],
                         "note": (text, T.TEXT)})
        self.profile_table.show(rows)

    def _pick_profile(self, label_text: str) -> None:
        nav = self._nav()
        if nav is not None:
            nav.profile_menu.set(label_text)
            nav._pick_profile(label_text)
            self._paint_profile()

    def _paint_origin(self) -> None:
        nav = self._nav()
        st = getattr(nav, "origin_state", None)
        if st is None:
            return
        lines = [st.summary()]
        if st.authority_reason:
            lines.append(st.authority_reason)
        for f in st.families:
            lines.append(f"{f.prefix}*  {f.text()}")
        if st.scripting_enabled is not None:
            lines.append(f"Lua scripting {'on' if st.scripting_enabled else 'OFF'} "
                         f"(SCR_ENABLE {int(st.scripting_enabled)})")
        for w in st.warnings:
            lines.append(f"▲ {w}")
        _set(self.origin_text, text="\n".join(lines))

    def _paint_messages(self, s) -> None:
        from .navstatus import message_health_rows
        if s is None or not s.messages:
            text = "Nothing read yet."
        else:
            rows = message_health_rows(s, time.monotonic())
            lines = [f"{'MESSAGE':<24}{'RATE':>9}{'AGE':>7}  NOTE"]
            lines += [f"{n:<24}{r:>9}{a:>7}  {note}" for n, r, a, note in rows]
            text = "\n".join(lines)
        if text != self._messages_text:
            self._messages_text = text
            self.messages.configure(text=text)

    # ------------------------------------------------------------------
    #  writing
    # ------------------------------------------------------------------

    def _writable(self, s) -> tuple[bool, str]:
        """Can a parameter be set now, and if not, why not."""
        if s is None or not s.link.connected:
            return False, "Setting a parameter needs the ROV connected and disarmed."
        if not s.armed.quality.has_value:
            return False, "Arming state unknown: nothing can be set."
        if s.armed.value:
            return False, "The ROV is armed: disarm it to set a parameter."
        if getattr(self.app, "simulating", False):
            return True, ("Disarmed. This is the simulator: a set is refused, "
                          "as it sends nothing to any vehicle.")
        return True, "Disarmed: a parameter can be set to the reference value."

    def _set(self, rows: list) -> None:
        rows = [r for r in rows if r.settable and r.reference is not None]
        if not rows:
            return
        s = self._snapshot()
        ok, why = self._writable(s)
        if not ok:
            messagebox.showinfo("Set to reference", why)
            return
        listing = "\n".join(f"{r.name}:  {PC.text(r.vehicle)}  →  {PC.text(r.reference)}"
                            for r in rows[:20])
        if len(rows) > 20:
            listing += f"\n… and {len(rows) - 20} more"
        if not messagebox.askyesno(
                "Set to reference",
                f"Write {len(rows)} parameter{'s' * (len(rows) != 1)} to "
                f"{VEHICLE}?\n\n{listing}\n\nEach is read back to confirm. "
                f"Writing stops if the ROV arms."):
            return
        if getattr(self.app, "simulating", False):
            messagebox.showinfo("Set to reference",
                                "Refused: the ROV flight simulator sends nothing "
                                "to any vehicle.")
            return
        col = getattr(self._nav(), "collector", None)
        mav0 = getattr(col, "mav", None)
        if mav0 is None:
            messagebox.showinfo("Set to reference", "Not connected to the vehicle.")
            return
        from ..nav import mav2rest as M2R
        from .navdialogs import _param_setter
        mav = M2R.Mavlink2Rest(mav0.host, port=mav0.port, system=mav0.system,
                               component=mav0.component, allow_writes=True)
        setter = _param_setter(mav)
        targets = [(r.name, float(r.reference)) for r in rows]

        def work(progress, cancel):
            done = []
            for i, (name, value) in enumerate(targets):
                if cancel.is_set():
                    break
                snap = col.snapshot()
                if not snap.armed.quality.has_value or snap.armed.value:
                    done.append((name, value, False, "stopped: the ROV is armed "
                                                      "or its state is unknown"))
                    break
                progress(i / len(targets), f"{name} = {value:g}")
                try:
                    ok, detail = setter(name, value)
                except Exception as ex:
                    ok, detail = False, str(ex)
                done.append((name, value, ok, detail))
            return done

        self.app.submit(work, f"Setting {len(targets)} parameter(s) to the "
                              f"reference…", on_done=self._set_done)

    def _set_done(self, result) -> None:
        if result is None or isinstance(result, Exception):
            messagebox.showwarning("Set to reference", f"Nothing was confirmed: {result}")
            return
        nav = self._nav()
        lines = []
        for name, value, ok, detail in result:
            if ok:
                self._confirmed[name] = (value, time.time())
            lines.append(f"{'✓' if ok else '✖'} {name} = {value:g}: {detail}")
            log.info("parameter set to reference: %s=%g ok=%s (%s)",
                     name, value, ok, detail)
            session = getattr(nav, "session", None)
            if session is not None:
                session.event("param_set", {"param": name, "value": value,
                                            "ok": ok, "detail": detail,
                                            "why": "set to reference"})
        self.app._log("\n".join(lines))
        self._update()

    # ------------------------------------------------------------------
    #  files
    # ------------------------------------------------------------------

    def _open_reference(self) -> None:
        path = paramfile.reference_path(VEHICLE)
        try:
            os.startfile(str(path))                  # noqa: S606 -- the user's own file
        except Exception as ex:
            messagebox.showinfo("Reference file", f"{path}\n\n{ex}")

    def _save_vehicle(self) -> None:
        """The vehicle's parameters as they are now, into the flight folder,
        in the reference file's own format so the two can be diffed."""
        s = self._snapshot()
        params = (s.params if s is not None else None) or {}
        folder = getattr(self.app, "flight_dir", None)
        if not params:
            messagebox.showinfo("Save", "No parameters have been read yet.")
            return
        if folder is None:
            messagebox.showinfo("Save", "Choose a flight folder on Monitoring first.")
            return
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        path = Path(folder) / "logs" / f"parameters_{stamp}.params"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"vehicle": VEHICLE, "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "count": str(len(params))}
        paramfile.write(path, params, meta)
        self.app._log(f"Saved {len(params):,} parameters to {path}.")
