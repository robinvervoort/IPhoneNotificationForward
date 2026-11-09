import struct

from app import hci_trace as t


def acl(handle: int, smp: bytes) -> bytes:
    l2 = struct.pack("<HH", len(smp), 0x0006) + smp
    return struct.pack("<HH", handle, len(l2)) + l2


def test_smp_pairing_request_and_failure():
    req = bytes([0x01, 0x03, 0x00, 0x2D, 16, 0x0F, 0x0F])  # NoInputNoOutput, Bonding|MITM|SC|CT2
    line = t.describe(t.MON_ACL_RX, acl(0x40, req))
    assert line.startswith("SMP iPhone -> bridge: Pairing Request")
    assert "io=NoInputNoOutput" in line and "Bonding|MITM|SC|CT2" in line
    assert "initiator_keys=EncKey+IdKey+SignKey+LinkKey" in line
    fail = t.describe(t.MON_ACL_TX, acl(0x40, bytes([0x05, 0x03])))
    assert fail == "SMP bridge -> iPhone: Pairing Failed: reason 0x03 (Authentication Requirements)"


def test_events():
    disc = bytes([0x05, 4, 0x00]) + struct.pack("<H", 0x40) + bytes([0x13])
    assert t.describe(t.MON_EVENT, disc) == "Disconnected handle 64: reason 0x13 (Remote User Terminated)"
    enc = bytes([0x08, 4, 0x00]) + struct.pack("<H", 0x40) + bytes([1])
    assert t.describe(t.MON_EVENT, enc) == "Encryption change handle 64: ON (status ok)"
    conn = bytes([0x3E, 19, 0x01, 0x00]) + struct.pack("<H", 0x40) + bytes([1, 1]) \
        + bytes([0x9F, 0x94, 0xE1, 0xB4, 0x35, 0x68]) + b"\x00" * 7
    assert t.describe(t.MON_EVENT, conn) == \
        "LE connected handle 64: peer 68:35:B4:E1:94:9F (random), role peripheral"
    ltk = bytes([0x3E, 13, 0x05]) + struct.pack("<H", 0x40) + b"\x00" * 10
    assert t.describe(t.MON_EVENT, ltk) == "iPhone asks bridge for its LTK (handle 64)"


def test_commands_and_ignored_traffic():
    neg = struct.pack("<HB", 0x201B, 2) + struct.pack("<H", 0x40)
    assert "NO stored LTK" in t.describe(t.MON_COMMAND, neg)
    att = struct.pack("<HH", 0x40, 7) + struct.pack("<HH", 3, 0x0004) + b"\x0a\x01\x00"
    assert t.describe(t.MON_ACL_RX, att) is None  # ATT traffic is not logged
    assert t.describe(t.MON_EVENT, bytes([0x13, 0])) is None
