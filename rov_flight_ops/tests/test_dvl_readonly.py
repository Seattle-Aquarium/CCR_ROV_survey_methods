"""
The DVL is only ever asked, never told.

The capture talks to the DVL more than anything else in this program does:
a TCP connection that sends commands, and dozens of web requests a second.
These pin down that none of it can change the DVL -- by what the code can
build, not by what it happens to do on a good day:

* a command can only be made by `protocol.command_bytes`, and that refuses
  everything outside the four read-only questions;
* the names of the commands that change the DVL appear nowhere but in the
  list that documents them;
* every web request is a GET;
* the one request that makes the DVL do something -- collecting Water
  Linked's diagnostic log -- lives in one module, behind one gate that needs
  the vehicle confirmed disarmed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rov_flight_ops.dvl import diagnostic as DIAG
from rov_flight_ops.dvl import protocol as P
from rov_flight_ops.dvl import webapi
from rov_flight_ops.dvl.simulator import SimulatedDvl

PKG = Path(P.__file__).parent
GUI = PKG.parent / "gui" / "dvlpage.py"


def _sources():
    return [*sorted(PKG.glob("*.py")), GUI]


@pytest.mark.parametrize("name", sorted(P.READ_ONLY_COMMANDS))
def test_the_read_only_commands_encode_as_one_document_each(name):
    data = P.command_bytes(name)
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"command": name}


@pytest.mark.parametrize("name", [*P.NEVER_SENT, "", "GET_CONFIG", "get_config "])
def test_everything_else_is_refused(name):
    with pytest.raises(P.CommandRefused):
        P.command_bytes(name)


def test_no_command_is_built_anywhere_but_command_bytes():
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        hits = [m.start() for m in re.finditer(r"""["']command["']\s*:""", text)]
        if path.name == "protocol.py":
            assert len(hits) == 1, "only command_bytes may build a command"
        elif path.name == "simulator.py":
            continue            # it reads commands; it is the DVL in the tests
        else:
            assert not hits, f"{path.name} builds a command itself"


def test_the_commands_that_change_the_dvl_are_named_only_in_their_list():
    for path in _sources():
        if path.name == "protocol.py":
            continue
        text = path.read_text(encoding="utf-8")
        for name in P.NEVER_SENT:
            assert name not in text, f"{path.name} mentions {name}"


def test_every_web_request_is_a_get():
    for path in _sources():
        if path.name == "simulator.py":
            continue
        text = path.read_text(encoding="utf-8")
        for verb in re.findall(r"""\.request\(\s*["'](\w+)["']""", text):
            assert verb == "GET", f"{path.name} sends {verb}"
        for word in ('method="POST"', 'method="PUT"', 'method="DELETE"',
                     'method="PATCH"', "urlopen("):
            assert word not in text, f"{path.name}: {word}"


def test_the_diagnostic_request_lives_in_one_module():
    for path in _sources():
        if path.name in ("diagnostic.py", "simulator.py"):
            continue
        assert "/api/collect" not in path.read_text(encoding="utf-8"), path.name


@pytest.fixture
def sim():
    s = SimulatedDvl().start()
    yield s
    s.stop()


def _collects(sim) -> int:
    return sum(1 for r in sim.requests if r[0] == "http" and "/api/collect" in r[2])


@pytest.mark.parametrize("state, why", [
    ((True, True), "ARMED"),
    ((None, False), "not known"),
    ((False, False), "not known"),
    ((None, True), "not known"),
])
def test_a_diagnostic_log_is_refused_unless_confirmed_disarmed(sim, tmp_path,
                                                                state, why):
    addr = webapi.Address.parse(sim.address)
    with pytest.raises(DIAG.Refused, match=why):
        DIAG.collect(addr, seconds=15, description="bench", folder=tmp_path,
                     armed_state=lambda: state)
    assert _collects(sim) == 0, "nothing may be sent before the gate passes"
    assert list(tmp_path.iterdir()) == []


def test_a_diagnostic_log_needs_a_description_and_a_real_duration(sim, tmp_path):
    addr = webapi.Address.parse(sim.address)
    with pytest.raises(DIAG.Refused):
        DIAG.collect(addr, seconds=15, description="  ", folder=tmp_path,
                     armed_state=lambda: (False, True))
    with pytest.raises(DIAG.Refused):
        DIAG.collect(addr, seconds=20, description="x", folder=tmp_path,
                     armed_state=lambda: (False, True))
    assert _collects(sim) == 0


def test_a_diagnostic_log_is_saved_whole_with_its_record(sim, tmp_path):
    addr = webapi.Address.parse(sim.address)
    res = DIAG.collect(addr, seconds=30, description="tank test, beam 2 flickers",
                       folder=tmp_path, armed_state=lambda: (False, True))
    assert res.ok, res.error
    saved = Path(res.path)
    assert saved.suffix == ".zip" and saved.read_bytes().startswith(b"PK")
    import hashlib
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == res.sha256
    assert res.filename_from_dvl == "dvl-diagnostic-report.zip"
    sidecar = json.loads(Path(res.sidecar).read_text(encoding="utf-8"))
    assert sidecar["sha256"] == res.sha256
    assert sidecar["request"] == "GET /api/collect?desc=tank+test%2C+beam+2+flickers&t=30"
    assert not list(tmp_path.glob("*.part"))
    # Exactly as the DVL's own form sends it: a GET, t omitted at 15 s.
    assert DIAG.query("x", 15) == "desc=x"


def test_a_dvl_that_will_not_collect_leaves_no_file(sim, tmp_path):
    sim.collect_supported = False                # what the demo DVL does
    addr = webapi.Address.parse(sim.address)
    res = DIAG.collect(addr, seconds=15, description="x", folder=tmp_path,
                       armed_state=lambda: (False, True))
    assert not res.ok and "406" in res.error
    assert [p.suffix for p in tmp_path.iterdir()] == [".json"]
