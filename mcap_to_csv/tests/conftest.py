"""Synthetic .mcap recordings, so the tests do not need a multi-gigabyte dive."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcap.writer import Writer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 2026-08-26 10:00:00 US/Pacific == 17:00:00 UTC.
BASE_EPOCH = 1787763600


class McapBuilder:
    """Writes MAVLink-over-JSON exactly the way the BlueOS recorder does."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = open(self.path, "wb")
        self._w = Writer(self._fh)
        self._w.start(profile="", library="mcap-test")
        self._channels: dict[str, int] = {}
        self._seq = 0

    def _channel(self, topic: str) -> int:
        if topic not in self._channels:
            name = "mavlink." + topic.split("/", 1)[1].replace("/", ".")
            schema_id = self._w.register_schema(
                name=name, encoding="jsonschema", data=b'{"type":"object"}')
            self._channels[topic] = self._w.register_channel(
                topic=topic, message_encoding="json", schema_id=schema_id)
        return self._channels[topic]

    def add(self, t: float, msg_type: str, fields: dict,
            sysid: int = 1, compid: int = 1) -> McapBuilder:
        topic = f"mavlink/{sysid}/{compid}/{msg_type}"
        payload = {
            "header": {"system_id": sysid, "component_id": compid,
                       "sequence": self._seq % 256, "message_id": 0},
            "message": {"type": msg_type, **fields},
        }
        self._seq += 1
        ns = int(t * 1e9)
        self._w.add_message(self._channel(topic), log_time=ns,
                            data=json.dumps(payload).encode(), publish_time=ns)
        return self

    def close(self) -> Path:
        self._w.finish()
        self._fh.close()
        return self.path


@pytest.fixture
def builder(tmp_path):
    made: list[McapBuilder] = []

    def make(name: str = "rec.mcap") -> McapBuilder:
        b = McapBuilder(tmp_path / name)
        made.append(b)
        return b

    yield make
    for b in made:
        if not b._fh.closed:
            b.close()


def straight_north_dive(b: McapBuilder, *, seconds: int = 20,
                        start: float = BASE_EPOCH) -> McapBuilder:
    """A dive heading due north at 0.5 m/s, 2 m off the bottom, 5 m deep.

    Every message the reader looks for is present, at plausible rates, so a test
    can assert on whole rows rather than on one field in isolation.
    """
    for i in range(seconds):
        t = start + i
        for k in range(10):                      # ATTITUDE at 10 Hz
            b.add(t + k / 10, "ATTITUDE",
                  {"roll": 0.0, "pitch": 0.0, "yaw": 0.0,
                   "rollspeed": 0.0, "pitchspeed": 0.0, "yawspeed": 0.0})
        for k in range(5):                       # DVL at 5 Hz, 0.1 m per step
            b.add(t + k / 5, "VISION_POSITION_DELTA",
                  {"time_delta_usec": 200000, "angle_delta": [0, 0, 0],
                   "position_delta": [0.1, 0.0, 0.0], "confidence": 99.0},
                  sysid=255, compid=0)
        b.add(t, "GPS_RAW_INT",
              {"lat": 476176249, "lon": -1223610207, "alt": 0, "eph": 100,
               "fix_type": {"type": "GPS_FIX_TYPE_RTK_FIXED"},
               "satellites_visible": 12})
        b.add(t, "GLOBAL_POSITION_INT",
              {"lat": 476176249, "lon": -1223610207, "alt": 0,
               "relative_alt": -5000, "vx": 0, "vy": 0, "vz": 0, "hdg": 0})
        b.add(t, "RANGEFINDER", {"distance": 2.0, "voltage": 0})
        b.add(t, "VFR_HUD", {"airspeed": 0, "groundspeed": 0, "alt": 0,
                             "climb": 0, "heading": 0, "throttle": 50})
        b.add(t, "SCALED_PRESSURE2",
              {"press_abs": 1013.25 + 5 * 100.53, "temperature": 1200})
        b.add(t, "HEARTBEAT", {"custom_mode": 19, "base_mode": ""})
        b.add(t, "BATTERY_STATUS",
              {"voltages": [14000] + [65535] * 9, "current_battery": 200,
               "current_consumed": 10 * i, "energy_consumed": 5 * i})
        b.add(t, "NAMED_VALUE_FLOAT", {"name": "Lights1", "value": 0.5})
    return b
