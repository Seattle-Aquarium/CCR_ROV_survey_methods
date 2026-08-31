"""
Leaflet map of every transect at a site.

The point of the map is comparison: where the transects sit relative to one
another, and whether the three localisation sources agree. So transects are
coloured individually and the *source* -- DVL dead reckoning, EKF, or surface
GPS -- is a switch that redraws all of them at once, rather than three fixed
colours that make two transects indistinguishable.

The page is a single self-contained HTML file: the track data is inlined as
JSON, and only Leaflet itself and the basemap tiles are fetched, so the file can
be copied around a shared drive and opened directly. It needs a network
connection the first time it is opened.

Reads either this tool's transect CSVs or the older tlog_to_csv.py ones -- the
coordinate columns are the same, and the alternative spellings that appeared in
earlier exports are accepted too.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from .fsutil import write_text

log = logging.getLogger(__name__)

#: Localisation sources, in the order they are offered. Each is (key, label,
#: list of acceptable (lat, lon) column pairs).
SOURCES: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("dvl", "DVL (dead reckoning)", [("DVLlat", "DVLlon")]),
    ("ekf", "EKF (fused)", [("EKFlat", "EKFlon"), ("EKF_lat", "EKF_lon"),
                            ("EKF.lat", "EKF.lon")]),
    ("gps", "GPS (surface)", [("Latitude", "Longitude")]),
]

#: Colour-blind-safe categorical palette, cycled across transects.
PALETTE = [
    "#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00",
    "#56B4E9", "#F0E442", "#7B3294", "#1B7837", "#B2182B",
]


def _coords(df: pd.DataFrame, pairs: Sequence[tuple[str, str]]) -> list[list[float]]:
    """Clean [lat, lon] pairs for the first column pair the frame actually has.

    Consecutive repeats are dropped. A hovering ROV holds one position for
    minutes at a time and a static surface fix repeats for the whole dive, so
    without this a track is mostly the same coordinate written over and over --
    which changes nothing on screen and multiplies the page size.
    """
    for lat_col, lon_col in pairs:
        if not {lat_col, lon_col}.issubset(df.columns):
            continue
        lat = pd.to_numeric(df[lat_col], errors="coerce")
        lon = pd.to_numeric(df[lon_col], errors="coerce")
        ok = np.isfinite(lat) & np.isfinite(lon) & (lat != 0) & (lon != 0)
        if not ok.any():
            continue
        out: list[list[float]] = []
        for a, b in zip(lat[ok], lon[ok]):
            point = [round(float(a), 7), round(float(b), 7)]
            if not out or point != out[-1]:
                out.append(point)
        return out
    return []


def _series(df: pd.DataFrame, col: str) -> pd.Series:
    """A numeric column, or an empty series if the CSV does not have it.

    ``df.get(col)`` returns None for a missing column, and ``pd.to_numeric(None)``
    quietly yields a bare NaN scalar rather than raising -- so the absence would
    surface much later as an AttributeError on a float.
    """
    if col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _stat(df: pd.DataFrame, col: str, how: str = "mean") -> float | None:
    s = _series(df, col).dropna()
    if s.empty:
        return None
    value = float(getattr(s, how)())
    return value if np.isfinite(value) else None


@dataclass
class TrackSet:
    name: str
    tracks: dict[str, list[list[float]]]
    stats: dict[str, object]


def _summarise(name: str, df: pd.DataFrame) -> TrackSet:
    tracks = {key: _coords(df, pairs) for key, _label, pairs in SOURCES}

    # Depth is stored negative-down; the map reads better in metres below the
    # surface, so it is flipped here rather than in the popup template.
    depth = _series(df, "Depth").dropna()
    distance = _stat(df, "Distance", "sum")
    width = _stat(df, "Width")

    stats: dict[str, object] = {
        "rows": int(len(df)),
        "start": str(df["Time"].iloc[0]) if "Time" in df.columns and len(df) else None,
        "end": str(df["Time"].iloc[-1]) if "Time" in df.columns and len(df) else None,
        "date": str(df["Date"].iloc[0]) if "Date" in df.columns and len(df) else None,
        "distance_m": distance,
        "altitude_m": _stat(df, "Altitude"),
        "depth_shallow_m": -float(depth.max()) if not depth.empty else None,
        "depth_deep_m": -float(depth.min()) if not depth.empty else None,
        # The per-second footprint, averaged. Summing the Area_m2 column would
        # count the same patch of seabed once per second the ROV hovered over it,
        # which at survey speed inflates the total by two orders of magnitude.
        "footprint_m2": _stat(df, "Area_m2"),
        # Ground actually covered: the camera swath dragged along the track.
        "swath_m2": (width * distance) if (width and distance) else None,
        "speed_mps": _stat(df, "Velocity_mps"),
        "temp_c": _stat(df, "Water_temp_C"),
    }
    return TrackSet(name=name, tracks=tracks, stats=stats)


def build_map_html(
    transects: Sequence[tuple[str, pd.DataFrame]],
    *,
    site_name: str = "",
    survey_date: str = "",
) -> tuple[str, list[str]]:
    """Render the map page. Returns (html, warnings)."""
    warnings: list[str] = []
    sets: list[TrackSet] = []
    for name, df in transects:
        ts = _summarise(name, df)
        if not any(ts.tracks.values()):
            warnings.append(f"{name}: no usable coordinates, left off the map")
            continue
        sets.append(ts)

    if not sets:
        raise ValueError("none of the transects had usable coordinates to map")

    payload = {
        "site": site_name,
        "date": survey_date,
        "sources": [{"key": k, "label": lbl} for k, lbl, _ in SOURCES],
        "palette": PALETTE,
        "transects": [
            {"name": t.name, "tracks": t.tracks, "stats": t.stats} for t in sets
        ],
    }
    title = " - ".join(x for x in ("ROV transects", site_name, survey_date) if x)
    # NaN and Infinity are not JSON, and a browser refuses the whole page if they
    # reach it. A statistic that could not be computed becomes null instead.
    blob = json.dumps(_clean(payload), separators=(",", ":"), allow_nan=False)
    return _PAGE.replace("__TITLE__", title).replace('"__DATA__"', blob), warnings


def _clean(obj):
    """Recursively replace non-finite floats with None, ready for JSON."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return obj


