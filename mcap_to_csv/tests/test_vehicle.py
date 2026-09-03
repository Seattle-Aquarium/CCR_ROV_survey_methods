"""What the vehicle was, and how it was configured."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from conftest import BASE_EPOCH
from ccr_m2c.vehicle import Vehicle, _fmt, read_vehicle


def _params(b, pairs, total=1038, seconds=1):
    for i, (name, value) in enumerate(pairs):
        b.add(BASE_EPOCH + i // 20, "PARAM_VALUE",
              {"param_id": name, "param_value": value, "param_count": total,
               "param_index": i, "param_type": {"type": "MAV_PARAM_TYPE_REAL32"}})
    return b


def test_parameters_come_through_with_their_total(builder):
    b = _params(builder(), [("RNGFND1_TYPE", 10), ("RNGFND1_MIN_CM", 20)])
    v = read_vehicle([b.close()])

    assert v.params == {"RNGFND1_TYPE": 10, "RNGFND1_MIN_CM": 20}
    assert v.param_total == 1038
    assert not v.params_complete


def test_a_partial_download_says_so(builder):
    """Otherwise six parameters look like the whole configuration."""
    b = _params(builder(), [("RNGFND1_TYPE", 10)])
    text = "\n".join(read_vehicle([b.close()]).lines())

    assert "1 of the vehicle's 1038 were captured" in text
    assert "partial download" in text


def test_a_complete_set_does_not_cry_partial(builder):
    b = _params(builder(), [("A", 1), ("B", 2)], total=2)
    text = "\n".join(read_vehicle([b.close()]).lines())

    assert "all 2" in text
    assert "partial" not in text


def test_the_firmware_version_is_unpacked(builder):
    b = builder()
    # ArduPilot packs this as major<<24 | minor<<16 | patch<<8 | release type
    b.add(BASE_EPOCH, "AUTOPILOT_VERSION",
          {"flight_sw_version": (4 << 24) | (5 << 16) | (0 << 8) | 255,
           "flight_custom_version": [48, 51, 99, 49, 50, 54, 57, 56]})
    v = read_vehicle([b.close()])

    assert v.ardusub == "4.5.0"
    assert v.ardusub_build == "03c12698"


def test_a_missing_version_explains_why(builder):
    """Absent means nobody asked for it, not that the firmware is unknown."""
    b = _params(builder(), [("RNGFND1_TYPE", 10)])
    text = "\n".join(read_vehicle([b.close()]).lines())

    assert "ArduSub          not recorded" in text
    assert "reply to a ground station request" in text


def test_blueos_is_reported_as_not_recorded(builder):
    """It is genuinely absent from the log; guessing it from Debian would be
    worse than saying so."""
    b = _params(builder(), [("A", 1)])
    text = "\n".join(read_vehicle([b.close()]).lines())
    assert "BlueOS           not recorded" in text


def test_the_board_and_os_come_from_the_service_logs(builder):
    b = _params(builder(), [("A", 1)])
    b.add(BASE_EPOCH, "PARAM_VALUE",
          {"param_id": "B", "param_value": 2, "param_count": 2, "param_index": 1,
           "param_type": {"type": "MAV_PARAM_TYPE_REAL32"}})
    path = b.close()

    v = read_vehicle([path])
    v.board, v.os_name, v.kernel = "Raspberry Pi 4 B (BCM2711)", "Debian GNU/Linux 12", "5.10.33"
    text = "\n".join(v.lines())
    assert "Raspberry Pi 4 B" in text
    assert "Debian GNU/Linux 12, kernel 5.10.33" in text


# --- the .BIN path ----------------------------------------------------------

def _fake_pymavlink(records):
    """Stand in for pymavlink, which is an optional dependency here."""
    class Conn:
        def __init__(self):
            self._left = list(records)

        def recv_match(self, type=None):        # noqa: A002 - pymavlink's name
            while self._left:
                r = self._left.pop(0)
                if type is None or r.get_type() in type:
                    return r
            return None

    return SimpleNamespace(mavutil=SimpleNamespace(
        mavlink_connection=lambda *_a, **_k: Conn()))


def _rec(kind, **fields):
    return SimpleNamespace(get_type=lambda k=kind: k, **fields)


def test_a_bin_supplies_the_whole_parameter_set(monkeypatch, tmp_path):
    bin_path = tmp_path / "00000170.BIN"
    bin_path.write_bytes(b"not really parsed - pymavlink is stubbed")

    monkeypatch.setitem(sys.modules, "pymavlink", _fake_pymavlink([
        _rec("VER", Maj=4, Min=5, Pat=0, FWS="ArduSub V4.5.0 (03c12698)"),
        _rec("PARM", Name="RNGFND1_TYPE", Value=10.0),
        _rec("PARM", Name="RNGFND1_MIN_CM", Value=20.0),
    ]))

    v = read_vehicle([bin_path])

    assert v.ardusub == "4.5.0"
    assert v.ardusub_build == "03c12698"
    assert v.params == {"RNGFND1_TYPE": 10.0, "RNGFND1_MIN_CM": 20.0}
    # a dataflash log carries every parameter by definition
    assert v.params_complete
    assert "partial" not in "\n".join(v.lines())


def test_older_firmware_is_read_from_the_boot_banner(monkeypatch, tmp_path):
    """Before the VER record, the version was only in a MSG line."""
    bin_path = tmp_path / "old.BIN"
    bin_path.write_bytes(b"stub")
    monkeypatch.setitem(sys.modules, "pymavlink", _fake_pymavlink([
        _rec("MSG", Message="ArduSub V4.1.0 (abcd1234)"),
        _rec("PARM", Name="A", Value=1.0),
    ]))

    v = read_vehicle([bin_path])
    assert v.ardusub == "4.1.0"
    assert v.ardusub_build == "abcd1234"


def test_a_bin_and_an_mcap_together(monkeypatch, tmp_path, builder):
    """The .BIN wins on parameters; the mcap still contributes its own side."""
    bin_path = tmp_path / "x.BIN"
    bin_path.write_bytes(b"stub")
    monkeypatch.setitem(sys.modules, "pymavlink", _fake_pymavlink([
        _rec("VER", Maj=4, Min=5, Pat=0, FWS="ArduSub V4.5.0 (deadbeef)"),
        _rec("PARM", Name="RNGFND1_TYPE", Value=10.0),
    ]))
    mcap = _params(builder(), [("SOMETHING_ELSE", 7)]).close()

    v = read_vehicle([bin_path, mcap])

    assert v.ardusub == "4.5.0"
    assert v.params["RNGFND1_TYPE"] == 10.0
    assert v.params["SOMETHING_ELSE"] == 7     # the mcap filled what it had


def test_without_pymavlink_a_bin_is_skipped_with_advice(monkeypatch, tmp_path, builder):
    bin_path = tmp_path / "x.BIN"
    bin_path.write_bytes(b"stub")

    real_import = __import__

    def blocked(name, *a, **k):
        if name.startswith("pymavlink"):
            raise ImportError("no pymavlink")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", blocked)
    mcap = _params(builder(), [("A", 1)]).close()

    v = read_vehicle([bin_path, mcap])

    assert any("pymavlink is not installed" in w for w in v.warnings)
    assert v.params == {"A": 1}                # the mcap still worked


# --- presentation -----------------------------------------------------------

def test_grep_narrows_the_listing(builder):
    b = _params(builder(), [("RNGFND1_TYPE", 10), ("BATT_CAPACITY", 18000)])
    v = read_vehicle([b.close()])

    text = "\n".join(v.lines(grep="rngfnd"))     # case-insensitive
    assert "RNGFND1_TYPE" in text
    assert "BATT_CAPACITY" not in text


def test_full_shows_everything_including_the_unnotable(builder):
    b = _params(builder(), [("RNGFND1_TYPE", 10), ("ZZ_OBSCURE", 3)])
    v = read_vehicle([b.close()])

    assert "ZZ_OBSCURE" not in "\n".join(v.lines())
    assert "ZZ_OBSCURE" in "\n".join(v.lines(full=True))


def test_whole_numbers_print_as_integers():
    """RNGFND1_TYPE is a type code; showing it as 10.0 invites a wrong edit."""
    assert _fmt(10.0) == "10"
    assert _fmt(-1.0) == "-1"
    assert _fmt(0.1) == "0.1"
    assert _fmt(1e-5) == "1e-05"


def test_a_recording_with_no_parameters_says_how_to_get_them(builder):
    b = builder()
    b.add(BASE_EPOCH, "ATTITUDE", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
    text = "\n".join(read_vehicle([b.close()]).lines())

    assert "none in this recording" in text
    assert "downloads them" in text


def test_the_report_stays_ascii(builder):
    b = _params(builder(), [("RNGFND1_TYPE", 10)])
    v = read_vehicle([b.close()])
    text = "\n".join(v.lines(full=True))
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, f"non-ascii in the console report: {bad}"
