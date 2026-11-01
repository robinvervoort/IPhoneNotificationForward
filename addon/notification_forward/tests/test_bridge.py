"""End-to-end: emulate the iPhone side and drive the bridge."""

import base64
import hashlib
import json
import os
import struct
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import mlkem, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import crypto, protocol
from app.bridge import Bridge

IDENT = "6F1A7C3E-1111-4000-8000-000000000001"
FEATURE = "0B5E2C7D-2222-4000-8000-000000000002"


def xwing_encapsulate(pk: bytes):
    pk_m, pk_x = pk[:1184], pk[1184:]
    a, b = mlkem.MLKEM768PublicKey.from_public_bytes(pk_m).encapsulate()
    ss_m, ct_m = (a, b) if len(a) == 32 else (b, a)
    eph = x25519.X25519PrivateKey.generate()
    ct_x = eph.public_key().public_bytes_raw()
    ss_x = eph.exchange(x25519.X25519PublicKey.from_public_bytes(pk_x))
    ss = hashlib.sha3_256(ss_m + ss_x + ct_x + pk_x + crypto.XWING_LABEL).digest()
    return ss, ct_m + ct_x


class FakeIPhone:
    """Plays the role of iOS + the three app extensions."""

    def __init__(self, bridge: Bridge, suite="xWing"):
        self.bridge, self.suite = bridge, suite
        rx = protocol.Reassembler()
        # GET_PUBKEY through the framing layer, as the BLE link would.
        resp = None
        for pkt in protocol.chunk(protocol.encode_get_pubkey(suite), 182):
            msg = rx.feed(pkt)
        resp = bridge.handle_message(msg)
        assert resp[0] == protocol.MSG_PUBKEY
        self.pk = resp[2:]
        assert suite == "xWing"
        ss, enc = xwing_encapsulate(self.pk)
        self.ctx = crypto.key_schedule(suite, ss, crypto.protocol_info(suite, IDENT))
        assert bridge.handle_message(protocol.encode_keyx(suite, IDENT, enc)) == protocol.encode_result(True)
        h2a, _ = crypto.internet_contexts(IDENT)
        self.chain = self.ctx.export(h2a, 32)
        self.index = 0

    def bluetooth(self, note: dict, context_style="string"):
        info = crypto.protocol_info(self.suite, IDENT)
        prefix = info if context_style == "string" else f"{len(info)} bytes".encode()
        secret = self.ctx.export(prefix + f"-HostToAccessory-{FEATURE}".encode(), 32)
        nonce = os.urandom(12)
        ct = nonce + AESGCM(secret).encrypt(nonce, json.dumps(note).encode(), None)
        return protocol.encode_data(protocol.KIND_CIPHERTEXT, FEATURE, ct)

    def internet(self, note: dict, index=None):
        if index is None:
            index = self.index
        ck = self.chain
        for i in range(index + 1):
            mk, ck = crypto.ratchet_step(ck, i)
        self.index = index + 1
        nonce = os.urandom(12)
        wire = (crypto.internet_key_id(self.pk) + struct.pack("<I", index) + nonce
                + AESGCM(mk).encrypt(nonce, json.dumps(note).encode(), None))
        return {"data": base64.b64encode(wire).decode(), "session_id": FEATURE, "push_token": "ab"}


@pytest.fixture
def setup(tmp_path):
    got = []
    bridge = Bridge(str(tmp_path / "state.json"), got.append)
    return bridge, FakeIPhone(bridge), got, tmp_path


@pytest.mark.parametrize("style", ["string", "bytes"])
def test_bluetooth_delivery(setup, style):
    bridge, phone, got, _ = setup
    assert bridge.handle_message(phone.bluetooth({"action": "add", "title": "Hi"}, style)) is None
    assert got == [{"action": "add", "title": "Hi", "transport": "bluetooth"}]


def test_internet_delivery_out_of_order_and_persistence(setup):
    bridge, phone, got, tmp = setup
    bridge.internet_data(phone.internet({"n": 0}))
    late = phone.internet({"n": 1})
    bridge.internet_data(phone.internet({"n": 2}))
    # Restart the bridge: state must survive.
    bridge2 = Bridge(str(tmp / "state.json"), got.append)
    bridge2.internet_data(late)
    assert [g["n"] for g in got] == [0, 2, 1]
    with pytest.raises(crypto.RatchetError):
        bridge2.internet_data(late)  # replay rejected


def test_duplicate_suppressed(setup):
    bridge, phone, got, _ = setup
    msg = phone.bluetooth({"action": "add", "id": "x"})
    bridge.handle_message(msg)
    bridge.handle_message(msg)
    assert len(got) == 1


def test_tampered_bluetooth_reports_error(setup):
    bridge, phone, got, _ = setup
    msg = bytearray(phone.bluetooth({"a": 1}))
    msg[-1] ^= 1
    resp = bridge.handle_message(bytes(msg))
    assert resp[0] == protocol.MSG_RESULT and resp[1] == 1
    assert got == []


def test_tampered_internet_does_not_burn_ratchet(setup):
    bridge, phone, got, _ = setup
    good = phone.internet({"ok": True})
    raw = bytearray(base64.b64decode(good["data"]))
    raw[-1] ^= 1
    with pytest.raises(ValueError):
        bridge.internet_data({**good, "data": base64.b64encode(raw).decode()})
    bridge.internet_data(good)
    assert got[0]["ok"] is True


def test_framing_roundtrip():
    msg = os.urandom(1000)
    rx = protocol.Reassembler()
    out = [rx.feed(p) for p in protocol.chunk(msg, 20)]
    assert out[-1] == msg and all(o is None for o in out[:-1])
    with pytest.raises(protocol.ProtocolError):
        protocol.Reassembler().feed(protocol.chunk(msg, 20)[1])


def test_data_message_uuid_is_uppercase():
    sid = str(uuid.uuid4())
    parsed = protocol.parse(protocol.encode_data(1, sid, b"x"))
    assert parsed.session_id == sid.upper()
