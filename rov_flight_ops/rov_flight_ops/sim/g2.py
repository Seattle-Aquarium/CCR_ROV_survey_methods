"""
The simulated UGPS G2 topside, answering on localhost, tracking the simulated ROV.

The tests' `G2Simulator` with its acoustic solution taken from where the
simulated vehicle actually is, seen from the topside antenna on Pier 59. The
Navigation tab's receiver row reads it through the program's real UGPS
capture. The Navigation tab's UGPS faults -- a weak receiver, a noisy one, the
solution lost -- are applied here, where a real G2 would show them.
"""

from __future__ import annotations

import math

from ..nav import geo
from ..ugps.simulator import G2Simulator
from .schedule import NAV
from .world import TOPSIDE, TOPSIDE_HEADING, SimWorld

#: The four receivers, metres from the antenna (east, north, down), as the
#: simulator's own configuration places them.
RECEIVERS = ((-4.1, 4.3, 0.9), (-3.61, 4.3, 0.9), (-4.1, 4.3, 0.9), (-3.61, 4.3, 0.9))


class FieldG2(G2Simulator):
    def __init__(self, world: SimWorld, port: int = 0) -> None:
        super().__init__(port)
        self.world = world

    def _state(self):
        t = self.world.t()
        f, p = self.world.fault(NAV, t)
        return t, (f.key if f else ""), p

    def _acoustic(self) -> dict:
        t, key, p = self._state()
        tick = int(t * 2)                          # a new solution twice a second
        with self._lock:
            if tick == self._solution_at:
                return dict(self._solution)
            self._solution_at = tick
            v = self.world.vehicle(t)
            dist_m, bearing, _ = geo.inverse(TOPSIDE[0], TOPSIDE[1], v.lat, v.lon)
            east = dist_m * math.sin(math.radians(bearing))
            north = dist_m * math.cos(math.radians(bearing))
            weak = {2: 14.0} if key == "ugps_weak_receiver" else {}
            noisy = {1: 12.0} if key == "ugps_noisy_receiver" else {}
            dist, rssi, nsd, valid = [], [], [], []
            for i, (rx, ry, rz) in enumerate(RECEIVERS):
                d = math.dist((east, north, v.depth), (rx + 0.25 * i, ry, rz))
                dist.append(round(d, 3))
                level = -57.0 - 20.0 * math.log10(max(d, 1.0) / 10.0)
                rssi.append(round(level - weak.get(i, 0.0) + self._rng.gauss(0, 1.0), 2))
                nsd.append(round(-100.0 + noisy.get(i, 0.0) + self._rng.gauss(0, 0.9), 2))
                bad = 0.25 if i in weak or i in noisy else 0.02
                valid.append(0 if self._rng.random() < bad else 1)
            std = 0.42 + 0.08 * math.sin(t / 7.0) + (0.45 if weak or noisy else 0.0)
            self._solution = {"position_valid": True, "x": round(east, 3),
                              "y": round(north, 3), "z": round(v.depth, 3),
                              "std": round(std, 3), "receiver_distance": dist,
                              "receiver_rssi": rssi, "receiver_nsd": nsd,
                              "receiver_valid": valid}
            return dict(self._solution)

    def answer(self, path: str) -> tuple[int, object]:
        _t, key, p = self._state()
        lost = key == "acoustic_lost" or (key == "vessel_nmea_lost" and p * 30 >= 4)
        if path in ("/api/v1/position/acoustic/filtered",
                    "/api/v1/position/acoustic/raw") and lost:
            return 500, {"error": "no position"}
        if path == "/api/v1/position/global":
            if lost:
                return 500, {"error": "no position"}
            v = self.world.vehicle(self.world.t())
            return 200, {"lat": v.lat, "lon": v.lon, "cog": 0, "sog": 0, "hdop": 0.8,
                         "numsats": 22, "fix_quality": 1, "orientation": -1}
        if path == "/api/v1/position/master":
            nmea = key != "vessel_nmea_lost"
            hdt = nmea and key != "vessel_hdt_stale"
            return 200, {"lat": TOPSIDE[0] if nmea else 0.0,
                         "lon": TOPSIDE[1] if nmea else 0.0, "cog": 0, "sog": 0,
                         "hdop": 0.8 if nmea else 0, "numsats": 22 if nmea else 0,
                         "fix_quality": 1 if nmea else 0,
                         "orientation": TOPSIDE_HEADING if hdt else -1}
        if path == "/api/v1/about":
            return 200, {"product_name": "Underwater GPS G2 (simulated)",
                         "version": "3.3.1", "chipid": "0xsimulated"}
        return super().answer(path)
