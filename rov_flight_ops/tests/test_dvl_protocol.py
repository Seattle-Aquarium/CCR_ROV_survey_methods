"""
The DVL's formats: framing without changing a byte, and flattening without
losing a field.

The velocity report below is Water Linked's own example from the `json_v3.3`
protocol page, so a change in how it is flattened is a change against the
documented format, not against something invented here.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from rov_flight_ops.dvl import protocol as P

#: Water Linked's example velocity-and-transducer report (json_v3.3).
REPORT = {
    "time": 106.3935775756836, "vx": -3.713480691658333e-05,
    "vy": 5.703703573090024e-05, "vz": 2.4990416932269e-05,
    "fom": 0.00016016385052353144,
    "covariance": [[2.4471841442164077e-08, -3.3937477272871774e-09, -1.6659699175747278e-09],
                   [-3.3937477272871774e-09, 1.4654466085062268e-08, 4.0409570134514183e-10],
                   [-1.6659699175747278e-09, 4.0409570134514183e-10, 1.5971971523143225e-09]],
    "altitude": 0.4949815273284912,
    "transducers": [
        {"id": 0, "velocity": 0.00010825289791682735, "distance": 0.5568000078201294,
         "rssi": -30.494251251220703, "nsd": -88.73271179199219, "beam_valid": True},
        {"id": 1, "velocity": -1.4719001228513662e-05, "distance": 0.5663999915122986,
         "rssi": -31.095735549926758, "nsd": -89.5116958618164, "beam_valid": True},
        {"id": 2, "velocity": 2.7863150535267778e-05, "distance": 0.537600040435791,
         "rssi": -27.180519104003906, "nsd": -96.98075103759766, "beam_valid": True},
        {"id": 3, "velocity": 1.9419496311456896e-05, "distance": 0.5472000241279602,
         "rssi": -28.006759643554688, "nsd": -88.32147216796875, "beam_valid": True}],
    "velocity_valid": True, "status": 0, "format": "json_v3.3", "type": "velocity",
    "time_of_validity": 1638191471563017, "time_of_transmission": 1638191471752336,
}

POSITION = {"ts": 49056.809, "x": 12.43563613697886467, "y": 64.617631152402609587,
            "z": 1.767641898933798075, "std": 0.001959984190762043,
            "roll": 0.6173566579818726, "pitch": 0.6173566579818726,
            "yaw": 0.6173566579818726, "type": "position_local", "status": 0,
            "format": "json_v3.3"}


def _line(obj, term=b"\r\n") -> bytes:
    return json.dumps(obj).encode() + term


def _stream() -> bytes:
    return (_line(REPORT) + _line(POSITION) + _line(REPORT, b"\n")
            + b"\r\n" + b"not json at all\r\n" + b'{"partial": tru')


# --------------------------------------------------------------------------
#  framing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 7, 64, 1000, 100_000])
def test_every_byte_comes_back_whatever_the_reads_were(size):
    stream = _stream()
    sp = P.LineSplitter()
    lines = []
    for n, k in enumerate(range(0, len(stream), size), start=1):
        lines += sp.feed(stream[k:k + size], n)
    tail = sp.flush()
    assert b"".join(ln.data for ln in lines) + tail.data == stream
    for ln in lines + [tail]:
        assert stream[ln.offset:ln.offset + len(ln.data)] == ln.data
    assert tail.terminator == "" and tail.data == b'{"partial": tru'
    assert sp.flush() is None


def test_terminators_are_recorded_not_normalised():
    sp = P.LineSplitter()
    lines = sp.feed(_line(REPORT) + _line(POSITION, b"\n"), 1)
    assert [ln.terminator for ln in lines] == ["CRLF", "LF"]
    assert json.loads(lines[0].body) == REPORT
    assert json.loads(lines[1].body) == POSITION


def test_a_line_across_two_reads_is_stamped_with_the_read_that_finished_it():
    data = _line(REPORT)
    sp = P.LineSplitter()
    assert sp.feed(data[:100], 7) == []
    (line,) = sp.feed(data[100:], 8)
    assert (line.first_chunk, line.chunk) == (7, 8)


def test_offsets_carry_on_across_connections():
    sp = P.LineSplitter(offset=1000)
    (line,) = sp.feed(_line(POSITION), 1)
    assert line.offset == 1000
    assert sp.offset == 1000 + len(_line(POSITION))


def test_parsing_never_raises():
    assert P.parse(b"")[0] is None and P.parse(b"")[1] == "empty line"
    obj, err = P.parse(b"{not json")
    assert obj is None and err
    assert P.parse(b'{"a": NaN}')[0]["a"] != P.parse(b'{"a": NaN}')[0]["a"]


@pytest.mark.parametrize("obj, kind", [
    (REPORT, "velocity"),
    ({**REPORT, "type": "velocity_water"}, "velocity_water"),
    (POSITION, "position_local"),
    ({"response_to": "get_config", "type": "response"}, "response"),
    ({"response_to": "get_config"}, "response"),
    ({"something": 1}, "no_type"),
    ([1, 2], "not_an_object"),
    ({"type": "imu"}, "imu"),
])
def test_kinds(obj, kind):
    assert P.kind_of(obj) == kind


# --------------------------------------------------------------------------
#  flattening
# --------------------------------------------------------------------------


def test_every_documented_velocity_field_has_a_column():
    row = P.flatten_velocity(REPORT)
    assert set(row) == set(P.VELOCITY_REPORT_COLUMNS)
    assert row["time_ms"] == REPORT["time"]
    assert row["time_of_validity_us"] == REPORT["time_of_validity"]
    assert row["time_of_transmission_us"] == REPORT["time_of_transmission"]
    assert row["cov_xy"] == REPORT["covariance"][0][1]
    assert row["cov_zx"] == REPORT["covariance"][2][0]
    for t in REPORT["transducers"]:
        i = t["id"]
        assert row[f"t{i}_distance"] == t["distance"]
        assert row[f"t{i}_rssi"] == t["rssi"]
        assert row[f"t{i}_nsd"] == t["nsd"]
        assert row[f"t{i}_valid"] is True
        assert row[f"t{i}_snr_db"] == pytest.approx(t["rssi"] - t["nsd"])
    assert row["n_transducers"] == 4
    assert row["status_high_temperature"] is False
    assert row["extra_json"] == ""


def test_nothing_unexpected_is_dropped():
    odd = json.loads(json.dumps(REPORT))
    odd["new_in_3_4"] = {"a": 1}
    odd["transducers"][1]["new_beam_field"] = 9
    odd["transducers"].append({"id": 7, "distance": 1})       # an id the A50 lacks
    odd["transducers"].append({"id": 0, "distance": 2})       # a second id 0
    odd["transducers"].append({"distance": 3})                # no id at all
    row = P.flatten_velocity(odd)
    extra = json.loads(row["extra_json"])
    assert extra["new_in_3_4"] == {"a": 1}
    unplaced = extra["transducers_unplaced"]
    assert {"id": 1, "new_beam_field": 9} in unplaced
    assert {"id": 7, "distance": 1} in unplaced
    assert {"id": 0, "distance": 2} in unplaced
    assert {"distance": 3} in unplaced
    # The first id 0 still has its own columns.
    assert row["t0_distance"] == REPORT["transducers"][0]["distance"]


def test_a_missing_field_is_blank_not_zero():
    thin = {"type": "velocity", "velocity_valid": False, "time": 300}
    row = P.flatten_velocity(thin)
    assert row["altitude"] is None and P.cell(row["altitude"]) == ""
    assert row["t2_distance"] is None
    assert row["n_transducers"] is None


def test_cells_round_trip_through_csv_at_full_precision():
    values = [0.1 + 0.2, REPORT["vx"], 1638191471563017, True, False, None,
              float("nan")]
    buf = io.StringIO()
    csv.writer(buf).writerow([P.cell(v) for v in values])
    back = next(csv.reader(io.StringIO(buf.getvalue())))
    assert float(back[0]) == 0.1 + 0.2
    assert float(back[1]) == REPORT["vx"]
    assert int(back[2]) == 1638191471563017
    assert back[3:6] == ["1", "0", ""]
    assert back[6] == "nan"


@pytest.mark.parametrize("status, hot", [(0, False), (1, True), (2, False), (3, True)])
def test_only_bit_zero_is_the_temperature_warning(status, hot):
    assert P.flatten_velocity({**REPORT, "status": status})["status_high_temperature"] is hot


def test_dead_reckoning_flattens_whole():
    row = P.flatten_position({**POSITION, "added_later": 1})
    for k in ("ts", "x", "y", "z", "std", "roll", "pitch", "yaw", "status"):
        assert row[k] == POSITION[k]
    assert json.loads(row["extra_json"]) == {"added_later": 1}


def test_the_web_streams_velocity_keeps_its_own_fields():
    payload = {"time": 100, "time_of_validity": 1790900562479232, "vx": 0.56,
               "vy": 0.2, "vz": 2.07, "std": 0.0028, "cov": REPORT["covariance"],
               "altitude": 17.6,
               "transducers": [{"id": i, "velocity": 0.8, "distance": 5.3,
                                "rssi": -39.6, "nsd": -40.6, "is_valid": 1 - (i == 2)}
                               for i in range(4)],
               "velocity_valid": True, "carrying_out_periodic_cycling": False,
               "run_config": 1, "is_watertracking": False}
    row = P.flatten_ws_velocity(payload)
    assert set(row) == set(P.WS_VELOCITY_COLUMNS)
    assert row["run_config"] == 1
    assert row["carrying_out_periodic_cycling"] is False
    assert row["t2_valid"] is False and row["t1_valid"] is True
    assert row["std"] == 0.0028 and row["cov_yy"] == REPORT["covariance"][1][1]
    assert row["extra_json"] == ""


@pytest.mark.parametrize("payload, v0, v1, other", [
    ([0.4, -1.1, 12.0], 0.4, -1.1, ""),
    (0.25, 0.25, None, ""),
    ({"odd": True}, None, None, '{"odd":true}'),
])
def test_the_web_streams_other_channels(payload, v0, v1, other):
    row = P.flatten_ws_motion("roll_pitch_yaw", payload)
    assert (row["v0"], row["v1"], row["value_json"]) == (v0, v1, other)


# --------------------------------------------------------------------------
#  the beams
# --------------------------------------------------------------------------


def test_the_transducer_numbers_are_the_ids_plus_one():
    assert [(b.id, b.number) for b in P.BEAMS] == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_beam_positions_follow_the_drawing_and_the_mounting_offset():
    by_id = {b.id: b for b in P.BEAMS}
    assert [P.beam_position(by_id[i], 0) for i in range(4)] == [
        "aft-starboard", "aft-port", "forward-port", "forward-starboard"]
    # Mounted turned 90° clockwise, forward-starboard becomes aft-starboard.
    assert P.beam_position(by_id[3], 90) == "aft-starboard"
    assert P.beam_position(by_id[3], 45) == "starboard"
    assert P.beam_position(by_id[3], 10) == "55° from forward"
    assert P.beam_position(by_id[0], None) == "aft-starboard"
