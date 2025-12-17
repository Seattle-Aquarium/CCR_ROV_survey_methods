import sys
from pathlib import Path

import pytest

# Ensure we can import code/DVL_meter_mark.py even if `code/` is not a package.
CODE_DIR = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE_DIR))

import DVL_meter_mark


def test_empty_positions_returns_empty_list():
    assert DVL_meter_mark.positions_to_meter_records([]) == []


def test_single_position_returns_empty_list():
    positions = [(0.0, 0.0, 0.0)]
    assert DVL_meter_mark.positions_to_meter_records(positions) == []


def test_exactly_one_meter_creates_one_record_and_formats_timestamp(capsys):
    # Epoch 0 in UTC = 1969-12-31 16:00:00 in US/Pacific (PST)
    positions = [
        (0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
    ]

    records = DVL_meter_mark.positions_to_meter_records(positions)
    assert len(records) == 1

    r = records[0]
    assert r["meter_number"] == 1
    assert r["timestamp"] == "1969_12_31_16-00-00"
    assert r["cumulative_dist"] == pytest.approx(1.0)
    assert r["increment"] == pytest.approx(1.0)
    assert (r["x"], r["y"]) == (1.0, 0.0)

    # avoid failing due to debug prints if we later assert on output
    capsys.readouterr()


def test_crossing_multiple_meters_in_single_step_emits_multiple_records_same_sample(capsys):
    positions = [
        (0.0, 0.0, 0.0),
        (0.0, 3.2, 0.0),  # crosses meters 1, 2, 3 at once
    ]

    records = DVL_meter_mark.positions_to_meter_records(positions)
    assert [r["meter_number"] for r in records] == [1, 2, 3]

    # Current behavior: all records use the same cumulative distance (the sample’s cumulative),
    # and "increment" is computed as (cumulative_distance - previous_distance) where
    # previous_distance is then set to cumulative_distance each time through the loop.
    assert records[0]["cumulative_dist"] == pytest.approx(3.2)
    assert records[1]["cumulative_dist"] == pytest.approx(3.2)
    assert records[2]["cumulative_dist"] == pytest.approx(3.2)

    assert records[0]["increment"] == pytest.approx(3.2)
    assert records[1]["increment"] == pytest.approx(0.0)
    assert records[2]["increment"] == pytest.approx(0.0)

    for r in records:
        assert r["timestamp"] == "1969_12_31_16-00-00"
        assert (r["x"], r["y"]) == (3.2, 0.0)

    capsys.readouterr()


def test_meter_is_not_emitted_until_threshold_is_reached(capsys):
    positions = [
        (0.0, 0.0, 0.0),
        (0.0, 0.6, 0.0),  # cumulative 0.6 -> no meter
        (0.0, 1.2, 0.0),  # cumulative 1.2 -> meter 1
    ]

    records = DVL_meter_mark.positions_to_meter_records(positions)
    assert len(records) == 1
    assert records[0]["meter_number"] == 1
    assert records[0]["cumulative_dist"] == pytest.approx(1.2)
    assert records[0]["increment"] == pytest.approx(1.2)
    assert (records[0]["x"], records[0]["y"]) == (1.2, 0.0)

    capsys.readouterr()


def test_meter_numbers_are_strictly_increasing(capsys):
    positions = [
        (0.0, 0.0, 0.0),
        (0.0, 0.9, 0.0),
        (0.0, 1.1, 0.0),  # meter 1
        (0.0, 2.05, 0.0),  # meter 2
        (0.0, 3.01, 0.0),  # meter 3
    ]

    records = DVL_meter_mark.positions_to_meter_records(positions)
    meter_numbers = [r["meter_number"] for r in records]

    assert meter_numbers == sorted(meter_numbers)
    assert meter_numbers == list(range(1, len(meter_numbers) + 1))

    capsys.readouterr()


def test_10_records(capsys):
    # Make sure the first 10 meter records data/meter_tape.tlog are the same, so any changes will be caught.

    meter_records = [
        {'meter_number': 1, 'timestamp': '2024_06_12_10-01-04', 'cumulative_dist': 1.0067526062782255, 'increment': 1.0067526062782255, 'x': -0.5483508706092834, 'y': 0.029338378459215164},
        {'meter_number': 2, 'timestamp': '2024_06_12_10-01-32', 'cumulative_dist': 2.0000103761784627, 'increment': 0.9932577699002372, 'x': -0.47780612111091614, 'y': -0.19961276650428772},
        {'meter_number': 3, 'timestamp': '2024_06_12_10-02-07', 'cumulative_dist': 3.0020515613100276, 'increment': 1.002041185131565, 'x': 0.1642475128173828, 'y': -0.32270368933677673},
        {'meter_number': 4, 'timestamp': '2024_06_12_10-02-14', 'cumulative_dist': 4.001327228380055, 'increment': 0.9992756670700276, 'x': 0.07753124833106995, 'y': -0.8646377325057983},
        {'meter_number': 5, 'timestamp': '2024_06_12_10-02-41', 'cumulative_dist': 5.012854583679196, 'increment': 1.0115273552991404, 'x': 0.4160335659980774, 'y': -1.0840703248977661},
        {'meter_number': 6, 'timestamp': '2024_06_12_10-03-03', 'cumulative_dist': 6.0003053643489555, 'increment': 0.9874507806697599, 'x': 0.891474187374115, 'y': -0.9848182797431946},
        {'meter_number': 7, 'timestamp': '2024_06_12_10-03-26', 'cumulative_dist': 7.016627968002212, 'increment': 1.0163226036532569, 'x': 1.1472258567810059, 'y': -1.3462392091751099},
        {'meter_number': 8, 'timestamp': '2024_06_12_10-03-48', 'cumulative_dist': 8.005500609729596, 'increment': 0.9888726417273839, 'x': 0.527729332447052, 'y': -1.2473492622375488},
        {'meter_number': 9, 'timestamp': '2024_06_12_10-04-07', 'cumulative_dist': 9.007809704872463, 'increment': 1.0023090951428664, 'x': -0.26831647753715515, 'y': -0.8096368312835693},
        {'meter_number': 10, 'timestamp': '2024_06_12_10-04-27', 'cumulative_dist': 10.034124873173088, 'increment': 1.0263151683006253, 'x': -1.1126914024353027, 'y': -0.44728943705558777}
    ]

    positions = DVL_meter_mark.process_tlog("data/meter_tape.tlog")
    test_records = DVL_meter_mark.positions_to_meter_records(positions)

    # Verify the records match
    assert test_records[:10] == meter_records

    capsys.readouterr()
