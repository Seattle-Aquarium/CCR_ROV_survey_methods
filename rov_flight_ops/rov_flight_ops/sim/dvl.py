"""
The simulated DVL: a Water Linked A50 on localhost, flown by the simulated ROV.

`FieldDvl` is the tests' `SimulatedDvl` -- the real TCP JSON stream, web API,
web stream and acoustic views, served on ephemeral localhost ports -- with its
numbers taken from the 15 October 2026 dive instead of constants. The DVL
tab's own capture connects to it exactly as it would to 192.168.2.95, so every
judgement on that tab -- cadence, gap classes, validity spans, beam tiles,
delivery to MAVLink -- is the program's own, made about the simulated stream.

Calibrated from the capture of that dive (10:39 local, the vehicle a metre off
the bottom at about 14 m):

* reports every 108 ms at 1.1 m of altitude (9.2 Hz), 260 ms at 14 m;
* RSSI per beam about -38.6, -35.3, -35.0, -37.4 dBm at a metre, falling
  about 35 dB per decade of range; noise floors -97.2, -97.0, -100.0,
  -95.4 dBm; so SNR near 60 dB on the bottom and 13-25 dB from the surface;
* each beam's range over the altitude 1.14, 1.08, 1.06, 1.11;
* figure of merit 0.0002 typical, 0.0008 at the 95th percentile;
* the DVL's processing time (transmission less validity) about 109 ms;
* its clock 871 days behind -- software 2.7.2, never NTP-synced -- which the
  tab reports as it did that day;
* 19-20 °C inside, CPU load about 2.

The DVL tab's faults (`schedule.CATALOG["DVL"]`) are applied here, at the
stream, so they reach the tab the way a real one would.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time

from ..dvl import capture as CAP
from ..dvl.live import ChartPoint
from ..dvl.recorder import DvlRecorder
from ..dvl.simulator import SimulatedDvl, _now_us
from .nav import SIM_HOST, SimMav
from .schedule import DVL
from .world import SimWorld

log = logging.getLogger(__name__)

RSSI_1M = (-35.8, -32.5, -32.2, -34.6)
NSD = (-97.2, -97.0, -100.0, -95.4)
RANGE_RATIO = (1.14, 1.08, 1.06, 1.11)
#: Beam azimuths in the DVL's frame, for each beam's share of the velocity.
BEAM_AZ = (45.0, 135.0, 225.0, 315.0)
TILT = math.radians(22.5)
#: The A50's clock on 15 October 2026, against the laptop's.
CLOCK_OFFSET_S = -(871 * 86_400 + 2 * 3600 + 42 * 60)


def report_interval_ms(altitude: float) -> float:
    """The A50 pings slower the further it has to listen: 108 ms at 1.1 m,
    260 ms at 14 m on 15 October 2026."""
    return 95.0 + 11.8 * max(0.3, altitude)


def dvl_fault(world: SimWorld, t: float) -> tuple[str, float]:
    f, p = world.fault(DVL, t)
    return (f.key if f else ""), p


def extension_rate(world: SimWorld, t: float) -> tuple[float, bool]:
    """(valid reports a second reaching the extension, is the extension up).

    The stock extension forwards only valid reports, so this is what its
    MAVLink counters climb at.
    """
    key, _p = dvl_fault(world, t)
    v = world.vehicle(t)
    rate = 1000.0 / report_interval_ms(v.altitude)
    if key in ("bottom_lock_lost", "acoustics_off"):
        return 0.0, True
    if key == "extension_stopped":
        return 0.0, False
    if key == "snr_collapse":
        rate *= 0.45
    if key == "periodic_cycling":
        rate *= 0.9
    return rate, True


class FieldDvl(SimulatedDvl):
    """`SimulatedDvl`, flown by the simulated ROV and broken on schedule."""

    def __init__(self, world: SimWorld, *, seed: int = 15) -> None:
        super().__init__(rate_hz=9.2, position_hz=5.0, ws_hz=10.0, version="2.7.2",
                         altitude=1.0, seed=seed, clock_offset_s=CLOCK_OFFSET_S)
        self.world = world
        self.rng = random.Random(seed)
        self._rssi_wander = [0.0] * 4
        self._fom_wander = 0.0
        self._temp = 19.4
        self._temp_at = time.monotonic()
        self._dropping = 0
        self._dr = (0.0, 0.0)
        self._x0 = None

    # -- the state of the DVL right now ------------------------------------

    def _fault(self) -> tuple[str, float]:
        return dvl_fault(self.world, self.world.t())

    def current_config(self) -> dict:
        key, _p = self._fault()
        cfg = dict(self.config)
        cfg["acoustic_enabled"] = key != "acoustics_off"
        cfg["periodic_cycling_enabled"] = key == "periodic_cycling"
        return cfg

    def _temperature(self) -> float:
        now = time.monotonic()
        dt, self._temp_at = now - self._temp_at, now
        key, _p = self._fault()
        target = 55.0 if key == "dvl_hot" else 19.4
        tau = 6.0 if key == "dvl_hot" else 3.0
        self._temp += (target - self._temp) * min(1.0, dt / tau)
        self._temp += self.rng.gauss(0, 0.02)
        self.temperature = round(self._temp, 2)
        return self.temperature

    # -- the velocity report ---------------------------------------------------

    def velocity_report(self, tov_us: int, time_ms: float, valid: bool) -> dict:
        t = self.world.t()
        v = self.world.vehicle(t)
        key, p = self._fault()
        rng = self.rng
        alt = max(0.25, v.altitude)
        lock_lost = key == "bottom_lock_lost"
        beams = []
        dists = []
        velocity = (v.v_fwd, v.v_right, v.v_body_down)
        n_valid = 0
        for i in range(4):
            self._rssi_wander[i] += (0 - self._rssi_wander[i]) * 0.08 + rng.gauss(0, 0.9)
            tilt_bias = 0.015 * math.sin(math.radians(v.roll + v.pitch) * 3 + i)
            dist = alt * (RANGE_RATIO[i] + tilt_bias) + rng.gauss(0, 0.004)
            rssi = RSSI_1M[i] - 35.0 * math.log10(max(dist, 0.3)) + self._rssi_wander[i] \
                + rng.gauss(0, 1.6)
            nsd = NSD[i] + rng.gauss(0, 0.35)
            ok = True
            if lock_lost:
                dist, rssi, ok = -1.0, -102.0 + rng.gauss(0, 0.6), False
                nsd = -94.6 + rng.gauss(0, 0.4)
            elif key == "beam_dead" and i == 2:
                dist, rssi, ok = -1.0, -103.0 + rng.gauss(0, 0.5), False
            elif key == "snr_collapse":
                nsd += 41.0 + rng.gauss(0, 2.0)
                ok = (rssi - nsd) > 14.0 and rng.random() > 0.35
            elif key == "piling_multipath" and i == 0:
                dist = 0.36 + rng.gauss(0, 0.03)
                rssi = -19.0 + rng.gauss(0, 1.5)
            az = math.radians(BEAM_AZ[i])
            b = (math.sin(TILT) * math.cos(az), math.sin(TILT) * math.sin(az),
                 math.cos(TILT))
            bv = sum(a * c for a, c in zip(b, velocity, strict=True)) + rng.gauss(0, 0.004)
            if ok:
                n_valid += 1
                dists.append(dist)
            beams.append({"id": i, "velocity": round(bv, 5) if ok else 0.0,
                          "distance": round(dist, 4), "rssi": round(rssi, 3),
                          "nsd": round(nsd, 3), "beam_valid": ok})
        self._fom_wander += (0 - self._fom_wander) * 0.1 + rng.gauss(0, 0.12)
        fom = 0.00017 * (1 + alt / 10.0) * math.exp(self._fom_wander + rng.gauss(0, 0.35))
        valid = valid and n_valid >= 3 and not lock_lost
        if key == "beam_dead":
            fom *= 3.0
        elif key == "snr_collapse":
            fom *= 12.0
        elif key == "piling_multipath":
            fom *= 6.0
            if rng.random() < 0.12:
                valid = False
        if not valid:
            fom = 10.0
        altitude = (sum(dists) / len(dists) * math.cos(TILT)) if dists and valid else -1.0
        vx, vy, vz = (velocity if valid else (0.0, 0.0, 0.0))
        if valid:
            vx += rng.gauss(0, 0.004)
            vy += rng.gauss(0, 0.004)
            vz += rng.gauss(0, 0.003)
        processing_us = int((109.0 + 4.0 * alt + rng.gauss(0, 6)) * 1000)
        dvl_now = self.clock_ns() // 1000
        cov = fom * fom
        status = 1 if self._temperature() >= 50.0 else 0
        return {"time": time_ms, "vx": round(vx, 6), "vy": round(vy, 6),
                "vz": round(vz, 6), "fom": round(fom, 8),
                "covariance": [[cov, cov * 0.12, cov * 0.05],
                               [cov * 0.12, cov * 0.97, -cov * 0.02],
                               [cov * 0.05, -cov * 0.02, cov * 0.09]],
                "altitude": round(altitude, 4), "transducers": beams,
                "velocity_valid": bool(valid), "status": status,
                "format": self.json_format, "type": "velocity",
                "time_of_validity": dvl_now - processing_us,
                "time_of_transmission": dvl_now - 2000}

    def _velocity_loop(self) -> None:
        """Reports at the A50's own cadence, with the stream faults applied."""
        due = time.monotonic()
        held_until = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < due:
                time.sleep(min(0.01, due - now))
                continue
            t = self.world.t()
            v = self.world.vehicle(t)
            period = report_interval_ms(v.altitude) / 1000.0 * (1 + self.rng.gauss(0, 0.012))
            due += period
            if due < now - 1.0:
                due = now + period                 # a stall in this process
            key, p = dvl_fault(self.world, t)
            into = p * 30.0
            if key == "acoustics_off":
                self._acoustics_were_off = True
                continue
            if key == "periodic_cycling" and (into % 10.0) < 1.1:
                continue
            sent_us = _now_us()
            time_ms = (period * 1000.0 if self._last_made_us is None
                       else (sent_us - self._last_made_us) / 1000.0)
            self._acoustics_were_off = False
            self._last_made_us = sent_us
            report = self.velocity_report(0, round(time_ms, 3), True)
            self.reports_made += 1
            self.world.dvl_reports += 1
            if report["velocity_valid"]:
                self.world.dvl_valid_reports += 1
            line = (json.dumps(report) + "\r\n").encode()
            self._queue_ws(report, key, into)
            if key == "missing_reports":
                if self._dropping > 0:
                    self._dropping -= 1
                    continue
                if self.rng.random() < 0.08:
                    self._dropping = self.rng.randint(1, 4)
                    continue
            if key == "delivery_stall":
                cyc = into % 6.0
                if 1.0 <= cyc < 3.6:
                    held_until = now + 0.1
                    self._held.append(line)
                    continue
            if self._held and now >= held_until:
                held, self._held = self._held, []
                for h in held:
                    self._broadcast(h)
                    self.reports_sent += 1
            self._broadcast(line)
            self.reports_sent += 1

    def _queue_ws(self, report: dict, key: str, into: float) -> None:
        cycling = key == "periodic_cycling" and (into % 10.0) < 1.6
        with self._lock:
            for q in self._ws_queues:
                q.append({"channel": "velocity", "payload": {
                    "time": report["time"],
                    "time_of_validity": report["time_of_validity"],
                    "vx": report["vx"], "vy": report["vy"], "vz": report["vz"],
                    "std": report["fom"], "cov": report["covariance"],
                    "altitude": report["altitude"],
                    "transducers": [{**{k: b[k] for k in
                                        ("id", "velocity", "distance", "rssi", "nsd")},
                                     "is_valid": 1 if b["beam_valid"] else 0}
                                    for b in report["transducers"]],
                    "velocity_valid": report["velocity_valid"],
                    "carrying_out_periodic_cycling": cycling,
                    "run_config": 1, "is_watertracking": False}})

    def _position_loop(self) -> None:
        period = 1.0 / self.position_hz
        while not self.stop_event.wait(period):
            t = self.world.t()
            key, _p = dvl_fault(self.world, t)
            if key == "acoustics_off":
                continue
            v = self.world.vehicle(t)
            if self._x0 is None:
                self._x0 = (v.north, v.east, t)
            x, y = v.north - self._x0[0], v.east - self._x0[1]
            report = {"ts": round(self.clock_ns() / 1e9, 5), "x": round(x, 4),
                      "y": round(y, 4), "z": round(v.depth, 4),
                      "std": round(0.002 * (t - self._x0[2]), 4),
                      "roll": round(v.roll, 3), "pitch": round(v.pitch, 3),
                      "yaw": round(v.heading, 3), "type": "position_local",
                      "status": 0, "format": self.json_format}
            self._broadcast((json.dumps(report) + "\r\n").encode())

    def _ws_loop(self) -> None:
        period = 1.0 / self.ws_hz
        while not self.stop_event.wait(period):
            v = self.world.vehicle(self.world.t())
            x0 = self._x0 or (v.north, v.east, 0.0)
            msgs = [{"channel": "roll_pitch_yaw", "payload": [v.roll, v.pitch, v.heading]},
                    {"channel": "position_local",
                     "payload": [v.north - x0[0], v.east - x0[1], v.depth]},
                    {"channel": "position_local_std", "payload": 0.05},
                    {"channel": "fusion_velocity",
                     "payload": [v.v_fwd, v.v_right, v.v_body_down]},
                    {"channel": "fusion_velocity_std", "payload": 0.003}]
            with self._lock:
                for q in self._ws_queues:
                    q.extend(msgs)

    # -- the acoustic views -------------------------------------------------------

    def echo_profile(self) -> dict:
        """256 range bins per beam, shaped like the dive's: ringing near the
        face, the bottom echo at each beam's range, a -100 dBm floor."""
        t = self.world.t()
        v = self.world.vehicle(t)
        key, _p = self._fault()
        rng = self.rng
        alt = max(0.3, v.altitude)
        x_scale = max(0.0118, alt * 2.75 / 256)
        noise_up = 38.0 if key == "snr_collapse" else 0.0
        rows = []
        peaks = []
        for i in range(4):
            if key in ("bottom_lock_lost", "acoustics_off") or (key == "beam_dead" and i == 2):
                peaks.append(None)
            elif key == "piling_multipath" and i == 0:
                peaks.append((0.36, -16.0))
            else:
                d = alt * RANGE_RATIO[i]
                peaks.append((d, RSSI_1M[i] - 35.0 * math.log10(d) - 4.0))
        for k in range(256):
            r = k * x_scale
            row = []
            for i in range(4):
                val = -101.0 + noise_up + rng.gauss(0, 5.5)
                if k < 16 and key != "acoustics_off":
                    val = max(val, -60.0 - 2.2 * k + rng.gauss(0, 3))
                pk = peaks[i]
                if pk is not None:
                    dd = abs(r - pk[0]) / x_scale
                    if dd < 14:
                        val = max(val, pk[1] - 3.2 * dd + rng.gauss(0, 2))
                row.append(int(round(val)))
            rows.append(row)
        return {"x_scale": round(x_scale, 6), "y_scale": 1, "data": rows}

    def spectrum(self) -> dict:
        """128 bins around the 1 MHz carrier, in milli-dBm, as the A50 serves it."""
        key, _p = self._fault()
        rng = self.rng
        up = 40_000 if key == "snr_collapse" else 0
        data = []
        for k in range(128):
            row = []
            for _b in range(4):
                v = -101_000 + up + rng.gauss(0, 5_200)
                if key == "snr_collapse" and k % 21 in (3, 4):
                    v += 9_000              # the interference's own harmonics
                row.append(int(v))
            data.append(row)
        return {"x_offset": 968.75, "x_scale": 0.48828125, "y_scale": 0.001,
                "data": data}

    def _get(self, path: str):
        if path == "/api/v1/about/status":
            return {"cpu_load": round(2.0 + self.rng.gauss(0, 0.25), 2),
                    "disk_free": {"data": 2.46, "root": 0.211},
                    "temperature": self._temperature()}
        if path == "/api/v1/warnings/":
            return ([{"id": "warnings:temperature_high",
                      "message": "Temperature is high", "severity": "warning"}]
                    if self._temperature() >= 50.0 else [])
        if path == "/api/v1/about":
            body = super()._get(path)
            body.update(variant="performance", chipid="0xf1419220ee000d",
                        version="2.7.2 (simulated)")
            return body
        return super()._get(path)


