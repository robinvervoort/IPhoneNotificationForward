"""Watch BlueZ devices on the adapter: log bonds, connects and removals.

iPhones use rotating private addresses; a bonded peer is recognised by the
identity key stored during pairing. If the bridge keeps forgetting the
iPhone ("Peer removed pairing information" on the phone), these logs show
whether the bond is stored (Paired/Bonded), whether it gets removed (and
when), and mark paired devices Trusted so BlueZ treats them as known.
"""

from __future__ import annotations

import asyncio
import logging

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

log = logging.getLogger(__name__)

DEVICE = "org.bluez.Device1"
WATCHED = ("Connected", "Paired", "Bonded", "Trusted", "ServicesResolved", "AddressType")


def _val(v):
    return v.value if isinstance(v, Variant) else v


def _describe(path: str, props: dict) -> str:
    p = {k: _val(v) for k, v in props.items()}
    return (f"{p.get('Address', path.rsplit('/', 1)[-1])} ({p.get('AddressType', '?')}) "
            f"name={p.get('Name') or p.get('Alias') or '-'} paired={p.get('Paired')} "
            f"bonded={p.get('Bonded')} trusted={p.get('Trusted')} connected={p.get('Connected')}")


async def _add_match(bus: MessageBus, rule: str) -> None:
    await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                           interface="org.freedesktop.DBus", member="AddMatch",
                           signature="s", body=[rule]))


async def _set_trusted(bus: MessageBus, path: str) -> None:
    reply = await bus.call(Message(destination="org.bluez", path=path,
                                   interface="org.freedesktop.DBus.Properties", member="Set",
                                   signature="ssv", body=[DEVICE, "Trusted", Variant("b", True)]))
    if reply.message_type == MessageType.ERROR:
        log.warning("Could not mark %s trusted: %s", path, reply.body)
    else:
        log.info("Marked %s trusted", path.rsplit("/", 1)[-1])


async def watch_devices(bus: MessageBus, adapter: str) -> None:
    prefix = f"/org/bluez/{adapter}/dev_"
    loop = asyncio.get_running_loop()

    reply = await bus.call(Message(destination="org.bluez", path="/",
                                   interface="org.freedesktop.DBus.ObjectManager",
                                   member="GetManagedObjects"))
    devices = {p: i[DEVICE] for p, i in (reply.body[0] if reply.body else {}).items()
               if p.startswith(prefix) and DEVICE in i}
    paired = [p for p, d in devices.items() if _val(d.get("Paired"))]
    log.info("BlueZ knows %d device(s) on %s, %d paired", len(devices), adapter, len(paired))
    for p in paired:
        log.info("  paired: %s", _describe(p, devices[p]))
        if not _val(devices[p].get("Trusted")):
            loop.create_task(_set_trusted(bus, p))

    def on_message(msg: Message):
        if msg.message_type != MessageType.SIGNAL or not msg.path:
            return
        if msg.member == "PropertiesChanged" and msg.path.startswith(prefix) and msg.body[0] == DEVICE:
            changed = {k: _val(v) for k, v in msg.body[1].items() if k in WATCHED}
            if changed:
                log.info("Device %s: %s", msg.path.rsplit("/", 1)[-1], changed)
            if changed.get("Paired") is True:
                loop.create_task(_set_trusted(bus, msg.path))
        elif msg.member == "InterfacesRemoved" and msg.body and str(msg.body[0]).startswith(prefix) \
                and DEVICE in msg.body[1]:
            log.info("Device %s dropped from BlueZ's cache", str(msg.body[0]).rsplit("/", 1)[-1])
        elif msg.member == "InterfacesAdded" and msg.body and str(msg.body[0]).startswith(prefix) \
                and DEVICE in msg.body[1]:
            d = msg.body[1][DEVICE]
            if _val(d.get("Paired")):
                log.info("Device added: %s", _describe(str(msg.body[0]), d))

    bus.add_message_handler(on_message)
    await _add_match(bus, f"type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                          f"member='PropertiesChanged',arg0='{DEVICE}'")
    await _add_match(bus, "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'")
