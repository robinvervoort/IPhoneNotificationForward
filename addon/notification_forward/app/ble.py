"""BLE GATT peripheral (BlueZ over D-Bus) that the iPhone pairs with."""

from __future__ import annotations

import asyncio
import logging

from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties
from dbus_next import Variant

from . import protocol
from .agent import resolve_adapter, setup_pairing
from .bridge import Bridge

log = logging.getLogger(__name__)

# iOS negotiates an ATT MTU of 185 with most adapters (182 bytes of payload).
# Stay a little below so notifications are never truncated.
NOTIFY_PACKET = 180


async def advertise_with_retry(first_start, retry_advertising, power_cycle, sleep=asyncio.sleep,
                               max_delay: float = 60) -> int:
    """Start the GATT server, retrying the advertisement until BlueZ accepts it.

    BlueZ sometimes refuses to (re)enable advertising, e.g. "Opcode 0x2039
    failed: -31" when the controller has stale connection state. From the
    second failure on, the adapter is power-cycled before retrying. Never
    raises for advertising failures, so the rest of the bridge keeps running.
    Returns the number of attempts it took.
    """
    attempt = 1
    try:
        await first_start()
        return attempt
    except Exception as e:  # noqa: BLE001 - BlueZ reports this as a generic DBusError
        log.warning("Advertising failed (%s); Bluetooth retries in the background, "
                    "the internet route keeps working", e)
    while True:
        attempt += 1
        if attempt >= 3:
            try:
                await power_cycle()
            except Exception as e:  # noqa: BLE001
                log.warning("Could not power-cycle the Bluetooth adapter: %s", e)
        await sleep(min(max_delay, 5 * attempt))
        try:
            await retry_advertising()
            log.info("Advertising started after %d attempts", attempt)
            return attempt
        except Exception as e:  # noqa: BLE001
            log.warning("Advertising attempt %d failed: %s", attempt, e)


class BleServer:
    def __init__(self, bridge: Bridge, name: str, adapter: str | None):
        self.bridge = bridge
        self.name = name
        self.adapter = adapter
        self.rx = protocol.Reassembler()
        self.server: BlessServer | None = None
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._agent_bus = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # iOS only forwards notifications over a bonded (encrypted) LE link.
        self.adapter = await resolve_adapter(self.adapter or "hci0")
        self._agent_bus = await setup_pairing(self.adapter)
        self.server = BlessServer(name=self.name, loop=loop, adapter=self.adapter)
        self.server.read_request_func = self._on_read
        self.server.write_request_func = self._on_write
        await self.server.add_new_service(protocol.SERVICE_UUID)
        await self.server.add_new_characteristic(
            protocol.SERVICE_UUID, protocol.RX_UUID,
            GATTCharacteristicProperties.write,
            None, GATTAttributePermissions.writeable | GATTAttributePermissions.write_encryption_required)
        await self.server.add_new_characteristic(
            protocol.SERVICE_UUID, protocol.TX_UUID,
            GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
            bytearray(), GATTAttributePermissions.readable)
        loop.create_task(self._sender())
        await advertise_with_retry(self.server.start, self._retry_advertising, self._power_cycle)
        log.info("Advertising as %r with service %s", self.name, protocol.SERVICE_UUID)

    async def _retry_advertising(self) -> None:
        # The GATT application stays registered; only the advertisement failed.
        assert self.server is not None
        await self.server.app.start_advertising(self.server.adapter)

    async def _power_cycle(self) -> None:
        """Adapter off and on clears a controller stuck refusing advertising."""
        assert self.server is not None
        props = self.server.adapter.get_interface("org.freedesktop.DBus.Properties")
        log.info("Power-cycling the Bluetooth adapter to clear its state")
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", False))
        await asyncio.sleep(2)
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await asyncio.sleep(2)

    def _on_read(self, characteristic, **_kwargs) -> bytearray:
        return characteristic.value or bytearray()

    def _on_write(self, characteristic, value, **_kwargs) -> None:
        if str(characteristic.uuid).upper() != protocol.RX_UUID:
            return
        try:
            message = self.rx.feed(bytes(value))
        except protocol.ProtocolError as e:
            log.warning("Dropped BLE packet: %s", e)
            return
        if message is None:
            return
        response = self.bridge.handle_message(message)
        if response:
            packets = protocol.chunk(response, NOTIFY_PACKET)
            log.info("Reply 0x%02x: %d bytes in %d notifications of <= %d bytes",
                     response[0], len(response), len(packets), NOTIFY_PACKET)
            for packet in packets:
                self._outbox.put_nowait(packet)

    async def _sender(self) -> None:
        """Send notifications one at a time so BlueZ doesn't coalesce them."""
        assert self.server is not None
        while True:
            packet = await self._outbox.get()
            char = self.server.get_characteristic(protocol.TX_UUID)
            char.value = bytearray(packet)
            self.server.update_value(protocol.SERVICE_UUID, protocol.TX_UUID)
            await asyncio.sleep(0.015)
