"""
The Light Monitor extension: the pieces wired together.

    controller --USB--> SerialSource --line--> Recorder (every line, first)
                                          '--> protocol.parse_line --> Model
    Model --once a second--> Recorder (CSV row, vehicle record)
                         --> Publisher --> mavlink2rest (NAMED_VALUE_FLOAT)
          --events-------> Recorder, mavlink2rest (STATUSTEXT)
    mavlink2rest --SYS_STATUS, NAMED_VALUE_FLOAT--> VehicleReader --> Model
    web server: the page, /api/v1/state, /api/v1/records, the files

The extension is the **only** thing that opens the controller's serial port.
Its own page, rov_flight_ops and anything else read the extension's API, so
any number of them can watch at once without competing for the port.

Nothing is published to MAVLink until a controller has been heard from, so
the extension can be installed on a vehicle without these lights (Nereo) and
stay quiet there.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import __version__
from .mavlink import Mavlink2Rest, Publisher, VehicleReader
from .model import Model, csv_columns
from .protocol import parse_line
from .recorder import Recorder
from .serialsource import SerialSource
from .simulator import SimulatedController
from .web import Api

log = logging.getLogger(__name__)

STATUSTEXT_PER_S = 3


class App(Api):

    def __init__(self, *, data: Path, mode: str = "auto", mavlink_url: str | None = None,
                 mavlink_component: int = 191, vehicle_warn_v: float = 10.5,
                 sim_monitor: bool = False, port: int = 9420) -> None:
        self.port = port
        self.mode = mode
        self.started = time.time()
        self.model = Model(vehicle_warn_v=vehicle_warn_v)
        self.recorder = Recorder(data, csv_columns(), session={
            "extension": "light_monitor", "version": __version__, "mode": mode,
            "mavlink": mavlink_url or "", "component": mavlink_component})
        self.mav = Mavlink2Rest(mavlink_url, component=mavlink_component) if mavlink_url else None
        self.publisher = Publisher(self.mav) if self.mav else None
        self.vehicle = (VehicleReader(self.mav, self._vehicle, self._ardusub)
                        if self.mav else None)
        self._heard = False
        self._texts: list = []
        self._texts_lock = threading.Lock()
        self._stop = threading.Event()
        if mode == "simulate":
            self.source = SimulatedController(self._line, monitor=sim_monitor)
            self.model.set_link(connected=True, source="simulator", port="(simulated)",
                                note="simulated controller -- not a real light")
        elif mode == "off":
            self.source = None
        else:
            self.source = SerialSource(self._line, self._status, mode=mode)

    # -- inputs ----------------------------------------------------------

    def _line(self, text: str, src: str, rx: float) -> None:
        self.recorder.line(text, src, rx)                # the record first
        rec = parse_line(text)
        self.model.note_line(rx, rec is not None)
        if rec is None:
            if text.lstrip().startswith(("{", "@L")):
                self.model.note_bad_line()
            return
        self._heard = True
        self._events(self.model.ingest(rec, rx))

    def _status(self, **fields) -> None:
        self._events(self.model.set_link(**fields))

    def _vehicle(self, v, a, source: str = "") -> None:
        self._events(self.model.set_vehicle(v, a, source=source))

    def _ardusub(self, **values) -> None:
        self.model.set_ardusub(**values)

    def _events(self, events) -> None:
        for e in events:
            log.info("event: %s", e.text)
            self.recorder.record("event", rx=e.t, lvl=e.lvl, text=e.text)
            if self.mav is not None and self._heard:
                with self._texts_lock:
                    self._texts.append(e)

    # -- the once-a-second loop -------------------------------------------

    def _tick(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                self._events(self.model.tick(now))
                self.recorder.csv_row(self.model.csv_row(now))
                veh = self.model.vehicle
                if veh is not None and now - veh.get("at", 0) < 2.0:
                    self.recorder.record("vehicle", rx=now, v=veh.get("v"), a=veh.get("a"),
                                         source=veh.get("source"),
                                         ardusub={k: v for k, v in self.model.ardusub.items()
                                                  if k != "at"})
                if self.publisher is not None and self._heard:
                    uptime_ms = int((self.model.last_ms or 0)) & 0xFFFFFFFF
                    self.publisher.publish(self.model.nvf_values(now), uptime_ms)
                    with self._texts_lock:
                        send, self._texts = (self._texts[:STATUSTEXT_PER_S],
                                             self._texts[STATUSTEXT_PER_S:])
                    for e in send:
                        self.mav.statustext("Lights: " + e.short, e.lvl)
            except Exception:                                  # noqa: BLE001
                log.exception("tick")
            self._stop.wait(max(0.05, 1.0 - (time.time() - now)))

    def start(self) -> None:
        if self.source is not None:
            self.source.start()
        if self.vehicle is not None:
            self.vehicle.start()
        threading.Thread(target=self._tick, daemon=True, name="tick").start()

    def stop(self) -> None:
        self._stop.set()
        if self.source is not None:
            self.source.stop()
        if self.vehicle is not None:
            self.vehicle.stop()
        self.recorder.close()

    # -- the API -----------------------------------------------------------

    def _mavlink_status(self) -> dict:
        if self.mav is None:
            return {"enabled": False}
        return {"enabled": True, "url": self.mav.url, "system": self.mav.system,
                "component": self.mav.component, "sent": self.mav.sent,
                "errors": self.mav.errors, "last_error": self.mav.last_error,
                "publishing": self._heard,
                "vehicle_ok": bool(self.vehicle and self.vehicle.ok)}

    def state(self) -> dict:
        released = None
        if isinstance(self.source, SerialSource):
            released = self.source.released_until
            self.model.link["released_until"] = released
        return self.model.snapshot(
            recording=self.recorder.status(), mavlink=self._mavlink_status(),
            extension={"version": __version__, "mode": self.mode, "started": self.started,
                       "port": self.port, "can_release": isinstance(self.source, SerialSource)})

    def records(self, after: int, limit: int):
        return self.recorder.after(after, limit)

    def files(self) -> list[dict]:
        return self.recorder.files()

    def file_path(self, name: str) -> Path | None:
        p = self.recorder.folder / name
        return p if p.parent == self.recorder.folder else None

    def release(self, minutes: float) -> dict:
        if not isinstance(self.source, SerialSource):
            return {"ok": False, "error": "no serial port in this mode"}
        until = self.source.release(minutes)
        self.recorder.record("event", lvl="info",
                             text=f"Port released for {minutes:g} min (maintenance)")
        return {"ok": True, "released_until": until}

    def resume(self) -> dict:
        if isinstance(self.source, SerialSource):
            self.source.resume()
            self.recorder.record("event", lvl="info", text="Port resumed")
        return {"ok": True}

    def service(self) -> dict:
        return {
            "name": "Light Monitor",
            "description": "The four DeepSea lights and their controller, live and "
                           "recorded, with a summary in MAVLink (tlog / mcap).",
            "icon": "mdi-car-light-high",
            "company": "Seattle Aquarium",
            "version": __version__,
            "webpage": "https://github.com/Seattle-Aquarium/CCR_ROV_survey_methods/"
                       "tree/main/blueos_extensions/light_monitor",
            "api": "/api/v1/state",
            "works_in_relative_paths": True,
        }