# --------------------------------------------------------------------------
#  the capture and its handle
# --------------------------------------------------------------------------


class SimDvlCapture(CAP.DvlCapture):
    """The DVL tab's own capture, live only, with the vehicle side simulated.

    The DVL is reached over localhost like a real one. What the vehicle says
    -- the BlueOS extension's status, and mavlink2rest's counts of what the
    extension sent -- comes from the simulated vehicle.
    """

    EXTENSION_RETRY_S = 2.0

    def __init__(self, world: SimWorld, **kw) -> None:
        super().__init__(**kw)
        self.world = world

    def _mavlink(self, host: str, system: int, component: int):
        return SimMav(self.world, system=system, component=component, timeout=2.0)

    def _read_extension(self, session, host: str):
        t = self.world.t()
        _rate, running = extension_rate(self.world, t)
        if self.world.pi_silent(t) or not running:
            return session, None, {"ext_reachable": False}
        if self.extension.get("port") is None:
            self.extension = {**self.extension, "name": "Water Linked DVL",
                              "port": 32768, "found_via": "helper", "note": ""}
        return session, None, {
            "ext_reachable": True, "ext_status": "Running", "ext_enabled": True,
            "ext_should_send": "POSITION_DELTA", "ext_rangefinder": True,
            "ext_orientation": 1, "ext_hostname": "192.168.2.95"}


