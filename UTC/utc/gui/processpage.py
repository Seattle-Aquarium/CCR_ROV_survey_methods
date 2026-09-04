"""
Developing a flight's GPR raws into delivery TIFs.

The step after import: take one folder of raws, crop every frame to the survey
size, remove chromatic aberration, run AI Denoise, and export 16-bit ProPhoto
TIFs into a TIF folder beside the source. Lightroom Classic does the imaging;
this page decides what it is asked to do and watches it happen.

Two things shape the screen.

**Check comes before Develop.** A run takes the machine away for the best part
of an hour, so nothing about it should be discoverable only by trying it.
Check is cheap -- it reads the frame sizes, looks for Lightroom, measures the
disk -- and prints what it found in the same box the run's problems appear in.
By the time the confirmation dialog opens, its numbers have been on screen
once already.

**The recipe is not adjustable.** Crop size, colour space and bit depth are
fixed by the survey protocol, not by taste, so they are stated rather than
offered. The only two choices that change the outcome for an operator are
whether to run Denoise -- the step that claims the screen -- and what to do
about TIFs that already exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .. import lightroom as lr
from ..lightroom import gpr
from . import theme as T
from .widgets import Card, button, entry, label

#: How often to notice that the flight folder changed on another page.
_FLIGHT_POLL_MS = 1200

#: How often to check whether the batch has finished.
_RUN_POLL_MS = 400


class ProcessPage(ctk.CTkFrame):
    """Crop, denoise and export a folder of GPRs through Lightroom Classic."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.pre: lr.Preflight | None = None
        self._seen_flight: Path | None = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color=T.BG)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        # ---- 1. source -----------------------------------------------
        c1 = Card(body, "1.  Which raws?",
                  "One folder of .GPR at a time. Sorting an import leaves a "
                  "GPR folder inside each transect; any of them, or any other "
                  "folder of raws, can be pointed at here.")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        c1.body.grid_columnconfigure(0, weight=1)

        row = ctk.CTkFrame(c1.body, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.src_entry = entry(row, "No folder selected", width=620)
        self.src_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        button(row, "Browse…", self._pick, "primary", width=110
               ).grid(row=0, column=1)

        self.recent_row = ctk.CTkFrame(c1.body, fg_color="transparent")
        self.recent_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        button(c1.body, "Check", self._check, "primary", width=140
               ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        self.out = ctk.CTkTextbox(c1.body, height=140, font=T.FONT_MONO,
                                  fg_color=T.FIELD_BG, text_color=T.TEXT_MUTED,
                                  border_width=1, border_color=T.BORDER,
                                  corner_radius=6, wrap="word")
        self.out.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._say("Nothing checked yet.")

        # ---- 2. recipe -----------------------------------------------
        c2 = Card(body, "2.  What gets done",
                  f"Every frame, identically: crop to {lr.CROP_W}x{lr.CROP_H}, "
                  f"remove chromatic aberration, AI Denoise at "
                  f"{lr.DENOISE_AMOUNT}, export 16-bit ProPhoto RGB TIF. The "
                  f"sizes and the colour space are the survey protocol, so "
                  f"they are not adjustable here.")
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        c2.body.grid_columnconfigure(0, weight=1)

        opts = ctk.CTkFrame(c2.body, fg_color="transparent")
        opts.grid(row=0, column=0, sticky="w")
        self.v_denoise = ctk.BooleanVar(value=True)
        self.v_overwrite = ctk.BooleanVar(value=False)
        for i, (txt, var) in enumerate((
            (f"Run AI Denoise (amount {lr.DENOISE_AMOUNT})", self.v_denoise),
            ("Overwrite TIFs that already exist", self.v_overwrite),
        )):
            ctk.CTkCheckBox(opts, text=txt, variable=var, font=T.FONT_BODY,
                            text_color=T.TEXT, fg_color=T.ACCENT,
                            hover_color=T.ACCENT_HOVER,
                            checkmark_color=T.ACCENT_TEXT,
                            border_color=T.FIELD_BORDER, corner_radius=4,
                            command=self._forget_check
                            ).grid(row=0, column=i, padx=(0, 22))

        ctk.CTkLabel(
            c2.body,
            text="Denoise has no scripting interface in any current Lightroom, "
                 "so it is driven through the application's own window: the "
                 "screen, mouse and keyboard belong to it until the batch "
                 "finishes, and the desktop has to stay unlocked. Turn it off "
                 "to run the crop and export unattended.",
            font=T.FONT_SMALL, text_color=T.TEXT_MUTED, anchor="w",
            justify="left", wraplength=780
        ).grid(row=1, column=0, sticky="w", padx=(26, 0), pady=(6, 0))

        # ---- 3. run --------------------------------------------------
        c3 = Card(body, "3.  Develop", "")
        c3.grid(row=2, column=0, sticky="ew")
        c3.body.grid_columnconfigure(0, weight=1)

        acts = ctk.CTkFrame(c3.body, fg_color="transparent")
        acts.grid(row=0, column=0, sticky="w")
        button(acts, "Develop and export", self._go, "primary", width=180
               ).grid(row=0, column=0)
        self.setup_btn = button(acts, "Set up Lightroom…",
                                self._setup_lightroom, "ghost", width=170)
        self.setup_btn.grid(row=0, column=1, padx=(8, 0))
        self.setup_btn.grid_remove()

        self.plan_note = ctk.CTkLabel(
            c3.body, text="Check a folder first.", font=T.FONT_SMALL,
            text_color=T.TEXT_MUTED, anchor="w", justify="left")
        self.plan_note.grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.refresh_sources()
        self._poll_flight()

    # ------------------------------------------------------------------
    #  the folder
    # ------------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.configure(state="disabled")

    @property
    def source(self) -> Path | None:
        t = self.src_entry.get().strip()
        return Path(t) if t else None

    def _set_src(self, path: Path) -> None:
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, str(path))
        self._forget_check()

    def _forget_check(self) -> None:
        """A changed folder or option makes the last Check stale."""
        self.pre = None
        self.plan_note.configure(text="Check a folder first.",
                                 text_color=T.TEXT_MUTED)

    def _pick(self) -> None:
        chosen = filedialog.askdirectory(title="Folder holding the .GPR files")
        if chosen:
            self._set_src(Path(chosen))
            self._check()

    def refresh_sources(self) -> None:
        """Offer the GPR folders the current flight actually has.

        Read off the flight folder rather than remembered from an import, so
        it is right whether the imagery arrived this session or last week. An
        import scatters raws across one folder per transect, so there is rarely
        a single "the" GPR folder -- offering them all beats guessing, and the
        field stays typeable either way.
        """
        for w in self.recent_row.winfo_children():
            w.destroy()
        flight = getattr(self.app, "flight_dir", None)
        self._seen_flight = Path(flight) if flight else None
        dirs = gpr.find_folders(flight) if flight else []
        if not dirs:
            return
        label(self.recent_row, "In this flight:", muted=True
              ).grid(row=0, column=0, padx=(0, 8))
        for i, d in enumerate(dirs[:6]):
            name = f"{d.parent.name}/{d.name}" if d.parent.name else d.name
            button(self.recent_row, name, lambda q=d: self._use(q), "ghost",
                   width=150).grid(row=0, column=i + 1, padx=(0, 6))
        if not self.source:
            self._set_src(dirs[0])

    def _use(self, path: Path) -> None:
        self._set_src(path)
        self._check()

    def _poll_flight(self) -> None:
        """Notice a flight folder chosen on another page.

        Polled rather than hooked into the navigator's selection callback,
        which the application does not currently pass one to. The comparison is
        an attribute read; the folder is only walked when it has changed.
        """
        try:
            flight = getattr(self.app, "flight_dir", None)
            now = Path(flight) if flight else None
            if now != self._seen_flight:
                self.refresh_sources()
        finally:
            self.after(_FLIGHT_POLL_MS, self._poll_flight)

    # ------------------------------------------------------------------
    #  checking
    # ------------------------------------------------------------------

    def _check(self) -> lr.Preflight | None:
        src = self.source
        if not src:
            messagebox.showinfo(self.app.title(),
                                "Choose a folder of GPR files.")
            return None
        self._say(f"Looking at {src} …")
        self.update_idletasks()
        pre = lr.check(src, self._current_options())
        self.pre = pre

        lines = [pre.survey.describe() if pre.survey else "nothing found"]
        lines.append(f"out: {pre.tif_dir}")
        for size, rect in sorted(pre.crops.items()):
            lines.append(f"  crop {size[0]}x{size[1]} -> "
                         f"{rect.size_in(*size)[0]}x{rect.size_in(*size)[1]}")
        for n in pre.notes:
            lines.append(f"  · {n}")
        for p in pre.problems:
            lines.append(f"  PROBLEM: {p}")
        self._say("\n".join(lines))

        if pre.ok:
            self.plan_note.configure(
                text=f"Ready: {pre.count} frame(s) -> {pre.tif_dir}",
                text_color=T.TEXT)
        else:
            self.plan_note.configure(text=pre.problems[0],
                                     text_color=T.TEXT_MUTED)
        if pre.needs_setup:
            self.setup_btn.grid()
        else:
            self.setup_btn.grid_remove()
        return pre

    # _current_options, not _options: tkinter.Widget owns that name, and
    # shadowing it breaks widget construction with a TypeError pointing
    # nowhere near the method that caused it. Same for _setup_lightroom.
    def _current_options(self) -> lr.RawDevelopOptions:
        return lr.RawDevelopOptions(
            denoise=bool(self.v_denoise.get()),
            overwrite=bool(self.v_overwrite.get()),
        )

    # ------------------------------------------------------------------
    #  one-time Lightroom setup
    # ------------------------------------------------------------------

    def _setup_lightroom(self) -> None:
        """The one-time setup: register the plug-in, and make the seed catalog.

        Both halves need Lightroom's own hands. The plug-in has to be added
        through the Plug-in Manager -- dropping it in a folder does nothing,
        because Lightroom Classic does not auto-load from Modules and says so
        nowhere. The seed has to be made with File > New Catalog, because
        neither the SDK nor the command line will create a catalog.

        So both are done in a single visit, in an order that survives the
        restart: register first, because creating a catalog relaunches
        Lightroom and the registration is what has to be in place afterwards.
        """
        from ..lightroom import install

        try:
            app = install.find_lightroom()
        except install.LightroomNotSetUp as ex:
            messagebox.showerror(self.app.title(), str(ex))
            return
        if install.lightroom_is_running():
            messagebox.showinfo(
                self.app.title(),
                "Close Lightroom Classic first, then press this again.")
            return

        plugin = install.install_plugin()
        need_plugin = install.plugin_registration() != "registered"
        need_seed = not install.seed_is_ready(app)
        if not (need_plugin or need_seed):
            messagebox.showinfo(
                self.app.title(),
                f"Lightroom {app.version} is already set up. Nothing to do.")
            self._check()
            return

        staging = install.seed_dir(app.version) / "staging"
        staging.mkdir(parents=True, exist_ok=True)

        # The plug-in path has to be pasted into a file dialog, so put it on
        # the clipboard rather than asking anyone to retype it.
        target = str(plugin) if need_plugin else str(staging)
        try:
            self.clipboard_clear()
            self.clipboard_append(target)
        except Exception:
            pass

        steps = []
        if need_plugin:
            steps.append(
                "  A. Register the plug-in\n"
                "       File > Plug-in Manager…  (Ctrl+Alt+Shift+comma)\n"
                "       Add  →  paste the path  →  Select Folder\n"
                "       it should list 'UTC RAW develop' as Installed and "
                "running\n"
                "       Done\n\n"
                f"       {plugin}\n"
                "       (already copied to your clipboard)")
        if need_seed:
            steps.append(
                "  B. Make the empty seed catalog\n"
                "       File > New Catalog…\n"
                "       save it in the staging folder that just opened\n"
                "       name it UTC_seed\n"
                "       Lightroom restarts — this is normal\n"
                "       import nothing at all\n\n"
                f"       {staging}")

        order = ("Do A first: creating a catalog restarts Lightroom, and the "
                 "registration has to survive that.\n\n"
                 if need_plugin and need_seed else "")

        if not messagebox.askokcancel(
            self.app.title(),
            f"One-time setup for Lightroom {app.version}.\n\n"
            + order
            + "\n\n".join(steps)
            + "\n\nThen quit Lightroom and press OK on the next dialog.\n\n"
              "Press OK now to open Lightroom."
        ):
            return

        try:
            os.startfile(staging if need_seed else plugin.parent)  # noqa: S606
        except OSError:
            pass
        # Against a catalog that exists: Lightroom reopens whatever it had
        # last, and after a run that is a scratch catalog UTC has cleaned, so
        # a bare launch would land on "the catalog was not found".
        own = install.last_real_catalog()
        try:
            subprocess.Popen([str(app.exe)] + ([str(own)] if own else []))
        except OSError as ex:
            messagebox.showerror(self.app.title(),
                                 f"Could not start Lightroom: {ex}")
            return

        messagebox.showinfo(
            self.app.title(),
            "Press OK once you have finished in Lightroom and quit it.")

        self._finish_setup(app, need_seed)

    def _finish_setup(self, app, need_seed: bool) -> None:
        """Check what actually got done, and say precisely what did not."""
        from ..lightroom import install

        outstanding = []

        if need_seed:
            staging = install.seed_dir(app.version) / "staging"
            made = install.find_new_catalog(staging)
            if made is None:
                outstanding.append(
                    f"No new empty catalog turned up in\n    {staging}\n"
                    f"If you saved it elsewhere, move the .lrcat folder there "
                    f"and press Set up Lightroom again.")
            else:
                try:
                    install.adopt_seed(made, app)
                except install.LightroomNotSetUp as ex:
                    outstanding.append(str(ex))

        state = install.plugin_registration()
        if state == "disabled":
            outstanding.append(
                "The plug-in is registered but switched off. Turn it back on "
                "in File > Plug-in Manager.")
        elif state == "absent":
            outstanding.append(
                "Lightroom still has no record of the plug-in. In Lightroom: "
                "File > Plug-in Manager > Add, then pick\n"
                f"    {install.installed_plugin()}")
        elif state == "unknown":
            outstanding.append(
                "Lightroom's preferences could not be read, so whether the "
                "plug-in registered is unknown. Try a run and see.")

        if outstanding:
            messagebox.showwarning(
                self.app.title(),
                "Not finished yet:\n\n• " + "\n\n• ".join(outstanding))
        else:
            messagebox.showinfo(
                self.app.title(),
                f"Lightroom {app.version} is set up.\n\n"
                f"The plug-in is registered and the seed catalog is in place. "
                f"Every run now copies the seed to a scratch catalog of its "
                f"own and deletes it afterwards. Only a Lightroom update asks "
                f"for this again.")
            self.app._log(f"Lightroom {app.version} set up for UTC RAW develop")
        self._check()

    # ------------------------------------------------------------------
    #  running
    # ------------------------------------------------------------------

    def _go(self) -> None:
        pre = self.pre if (self.pre and self.pre.source == self.source) \
            else self._check()
        if pre is None:
            return
        if not pre.ok:
            messagebox.showerror(
                self.app.title(),
                "Not ready yet:\n\n• " + "\n• ".join(pre.problems[:6]))
            return

        opts = self._current_options()
        detail = "\n".join(f"• {n}" for n in pre.notes)
        warning = (
            "\n\nWhile Denoise runs, Lightroom owns the screen, the mouse and "
            "the keyboard. Do not use this machine until it finishes, and do "
            "not lock the screen or disconnect — both stop the batch.\n\n"
            "Stop is immediate up until Denoise starts. After that it can only "
            "skip the export; Lightroom finishes the batch it has begun."
            if opts.denoise else
            "\n\nDenoise is off, so this runs unattended: crop, chromatic "
            "aberration and export only."
        )
        if not messagebox.askyesno(
            self.app.title(),
            f"Develop {pre.count} frame(s) from\n{pre.source}\n\n"
            f"{detail}{warning}\n\nStart?"
        ):
            return

        src = pre.source

        def work(progress, cancel):
            return lr.run_batch(src, opts, progress, cancel, preflight=pre)

        if self.app.submit(work, f"Developing {pre.count} GPR to TIF…"):
            self._lock_app(True)
            self._watch_run()

    # ------------------------------------------------------------------
    #  keeping the rest of the app out of the way
    # ------------------------------------------------------------------

    def _lock_app(self, on: bool) -> None:
        """Grey out the page rail while Lightroom has the screen.

        The app already allows only one worker, so this is not about a second
        job -- it is that the batch drives Lightroom's window with synthetic
        keystrokes, and a click that switches tabs mid-sequence lands somewhere
        nobody intended. Done from here rather than by adding a lock to App, so
        the feature stays inside its own files.
        """
        nav = getattr(self.app, "nav", None)
        if nav is None:
            return
        for name in ["Flight setup"] + list(getattr(self.app, "pages", {})):
            try:
                nav.set_enabled(name, not on)
            except Exception:
                pass

    def _watch_run(self) -> None:
        """Release the lock once the worker finishes, however it finished.

        Polled rather than hooked into the application's completion path: a Tk
        timer here costs nothing and needs no callback wired into shared code.
        """
        if self.app.busy:
            self.after(_RUN_POLL_MS, self._watch_run)
            return
        self._lock_app(False)
        self.refresh_sources()
