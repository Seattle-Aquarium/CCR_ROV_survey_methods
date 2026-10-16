"""
    python -m light_monitor [--simulate] [--port 9420] [--data DIR] ...

Every option also reads an environment variable, which is how the Docker
image is configured on the vehicle (see the Dockerfile).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from . import __version__
from .app import App
from .web import serve


def main(argv=None) -> None:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="light_monitor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(env("LIGHT_MONITOR_PORT", "9420")))
    ap.add_argument("--host", default=env("LIGHT_MONITOR_HOST", "0.0.0.0"))
    ap.add_argument("--data", default=env("LIGHT_MONITOR_DATA", "/data"),
                    help="where the recording goes")
    ap.add_argument("--mode", default=env("LIGHT_MONITOR_MODE", "auto"),
                    choices=("auto", "data", "console", "simulate", "off"),
                    help="auto: the telemetry port if there is one, else the console")
    ap.add_argument("--simulate", action="store_true", help="same as --mode simulate")
    ap.add_argument("--sim-monitor", action="store_true",
                    help="give the simulated controller a power monitor")
    ap.add_argument("--mavlink", default=env("MAVLINK2REST_URL", "http://127.0.0.1:6040"))
    ap.add_argument("--no-mavlink", action="store_true",
                    default=env("LIGHT_MONITOR_NO_MAVLINK", "") not in ("", "0"))
    ap.add_argument("--mavlink-component", type=int,
                    default=int(env("LIGHT_MONITOR_COMPONENT", "191")))
    ap.add_argument("--vehicle-warn-v", type=float,
                    default=float(env("LIGHT_MONITOR_VEHICLE_WARN_V", "10.5")))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = App(data=Path(a.data), mode="simulate" if a.simulate else a.mode,
              mavlink_url=None if a.no_mavlink else a.mavlink,
              mavlink_component=a.mavlink_component, vehicle_warn_v=a.vehicle_warn_v,
              sim_monitor=a.sim_monitor, port=a.port)
    app.start()
    server = serve(app, a.host, a.port)
    logging.info("Light Monitor %s on http://%s:%d (mode %s, data %s)", __version__,
                 a.host, a.port, app.mode, a.data)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: done.set())
        except (ValueError, OSError):
            pass
    try:
        while not done.wait(0.5):      # a bare wait() ignores Ctrl-C on Windows
            pass
    except KeyboardInterrupt:
        pass
    server.shutdown()
    app.stop()


if __name__ == "__main__":
    main()
