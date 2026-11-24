"""
Profile ROV Flight Operations' window on the laptop it runs on.

Written on the field laptop in November 2026 to find out why the window was
slow there (README: "Changes for the field laptops' screens"). It runs the
real application in its own event loop, with a **temporary LOCALAPPDATA** --
so this laptop's settings, flight folder and cache are never touched -- and,
with `--sim`, the ROV flight simulator flying (which reaches no vehicle and
writes only into that temporary folder). Nothing it does is read-only-unsafe:
it opens tabs, waits, and resizes and moves its own window.

What it reports, per tab:

* the time to open it (select, lay out, paint);
* while it sits on screen: the window thread's CPU and the other threads',
  how late a 10 ms probe timer runs (how long the event loop was blocked --
  the "stutter"), and which Python callbacks (timers, bindings, commands)
  took the time;
* then a second round of tab changes, at a window size every tab has seen;
* with `--resize`, a 44-step drag of the window's width on four tabs, and with
  `--move`, sixty small moves.

Run it from the `rov_flight_ops` folder, in the program's environment::

    %LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv\Scripts\python.exe ^
        tools\profile_window.py --sim --zoomed --resize --move --out report.json

Options: `--tabs "Monitoring,DVL"` for some tabs only, `--dwell 6` seconds
per tab, `--shots folder` to save a screenshot of each tab, `--profile
Navigation` for a cProfile of one tab's dwell (on Python 3.12 and later that
profile covers every thread, so read it with that in mind). Environment:
`PERF_INSTRUMENT=1` times the main pages' own methods as well;
`PERF_MONITOR_CANVAS=1` and `PERF_DVL_CANVAS=1` time building and drawing the
Monitoring and DVL charts separately; `ROV_UI_DENSITY` chooses the layout.

It opens a window on the screen for a minute or two; leave it alone while it
runs -- a click lands in the measurement.
"""

from __future__ import annotations

import argparse
import collections
import cProfile
import io
import json
import os
import pstats
import statistics
import tempfile
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--sim", action="store_true")
ap.add_argument("--zoomed", action="store_true")
ap.add_argument("--dwell", type=float, default=6.0)
ap.add_argument("--profile", default="")
ap.add_argument("--resize", action="store_true")
ap.add_argument("--move", action="store_true")
ap.add_argument("--tabs", default="all")
ap.add_argument("--out", default="")
ap.add_argument("--top", type=int, default=12)
ap.add_argument("--shots", default="")
args = ap.parse_args()

tmp = tempfile.mkdtemp(prefix="rfo_perf_")
os.environ["LOCALAPPDATA"] = tmp

import tkinter  # noqa: E402

import psutil  # noqa: E402

# ---------------------------------------------------------------------------
#  time every Python callback Tcl makes: after, bindings, commands
# ---------------------------------------------------------------------------

STATS: dict[str, list] = collections.defaultdict(lambda: [0, 0.0, 0.0])
_orig_call = tkinter.CallWrapper.__call__
_depth = [0]
#: Callbacks at this depth or shallower are counted; the resize test raises it
#: to 2 so what runs inside its update() calls is seen.
RECORD_DEPTH = [1]


def key_of(func) -> str:
    f = func
    prefix = ""
    qn = getattr(f, "__qualname__", "")
    if qn.endswith("after.<locals>.callit"):
        prefix = "after:"
        try:
            cells = dict(zip(f.__code__.co_freevars, f.__closure__, strict=False))
            f = cells["func"].cell_contents
        except Exception:
            pass
    self_ = getattr(f, "__self__", None)
    if self_ is not None:
        return prefix + f"{type(self_).__name__}.{getattr(f, '__name__', '?')}"
    return prefix + getattr(f, "__qualname__", repr(f))


def timed_call(self, *a):
    _depth[0] += 1
    t0 = time.perf_counter()
    try:
        return _orig_call(self, *a)
    finally:
        dt = time.perf_counter() - t0
        _depth[0] -= 1
        if _depth[0] < RECORD_DEPTH[0]:    # outermost only: no double counting
            s = STATS[key_of(self.func)]
            s[0] += 1
            s[1] += dt
            if dt > s[2]:
                s[2] = dt


