"""Turn a ``Conditions`` into the compiled PDF flight plan.

figures -> Jinja2 -> pdflatex. The LaTeX preamble mirrors
docs/ROV_field_tracking/CCR_ROV_field_log.tex (montserrat, the SAQ palette,
tcolorbox), so it is already known to build on the team's MiKTeX.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import config, contacts as contacts_mod
from .conditions import Conditions
from .figures.seastate import plot_sea_state
from .figures.tide import plot_tide
from .figures.windmap import plot_wind_map
from .sources.geo import compass_point

TEMPLATE = "flight_plan.tex.j2"
_LOGO = config.LATEX_DIR / "assets" / "SAQ_PrimaryLogo_MedBlue.pdf"

_TEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


_TEX_UNICODE = {
    "–": "--", "—": "---", "°": r"$^\circ$", "·": r"$\cdot$", "→": r"$\rightarrow$",
    "’": "'", "‘": "'", "“": "``", "”": "''", " ": "~", "–": "--",
}


def tex_escape(s) -> str:
    if s is None:
        return ""
    out = str(s).translate({ord(k): v for k, v in _TEX_SPECIAL.items()})
    for k, v in _TEX_UNICODE.items():
        out = out.replace(k, v)
    return out


@dataclass
class Meta:
    """Free-text fields the GUI/CLI supply, not scraped."""

    pilot: str = ""
    tender: str = ""
    other_personnel: str = ""
    rov: str = ""
    vessel: str = ""
    deployment: str = ""
    objective: str = ""
    sequencing: list[str] = field(default_factory=list)
    hazards: list[str] = field(default_factory=list)
    notes: str = ""


def _env() -> Environment:
    env = Environment(
        block_start_string="((*", block_end_string="*))",
        variable_start_string="(((", variable_end_string=")))",
        comment_start_string="((#", comment_end_string="#))",
        trim_blocks=True, lstrip_blocks=True, autoescape=False,
        undefined=StrictUndefined,
        loader=FileSystemLoader(str(config.LATEX_DIR)),
    )
    env.filters["tex"] = tex_escape
    return env


def _alert_what(description: str) -> str:
    m = re.search(r"\*\s*WHAT\.*\s*(.+?)(?:\n\s*\n|\Z)", description, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _glance_rows(c: Conditions) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    t = c.tide
    if t:
        lo, hi = t.range_ft()
        rows.append(("Tide range", f"{lo:.1f}--{hi:.1f} ft {t.datum}"))
        hs, he = t.height_at(c.flight_window.start), t.height_at(c.flight_window.end)
        st = t.state_at(c.flight_window.mid)
        if hs is not None and he is not None:
            rows.append(("Tide @ flight", f"{hs:.1f} $\\rightarrow$ {he:.1f} ft ({st})"))
        hilo = [e for e in t.extremes if e.time.date() == c.day]
        if hilo:
            rows.append(("Highs / lows",
                         " \\, ".join(f"{e.kind}\\,{e.height_ft:.1f}\\,ft\\,{e.time:%H:%M}"
                                      for e in hilo)))
    s = c.sun
    if s and s.sunrise and s.sunset:
        rows.append(("Sun", f"up {s.sunrise:%H:%M} / down {s.sunset:%H:%M}"))

    w = c.wind_at_flight()
    if w and w.wind_kt is not None:
        d = f" from {compass_point(w.wind_dir)}" if w.wind_dir is not None else ""
        g = f", gust {w.gust_kt:.0f}" if w.gust_kt else ""
        rows.append(("Wind @ flight", f"{w.wind_kt:.0f} kt{d}{g}"))
    if c.weather:
        tr = c.weather.temp_range_f()
        if tr:
            rows.append(("Air temp", f"{tr[0]:.0f}--{tr[1]:.0f} $^\\circ$F"))
        rows.append(("Sky", c.weather.sky_summary()))
        pop = c.weather.max_pop()
        if pop is not None:
            rows.append(("Precip. chance", f"{pop:.0f}\\%"))

    wv = c.waves_at_flight()
    if wv and wv.wave_ft is not None:
        extra = ""
        if wv.swell_ft and wv.swell_dir_deg is not None:
            per = f" at {wv.swell_period_s:.0f} s" if wv.swell_period_s else ""
            extra = f"; swell {wv.swell_ft:.1f} ft{per} from {compass_point(wv.swell_dir_deg)}"
        rows.append(("Sea @ flight", f"Hs {wv.wave_ft:.1f} ft{extra}"))
    b = c.waves.buoy if c.waves else None
    if b and b.wvht_ft and b.distance_km < 75:
        per = f" at {b.dom_period_s:.0f} s" if b.dom_period_s else ""
        rows.append((f"Buoy {b.station_id.upper()}",
                     f"Hs {b.wvht_ft:.1f} ft{per} ({b.distance_km:.0f} km)"))
    return rows


def _context(c: Conditions, meta: Meta, contacts: dict) -> dict:
    ser = []
    for a in c.serious_alerts:
        until = f"until {a.ends:%a %H:%M}" if a.ends else "in effect"
        ser.append({"event": a.event, "until": until,
                    "what": _alert_what(a.description) or a.headline,
                    "sender": a.sender})
    other = [{"event": a.event, "until": f"until {a.ends:%a %H:%M}" if a.ends else ""}
             for a in c.alerts if not a.serious]

    marine = None
    if c.marine and (c.marine.synopsis or c.marine.periods):
        marine = {
            "zone": c.marine.zone_id, "area": c.marine.area,
            "product": c.marine.product_type,
            "synopsis": c.marine.synopsis,
            "advisory": c.marine.advisory,
            "periods": [{"name": n, "text": tx} for n, tx in c.marine.periods],
        }

    sources = []
    if c.tide:
        sources.append(c.tide.source)
    if c.weather:
        sources.append(c.weather.source)
    if c.waves:
        sources.append(c.waves.source)
    if c.wind_field:
        sources.append(c.wind_field.source)
    if c.alerts is not None:
        sources.append("NWS active alerts (api.weather.gov)")
    if c.sun:
        sources.append("Sun times: astral (computed)")

    rows = _glance_rows(c)
    half = (len(rows) + 1) // 2
    tzab = c.float_window.start.tzname() or c.tz.split("/")[-1].replace("_", " ")
    return {
        "tz_abbr": tzab,
        "glance_left": rows[:half],
        "glance_right": rows[half:],
        "site_name": c.site.name,
        "coords": f"{c.site.lat:.4f}, {c.site.lon:.4f}",
        "date_long": f"{c.day:%A %d %B %Y}",
        "float_window": c.float_window.label(),
        "flight_window": c.flight_window.label(),
        "meta": meta,
        "serious_alerts": ser,
        "other_alerts": other,
        "marine": marine,
        "marine_zone_name": c.weather.marine_zone_name if c.weather else "",
        "figures": {},          # filled by render_flight_plan
        "contacts": contacts,
        "sources": sources,
        "retrieved": c.retrieved_at.strftime("%Y-%m-%d %H:%M %Z") or
                     c.retrieved_at.strftime("%Y-%m-%d %H:%M"),
        "stale": c.stale,
        "warnings": c.warnings,
        "logo": "SAQ_PrimaryLogo_MedBlue.pdf",
    }


def _publish_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copyfile(src, tmp)
    for attempt in range(6):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            if attempt == 5:
                alt = dst.with_name(f"{dst.stem} (new){dst.suffix}")
                tmp.replace(alt)
                raise RuntimeError(
                    f"{dst.name} is open in another program (Dropbox, a PDF "
                    f"viewer?). Wrote {alt.name} instead.")
            time.sleep(1.0 * (attempt + 1))


def render_flight_plan(c: Conditions, *, out_pdf: Path | str, theme: str = "light",
                       meta: Meta | None = None, contacts: dict | None = None,
                       keep_tex: bool = False) -> Path:
    meta = meta or Meta()
    contacts = contacts or contacts_mod.default_contacts()
    out_pdf = Path(out_pdf)

    work = Path(tempfile.mkdtemp(prefix="flightplan_", dir=config.CACHE_DIR
                                 if config.CACHE_DIR.exists() else None))
    try:
        figs = {}
        for key, fn in (("tide", plot_tide), ("wind", plot_wind_map),
                        ("sea", plot_sea_state)):
            try:
                p = fn(c, theme, work / f"fig_{key}")
                if p:
                    figs[key] = p.name
            except Exception as exc:  # noqa: BLE001
                c.warnings.append(f"figure {key}: {type(exc).__name__}: {exc}")

        if _LOGO.is_file():
            shutil.copyfile(_LOGO, work / _LOGO.name)

        ctx = _context(c, meta, contacts)
        ctx["figures"] = figs
        ctx["theme"] = theme

        tex = _env().get_template(TEMPLATE).render(**ctx)
        (work / "flight_plan.tex").write_text(tex, encoding="utf-8")

        log = ""
        for _ in range(2):
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "flight_plan.tex"],
                cwd=work, capture_output=True, text=True)
            log = proc.stdout + proc.stderr
        pdf = work / "flight_plan.pdf"
        if not pdf.is_file():
            tail = "\n".join(log.splitlines()[-40:])
            raise RuntimeError(f"pdflatex did not produce a PDF:\n{tail}")

        _publish_atomic(pdf, out_pdf)
        if keep_tex:
            _publish_atomic(work / "flight_plan.tex", out_pdf.with_suffix(".tex"))
        return out_pdf
    finally:
        if not keep_tex:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"[render] kept build dir: {work}")
