"""Wire protocol between the iOS extensions and the bridge over BLE GATT.

Keep in sync with ``ios/Shared/BridgeProtocol.swift``.

GATT layout
-----------
* Service ``SERVICE_UUID`` (advertised; AccessorySetupKit discovers it).
* ``RX_UUID``  write (with response): iPhone -> bridge.
* ``TX_UUID``  notify:                bridge -> iPhone.

Framing
-------
A logical message may exceed one ATT packet, so every packet starts with a
header byte: bit 7 = START, bit 6 = END, bits 0-5 = sequence number (mod 64).
A packet with START resets the reassembly buffer.

Messages (first byte = type)
----------------------------
iPhone -> bridge
  0x01 GET_PUBKEY  [suite u8]
  0x02 KEYX        [suite u8][id_len u16 BE][identifier utf8]
                   [info_len u16 BE][HPKE info utf8, as iOS spells it][encapsulated key]
  0x03 DATA        [kind u8: 1=ciphertext 2=plaintext][session UUID 16 bytes][data]
  0x04 LOG         [utf8 text]  diagnostics from the iPhone extensions (no reply)
bridge -> iPhone
  0x81 PUBKEY      [suite u8][public key]
  0x82 RESULT      [status u8: 0=ok, 1=error][utf8 message]
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

SERVICE_UUID = "46B6C6E3-47D4-4FD7-98B7-D41D1109B69B"
RX_UUID = "297E3C45-E532-40FD-8303-0C6E857AA0F2"
TX_UUID = "67334EA1-143C-4268-9A63-E34642C7C8DB"

START = 0x80
END = 0x40
SEQ_MASK = 0x3F

MSG_GET_PUBKEY = 0x01
MSG_KEYX = 0x02
MSG_DATA = 0x03
MSG_LOG = 0x04
MSG_PUBKEY = 0x81
MSG_RESULT = 0x82

KIND_CIPHERTEXT = 1
KIND_PLAINTEXT = 2

SUITES = {0: "xWing", 1: "p256"}
SUITE_CODES = {v: k for k, v in SUITES.items()}


class ProtocolError(Exception):
    pass


def chunk(message: bytes, max_packet: int) -> list[bytes]:
    if max_packet < 2:
        raise ValueError("packet size too small")
    size = max_packet - 1
    parts = [message[i:i + size] for i in range(0, len(message), size)] or [b""]
    out = []
    for n, part in enumerate(parts):
        header = n & SEQ_MASK
        if n == 0:
            header |= START
        if n == len(parts) - 1:
            header |= END
        out.append(bytes([header]) + part)
    return out


class Reassembler:
    MAX_MESSAGE = 64 * 1024

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._next_seq = 0

    def feed(self, packet: bytes) -> bytes | None:
        """Feed one packet; return a complete message or ``None``."""
        if not packet:
            raise ProtocolError("empty packet")
        header, body = packet[0], packet[1:]
        seq = header & SEQ_MASK
        if header & START:
            self._buf = bytearray()
            self._next_seq = 0
        if self._buf is None:
            raise ProtocolError("continuation without start")
        if seq != self._next_seq:
            self._buf = None
            raise ProtocolError(f"out of order packet {seq}, expected {self._next_seq}")
        self._buf += body
        self._next_seq = (self._next_seq + 1) & SEQ_MASK
        if len(self._buf) > self.MAX_MESSAGE:
            self._buf = None
            raise ProtocolError("message too large")
        if header & END:
            msg, self._buf = bytes(self._buf), None
            return msg
        return None


@dataclass
class GetPubKey:
    suite: str


@dataclass
class KeyExchange:
    suite: str
    identifier: str
    info: str  # "<cipherSuite>-<version>-<identifier>" exactly as iOS builds it
    encapsulated_key: bytes


@dataclass
class Data:
    kind: int
    session_id: str  # canonical uppercase UUID string, like Swift's UUID.uuidString
    data: bytes


@dataclass
class Log:
    text: str


def _suite(code: int) -> str:
    try:
        return SUITES[code]
    except KeyError:
        raise ProtocolError(f"unknown suite {code}") from None


def parse(message: bytes):
    if not message:
        raise ProtocolError("empty message")
    t, body = message[0], message[1:]
    if t == MSG_GET_PUBKEY:
        if len(body) != 1:
            raise ProtocolError("bad GET_PUBKEY")
        return GetPubKey(_suite(body[0]))
    if t == MSG_KEYX:
        if len(body) < 3:
            raise ProtocolError("bad KEYX")
        suite = _suite(body[0])
        pos = 1
        fields = []
        for _ in range(2):  # identifier, info
            if len(body) < pos + 2:
                raise ProtocolError("truncated KEYX")
            (n,) = struct.unpack(">H", body[pos:pos + 2])
            raw = body[pos + 2:pos + 2 + n]
            if len(raw) != n:
                raise ProtocolError("truncated KEYX")
            fields.append(raw.decode())
            pos += 2 + n
        enc = body[pos:]
        if not enc:
            raise ProtocolError("KEYX without encapsulated key")
        return KeyExchange(suite, fields[0], fields[1], enc)
    if t == MSG_LOG:
        return Log(body.decode("utf-8", "replace"))
    if t == MSG_DATA:
        if len(body) < 17:
            raise ProtocolError("bad DATA")
        return Data(body[0], str(uuid.UUID(bytes=body[1:17])).upper(), body[17:])
    raise ProtocolError(f"unknown message type 0x{t:02x}")


def encode_pubkey(suite: str, key: bytes) -> bytes:
    return bytes([MSG_PUBKEY, SUITE_CODES[suite]]) + key


def encode_result(ok: bool, message: str = "") -> bytes:
    return bytes([MSG_RESULT, 0 if ok else 1]) + message.encode()[:200]


# Helpers used by tests to emulate the iPhone side.

def encode_get_pubkey(suite: str) -> bytes:
    return bytes([MSG_GET_PUBKEY, SUITE_CODES[suite]])


def encode_keyx(suite: str, identifier: str, enc: bytes, info: str | None = None) -> bytes:
    ident = identifier.encode()
    inf = (info if info is not None else f"{suite}-Version1-{identifier}").encode()
    return (bytes([MSG_KEYX, SUITE_CODES[suite]]) + struct.pack(">H", len(ident)) + ident
            + struct.pack(">H", len(inf)) + inf + enc)


def encode_data(kind: int, session_id: str, data: bytes) -> bytes:
    return bytes([MSG_DATA, kind]) + uuid.UUID(session_id).bytes + data


def encode_log(text: str) -> bytes:
    return bytes([MSG_LOG]) + text.encode()
