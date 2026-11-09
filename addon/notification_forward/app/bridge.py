"""Bridge core: key management, decryption and notification decoding.

Transport-agnostic and synchronous so it can be unit tested without BLE or
Home Assistant.  ``ble.py`` feeds it GATT messages, ``ha.py`` feeds it
internet deliveries (via a Home Assistant webhook) and receives the
decoded notifications through ``on_notification``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from cryptography.exceptions import InvalidTag

from . import crypto, protocol

log = logging.getLogger(__name__)


@dataclass
class Session:
    """Key material for one key exchange with one iPhone."""

    identifier: str
    suite: str
    exporter_secret: bytes
    protocol_info: bytes
    created: float
    # Internet transport (X-Wing only)
    key_id: bytes | None = None
    incoming: crypto.ReceivingRatchet | None = None
    # feature/session UUID -> matching exporter context, learned on first success
    bt_contexts: dict[str, bytes] = field(default_factory=dict)

    def export(self, context: bytes) -> bytes:
        return crypto.labeled_expand(
            crypto.hpke_suite_id(crypto.KEM_ID[self.suite]), self.exporter_secret, b"sec", context, 32
        )

    def to_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "suite": self.suite,
            "exporter_secret": self.exporter_secret.hex(),
            "protocol_info": self.protocol_info.decode(),
            "created": self.created,
            "key_id": self.key_id.hex() if self.key_id else None,
            "incoming": self.incoming.to_dict() if self.incoming else None,
            "bt_contexts": {k: v.decode() for k, v in self.bt_contexts.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            identifier=d["identifier"],
            suite=d["suite"],
            exporter_secret=bytes.fromhex(d["exporter_secret"]),
            protocol_info=d["protocol_info"].encode(),
            created=d["created"],
            key_id=bytes.fromhex(d["key_id"]) if d.get("key_id") else None,
            incoming=crypto.ReceivingRatchet.from_dict(d["incoming"]) if d.get("incoming") else None,
            bt_contexts={k: v.encode() for k, v in d.get("bt_contexts", {}).items()},
        )


class Bridge:
    def __init__(self, state_path: str, on_notification: Callable[[dict], None]):
        self.state_path = state_path
        self.on_notification = on_notification
        self.keys: dict[str, object] = {}
        self.sessions: dict[str, Session] = {}
        self._recent: OrderedDict[str, float] = OrderedDict()
        self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        data: dict = {}
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                data = json.load(f)
        for suite, raw in data.get("keys", {}).items():
            self.keys[suite] = crypto.private_key_from_bytes(suite, bytes.fromhex(raw))
        if crypto.XWING not in self.keys:
            self.keys[crypto.XWING] = crypto.XWingPrivateKey.generate()
        if crypto.P256 not in self.keys:
            self.keys[crypto.P256] = crypto.P256PrivateKey.generate()
        for ident, s in data.get("sessions", {}).items():
            self.sessions[ident] = Session.from_dict(s)
        self._save()

    def _save(self) -> None:
        data = {
            "keys": {suite: k.private_bytes().hex() for suite, k in self.keys.items()},
            "sessions": {i: s.to_dict() for i, s in self.sessions.items()},
        }
        directory = os.path.dirname(self.state_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    def reset(self) -> None:
        """Forget all keys and sessions (forces re-pairing)."""
        self.keys.clear()
        self.sessions.clear()
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        self._load()

    # --------------------------------------------------------------- GATT API

    def handle_message(self, message: bytes) -> bytes | None:
        """Handle one reassembled GATT message; return a response message."""
        try:
            msg = protocol.parse(message)
            if isinstance(msg, protocol.GetPubKey):
                key = self.keys[msg.suite]
                log.info("iPhone requested %s public key", msg.suite)
                return protocol.encode_pubkey(msg.suite, key.public_bytes())
            if isinstance(msg, protocol.KeyExchange):
                self.key_exchange(msg.suite, msg.identifier, msg.encapsulated_key, msg.info)
                return protocol.encode_result(True)
            if isinstance(msg, protocol.Data):
                self.bluetooth_data(msg.kind, msg.session_id, msg.data)
                return None  # data writes are acknowledged by the ATT write response
        except Exception as e:  # noqa: BLE001 - report every failure to the phone
            log.warning("GATT message failed: %s", e)
            return protocol.encode_result(False, str(e))
        return None

    def key_exchange(self, suite: str, identifier: str, enc: bytes, info_text: str | None = None) -> Session:
        key = self.keys[suite]
        # Use iOS's own spelling of "<suite>-<version>-<id>" when the phone sends it
        # (the docs say "xWing"/"p256", accessories in the field have seen "P256").
        info = info_text.encode() if info_text else crypto.protocol_info(suite, identifier)
        ctx = crypto.setup_base_recipient(key, enc, info)
        session = Session(identifier, suite, ctx.exporter_secret, info, time.time())
        if suite == crypto.XWING:
            h2a, _a2h = crypto.internet_contexts(identifier)
            session.key_id = crypto.internet_key_id(key.public_bytes())
            session.incoming = crypto.ReceivingRatchet(chain_key=ctx.export(h2a, 32))
        self.sessions[identifier] = session
        self._save()
        log.info("Key exchange complete (%s) for iPhone %s, info %r", suite, identifier, info.decode())
        return session

    def bluetooth_data(self, kind: int, session_id: str, data: bytes) -> None:
        if kind == protocol.KIND_PLAINTEXT:
            self._deliver(data, "bluetooth")
            return
        if kind != protocol.KIND_CIPHERTEXT:
            raise protocol.ProtocolError(f"unknown data kind {kind}")
        for session in sorted(self.sessions.values(), key=lambda s: -s.created):
            known = session.bt_contexts.get(session_id)
            candidates = [known] if known else crypto.bluetooth_feature_contexts(
                session.protocol_info, session_id)
            for context in candidates:
                try:
                    plaintext = crypto.aes_gcm_open_combined(session.export(context), data)
                except InvalidTag:
                    continue
                if not known:
                    session.bt_contexts[session_id] = context
                    self._save()
                    log.info("Bluetooth key context identified: %s", context.decode())
                self._deliver(plaintext, "bluetooth")
                return
        raise ValueError("could not decrypt Bluetooth message (no matching session; re-pair?)")

    # ----------------------------------------------------------- internet API

    def internet_data(self, payload: dict) -> None:
        """Handle a delivery that arrived through the Home Assistant webhook.

        ``payload`` = {"data": base64, "session_id": str, "push_token": hex}
        """
        data = base64.b64decode(payload["data"])
        key_id, index, ciphertext = crypto.parse_internet_message(data)
        session = next((s for s in self.sessions.values() if s.key_id == key_id), None)
        if session is None or session.incoming is None:
            raise ValueError(f"unknown key id {key_id.hex()} (re-pair the iPhone over Bluetooth)")
        backup = session.incoming.to_dict()
        message_key = session.incoming.message_key(index)
        try:
            plaintext = crypto.aes_gcm_open_combined(message_key, ciphertext)
        except InvalidTag:
            session.incoming = crypto.ReceivingRatchet.from_dict(backup)
            raise ValueError("internet message failed authentication") from None
        self._save()
        self._deliver(plaintext, "internet")

    # -------------------------------------------------------------- delivery

    def _deliver(self, plaintext: bytes, transport: str) -> None:
        try:
            note = json.loads(plaintext)
        except ValueError:
            log.warning("Notification payload is not JSON (%d bytes)", len(plaintext))
            return
        if not isinstance(note, dict):
            return
        # The system may retry on another transport; drop exact duplicates.
        digest = hashlib.sha256(plaintext).hexdigest()
        now = time.time()
        while self._recent and next(iter(self._recent.values())) < now - 600:
            self._recent.popitem(last=False)
        if digest in self._recent:
            log.debug("Dropping duplicate notification")
            return
        self._recent[digest] = now
        note["transport"] = transport
        self.on_notification(note)
