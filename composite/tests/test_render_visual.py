"""Render the panel and the gauge convention grid for visual inspection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

from composite import brand, gauges
from composite.config import Layout
from composite.overlay import measure_panel, render_panel

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
OUT.mkdir(parents=True, exist_ok=True)

cfg = Layout()
print("Montserrat installed:", brand.font_available())
print("font path:", brand.font_path("medium"))

# ---- panel -----------------------------------------------------------
vals = {
    "altitude": 0.89, "speed": 0.09, "depth": 14.80,
    "mode": "STABILIZE", "lights": 60.0, "gain": 30.0,
    "cam_tilt": 1.00, "temp_c": 13.1,
    "power_w": 112.0, "voltage_v": 13.51, "current_a": 8.3,
    "heading": 260.0, "pitch": -0.0, "roll": 0.1,
}
m = measure_panel(cfg)
print(f"panel metrics: {m.width}x{m.height}  label_w={m.label_w} "
      f"value_w={m.value_w} unit_w={m.unit_w} line_h={m.line_h}")

panel = render_panel(vals, cfg, m)
g = gauges.render_gauges(vals, cfg)
print(f"panel png: {panel.size}   gauge png: {g.size}  (inset h = {cfg.inset_height()})")

# lay them out as they will appear, over a mid-grey stand-in for video
strip = Image.new("RGBA", (g.width + panel.width + 60, max(g.height, panel.height) + 40),
                  (70, 78, 70, 255))
strip.alpha_composite(g, (20, 20))
strip.alpha_composite(panel, (20 + g.width + 24, 20))
strip.convert("RGB").save(OUT / "py_strip.png")

# ---- gauge conventions ----------------------------------------------
cases = [
    ("HDG 0 (N up)", dict(heading=0, pitch=0, roll=0)),
    ("HDG 90 (E up)", dict(heading=90, pitch=0, roll=0)),
    ("HDG 180 (S up)", dict(heading=180, pitch=0, roll=0)),
    ("HDG 270 (W up)", dict(heading=270, pitch=0, roll=0)),
    ("PITCH +20 up", dict(heading=45, pitch=20, roll=0)),
    ("PITCH -20 down", dict(heading=45, pitch=-20, roll=0)),
    ("ROLL +30 right", dict(heading=45, pitch=0, roll=30)),
    ("ROLL -30 left", dict(heading=45, pitch=0, roll=-30)),
]
tiles = []
for label, v in cases:
    im = gauges.render_gauges(v, cfg)
    d = ImageDraw.Draw(im)
    d.text((im.width // 2, 8), label, font=gauges._font(26, "bold"),
           fill=(255, 220, 90, 255), anchor="ma")
    tiles.append(im)

cols, tw, th = 4, tiles[0].width, tiles[0].height
grid = Image.new("RGB", (cols * tw, 2 * th), (70, 70, 70))
for i, t in enumerate(tiles):
    grid.paste(t.convert("RGB"), ((i % cols) * tw, (i // cols) * th))
grid.save(OUT / "py_gauge_grid.png")
print("wrote py_strip.png and py_gauge_grid.png ->", OUT)
