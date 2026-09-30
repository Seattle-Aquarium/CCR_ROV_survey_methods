# Light-beam visualizers

Two interactive notebooks, written while choosing and angling the V4 DeepSea
lights. Each lets you set the ROV's height above the seafloor, the light
spacing, the beam angle and the lights' tilt with sliders, and draws the
resulting illuminated footprint on the seafloor. They supported the choice of
the narrower 75° DeepSea beam and of 10° lateral and forward tilts, which widen
the lit field of view while keeping some overlap between beams for uneven
terrain (Supplementary Section S5).

| Notebook | What it shows | Open in Colab |
|---|---|---|
| `ROV_Beam_Visualizer_LuxBands.ipynb` | 2D forward-facing view of two tilted lights, with the main beam and 100/90/80/70% lux bands, and the width each band covers on the seafloor | [open](https://colab.research.google.com/github/Seattle-Aquarium/CCR_ROV_survey_methods/blob/main/lighting/beam_visualizers/ROV_Beam_Visualizer_LuxBands.ipynb) |
| `3D_ROV_Beam_Visualizer.ipynb` | 3D view of the four lights' beam cones and their footprints on the seafloor | [open](https://colab.research.google.com/github/Seattle-Aquarium/CCR_ROV_survey_methods/blob/main/lighting/beam_visualizers/3D_ROV_Beam_Visualizer.ipynb) |

They also run in any Jupyter environment with matplotlib, numpy and
ipywidgets. The defaults match ROV *Lutris*: 0.8 m altitude, lights 0.514 m
apart side to side and 0.4635 m front to back.

For a physically based model of the same lights (photometry from the
manufacturer's data, irradiance on the seafloor), see `../simulation/`.
