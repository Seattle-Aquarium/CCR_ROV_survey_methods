"""Helpers for the NWS gridpoint format: ISO-8601 interval strings and unit
normalisation.

A gridpoint layer is a list of ``{"validTime": "<start>/<ISO8601 duration>",
"value": v}``. Consecutive equal values are merged upstream, so a single entry
can cover many hours; we expand each back to an hourly (time, value) series.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_DUR = re.compile(
    r"P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def parse_duration(s: str) -> timedelta:
    m = _DUR.match(s)
    if not m:
        raise ValueError(f"bad ISO-8601 duration: {s!r}")
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return timedelta(weeks=g["w"], days=g["d"], hours=g["h"],
                     minutes=g["m"], seconds=g["s"])


# uom string -> (callable, canonical unit label)
_CONVERT = {
    "wmoUnit:degC": (lambda c: c * 9 / 5 + 32, "degF"),
    "wmoUnit:degF": (lambda f: f, "degF"),
    "wmoUnit:km_h-1": (lambda k: k * 0.5399568, "kt"),
    "wmoUnit:m_s-1": (lambda v: v * 1.9438445, "kt"),
    "wmoUnit:percent": (lambda v: v, "percent"),
    "wmoUnit:degree_(angle)": (lambda v: v, "deg"),
    "wmoUnit:mm": (lambda v: v, "mm"),
    "wmoUnit:m": (lambda v: v, "m"),
    "nwsUnit:s": (lambda v: v, "s"),
}


def expand_layer(layer: dict, zone) -> list[tuple[datetime, float]]:
    """Expand one gridpoint layer to an hourly [(aware datetime, value)] series
    in ``zone``. Values are converted to canonical units (degF, kt, %, deg, mm)."""
    if not layer or "values" not in layer:
        return []
    conv, _unit = _CONVERT.get(layer.get("uom", ""), (lambda v: v, ""))
    out: list[tuple[datetime, float]] = []
    for item in layer["values"]:
        v = item.get("value")
        if v is None:
            continue
        start_s, dur_s = item["validTime"].split("/")
        start = datetime.fromisoformat(start_s).astimezone(zone)
        span = parse_duration(dur_s)
        steps = max(1, int(span.total_seconds() // 3600))
        for k in range(steps):
            out.append((start + timedelta(hours=k), float(conv(v))))
    out.sort(key=lambda t: t[0])
    return out


def on_date(series: list[tuple[datetime, float]], day, zone) -> dict[int, float]:
    """{hour 0..23: value} for the given local date."""
    hourly: dict[int, float] = {}
    for t, v in series:
        lt = t.astimezone(zone)
        if lt.date() == day:
            hourly[lt.hour] = v
    return hourly
