"""
Navigation: planning and following surveys, and the health of what navigates.

This chapter is about *where*: laying out a survey, following it, and knowing
whether the position it is drawn against can be believed. Altitude, velocity,
depth and power used to have gauges here and no longer do -- they are flying
instruments rather than navigation ones, the space they took is worth more to
the map, and the telemetry behind them is still collected, logged, replayed
and used wherever navigation needs it.

    +-----------------------------------+----------------------+
    |                                   |  readiness · profile |
    |              MAP                  |  sensor health       |
    |     (plan drawing lives here)     |                      |
    |                                   |  survey plan:        |
    +-----------------------------------+  tools, features,    |
    |  guidance / site / origin strip    |  inspector          |
    +-----------------------------------+----------------------+

Two thirds of the width is the map, because the map is the problem. The strip
beneath it carries what must never need looking for: which line is being
followed and how far off it the vehicle is, the site, and whether the origin
is confirmed. Details expand into a drawer over the page; the map stays
visible behind them.

**It works with no vehicle.** The map opens on Pier 59 from a basemap that
ships with the program, and a plan can be drawn, measured, saved and reopened
with nothing connected at all. Nothing geographic is gated on telemetry.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import diagnostics
from ..nav import bundled, geo, offline, tiles, waypoints
from ..nav import guidance as GD
from ..nav import model as M
from ..nav import origin as O
from ..nav import plan as P
from ..nav import profiles as PR
from ..nav import replay as RP
from ..nav import session as SESS
from ..nav import trust as TR
from ..nav.collector import NavCollector
from ..nav.model import NO_VALUE, Quality
from . import navdraw, navmap
from . import theme as T
from .navmap import MapCanvas, Marker, format_position
from .navplanpanel import PlanPanel
from .widgets import Card, button, label

log = logging.getLogger(__name__)

#: How often the page reads the collector's snapshot and redraws.
REFRESH_MS = 250

#: Below this logical width the right column is too narrow for the matrix, so
#: it goes under the map instead.
NARROW_PX = 1100

#: The map takes two thirds; the right column is readiness, profile, health
#: and the plan inspector.
MAP_WEIGHT, SIDE_WEIGHT = 64, 36

#: What "follow this line" draws as a corridor unless the operator changes it.
DEFAULT_CORRIDOR_M = 1.0

#: Floors for the two cards in the side column, in logical pixels. Enough for
#: the readiness header plus about six sources, and for the plan's tool bar,
#: a few features and the first inspector fields.
#: The share of the matrix's width the Detail column gets, and the floor
#: below which eliding it stops being useful. 13 of 32 is the column's grid
#: weight; keeping the two in step is what makes the elide match what is
#: actually on screen.
DETAIL_SHARE = 13 / 32
DETAIL_MIN_PX = 120

READINESS_MIN_PX = 260
PLAN_MIN_PX = 240


def _set_label(widget, *, text=None, text_color=None, font=None) -> None:
    """Configure a label only when something has actually changed.

    CustomTkinter's `configure` reads the current values back out of Tk before
    applying, and a profile of this page found ~96 label configures per redraw
    costing about 46 ms -- more than the map. Most of them set a label to
    exactly what it already said.
    """
    cache = getattr(widget, "_nav_cache", None)
    if cache is None:
        cache = widget._nav_cache = {}
    changes = {}
    if text is not None and cache.get("text") != text:
        changes["text"] = text
    if text_color is not None and cache.get("text_color") != text_color:
        changes["text_color"] = text_color
    if font is not None and cache.get("font") != font:
        changes["font"] = font
    if changes:
        cache.update(changes)
        widget.configure(**changes)


class NavigationPage(ctk.CTkFrame):
    """The whole chapter. Owns the collector, the plan, the log and the map."""

    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.app = app
        self.collector = None
        self.session: SESS.NavSession | None = None
        self.waypoints: waypoints.WaypointStore | None = None
        self.bundled = bundled.BundledMaps()
        self.tile_cache = tiles.TileCache(bundled=self.bundled)
        self.prepare = offline.OfflinePrepare(self.tile_cache)

        #: The site the map opens on and recenters to. Pier 59 until something
        #: else is chosen, because that is where this programme dives and
        #: because it is the one place a basemap ships for.
        self.site = dict(bundled.PIER59)

        self.origin_state = O.OriginState()
        self.profile_key = "dvl"
        self.check: PR.CheckResult | None = None
        self.writes_unlocked = False

        #: The plan being drawn, its undo history and where it is saved.
        self.plan = P.Plan(name="Survey plan", site=self.site["key"])
        self.history = P.History(self.plan)
        self.plan_path: Path | None = None
        self.editor = None
        self.swath_m: float = 0.0

        #: What is being followed, if anything.
        self.following: tuple | None = None     # (feature_id, lane_index)
        self.guidance = GD.Guidance()
        self.progress = GD.Progress()

        self._narrow = False
        self._centred_once = False

        self.grid_columnconfigure(0, weight=MAP_WEIGHT, uniform="nav")
        self.grid_columnconfigure(1, weight=SIDE_WEIGHT, uniform="nav")
        self.grid_rowconfigure(0, weight=1)

        self._build_map_column()
        self._build_side_column()

        self.editor = navdraw.PlanEditor(self.map, on_change=self._plan_changed,
                                         on_select=lambda _f: self.plan_panel.refresh())
        self.editor.plan = self.plan
        self.map.editor = self.editor
        self.map.site = self.site

        self.bind("<Configure>", self._on_resize, add="+")
        top = self.winfo_toplevel()
        top.bind("<Escape>", self._escape, add="+")
        top.bind("<Return>", self._enter, add="+")
        self.after(REFRESH_MS, self._tick)
        self.after(600, self._centre_on_site_once)

    # ------------------------------------------------------------------
    #  layout
    # ------------------------------------------------------------------

    def _build_map_column(self) -> None:
        col = ctk.CTkFrame(self, fg_color="transparent")
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        col.grid_columnconfigure(0, weight=1)
        col.grid_rowconfigure(1, weight=1)
        self.map_col = col

        bar = ctk.CTkFrame(col, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.base_menu = ctk.CTkOptionMenu(
            bar, width=190, font=T.FONT_SMALL,
            values=[tiles.SOURCES[k].label for k in tiles.BASE_KEYS],
            command=self._pick_base)
        self.base_menu.set(tiles.SOURCES[tiles.DEFAULT_BASE].label)
        self.base_menu.grid(row=0, column=0, padx=(0, 6))
        button(bar, f"◎ {self.site['short']}", self.centre_on_site, "ghost",
               width=130).grid(row=0, column=1, padx=(0, 6))
        self.follow_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="Follow ROV", variable=self.follow_var,
                        font=T.FONT_SMALL, width=100,
                        command=self._toggle_follow).grid(row=0, column=2)
        button(bar, "Fit", self._fit, "ghost", width=48).grid(row=0, column=3,
                                                              padx=(6, 0))
        button(bar, "Offline…", self._open_offline, "ghost", width=86
               ).grid(row=0, column=4, padx=(6, 0))
        bar.grid_columnconfigure(5, weight=1)
        self.wp_button = button(bar, "＋ Waypoint", self._create_waypoint,
                                "primary", width=120)
        self.wp_button.grid(row=0, column=6)

        self.map = MapCanvas(col, cache=self.tile_cache)
        self.map.grid(row=1, column=0, sticky="nsew")

        readout = ctk.CTkFrame(col, fg_color="transparent")
        readout.grid(row=2, column=0, sticky="ew", pady=(3, 3))
        readout.grid_columnconfigure(0, weight=1)
        self.position_label = label(readout, f"{NO_VALUE}  no position",
                                    muted=True)
        self.position_label.configure(font=T.FONT_MONO, anchor="w")
        self.position_label.grid(row=0, column=0, sticky="ew")

        #: What the track's colours mean, built from the track itself so it
        #: never lists a state the dive did not reach.
        #:
        #: `height=0` matters: an empty CTkFrame asks for its default 200
        #: logical pixels, and before the first fix this one has no chips in
        #: it at all -- which left a third of a metre of blank screen under
        #: the map on a tall window. It grows with its chips.
        self.trust_legend = ctk.CTkFrame(readout, fg_color="transparent",
                                         width=0, height=0)
        self.trust_legend.grid(row=0, column=1, sticky="e")
        self._legend_shown: list = []

        self.trust_label = label(readout, "", muted=True)
        self.trust_label.configure(anchor="w")
        self.trust_label.grid(row=1, column=0, columnspan=2, sticky="ew")
        #: Hidden until there is something to say. An empty label still
        #: occupies a row, and "unknown — no position" under "— no position"
        #: is the same sentence twice.
        self.trust_label.grid_remove()
        self._trust_shown = False

        self._build_strip(col, row=3)

    def _build_strip(self, parent, row: int) -> None:
        """The three things that must never need looking for."""
        strip = ctk.CTkFrame(parent, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                             border_width=1, border_color=T.BORDER)
        strip.grid(row=row, column=0, sticky="ew")
        strip.grid_columnconfigure((0, 1, 2), weight=1, uniform="strip")
        self.strip = strip

        self.strip_guide = _StripCell(strip, "GUIDANCE")
        self.strip_guide.grid(row=0, column=0, sticky="nsew", padx=8, pady=7)
        self.strip_site = _StripCell(strip, "SITE / ORIGIN")
        self.strip_site.grid(row=0, column=1, sticky="nsew", padx=8, pady=7)
        self.strip_home = _StripCell(strip, "TO SITE")
        self.strip_home.grid(row=0, column=2, sticky="nsew", padx=8, pady=7)

    def _build_side_column(self) -> None:
        col = ctk.CTkFrame(self, fg_color="transparent")
        col.grid(row=0, column=1, sticky="nsew")
        col.grid_columnconfigure(0, weight=1)
        # `uniform` matters more than the weights here. Without it Tk hands
        # each row its requested height first and splits only the remainder,
        # and the plan panel -- a scrollable list plus an inspector full of
        # entry boxes -- asks for far more than the matrix does. The readiness
        # card ended up one row tall, which is the opposite of the point of
        # taking the flight gauges out of this chapter. In a uniform group the
        # two are sized in proportion to their weights whatever they ask for,
        # and the minsize keeps the header and several sources visible even in
        # a short window.
        col.grid_rowconfigure(0, weight=48, uniform="navside",
                              minsize=READINESS_MIN_PX)
        col.grid_rowconfigure(1, weight=52, uniform="navside",
                              minsize=PLAN_MIN_PX)
        self.side_col = col

        top = Card(col, "Navigation readiness",
                   "Configured, receiving, valid and fused are four different "
                   "questions.")
        top.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        top.body.grid_rowconfigure(1, weight=1)
        top.body.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(top.body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        head.grid_columnconfigure(2, weight=1)
        self.profile_menu = ctk.CTkOptionMenu(
            head, width=170, font=T.FONT_SMALL,
            values=[PR.PROFILES[k].label for k in PR.PROFILE_ORDER],
            command=self._pick_profile)
        self.profile_menu.set(PR.PROFILES[self.profile_key].label)
        self.profile_menu.grid(row=0, column=0)
        self.check_label = label(head, "not checked", muted=True)
        self.check_label.grid(row=0, column=1, padx=(8, 0))
        button(head, "Start…", self._open_start, "ghost", width=70
               ).grid(row=0, column=3, padx=(4, 0))
        button(head, "Health…", self._open_details, "ghost", width=78
               ).grid(row=0, column=4, padx=(4, 0))
        self.apply_button = button(head, "Apply (locked)", self._open_apply,
                                   "primary", width=118)
        self.apply_button.grid(row=0, column=5, padx=(4, 0))

        self.matrix = _SensorMatrix(top.body)
        self.matrix.grid(row=1, column=0, sticky="nsew")

        foot = ctk.CTkFrame(top.body, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.foot_label = label(foot, "Not connected.", muted=True)
        self.foot_label.configure(anchor="w")
        self.foot_label.grid(row=0, column=0, sticky="ew")
        self.unlock_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(foot, text="Allow writes", variable=self.unlock_var,
                        font=T.FONT_SMALL, width=110,
                        command=self._toggle_unlock).grid(row=0, column=1)

        plan_card = Card(col, "Survey plan",
                         "Measured lines, rotated boxes and the lanes that "
                         "fill them. Metres, never pixels.")
        plan_card.grid(row=1, column=0, sticky="nsew")
        plan_card.body.grid_rowconfigure(0, weight=1)
        plan_card.body.grid_columnconfigure(0, weight=1)
        self.plan_panel = PlanPanel(plan_card.body, self)
        self.plan_panel.grid(row=0, column=0, sticky="nsew")

    def _on_resize(self, event=None) -> None:
        try:
            w = self.winfo_width()
            scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:
            return
        self._on_resize_to(w / scaling)

    def _on_resize_to(self, logical_width: float) -> None:
        """The layout decision, separated from measuring the window.

        Split out so it can be exercised without a visible window: a
        withdrawn Tk window reports 1 px for its children, which makes a
        pixel-measuring test prove nothing at all.
        """
        narrow = logical_width < NARROW_PX
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            self.grid_columnconfigure(1, weight=0, uniform="")
            self.grid_rowconfigure((0, 1), weight=1, uniform="navrow")
            self.side_col.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        else:
            self.grid_columnconfigure(0, weight=MAP_WEIGHT, uniform="nav")
            self.grid_columnconfigure(1, weight=SIDE_WEIGHT, uniform="nav")
            self.grid_rowconfigure(1, weight=0)
            self.side_col.grid(row=0, column=1, sticky="nsew", pady=0)

    # ------------------------------------------------------------------
    #  the site
    # ------------------------------------------------------------------

    def _centre_on_site_once(self) -> None:
        """Open on the site, once, and then leave the view alone.

        A first launch with nothing connected must still show somewhere real.
        After that the operator's own panning, and Follow when they turn it
        on, decide what is on screen -- the map must not keep dragging itself
        back to Pier 59 while somebody is working at another pier.
        """
        if self._centred_once:
            return
        self._centred_once = True
        if self.map.centre is None:
            self.centre_on_site()

    def centre_on_site(self) -> None:
        self.map.centre = (self.site["lat"], self.site["lon"])
        self.map.zoom = self.site.get("zoom", 18)
        self.map.follow = False
        self.follow_var.set(False)
        self.map.draw()

    # ------------------------------------------------------------------
    #  the plan
    # ------------------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        if self.editor is not None:
            self.editor.set_tool(tool)
            self.plan_panel.refresh()

    def select(self, feature_id: str | None) -> None:
        if self.editor is not None:
            self.editor.select(feature_id)
            self.plan_panel.refresh()

    def _plan_changed(self, label_text: str) -> None:
        self.history.record(self.plan, label_text)
        self._autosave()
        self.plan_panel.refresh()
        if self.session is not None:
            self.session.event("plan_edit",
                               {"what": label_text,
                                "revision": self.plan.revision,
                                "features": len(self.plan.features)})

    def _autosave(self) -> None:
        folder = getattr(self.app, "flight_dir", None)
        if folder is None:
            return
        path = self.plan_path or (Path(folder) / P.FILENAME)
        self.plan_path = path
        if not self.plan.save(path):
            log.warning("survey plan could not be autosaved to %s", path)

    def undo(self) -> None:
        got = self.history.undo()
        if got is not None:
            self._adopt(got)

    def redo(self) -> None:
        got = self.history.redo()
        if got is not None:
            self._adopt(got)

    def _adopt(self, plan: P.Plan) -> None:
        self.plan = plan
        if self.editor is not None:
            self.editor.plan = plan
            if self.editor.selected_id and plan.get(self.editor.selected_id) is None:
                self.editor.selected_id = None
        self._autosave()
        self.plan_panel.refresh()
        self.map.draw()

    def new_plan(self) -> None:
        if self.plan.features and not messagebox.askyesno(
                "Survey plan",
                f"Discard {len(self.plan.features)} feature(s) and start a "
                f"new plan?", icon="warning", default="no"):
            return
        self.plan = P.Plan(name="Survey plan", site=self.site["key"])
        self.history = P.History(self.plan)
        self.plan_path = None
        self._adopt(self.plan)

    def open_plan(self) -> None:
        start = str(self.app.flight_dir) if getattr(self.app, "flight_dir",
                                                    None) else None
        chosen = filedialog.askopenfilename(
            title="Open a survey plan", initialdir=start,
            filetypes=[("Survey plan", "*.json"), ("GeoJSON", "*.geojson"),
                       ("All files", "*.*")])
        if not chosen:
            return
        path = Path(chosen)
        try:
            if path.suffix.lower() == ".geojson":
                feats, problems = waypoints.read_planned(path)
                self.map.planned = feats
                self.map.draw()
                messagebox.showinfo(
                    "Survey plan",
                    f"Imported {len(feats)} feature(s) as a read-only overlay."
                    + ("\n\nNot imported:\n  " + "\n  ".join(problems[:10])
                       if problems else ""))
                return
            self.plan = P.Plan.load(path)
            self.plan_path = path
            self.history = P.History(self.plan)
            self._adopt(self.plan)
            self._fit()
        except Exception as ex:
            messagebox.showerror("Survey plan", f"Could not open {path.name}:\n{ex}")

    def save_plan_as(self) -> None:
        start = str(self.app.flight_dir) if getattr(self.app, "flight_dir",
                                                    None) else None
        chosen = filedialog.asksaveasfilename(
            title="Save the survey plan", initialdir=start,
            defaultextension=".json", initialfile=P.FILENAME,
            filetypes=[("Survey plan", "*.json")])
        if not chosen:
            return
        self.plan_path = Path(chosen)
        if self.plan.save(self.plan_path):
            messagebox.showinfo("Survey plan", f"Saved to {self.plan_path.name}.")
        else:
            messagebox.showerror("Survey plan", "Could not save.")

    def export_plan(self) -> None:
        start = str(self.app.flight_dir) if getattr(self.app, "flight_dir",
                                                    None) else None
        chosen = filedialog.asksaveasfilename(
            title="Export as GeoJSON", initialdir=start,
            defaultextension=".geojson", initialfile="survey_plan.geojson",
            filetypes=[("GeoJSON", "*.geojson")])
        if not chosen:
            return
        try:
            import json
            data = self.plan.to_geojson()
            Path(chosen).write_text(json.dumps(data, indent=1), encoding="utf-8")
            messagebox.showinfo(
                "Survey plan",
                f"Exported {len(data['features'])} feature(s).\n\nRectangles "
                f"and grids keep their dimensions and lanes in the feature "
                f"properties, so reopening this file gives editable shapes "
                f"rather than flattened lines.")
        except ValueError as ex:
            messagebox.showwarning("Survey plan", str(ex))
        except Exception as ex:
            messagebox.showerror("Survey plan", f"Could not export:\n{ex}")

    def toggle_hidden(self, fid: str) -> None:
        f = self.plan.get(fid)
        if f is not None:
            f.hidden = not f.hidden
            self._plan_changed("hide" if f.hidden else "show")

    def toggle_locked(self, fid: str) -> None:
        f = self.plan.get(fid)
        if f is not None:
            f.locked = not f.locked
            self._plan_changed("lock" if f.locked else "unlock")

    def delete_feature(self, fid: str) -> None:
        f = self.plan.get(fid)
        if f is None:
            return
        self.plan.remove(fid)
        if self.editor is not None and self.editor.selected_id == fid:
            self.editor.selected_id = None
        self._plan_changed(f"delete {f.name}")

    def reverse_line(self) -> None:
        f = self.editor.selected() if self.editor else None
        if isinstance(f, P.Line):
            f.reverse()
            self._plan_changed("reverse")

    def make_parallels(self) -> None:
        f = self.editor.selected() if self.editor else None
        if not isinstance(f, P.Line):
            return
        dlg = ctk.CTkInputDialog(
            text="How many parallel lines, and how far apart?\n"
                 "For example: 3 at 2", title="Parallel lines")
        got = dlg.get_input()
        if not got:
            return
        try:
            parts = got.replace("at", " ").replace("@", " ").split()
            count, spacing = int(parts[0]), float(parts[1])
            for copy in f.parallels(count, spacing):
                self.plan.add(copy)
            self._plan_changed(f"{count} parallels")
        except Exception as ex:
            messagebox.showwarning("Parallel lines",
                                   f"Could not read that: {ex}")

    def apply_fields(self) -> None:
        """Type exact dimensions, rather than dragging until they look right."""
        f = self.editor.selected() if self.editor else None
        if f is None:
            return
        v = self.plan_panel.values()
        try:
            if v.get("name"):
                f.name = str(v["name"]).strip() or f.name
            if isinstance(f, P.Line) and "length_m" in v:
                f.set_length_bearing(float(v["length_m"]),
                                     float(v["bearing_deg"]),
                                     fix=v.get("fix", "start"))
            if isinstance(f, (P.Rect, P.Grid)):
                corner = v.get("anchor_corner", "centre")
                idx = None if corner == "centre" else int(corner) - 1
                f.rotation_deg = float(v.get("rotation_deg", f.rotation_deg))
                f.set_size(float(v["length_m"]), float(v["width_m"]),
                           anchor_corner=idx)
            if isinstance(f, P.Grid):
                spacing = float(v.get("spacing_m", f.spacing_m))
                if not (P.MIN_SPACING_M <= spacing <= P.MAX_SPACING_M):
                    raise ValueError(
                        f"lane spacing must be between {P.MIN_SPACING_M} and "
                        f"{P.MAX_SPACING_M} m")
                f.spacing_m = spacing
                f.lane_axis = v.get("lane_axis", f.lane_axis)
                f.start_corner = int(v.get("start_corner", 1)) - 1
                f.boustrophedon = bool(v.get("boustrophedon", True))
                f.second_pass = bool(v.get("second_pass", False))
                try:
                    self.swath_m = float(v.get("swath_m") or 0.0)
                except ValueError:
                    self.swath_m = 0.0
            if isinstance(f, P.Circle) and "radius_m" in v:
                f.radius_m = max(0.25, float(v["radius_m"]))
            f.touch()
            self._plan_changed("edit dimensions")
        except Exception as ex:
            messagebox.showwarning("Survey plan", f"Could not apply: {ex}")

    # ------------------------------------------------------------------
    #  following a line
    # ------------------------------------------------------------------

    def fly_selected(self) -> None:
        f = self.editor.selected() if self.editor else None
        if f is None:
            return
        if isinstance(f, P.Grid):
            lanes = f.lanes()
            nxt = next((x for x in lanes if x.state != "done"), None)
            if nxt is None:
                messagebox.showinfo("Guidance", "Every lane is marked done.")
                return
            self.following = (f.id, nxt.index)
        elif isinstance(f, P.Line):
            self.following = (f.id, None)
        else:
            messagebox.showinfo(
                "Guidance",
                "Guidance follows a line or a grid lane. Draw one, or pick a "
                "grid and its next lane will be offered.")
            return
        self.progress = GD.Progress()
        if self.session is not None:
            self.session.event("follow_start", {
                "plan": self.plan.name, "revision": self.plan.revision,
                "feature": f.id, "feature_name": f.name,
                "lane": self.following[1]})
        self.plan_panel.refresh()

    def stop_following(self, why: str = "stopped") -> None:
        if self.following and self.session is not None:
            self.session.event("follow_end", {
                "feature": self.following[0], "lane": self.following[1],
                "why": why, "progress": round(self.progress.fraction, 3),
                "wander": self.progress.wander()})
        self.following = None
        self.guidance = GD.Guidance()

    def _following_line(self):
        """(anchor, start, end, name, lane) for whatever is being followed."""
        if not self.following:
            return None
        f = self.plan.get(self.following[0])
        if f is None:
            return None
        lane_i = self.following[1]
        if isinstance(f, P.Grid) and lane_i is not None:
            for lane in f.lanes() + f.second_pass_lanes():
                if lane.index == lane_i:
                    return f.anchor, lane.start, lane.end, f.name, lane
            return None
        if isinstance(f, P.Line) and len(f.points) >= 2:
            return f.anchor, f.points[0], f.points[-1], f.name, None
        return None

    # ------------------------------------------------------------------
    #  keyboard
    # ------------------------------------------------------------------

    def _escape(self, _event=None) -> None:
        if self.editor is None or not self.winfo_viewable():
            return
        if self.editor.drawing or self.editor._draft:
            self.editor.cancel_draft()
            self.editor.set_tool("pan")
            self.plan_panel.refresh()

    def _enter(self, _event=None) -> None:
        if self.editor is not None and self.winfo_viewable():
            if self.editor.finish():
                self.plan_panel.refresh()

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        folder = getattr(self.app, "flight_dir", None)
        if folder is not None and (self.waypoints is None
                                   or self.waypoints.folder != Path(folder)):
            self.waypoints = waypoints.WaypointStore(Path(folder))
            saved = Path(folder) / P.FILENAME
            if saved.is_file() and not self.plan.features:
                try:
                    self.plan = P.Plan.load(saved)
                    self.plan_path = saved
                    self.history = P.History(self.plan)
                    self._adopt(self.plan)
                except Exception as ex:
                    log.warning("survey plan at %s could not be read: %s",
                                saved, ex)
            self._load_overlay(Path(folder) / waypoints.PLANNED_FILENAME,
                               quiet=True)
            self._sync_markers()
        if self.collector is None:
            self._start_live()
        self._render()

    def _start_live(self) -> None:
        host = (self.app.vehicle_host() or "192.168.2.2"
                if hasattr(self.app, "vehicle_host") else "192.168.2.2")
        self._open_session("live")
        self.collector = NavCollector(host, allow_writes=False,
                                      on_event=self._collector_event)
        self._sync_profile_to_collector()
        try:
            from .. import blueos
            self.collector.set_parameter_reader(
                lambda: blueos.read_parameters_now(host)[0])
        except Exception as ex:
            log.warning("navigation could not wire the parameter reader: %s", ex)
        self.collector.start(self.session.session_id if self.session else "")

    def _open_session(self, mode: str) -> None:
        folder = getattr(self.app, "flight_dir", None)
        if folder is None:
            return
        if self.session is not None:
            self.session.close("replaced")
        flight_id = time.strftime("%Y-%m-%d_%H%M%S")
        rec = getattr(self.app, "recorder", None)
        if rec is not None and getattr(rec.status, "flight_id", ""):
            flight_id = rec.status.flight_id
        self.session = SESS.NavSession(Path(folder) / "logs", flight_id,
                                       mode=mode)
        self.session.manifest.profiles_version = PR.PROFILES_VERSION
        self.session.manifest.thresholds = {
            "corridor_m": DEFAULT_CORRIDOR_M,
            "site": self.site["name"],
            "bundled_map": self.bundled.summary(),
        }
        self.session.open()

    def _collector_event(self, kind: str, data: dict) -> None:
        if self.session is not None:
            self.session.event(kind, data)

    def shutdown(self, *, wait: float = 0.0) -> None:
        """Stop everything this page owns, without holding the window."""
        if self.collector is not None:
            self.collector.stop(wait)
        self.prepare.cancel()
        self.tile_cache.stop(wait)
        self.bundled.close()
        if self.plan.features:
            self._autosave()
        if self.session is not None:
            self.session.close("window closed")

    # ------------------------------------------------------------------
    #  the refresh loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._render()
        except Exception as ex:
            diagnostics.log_exception("navigation redraw", *sys.exc_info(),
                                      level=logging.WARNING)
            try:
                _set_label(self.foot_label, text=f"Redraw failed: {ex}",
                           text_color=T.ERROR)
            except Exception:
                pass
        self.after(REFRESH_MS, self._tick)

    def _render(self) -> None:
        if self.collector is None:
            self.map.draw()
            return
        s = self.collector.snapshot()
        now = time.monotonic()
        self._render_map(s, now)
        self._render_guidance(s, now)
        self._render_strip(s, now)
        self.matrix.update_from(s, now)
        self._render_foot(s)
        self.plan_panel.refresh()

    def _render_map(self, s, now: float) -> None:
        m = self.map
        m.rov_fix = s.rov_fix
        m.vessel_fix = s.vessel_fix if self.profile_key == "acoustic" else None
        vh = s.vessel_heading
        m.vessel_heading = vh.number() if vh is not None else None
        m.rov_heading = s.heading.number()
        if self.collector is not None:
            m.rov_track = list(self.collector.rov_track)
            m.vessel_track = (list(self.collector.vessel_track)
                              if self.profile_key == "acoustic" else [])
        # The map is never put into the local-only view now: a plan is drawn
        # against real geography whether or not the vehicle knows where it is.
        # A vehicle with no geographic position simply has no marker on it.
        m.local_only = False
        m.draw()
        _set_label(self.position_label,
                   text=format_position(s.rov_fix, now=now),
                   text_color=(T.TEXT if s.rov_fix
                               and s.rov_fix.quality is Quality.OK else T.WARN))
        self._render_trust(s, now)

    def _render_trust(self, s, now: float) -> None:
        """What the position rests on, in one line, plus the track's key.

        Separate from the coordinates on purpose: an operator who reads a
        latitude and a longitude has been told where the vehicle is, and not
        how much that claim is worth.
        """
        state, note = TR.track_state(s, now, profile_key=self.profile_key)
        jumps = getattr(self.collector, "jumps", None)

        if s.rov_fix is None and not jumps:
            # The position readout above already says there is no position;
            # repeating it in different words helps nobody.
            line = ""
        else:
            line = f"{navmap.TRUST_WORDS.get(state, state)} — {note}"
            if self.profile_key == "acoustic":
                line += "  ·  " + TR.acoustic_line(s, now)
            if jumps:
                line += (f"  ·  {len(jumps)} position jump(s), newest: "
                         f"{jumps[-1].line()}")

        if bool(line) != self._trust_shown:
            self._trust_shown = bool(line)
            if line:
                self.trust_label.grid()
            else:
                self.trust_label.grid_remove()
        if line:
            _set_label(self.trust_label, text=line[:190],
                       text_color=(T.TEXT_MUTED if state in (TR.ABSOLUTE,
                                                             TR.RELATIVE)
                                   else T.WARN))

        styles = self.map.trust_styles()
        if [x[0] for x in styles] == self._legend_shown:
            return
        self._legend_shown = [x[0] for x in styles]
        for kid in self.trust_legend.winfo_children():
            kid.destroy()
        for i, (_state, text, colour, dash) in enumerate(styles):
            chip = ctk.CTkLabel(self.trust_legend,
                                text=("- - " if dash else "— ") + text,
                                font=T.FONT_SMALL, text_color=colour)
            chip.grid(row=0, column=i, padx=(8, 0))

    def _render_guidance(self, s, now: float) -> None:
        got = self._following_line()
        if got is None:
            self.guidance = GD.Guidance()
            return
        anchor, start, end, name, lane = got
        hdg = s.heading
        self.guidance = GD.follow(
            anchor, start, end, s.rov_fix, plan_name=name,
            feature_id=self.following[0], lane_index=self.following[1],
            corridor_m=DEFAULT_CORRIDOR_M,
            heading_deg=hdg.number(),
            # The compass's north reference has not been verified against
            # anything, so a relative turn is not offered. Saying so is the
            # point: a magnetic heading against a true bearing is 15 degrees
            # wrong at Seattle.
            heading_referenced=False, now_mono=now)
        self.progress.update(self.guidance, s.rov_fix)
        self.map.guidance = self.guidance

    def _render_strip(self, s, now: float) -> None:
        g = self.guidance
        if self.following and g.valid:
            side = "right" if g.cross_track_m >= 0 else "left"
            self.strip_guide.title(
                f"FOLLOWING {g.plan_name}"
                + (f" · lane {g.lane_index}" if g.lane_index else ""))
            self.strip_guide.set(
                f"{abs(g.cross_track_m):.1f} m {side}",
                f"{g.along_m:.0f} / {g.line_length_m:.0f} m along · "
                f"{g.remaining_m:.0f} m to run · {self.progress.line()}",
                T.OK if g.within_corridor else T.WARN)
        elif self.following:
            self.strip_guide.title("FOLLOWING")
            self.strip_guide.set(NO_VALUE, g.reason or "no guidance", T.WARN)
        else:
            self.strip_guide.title("GUIDANCE")
            self.strip_guide.set("—", "pick a line or grid and press "
                                      "Fly this line", T.TEXT_MUTED)

        st = self.origin_state
        origin_bit = (f"origin {st.active_lat:.5f}, {st.active_lon:.5f}"
                      if st.active is True and st.active_lat is not None
                      else "no confirmed origin" if st.active is False
                      else "origin not confirmed")
        self.strip_site.set(
            self.site["short"],
            f"{self.site['lat']:.5f}, {self.site['lon']:.5f} · {origin_bit}",
            T.OK if st.active is True else
            T.ERROR if st.active is False else T.WARN)

        self._render_home(s, now)

    def _render_home(self, s, now: float) -> None:
        """Bearing to the vessel when there is one, else to the launch site."""
        rov = s.rov_fix
        if self.profile_key == "acoustic" and s.vessel_fix is not None:
            self.strip_home.title("TO VESSEL")
            target, tone, note = s.vessel_fix, T.OK, "vessel"
            if target.quality is not Quality.OK:
                self.strip_home.set(NO_VALUE, "vessel position stale — no "
                                              "bearing from it", T.WARN)
                return
        else:
            self.strip_home.title("TO SITE")
            target = M.Fix(lat=self.site["lat"], lon=self.site["lon"],
                           kind="manual")
            tone, note = T.TEXT_MUTED, f"{self.site['short']} — a fixed mark"
        if rov is None or rov.quality is not Quality.OK:
            self.strip_home.set(NO_VALUE, "no usable ROV position", T.WARN)
            return
        dist, brg, _ = geo.inverse(rov.lat, rov.lon, target.lat, target.lon)
        if dist < geo.MIN_BEARING_M:
            self.strip_home.set("at/near target",
                                f"{dist:.1f} m — too close for a stable "
                                f"bearing", tone)
            return
        if rov.kind == "dead":
            note += " · dead-reckoned, drifts as a whole"
        self.strip_home.set(f"{geo.format_bearing(brg)} · {dist:,.0f} m",
                            note, tone)

    def _render_foot(self, s) -> None:
        bits = [s.link.line()]
        if self.session is not None and not self.session.healthy:
            bits.append(self.session.status_line())
        if self.prepare.running and self.prepare.job:
            bits.append(self.prepare.job.line())
        if self.tile_cache.failures:
            bits.append(f"map: {self.tile_cache.last_error}")
        if s.link.mode == "replay":
            bits.insert(0, "REPLAY — not a live vehicle")
        colour = (T.WARN if (s.link.problem or not s.link.connected
                             or s.link.mode == "replay") else T.TEXT_MUTED)
        _set_label(self.foot_label, text="  ·  ".join(bits)[:150],
                   text_color=colour)
        can = self._can_write()
        self.apply_button.configure(
            state="normal" if can else "disabled",
            text="Review & apply…" if can else "Apply (locked)")

    # ------------------------------------------------------------------
    #  controls
    # ------------------------------------------------------------------

    def _pick_base(self, label_text: str) -> None:
        for k in tiles.BASE_KEYS:
            if tiles.SOURCES[k].label == label_text:
                self.map.set_base(k)
                return

    def _toggle_follow(self) -> None:
        self.map.follow = self.follow_var.get()
        if self.map.follow and self.map.rov_fix is not None:
            self.map.centre_on_vehicle()

    def _fit(self) -> None:
        pts = [(f.anchor.to_geo(*p)) for f in self.plan.features
               for p in f.local_points()]
        if pts:
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            self.map.centre = ((min(lats) + max(lats)) / 2,
                               (min(lons) + max(lons)) / 2)
            span = max(geo.distance_m(min(lats), self.map.centre[1],
                                      max(lats), self.map.centre[1]),
                       geo.distance_m(self.map.centre[0], min(lons),
                                      self.map.centre[0], max(lons)), 30.0)
            w = max(200, self.map.canvas.winfo_width())
            h = max(200, self.map.canvas.winfo_height())
            self.map.zoom = max(3, min(19, geo.zoom_for_span(
                self.map.centre[0], span * 1.4, min(w, h))))
            self.map.follow = False
            self.follow_var.set(False)
            self.map.draw()
        else:
            self.map.fit_track()

    def _open_offline(self) -> None:
        from .navdialogs import OfflineDialog
        OfflineDialog(self)

    def _open_start(self) -> None:
        from .navdialogs import StartDialog
        StartDialog(self)

    def _pick_profile(self, label_text: str) -> None:
        for k, p in PR.PROFILES.items():
            if p.label == label_text:
                if k != self.profile_key:
                    self.profile_key = k
                    self._sync_profile_to_collector()
                    if self.session is not None:
                        self.session.event("profile_selected", {"profile": k})
                    self._revalidate()
                return

    def _sync_profile_to_collector(self) -> None:
        """The collector tags each track point with what it rested on, and in
        the acoustic profile that judgement needs to know the profile."""
        c = self.collector
        if c is not None and hasattr(c, "profile_key"):
            c.profile_key = self.profile_key

    def _toggle_unlock(self) -> None:
        want = self.unlock_var.get()
        if want:
            s = self.collector.snapshot() if self.collector else None
            if s is not None and s.armed.number():
                self.unlock_var.set(False)
                messagebox.showwarning(
                    "Navigation",
                    "The vehicle is armed. Parameter changes are not made to "
                    "an armed vehicle — disarm it first.")
                return
            if s is not None and s.link.mode == "replay":
                self.unlock_var.set(False)
                messagebox.showinfo("Navigation",
                                    "This is a replay. There is no vehicle "
                                    "to write to.")
                return
            if not messagebox.askyesno(
                "Navigation",
                "Allow this program to write ArduSub parameters to the "
                "vehicle?\n\nNothing is written until you review and confirm "
                "a specific change. This permission is forgotten when the "
                "program closes.", icon="warning", default="no"):
                self.unlock_var.set(False)
                return
        self.writes_unlocked = want
        if self.collector is not None and self.collector.mav is not None:
            self.collector.mav.allow_writes = want
        if self.session is not None:
            self.session.event("writes_unlocked", {"unlocked": want})
        log.info("navigation writes %s", "unlocked" if want else "locked")

    def _can_write(self) -> bool:
        if not self.writes_unlocked or self.collector is None:
            return False
        if getattr(self.collector, "mav", None) is None:
            return False
        s = self.collector.snapshot()
        return not s.armed.number()

    def _create_waypoint(self) -> None:
        if self.waypoints is None:
            messagebox.showinfo("Navigation",
                                "Choose a flight folder on Monitoring first — "
                                "waypoints are saved into it.")
            return
        s = self.collector.snapshot() if self.collector else None
        fix = s.rov_fix if s else None
        try:
            wp = self.waypoints.capture(
                fix, profile=self.profile_key,
                session=self.session.session_id if self.session else "",
                origin=((self.origin_state.active_lat,
                         self.origin_state.active_lon)
                        if self.origin_state.active_lat is not None else None),
                depth_m=s.depth.number() if s else None,
                altitude_m=s.altitude.number() if s else None)
        except waypoints.CaptureRefused as ex:
            messagebox.showwarning("Create waypoint", str(ex))
            return
        if self.session is not None:
            self.session.event("waypoint", wp.to_json())
        self._sync_markers()
        self._rename_waypoint(wp)

    def _rename_waypoint(self, wp) -> None:
        dlg = ctk.CTkInputDialog(
            text=f"Saved {wp.name} at {wp.lat:.6f}, {wp.lon:.6f}.\n"
                 f"Name it, or cancel to keep {wp.name}.",
            title="Name this waypoint")
        name = dlg.get_input()
        if name and name.strip() and self.waypoints is not None:
            self.waypoints.rename(wp.id, name.strip())
            if self.session is not None:
                self.session.event("waypoint_renamed",
                                   {"id": wp.id, "name": name.strip()})
            self._sync_markers()

    def _sync_markers(self) -> None:
        marks: list[Marker] = []
        if self.waypoints is not None:
            marks += [Marker(w.lat, w.lon, w.name, "waypoint")
                      for w in self.waypoints.points]
        o = self.origin_state
        if o.active_lat is not None and o.active_lon is not None:
            marks.append(Marker(o.active_lat, o.active_lon, "EKF origin",
                                "origin"))
        self.map.markers = marks
        self.map.draw()

    def _load_overlay(self, path: Path, *, quiet: bool = False) -> None:
        if quiet and not path.is_file():
            return
        features, problems = waypoints.read_planned(path)
        self.map.planned = features
        self.map.draw()
        if self.session is not None:
            self.session.event("overlay_imported", {
                "path": str(path), "features": len(features),
                "problems": problems})

    # ------------------------------------------------------------------
    #  validation and dialogs
    # ------------------------------------------------------------------

    def _revalidate(self) -> None:
        if self.collector is None:
            return
        s = self.collector.snapshot()
        params = s.params or None
        dvl = s.dvl.get("message_type")
        self.check = PR.check(
            PR.PROFILES[self.profile_key], params,
            params_age=s.params_age,
            dvl_message_type=(dvl.value if dvl is not None
                              and isinstance(dvl.value, str) else ""),
            dvl_reachable=bool(s.dvl.get("extension")
                               and s.dvl["extension"].quality.has_value),
            origin_set=self.origin_state.active,
            vessel_feeding=_vessel_feeding(s))
        self.origin_state = O.detect(params, active=self.origin_state.active)
        if self.check is not None:
            _set_label(self.check_label, text=self.check.summary()[:40],
                       text_color=T.OK if self.check.matches else T.WARN)
            if self.session is not None:
                self.session.event("profile_check", {
                    "profile": self.profile_key,
                    "matches": self.check.matches,
                    "blockers": [f.param for f in self.check.blockers],
                    "notes": [t for _s, t in self.check.notes]})

    def _open_details(self) -> None:
        self._revalidate()
        from .navdialogs import DetailsDrawer
        DetailsDrawer(self, self.collector.snapshot() if self.collector else None,
                      self.check, self.origin_state)

    def save_diagnostic_snapshot(self):
        """Write everything needed to work out what happened, later.

        Reuses the session log and the parameters already being read; it
        starts nothing and asks the vehicle for nothing, because this gets
        pressed when something has already gone wrong.
        """
        from ..nav import snapshot as SNAP

        folder = getattr(self.app, "flight_dir", None)
        if folder is None:
            messagebox.showinfo(
                "Diagnostic snapshot",
                "Choose a flight folder on 1 Monitoring first — the snapshot "
                "is saved beside the flight it describes.")
            return None
        s = self.collector.snapshot() if self.collector else M.NavSnapshot()
        events = []
        if self.session is not None:
            try:
                events = SESS.read_events(self.session.events_path)
            except Exception:
                events = []
        bundle = SNAP.build(
            s, time.monotonic(), profile_key=self.profile_key,
            origin_confirmed=bool(getattr(self.origin_state, "confirmed",
                                          False)),
            events=events, params_age_s=s.params_age, site=dict(self.site),
            jumps=list(getattr(self.collector, "jumps", []) or []),
            plan_summary={"name": self.plan.name,
                          "features": len(self.plan.features),
                          "revision": self.plan.revision})
        path = SNAP.save(bundle, folder)
        if path is None:
            messagebox.showwarning(
                "Diagnostic snapshot",
                "The snapshot could not be written. The diagnostics log has "
                "the reason.")
            return None
        if self.session is not None:
            self.session.event("diagnostic_snapshot", {"file": path.name})
        messagebox.showinfo("Diagnostic snapshot", f"Saved {path.name}")
        return path

    def _open_origin(self) -> None:
        from .navdialogs import OriginDialog
        OriginDialog(self)

    def _open_apply(self) -> None:
        self._revalidate()
        from .navdialogs import ApplyDialog
        ApplyDialog(self, self.check)

    def start_replay(self, frames, label_text: str, *, synthetic: bool = False
                     ) -> None:
        if self.collector is not None:
            self.collector.stop()
        self._open_session("replay")
        self.collector = RP.ReplayCollector(
            frames, label=label_text, synthetic=synthetic,
            on_event=self._collector_event)
        self.writes_unlocked = False
        self.unlock_var.set(False)
        self.collector.start(self.session.session_id if self.session else "")
        self.map.rov_track = []
        log.info("navigation replay started: %s (%d frames)", label_text,
                 len(frames))


# --------------------------------------------------------------------------
#  pieces
# --------------------------------------------------------------------------


class _StripCell(ctk.CTkFrame):
    """One cell of the strip under the map."""

    def __init__(self, master, title: str, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_columnconfigure(0, weight=1)
        self._title = ctk.CTkLabel(self, text=title, font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
        self._title.grid(row=0, column=0, sticky="ew")
        self._value = ctk.CTkLabel(self, text=NO_VALUE, font=T.FONT_H2,
                                   text_color=T.TEXT, anchor="w")
        self._value.grid(row=1, column=0, sticky="ew")
        self._note = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                  text_color=T.TEXT_MUTED, anchor="w")
        self._note.grid(row=2, column=0, sticky="ew")

    def title(self, text: str) -> None:
        _set_label(self._title, text=text[:46])

    def set(self, value: str, note: str = "", tone=None) -> None:
        _set_label(self._value, text=value, text_color=tone or T.TEXT)
        _set_label(self._note, text=(note or "")[:86])


class _SensorMatrix(ctk.CTkScrollableFrame):
    """Configured / receiving / valid / fused, per source.

    Four columns rather than three. The old third column said "Used" and
    inferred it from general estimator flags, which is not evidence that a
    particular sensor is being fused -- so it is now split: **valid** is what
    the measurement itself says, and **fused** carries the estimator's own
    evidence with its basis, or a question mark when there is none.
    """

    ROWS = (
        ("acoustic", "Acoustic position"),
        ("vessel_gga", "Vessel GNSS position"),
        ("vessel_hdt", "Vessel heading (HDT)"),
        ("dvl_position", "DVL position"),
        ("dvl_velocity", "DVL velocity"),
        ("compass", "ROV compass"),
        ("imu", "IMU / attitude"),
        ("depth", "Depth"),
        ("range", "Downward range"),
        ("ekf", "Estimator"),
    )

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_columnconfigure(0, weight=7, uniform="mx")
        for i in range(1, 5):
            self.grid_columnconfigure(i, weight=3, uniform="mx")
        self.grid_columnconfigure(5, weight=13, uniform="mx")
        heads = ("Source", "Conf", "Recv", "Valid", "Fused", "Detail")
        for i, text in enumerate(heads):
            ctk.CTkLabel(self, text=text, font=T.FONT_SMALL,
                         text_color=T.TEXT_MUTED,
                         anchor="w").grid(row=0, column=i, sticky="ew",
                                          padx=3, pady=(0, 3))
        self._cells: dict[str, tuple] = {}
        for r, (key, name) in enumerate(self.ROWS, start=1):
            n = ctk.CTkLabel(self, text=name, font=T.FONT_SMALL, anchor="w")
            n.grid(row=r, column=0, sticky="ew", padx=3, pady=1)
            marks = []
            for col in range(1, 5):
                lab = ctk.CTkLabel(self, text="–", font=T.FONT_SMALL,
                                   text_color=T.TEXT_MUTED, anchor="w")
                lab.grid(row=r, column=col, sticky="ew", padx=3)
                marks.append(lab)
            detail = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                  text_color=T.TEXT_MUTED, anchor="w")
            detail.grid(row=r, column=5, sticky="ew", padx=3)
            detail._full = ""
            self._cells[key] = (*marks, detail)
        self._detail_px = 0
        self.bind("<Configure>", self._refit)

    def _refit(self, _event=None) -> None:
        """Give the detail column a width, and fit its sentences to it.

        Without this the column has no width of its own: a grid column cannot
        shrink below what its widget asks for, a label asks for however wide
        its text is, and the row then runs off the side of the panel and is
        cut off by the frame. Which reads as a rendering fault rather than as
        a sentence that did not fit.
        """
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
            total = self.winfo_width() / scale
        except Exception:
            return
        if total < 120:
            return                              # not laid out yet
        room = max(DETAIL_MIN_PX, int((total - 40) * DETAIL_SHARE))
        if room == self._detail_px:
            return
        self._detail_px = room
        for cells in self._cells.values():
            cells[4].configure(width=room)
            _elide(cells[4], cells[4]._full, room * scale)

    def update_from(self, s, now: float) -> None:
        from .navstatus import matrix_rows
        for key, row in matrix_rows(s, now).items():
            cells = self._cells.get(key)
            if cells is None:
                continue
            for lab, (text, tone) in zip(cells[:4], row.marks, strict=False):
                _set_label(lab, text=text, text_color=tone)
            _elide(cells[4], row.detail,
                   self._detail_px * (ctk.ScalingTracker
                                      .get_widget_scaling(self) or 1.0))


#: Measuring fonts, keyed by spec. Building a `tkinter.font.Font` is not free
#: and the matrix re-fits on every resize event.
_FONT_CACHE: dict = {}


def _measurer(label):
    """A font object that measures what this label actually draws with.

    Not `label.cget("font")`: on a CTkLabel that returns the *unscaled* spec
    tuple, which has no `measure` and, once wrapped in a Font, is a sixth too
    narrow at 150 % scaling. The inner Tk label carries the scaled font, which
    is the one on the screen.
    """
    widget = getattr(label, "_label", None) or label
    spec = widget.cget("font")
    key = tuple(spec) if isinstance(spec, (list, tuple)) else str(spec)
    hit = _FONT_CACHE.get(key)
    if hit is None:
        from tkinter import font as tkfont
        hit = _FONT_CACHE[key] = tkfont.Font(font=spec)
    return hit


def _elide(label, text: str, room_px: float) -> None:
    """Set a label's text, cut to an ellipsis when it will not fit.

    A sentence that stops mid-word looks like a rendering fault, and on a
    panel whose whole job is to be believed that is expensive. An ellipsis
    says "there is more"; the Health drawer has the rest.

    `room_px` is in device pixels, because that is what a font measures in.
    """
    label._full = text
    room = room_px - 8
    if room <= 1:
        _set_label(label, text=text)     # not laid out yet; _refit will return
        return
    try:
        measure = _measurer(label).measure
    except Exception:
        # Never let a measuring problem blank a diagnostic row -- but say so,
        # because the first version of this swallowed an AttributeError and
        # quietly stopped eliding anything at all.
        log.warning("could not measure %r for the matrix", text[:40],
                    exc_info=True)
        _set_label(label, text=text)
        return
    if measure(text) <= room:
        _set_label(label, text=text)
        return
    cut = text
    while cut and measure(cut + "…") > room:
        cut = cut[:-1]
    _set_label(label, text=(cut.rstrip(" ,;·-") + "…") if cut else "…")


def _vessel_feeding(s) -> bool | None:
    v = s.vessel.get("position")
    if v is None or v.quality is Quality.NEVER_RECEIVED:
        return None
    inject = s.vessel.get("inject")
    if inject is None:
        return None
    return bool(v.quality is Quality.OK and inject.number())
