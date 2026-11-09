"""Low-level Bluetooth trace (like `btmon`), filtered to pairing/encryption.

Reads the kernel's HCI monitor channel and logs only what explains a failed
LE pairing: SMP pairing PDUs in both directions (with "Pairing Failed"
reasons and the bonding/SC/MITM flags), encryption changes, long-term-key
requests/replies and disconnect reasons, plus failed controller commands.

Needs the add-on to run with host networking (Bluetooth sockets only exist
in the host network namespace) and the NET_RAW capability.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import socket
import struct

log = logging.getLogger(__name__)

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_MONITOR = 2

# Monitor opcodes
MON_COMMAND, MON_EVENT, MON_ACL_TX, MON_ACL_RX = 2, 3, 4, 5

SMP_CID = 0x0006
SMP_CODES = {
    0x01: "Pairing Request", 0x02: "Pairing Response", 0x03: "Pairing Confirm",
    0x04: "Pairing Random", 0x05: "Pairing Failed", 0x06: "Encryption Information (LTK)",
    0x07: "Central Identification", 0x08: "Identity Information (IRK)",
    0x09: "Identity Address Information", 0x0A: "Signing Information",
    0x0B: "Security Request", 0x0C: "Pairing Public Key", 0x0D: "Pairing DHKey Check",
    0x0E: "Pairing Keypress Notification",
}
SMP_FAIL_REASONS = {
    0x01: "Passkey Entry Failed", 0x02: "OOB Not Available", 0x03: "Authentication Requirements",
    0x04: "Confirm Value Failed", 0x05: "Pairing Not Supported", 0x06: "Encryption Key Size",
    0x07: "Command Not Supported", 0x08: "Unspecified Reason", 0x09: "Repeated Attempts",
    0x0A: "Invalid Parameters", 0x0B: "DHKey Check Failed", 0x0C: "Numeric Comparison Failed",
    0x0D: "BR/EDR pairing in progress", 0x0E: "Cross-transport Key Derivation Not Allowed",
    0x0F: "Key Rejected",
}
IO_CAPS = {0: "DisplayOnly", 1: "DisplayYesNo", 2: "KeyboardOnly", 3: "NoInputNoOutput", 4: "KeyboardDisplay"}
HCI_ERRORS = {
    0x05: "Authentication Failure", 0x06: "PIN or Key Missing", 0x08: "Connection Timeout",
    0x13: "Remote User Terminated", 0x16: "Local Host Terminated", 0x3D: "MIC Failure",
    0x3E: "Connection Failed to be Established", 0x0C: "Command Disallowed", 0x12: "Invalid Parameters",
}
INTERESTING_COMMANDS = {0x2019: "LE Start Encryption", 0x201A: "LE LTK Request Reply",
                        0x201B: "LE LTK Request Negative Reply"}


def _keys(b: int) -> str:
    names = [n for bit, n in ((1, "EncKey"), (2, "IdKey"), (4, "SignKey"), (8, "LinkKey")) if b & bit]
    return "+".join(names) or "none"


def _authreq(b: int) -> str:
    flags = ["Bonding" if b & 0x03 == 0x01 else "NoBonding"]
    if b & 0x04:
        flags.append("MITM")
    if b & 0x08:
        flags.append("SC")
    if b & 0x20:
        flags.append("CT2")
    return "|".join(flags)


def describe_smp(pdu: bytes) -> str:
    code = pdu[0]
    name = SMP_CODES.get(code, f"SMP 0x{code:02x}")
    if code in (0x01, 0x02) and len(pdu) >= 7:
        io, oob, auth, keysize, init_keys, resp_keys = pdu[1:7]
        return (f"{name}: io={IO_CAPS.get(io, io)} oob={oob} auth={_authreq(auth)} keysize={keysize} "
                f"initiator_keys={_keys(init_keys)} responder_keys={_keys(resp_keys)}")
    if code == 0x05 and len(pdu) >= 2:
        return f"{name}: reason 0x{pdu[1]:02x} ({SMP_FAIL_REASONS.get(pdu[1], 'unknown')})"
    if code == 0x0B and len(pdu) >= 2:
        return f"{name}: auth={_authreq(pdu[1])}"
    if code == 0x09 and len(pdu) >= 8:
        addr = ":".join(f"{x:02X}" for x in reversed(pdu[2:8]))
        return f"{name}: {'random' if pdu[1] else 'public'} {addr}"
    return name


def _err(status: int) -> str:
    return f"0x{status:02x} ({HCI_ERRORS.get(status, 'see HCI error codes')})"


def describe(opcode: int, data: bytes) -> str | None:
    """Return a log line for an interesting monitor frame, else None."""
    if opcode in (MON_ACL_TX, MON_ACL_RX) and len(data) >= 9:
        (acl_len,) = struct.unpack_from("<H", data, 2)
        l2_len, cid = struct.unpack_from("<HH", data, 4)
        if cid == SMP_CID and l2_len >= 1:
            direction = "bridge -> iPhone" if opcode == MON_ACL_TX else "iPhone -> bridge"
            return f"SMP {direction}: {describe_smp(data[8:8 + l2_len])}"
        return None
    if opcode == MON_EVENT and len(data) >= 2:
        code, params = data[0], data[2:]
        if code == 0x05 and len(params) >= 4:  # Disconnection Complete
            handle = struct.unpack_from("<H", params, 1)[0] & 0x0FFF
            return f"Disconnected handle {handle}: reason {_err(params[3])}"
        if code == 0x08 and len(params) >= 4:  # Encryption Change
            handle = struct.unpack_from("<H", params, 1)[0] & 0x0FFF
            status = "ok" if params[0] == 0 else _err(params[0])
            return f"Encryption change handle {handle}: {'ON' if params[3] else 'OFF'} (status {status})"
        if code == 0x3E and params:  # LE Meta
            sub = params[0]
            if sub in (0x01, 0x0A) and len(params) >= 12:
                status, role, addr_type = params[1], params[4], params[5]
                (handle,) = struct.unpack_from("<H", params, 2)
                addr = ":".join(f"{x:02X}" for x in reversed(params[6:12]))
                if status:
                    return f"LE connection failed: {_err(status)}"
                return (f"LE connected handle {handle & 0x0FFF}: peer {addr} "
                        f"({'random' if addr_type & 1 else 'public'}), role {'peripheral' if role else 'central'}")
            if sub == 0x05 and len(params) >= 3:
                handle = struct.unpack_from("<H", params, 1)[0] & 0x0FFF
                return f"iPhone asks bridge for its LTK (handle {handle})"
        if code == 0x0E and len(params) >= 3:  # Command Complete
            (op,) = struct.unpack_from("<H", params, 1)
            if op in INTERESTING_COMMANDS:
                st = params[3] if len(params) > 3 else 0
                return f"{INTERESTING_COMMANDS[op]} completed: {'ok' if st == 0 else _err(st)}"
        if code == 0x0F and len(params) >= 4:  # Command Status
            status, (op,) = params[0], struct.unpack_from("<H", params, 2)
            if status:
                return f"Controller rejected command 0x{op:04x}: {_err(status)}"
        return None
    if opcode == MON_COMMAND and len(data) >= 3:
        (op,) = struct.unpack_from("<H", data, 0)
        if op == 0x201B:
            return "Bridge has NO stored LTK for this iPhone (sending LTK Negative Reply)"
        if op == 0x201A:
            return "Bridge replies with its stored LTK"
    return None


class _SockaddrHCI(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]


def open_monitor() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI)
    if fd < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    addr = _SockaddrHCI(AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
    if libc.bind(fd, ctypes.byref(addr), ctypes.sizeof(addr)) < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise OSError(err, os.strerror(err))
    os.set_blocking(fd, False)
    return fd


def start(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        fd = open_monitor()
    except OSError as e:
        log.warning("HCI trace unavailable (%s); needs host_network and NET_RAW", e)
        return False

    def on_readable() -> None:
        while True:
            try:
                frame = os.read(fd, 65535)
            except BlockingIOError:
                return
            except OSError as e:
                log.warning("HCI trace stopped: %s", e)
                loop.remove_reader(fd)
                return
            if len(frame) < 6:
                continue
            opcode, _index, length = struct.unpack_from("<HHH", frame, 0)
            try:
                line = describe(opcode, frame[6:6 + length])
            except (struct.error, IndexError):
                line = None
            if line:
                log.info("HCI %s", line)

    loop.add_reader(fd, on_readable)
    log.info("HCI trace active (pairing, encryption, LTK and disconnect events)")
    return True