class SimDvlRecorder(DvlRecorder):
    """`DvlRecorder` over the simulated DVL. Never writes: always a live view."""

    def __init__(self, world: SimWorld, dvl: FieldDvl, **kw) -> None:
        super().__init__(vehicle_host=SIM_HOST, dvl_host=dvl.address, **kw)
        self.world = world
        self.device = dvl

    def _restart(self, folder) -> None:
        if self._closed:
            return
        cap = self.capture
        if cap is not None and cap.running:
            return
        self._stop_current()
        cap = SimDvlCapture(self.world, folder=None, vehicle_host=SIM_HOST,
                            dvl_override=self.device.address,
                            snapshot_hz=self.snapshot_hz, flight_id=self.flight_id,
                            live=self.live, vehicle=True)
        self.capture = cap
        cap.start()
        backfill(self.world, self.live)

    def set_dvl_host(self, text: str) -> None:
        """The simulated DVL is where it is; a typed address is kept for later."""
        self.dvl_host = self.device.address

    def set_vehicle_host(self, host: str) -> None:
        self.vehicle_host = SIM_HOST


def backfill(world: SimWorld, live, seconds: float = 600.0) -> None:
    """The last ten minutes of chart history, so the charts open full.

    Only the charts: the stream statistics -- rates, gaps, validity spans --
    are the capture's own, counted from the moment it connected. Put in
    front of whatever the live stream has added meanwhile, in one step,
    because the page reads the history newest-first.
    """
    rng = random.Random(4)
    now_t = world.t()
    t = max(0.0, now_t - seconds)
    prev = None
    points, temps = [], []
    while t < now_t - 0.3:
        v = world.vehicle(t)
        period = report_interval_ms(v.altitude)
        alt = max(0.25, v.altitude)
        dist = tuple(round(alt * RANGE_RATIO[i] + rng.gauss(0, 0.004), 4) for i in range(4))
        rssi = tuple(round(RSSI_1M[i] - 35.0 * math.log10(dist[i]) + rng.gauss(0, 2.0), 2)
                     for i in range(4))
        nsd = tuple(round(NSD[i] + rng.gauss(0, 0.35), 2) for i in range(4))
        fom = 0.00017 * (1 + alt / 10.0) * math.exp(rng.gauss(0, 0.45))
        d = period * (1 + rng.gauss(0, 0.012))
        points.append(ChartPoint(
            t=world.wall(t), altitude=sum(dist) / 4 * math.cos(TILT), distance=dist,
            rssi=rssi, nsd=nsd, valid=True, fom=fom,
            d_tov_ms=round(d, 3), d_rx_ms=round(d + rng.gauss(0, 9), 1)))
        if prev is None or t - prev >= 2.0:
            temps.append((world.wall(t), 19.4 + rng.gauss(0, 0.05)))
            prev = t
        t += d / 1000.0
    live.prepend(points, temps)
