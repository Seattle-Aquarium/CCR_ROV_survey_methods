"""
Navigation: the primary instruments display.

This is the page an operator sits in front of for a whole dive. Cockpit is on
the monitor above it and has the forward camera; this has everything else, and
on a survey where the vehicle is flown 0.8 m off the bottom in low visibility,
the numbers here matter more to the pilot than the picture does.

**The layout is fixed and deliberate**, because an instrument panel whose
instruments move is not an instrument panel:

    +---------------------------+------------------+------------------+
    |                           |  Flight Ops HUD  |    Power HUD     |
    |          MAP              |  gauge | 3 rows  |  gauge | 3 rows   |
    |                           +------------------+------------------+
    |                           |                                     |
    +---------------------------+     Navigation workspace            |
    |  profile · origin · bearing|                                    |
    +---------------------------+-------------------------------------+

The map takes the left 47%; the two HUDs sit side by side across the upper
right and keep their tops and bottoms aligned; the navigation workspace fills
the lower right. The strip under the map carries the three things an operator
must be able to see without looking for them -- which profile is active, is
the origin confirmed, and which way is home.

**Nothing critical is behind a scroll or a tab.** Parameter tables, message
health and history open in a drawer over the page; the instruments stay
visible behind them. That is the one rule that survives every layout argument:
if a number is needed to fly the vehicle it is on this screen, always.

**The page never polls.** It reads the collector's newest snapshot on its own
timer and draws it. A collector that stalls leaves a page that says so;
neither can block the other.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .. import diagnostics
from ..nav import geo, tiles, waypoints
from ..nav import model as M
from ..nav import origin as O
from ..nav import power as P
from ..nav import profiles as PR
from ..nav import replay as RP
from ..nav import session as SESS
from ..nav.collector import NavCollector
from ..nav.model import NO_VALUE, Quality, Reading
from . import theme as T
from .navgauges import AltitudeGauge, PowerGauge
from .navmap import MapCanvas, Marker, format_position
from .widgets import Card, button, label

log = logging.getLogger(__name__)

#: How often the page reads the collector's snapshot and redraws. Four times
#: a second matches the collector's own fast cadence; going faster would
#: redraw the same numbers.
REFRESH_MS = 250

#: Below this *logical* width the two HUDs cannot both hold a legible number,
#: so they stack instead.
#:
#: Logical, not device: CustomTkinter scales fonts by the display factor but
#: `winfo_width` reports device pixels, so on a 150% display a 1,100 px window
#: measures 1,650 and a device-pixel threshold never fires -- which is exactly
#: what happened here, and the stacked fallback silently never appeared. The
#: same correction `Card._fit_subtitle` makes for wraplength.
NARROW_PX = 1180

#: The navigation workspace never gets less than this many logical pixels,
#: whatever else has to give.
#:
#: Without a floor the workspace is the row that loses: at 1366x768 on a 150%
#: display it collapsed to 70 px -- its title and nothing else -- while the
#: two HUDs kept 200 px each. That inverts the priority. A mismatch an
#: operator cannot see is a mismatch that does not exist, so the matrix keeps
#: its space and the gauges, which stay legible smaller, give theirs up.
WORKSPACE_MIN_PX = 230

#: The map column, as a fraction of the page.
MAP_WEIGHT, HUD_WEIGHT = 47, 53


class NavigationPage(ctk.CTkFrame):
    """The whole chapter. Owns the collector, the log and the map."""

    def __init__(self, master, app, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.app = app
        self.collector = None
        self.session: SESS.NavSession | None = None
        self.waypoints: waypoints.WaypointStore | None = None
        self.tile_cache = tiles.TileCache()

        self.origin_state = O.OriginState()
        self.profile_key = "dvl"
        self.check: PR.CheckResult | None = None
        #: The operator's explicit unlock. False every launch: a program that
        #: remembers permission to write to a vehicle is a program that writes
        #: to a vehicle somebody did not expect.
        self.writes_unlocked = False
        self._narrow = False
        self._last_draw = 0.0
        self._drawer = None

        self.grid_columnconfigure(0, weight=MAP_WEIGHT, uniform="nav")
        self.grid_columnconfigure(1, weight=HUD_WEIGHT, uniform="nav")
        self.grid_rowconfigure(0, weight=1)

        self._build_map_column()
        self._build_right_column()
        self.bind("<Configure>", self._on_resize, add="+")
        self.after(REFRESH_MS, self._tick)

    # ------------------------------------------------------------------
    #  layout
    # ------------------------------------------------------------------

    def _build_map_column(self) -> None:
        col = ctk.CTkFrame(self, fg_color="transparent")
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        col.grid_columnconfigure(0, weight=1)
        # The map takes what is left after the strip; the strip is sized by
        # its contents rather than by a fraction, so a long warning grows it
        # instead of being clipped.
        col.grid_rowconfigure(1, weight=1)
        self.map_col = col

        bar = ctk.CTkFrame(col, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.base_menu = ctk.CTkOptionMenu(
            bar, width=170, font=T.FONT_SMALL,
            values=[tiles.SOURCES[k].label for k in tiles.BASE_KEYS],
            command=self._pick_base)
        self.base_menu.set(tiles.SOURCES[tiles.DEFAULT_BASE].label)
        self.base_menu.grid(row=0, column=0, padx=(0, 6))
        self.follow_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(bar, text="Follow", variable=self.follow_var,
                        font=T.FONT_SMALL, width=70,
                        command=self._toggle_follow).grid(row=0, column=1)
        self.depth_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="Seabed depth", variable=self.depth_var,
                        font=T.FONT_SMALL, width=110,
                        command=self._toggle_depth).grid(row=0, column=2,
                                                         padx=(6, 0))
        button(bar, "Fit", self._fit, "ghost", width=54).grid(row=0, column=3,
                                                              padx=(6, 0))
        bar.grid_columnconfigure(4, weight=1)
        self.wp_button = button(bar, "＋ Create waypoint", self._create_waypoint,
                                "primary", width=160)
        self.wp_button.grid(row=0, column=5)

        self.map = MapCanvas(col, cache=self.tile_cache)
        self.map.grid(row=1, column=0, sticky="nsew")

        self.position_label = label(col, f"{NO_VALUE}  no position", muted=True)
        self.position_label.configure(font=T.FONT_MONO, anchor="w")
        self.position_label.grid(row=2, column=0, sticky="ew", pady=(3, 3))

        self._build_strip(col, row=3)

    def _build_strip(self, parent, row: int) -> None:
        """The three things that must never need looking for."""
        strip = ctk.CTkFrame(parent, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                             border_width=1, border_color=T.BORDER)
        strip.grid(row=row, column=0, sticky="ew")
        strip.grid_columnconfigure((0, 1, 2), weight=1, uniform="strip")
        self.strip = strip

        self.strip_profile = _StripCell(strip, "PROFILE")
        self.strip_profile.grid(row=0, column=0, sticky="nsew", padx=8, pady=7)
        self.strip_origin = _StripCell(strip, "ORIGIN")
        self.strip_origin.grid(row=0, column=1, sticky="nsew", padx=8, pady=7)
        self.strip_home = _StripCell(strip, "TO VESSEL")
        self.strip_home.grid(row=0, column=2, sticky="nsew", padx=8, pady=7)

    def _build_right_column(self) -> None:
        col = ctk.CTkFrame(self, fg_color="transparent")
        col.grid(row=0, column=1, sticky="nsew")
        col.grid_columnconfigure(0, weight=1)
        # Roughly half each, tilted towards the workspace: the two HUDs
        # stay perfectly legible a little shorter, and the nine matrix rows
        # plus the profile controls must all be on screen at once. The
        # requirement that failures are visible without scrolling is what
        # decides this, not the tidiness of a 50/50 split.
        col.grid_rowconfigure(0, weight=45)
        col.grid_rowconfigure(1, weight=55, minsize=WORKSPACE_MIN_PX)
        self.right_col = col

        huds = ctk.CTkFrame(col, fg_color="transparent")
        huds.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        huds.grid_columnconfigure((0, 1), weight=1, uniform="hud")
        huds.grid_rowconfigure(0, weight=1)
        self.huds = huds

        self.flight_hud = _Hud(huds, "Flight Ops", AltitudeGauge)
        self.flight_hud.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        self.power_hud = _Hud(huds, "Power", PowerGauge)
        self.power_hud.grid(row=0, column=1, sticky="nsew", padx=(3, 0))

        self._build_workspace(col, row=1)

    def _build_workspace(self, parent, row: int) -> None:
        card = Card(parent, "Navigation",
                    "Sensors, estimator and profile. Configured, available "
                    "and used are three different questions.")
        card.grid(row=row, column=0, sticky="nsew")
        card.grid_rowconfigure(1, weight=1)
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        self.workspace = card

        head = ctk.CTkFrame(body, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.grid_columnconfigure(3, weight=1)
        label(head, "Profile", muted=True).grid(row=0, column=0, padx=(0, 6))
        self.profile_menu = ctk.CTkOptionMenu(
            head, width=190, font=T.FONT_SMALL,
            values=[PR.PROFILES[k].label for k in PR.PROFILE_ORDER],
            command=self._pick_profile)
        self.profile_menu.set(PR.PROFILES[self.profile_key].label)
        self.profile_menu.grid(row=0, column=1)
        self.check_label = label(head, "not checked", muted=True)
        self.check_label.grid(row=0, column=2, padx=(10, 0))
        button(head, "Details…", self._open_details, "ghost", width=90
               ).grid(row=0, column=4, padx=(6, 0))
        button(head, "Origin…", self._open_origin, "ghost", width=90
               ).grid(row=0, column=5, padx=(6, 0))
        self.apply_button = button(head, "Review & apply…", self._open_apply,
                                   "primary", width=140)
        self.apply_button.grid(row=0, column=6, padx=(6, 0))

        self.matrix = _SensorMatrix(body)
        self.matrix.grid(row=1, column=0, sticky="nsew")

        foot = ctk.CTkFrame(body, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        foot.grid_columnconfigure(0, weight=1)
        self.foot_label = label(foot, "Not connected.", muted=True)
        self.foot_label.configure(anchor="w")
        self.foot_label.grid(row=0, column=0, sticky="ew")
        self.unlock_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(foot, text="Allow writes to this vehicle",
                        variable=self.unlock_var, font=T.FONT_SMALL,
                        width=200, command=self._toggle_unlock
                        ).grid(row=0, column=1, padx=(8, 0))

    def _on_resize(self, event=None) -> None:
        """Fall back to stacked HUDs when they cannot both be legible.

        The threshold is a size, not a guess about a laptop model: whatever
        the display, two HUDs each needing a five-character number stop
        fitting side by side at about 1,180 logical pixels of page width.

        Stacking is not the only fallback -- a page that is wide but short
        cannot fit three full-size rows either, so the HUDs are also told the
        height they have and shrink their numbers to suit. Between them the
        page stays readable down to the minimum viewport in the operator
        documentation rather than quietly clipping its own values.
        """
        try:
            w, h = self.winfo_width(), self.winfo_height()
            scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:
            return
        w, h = w / scaling, h / scaling
        # Width only. Stacking because the page is *short* would be backwards:
        # it halves the height each HUD gets, which is the thing that was
        # already scarce. A short page is handled by the HUDs re-flowing their
        # own rows and by the workspace's floor, not by stacking.
        narrow = w < NARROW_PX
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            self.huds.grid_columnconfigure(1, weight=0, uniform="")
            self.huds.grid_rowconfigure((0, 1), weight=1, uniform="hudrow")
            self.power_hud.grid(row=1, column=0, sticky="nsew", padx=0,
                                pady=(6, 0))
        else:
            self.huds.grid_columnconfigure((0, 1), weight=1, uniform="hud")
            self.huds.grid_rowconfigure(1, weight=0)
            self.power_hud.grid(row=0, column=1, sticky="nsew", padx=(3, 0),
                                pady=0)

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Called when the tab is shown and when the flight folder changes.

        Starting the collector is idempotent, and switching tabs must never
        start a second one -- that is how a page ends up with two pollers and
        a vehicle being asked for everything twice.
        """
        folder = getattr(self.app, "flight_dir", None)
        if folder is not None and (self.waypoints is None
                                   or self.waypoints.folder != Path(folder)):
            self.waypoints = waypoints.WaypointStore(Path(folder))
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
            "gauge_max_w": P.GAUGE_MAX_W, "high_load_w": P.HIGH_LOAD_W,
            "survey_enter_m": 1.5, "survey_exit_m": 1.7,
            "survey_reference_m": 0.8, "energy_max_gap_s": P.MAX_GAP_S,
        }
        self.session.open()

    def _collector_event(self, kind: str, data: dict) -> None:
        """From the collector's thread. Writes to the log, touches no widget."""
        if self.session is not None:
            self.session.event(kind, data)

    def shutdown(self, *, wait: float = 0.0) -> None:
        """Stop everything this page owns, without holding the window.

        Nothing here joins by default. The collector can be inside a request
        to a vehicle that has stopped answering and the tile fetcher inside a
        tile download; joining either on the window's thread would freeze the
        close for as long as those timeouts have left to run. Both are daemon
        threads with bounded requests, both are signalled here, and neither
        can touch a widget on its way out.

        The session log *is* closed synchronously: it is a local file write,
        it is the record of the flight, and losing its last events to save a
        millisecond would be the wrong trade.
        """
        if self.collector is not None:
            self.collector.stop(wait)
        self.tile_cache.stop(wait)
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
                self.foot_label.configure(text=f"Redraw failed: {ex}",
                                          text_color=T.ERROR)
            except Exception:
                pass
        self.after(REFRESH_MS, self._tick)

    def _render(self) -> None:
        if self.collector is None:
            return
        s = self.collector.snapshot()
        now = time.monotonic()

        # -- Flight Ops HUD -------------------------------------------------
        alt = M.age_out(s.altitude, 3.0, now_mono=now)
        target = M.age_out(s.surftrak_target, 8.0, now_mono=now)
        self.flight_hud.gauge.update_reading(alt, target, now=now)

        mode = M.age_out(s.mode, 8.0, now_mono=now)
        mode_text = mode.text(unit=False)
        extra = ""
        if isinstance(mode.value, str) and "surftrak" in mode.value.lower():
            tv = target.number()
            extra = (f"target {tv:.2f} m" if tv is not None
                     else "target unavailable")
        self.flight_hud.set_row(0, "MODE", mode_text, extra,
                                _tone(mode))
        spd = M.age_out(s.speed, 4.0, now_mono=now)
        self.flight_hud.set_row(1, "VELOCITY", spd.text(2, unit=False), "m/s",
                                _tone(spd), hint=spd.note or spd.source.label())
        dep = M.age_out(s.depth, 4.0, now_mono=now)
        dv = dep.held()
        self.flight_hud.set_row(
            2, "DEPTH", (f"{dv:.1f}" if dv is not None else NO_VALUE), "m",
            _tone(dep), hint="negative is below the surface")

        # -- Power HUD -------------------------------------------------------
        watts = M.age_out(s.watts, 5.0, now_mono=now)
        self.power_hud.gauge.update_reading(watts, s.peak_w)
        volt = M.age_out(s.voltage, 5.0, now_mono=now)
        curr = M.age_out(s.current, 5.0, now_mono=now)
        self.power_hud.set_row(0, "VOLTAGE", volt.text(2, unit=False), "V",
                               _tone(volt))
        self.power_hud.set_row(1, "CURRENT", curr.text(1, unit=False), "A",
                               _tone(curr))
        wh = s.energy_wh
        self.power_hud.set_row(2, "ENERGY", wh.text(2, unit=False), "Wh",
                               _tone(wh), hint=wh.note)

        # -- map --------------------------------------------------------------
        self._render_map(s, now)

        # -- strip ------------------------------------------------------------
        self._render_strip(s, now)

        # -- workspace ---------------------------------------------------------
        self.matrix.update_from(s, now)
        self._render_foot(s)

    def _render_map(self, s, now: float) -> None:
        m = self.map
        m.rov_fix = s.rov_fix
        m.vessel_fix = s.vessel_fix if self.profile_key == "acoustic" else None
        vh = s.vessel_heading
        m.vessel_heading = vh.number() if vh is not None else None
        hdg = s.heading
        m.rov_heading = hdg.number()
        if self.collector is not None:
            m.rov_track = list(self.collector.rov_track)
            m.vessel_track = (list(self.collector.vessel_track)
                              if self.profile_key == "acoustic" else [])

        # No geographic reference: the local-metre view, saying so.
        has_geo = s.rov_fix is not None
        m.local_only = not has_geo
        if not has_geo:
            ned = s.local_ned
            if ned.quality.has_value and isinstance(ned.value, tuple):
                m.local_track.append((ned.value[0], ned.value[1]))
                if len(m.local_track) > 8000:
                    del m.local_track[:2000]
            m.status_note = (
                "The EKF has no origin, so there is no latitude or longitude "
                "to plot. The track's shape is real; its place on the Earth "
                "is not known." if self.origin_state.active is False else
                "Waiting for a position from the vehicle.")
        elif self.depth_var.get():
            # Seabed depth under the vehicle: depth below surface minus
            # altitude above the bottom, both measured, both already here.
            d, a = s.depth.number(), s.altitude.number()
            if d is not None and a is not None:
                seabed = abs(d) + a if d < 0 else d + a
                m.depth_points.append((s.rov_fix.lat, s.rov_fix.lon, seabed))
                if len(m.depth_points) > 20_000:
                    del m.depth_points[:5_000]
        m.draw()
        self.position_label.configure(
            text=format_position(s.rov_fix, now=now),
            text_color=(T.TEXT if s.rov_fix and s.rov_fix.quality is Quality.OK
                        else T.WARN))

    def _render_strip(self, s, now: float) -> None:
        prof = PR.PROFILES[self.profile_key]
        detected, why = PR.detect(s.params or None)
        if detected is None:
            self.strip_profile.set(prof.label, why, T.WARN)
        elif detected == self.profile_key:
            summary = self.check.summary() if self.check else "not checked"
            tone = (T.OK if self.check and self.check.matches else T.WARN)
            self.strip_profile.set(prof.label, summary, tone)
        else:
            self.strip_profile.set(
                prof.label,
                f"vehicle is configured as {PR.PROFILES[detected].label}",
                T.ERROR)

        st = self.origin_state
        self.strip_origin.set(
            st.summary(),
            (st.authority_reason or "").split(".")[0][:70],
            T.OK if st.active is True else
            T.ERROR if st.active is False else T.WARN)

        self._render_home(s, now)

    def _render_home(self, s, now: float) -> None:
        """Return bearing: to the vessel, or to the start, and never confused.

        In the acoustic profile this is a straight-line geodesic bearing to
        where the vessel is now. In DVL-only there is no vessel here at all,
        and the readout is relabelled "TO START" against the fixed origin --
        the same arithmetic, a completely different claim, so it never keeps
        the vessel's label.

        It is a direction to a point, not a course to steer: it knows nothing
        about the tether, obstacles or current.
        """
        rov = s.rov_fix
        if rov is None or rov.quality is not Quality.OK:
            self.strip_home.title("TO VESSEL" if self.profile_key == "acoustic"
                                  else "TO START")
            self.strip_home.set(NO_VALUE, "no usable ROV position", T.WARN)
            return

        if self.profile_key == "acoustic":
            self.strip_home.title("TO VESSEL")
            target = s.vessel_fix
            if target is None:
                self.strip_home.set(NO_VALUE, "no vessel position", T.WARN)
                return
            if target.quality is not Quality.OK:
                age = target.age(now)
                self.strip_home.set(
                    NO_VALUE,
                    f"vessel position stale{f' · {age:.0f} s' if age else ''}"
                    f" — no bearing from it", T.WARN)
                return
            tone = T.OK
            name = "vessel"
        else:
            self.strip_home.title("TO START")
            o = self.origin_state
            if o.active is not True or o.active_lat is None:
                self.strip_home.set(NO_VALUE, "no confirmed origin", T.WARN)
                return
            from ..nav.model import Fix
            target = Fix(lat=o.active_lat, lon=o.active_lon, kind="manual")
            tone = T.TEXT_MUTED
            name = "fixed origin · subject to dead-reckoning drift"

        dist, brg, _ = geo.inverse(rov.lat, rov.lon, target.lat, target.lon)
        if dist < geo.MIN_BEARING_M:
            self.strip_home.set("at/near target", f"{dist:.1f} m — too close "
                                                  f"for a stable bearing", tone)
            return
        self.strip_home.set(f"{geo.format_bearing(brg)} · {dist:,.0f} m", name,
                            tone)

    def _render_foot(self, s) -> None:
        bits = [s.link.line()]
        if self.session is not None and not self.session.healthy:
            bits.append(self.session.status_line())
        if self.tile_cache.failures:
            bits.append(f"map: {self.tile_cache.last_error}")
        if s.link.mode == "replay":
            bits.insert(0, "REPLAY — not a live vehicle")
        colour = (T.WARN if (s.link.problem or not s.link.connected
                             or s.link.mode == "replay") else T.TEXT_MUTED)
        self.foot_label.configure(text="  ·  ".join(bits), text_color=colour)
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
        if self.map.follow:
            self.map.centre_on_vehicle()

    def _toggle_depth(self) -> None:
        self.map.colour_by_depth = self.depth_var.get()
        self.map.draw()

    def _fit(self) -> None:
        self.map.fit_track()
        self.follow_var.set(False)

    def _pick_profile(self, label_text: str) -> None:
        for k, p in PR.PROFILES.items():
            if p.label == label_text:
                if k != self.profile_key:
                    self.profile_key = k
                    if self.session is not None:
                        self.session.event("profile_selected", {"profile": k})
                    self._revalidate()
                return

    def _toggle_unlock(self) -> None:
        """Arm or disarm the one path that can change the vehicle.

        Two deliberate acts are needed before a parameter moves: this, and the
        confirmation in the review dialog. It is never remembered between
        runs, and it is refused outright while the vehicle is armed.
        """
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
            return False           # replay: there is no connection at all
        s = self.collector.snapshot()
        return not s.armed.number()

    def _create_waypoint(self) -> None:
        """Capture now; name afterwards.

        The position is taken on this line, before any dialog exists. By the
        time an operator has finished typing a name the vehicle has moved, and
        a point recorded at the end of the typing is confidently wrong.
        """
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
        # Cancelling keeps the point: it is already on the disk, and the name
        # was never the reason it was captured.
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
            marks.append(Marker(o.active_lat, o.active_lon, "origin", "origin"))
        self.map.markers = marks
        self.map.draw()

    # ------------------------------------------------------------------
    #  validation, origin and apply
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
            self.check_label.configure(
                text=self.check.summary(),
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

    def _open_origin(self) -> None:
        from .navdialogs import OriginDialog
        OriginDialog(self)

    def _open_apply(self) -> None:
        self._revalidate()
        from .navdialogs import ApplyDialog
        ApplyDialog(self, self.check)

    # -- replay -------------------------------------------------------------

    def start_replay(self, frames, label_text: str, *, synthetic: bool = False
                     ) -> None:
        """Swap the live collector for a replay one. No vehicle is reachable."""
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
        self.map.depth_points = []
        log.info("navigation replay started: %s (%d frames)", label_text,
                 len(frames))


# --------------------------------------------------------------------------
#  pieces
# --------------------------------------------------------------------------


class _Hud(ctk.CTkFrame):
    """A gauge on the left and exactly three primary rows beside it.

    Three rows, always -- the count is part of the specification and does not
    change with the window. What changes is how they are arranged, because a
    HUD that is wide and short has room across where it has none down:

    * **tall enough** (the normal case): the three rows are stacked in one
      column beside the gauge, which is how an instrument panel should read.
    * **short**: the three go side by side across the bottom, and the gauge
      keeps the height. This is the compact fallback, and it is reached on a
      1366x768 display at 150% scaling -- 911x512 logical, which is a real
      configuration and not a hypothetical one.

    Either way the number sizes itself to the space it actually has. The HUD
    measures itself rather than being told, because what it is given depends
    on whether the two HUDs are side by side or stacked, and a guess made by
    the page was wrong in exactly the case that mattered.
    """

    #: Below this logical height the rows go across instead of down.
    #: Measured: three stacked rows need about 240 logical pixels before the
    #: type has to shrink past comfortable reading distance.
    SHORT_H = 245
    #: Logical height one stacked row needs for a full-size number.
    ROW_H = 72

    def __init__(self, master, title: str, gauge_cls, **kw):
        super().__init__(master, fg_color=T.SURFACE, corner_radius=T.RADIUS,
                         border_width=1, border_color=T.BORDER, **kw)
        self.grid_rowconfigure(1, weight=1)
        self.title_label = ctk.CTkLabel(self, text=title, font=T.FONT_H2,
                                        text_color=T.HEADING, anchor="w")
        self.title_label.grid(row=0, column=0, columnspan=2, sticky="ew",
                              padx=10, pady=(6, 2))
        self.gauge = gauge_cls(self)
        self.rows_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._rows = [_HudRow(self.rows_frame) for _ in range(3)]
        self._layout = ""
        self._scale = 0.0
        self._apply_layout("column")
        self.bind("<Configure>", self._measure, add="+")

    # -- arrangement -------------------------------------------------------

    def _measure(self, _event=None) -> None:
        try:
            w, h = self.winfo_width(), self.winfo_height()
            scaling = ctk.ScalingTracker.get_widget_scaling(self) or 1.0
        except Exception:
            return
        w, h = w / scaling, h / scaling
        if w < 40 or h < 40:
            return
        want = "row" if h < self.SHORT_H else "column"
        if want != self._layout:
            self._apply_layout(want)
        # How much height one row actually has, and what type fits in it.
        per_row = (h - 30) if want == "row" else (h - 30) / 3.0
        factor = max(0.55, min(1.0, per_row / self.ROW_H))
        if abs(factor - self._scale) >= 0.05:
            self._scale = factor
            for r in self._rows:
                r.set_scale(factor)

    def _apply_layout(self, how: str) -> None:
        self._layout = how
        for r in self._rows:
            r.grid_forget()
        self.rows_frame.grid_forget()
        self.gauge.grid_forget()
        for i in range(3):
            self.rows_frame.grid_columnconfigure(i, weight=0, uniform="")
            self.rows_frame.grid_rowconfigure(i, weight=0, uniform="")
        if how == "row":
            # Gauge above, three readings across the foot.
            self.grid_columnconfigure(0, weight=1, uniform="")
            self.grid_columnconfigure(1, weight=0, uniform="")
            self.grid_rowconfigure(1, weight=1)
            self.grid_rowconfigure(2, weight=0)
            self.gauge.grid(row=1, column=0, columnspan=2, sticky="nsew",
                            padx=8, pady=(0, 2))
            self.rows_frame.grid(row=2, column=0, columnspan=2, sticky="ew",
                                 padx=8, pady=(0, 6))
            for i, r in enumerate(self._rows):
                # The first row is the one carrying a word rather than a
                # number -- the flight mode, and "Position Hold" is three
                # times the width of "0.32". Equal columns cut it off.
                self.rows_frame.grid_columnconfigure(i, weight=4 if i == 0
                                                     else 3, uniform="hudcell")
                r.grid(row=0, column=i, sticky="nsew", padx=(0, 6))
        else:
            self.grid_columnconfigure(0, weight=38, uniform="hud")
            self.grid_columnconfigure(1, weight=62, uniform="hud")
            self.grid_rowconfigure(1, weight=1)
            self.grid_rowconfigure(2, weight=0)
            self.gauge.grid(row=1, column=0, sticky="nsew", padx=(8, 2),
                            pady=(0, 8))
            self.rows_frame.grid(row=1, column=1, sticky="nsew", padx=(2, 10),
                                 pady=(0, 8))
            self.rows_frame.grid_columnconfigure(0, weight=1)
            for i, r in enumerate(self._rows):
                self.rows_frame.grid_rowconfigure(i, weight=1, uniform="hudrow")
                r.grid(row=i, column=0, sticky="nsew", pady=1)

    # -- contents -----------------------------------------------------------

    def set_row(self, i: int, name: str, value: str, unit: str = "",
                tone=None, hint: str = "") -> None:
        self._rows[i].set(name, value, unit, tone, hint,
                          compact=self._layout == "row")

    def refresh_theme(self) -> None:
        self.configure(fg_color=T.SURFACE, border_color=T.BORDER)
        self.gauge.refresh_theme()


class _HudRow(ctk.CTkFrame):
    """One of the three: a small name, a large number, a unit and a hint."""

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_columnconfigure(0, weight=1)
        self.name = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                 text_color=T.TEXT_MUTED, anchor="w")
        self.name.grid(row=0, column=0, sticky="ew")
        line = ctk.CTkFrame(self, fg_color="transparent")
        line.grid(row=1, column=0, sticky="ew")
        self._base = 1.25
        #: The height-driven factor, and the size actually on screen once the
        #: value's own length has been taken into account.
        self._scale = 1.0
        self._shown = 1.25
        self.value = ctk.CTkLabel(line, text=NO_VALUE,
                                  font=T.scale_font(T.FONT_TITLE, self._base),
                                  text_color=T.TEXT, anchor="w")
        self.value.grid(row=0, column=0, sticky="w")
        self.unit = ctk.CTkLabel(line, text="", font=T.FONT_BODY,
                                 text_color=T.TEXT_MUTED, anchor="w")
        # Bottom-aligned rather than offset by a fixed number of pixels: the
        # value's type shrinks with the window and a fixed offset pushed the
        # unit off the bottom of a short row.
        self.unit.grid(row=0, column=1, sticky="sw", padx=(5, 0), pady=(0, 3))
        self.hint = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                 text_color=T.TEXT_MUTED, anchor="w")
        self.hint.grid(row=2, column=0, sticky="ew")

    def set_scale(self, factor: float) -> None:
        """The factor the HUD derived from the height it actually has."""
        self._scale = factor

    #: A value longer than this sets its own type down so it is not cut off.
    #: "Surftrak" fits; "Position Hold" and "Motor Detect" do not.
    LONG_VALUE = 8

    def set(self, name: str, value: str, unit: str = "", tone=None,
            hint: str = "", compact: bool = False) -> None:
        self.name.configure(text=name)
        text = value or NO_VALUE
        # Long values shrink rather than clip. A flight mode truncated to
        # "Depth I" is not a flight mode, and nothing on the page says it was
        # shortened -- which is the whole problem with clipping.
        long_factor = (min(1.0, self.LONG_VALUE / len(text))
                       if len(text) > self.LONG_VALUE else 1.0)
        want = self._base * self._scale * max(0.5, long_factor)
        if abs(want - self._shown) > 0.02:
            self._shown = want
            self.value.configure(font=T.scale_font(T.FONT_TITLE, want))
        self.value.configure(text=text, text_color=tone or T.TEXT)
        self.unit.configure(text=unit)
        # When the space runs out the hint is what goes. The value never is:
        # a number clipped to "0.3" is worse than no hint, and it is silent.
        self.hint.configure(text="" if compact else (hint or "")[:52])


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
        self._title.configure(text=text)

    def set(self, value: str, note: str = "", tone=None) -> None:
        self._value.configure(text=value, text_color=tone or T.TEXT)
        self._note.configure(text=(note or "")[:78])


