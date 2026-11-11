"""BLE GATT peripheral (BlueZ over D-Bus) that the iPhone pairs with."""

from __future__ import annotations

import asyncio
import logging

from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

from . import protocol
from .agent import setup_pairing
from .bridge import Bridge

log = logging.getLogger(__name__)

# iOS negotiates an ATT MTU of 185 with most adapters (182 bytes of payload).
# Stay a little below so notifications are never truncated.
NOTIFY_PACKET = 180


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
        self._agent_bus = await setup_pairing(self.adapter or "hci0")
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
        await self.server.start()
        loop.create_task(self._sender())
        log.info("Advertising as %r with service %s", self.name, protocol.SERVICE_UUID)

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
