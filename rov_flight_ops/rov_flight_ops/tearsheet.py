"""
The flight, on paper, in the Aquarium's own identity.

One PDF per survey day, built from the logs the day already wrote. It exists
because the analysis after 11 September took an evening and produced something
a colleague could read in two minutes -- and the two minutes were the valuable
part. Everything in here is arithmetic that `flightreport` has already done;
this module only decides what a page should look like.

**The timeline is the page.** Three rows on one clock -- what was recording,
whether the tether was alive, which client was flying -- with the disarms
marked. Every question that took an evening to answer is legible from it in a
glance: five recordings is five disarms, the red under them is why, and the
unbroken hour after the client changed is the control.

Drawn with matplotlib into a vector PDF rather than an image, so the text is
selectable, the figure is sharp at any zoom, and the whole thing is one file a
person can e-mail. Montserrat is embedded from the fonts this application
already ships, so the sheet reads the same on a machine that has never
installed it.

Brand rules followed from SAQ-001 (v1, Aug 2023), via `brand`:
  * Montserrat throughout: ExtraBold display, Bold titles, Medium and SemiBold
    headers, Regular body. Barlow Condensed Bold is the guideline's eyebrow
    face; where it is not installed, Montserrat SemiBold in caps with wide
    tracking stands in rather than substituting an unrelated family.
  * Body copy on White is Stone Gray, always.
  * Salish, Fathom and Mediterranean lead; Algae, Seafoam, Coral and Purple
    Star accent. No tints of anything but Stone.
  * Severity uses Coral, not red: the palette has a warm accent and the
    guidelines ask that the brand's own colours do the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import brand
from . import flightreport as R
from . import flightscan as S

# --------------------------------------------------------------------------
#  the page
# --------------------------------------------------------------------------

#: US Letter landscape, in inches. Landscape because the timeline is the
#: page's reason to exist and an afternoon does not fit across a portrait
#: measure without becoming unreadable.
PAGE_W, PAGE_H = 11.0, 8.5

#: Margins, in figure fractions. Generous: the guidelines ask for open space,
#: and a sheet read on a boat needs it more than a sheet read at a desk.
LEFT, RIGHT = 0.055, 0.945
TOP, BOTTOM = 0.945, 0.055

#: Severity to colour. Coral for the things that stopped the survey, Seafoam
#: for the things worth reading, Algae for the things that went right.
LEVEL_COLOUR = {
    R.CRITICAL: brand.CORAL,
    R.WARNING: brand.SEAFOAM,
    R.NOTE: brand.MEDITERRANEAN,
    R.GOOD: brand.ALGAE,
}
LEVEL_WORD = {R.CRITICAL: "STOPPED THE SURVEY", R.WARNING: "WORTH READING",
              R.NOTE: "FOR THE RECORD", R.GOOD: "WENT WELL"}


def _fonts() -> dict:
    """Register the bundled faces and say which names came back.

    Returns the family names to use for each role. A machine missing a weight
    falls back within Montserrat rather than to a different family, so the
    sheet degrades in weight and never in identity.
    """
    names = {"display": "Montserrat", "body": "Montserrat",
             "eyebrow": "Montserrat", "eyebrow_weight": "semibold"}
    try:
        import matplotlib.font_manager as fm  # noqa: PLC0415
        found = set()
        for path in sorted(Path(brand._BUNDLED_FONT_DIR).glob("*.ttf")):
            try:
                fm.fontManager.addfont(str(path))
                found.add(fm.FontProperties(fname=str(path)).get_name())
            except Exception:
                continue
        # Barlow Condensed Bold is the guideline's eyebrow face. Drop its TTF
        # into assets/fonts and it is used; until then Montserrat SemiBold in
        # caps with wide tracking carries the role.
        for family in fm.fontManager.ttflist:
            if "Barlow Condensed" in family.name:
                names["eyebrow"] = family.name
                names["eyebrow_weight"] = "bold"
                break
        if "Montserrat" not in found:
            names["display"] = names["body"] = names["eyebrow"] = "DejaVu Sans"
    except Exception:                                 # pragma: no cover
        pass
    return names


def _hm(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%H:%M")


def _hms(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%H:%M:%S")


@dataclass
class Sheet:
    """One rendered tear-sheet and what it cost."""

    path: Path
    pages: int = 0
    seconds: float = 0.0


# --------------------------------------------------------------------------
#  drawing helpers
# --------------------------------------------------------------------------


class _Pen:
    """Text and rules in figure coordinates, in the brand's type hierarchy.

    Figure coordinates rather than axes throughout: a tear-sheet is a laid-out
    page, not a plot with annotations, and mixing the two coordinate systems
    is how a layout ends up depending on a chart's data limits.
    """

    def __init__(self, fig, fonts: dict):
        self.fig = fig
        self.f = fonts

    def display(self, x, y, text, colour=brand.SALISH, size=26, **kw):
        return self.fig.text(x, y, text.upper(), color=colour, size=size,
                             family=self.f["display"], weight="extra bold",
                             va="top", **kw)

    def title(self, x, y, text, colour=brand.SALISH, size=15, **kw):
        return self.fig.text(x, y, text, color=colour, size=size,
                             family=self.f["display"], weight="bold",
                             va="top", **kw)

    def header(self, x, y, text, colour=brand.SALISH, size=11, **kw):
        return self.fig.text(x, y, text, color=colour, size=size,
                             family=self.f["body"], weight="semibold",
                             va="top", **kw)

    def eyebrow(self, x, y, text, colour=brand.MEDITERRANEAN, size=7.5, **kw):
        return self.fig.text(x, y, text.upper(), color=colour, size=size,
                             family=self.f["eyebrow"],
                             weight=self.f["eyebrow_weight"],
                             va="top", **kw)

    def body(self, x, y, text, colour=brand.STONE, size=8.2,
             weight="regular", **kw):
        return self.fig.text(x, y, text, color=colour, size=size,
                             family=self.f["body"], weight=weight,
                             va="top", linespacing=1.45, **kw)

    def mono(self, x, y, text, colour=brand.STONE, size=7.4, **kw):
        return self.fig.text(x, y, text, color=colour, size=size,
                             family="DejaVu Sans Mono", va="top",
                             linespacing=1.5, **kw)

    def rule(self, x0, x1, y, colour=brand.STONE_TINTS[20], width=0.8):
        line = self.fig.add_artist(
            __import__("matplotlib.lines", fromlist=["Line2D"]).Line2D(
                [x0, x1], [y, y], transform=self.fig.transFigure,
                color=colour, linewidth=width))
        return line

    def measure(self, artist) -> float:
        """How much page a drawn artist actually took.

        The only honest way to stack text blocks: ask the renderer what it
        just drew. Everything above this in the file positions by measurement
        rather than by predicting, because the prediction was wrong by a fifth
        and the error only shows up as overlapping paragraphs on page two.
        """
        try:
            renderer = self.fig.canvas.get_renderer()
        except AttributeError:                        # pragma: no cover
            self.fig.canvas.draw()
            renderer = self.fig.canvas.get_renderer()
        try:
            box = artist.get_window_extent(renderer=renderer)
        except Exception:                             # pragma: no cover
            return 0.0
        return box.height / self.fig.bbox.height

    def panel(self, x0, y0, x1, y1, colour=brand.PUMICE, alpha=1.0, z=0):
        import matplotlib.patches as mp  # noqa: PLC0415
        self.fig.add_artist(mp.Rectangle(
            (x0, y0), x1 - x0, y1 - y0, transform=self.fig.transFigure,
            facecolor=colour, edgecolor="none", alpha=alpha, zorder=z))


def _wrap(text: str, width: int) -> str:
    import textwrap  # noqa: PLC0415
    return "\n".join(textwrap.wrap(text, width)) if text else ""


def line_height(size_pt: float, spacing: float = 1.45) -> float:
    """One line of type, as a fraction of the page height -- an estimate.

    Used only to decide whether a block is *worth starting*. It is not what
    the block is laid out from, because the arithmetic is wrong in a way that
    is invisible until it is not: a rendered line occupies its font's full
    ascent and descent, not its point size times its leading, which on
    Montserrat at 7.6 pt is 13.4 pt rather than 11.0 -- a fifth more, and it
    compounds down a column until the text is on top of itself.

    `_Pen.measure` is what the layout actually uses. This is the cheap guess
    that avoids drawing something only to find it did not fit.
    """
    return size_pt * spacing * 1.22 / 72.0 / PAGE_H


def _block_height(text: str, size_pt: float, width: int,
                  spacing: float = 1.45) -> tuple[str, float]:
    """Wrapped text, and roughly how much page it will take."""
    wrapped = _wrap(text, width)
    lines = 1 + wrapped.count("\n") if wrapped else 0
    return wrapped, lines * line_height(size_pt, spacing)


# --------------------------------------------------------------------------
#  page 1 -- the timeline and the verdict
# --------------------------------------------------------------------------


def _masthead(pen: _Pen, report: R.DayReport, page: str) -> None:
    day = report.day
    site = day.site or "ROV survey"
    when = datetime.fromtimestamp(day.start or 0, timezone.utc)
    pen.panel(0, 0.955, 1, 1, brand.SALISH)
    pen.eyebrow(LEFT, 0.992, "Seattle Aquarium  ·  Coastal Climate Resilience",
                brand.WHITE, size=7.2)
    pen.eyebrow(RIGHT, 0.992, page, brand.SEAFOAM, size=7.2, ha="right")
    pen.display(LEFT, 0.925, "Flight report", brand.SALISH, size=27)
    # Spelled out rather than with a strftime day modifier: `%-d` is a POSIX
    # extension that Windows rejects outright, and `%#d` is the Windows one.
    # A sheet that will not render on the field laptop is no sheet at all.
    pen.header(LEFT, 0.868,
               f"{site}   ·   {when:%A} {when.day} {when:%B %Y}",
               brand.MEDITERRANEAN, size=11.5)
    pen.body(RIGHT, 0.868, report.headline, brand.STONE, size=8.4, ha="right")


def _verdict_strip(pen: _Pen, report: R.DayReport, y: float) -> float:
    """The four counts that decide whether anybody reads the rest."""
    day = report.day
    failsafes = sum(1 for d in report.disarms if d.cause == "gcs failsafe")
    outages = [o for o in report.outages if o.seconds >= 2.0]
    lost = sum(o.seconds for o in outages)
    monitored = day.monitored_seconds or 1.0
    tiles = [
        ("Armed", R._dur(day.recorded_seconds), brand.SALISH),
        ("Recorded", f"{day.bytes_recorded / 2 ** 30:.1f} GiB", brand.SALISH),
        ("Failsafe disarms", str(failsafes),
         brand.CORAL if failsafes else brand.ALGAE),
        ("Link available",
         f"{100 * (1 - lost / monitored):.0f}%",
         brand.CORAL if lost / monitored > 0.05 else brand.ALGAE),
    ]
    width = (RIGHT - LEFT) / len(tiles)
    for i, (label, value, colour) in enumerate(tiles):
        x = LEFT + i * width
        pen.eyebrow(x, y, label, brand.STONE, size=7)
        pen.fig.text(x, y - 0.018, value, color=colour, size=19,
                     family=pen.f["display"], weight="bold", va="top")
    return y - 0.072


def _timeline(fig, pen: _Pen, report: R.DayReport, rect) -> None:
    """The afternoon: what recorded, what the tether did, who was flying."""
    import matplotlib.patches as mp  # noqa: PLC0415

    day = report.day
    t0, t1 = day.start, day.end
    if not t1 or t1 <= t0:
        return
    ax = fig.add_axes(rect)
    ax.set_xlim(t0, t1)
    ax.set_ylim(0, 3.35)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(brand.STONE_TINTS[20])
    ax.set_yticks([])
    ax.tick_params(axis="x", colors=brand.STONE, labelsize=7.2, length=3,
                   width=0.8, color=brand.STONE_TINTS[20])
    for label in ax.get_xticklabels():
        label.set_family(pen.f["body"])

    step = 600 if (t1 - t0) < 9000 else 1800
    first = t0 - (t0 % step) + step
    ticks = []
    while first < t1:
        ticks.append(first)
        first += step
    ax.set_xticks(ticks)
    ax.set_xticklabels([_hm(t) for t in ticks])

    rows = {"Recording": 2.4, "Tether link": 1.5, "Flown from": 0.6}
    height = 0.52
    for name, y in rows.items():
        ax.text(t0 - (t1 - t0) * 0.012, y + height / 2, name, ha="right",
                va="center", size=8, color=brand.SALISH,
                family=pen.f["body"], weight="semibold")
        ax.axhline(y - 0.06, color=brand.STONE_TINTS[10], lw=0.7, zorder=0)

    # ---- recordings ---------------------------------------------------
    for recording in day.recordings:
        if not recording.end:
            continue
        ax.add_patch(mp.Rectangle(
            (recording.start, rows["Recording"]), recording.seconds, height,
            facecolor=brand.SALISH, edgecolor="none", zorder=3))
    longest = max(day.recordings, key=lambda r: r.seconds, default=None)
    if longest is not None and longest.seconds > (t1 - t0) * 0.25:
        ax.text(longest.start + longest.seconds / 2,
                rows["Recording"] + height + 0.12,
                f"{longest.size_bytes / 2 ** 30:.1f} GiB · "
                f"{longest.seconds / 60:.0f} min · uninterrupted",
                ha="center", va="bottom", size=7, color=brand.STONE,
                family=pen.f["body"])

    # ---- the tether ---------------------------------------------------
    y = rows["Tether link"]
    monitored = [(m.start, m.end) for m in day.monitors]
    if monitored:
        ax.add_patch(mp.Rectangle(
            (t0, y), t1 - t0, height, facecolor=brand.STONE_TINTS[10],
            edgecolor="none", zorder=1))
        for start, end in monitored:
            ax.add_patch(mp.Rectangle(
                (start, y), end - start, height, facecolor=brand.ALGAE,
                alpha=0.55, edgecolor="none", zorder=2))
        for outage in report.outages:
            if outage.seconds < 1.0:
                continue
            ax.add_patch(mp.Rectangle(
                (outage.start, y), max(outage.seconds, (t1 - t0) * 0.0015),
                height, facecolor=brand.CORAL, edgecolor="none", zorder=4))
        holes = _unmonitored(t0, t1, monitored)
        for start, end in holes:
            if end - start < (t1 - t0) * 0.02:
                continue
            ax.text((start + end) / 2, y + height / 2,
                    "not monitored", ha="center", va="center", size=6.6,
                    color=brand.STONE, family=pen.f["body"], zorder=5)

    # ---- who was flying ------------------------------------------------
    y = rows["Flown from"]
    for start, end, gcs in R._gcs_intervals(day):
        name = R.GCS_NAMES.get(gcs.split("/")[-1], gcs)
        colour = brand.MEDITERRANEAN if "Cockpit" in name else brand.SEAFOAM
        ax.add_patch(mp.Rectangle(
            (start, y), end - start, height, facecolor=colour, alpha=0.30,
            edgecolor="none", zorder=2))
        if end - start > (t1 - t0) * 0.08:
            ax.text((start + end) / 2, y + height / 2, name, ha="center",
                    va="center", size=7.6, color=brand.SALISH,
                    family=pen.f["body"], weight="semibold", zorder=3)

    # ---- the disarms ---------------------------------------------------
    failsafes = [d for d in report.disarms if d.cause == "gcs failsafe"]
    for i, disarm in enumerate(failsafes, start=1):
        ax.axvline(disarm.when, ymin=0.02, ymax=0.90, color=brand.CORAL,
                   lw=0.9, ls=(0, (2, 2)), zorder=6)
        ax.plot([disarm.when], [3.16], marker="o", markersize=9,
                markerfacecolor=brand.CORAL, markeredgecolor="none", zorder=7)
        ax.text(disarm.when, 3.16, str(i), ha="center", va="center", size=6.2,
                color=brand.WHITE, family=pen.f["body"], weight="bold",
                zorder=8)
    if failsafes:
        ax.text(t0, 3.30, "DISARMS  ·  ground-station heartbeat lost",
                ha="left", va="bottom", size=6.8, color=brand.CORAL,
                family=pen.f["eyebrow"], weight=pen.f["eyebrow_weight"])

    for when in report.reboots:
        ax.axvline(when, ymin=0.0, ymax=0.20, color=brand.STONE, lw=1.1,
                   zorder=6)
        ax.text(when, 0.06, "reboot", ha="center", va="top", size=6,
                color=brand.STONE, family=pen.f["body"])


def _unmonitored(t0: float, t1: float, spans) -> list[tuple[float, float]]:
    out, cursor = [], t0
    for start, end in sorted(spans):
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < t1:
        out.append((cursor, t1))
    return out


def _legend(pen: _Pen, y: float) -> None:
    items = [("Recording (armed)", brand.SALISH),
             ("Tether alive", brand.ALGAE),
             ("Tether down", brand.CORAL),
             ("Not monitored", brand.STONE_TINTS[10])]
    x = LEFT
    for label, colour in items:
        pen.panel(x, y - 0.004, x + 0.016, y + 0.008, colour)
        pen.body(x + 0.021, y + 0.011, label, brand.STONE, size=7)
        x += 0.021 + 0.010 * len(label)


#: Characters per line at body size, for the two column widths used. Montserrat
#: is a wide face; a wrap tuned for a narrower one overflows the measure on
#: every long line, which is how the first version ran text off the page.
WRAP_FULL = 126
WRAP_HALF = 60

TITLE_PT, BODY_PT, MONO_PT = 9.4, 7.6, 6.6


#: Space under one finding before the next begins.
FINDING_GAP = 0.018


def _finding(pen: _Pen, finding, x: float, y: float, width: int,
             *, evidence: bool = False, floor: float | None = None):
    """Draw one finding. Returns (next y, drawn) -- drawn is False if it did
    not fit, in which case nothing was left on the page.

    Drawn and then measured rather than predicted and then drawn. When a
    block overruns the floor its artists are removed again, so a column
    always ends on a whole finding instead of on half of one.
    """
    colour = LEVEL_COLOUR.get(finding.level, brand.STONE)
    wrapped = _wrap(finding.detail, width)
    lines = finding.evidence[:4] if (evidence and finding.evidence) else []
    drawn = []

    title = pen.header(x + 0.012, y, finding.title, brand.SALISH,
                       size=TITLE_PT)
    drawn.append(title)
    cursor = y - pen.measure(title) - 0.002

    if wrapped:
        body = pen.body(x + 0.012, cursor, wrapped, brand.STONE, size=BODY_PT)
        drawn.append(body)
        cursor -= pen.measure(body)
    if lines:
        mono = pen.mono(x + 0.012, cursor - 0.003, "\n".join(lines),
                        brand.STONE_TINTS[40], size=MONO_PT)
        drawn.append(mono)
        cursor -= pen.measure(mono) + 0.003

    if floor is not None and cursor < floor:
        for artist in drawn:
            artist.remove()
        return y, False

    pen.panel(x, cursor + 0.004, x + 0.0032, y + 0.008, colour)
    return cursor - FINDING_GAP, True


def _findings_block(pen: _Pen, report: R.DayReport, y: float,
                    floor: float = 0.075, limit: int = 6) -> float:
    pen.eyebrow(LEFT, y, "What the logs say")
    y -= 0.032
    for finding in report.sorted_findings[:limit]:
        y, drawn = _finding(pen, finding, LEFT, y, WRAP_FULL, floor=floor)
        if not drawn:
            break
    return y


def render_page_one(fig, pen: _Pen, report: R.DayReport) -> None:
    _masthead(pen, report, "The day")
    _verdict_strip(pen, report, 0.815)
    pen.eyebrow(LEFT, 0.735, "The afternoon, on one clock")
    _timeline(fig, pen, report,
              [LEFT + 0.075, 0.455, RIGHT - LEFT - 0.075, 0.235])
    _legend(pen, 0.418)
    pen.rule(LEFT, RIGHT, 0.398)
    _findings_block(pen, report, 0.378, floor=0.070, limit=6)


def render_page_findings(fig, pen: _Pen, report: R.DayReport,
                         start: int = 0, shown_levels=None,
                         page_label: str = "") -> int:
    """Everything the logs say, with what each was worked out from.

    Two columns, because a finding's evidence is a handful of timestamps and a
    full-measure line for those wastes most of the page. Ordered by weight, so
    a reader who stops halfway has stopped in the right place.

    Returns the index of the first finding it could not fit, so the caller can
    open another page and carry on. A day with a lot to say gets the pages it
    needs rather than a truncation notice -- the findings *are* the report,
    and dropping the quiet ones to save paper drops exactly the observations
    nobody thought to look for.
    """
    _masthead(pen, report, page_label or "What the logs say")
    gap = 0.05
    col_w = (RIGHT - LEFT - gap) / 2
    columns = [(LEFT, 0.83), (LEFT + col_w + gap, 0.83)]

    floor = BOTTOM + 0.030
    if shown_levels is None:
        shown_levels = set()
    index = start
    for x, top in columns:
        y = top
        while index < len(report.sorted_findings):
            finding = report.sorted_findings[index]
            heading = None
            if finding.level not in shown_levels:
                if y - 0.09 < floor:
                    break
                heading = pen.eyebrow(
                    x, y, LEVEL_WORD.get(finding.level, finding.level),
                    LEVEL_COLOUR.get(finding.level, brand.STONE))
                y -= 0.028
            y, drawn = _finding(pen, finding, x, y, WRAP_HALF,
                                evidence=True, floor=floor)
            if not drawn:
                # The heading belongs to the finding under it. If that one
                # went to the next column, the heading goes with it.
                if heading is not None:
                    heading.remove()
                    y += 0.028
                break
            if heading is not None:
                shown_levels.add(finding.level)
            index += 1
    return index


# --------------------------------------------------------------------------
#  page 2 -- the systems, one by one
# --------------------------------------------------------------------------


def _series_panel(fig, pen, report, rect, columns, title, unit) -> None:
    """One small multiple: a reading over the day, with the outages behind it."""
    day = report.day
    ax = fig.add_axes(rect)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(brand.STONE_TINTS[20])
    ax.tick_params(colors=brand.STONE, labelsize=6.2, length=2, width=0.7)
    for label in list(ax.get_xticklabels()) + list(ax.get_yticklabels()):
        label.set_family(pen.f["body"])
    ax.set_title(title, size=8, color=brand.SALISH, family=pen.f["body"],
                 weight="semibold", loc="left", pad=7)

    colours = (brand.MEDITERRANEAN, brand.ALGAE, brand.PURPLE_STAR)
    drawn = False
    for i, column in enumerate(columns):
        xs, ys = [], []
        for session in day.monitors:
            t, v = session.series(column)
            if not t:
                continue
            xs.extend(t)
            ys.extend(v)
            xs.append(None)
            ys.append(None)
        if not xs:
            continue
        clean_x = [x for x in xs if x is not None]
        if not clean_x:
            continue
        ax.plot(xs, ys, lw=0.9, color=colours[i % len(colours)],
                label=column, solid_joinstyle="round")
        drawn = True
    if not drawn:
        ax.text(0.5, 0.5, "not recorded on this flight", ha="center",
                va="center", transform=ax.transAxes, size=7,
                color=brand.STONE_TINTS[40], family=pen.f["body"])
        ax.set_xticks([])
        ax.set_yticks([])
        return

    ax.set_xlim(day.start, day.end)
    for outage in report.outages:
        if outage.seconds < 2:
            continue
        ax.axvspan(outage.start, outage.end, color=brand.CORAL, alpha=0.18,
                   lw=0, zorder=0)
    step = 1800 if (day.end - day.start) > 3600 else 900
    first = day.start - (day.start % step) + step
    ticks = []
    while first < day.end:
        ticks.append(first)
        first += step
    ax.set_xticks(ticks)
    ax.set_xticklabels([_hm(t) for t in ticks])
    ax.set_ylabel(unit, size=6.4, color=brand.STONE, family=pen.f["body"])
    if len(columns) > 1:
        legend = ax.legend(fontsize=5.8, frameon=False, loc="upper right",
                           ncol=len(columns), handlelength=1.2,
                           borderaxespad=0.1)
        for text in legend.get_texts():
            text.set_color(brand.STONE)
            text.set_family(pen.f["body"])


def _table(pen: _Pen, x: float, y: float, rows, width_chars=46) -> float:
    for label, value in rows:
        pen.body(x, y, str(label), brand.STONE, size=7.4)
        pen.body(x + 0.20, y, str(value), brand.SALISH, size=7.4,
                 weight="semibold")
        y -= 0.0165
    return y


def render_page_two(fig, pen: _Pen, report: R.DayReport) -> None:
    _masthead(pen, report, "The systems")
    day = report.day
    top = 0.83

    # ---- the ground stations, if there were two -------------------------
    pen.eyebrow(LEFT, top, "Ground stations, compared")
    y = top - 0.026
    if len(report.gcs) >= 2:
        width = (RIGHT - LEFT) / len(report.gcs)
        for i, profile in enumerate(report.gcs):
            x = LEFT + i * width
            good = (profile.failsafes == 0)
            pen.header(x, y, profile.name,
                       brand.ALGAE if good else brand.CORAL, size=10)
            rows = [
                ("Recordings", profile.recordings),
                ("Armed", R._dur(profile.seconds)),
                ("Link available",
                 f"{profile.availability_pct:.1f}%"
                 if profile.availability_pct is not None else "—"),
                ("Failsafe disarms", profile.failsafes),
                ("Mean receive",
                 f"{profile.mean_rx_mbps:.1f} Mbps"
                 if profile.mean_rx_mbps else "—"),
            ]
            yy = y - 0.020
            for label, value in rows:
                pen.body(x, yy, str(label), brand.STONE, size=7.4)
                pen.body(x + 0.115, yy, str(value), brand.SALISH, size=7.4,
                         weight="semibold")
                yy -= 0.0165
        y = yy - 0.012
    else:
        only = report.gcs[0].name if report.gcs else "not identified"
        pen.body(LEFT, y, f"Flown from {only} throughout — nothing to compare.",
                 brand.STONE, size=7.8)
        y -= 0.030

    pen.rule(LEFT, RIGHT, y)
    y -= 0.020

    # ---- the small multiples --------------------------------------------
    pen.eyebrow(LEFT, y, "The systems, through the day  ·  "
                         "shaded where the tether was down")
    plot_top = y - 0.035
    plot_h = 0.115
    gap_x = 0.045
    col_w = (RIGHT - LEFT - gap_x) / 2
    panels = [
        (["network_receive_mbps"], "Tether throughput", "Mbps"),
        (["rov_ping_latency_ms"], "Round trip to the vehicle", "ms"),
        (["cpu_usage_pct", "gpu_usage_pct"], "Laptop load", "%"),
        (["pi_soc_temp_c", "motherboard_temp_c"], "Temperatures", "°C"),
    ]
    for i, (columns, title, unit) in enumerate(panels):
        row, col = divmod(i, 2)
        rect = [LEFT + col * (col_w + gap_x),
                plot_top - plot_h - row * (plot_h + 0.075),
                col_w, plot_h]
        _series_panel(fig, pen, report, rect, columns, title, unit)

    y = plot_top - 2 * (plot_h + 0.075) - 0.010
    pen.rule(LEFT, RIGHT, y)
    y -= 0.020

    # ---- the record ------------------------------------------------------
    col_w = (RIGHT - LEFT) / 3
    pen.eyebrow(LEFT, y, "Vehicle")
    versions = report.vehicle.get("versions") or {}
    rows = [("BlueOS", versions.get("blueos") or "—"),
            ("ArduSub", f"{versions.get('ardusub') or '—'} "
                        f"{versions.get('ardusub_type') or ''}".strip()),
            ("Board", versions.get("board") or "—"),
            ("Pi peak temperature",
             f"{(report.vehicle.get('soc_temp_c') or {}).get('max', 0):.0f} °C"
             if report.vehicle.get("soc_temp_c") else "—"),
            ("Extensions", len(versions.get("extensions") or []) or "—")]
    _table(pen, LEFT, y - 0.022, rows)

    pen.eyebrow(LEFT + col_w, y, "Parameters")
    params = report.parameters
    operator = params.get("operator_changes") or {}
    rows = [("Recorded", params.get("count", 0) or "—"),
            ("Changed by hand", len(operator)),
            ("Autopilot bookkeeping",
             len(params.get("autopilot_changes") or {})),
            ("Snapshots that failed",
             len(params.get("failed_reads") or []) or "none"),
            ("Software changes",
             len(params.get("version_changes") or {}) or "none")]
    _table(pen, LEFT + col_w, y - 0.022, rows)

    pen.eyebrow(LEFT + 2 * col_w, y, "Topside")
    stats = report.topside.get("stats", {})
    def _s(name, key="p95", fmt="{:.0f}"):
        stat = stats.get(name)
        return fmt.format(stat[key]) if stat else "—"
    rows = [("CPU, 95th percentile", _s("cpu_usage_pct") + " %"),
            ("Memory free, lowest",
             _s("ram_available_gb", "min", "{:.1f}") + " GB"),
            ("Adapter errors",
             f"{report.topside.get('nic_errors', 0):.0f}"),
            ("Power source",
             ", ".join(report.topside.get("power") or []) or
             ("battery" if report.topside.get("on_battery") else "—")),
            ("Monitored", R._dur(day.monitored_seconds))]
    _table(pen, LEFT + 2 * col_w, y - 0.022, rows)

    # ---- footer ----------------------------------------------------------
    pen.rule(LEFT, RIGHT, BOTTOM + 0.022)
    pen.body(LEFT, BOTTOM + 0.014,
             f"Built by Underwater Telemetry Compositing from "
             f"{len(day.recordings)} recording(s), "
             f"{len(day.monitors)} topside session(s) and "
             f"{len(day.pings)} ping log(s) in {day.folder.name}.",
             brand.STONE_TINTS[40], size=6.4)
    pen.body(RIGHT, BOTTOM + 0.014,
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             brand.STONE_TINTS[40], size=6.4, ha="right")


# --------------------------------------------------------------------------
#  the whole sheet
# --------------------------------------------------------------------------


def build(report: R.DayReport, path: Path | None = None) -> Sheet:
    """Render the tear-sheet for one analysed day.

    Written next to the logs it came from unless told otherwise, because the
    sheet travelling with the flight folder is the point -- a PDF in a
    downloads folder is separated from its evidence the moment it is moved.
    """
    import time  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from matplotlib.backends.backend_pdf import PdfPages  # noqa: PLC0415

    started = time.monotonic()
    fonts = _fonts()
    day = report.day
    if path is None:
        stem = (day.flight_dir.name if day.flight_dir else day.folder.name)
        path = day.folder / f"flight_report_{stem}.pdf"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def page():
        fig = plt.figure(figsize=(PAGE_W, PAGE_H), dpi=200)
        fig.patch.set_facecolor(brand.WHITE)
        return fig, _Pen(fig, fonts)

    def keep(fig):
        pdf.savefig(fig, facecolor=brand.WHITE)
        plt.close(fig)

    with PdfPages(path) as pdf:
        pages = 0
        fig, pen = page()
        try:
            render_page_one(fig, pen, report)
        finally:
            keep(fig)
            pages += 1

        # As many findings pages as the findings need. Bounded only against a
        # pathological report that cannot place anything, which would
        # otherwise spin forever writing empty pages.
        index, seen, guard = 0, set(), 0
        total = len(report.sorted_findings)
        while index < total and guard < 12:
            fig, pen = page()
            try:
                moved = render_page_findings(
                    fig, pen, report, index, seen,
                    f"What the logs say  ·  {index + 1}–{total} of {total}")
            finally:
                keep(fig)
                pages += 1
            if moved == index:
                break
            index = moved
            guard += 1

        fig, pen = page()
        try:
            render_page_two(fig, pen, report)
        finally:
            keep(fig)
            pages += 1
        info = pdf.infodict()
        info["Title"] = f"ROV flight report — {day.site or day.folder.name}"
        info["Author"] = "Seattle Aquarium · Coastal Climate Resilience"
        info["Subject"] = report.headline
        info["Creator"] = "Underwater Telemetry Compositing"
    return Sheet(path=path, pages=pages,
                 seconds=round(time.monotonic() - started, 2))


def build_for(folder: Path, *, progress=None, path: Path | None = None) -> Sheet:
    """Scan, analyse and render in one call. The whole tool, from a folder."""
    day = S.scan(Path(folder), progress=progress)
    if progress:
        progress(0.97, "Working out what it means")
    report = R.analyse(day)
    if progress:
        progress(0.99, "Drawing the sheet")
    return build(report, path)