tkinter.CallWrapper.__call__ = timed_call

from rov_flight_ops.gui.app import App  # noqa: E402

# ---------------------------------------------------------------------------
#  method-level timing (main thread only, inclusive)
# ---------------------------------------------------------------------------

MSTATS: dict[str, list] = collections.defaultdict(lambda: [0, 0.0, 0.0])
_MAIN = threading.get_ident()


def instrument(cls, name: str, label: str | None = None) -> None:
    fn = getattr(cls, name, None)
    if fn is None or getattr(fn, "_perf", False):
        return
    key = label or f"{getattr(cls, '__name__', cls)}.{name}"

    def wrapper(*a, **kw):
        if threading.get_ident() != _MAIN:
            return fn(*a, **kw)
        t = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            dt = time.perf_counter() - t
            s = MSTATS[key]
            s[0] += 1
            s[1] += dt
            if dt > s[2]:
                s[2] = dt
    wrapper._perf = True
    setattr(cls, name, wrapper)


def instrument_all() -> None:
    import customtkinter as _ctk

    from rov_flight_ops import board as B
    from rov_flight_ops.gui import (
        dvlpage,
        flightstrip,
        goboard,
        monitorpage,
        navdraw,
        navmap,
        navpage,
        navplanpanel,
        shell,
        simbanner,
        ugpsstrip,
    )
    from rov_flight_ops.sim import controller
    for cls, names in [
        (navpage.NavigationPage, ["_tick", "_render", "_render_map", "_render_trust",
                                  "_render_guidance", "_render_strip", "_render_foot",
                                  "_render_ugps", "refresh"]),
        (navpage._SensorMatrix, ["update_from"]),
        (navplanpanel.PlanPanel, ["refresh"]),
        (flightstrip.FlightNumbers, ["update_from", "_tick_clock"]),
        (navmap.MapCanvas, ["draw", "_draw_tiles", "_draw_grid", "_draw_track",
                            "_draw_planned", "_draw_markers", "_draw_site",
                            "_draw_vessel", "_draw_rov", "_draw_scale",
                            "_draw_attribution"]),
        (navdraw.PlanEditor, ["draw"]),
        (ugpsstrip.ReceiversStrip, ["update_from"]),
        (monitorpage.MonitorPage, ["_tick", "_update_state", "_redraw", "_update_board",
                                   "_judged", "refresh"]),
        (goboard.GoBoard, ["update_from", "fit"]),
        (dvlpage.DvlPage, ["_tick", "_update_text", "_draw_beams", "_draw_charts",
                           "_draw_acoustic", "refresh"]),
        (shell.Shell, ["_tick_status", "_maybe_read_versions", "_paint_header"]),
        (shell.Lamp, ["set_state"]),
        (shell.TabStrip, ["select"]),
        (simbanner.SimControls, ["tick", "_paint"]),
        (controller.Simulator, ["tick", "banner"]),
        (_ctk.CTkLabel, ["configure", "_draw"]),
        (_ctk.CTkButton, ["configure", "_draw"]),
        (_ctk.CTkFrame, ["configure", "_draw"]),
        (_ctk.CTkSwitch, ["configure", "_draw"]),
    ]:
        for n in names:
            instrument(cls, n)
    instrument(B, "judge", "board.judge")
    from rov_flight_ops import bands as BANDS
    from rov_flight_ops.ugps import health as UH
    for name in ("_latest_levels", "_receivers", "_tether", "_laptop", "_dvl",
                 "_recording", "_vehicle_link"):
        instrument(B, name, f"board.{name}")
    instrument(BANDS, "level_now", "bands.level_now")
    instrument(BANDS, "levels", "bands.levels")
    instrument(UH, "judge", "ugps.health.judge")
    instrument(monitorpage.MonitorPage, "_strip")
    instrument(monitorpage.MonitorPage, "_plot")
    try:
        from rov_flight_ops.gui import paramspage
        instrument(paramspage.ParamsPage, "_tick")
        instrument(paramspage.ParamsPage, "_update")
        instrument(paramspage.Table, "show")
    except Exception:
        pass


