"""Check the hand-written HPKE key schedule against `cryptography`'s HPKE."""

import os
import struct

import pytest
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import crypto


def _library_xwing_public(key: crypto.XWingPrivateKey):
    lib_priv = hpke.MLKEM768X25519PrivateKey(key._mlkem, key._x25519)
    return lib_priv.public_key()


@pytest.mark.parametrize("info", [b"", b"xWing-Version1-6F1A7C3E-0000-4000-8000-000000000001"])
def test_xwing_key_schedule_matches_library(info):
    key = crypto.XWingPrivateKey.generate()
    suite = hpke.Suite(hpke.KEM.MLKEM768_X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)
    blob = suite.encrypt(b"hello bridge", _library_xwing_public(key), info=info)
    enc, ct = blob[:1120], blob[1120:]
    ctx = crypto.setup_base_recipient(key, enc, info)
    assert ctx.open(ct) == b"hello bridge"


def test_xwing_public_key_layout():
    key = crypto.XWingPrivateKey.generate()
    assert len(key.public_bytes()) == 1216
    # Same seed -> same key (CryptoKit seedRepresentation round trip).
    assert crypto.XWingPrivateKey(key.seed).public_bytes() == key.public_bytes()


@pytest.mark.parametrize("info", [b"", b"p256-Version1-ABC"])
def test_p256_key_schedule_matches_library(info):
    key = crypto.P256PrivateKey.generate()
    suite = hpke.Suite(hpke.KEM.P256, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)
    blob = suite.encrypt(b"hi", key._key.public_key(), info=info)
    enc, ct = blob[:65], blob[65:]
    ctx = crypto.setup_base_recipient(key, enc, info)
    assert ctx.open(ct) == b"hi"
    assert len(key.public_bytes()) == 64
    restored = crypto.P256PrivateKey.from_private_bytes(key.private_bytes())
    assert restored.public_bytes() == key.public_bytes()


def test_rfc9180_a3_export_vectors():
    """RFC 9180 A.3: DHKEM(P-256), HKDF-SHA256, AES-128-GCM, base mode."""
    sk_rm = bytes.fromhex("f3ce7fdae57e1a310d87f1ebbde6f328be0a99cdbcadf4d6589cf29de4b8ffd2")
    enc = bytes.fromhex(
        "04a92719c6195d5085104f469a8b9814d5838ff72b60501e2c4466e5e67b32"
        "5ac98536d7b61a1af4b78e5b7f951c0900be863c403ce65c9bfcb9382657222d18c4")
    info = bytes.fromhex("4f6465206f6e2061204772656369616e2055726e")
    ctx = crypto.setup_base_recipient(crypto.P256PrivateKey.from_private_bytes(sk_rm), enc, info,
                                      aead_id=0x0001)
    assert ctx.export(b"", 32).hex() == "5e9bc3d236e1911d95e65b576a8a86d478fb827e8bdfe77b741b289890490d4d"


def test_ratchet_in_order_out_of_order_and_replay():
    root = os.urandom(32)
    # Sender side: derive keys 0..4 sequentially.
    ck, keys = root, []
    for i in range(5):
        mk, ck = crypto.ratchet_step(ck, i)
        keys.append(mk)
    r = crypto.ReceivingRatchet(chain_key=root)
    assert r.message_key(0) == keys[0]
    assert r.message_key(3) == keys[3]   # skip 1, 2
    assert r.message_key(1) == keys[1]   # late arrival
    with pytest.raises(crypto.RatchetError):
        r.message_key(1)                 # replay
    r2 = crypto.ReceivingRatchet.from_dict(r.to_dict())
    assert r2.message_key(2) == keys[2]
    assert r2.message_key(4) == keys[4]


def test_internet_message_roundtrip():
    root = os.urandom(32)
    mk, _ = crypto.ratchet_step(root, 0)
    nonce = os.urandom(12)
    wire = b"\x01\x02\x03\x04" + struct.pack("<I", 0) + nonce + AESGCM(mk).encrypt(nonce, b"{}", None)
    key_id, idx, ct = crypto.parse_internet_message(wire)
    assert (key_id, idx) == (b"\x01\x02\x03\x04", 0)
    r = crypto.ReceivingRatchet(chain_key=root)
    assert crypto.aes_gcm_open_combined(r.message_key(idx), ct) == b"{}"


def test_bluetooth_context_candidates():
    info = crypto.protocol_info("xWing", "ABC")
    cands = crypto.bluetooth_feature_contexts(info, "5f0e-aa")
    assert b"xWing-Version1-ABC-HostToAccessory-5F0E-AA" in cands
    assert f"{len(info)} bytes-HostToAccessory-5F0E-AA".encode() in cands
