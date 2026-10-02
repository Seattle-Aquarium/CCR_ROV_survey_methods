"""
A stand-in Water Linked DVL A50, for the tests and for the bench.

It serves what the capture reads -- the TCP JSON stream, the web API, the web
GUI's stream and the two acoustic views -- in the shapes the real DVL and
Water Linked's public demo were seen to use, including the habits worth
testing against: lines that end in CR LF, and commands answered with
"Invalid JSON" when more than one arrives in a single read.

It can also misbehave on request, which is what it is for:

``pause(s)``       the DVL produces nothing for `s` seconds
``drop(n)``        the next `n` reports are made but never sent
``stall(s)``       reports are made on time but held back, then sent together
``invalid(s)``     reports keep coming with velocity_valid false
``split_writes``   each line is sent in pieces, so lines straddle reads

and it keeps a list of every request and command it was sent, so a test can
prove the capture asked for nothing it should not have.

From a terminal, for the DVL tab without a vehicle::

    python -m rov_flight_ops.dvl.simulator --http 8080

then type ``127.0.0.1:8080`` as the DVL address. The capture follows the TCP
port the simulator reports on its outputs page.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import websocket as W


def _now_us() -> int:
    return time.time_ns() // 1000


def rfc3339_ns(ns: int | None = None) -> str:
    ns = time.time_ns() if ns is None else ns
    whole = datetime.fromtimestamp(ns // 1_000_000_000, timezone.utc)
    return whole.strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % 1_000_000_000:09d}Z"


class SimulatedDvl:
    """A DVL on localhost. `start()`, use `tcp_port`/`http_port`, `stop()`."""

    def __init__(self, *, tcp_port: int = 0, http_port: int = 0,
                 rate_hz: float = 10.0, position_hz: float = 5.0,
                 ws_hz: float = 10.0, version: str = "2.7.2",
                 json_format: str = "json_v3.3", altitude: float = 1.0,
                 periodic_cycling: bool = False, seed: int = 7) -> None:
        self.rate_hz = rate_hz
        self.position_hz = position_hz
        self.ws_hz = ws_hz
        self.version = version
        self.json_format = json_format
        self.altitude = altitude
        self.config = {"speed_of_sound": 1475.0, "acoustic_enabled": True,
                       "dark_mode_enabled": False, "mounting_rotation_offset": 0.0,
                       "range_mode": "auto",
                       "periodic_cycling_enabled": periodic_cycling}
        self.temperature = 31.5
        self.split_writes = False
        #: Water Linked's demo answers the diagnostic-log request with 406.
        self.collect_supported = True
        self.rng = random.Random(seed)

        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._tcp_clients: list[socket.socket] = []
        self._send_locks: dict[int, threading.Lock] = {}
        self._ws_queues: list[deque] = []
        #: Everything asked of it: ("tcp", raw bytes) and ("http", method, path).
        self.requests: list[tuple] = []
        #: What it sent on TCP, per client connection, byte for byte.
        self.sent: dict[int, bytearray] = {}
        self.reports_made = 0
        self.reports_sent = 0

        self._pause_until = 0.0
        self._stall_until = 0.0
        self._invalid_until = 0.0
        self._drop = 0
        self._held: list[bytes] = []
        self._last_made_us: int | None = None
        self._t0 = time.monotonic()

        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("127.0.0.1", tcp_port))
        self._tcp.listen(8)
        self.tcp_port = self._tcp.getsockname()[1]
        self._http = ThreadingHTTPServer(("127.0.0.1", http_port), self._handler())
        self._http.daemon_threads = True
        self.http_port = self._http.server_address[1]
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    #  control
    # ------------------------------------------------------------------

    def start(self) -> SimulatedDvl:
        for target, name in ((self._accept_loop, "sim-accept"),
                             (self._velocity_loop, "sim-velocity"),
                             (self._position_loop, "sim-position"),
                             (self._ws_loop, "sim-ws"),
                             (self._http.serve_forever, "sim-http")):
            t = threading.Thread(target=target, daemon=True, name=name)
            self._threads.append(t)
            t.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self._http.shutdown()
            self._http.server_close()
        except Exception:
            pass
        try:
            self._tcp.close()
        except Exception:
            pass
        with self._lock:
            for c in self._tcp_clients:
                try:
                    c.close()
                except Exception:
                    pass
            self._tcp_clients.clear()

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.http_port}"

    def pause(self, seconds: float) -> None:
        self._pause_until = time.monotonic() + seconds

    def stall(self, seconds: float) -> None:
        self._stall_until = time.monotonic() + seconds

    def invalid(self, seconds: float) -> None:
        self._invalid_until = time.monotonic() + seconds

    def drop(self, n: int) -> None:
        self._drop = n

    def disconnect_clients(self) -> None:
        with self._lock:
            for c in self._tcp_clients:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                    c.close()
                except Exception:
                    pass
            self._tcp_clients.clear()

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._tcp_clients)

    # ------------------------------------------------------------------
    #  the TCP JSON stream
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        self._tcp.settimeout(0.2)
        while not self.stop_event.is_set():
            try:
                conn, _ = self._tcp.accept()
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self._tcp_clients.append(conn)
                self.sent[id(conn)] = bytearray()
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True,
                             name="sim-client").start()

    def _client_loop(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        while not self.stop_event.is_set():
            try:
                data = conn.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            self.requests.append(("tcp", data))
            docs = [d for d in data.split(b"\n") if d.strip()]
            if len(docs) != 1:
                self._send(conn, self._response("", False, "Invalid JSON", None))
                continue
            try:
                name = json.loads(docs[0]).get("command", "")
            except Exception:
                self._send(conn, self._response("", False, "Invalid JSON", None))
                continue
            self._send(conn, self._answer(name))
        with self._lock:
            if conn in self._tcp_clients:
                self._tcp_clients.remove(conn)

    def _response(self, to, ok, err, result) -> bytes:
        return (json.dumps({"response_to": to, "success": ok, "error_message": err,
                            "result": result, "format": self.json_format,
                            "type": "response"}) + "\r\n").encode()

    def _answer(self, name: str) -> bytes:
        modern = tuple(int(x) for x in self.version.split(".")[:3]) >= (2, 7, 2)
        if name == "get_config":
            return self._response(name, True, "", dict(self.config))
        if name == "get_version_info" and modern:
            return self._response(name, True, "", {
                "chipid": "0xsimulated", "hardware_revision": 4,
                "product_id": 21035, "product_name": "DVL A50",
                "variant": "simulated", "version_short": self.version,
                "version": f"{self.version} (simulator)", "is_ready": True})
        if name == "get_time_status" and modern:
            return self._response(name, True, "", {
                "system_time": rfc3339_ns()[:19] + "Z", "ntp_synced": False,
                "ntp_synced_to": "", "ntp_seconds_since_last_sync": None})
        if name == "get_time_ntp" and modern:
            return self._response(name, True, "", {"ntp_address": "auto"})
        # What software 2.7.1 answered to the 2.7.2 commands on the demo DVL.
        return self._response(name, False, "Command not recognised", None)

    def _send(self, conn: socket.socket, data: bytes) -> bool:
        # One line at a time per connection, as the DVL sends them: three
        # threads write to each client, and a line split for the test must not
        # have another thread's line spliced into the middle of it.
        with self._lock:
            lock = self._send_locks.setdefault(id(conn), threading.Lock())
        if self.split_writes and len(data) > 8:
            cut = [len(data) // 3, 2 * len(data) // 3]
            pieces = [data[:cut[0]], data[cut[0]:cut[1]], data[cut[1]:]]
        else:
            pieces = [data]
        try:
            with lock:
                for n, piece in enumerate(pieces):
                    if n:
                        time.sleep(0.002)
                    conn.sendall(piece)
                    # Recorded piece by piece: what was handed to the socket,
                    # which is what a client can have received.
                    with self._lock:
                        buf = self.sent.get(id(conn))
                        if buf is not None:
                            buf += piece
            return True
        except OSError:
            with self._lock:
                if conn in self._tcp_clients:
                    self._tcp_clients.remove(conn)
            return False

    def _broadcast(self, data: bytes) -> None:
        with self._lock:
            clients = list(self._tcp_clients)
        for c in clients:
            self._send(c, data)

    def velocity_report(self, tov_us: int, time_ms: float, valid: bool) -> dict:
        t = time.monotonic() - self._t0
        alt = self.altitude + 0.05 * math.sin(t / 3.0)
        beams = []
        for i, off in enumerate((0.03, 0.06, -0.02, 0.01)):
            rssi = -35.0 - 3 * i + self.rng.uniform(-1, 1)
            beams.append({"id": i, "velocity": 0.2 + 0.01 * i,
                          "distance": round(alt * 1.08 + off, 4),
                          "rssi": rssi, "nsd": -95.0 + self.rng.uniform(-2, 2),
                          "beam_valid": valid})
        return {"time": time_ms, "vx": 0.25, "vy": -0.01, "vz": 0.002,
                "fom": 0.0021 if valid else 3.4,
                "covariance": [[1e-6, 0, 0], [0, 1e-6, 0], [0, 0, 1e-7]],
                "altitude": round(alt, 4) if valid else -1.0,
                "transducers": beams, "velocity_valid": valid, "status": 0,
                "format": self.json_format, "type": "velocity",
                "time_of_validity": tov_us,
                "time_of_transmission": tov_us + 2500}

    def _velocity_loop(self) -> None:
        period = 1.0 / self.rate_hz
        due = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < due:
                time.sleep(min(0.01, due - now))
                continue
            due += period
            if now < self._pause_until:
                continue                       # the DVL makes nothing
            tov = _now_us()
            time_ms = (period * 1000.0 if self._last_made_us is None
                       else (tov - self._last_made_us) / 1000.0)
            self._last_made_us = tov
            valid = now >= self._invalid_until
            report = self.velocity_report(tov, round(time_ms, 3), valid)
            self.reports_made += 1
            line = (json.dumps(report) + "\r\n").encode()
            if self._drop > 0:
                self._drop -= 1                # made, never sent
                continue
            with self._lock:
                for q in self._ws_queues:
                    q.append({"channel": "velocity", "payload": {
                        "time": report["time"], "time_of_validity": tov,
                        "vx": report["vx"], "vy": report["vy"], "vz": report["vz"],
                        "std": report["fom"], "cov": report["covariance"],
                        "altitude": report["altitude"],
                        "transducers": [{**{k: b[k] for k in
                                            ("id", "velocity", "distance", "rssi", "nsd")},
                                         "is_valid": 1 if b["beam_valid"] else 0}
                                        for b in report["transducers"]],
                        "velocity_valid": valid,
                        "carrying_out_periodic_cycling": False,
                        "run_config": 1, "is_watertracking": False}})
            if now < self._stall_until:
                self._held.append(line)
                continue
            if self._held:
                held, self._held = self._held, []
                for h in held:
                    self._broadcast(h)
                    self.reports_sent += 1
            self._broadcast(line)
            self.reports_sent += 1

    def _position_loop(self) -> None:
        period = 1.0 / self.position_hz
        while not self.stop_event.wait(period):
            if time.monotonic() < self._pause_until:
                continue
            t = time.monotonic() - self._t0
            report = {"ts": round(t, 3), "x": 0.25 * t, "y": 0.0, "z": 3.2,
                      "std": 0.01 * t, "roll": 0.4, "pitch": -1.1,
                      "yaw": 12.0 + 0.1 * t, "type": "position_local",
                      "status": 0, "format": self.json_format}
            self._broadcast((json.dumps(report) + "\r\n").encode())

    # ------------------------------------------------------------------
    #  the web side
    # ------------------------------------------------------------------

    def _ws_loop(self) -> None:
        period = 1.0 / self.ws_hz
        while not self.stop_event.wait(period):
            t = time.monotonic() - self._t0
            msgs = [{"channel": "roll_pitch_yaw", "payload": [0.4, -1.1, 12 + 0.1 * t]},
                    {"channel": "position_local", "payload": [0.25 * t, 0.0, 3.2]},
                    {"channel": "position_local_std", "payload": 0.01 * t},
                    {"channel": "fusion_velocity", "payload": [0.25, -0.01, 0.002]},
                    {"channel": "fusion_velocity_std", "payload": 0.003}]
            with self._lock:
                for q in self._ws_queues:
                    q.extend(msgs)

    def echo_profile(self) -> dict:
        n, x_scale = 1024, 0.01
        peak = int(self.altitude * 1.08 / x_scale)
        data = []
        for k in range(n):
            row = []
            for b in range(4):
                v = -85 + self.rng.randint(-3, 3)
                d = abs(k - (peak + 3 * b))
                if d < 12:
                    v = max(v, -25 - 4 * d)
                row.append(v)
            data.append(row)
        return {"x_scale": x_scale, "y_scale": 1, "data": data}

    def spectrum(self) -> dict:
        data = [[-126 + self.rng.randint(-3, 3) + (30 if 60 <= k <= 68 else 0)
                 for _b in range(4)] for k in range(128)]
        return {"x_offset": 968.75, "x_scale": 0.48828125, "y_scale": 1,
                "data": data}

    def _get(self, path: str):
        if path == "/api/v1/about":
            return {"chipid": "0xsimulated", "hardware_revision": 4,
                    "is_ready": True, "product_id": 21035,
                    "product_name": "DVL A50", "variant": "simulated",
                    "version": f"{self.version} (simulator)",
                    "version_short": self.version}
        if path == "/api/v1/about/status":
            return {"cpu_load": 0.31, "disk_free": {"data": 1.2, "root": 120.5},
                    "temperature": self.temperature}
        if path == "/api/v1/warnings/":
            return ([{"id": "warnings:temperature_high", "message": "Hot",
                      "severity": "warning"}] if self.temperature >= 50 else [])
        if path == "/api/v1/outputs/":
            return [{"clients": self.clients, "error": "", "format": self.json_format,
                     "id": "tcp_water_linked", "name": "Water Linked protocol",
                     "port": str(self.tcp_port), "speed": 0, "type": "TCP"},
                    {"clients": 0, "error": "", "format": "PD6", "id": "tcp_pd6",
                     "name": "PD6 protocol", "port": "1037", "speed": 0, "type": "TCP"},
                    {"clients": 0, "error": "", "format": "PD4", "id": "tcp_pd4",
                     "name": "PD4 protocol", "port": "1038", "speed": 0, "type": "TCP"},
                    {"clients": -1, "error": "Serial protocol is disabled",
                     "format": "Disabled", "id": "serial", "name": "Serial protocol",
                     "port": "n/a", "speed": 0, "type": "Serial"}]
        if path == "/api/v1/config":
            return dict(self.config)
        if path == "/api/v1/time":
            return {"ntp_enabled": False, "ntp_server": "", "ntp_synchronized": False,
                    "time": rfc3339_ns()}
        if path == "/api/v1/ip":
            return {"address": "", "dhcp": True, "dns": "", "gateway": "", "prefix": 24}
        if path == "/api/v1/ip/current":
            return [{"address": "127.0.0.1", "dhcp": False, "dns": "",
                     "gateway": "", "prefix": 8}]
        if path == "/api/graph":
            return self.echo_profile()
        if path == "/api/spectrum":
            return self.spectrum()
        return None

    def _handler(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):          # quiet
                pass

            def _reply(self, code: int, body: bytes = b"",
                       ctype: str = "application/json", extra=None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                sim.requests.append(("http", "GET", self.path))
                if self.path == "/ws":
                    return self._websocket()
                if self.path.startswith("/api/collect"):
                    # The real format has not been seen; a zip stands in.
                    if not sim.collect_supported:
                        return self._reply(406, b"406 - Sorry not simulated",
                                           "text/plain; charset=utf-8")
                    body = b"PK\x03\x04" + b"simulated diagnostic log " * 40
                    return self._reply(
                        200, body, "application/zip",
                        {"Content-Disposition":
                         'attachment; filename="dvl-diagnostic-report.zip"'})
                body = sim._get(self.path.split("?", 1)[0])
                if body is None:
                    return self._reply(404, b'{"code":"not_found","status":404}')
                return self._reply(200, json.dumps(body).encode())

            def _refuse(self):
                sim.requests.append(("http", self.command, self.path))
                self._reply(405, b"")

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

            def _websocket(self):
                key = self.headers.get("Sec-WebSocket-Key", "")
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", W.accept_key(key))
                self.end_headers()
                self.wfile.flush()
                q: deque = deque()
                with sim._lock:
                    sim._ws_queues.append(q)
                sock = self.connection
                try:
                    while not sim.stop_event.is_set():
                        while q:
                            frame = W.encode_frame(
                                W.OP_TEXT, json.dumps(q.popleft()).encode(),
                                mask=False)
                            sock.sendall(frame)
                        time.sleep(0.01)
                except OSError:
                    pass
                finally:
                    with sim._lock:
                        if q in sim._ws_queues:
                            sim._ws_queues.remove(q)
                    self.close_connection = True

        return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="A simulated Water Linked DVL A50.")
    ap.add_argument("--tcp", type=int, default=16171, help="TCP JSON port")
    ap.add_argument("--http", type=int, default=8080, help="web port")
    ap.add_argument("--rate", type=float, default=10.0, help="velocity reports per second")
    args = ap.parse_args()
    sim = SimulatedDvl(tcp_port=args.tcp, http_port=args.http, rate_hz=args.rate).start()
    print(f"Simulated DVL: TCP {sim.tcp_port}, web {sim.http_port}. "
          f"Type 127.0.0.1:{sim.http_port} as the DVL address. Ctrl+C stops it.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