if os.environ.get("PERF_INSTRUMENT"):
    instrument_all()

PROC = psutil.Process()
REPORT: dict = {"localappdata": tmp, "phases": []}


def log(*a) -> None:
    print(*a, flush=True)


t0 = time.perf_counter()
app = App()
t_build = time.perf_counter() - t0
REPORT["build_s"] = round(t_build, 3)

import customtkinter as ctk  # noqa: E402

main_ident = threading.get_ident()


class Probe:
    """A 10 ms timer: how late it runs is how long the loop was busy."""

    def __init__(self, root, period_ms: int = 10) -> None:
        self.root = root
        self.period_ms = period_ms
        self.lates: list[float] = []
        self.running = False

    def start(self) -> None:
        self.lates = []
        self.running = True
        self.due = time.perf_counter() + self.period_ms / 1000
        self.root.after(self.period_ms, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        self.lates.append(now - self.due)
        if self.running:
            self.due = now + self.period_ms / 1000
            self.root.after(self.period_ms, self._tick)

    def stop(self) -> None:
        self.running = False


probe = Probe(app)


def snapshot_stats() -> dict:
    return {k: list(v) for k, v in STATS.items()}


def diff_stats(before: dict, after: dict) -> list[tuple]:
    out = []
    for k, v in after.items():
        b = before.get(k, [0, 0.0, 0.0])
        n = v[0] - b[0]
        tot = v[1] - b[1]
        if n:
            out.append((k, n, tot, v[2]))
    out.sort(key=lambda x: -x[2])
    return out


def cpu_split() -> tuple[float, float]:
    """(main thread CPU s, all threads CPU s)."""
    main = time.thread_time()
    ct = PROC.cpu_times()
    return main, ct.user + ct.system


def pump_once() -> None:
    try:
        app.update_idletasks()
        app.update()
    except tkinter.TclError:
        pass


def screen_info() -> dict:
    return {
        "screen_px": (app.winfo_screenwidth(), app.winfo_screenheight()),
        "ctk_widget_scaling": ctk.ScalingTracker.get_widget_scaling(app),
        "ctk_window_scaling": ctk.ScalingTracker.get_window_scaling(app),
        "tk_scaling": float(app.tk.call("tk", "scaling")),
        "window_geometry": tkinter.Tk.geometry(app),
        "window_state": app.state(),
        "content_px": (app.content.winfo_width(), app.content.winfo_height()),
        "header_px": (app.header.winfo_width(), app.header.winfo_height()),
        "footer_px": (app.footer.winfo_width(), app.footer.winfo_height()),
    }


def count_widgets(w) -> int:
    n = 1
    for c in w.winfo_children():
        n += count_widgets(c)
    return n


def shot(name: str) -> None:
    if not args.shots:
        return
    try:
        from PIL import ImageGrab
        app.update()
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        os.makedirs(args.shots, exist_ok=True)
        img.save(os.path.join(args.shots, f"{name}.png"))
    except Exception as ex:
        log("screenshot failed", ex)


def director():
    # Let the start-up warm-up go round the tabs and come back.
    yield 2.0
    REPORT["screen"] = screen_info()
    REPORT["widgets_total"] = count_widgets(app)
    REPORT["widgets_per_tab"] = {n: count_widgets(f) for n, f in app.nav._page_frames.items()}
    log("screen", json.dumps(REPORT["screen"]))
    log("widgets", REPORT["widgets_total"], REPORT["widgets_per_tab"])
    kinds = collections.Counter()

    def walk(w):
        kinds[type(w).__name__] += 1
        for c in w.winfo_children():
            walk(c)
    walk(app)
    REPORT["widget_kinds"] = kinds.most_common(25)
    log("kinds", kinds.most_common(25))
    # Native windows: every Tk widget on Windows is its own HWND.
    try:
        import ctypes
        n_hwnd = [0]
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(h, _l):
            n_hwnd[0] += 1
            return True
        top = ctypes.windll.user32.GetParent(app.winfo_id())
        ctypes.windll.user32.EnumChildWindows(top, EnumProc(cb), 0)
        REPORT["hwnds"] = n_hwnd[0]
        log("child HWNDs under the window:", n_hwnd[0])
    except Exception as ex:
        log("hwnd count failed", ex)

    # Which hidden widgets still claim to be mapped?
    nav = app.pages.get("navigation")
    mon = app.pages.get("monitor")
    dvl = app.pages.get("dvl")
    gates = {
        "nav.numbers ismapped/viewable": (nav.numbers.winfo_ismapped(), nav.numbers.winfo_viewable()),
        "nav.map.canvas ismapped/viewable": (nav.map.canvas.winfo_ismapped(), nav.map.canvas.winfo_viewable()),
        "monitor.canvas ismapped/viewable": (mon.canvas.winfo_ismapped(), mon.canvas.winfo_viewable()),
        "dvl.beams ismapped/viewable": (dvl.beams.winfo_ismapped(), dvl.beams.winfo_viewable()),
        "current tab": app.nav.current,
    }
    REPORT["visibility_gates"] = gates
    log("gates", gates)

    if args.zoomed:
        app.state("zoomed")
        yield 1.0
        REPORT["screen_zoomed"] = screen_info()
        log("zoomed", json.dumps(REPORT["screen_zoomed"]))

    if args.sim:
        app.simulator.engage()
        if app.sim_controls is not None:
            app.sim_controls.switch.select()
        yield 4.0

    tabs = app.nav.sections if args.tabs == "all" else args.tabs.split(",")
    for name in tabs:
        # open the tab, timing the switch
        before_sel = snapshot_stats()
        ta = time.perf_counter()
        app.nav.select(name)
        tb = time.perf_counter()
        app.update_idletasks()
        tc = time.perf_counter()
        app.update()
        app.update()
        td = time.perf_counter()
        sw = {"select_ms": round((tb - ta) * 1000, 1),
              "layout_ms": round((tc - tb) * 1000, 1),
              "paint_ms": round((td - tc) * 1000, 1),
              "total_ms": round((td - ta) * 1000, 1)}
        sel_top = diff_stats(before_sel, snapshot_stats())[:6]
        yield 0.6
        shot(f"tab_{name.replace(' ', '_')}")
        # dwell
        prof = cProfile.Profile() if args.profile and args.profile == name else None
        mbefore = {k: list(v) for k, v in MSTATS.items()}
        before = snapshot_stats()
        m0, a0 = cpu_split()
        w0 = time.perf_counter()
        probe.start()
        if prof:
            prof.enable()
        yield args.dwell
        if prof:
            prof.disable()
        probe.stop()
        w1 = time.perf_counter()
        m1, a1 = cpu_split()
        top = diff_stats(before, snapshot_stats())
        lates = sorted(probe.lates) or [0.0]
        wall = w1 - w0
        ph = {
            "tab": name,
            "switch": sw,
            "switch_top_callbacks": [(k, n, round(t * 1000, 1)) for k, n, t, _m in sel_top],
            "main_cpu_pct": round(100 * (m1 - m0) / wall, 1),
            "other_threads_cpu_pct": round(100 * ((a1 - a0) - (m1 - m0)) / wall, 1),
            "probe_late_ms": {
                "p50": round(1000 * statistics.median(lates), 1),
                "p95": round(1000 * lates[int(0.95 * (len(lates) - 1))], 1),
                "max": round(1000 * lates[-1], 1),
                "n": len(lates),
            },
            "callbacks_ms_per_s": round(1000 * sum(t for k, n, t, m in top
                                                   if not k.endswith("Probe._tick")) / wall, 1),
            "top": [(k, n, round(1000 * t / wall, 2), round(1000 * m, 1))
                    for k, n, t, m in top if not k.endswith("Probe._tick")][:args.top],
        }
        REPORT["phases"].append(ph)
        log(f"\n=== {name}: switch {sw}  main CPU {ph['main_cpu_pct']}%  "
            f"other threads {ph['other_threads_cpu_pct']}%  "
            f"probe late {ph['probe_late_ms']}  callbacks {ph['callbacks_ms_per_s']} ms/s")
        for k, n, per_s, mx in ph["top"]:
            log(f"   {per_s:8.2f} ms/s  n={n:5d}  max={mx:7.1f} ms  {k}")
        if MSTATS:
            mt = diff_stats(mbefore, {k: list(v) for k, v in MSTATS.items()})
            ph["methods"] = [(k, n, round(1000 * t / wall, 2), round(1000 * m, 1),
                              round(1000 * t / n, 2)) for k, n, t, m in mt][:40]
            log("   -- methods (inclusive, main thread) --")
            for k, n, per_s, mx, mean in ph["methods"][:28]:
                log(f"   {per_s:8.2f} ms/s  n={n:5d}  mean={mean:6.2f}  max={mx:7.1f} ms  {k}")
        if prof:
            sio = io.StringIO()
            st = pstats.Stats(prof, stream=sio).sort_stats("cumulative")
            st.print_stats(45)
            REPORT["profile_" + name] = sio.getvalue()
            sio2 = io.StringIO()
            pstats.Stats(prof, stream=sio2).sort_stats("tottime").print_stats(35)
            REPORT["profile_tottime_" + name] = sio2.getvalue()
            log(sio2.getvalue()[:6000])

    if os.environ.get("PERF_MONITOR_CANVAS"):
        app.nav.select("Monitoring")
        app.update()
        page = app.pages["monitor"]
        for win in ("2 min", "10 min", "30 min"):
            page._window = win
            builds, shows = [], []
            for _ in range(6):
                ta = time.perf_counter()
                page._redraw()
                tb = time.perf_counter()
                app.update_idletasks()
                tc = time.perf_counter()
                builds.append(tb - ta)
                shows.append(tc - tb)
            log(f"--- monitor canvas {win}: build median {1000*statistics.median(builds):.1f} ms"
                f"  display median {1000*statistics.median(shows):.1f} ms  items "
                f"{len(page.canvas.find_all())}")
        page._window = "10 min"
        yield 0.2

    if os.environ.get("PERF_DVL_CANVAS"):
        app.nav.select("DVL")
        app.update()
        yield 3.0                       # let a few seconds of history arrive
        page = app.pages["dvl"]
        for name, fn, cv in (("charts", page._draw_charts, page.charts),
                             ("beams", page._draw_beams, page.beams),
                             ("acoustic", page._draw_acoustic, page.acoustic)):
            builds, shows = [], []
            for _ in range(5):
                ta = time.perf_counter()
                fn()
                tb = time.perf_counter()
                app.update_idletasks()
                tc = time.perf_counter()
                builds.append(tb - ta)
                shows.append(tc - tb)
            pts = sum(len(cv.coords(i)) // 2 for i in cv.find_all() if cv.type(i) == "line")
            log(f"--- dvl {name}: build median {1000*statistics.median(builds):.1f} ms"
                f"  display median {1000*statistics.median(shows):.1f} ms  line points {pts}")
        yield 0.2

    # Steady state: every tab has been visited at this window size already.
    steady = {}
    for _round in range(2):
        for name in tabs:
            ta = time.perf_counter()
            app.nav.select(name)
            app.update_idletasks()
            app.update()
            app.update()
            steady.setdefault(name, []).append(round((time.perf_counter() - ta) * 1000, 1))
            yield 0.35
    REPORT["steady_switch_ms"] = steady
    log("\n--- steady-state tab switches (ms, two rounds):")
    for name, v in steady.items():
        log(f"   {name:20s} {v}")

    if args.resize:
        for name in ("Monitoring", "Navigation", "DVL", "BlueOS logs"):
            app.nav.select(name)
            app.state("normal")
            pump_once()
            # Physical pixels: Tk's own screen size is the pre-DPI-awareness one.
            h = 900
            widths = list(range(1900, 1380, -24)) + list(range(1380, 1900, 24))
            times = []
            before = snapshot_stats()
            RECORD_DEPTH[0] = 2
            shows = [0]
            real_show = getattr(app.nav, "_show", None)
            if real_show is not None:
                def counted(nm, _real=real_show, _shows=shows):
                    _shows[0] += 1
                    if not os.environ.get("PERF_NO_FOLLOW"):
                        _real(nm)
                app.nav._show = counted
            sweep0 = time.perf_counter()
            for wpx in widths:
                tkinter.Tk.geometry(app, f"{wpx}x{h}+0+0")
                ta = time.perf_counter()
                app.update_idletasks()
                app.update()
                times.append(time.perf_counter() - ta)
            # the drag "ends": let any trailing layout pass run, and time it
            settle0 = time.perf_counter()
            end_by = settle0 + 1.0
            while time.perf_counter() < end_by:
                app.update()
                time.sleep(0.005)
            sweep = settle0 - sweep0
            RECORD_DEPTH[0] = 1
            if real_show is not None:
                app.nav._show = real_show
                log(f"   page laid out {shows[0]} times during the sweep")
            top = [x for x in diff_stats(before, snapshot_stats())
                   if not x[0].startswith("after:run.")][:12]
            ts = sorted(times)
            res = {"tab": name, "steps": len(times),
                   "median_ms": round(1000 * statistics.median(ts), 1),
                   "p90_ms": round(1000 * ts[int(0.9 * (len(ts) - 1))], 1),
                   "max_ms": round(1000 * ts[-1], 1),
                   "sweep_s": round(sweep, 2),
                   "top": [(k, n, round(1000 * t, 1)) for k, n, t, _m in top]}
            REPORT.setdefault("resize", []).append(res)
            log(f"\n--- resize {name}: {res['steps']} steps median {res['median_ms']} ms "
                f"p90 {res['p90_ms']} max {res['max_ms']}  whole sweep {res['sweep_s']} s")
            for k, n, t in res["top"]:
                log(f"     {t:8.1f} ms total  n={n:5d}  {k}")
            yield 0.3

    if args.move:
        app.nav.select("Navigation")
        app.state("normal")
        tkinter.Tk.geometry(app, "1600x900+0+0")
        pump_once()
        times = []
        before = snapshot_stats()
        for i in range(60):
            x = 10 + (i % 30) * 8
            tkinter.Tk.geometry(app, f"+{x}+{10 + (i % 7) * 3}")
            ta = time.perf_counter()
            app.update_idletasks()
            app.update()
            times.append(time.perf_counter() - ta)
        ts = sorted(times)
        top = diff_stats(before, snapshot_stats())[:10]
        REPORT["move"] = {"median_ms": round(1000 * statistics.median(ts), 1),
                          "max_ms": round(1000 * ts[-1], 1),
                          "top": [(k, n, round(1000 * t, 1)) for k, n, t, _m in top]}
        log("\n--- move", REPORT["move"])

    yield 0.2


def run(gen) -> None:
    def step() -> None:
        try:
            delay = next(gen)
        except StopIteration:
            try:
                if args.out:
                    with open(args.out, "w", encoding="utf-8") as fh:
                        json.dump(REPORT, fh, indent=1, default=str)
            finally:
                os._exit(0)
            return
        except Exception:
            import traceback
            traceback.print_exc()
            os._exit(1)
        app.after(int(delay * 1000), step)
    app.after(0, step)


log(f"built in {t_build:.2f} s; LOCALAPPDATA={tmp}")
run(director())
app.mainloop()
