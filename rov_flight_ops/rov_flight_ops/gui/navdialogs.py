"""
The three dialogs: details, origin, and the one that can change the vehicle.

They are dialogs rather than panels because the instruments must stay visible
behind them -- an operator opening a parameter table has not stopped flying.
Each is modeless and each closes without side effects.

**The apply dialog is the only path in this program that writes a parameter to
an ArduSub vehicle**, and it is deliberately tedious: nothing happens until
the operator has unlocked writes on the page, read a list of exactly what will
change, and confirmed. Every step is read back, every outcome is logged, and a
batch that half-succeeds says so rather than reporting either success or
failure.
"""

from __future__ import annotations

import logging
import threading
import time
from tkinter import messagebox

import customtkinter as ctk

from ..nav import origin as O
from ..nav import profiles as PR
from . import theme as T
from .navstatus import message_health_rows
from .widgets import Card, button, entry, label, output_box, say

log = logging.getLogger(__name__)


class _Drawer(ctk.CTkToplevel):
    """A modeless window over the page, sized to leave the instruments shown."""

    def __init__(self, page, title: str, size: str = "980x640"):
        super().__init__(page)
        self.page = page
        self.title(title)
        self.geometry(size)
        self.configure(fg_color=T.BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        try:
            self.transient(page.winfo_toplevel())
        except Exception:
            pass


# --------------------------------------------------------------------------
#  Details
# --------------------------------------------------------------------------


class DetailsDrawer(_Drawer):
    """Parameters, message health and the profile findings, in full."""

    def __init__(self, page, snapshot, check, origin_state):
        super().__init__(page, "Navigation details")
        tabs = ctk.CTkTabview(self, fg_color=T.SURFACE)
        tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self._profile_tab(tabs.add("Profile check"), check)
        self._messages_tab(tabs.add("Message health"), snapshot)
        self._params_tab(tabs.add("Parameters"), snapshot)
        self._origin_tab(tabs.add("Origin"), origin_state)

    def _profile_tab(self, tab, check) -> None:
        box = output_box(tab, wrap="none", muted=False)
        box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        box.configure(height=520)
        if check is None:
            say(box, "The profile has not been checked yet.")
            return
        lines = [f"{check.profile.label}",
                 f"{check.profile.summary}",
                 "",
                 f"Definition {check.profile.version}, written against "
                 f"{check.profile.written_for}",
                 f"Result: {check.summary()}"]
        if check.params_age is not None:
            lines.append(f"Parameters read {check.params_age:.0f} s ago")
        lines += ["", "PARAMETERS", "-" * 70]
        for f in check.findings:
            mark = "  ok " if f.ok else f"{f.severity.value.upper():>5}"
            lines.append(f"[{mark}] {f.param:<18} has {f.current_text():<26} "
                         f"wants {f.want_text()}")
            if not f.ok:
                lines.append(f"          {f.requirement.why}")
        if check.notes:
            lines += ["", "INTEGRATION", "-" * 70]
            for sev, text in check.notes:
                lines.append(f"[{sev.value.upper():>5}] {text}")
        say(box, "\n".join(lines))

    def _messages_tab(self, tab, snapshot) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        box = output_box(tab, wrap="none", muted=False)
        box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        box.configure(height=520)
        if snapshot is None:
            say(box, "Nothing has been read yet.")
            return
        now = time.monotonic()
        rows = message_health_rows(snapshot, now)
        lines = [
            "Age is time since a NEW message arrived — mavlink2rest's own",
            "counter — not time since the last successful request. A message",
            "whose counter has not moved is stale however often it is fetched.",
            "",
            f"{'MESSAGE':<24}{'RATE':>10}{'AGE':>8}  NOTE",
            "-" * 78]
        for name, rate, age, note in rows:
            lines.append(f"{name:<24}{rate:>10}{age:>8}  {note}")
        say(box, "\n".join(lines))

    def _params_tab(self, tab, snapshot) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        box = output_box(tab, wrap="none", muted=False)
        box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        box.configure(height=520)
        params = (snapshot.params if snapshot else {}) or {}
        if not params:
            say(box, "No parameters have been read from the vehicle.\n\n"
                     "They are read from the head of the autopilot's own "
                     "dataflash log, which sends the vehicle nothing.")
            return
        keep = [k for k in sorted(params)
                if k.startswith(("EK3_", "EK2_", "AHRS_", "VISO_", "GPS_",
                                 "RNGFND1_", "ORIGIN_", "SCR_", "BATT_",
                                 "SURFTRAK"))]
        lines = [f"{len(params):,} parameters read; "
                 f"{len(keep)} relevant to navigation.", "-" * 60]
        for k in keep:
            v = params[k]
            extra = ""
            if k.startswith("EK3_SRC"):
                extra = f"   {PR.source_name(k, v)}"
            lines.append(f"{k:<22} {v:>14g}{extra}")
        say(box, "\n".join(lines))

    def _origin_tab(self, tab, st) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        box = output_box(tab, wrap="word", muted=False)
        box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        box.configure(height=520)
        lines = [st.summary(), ""]
        if st.authority_reason:
            lines += ["AUTHORITY", st.authority_reason, ""]
        if st.families:
            lines.append("PARAMETER FAMILIES ON THIS VEHICLE")
            for f in st.families:
                lines.append(f"  {f.prefix}*  {f.text()}")
                lines.append(f"      {f.note()}")
            lines.append("")
        if st.scripting_enabled is not None:
            lines.append(f"SCR_ENABLE is {int(st.scripting_enabled)} — Lua "
                         f"scripting is "
                         f"{'on' if st.scripting_enabled else 'OFF'}.")
            lines.append("")
        if st.warnings:
            lines.append("WARNINGS")
            for w in st.warnings:
                lines.append(f"  • {w}")
        say(box, "\n".join(lines))


# --------------------------------------------------------------------------
#  Origin
# --------------------------------------------------------------------------


class OriginDialog(_Drawer):
    """Set the EKF origin, and save it for the next boot — in that order.

    The order is the safety property, not a preference. While the EKF has no
    origin an `ahrs-set-origin` applet may be sitting in its five-second retry
    loop, and its guard passes as soon as *any one* of its three parameters is
    non-zero — so writing them one at a time can hand it a latitude with no
    longitude and lock in an origin in the Atlantic. Once the origin is set
    the applet returns at `if ahrs:get_origin()` before it reads them at all.

    So the dialog offers, in order:

    1. **Set on the vehicle now** — `SET_GPS_GLOBAL_ORIGIN`, read back and
       confirmed. Immediate, verifiable, and lost at the next reboot.
    2. **Save for the next boot** — the applet's parameters, which is refused
       until step 1 has succeeded.
    """

    def __init__(self, page):
        super().__init__(page, "EKF origin", "760x620")
        st = page.origin_state
        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)

        status = Card(body, "Now", st.summary())
        status.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.status_box = output_box(status.body, wrap="word")
        self.status_box.grid(row=0, column=0, sticky="ew")
        self.status_box.configure(height=90)
        say(self.status_box, "\n".join(
            [st.authority_reason or ""] + [f"⚠ {w}" for w in st.warnings]))
        button(status.body, "Read the origin from the vehicle",
               self._read_origin, "ghost", width=250
               ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        card = Card(body, "Set an origin",
                    "Decimal degrees. This is the point the dead-reckoned "
                    "track is referenced to — it is not a measurement of "
                    "where the ROV is.")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        grid = ctk.CTkFrame(card.body, fg_color="transparent")
        grid.grid(row=0, column=0, sticky="ew")
        label(grid, "Latitude").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.lat = entry(grid, "47.62691", width=170)
        self.lat.grid(row=0, column=1, padx=(0, 14))
        label(grid, "Longitude").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.lon = entry(grid, "-122.39018", width=170)
        self.lon.grid(row=0, column=3, padx=(0, 14))
        label(grid, "Alt (m MSL)").grid(row=0, column=4, sticky="w", padx=(0, 6))
        self.alt = entry(grid, "0", width=80)
        self.alt.grid(row=0, column=5)

        saved = st.configured_families
        if saved:
            f = saved[0]
            self.lat.insert(0, f"{f.lat:.6f}")
            self.lon.insert(0, f"{f.lon:.6f}")
            self.alt.insert(0, f"{f.alt or 0:g}")
            label(card.body,
                  f"Pre-filled from {f.prefix}* already on the vehicle. "
                  f"Check it is today's site before using it.",
                  muted=True).grid(row=1, column=0, sticky="w", pady=(6, 0))

        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.set_btn = button(row, "1.  Set on the vehicle now",
                              self._set_now, "primary", width=230)
        self.set_btn.grid(row=0, column=0)
        self.save_btn = button(row, "2.  Save for the next boot",
                               self._save_boot, "ghost", width=220)
        self.save_btn.grid(row=0, column=1, padx=(8, 0))

        label(card.body,
              "Set first, then save. While the EKF has no origin a running "
              "Lua applet can act on a half-written coordinate pair; once the "
              "origin is set it stops before reading the parameters at all.",
              muted=True).grid(row=3, column=0, sticky="w", pady=(8, 0))

        out = Card(body, "Result")
        out.grid(row=2, column=0, sticky="ew")
        self.result = output_box(out.body, wrap="word")
        self.result.grid(row=0, column=0, sticky="ew")
        self.result.configure(height=150)
        say(self.result, "Nothing has been sent.")
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        can = self.page._can_write()
        self.set_btn.configure(state="normal" if can else "disabled")
        self.save_btn.configure(
            state="normal" if (can and self.page.origin_state.active is True)
            else "disabled")
        if not can:
            say(self.result,
                "Writes are locked. Tick 'Allow writes to this vehicle' on "
                "the Navigation page, with the vehicle disarmed.")

    def _mav(self):
        c = self.page.collector
        return getattr(c, "mav", None) if c is not None else None

    def _read_origin(self) -> None:
        mav = self._mav()
        if mav is None:
            say(self.result, "There is no vehicle connection — this is a "
                             "replay.")
            return
        if not self.page._can_write():
            say(self.result,
                "Reading the origin sends the vehicle a MAV_CMD_REQUEST_MESSAGE, "
                "because ArduPilot does not stream GPS_GLOBAL_ORIGIN. Unlock "
                "writes to allow it.")
            return
        say(self.result, "Asking the vehicle for GPS_GLOBAL_ORIGIN…")
        self._run(lambda: O.read_active(mav, request=True), self._origin_read)

    def _origin_read(self, result) -> None:
        if isinstance(result, Exception):
            say(self.result, f"Could not read the origin: {result}")
            return
        is_set, detail = result
        st = self.page.origin_state
        st.active = is_set
        st.confirmed_mono = time.monotonic()
        if is_set:
            st.active_lat, st.active_lon = detail.get("lat"), detail.get("lon")
            st.active_alt_m = detail.get("alt_m")
            c = self.page.collector
            if c is not None and st.active_lat is not None:
                c.set_confirmed_origin(st.active_lat, st.active_lon)
            say(self.result,
                f"The EKF origin is {st.active_lat:.6f}, {st.active_lon:.6f}.")
            self.page._sync_markers()
        else:
            st.active_lat = st.active_lon = None
            c = self.page.collector
            if c is not None:
                c.clear_origin()
            say(self.result,
                "The vehicle reports no EKF origin. "
                + str(detail.get("note", "")))
        if self.page.session is not None:
            self.page.session.event("origin_read",
                                    {"is_set": is_set, **detail})
        self._refresh_buttons()

    def _set_now(self) -> None:
        mav = self._mav()
        if mav is None or not self.page._can_write():
            self._refresh_buttons()
            return
        try:
            lat, lon, alt = O.validate(self.lat.get(), self.lon.get(),
                                       self.alt.get())
        except O.OriginError as ex:
            messagebox.showwarning("EKF origin", str(ex))
            return
        if not messagebox.askyesno(
            "EKF origin",
            f"Send origin {lat:.6f}, {lon:.6f} at {alt:g} m to the vehicle?\n\n"
            f"This takes effect immediately and is not stored — a reboot "
            f"loses it.", icon="warning", default="no"):
            return
        say(self.result, "Sending…")
        self._run(lambda: O.set_origin_now(mav, lat, lon, alt),
                  lambda r: self._applied(r, lat, lon))

    def _applied(self, res, lat, lon) -> None:
        if isinstance(res, Exception):
            say(self.result, f"Failed: {res}")
            return
        say(self.result, f"{res.report()}\n\n{res.message}")
        if self.page.session is not None:
            self.page.session.event("origin_set", {
                "lat": lat, "lon": lon, "outcome": res.outcome,
                "steps": [s.what for s in res.steps]})
        if res.outcome == "succeeded":
            st = self.page.origin_state
            st.active, st.active_lat, st.active_lon = True, lat, lon
            st.confirmed_mono = time.monotonic()
            c = self.page.collector
            if c is not None:
                c.set_confirmed_origin(lat, lon)
            self.page._sync_markers()
        self._refresh_buttons()

    def _save_boot(self) -> None:
        mav = self._mav()
        st = self.page.origin_state
        if mav is None or not self.page._can_write():
            return
        try:
            lat, lon, alt = O.validate(self.lat.get(), self.lon.get(),
                                       self.alt.get())
        except O.OriginError as ex:
            messagebox.showwarning("EKF origin", str(ex))
            return
        family = st.authority
        if family == "gcs" or family not in O.FAMILIES:
            messagebox.showinfo(
                "EKF origin",
                "This vehicle has no origin parameter family, so there is "
                "nothing to save into. Install the fixed ahrs-set-origin "
                "applet from lua_scripts/ first.")
            return
        names = ", ".join(O.FAMILIES[family])
        if not messagebox.askyesno(
            "EKF origin",
            f"Write {names}?\n\nThey take effect at the next reboot, if an "
            f"applet is installed and running to read them.",
            icon="warning", default="no"):
            return
        say(self.result, f"Writing {names}…")
        setter = _param_setter(mav)
        self._run(
            lambda: O.save_for_next_boot(setter, family, lat, lon, alt,
                                         origin_is_set=st.active is True),
            lambda r: self._saved(r, family))

    def _saved(self, res, family) -> None:
        if isinstance(res, Exception):
            say(self.result, f"Failed: {res}")
            return
        say(self.result, f"{res.report()}\n\n{res.message}")
        if self.page.session is not None:
            self.page.session.event("origin_saved",
                                    {"family": family,
                                     "outcome": res.outcome})

    def _run(self, work, done) -> None:
        """Run a vehicle operation off the window's thread.

        Everything here talks to a vehicle that may not answer. Doing it in
        the button's callback is what froze this program before.
        """
        def thread() -> None:
            try:
                result = work()
            except Exception as ex:
                result = ex
            try:
                self.after(0, lambda: done(result))
            except Exception:
                pass
        threading.Thread(target=thread, daemon=True).start()


def _param_setter(mav):
    """`(name, value) -> (ok, detail)`: write one parameter and read it back.

    The read-back is the point. A `PARAM_SET` is fire-and-forget; the
    autopilot answers with a `PARAM_VALUE`, and only that says what was
    actually stored — which for a float32 parameter is not always the number
    that was sent.
    """
    def setter(name: str, value: float) -> tuple[bool, str]:
        tpl = mav.helper_template("PARAM_SET")
        if tpl is None:
            return False, "mavlink2rest would not supply a PARAM_SET template"
        msg = tpl.setdefault("message", {})
        # mavlink2rest wants the id as a fixed-width character array.
        msg["param_id"] = list(name.ljust(16, " ")[:16])
        msg["param_value"] = float(value)
        msg["param_type"] = {"type": "MAV_PARAM_TYPE_REAL32"}
        msg["target_system"] = mav.system
        msg["target_component"] = mav.component
        ans = mav.send(tpl, what=f"PARAM_SET {name}={value:g}")
        if not ans.ok:
            return False, ans.error
        for _ in range(12):
            time.sleep(0.25)
            got = mav.parameter(name)
            if got is None:
                continue
            if abs(got - O.float32(value)) < max(1e-6, abs(value) * 1e-6):
                return True, f"read back {got:g}"
            return False, f"read back {got:g}, not {value:g}"
        return False, "no PARAM_VALUE came back to confirm the write"
    return setter


# --------------------------------------------------------------------------
#  Apply
# --------------------------------------------------------------------------


class ApplyDialog(_Drawer):
    """Review exactly what will change, then apply it, then verify it.

    A bounded state machine, shown to the operator as it runs: idle →
    validating → applying → verifying → succeeded / failed / partially applied
    / unconfirmed. Partial application is a first-class outcome, because a
    batch of parameter writes is not a transaction and pretending otherwise is
    how a vehicle ends up in a configuration nobody chose.

    Nothing is rolled back automatically. Once the state is uncertain, an
    automatic rollback is another blind write; the operator gets fresh values
    and decides.
    """

    def __init__(self, page, check):
        super().__init__(page, "Review and apply", "820x600")
        self.check = check
        self.state = "idle"
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        head = Card(body, check.profile.label if check else "No profile",
                    check.profile.summary if check else "")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.state_label = label(head.body, "Ready.", muted=True)
        self.state_label.grid(row=0, column=0, sticky="w")

        plan = Card(body, "What will change")
        plan.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        plan.body.grid_rowconfigure(0, weight=1)
        self.plan_box = output_box(plan.body, wrap="none", muted=False)
        self.plan_box.grid(row=0, column=0, sticky="nsew")
        self.plan_box.configure(height=280)

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew")
        self.apply_btn = button(row, "Apply these changes", self._apply,
                                "primary", width=190)
        self.apply_btn.grid(row=0, column=0)
        button(row, "Close", self.destroy, "ghost", width=90
               ).grid(row=0, column=1, padx=(8, 0))
        self._describe()

    def _describe(self) -> None:
        if self.check is None:
            say(self.plan_box, "Nothing to check.")
            self.apply_btn.configure(state="disabled")
            return
        changes = self.check.changes()
        lines: list[str] = []
        opt = PR.source_set_available(self.check.profile,
                                      self.page.collector.snapshot().params
                                      if self.page.collector else None)
        lines.append("EKF SOURCE SET")
        lines.append(f"  {'available' if opt.available else 'not available'}: "
                     f"{opt.reason}")
        lines.append("")
        if not changes:
            lines.append("No parameter changes are needed for this profile.")
        else:
            lines.append("PARAMETER WRITES")
            for f in changes:
                lines.append(f"  {f.param:<18} {f.current_text():<26} → "
                             f"{f.want_text()}")
                lines.append(f"      {f.requirement.why}")
        if self.check.notes:
            lines += ["", "THESE ARE NOT PARAMETERS AND WILL NOT BE CHANGED "
                          "HERE"]
            for sev, text in self.check.notes:
                lines.append(f"  [{sev.value}] {text}")
        say(self.plan_box, "\n".join(lines))
        self.apply_btn.configure(
            state="normal" if (changes and self.page._can_write())
            else "disabled")

    def _apply(self) -> None:
        changes = self.check.changes() if self.check else []
        if not changes or not self.page._can_write():
            return
        listing = "\n".join(f"  {f.param} = {f.requirement.want:g}"
                            for f in changes)
        if not messagebox.askyesno(
            "Apply changes",
            f"Write {len(changes)} parameter(s) to the vehicle?\n\n{listing}\n\n"
            f"Each is read back. A batch is not a transaction: if one fails "
            f"the ones before it stay written.",
            icon="warning", default="no"):
            return
        self._set_state("applying", f"Writing {len(changes)} parameter(s)…")
        self.apply_btn.configure(state="disabled")
        mav = getattr(self.page.collector, "mav", None)
        setter = _param_setter(mav)
        snapshot_before = dict(self.page.collector.snapshot().params)

        def work():
            results = []
            for f in changes:
                ok, detail = setter(f.param, float(f.requirement.want))
                results.append((f.param, f.requirement.want, ok, detail))
                if not ok:
                    break
            return results

        def done(results):
            if isinstance(results, Exception):
                self._set_state("failed", f"{results}")
                return
            ok_count = sum(1 for *_x, ok, _d in results if ok)
            lines = ["APPLIED", "-" * 60]
            for name, want, ok, detail in results:
                lines.append(f"[{'ok' if ok else 'FAIL'}] {name} = {want:g}"
                             f"   {detail}")
            if ok_count == len(changes):
                state = "succeeded"
                msg = (f"All {ok_count} parameter(s) written and read back.\n\n"
                       f"The configuration now matches the profile. That is "
                       f"not the same as the EKF using these sources — watch "
                       f"the Used column.")
            elif ok_count:
                state = "partially applied"
                msg = (f"{ok_count} of {len(changes)} written. The rest were "
                       f"not attempted or failed.\n\nNothing has been rolled "
                       f"back: the vehicle's state is now uncertain and "
                       f"another blind write would not help. Re-read the "
                       f"parameters and decide.")
            else:
                state = "failed"
                msg = "Nothing was written."
            lines += ["", msg]
            say(self.plan_box, "\n".join(lines))
            self._set_state(state, msg.split("\n")[0])
            if self.page.session is not None:
                self.page.session.event("profile_apply", {
                    "profile": self.page.profile_key,
                    "outcome": state,
                    "before": {n: snapshot_before.get(n)
                               for n, *_r in results},
                    "results": [{"param": n, "want": w, "ok": ok,
                                 "detail": d} for n, w, ok, d in results]})
            self.page._revalidate()

        def thread():
            try:
                out = work()
            except Exception as ex:
                out = ex
            try:
                self.after(0, lambda: done(out))
            except Exception:
                pass
        threading.Thread(target=thread, daemon=True).start()

    def _set_state(self, state: str, text: str) -> None:
        self.state = state
        colour = {"succeeded": T.OK, "failed": T.ERROR,
                  "partially applied": T.WARN}.get(state, T.TEXT_MUTED)
        self.state_label.configure(text=f"{state.upper()} — {text}",
                                   text_color=colour)
