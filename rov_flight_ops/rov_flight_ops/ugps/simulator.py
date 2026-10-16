"""
A stand-in G2 topside: the read paths the UGPS capture uses, on localhost.

For the tests, and for trying the Navigation tab's receiver row without a
UGPS::

    python -m rov_flight_ops.ugps.simulator --port 8094 --weak 2

then set the UGPS address to ``127.0.0.1:8094``. ``--weak N`` makes receiver
N's signal 10 dB lower than the others, ``--noisy N`` its noise 10 dB higher.

Answers have the shapes Water Linked's public demo G2 gave on 15 October
2026. The acoustic solution changes twice a second, as the real one does.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class G2Simulator:
    def __init__(self, port: int = 0) -> None:
        self.weak: dict[int, float] = {}       # receiver -> dB of signal lost
        self.noisy: dict[int, float] = {}      # receiver -> dB of extra noise
        self.dropouts: dict[int, float] = {}   # receiver -> share of solutions invalid
        self.down = False                      # stop answering at all
        self.no_fix = False                    # acoustic paths answer 500, as with no locator
        self.raw_404 = False                   # a firmware without the deprecated raw path
        self.status_report = [
            {"status_group": g, "status_group_id": g, "status": "ok",
             "message": "", "error_ids": None}
            for g in ("receivers", "locator", "timesync", "gnss", "compass", "depth")]
        self.requests: list[tuple[str, str]] = []
        self._rng = random.Random(7)
        self._lock = threading.Lock()
        self._epoch = time.monotonic()
        self._solution_at = -1
        self._solution: dict = {}
        sim = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                sim.requests.append(("GET", self.path))
                if sim.down:
                    time.sleep(2.0)
                    self.close_connection = True
                    return
                code, obj = sim.answer(self.path.split("?", 1)[0])
                self._send(code, obj)

            def _refuse(self):
                sim.requests.append((self.command, self.path))
                self._send(405, {"error": "this simulator only answers GET"})

            do_PUT = do_POST = do_PATCH = do_DELETE = _refuse

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None

    # -- running ---------------------------------------------------------------

    def start(self) -> G2Simulator:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                        name="g2-sim")
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- answers ---------------------------------------------------------------

    def _acoustic(self) -> dict:
        tick = int((time.monotonic() - self._epoch) * 2)      # 2 Hz
        with self._lock:
            if tick != self._solution_at:
                self._solution_at = tick
                t = tick / 2
                x, y, z = 6 * math.cos(t / 20), 12 + 3 * math.sin(t / 20), 13.5
                dist, rssi, nsd, valid = [], [], [], []
                for i in range(4):
                    dist.append(round(math.hypot(x, y) + 0.2 * i, 3))
                    rssi.append(-60 - self.weak.get(i, 0.0) + self._rng.gauss(0, 1.0))
                    nsd.append(-100 + self.noisy.get(i, 0.0) + self._rng.gauss(0, 1.0))
                    valid.append(0 if self._rng.random() < self.dropouts.get(i, 0.0) else 1)
                self._solution = {"position_valid": True, "x": x, "y": y, "z": z,
                                  "std": 0.4, "receiver_distance": dist,
                                  "receiver_rssi": rssi, "receiver_nsd": nsd,
                                  "receiver_valid": valid}
            return dict(self._solution)

    def answer(self, path: str) -> tuple[int, object]:
        if path in ("/api/v1/position/acoustic/filtered", "/api/v1/position/acoustic/raw"):
            if path.endswith("raw") and self.raw_404:
                return 404, {"error": "not found"}
            if self.no_fix:
                return 500, {"error": "no position"}
            return 200, self._acoustic()
        if path == "/api/v1/position/global":
            a = self._acoustic()
            return 200, {"lat": 47.60754 + a["y"] / 111320, "lon": -122.34397 + a["x"] / 75100,
                         "cog": 0, "sog": 0, "hdop": 0.8, "numsats": 22, "fix_quality": 1,
                         "orientation": -1}
        if path == "/api/v1/position/master":
            return 200, {"lat": 47.60754, "lon": -122.34397, "cog": 0, "sog": 0, "hdop": 0.8,
                         "numsats": 22, "fix_quality": 1, "orientation": 272.0}
        if path == "/api/v1/about/status":
            return 200, {"battery": 80, "gps": 3, "imu": 2}
        if path == "/api/v1/about/temperature":
            return 200, {"board": 54.2, "water": 12.9}
        if path == "/api/v1/status_report/":
            return 200, self.status_report
        if path == "/api/v1/about":
            return 200, {"product_name": "Underwater GPS G2 (simulated)", "version": "3.3.1",
                         "chipid": "0xsimulated"}
        if path == "/api/v1/config/generic":
            return 200, {"gps": "external", "compass": "external", "channel": 5015,
                         "speed_of_sound": 1475}
        if path == "/api/v1/config/receivers/":
            return 200, [{"id": i, "x": -4.1 + 0.49 * (i % 2), "y": 4.3, "z": 0.9}
                         for i in range(4)]
        if path == "/api/v1/config/antenna":
            return 200, {"antenna_type": "simulated"}
        return 404, {"error": "not found"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8094)
    ap.add_argument("--weak", type=int, action="append", default=[],
                    help="a receiver (0-3) whose signal is 10 dB low")
    ap.add_argument("--noisy", type=int, action="append", default=[],
                    help="a receiver (0-3) whose noise is 10 dB high")
    args = ap.parse_args()
    sim = G2Simulator(args.port)
    sim.weak = dict.fromkeys(args.weak, 10.0)
    sim.noisy = dict.fromkeys(args.noisy, 10.0)
    print(f"simulated G2 at http://{sim.host}  (Ctrl-C to stop)")
    try:
        sim.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
