import asyncio

from dbus_next import Message, MessageType, Variant

from app import bonds

IPHONE = "/org/bluez/hci0/dev_44_33_81_29_F9_F3"
OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class FakeBus:
    def __init__(self, fail_disconnects=0):
        self.handlers, self.calls, self.fail = [], [], fail_disconnects

    async def call(self, m):
        self.calls.append((m.member, m.path))
        if m.member == "GetManagedObjects":
            return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1, signature="a{oa{sa{sv}}}",
                           body=[{IPHONE: {"org.bluez.Device1": {
                               "Address": Variant("s", "E4:9C:67:3C:11:79"), "AddressType": Variant("s", "public"),
                               "Paired": Variant("b", True), "Bonded": Variant("b", True),
                               "Connected": Variant("b", False)}}}])
        if m.member == "Disconnect" and self.fail:
            self.fail -= 1
            return Message(message_type=MessageType.ERROR, reply_serial=1, error_name="org.bluez.Error.NotConnected")
        return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)

    def add_message_handler(self, h):
        self.handlers.append(h)

    def connected(self, path, value=True):
        sig = Message.new_signal(path, "org.freedesktop.DBus.Properties", "PropertiesChanged", "sa{sv}as",
                                 ["org.bluez.Device1", {"Connected": Variant("b", value)}, []])
        for h in self.handlers:
            h(sig)


def disconnects(bus):
    return [p for m, p in bus.calls if m == "Disconnect"]


def test_autoconnect_disabled_once_for_paired_device(monkeypatch):
    monkeypatch.setattr(bonds, "DISABLE_AUTOCONNECT_DELAY", 0)

    async def go():
        bus = FakeBus()
        await bonds.watch_devices(bus, "hci0")
        bus.connected(OTHER)          # never paired: left alone
        bus.connected(IPHONE)
        await asyncio.sleep(0.01)
        bus.connected(IPHONE)         # already disabled: no second disconnect
        await asyncio.sleep(0.01)
        return bus

    bus = asyncio.run(go())
    assert disconnects(bus) == [IPHONE]
    assert ("Set", IPHONE) not in bus.calls  # no longer marks devices trusted


def test_autoconnect_retried_when_disconnect_fails(monkeypatch):
    monkeypatch.setattr(bonds, "DISABLE_AUTOCONNECT_DELAY", 0)

    async def go():
        bus = FakeBus(fail_disconnects=1)
        await bonds.watch_devices(bus, "hci0")
        bus.connected(IPHONE)
        await asyncio.sleep(0.01)
        bus.connected(IPHONE)
        await asyncio.sleep(0.01)
        return bus

    assert disconnects(asyncio.run(go())) == [IPHONE, IPHONE]
