"""
A small WebSocket client, for the stream the DVL's web GUI reads.

The DVL serves `/ws` beside its web pages. It is the GUI's live feed, and it
carries things the TCP JSON stream does not: orientation at ten hertz, the
dead-reckoning filter's own velocity, and three fields of the velocity report
that only appear here -- whether the DVL is mid-way through a periodic-cycling
check, which range configuration it is running, and whether it is water
tracking.

Written here rather than taken from a library because this program installs
nothing it does not have to, and the part of RFC 6455 a read-only client needs
is short: one handshake, unmasked frames in, masked control frames out. The
frame functions are pure so the tests can drive them without a socket.

Text frames are kept as the exact string the DVL sent. Binary frames, which
the DVL has not been seen to send, are kept as bytes.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from dataclasses import dataclass

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

#: The largest message accepted. The DVL's are a few hundred bytes; a peer
#: that announces a gigabyte is not a DVL and must not be read into memory.
MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(Exception):
    """The handshake failed, or the peer broke the protocol."""


class Closed(WebSocketError):
    """The peer closed the connection."""


def accept_key(key: str) -> str:
    """What a server must answer to `key` (RFC 6455 §4.2.2)."""
    return base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()


def encode_frame(opcode: int, payload: bytes, *, mask: bool = True,
                 fin: bool = True, mask_key: bytes | None = None) -> bytes:
    """One frame. Clients must mask what they send (RFC 6455 §5.3)."""
    head = bytearray([(0x80 if fin else 0) | (opcode & 0x0F)])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head.append(bit | n)
    elif n < 1 << 16:
        head.append(bit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(bit | 127)
        head += struct.pack(">Q", n)
    if not mask:
        return bytes(head) + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    head += key
    return bytes(head) + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


@dataclass
class Frame:
    fin: bool
    opcode: int
    payload: bytes


def decode_frame(buf: bytes | bytearray) -> tuple[Frame | None, int]:
    """(a frame, bytes used) from the front of `buf`, or (None, 0) if incomplete."""
    if len(buf) < 2:
        return None, 0
    b1, b2 = buf[0], buf[1]
    fin, opcode = bool(b1 & 0x80), b1 & 0x0F
    masked, n = bool(b2 & 0x80), b2 & 0x7F
    pos = 2
    if n == 126:
        if len(buf) < 4:
            return None, 0
        n = struct.unpack(">H", bytes(buf[2:4]))[0]
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None, 0
        n = struct.unpack(">Q", bytes(buf[2:10]))[0]
        pos = 10
    if n > MAX_MESSAGE:
        raise WebSocketError(f"frame of {n:,} bytes is larger than any DVL sends")
    key = b""
    if masked:
        if len(buf) < pos + 4:
            return None, 0
        key = bytes(buf[pos:pos + 4])
        pos += 4
    if len(buf) < pos + n:
        return None, 0
    payload = bytes(buf[pos:pos + n])
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return Frame(fin, opcode, payload), pos + n


class Client:
    """One connection to a WebSocket server. One thread uses it.

    `receive` returns whole messages, answers pings itself, and raises
    `Closed` when the peer closes. A receive timeout raises `socket.timeout`
    and leaves the connection usable, so the owner can look at its stop
    signal between messages.
    """

    def __init__(self, host: str, *, port: int | None = None, path: str = "/ws",
                 secure: bool = False, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port or (443 if secure else 80)
        self.path = path
        self.secure = secure
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = bytearray()
        self._parts: list[bytes] = []
        self._part_op = OP_TEXT

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            if self.secure:
                raw = ssl.create_default_context().wrap_socket(
                    raw, server_hostname=self.host)
            key = base64.b64encode(os.urandom(16)).decode()
            host = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            raw.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {host}\r\n"
                         f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Key: {key}\r\n"
                         f"Sec-WebSocket-Version: 13\r\n"
                         f"User-Agent: rov_flight_ops DVL capture\r\n\r\n").encode())
            head = bytearray()
            while b"\r\n\r\n" not in head:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during the handshake")
                head += chunk
                if len(head) > 16384:
                    raise WebSocketError("handshake reply too long")
            header, _, rest = bytes(head).partition(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            if not lines or " 101 " not in f"{lines[0]} ":
                raise WebSocketError(f"handshake refused: {lines[0] if lines else '?'}")
            fields = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                fields[k.strip().lower()] = v.strip()
            if fields.get("sec-websocket-accept") != accept_key(key):
                raise WebSocketError("handshake answer does not match the key")
            self._buf = bytearray(rest)
            self.sock = raw
        except BaseException:
            try:
                raw.close()
            except Exception:
                pass
            raise

    def settimeout(self, seconds: float) -> None:
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def receive(self) -> tuple[int, bytes]:
        """The next whole message: (OP_TEXT or OP_BINARY, payload)."""
        while True:
            frame, used = decode_frame(self._buf)
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise Closed("the server closed the connection")
                self._buf += chunk
                continue
            del self._buf[:used]
            if frame.opcode == OP_PING:
                self._send(OP_PONG, frame.payload)
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CLOSE:
                try:
                    self._send(OP_CLOSE, frame.payload[:2])
                except Exception:
                    pass
                raise Closed("the server sent close")
            if frame.opcode == OP_CONT:
                self._parts.append(frame.payload)
            elif frame.opcode in (OP_TEXT, OP_BINARY):
                self._parts = [frame.payload]
                self._part_op = frame.opcode
            else:
                raise WebSocketError(f"unknown opcode {frame.opcode}")
            if sum(len(p) for p in self._parts) > MAX_MESSAGE:
                raise WebSocketError("message larger than any DVL sends")
            if frame.fin:
                payload = b"".join(self._parts)
                self._parts = []
                return self._part_op, payload

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.sock is not None:
            self.sock.sendall(encode_frame(opcode, payload, mask=True))

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.sendall(encode_frame(OP_CLOSE, struct.pack(">H", 1000), mask=True))
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
