"""Offline smoke tests -- parsers and pure logic, no network.

    python -m pytest docs/ROV_flight_plan/tests
"""
import datetime as dt

from flightplan.conditions import parse_window
from flightplan.render import tex_escape
from flightplan.sources._timeseries import parse_duration
from flightplan.sources.geo import compass_point, haversine_km
from flightplan.sources.waves import _parse_ndbc_realtime, _parse_ndbc_spec
from zoneinfo import ZoneInfo


def test_iso_duration():
    assert parse_duration("PT1H").total_seconds() == 3600
    assert parse_duration("P7D").days == 7
    assert parse_duration("P1DT6H").total_seconds() == 30 * 3600


def test_compass():
    assert compass_point(0) == "N"
    assert compass_point(90) == "E"
    assert compass_point(247.5) == "WSW"


def test_haversine_seattle_to_bodega():
    d = haversine_km(47.61, -122.35, 38.31, -123.05)
    assert 1000 < d < 1100


def test_window_parse_rolls_past_midnight():
    w = parse_window("22:00-02:00", dt.date(2026, 8, 29), "America/Los_Angeles")
    assert w.end > w.start
    assert (w.end - w.start).total_seconds() == 4 * 3600


def test_tex_escape():
    assert tex_escape("a & b_c 50%") == r"a \& b\_c 50\%"
    assert tex_escape("3–4 °C") == r"3--4 $^\circ$C"
    assert tex_escape(None) == ""


NDBC_RT = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 08 27 14 50 310 11.0 14.0   2.0    13   5.3 178 1014.2  13.7    MM  13.4   MM   MM    MM
"""

NDBC_SPEC = """#YY  MM DD hh mm WVHT  SwH  SwP  WWH  WWP SwD WWD  STEEPNESS  APD MWD
#yr  mo dy hr mn    m    m  sec    m  sec  -  degT     -      sec degT
2026 08 27 14 40  2.0  1.1 12.9  1.7  5.9   S  NW    AVERAGE  5.3 178
"""


def test_ndbc_realtime_parse():
    row = _parse_ndbc_realtime(NDBC_RT, ZoneInfo("UTC"))
    assert row is not None
    assert row["WVHT"] == 2.0
    assert row["DPD"] == 13.0
    assert row["WTMP"] is None  # "MM"


def test_ndbc_spec_parse_keeps_compass_strings():
    row = _parse_ndbc_spec(NDBC_SPEC, ZoneInfo("UTC"))
    assert row["SwH"] == "1.1"
    assert row["SwD"] == "S"
