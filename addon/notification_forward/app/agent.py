"""BlueZ pairing agent so the iPhone can bond with the bridge.

AccessorySetupKit pairs with `bluetoothPairingLE`, and iOS only forwards
notifications to an accessory it has a secure (bonded) LE link with. The
bridge has no display or buttons, so it registers a NoInputNoOutput agent
that accepts "Just Works" pairing and makes the adapter pairable.

No `from __future__ import annotations` here: dbus-next reads the D-Bus
signatures ('o', 's', 'u') from the real annotation strings.
"""

# ruff: noqa: F821  (dbus-next reads D-Bus type codes like "o" from annotations)
import logging

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/be/robinvervoort/notificationforward/agent"


class JustWorksAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        log.info("Pairing agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        return "000000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        log.info("Accepting pairing with %s", device)

    @method()
    def RequestAuthorization(self, device: "o"):
        log.info("Authorizing pairing with %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def Cancel(self):
        log.info("Pairing cancelled")


def pick_adapter(wanted: str, available: list[str]) -> str:
    """The configured adapter if it exists, else the first one there is.

    Linux renumbers a USB dongle (hci0 -> hci1) when it is detected again,
    for example after a Home Assistant OS update or a VM reboot."""
    adapters = sorted(a for a in available if a.startswith("hci"))
    if wanted in adapters or not adapters:
        return wanted
    log.warning("Bluetooth adapter %s not found; using %s instead (set 'adapter' to it to hide this)",
                wanted, adapters[0])
    return adapters[0]


async def resolve_adapter(wanted: str) -> str:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        node = await bus.introspect("org.bluez", "/org/bluez")
        return pick_adapter(wanted, [child.name for child in node.nodes])
    finally:
        bus.disconnect()


async def setup_pairing(adapter: str) -> MessageBus:
    """Register the agent and make `adapter` pairable. Returns the bus to keep alive."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export(AGENT_PATH, JustWorksAgent())

    root = bus.get_proxy_object("org.bluez", "/org/bluez", await bus.introspect("org.bluez", "/org/bluez"))
    manager = root.get_interface("org.bluez.AgentManager1")
    await manager.call_register_agent(AGENT_PATH, "NoInputNoOutput")
    try:
        await manager.call_request_default_agent(AGENT_PATH)
    except Exception as e:  # noqa: BLE001 - another default agent may exist
        log.warning("Could not become the default pairing agent: %s", e)

    path = f"/org/bluez/{adapter}"
    obj = bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path))
    props = obj.get_interface("org.freedesktop.DBus.Properties")
    await props.call_set("org.bluez.Adapter1", "Pairable", Variant("b", True))
    await props.call_set("org.bluez.Adapter1", "PairableTimeout", Variant("u", 0))
    log.info("Pairing agent registered; %s is pairable (Just Works)", adapter)
    try:
        from .bonds import watch_devices
        await watch_devices(bus, adapter)
    except Exception as e:  # noqa: BLE001 - diagnostics must never stop the bridge
        log.warning("Device watcher unavailable: %s", e)
    return bus
