---
title: ROV Flight Operations simulator
emoji: 🌊
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
fullWidth: true
header: mini
pinned: false
license: mit
short_description: The Seattle Aquarium's ROV survey program, simulated
---

# ROV Flight Operations — the simulator, in a browser

ROV Flight Operations is the Seattle Aquarium's desktop program for flying
ROV surveys: connectivity, live monitoring of the laptop and tether, the
vehicle's parameters, navigation, the DVL beam by beam, transects and the
vehicle's log files. This Space runs the real program — the same window an
operator sees on the field laptop — with its **ROV flight simulator** switched
on: a vehicle flying four survey lanes off Pier 59 in Seattle, and on each live
tab one thing going wrong for 30 seconds, then 15 seconds of all green, then
the next. **Show what's wrong** in the banner names the fault and explains it.

* **Everyone on this page shares one window.** It is one desktop in one
  container: if somebody else is here, you will see them click, and they will
  see you.
* Nothing here reaches a vehicle and nothing is kept; the simulated flight
  starts again every hour.
* The program and its simulator are open source:
  [Seattle-Aquarium/CCR_ROV_survey_methods](https://github.com/Seattle-Aquarium/CCR_ROV_survey_methods),
  folder `rov_flight_ops/`; the simulator is described in
  `rov_flight_ops/specifications/simulator.md`.

How it is built: the program runs on a virtual X display (Xvfb) in a Linux
container, x11vnc shares that display, and noVNC draws it in the browser. The
build's own check — every module imported, every tab opened once and
photographed — is served by the container itself, at `/selfcheck/report.txt`
under the app's address.

This folder is assembled and uploaded by `rov_flight_ops/web_demo/deploy.py`;
edit the program on GitHub, not here.