def write_map(
    transects: Sequence[tuple[str, pd.DataFrame]],
    out_path: Path | str,
    *,
    site_name: str = "",
    survey_date: str = "",
) -> tuple[Path, list[str]]:
    html, warnings = build_map_html(transects, site_name=site_name, survey_date=survey_date)
    path = write_text(Path(out_path), html, log=warnings.append)
    return path, warnings


def write_map_from_csvs(
    csv_paths: Sequence[Path | str],
    out_path: Path | str,
    *,
    site_name: str = "",
    survey_date: str = "",
) -> tuple[Path, list[str]]:
    """Build the map from transect CSVs already on disk."""
    transects = []
    for p in csv_paths:
        p = Path(p)
        try:
            transects.append((p.stem, pd.read_csv(p)))
        except Exception as ex:
            log.warning("could not read %s: %s", p, ex)
    if not transects:
        raise ValueError("no readable transect CSVs")
    if not site_name:
        first = transects[0][1]
        if "Site_name" in first.columns and first["Site_name"].notna().any():
            site_name = str(first["Site_name"].dropna().iloc[0])
    return write_map(transects, out_path, site_name=site_name, survey_date=survey_date)


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { position: absolute; inset: 0; background: #0b1622; }
  .panel {
    font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
    background: rgba(255,255,255,.95); color: #16242e;
    border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,.35);
    padding: 10px 12px; max-height: 78vh; overflow-y: auto;
  }
  .panel h2 { font-size: 13px; margin: 0 0 2px; letter-spacing: .02em; }
  .panel .sub { color: #5b6b78; font-size: 11.5px; margin-bottom: 8px; }
  .panel h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
              color: #5b6b78; margin: 10px 0 5px; font-weight: 600; }
  .panel label { display: flex; align-items: center; gap: 7px; cursor: pointer;
                 padding: 2px 0; }
  .swatch { width: 22px; height: 3px; border-radius: 2px; flex: none; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .row { display: flex; align-items: center; gap: 7px; padding: 2px 0; }
  .muted { color: #8c9aa5; }
  .btn { display: block; width: 100%; margin-top: 9px; padding: 5px 8px;
         font: inherit; font-size: 12px; background: #16242e; color: #fff;
         border: 0; border-radius: 5px; cursor: pointer; }
  .btn:hover { background: #294050; }
  /* Leaflet puts controls above popups by default, so the panel would cover
     half of any popup opened near it. */
  .leaflet-popup-pane { z-index: 900; }
  .leaflet-popup-content { font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; margin: 10px 12px; }
  .leaflet-popup-content b { font-size: 13.5px; }
  .leaflet-popup-content table { border-collapse: collapse; margin-top: 6px; }
  .leaflet-popup-content td { padding: 1px 0; }
  .leaflet-popup-content td:first-child { color: #5b6b78; padding-right: 12px; }
  .leaflet-popup-content td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const DATA = "__DATA__";

const map = L.map('map', { preferCanvas: true });
const bases = {
  'Satellite': L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 21, maxNativeZoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics' }),
  'Ocean': L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 21, maxNativeZoom: 13, attribution: 'Esri, GEBCO, NOAA' }),
  'Street': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }),
};
bases['Satellite'].addTo(map);
L.control.layers(bases, null, { position: 'topright' }).addTo(map);
L.control.scale({ imperial: false, position: 'bottomright' }).addTo(map);

const colourOf = i => DATA.palette[i % DATA.palette.length];
const fmt = (v, d, unit) => (v === null || v === undefined) ? '&mdash;'
  : v.toFixed(d) + (unit ? ' ' + unit : '');

// One layer group per transect per source. Only the selected source is on the
// map at a time, so switching sources does not disturb which transects are shown.
const layers = {};
DATA.transects.forEach((t, i) => {
  const colour = colourOf(i);
  layers[t.name] = {};
  DATA.sources.forEach(src => {
    const pts = t.tracks[src.key] || [];
    if (!pts.length) return;
    const g = L.layerGroup();
    L.polyline(pts, { color: colour, weight: 3, opacity: .95 })
      .bindTooltip(t.name + ' &middot; ' + src.label, { sticky: true })
      .bindPopup(popupFor(t, src, pts))
      .addTo(g);
    // A hollow ring for the start and a solid disc for the end. Transects at one
    // site often share a seed position, so the two markers land on top of each
    // other and have to stay distinguishable at a glance.
    L.circleMarker(pts[0], { radius: 7, color: colour, weight: 3,
        fillColor: '#fff', fillOpacity: 1 })
      .bindTooltip(t.name + ' start', { direction: 'top' }).addTo(g);
    L.circleMarker(pts[pts.length - 1], { radius: 5, color: '#fff', weight: 2,
        fillColor: colour, fillOpacity: 1 })
      .bindTooltip(t.name + ' end', { direction: 'top' }).addTo(g);
    layers[t.name][src.key] = g;
  });
});

function popupFor(t, src, pts) {
  const s = t.stats;
  const rows = [
    ['Source', src.label],
    ['Date', s.date || '&mdash;'],
    ['Time', (s.start && s.end) ? s.start + ' &ndash; ' + s.end : '&mdash;'],
    ['Duration', fmt(s.rows / 60, 1, 'min')],
    ['Distance', fmt(s.distance_m, 1, 'm')],
    ['Depth', (s.depth_shallow_m === null || s.depth_shallow_m === undefined) ? '&mdash;'
       : fmt(s.depth_shallow_m, 1, '') + ' &ndash; ' + fmt(s.depth_deep_m, 1, 'm')],
    ['Mean altitude', fmt(s.altitude_m, 2, 'm')],
    ['Mean speed', fmt(s.speed_mps, 2, 'm/s')],
    ['Mean footprint', fmt(s.footprint_m2, 2, 'm&sup2;')],
    ['Swath covered', fmt(s.swath_m2, 0, 'm&sup2;')],
    ['Water temp', fmt(s.temp_c, 1, '&deg;C')],
    ['Track points', pts.length.toString()],
  ];
  return '<b>' + t.name + '</b><table>' +
    rows.map(r => '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td></tr>').join('') +
    '</table>';
}

// ---- control panel ----
const panel = L.control({ position: 'topleft' });
panel.onAdd = function () {
  const d = L.DomUtil.create('div', 'panel');
  L.DomEvent.disableClickPropagation(d).disableScrollPropagation(d);

  const heading = [DATA.site, DATA.date].filter(Boolean).join(' &middot; ');
  d.innerHTML = '<h2>ROV transects</h2>' +
    (heading ? '<div class="sub">' + heading + '</div>' : '') +
    '<h3>Localisation</h3><div id="srcs"></div>' +
    '<h3>Transects</h3><div id="trs"></div>';

  const srcs = d.querySelector('#srcs');
  const available = DATA.sources.filter(s =>
    DATA.transects.some(t => (t.tracks[s.key] || []).length));
  available.forEach((s, i) => {
    const lab = document.createElement('label');
    lab.innerHTML = '<input type="radio" name="src" value="' + s.key + '"' +
      (i === 0 ? ' checked' : '') + '><span>' + s.label + '</span>';
    lab.querySelector('input').addEventListener('change', render);
    srcs.appendChild(lab);
  });
  if (!available.length) srcs.innerHTML = '<div class="muted">no coordinates</div>';

  const trs = d.querySelector('#trs');
  DATA.transects.forEach((t, i) => {
    const lab = document.createElement('label');
    lab.innerHTML = '<input type="checkbox" data-t="' + i + '" checked>' +
      '<span class="swatch" style="background:' + colourOf(i) + '"></span>' +
      '<span>' + t.name + '</span>';
    lab.querySelector('input').addEventListener('change', render);
    trs.appendChild(lab);
  });

  const legend = document.createElement('div');
  legend.innerHTML =
    '<h3>Markers</h3>' +
    '<div class="row"><span class="dot" style="background:#fff;border:2px solid #16242e"></span>start</div>' +
    '<div class="row"><span class="dot" style="background:#16242e;border:2px solid #fff;box-shadow:0 0 0 1px #16242e"></span>end</div>';
  d.appendChild(legend);

  const btn = document.createElement('button');
  btn.className = 'btn';
  btn.textContent = 'Zoom to visible';
  btn.addEventListener('click', () => fit());
  d.appendChild(btn);
  return d;
};
panel.addTo(map);

function selectedSource() {
  const el = document.querySelector('input[name=src]:checked');
  return el ? el.value : (DATA.sources[0] && DATA.sources[0].key);
}

function shown() {
  const src = selectedSource();
  const out = [];
  document.querySelectorAll('#trs input[type=checkbox]').forEach(cb => {
    if (!cb.checked) return;
    const t = DATA.transects[+cb.dataset.t];
    const g = layers[t.name] && layers[t.name][src];
    if (g) out.push(g);
  });
  return out;
}

function render() {
  Object.values(layers).forEach(bySrc =>
    Object.values(bySrc).forEach(g => map.removeLayer(g)));
  shown().forEach(g => g.addTo(map));
}

function fit() {
  const groups = shown();
  if (!groups.length) return;
  // A fresh bounds object: L.LatLngBounds.extend mutates, and a polyline's
  // getBounds() hands back its own internal one, so extending it in place
  // corrupts that layer's cached extent.
  let bounds = L.latLngBounds([]);
  groups.forEach(g => g.eachLayer(l => {
    if (l.getLatLngs) bounds.extend(l.getBounds());
  }));
  if (!bounds.isValid()) return;
  // Padded clear of the control panel, which floats over the top-left corner --
  // a transect fitted underneath it is on the map but cannot be seen.
  const panel = document.querySelector('.panel');
  const wide = window.innerWidth > 620;
  const left = (panel && wide) ? panel.offsetWidth + 40 : 30;
  const top = (panel && !wide) ? panel.offsetHeight + 30 : 30;
  // maxZoom matters when a track is a single repeated fix -- a zero-area bounds
  // would otherwise slam the map to its deepest zoom on one pixel of tile.
  map.fitBounds(bounds, {
    paddingTopLeft: [left, top], paddingBottomRight: [40, 50], maxZoom: 20,
  });
}

render();
fit();
if (!map._loaded) map.setView([47.6, -122.35], 13);
</script>
</body>
</html>
"""
