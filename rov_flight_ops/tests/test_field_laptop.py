"""
What keeps the window quick and the layout whole on the field laptops.

The field laptops are Dell Rugged machines at 1920 x 1080 and 150 % -- 1280 x
672 logical pixels above the taskbar -- where the program had been laid out on
a 4K screen at 250 %. Profiling one in November 2026 found the costs these
tests hold down; none of them measures a time (a test that fails when a laptop
is busy is a test people learn to ignore), they pin the structure the
measurements led to:

* text is cut to fit by halving, not a character at a time, and remembered;
* `widgets.Label` is one native window, follows the appearance mode and the
  display's scaling, and does nothing when told what it already shows;
* the layout density follows the screen, and can be chosen;
* the strip charts draw a point every few pixels, keep a spike's height, and
  never a wide line with rounded joins;
* a page that is not on screen is not redrawn;
* the corrections to CustomTkinter are on.
"""

from __future__ import annotations

import sys
import tkinter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops.gui import theme as T  # noqa: E402
from rov_flight_ops.gui.widgets import elide  # noqa: E402

# --------------------------------------------------------------------------
#  cutting text to fit
# --------------------------------------------------------------------------


class _Mono:
    """A font where every character is 7 pixels, counting its measurements."""

    calls = 0

    def measure(self, text: str) -> int:
        _Mono.calls += 1
        return 7 * len(text)

    def __str__(self) -> str:
        return "mono7"


def _linear(font, text, room, strip=""):
    """The way it used to be done, one character at a time."""
    if font.measure(text) <= room:
        return text
    cut = text
    while cut and font.measure(cut + "…") > room:
        cut = cut[:-1]
    cut = cut.rstrip(strip) if strip else cut
    return cut + "…" if cut else "…"


@pytest.mark.parametrize("room", [0.5, 6, 7, 20, 70, 140, 300, 1000])
def test_halving_cuts_exactly_where_one_character_at_a_time_did(room):
    text = "acoustically aided — last fix 2 s ago · received, not confirmed"
    for strip in ("", " ,;·-—"):
        assert elide(_Mono(), text, room, strip) == _linear(_Mono(), text, room, strip)


def test_cutting_a_long_line_measures_a_handful_of_times_and_remembers():
    text = "x" * 400 + str(id(object()))          # unique, so not cached yet
    _Mono.calls = 0
    first = elide(_Mono(), text, 210)
    assert first.endswith("…") and _Mono().measure(first) <= 210
    assert _Mono.calls <= 12, "one measurement per character dropped"
    _Mono.calls = 0
    assert elide(_Mono(), text, 210) == first
    assert _Mono.calls == 0, "the same text at the same width is remembered"


# --------------------------------------------------------------------------
#  the density
# --------------------------------------------------------------------------


def test_the_density_follows_the_screen_and_can_be_chosen():
    assert T.density_for(672) == "compact"          # the field laptop
    assert T.density_for(912) == "comfortable"      # the 4K development laptop
    assert T.density_for(None) == "comfortable"
    assert T.density_for(1200, "compact") == "compact"
    assert T.density_for(600, "comfortable") == "comfortable"


def test_compact_takes_the_room_from_chrome_not_from_body_copy():
    was = T.DENSITY
    try:
        T.set_density("comfortable")
        roomy = (T.PAD, T.GAP, T.FONT_H1[1], T.FONT_TAB[1], T.TAB_H)
        body = T.FONT_BODY
        T.set_density("compact")
        tight = (T.PAD, T.GAP, T.FONT_H1[1], T.FONT_TAB[1], T.TAB_H)
        assert all(t < r for t, r in zip(tight, roomy, strict=True))
        assert T.FONT_BODY == body, "what is read keeps its size"
        assert T.compact()
    finally:
        T.set_density(was)


# --------------------------------------------------------------------------
#  the one-window label
# --------------------------------------------------------------------------


def test_a_label_is_one_native_window(app):
    from rov_flight_ops.gui.widgets import Label

    lab = Label(app, text="hello", font=T.FONT_SMALL, text_color=T.TEXT_MUTED)
    try:
        assert isinstance(lab, tkinter.Label)
        assert lab.winfo_children() == []
        assert lab.cget("text") == "hello"
        assert lab.cget("font") == T.FONT_SMALL       # as given, like a CTkLabel
        assert lab.cget("text_color") == T.TEXT_MUTED
    finally:
        lab.destroy()


