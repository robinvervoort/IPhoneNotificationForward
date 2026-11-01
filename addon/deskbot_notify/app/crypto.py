"""Cryptography for Apple's AccessoryTransportExtension notification forwarding.

The iPhone is the HPKE *sender*; this bridge is the HPKE *recipient*.

Two cipher suites are supported, mirroring ``SecurityMessage.CipherSuite``:

* ``xWing`` - KEM X-Wing (ML-KEM-768 + X25519, HPKE KEM id 0x647A),
  HKDF-SHA256, AES-256-GCM.  Required for the internet transport.
* ``p256``  - DHKEM(P-256, HKDF-SHA256), HKDF-SHA256, AES-256-GCM.

Only the HPKE *exporter* interface is used (RFC 9180 section 5.3): the
system and the accessory both derive symmetric secrets from the HPKE
context and then encrypt with plain AES-GCM (Bluetooth) or a symmetric key
ratchet (internet).  The ``cryptography`` package implements single-shot
HPKE but does not expose ``export``, so the key schedule lives here.  The
test-suite checks it against ``cryptography``'s own HPKE implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, mlkem, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

XWING = "xWing"
P256 = "p256"

KEM_ID = {XWING: 0x647A, P256: 0x0010}
KDF_HKDF_SHA256 = 0x0001
AEAD_AES_256_GCM = 0x0002

# X-Wing combiner label: the six ASCII bytes  \.//^\
XWING_LABEL = b"\x5c\x2e\x2f\x2f\x5e\x5c"


# --------------------------------------------------------------------------
# HKDF / HPKE labeled primitives (RFC 9180 section 4)
# --------------------------------------------------------------------------

def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


def labeled_extract(suite_id: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return hkdf_extract(salt, b"HPKE-v1" + suite_id + label + ikm)


def labeled_expand(suite_id: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    labeled_info = struct.pack(">H", length) + b"HPKE-v1" + suite_id + label + info
    return hkdf_expand(prk, labeled_info, length)


def hpke_suite_id(kem_id: int, kdf_id: int = KDF_HKDF_SHA256, aead_id: int = AEAD_AES_256_GCM) -> bytes:
    return b"HPKE" + struct.pack(">HHH", kem_id, kdf_id, aead_id)


# --------------------------------------------------------------------------
# Recipient key pairs
# --------------------------------------------------------------------------

class XWingPrivateKey:
    """X-Wing private key, stored as its 32-byte seed (like CryptoKit's
    ``XWingMLKEM768X25519.PrivateKey.seedRepresentation``)."""

    suite = XWING

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("X-Wing seed must be 32 bytes")
        self.seed = seed
        expanded = hashlib.shake_256(seed).digest(96)
        self._mlkem = mlkem.MLKEM768PrivateKey.from_seed_bytes(expanded[:64])
        self._x25519 = x25519.X25519PrivateKey.from_private_bytes(expanded[64:96])
        self._pk_m = self._mlkem.public_key().public_bytes_raw()
        self._pk_x = self._x25519.public_key().public_bytes_raw()

    @classmethod
    def generate(cls) -> "XWingPrivateKey":
        return cls(os.urandom(32))

    def public_bytes(self) -> bytes:
        """pk_M (1184 bytes) || pk_X (32 bytes)"""
        return self._pk_m + self._pk_x

    def private_bytes(self) -> bytes:
        return self.seed

    def decapsulate(self, enc: bytes) -> bytes:
        if len(enc) != 1088 + 32:
            raise ValueError(f"X-Wing ciphertext must be 1120 bytes, got {len(enc)}")
        ct_m, ct_x = enc[:1088], enc[1088:]
        ss_m = self._mlkem.decapsulate(ct_m)
        ss_x = self._x25519.exchange(x25519.X25519PublicKey.from_public_bytes(ct_x))
        return hashlib.sha3_256(ss_m + ss_x + ct_x + self._pk_x + XWING_LABEL).digest()


class P256PrivateKey:
    suite = P256

    def __init__(self, key: ec.EllipticCurvePrivateKey):
        self._key = key

    @classmethod
    def generate(cls) -> "P256PrivateKey":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "P256PrivateKey":
        return cls(ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1()))

    def private_bytes(self) -> bytes:
        return self._key.private_numbers().private_value.to_bytes(32, "big")

    def _uncompressed(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )

    def public_bytes(self) -> bytes:
        """CryptoKit ``rawRepresentation``: X || Y (64 bytes, no 0x04 prefix)."""
        return self._uncompressed()[1:]

    def decapsulate(self, enc: bytes) -> bytes:
        if len(enc) == 64:  # tolerate rawRepresentation
            enc = b"\x04" + enc
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), enc)
        dh = self._key.exchange(ec.ECDH(), peer)
        kem_suite = b"KEM" + struct.pack(">H", KEM_ID[P256])
        eae_prk = labeled_extract(kem_suite, b"", b"eae_prk", dh)
        kem_context = enc + self._uncompressed()
        return labeled_expand(kem_suite, eae_prk, b"shared_secret", kem_context, 32)


def private_key_from_bytes(suite: str, raw: bytes):
    if suite == XWING:
        return XWingPrivateKey(raw)
    if suite == P256:
        return P256PrivateKey.from_private_bytes(raw)
    raise ValueError(f"unknown cipher suite {suite!r}")


# --------------------------------------------------------------------------
# HPKE base-mode recipient context
# --------------------------------------------------------------------------

@dataclass
class HPKEContext:
    suite_id: bytes
    key: bytes
    base_nonce: bytes
    exporter_secret: bytes
    seq: int = 0

    def export(self, exporter_context: bytes, length: int) -> bytes:
        return labeled_expand(self.suite_id, self.exporter_secret, b"sec", exporter_context, length)

    def open(self, ciphertext: bytes, aad: bytes = b"") -> bytes:
        """Single-message open (used by tests to check the key schedule)."""
        nonce = bytes(a ^ b for a, b in zip(self.base_nonce, self.seq.to_bytes(12, "big"), strict=True))
        self.seq += 1
        return AESGCM(self.key).decrypt(nonce, ciphertext, aad)


def setup_base_recipient(private_key, enc: bytes, info: bytes,
                         aead_id: int = AEAD_AES_256_GCM) -> HPKEContext:
    return key_schedule(private_key.suite, private_key.decapsulate(enc), info, aead_id)


def key_schedule(suite: str, shared_secret: bytes, info: bytes,
                 aead_id: int = AEAD_AES_256_GCM) -> HPKEContext:
    """RFC 9180 KeySchedule for base mode (identical for sender and recipient)."""
    suite_id = hpke_suite_id(KEM_ID[suite], KDF_HKDF_SHA256, aead_id)
    nk = 32 if aead_id == AEAD_AES_256_GCM else 16
    psk_id_hash = labeled_extract(suite_id, b"", b"psk_id_hash", b"")
    info_hash = labeled_extract(suite_id, b"", b"info_hash", info)
    ks_context = b"\x00" + psk_id_hash + info_hash
    secret = labeled_extract(suite_id, shared_secret, b"secret", b"")
    return HPKEContext(
        suite_id=suite_id,
        key=labeled_expand(suite_id, secret, b"key", ks_context, nk),
        base_nonce=labeled_expand(suite_id, secret, b"base_nonce", ks_context, 12),
        exporter_secret=labeled_expand(suite_id, secret, b"exp", ks_context, 32),
    )


# --------------------------------------------------------------------------
# Apple-specific derivations
# --------------------------------------------------------------------------

def protocol_info(suite: str, identifier: str) -> bytes:
    """``"<cipherSuite>-Version1-<identifier>"`` (Apple spells out Version1)."""
    return f"{suite}-Version1-{identifier}".encode()


def bluetooth_feature_contexts(info: bytes, feature_id: str, direction: str = "HostToAccessory") -> list[bytes]:
    """Candidate exporter contexts for a Bluetooth feature secret.

    Apple's sample builds the context with Swift string interpolation of a
    ``Data`` value (``"\\(protocolInfo)-HostToAccessory-\\(featureID)"``), which
    is ambiguous: interpolating ``Data`` yields ``"<n> bytes"``.  We try the
    plausible spellings; AES-GCM authentication tells us which one is right.
    """
    prefixes = [info, f"{len(info)} bytes".encode()]
    ids = list(dict.fromkeys([feature_id.upper(), feature_id.lower(), feature_id]))
    return [p + f"-{direction}-{i}".encode() for p in prefixes for i in ids]


def internet_contexts(identifier: str) -> tuple[bytes, bytes]:
    return (
        f"AccessoryInternetKey-HostToAccessory-{identifier}".encode(),
        f"AccessoryInternetKey-AccessoryToHost-{identifier}".encode(),
    )


def internet_key_id(xwing_public_key: bytes) -> bytes:
    return hashlib.sha384(xwing_public_key).digest()[:4]


def aes_gcm_open_combined(key: bytes, combined: bytes) -> bytes:
    """Open ``IV (12) || ciphertext || tag (16)``."""
    if len(combined) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(combined[:12], combined[12:], None)


# --------------------------------------------------------------------------
# Symmetric key ratchet (internet transport)
# --------------------------------------------------------------------------

RATCHET_LABEL = b"AccessoryTransportSecurity-RatchetKeys-"
MAX_SKIPPED_KEYS = 2000  # Apple allows up to 50k; we keep state small.


def ratchet_step(chain_key: bytes, index: int) -> tuple[bytes, bytes]:
    out = hkdf_expand(chain_key, RATCHET_LABEL + struct.pack("<I", index), 64)
    return out[:32], out[32:]


class RatchetError(Exception):
    pass


@dataclass
class ReceivingRatchet:
    chain_key: bytes
    next_index: int = 0
    skipped: dict[int, bytes] = field(default_factory=dict)

    def message_key(self, index: int) -> bytes:
        """Return the key for ``index``; each key can be used only once."""
        if index < self.next_index:
            key = self.skipped.pop(index, None)
            if key is None:
                raise RatchetError(f"message index {index} already used or too old")
            return key
        if index - self.next_index > MAX_SKIPPED_KEYS:
            raise RatchetError(f"message index {index} too far ahead of {self.next_index}")
        while self.next_index < index:
            mk, self.chain_key = ratchet_step(self.chain_key, self.next_index)
            self.skipped[self.next_index] = mk
            self.next_index += 1
        mk, self.chain_key = ratchet_step(self.chain_key, index)
        self.next_index = index + 1
        while len(self.skipped) > MAX_SKIPPED_KEYS:
            self.skipped.pop(min(self.skipped))
        return mk

    def to_dict(self) -> dict:
        return {
            "chain_key": self.chain_key.hex(),
            "next_index": self.next_index,
            "skipped": {str(k): v.hex() for k, v in self.skipped.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReceivingRatchet":
        return cls(
            chain_key=bytes.fromhex(d["chain_key"]),
            next_index=int(d["next_index"]),
            skipped={int(k): bytes.fromhex(v) for k, v in d.get("skipped", {}).items()},
        )


def parse_internet_message(data: bytes) -> tuple[bytes, int, bytes]:
    """``keyID (4) || messageIndex (u32 LE) || nonce || ciphertext || tag``"""
    if len(data) < 8 + 28:
        raise ValueError("internet message too short")
    return data[:4], struct.unpack("<I", data[4:8])[0], data[8:]