class _SensorMatrix(ctk.CTkScrollableFrame):
    """Configured / available / used, per source.

    Three columns because they are three different questions and a single
    green tick cannot answer them. A source can be configured perfectly,
    delivering data beautifully, and simply not be used by the estimator --
    which is the most common and most confusing state there is, and the one
    this page exists to make visible.
    """

    ROWS = (
        ("acoustic", "Acoustic position"),
        ("vessel_gga", "Vessel GNSS position"),
        ("vessel_hdt", "Vessel heading (HDT)"),
        ("dvl", "DVL"),
        ("compass", "ROV compass"),
        ("imu", "IMU / attitude"),
        ("depth", "Depth"),
        ("range", "Downward range"),
        ("ekf", "EKF"),
    )

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_columnconfigure(0, weight=6, uniform="mx")
        for i in range(1, 4):
            self.grid_columnconfigure(i, weight=3, uniform="mx")
        self.grid_columnconfigure(4, weight=14, uniform="mx")
        heads = ("Source", "Configured", "Available", "Used", "Detail")
        for i, text in enumerate(heads):
            ctk.CTkLabel(self, text=text, font=T.FONT_SMALL,
                         text_color=T.TEXT_MUTED,
                         anchor="w").grid(row=0, column=i, sticky="ew",
                                          padx=4, pady=(0, 3))
        self._cells: dict[str, tuple] = {}
        for r, (key, name) in enumerate(self.ROWS, start=1):
            n = ctk.CTkLabel(self, text=name, font=T.FONT_BODY, anchor="w")
            n.grid(row=r, column=0, sticky="ew", padx=4, pady=1)
            marks = []
            for col in range(1, 4):
                lab = ctk.CTkLabel(self, text="–", font=T.FONT_BODY,
                                   text_color=T.TEXT_MUTED, anchor="w")
                lab.grid(row=r, column=col, sticky="ew", padx=4)
                marks.append(lab)
            detail = ctk.CTkLabel(self, text="", font=T.FONT_SMALL,
                                  text_color=T.TEXT_MUTED, anchor="w")
            detail.grid(row=r, column=4, sticky="ew", padx=4)
            self._cells[key] = (marks[0], marks[1], marks[2], detail)

    def update_from(self, s, now: float) -> None:
        from .navstatus import matrix_rows
        for key, row in matrix_rows(s, now).items():
            cells = self._cells.get(key)
            if cells is None:
                continue
            for lab, (text, tone) in zip(cells[:3], row.marks, strict=False):
                lab.configure(text=text, text_color=tone)
            cells[3].configure(text=row.detail)


def _tone(r: Reading):
    if r.quality is Quality.OK:
        return T.TEXT
    if r.quality is Quality.STALE:
        return T.WARN
    if r.quality is Quality.UNSUPPORTED:
        return T.TEXT_MUTED
    return T.ERROR


def _vessel_feeding(s) -> bool | None:
    v = s.vessel.get("position")
    if v is None or v.quality is Quality.NEVER_RECEIVED:
        return None
    inject = s.vessel.get("inject")
    if inject is None:
        return None
    return bool(v.quality is Quality.OK and inject.number())