def test_a_label_told_what_it_already_shows_does_nothing(app, monkeypatch):
    from rov_flight_ops.gui.widgets import Label

    lab = Label(app, text="same", text_color=T.TEXT)
    calls = []
    real = tkinter.Label.configure

    def counting(self, cnf=None, **kw):
        calls.append(kw)
        return real(self, cnf, **kw)

    monkeypatch.setattr(tkinter.Label, "configure", counting)
    try:
        lab.configure(text="same", text_color=T.TEXT)
        assert calls == []
        lab.configure(text="different")
        assert calls and calls[-1] == {"text": "different"}
    finally:
        monkeypatch.undo()
        lab.destroy()


def test_a_label_scales_its_font_its_wrap_and_its_padding_like_customtkinter(app):
    from rov_flight_ops.gui.widgets import Label, _scaling_of

    s = _scaling_of(app)
    holder = tkinter.Frame(app)
    lab = Label(holder, text="x", font=("Consolas", 12), wraplength=200)
    try:
        lab.grid(row=0, column=0, padx=(10, 4), pady=6)
        drawn = lab.drawn_font()
        size = drawn[1] if isinstance(drawn, tuple) else app.tk.splitlist(drawn)[1]
        assert int(size) == -abs(round(12 * s))
        assert int(tkinter.Label.cget(lab, "wraplength")) == int(round(200 * s))
        assert lab.cget("wraplength") == 200          # logical, as given
        info = lab.grid_info()

        def ints(v):
            return [int(x) for x in (v if isinstance(v, (tuple, list))
                                     else str(v).split())]
        assert ints(info["padx"]) == [int(10 * s), int(4 * s)]
        assert ints(info["pady"]) == [int(6 * s)]
    finally:
        holder.destroy()


def test_a_label_follows_the_appearance_mode(app):
    from rov_flight_ops.gui.widgets import Label

    lab = Label(app, text="x", text_color=("#111111", "#EEEEEE"))
    try:
        lab._set_appearance_mode("Light")
        assert tkinter.Label.cget(lab, "fg").lower() == "#111111"
        lab._set_appearance_mode("Dark")
        assert tkinter.Label.cget(lab, "fg").lower() == "#eeeeee"
    finally:
        lab._set_appearance_mode("Dark" if app.mode == "dark" else "Light")
        lab.destroy()


def test_no_customtkinter_labels_are_left_in_the_window(app):
    """Every CTkLabel was three native windows. The only ones left are the
    caption CustomTkinter builds inside each scrollable frame for itself."""
    import customtkinter as ctk

    labels, internal = [], set()

    def walk(w):
        if isinstance(w, ctk.CTkLabel):
            labels.append(w)
        if isinstance(w, ctk.CTkScrollableFrame):
            internal.add(getattr(w, "_label", None))
        for c in w.winfo_children():
            walk(c)

    walk(app)
    for page in app.pages.values():          # scrollable frames are children
        if isinstance(page, ctk.CTkScrollableFrame):
            internal.add(getattr(page, "_label", None))
    stray = [w for w in labels if w not in internal]
    assert stray == [], [str(w) for w in stray]


# --------------------------------------------------------------------------
#  the strip charts
# --------------------------------------------------------------------------


def _chart_lines(page):
    cv = page.canvas
    return [i for i in cv.find_all() if cv.type(i) == "line"
            and float(cv.itemcget(i, "width")) > 1.5]


