"""
The small WebSocket client: frames in both directions, and a real handshake
against the simulated DVL.
"""

from __future__ import annotations

import json
import socket

import pytest

from rov_flight_ops.dvl import websocket as W
from rov_flight_ops.dvl.simulator import SimulatedDvl


def test_the_accept_key_is_rfc_6455s_worked_example():
    # RFC 6455 §1.3.
    assert W.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("n", [0, 1, 125, 126, 65535, 65536, 200_000])
def test_frames_round_trip_at_every_length_encoding(n):
    payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
    for mask in (True, False):
        frame = W.encode_frame(W.OP_TEXT, payload, mask=mask)
        got, used = W.decode_frame(frame + b"next")
        assert used == len(frame)
        assert got.payload == payload and got.opcode == W.OP_TEXT and got.fin


def test_an_incomplete_frame_waits_for_more():
    frame = W.encode_frame(W.OP_BINARY, b"x" * 300, mask=False)
    for cut in (0, 1, 3, 10, len(frame) - 1):
        assert W.decode_frame(frame[:cut]) == (None, 0)


def test_client_frames_are_masked():
    frame = W.encode_frame(W.OP_PONG, b"abc", mask=True, mask_key=b"\x01\x02\x03\x04")
    assert frame[1] & 0x80
    assert frame[2:6] == b"\x01\x02\x03\x04"
    assert frame[6:] == bytes([ord("a") ^ 1, ord("b") ^ 2, ord("c") ^ 3])


def test_an_enormous_frame_is_refused_before_it_is_read():
    head = bytes([0x81, 127]) + (W.MAX_MESSAGE + 1).to_bytes(8, "big")
    with pytest.raises(W.WebSocketError):
        W.decode_frame(head)


class _FakeSock:
    """Feeds frames to a Client, records what it sends back."""

    def __init__(self, frames: bytes):
        self.data = frames
        self.sent = b""

    def recv(self, n):
        if not self.data:
            raise TimeoutError()
        out, self.data = self.data[:n], self.data[n:]
        return out

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass

    def settimeout(self, _s):
        pass


def test_fragments_are_joined_and_pings_answered():
    frames = (W.encode_frame(W.OP_TEXT, b'{"chan', mask=False, fin=False)
              + W.encode_frame(W.OP_PING, b"hi", mask=False)
              + W.encode_frame(W.OP_CONT, b'nel": 1}', mask=False, fin=True))
    c = W.Client("x")
    c.sock = _FakeSock(frames)
    op, payload = c.receive()
    assert (op, payload) == (W.OP_TEXT, b'{"channel": 1}')
    pong, _ = W.decode_frame(c.sock.sent)
    assert pong.opcode == W.OP_PONG and pong.payload == b"hi"


def test_a_close_from_the_server_is_raised_as_closed():
    c = W.Client("x")
    c.sock = _FakeSock(W.encode_frame(W.OP_CLOSE, b"\x03\xe8", mask=False))
    with pytest.raises(W.Closed):
        c.receive()


def test_a_real_handshake_and_messages_from_the_simulated_dvl():
    sim = SimulatedDvl(rate_hz=20).start()
    try:
        c = W.Client("127.0.0.1", port=sim.http_port, path="/ws", timeout=3)
        c.connect()
        c.settimeout(3)
        channels = set()
        for _ in range(40):
            _op, payload = c.receive()
            channels.add(json.loads(payload)["channel"])
        c.close()
        assert {"velocity", "roll_pitch_yaw", "position_local"} <= channels
    finally:
        sim.stop()


def test_a_server_that_is_not_a_websocket_is_refused():
    sim = SimulatedDvl().start()
    try:
        c = W.Client("127.0.0.1", port=sim.http_port, path="/api/v1/about", timeout=3)
        with pytest.raises(W.WebSocketError):
            c.connect()
    finally:
        sim.stop()