def test_the_strip_charts_draw_a_point_every_few_pixels_and_keep_a_spike(app):
    from rov_flight_ops.gui import monitorpage as MP

    page = app.pages["monitor"]
    cv = page.canvas
    cv.delete("all")
    s, x0, x1 = 1.5, 0.0, 1650.0
    n = 1800                                   # half an hour at 1 Hz
    times = [float(t) for t in range(n)]
    values = [50.0] * n
    values[1234] = 99.0                        # one second, way out of line
    levels = [None] * n

    page._plot(cv, times, values, levels, "cpu_usage_pct", 0.0,
               page._colors(), s, x0, x1, now=float(n - 1), span=float(n))
    lines = [i for i in cv.find_all() if cv.type(i) == "line"
             and cv.itemcget(i, "joinstyle") == "bevel"]
    assert lines, "the data line is not drawn with bevelled joins"
    points = sum(len(cv.coords(i)) // 2 for i in lines)
    budget = (x1 - x0) / (MP.PLOT_PX_PER_POINT * s) + 4
    assert points <= budget, (points, budget)
    ys = [cv.coords(i)[k] for i in lines for k in range(1, len(cv.coords(i)), 2)]
    assert len(set(round(y, 1) for y in ys)) >= 2, "the spike was decimated away"
    cv.delete("all")


def test_no_wide_line_on_any_chart_has_rounded_joins(app):
    """Rounded joins on a wide line are what made GDI's drawing so slow. (A
    line of two points has no join, so a gap marker may keep the default.)"""
    for key, attr in (("monitor", "canvas"), ("dvl", "charts")):
        cv = getattr(app.pages[key], attr)
        for i in cv.find_all():
            if (cv.type(i) == "line" and float(cv.itemcget(i, "width")) > 1.5
                    and len(cv.coords(i)) > 4):
                assert cv.itemcget(i, "joinstyle") != "round", (key, i)


# --------------------------------------------------------------------------
#  pages off screen
# --------------------------------------------------------------------------


def test_the_navigation_tick_draws_nothing_while_its_tab_is_not_open(app, monkeypatch):
    page = app.pages["navigation"]
    was = app.nav.current
    drawn = []
    monkeypatch.setattr(page, "_render", lambda: drawn.append(1))
    monkeypatch.setattr(page, "_render_ugps", lambda: None)   # starts the G2 reader
    monkeypatch.setattr(page, "after", lambda *_a, **_k: None)
    try:
        app.nav.select("Monitoring")
        page._tick()
        assert drawn == [], "Navigation redrew itself behind another tab"
        monkeypatch.setattr(page, "on_screen", lambda: True)
        page._tick()
        assert drawn == [1]
    finally:
        monkeypatch.undo()
        app.nav.select(was)


def test_off_screen_the_navigation_tick_still_follows_the_line(app, monkeypatch):
    """The guidance accumulates the line's progress for the navigation log, so
    it is kept up to date behind other tabs even though nothing is drawn."""
    page = app.pages["navigation"]
    was = app.nav.current
    followed = []
    monkeypatch.setattr(page, "_render_guidance", lambda s, now: followed.append(now))
    monkeypatch.setattr(page, "after", lambda *_a, **_k: None)
    monkeypatch.setattr(page, "following", ("feature", None))

    class _Collector:
        def snapshot(self):
            return object()

    monkeypatch.setattr(page, "collector", _Collector())
    try:
        app.nav.select("Monitoring")
        page._tick()
        assert len(followed) == 1
    finally:
        monkeypatch.undo()
        app.nav.select(was)


# --------------------------------------------------------------------------
#  CustomTkinter, corrected
# --------------------------------------------------------------------------


def test_configure_no_longer_reads_every_option_back(app, monkeypatch):
    """`CTkBaseClass.configure` handed Tk's frame an empty configure, which
    asks Tk for the whole configuration. It must not any more."""
    import customtkinter as ctk

    btn = ctk.CTkButton(app, text="a")
    empty = []
    real = tkinter.Frame.configure

    def watch(self, cnf=None, **kw):
        if cnf is None and not kw:
            empty.append(self)
        return real(self, cnf, **kw)

    monkeypatch.setattr(tkinter.Frame, "configure", watch)
    try:
        btn.configure(text="b")
        btn.configure(state="disabled")
        assert empty == []
        assert btn.cget("text") == "b"
    finally:
        monkeypatch.undo()
        btn.destroy()


def test_customtkinters_polls_run_once_a_second():
    import customtkinter as ctk

    from rov_flight_ops.gui import ctk_tuning

    assert ctk_tuning.apply() is True
    assert ctk.ScalingTracker.update_loop_interval >= ctk_tuning.POLL_MS
    assert ctk.AppearanceModeTracker.update_loop_interval >= ctk_tuning.POLL_MS


def test_the_root_window_configure_is_filtered_in_tcl(app):
    """So Python is called for the root window's own <Configure>, not once for
    every widget inside it that moves or changes size."""
    script = app.tk.call("bind", str(app), "<Configure>")
    assert '"%W" eq' in str(script)
