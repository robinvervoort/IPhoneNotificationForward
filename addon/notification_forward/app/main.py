"""Add-on entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp

from . import hci_trace
from .ble import BleServer
from .bridge import Bridge
from .ha import HomeAssistant, NotificationSink
from .mqtt import CountStore, MqttPublisher

OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/state.json"
COUNTS_PATH = "/data/counts.json"
VERSION = os.environ.get("BUILD_VERSION", "")

log = logging.getLogger("notification_forward")


def load_options() -> dict:
    defaults = {
        "adapter": "hci0",
        "device_name": "NotifFwd",
        "event_type": "iphone_notification",
        "sensor_entity": "sensor.iphone_last_notification",
        "inbound_event_type": "notification_forward_inbound",
        "log_level": "info",
        "hci_trace": False,
    }
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            defaults.update(json.load(f))
    return defaults


async def run() -> None:
    opts = load_options()
    logging.basicConfig(level=opts["log_level"].upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise SystemExit("SUPERVISOR_TOKEN missing; run this as a Home Assistant add-on")

    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session, token)
        loop = asyncio.get_running_loop()

        # Device + counters through MQTT discovery when the Mosquitto app is there.
        publisher = None
        broker = await ha.mqtt_service()
        if broker:
            await ha.delete_state(opts["sensor_entity"])  # free the entity id for the MQTT sensor
            publisher = MqttPublisher(broker["host"], int(broker.get("port", 1883)), broker.get("username"),
                                      broker.get("password"), VERSION or "?", opts["sensor_entity"],
                                      CountStore(COUNTS_PATH))
            loop.create_task(publisher.run())
        else:
            log.warning("MQTT unavailable: keeping the plain %s sensor (no device, no counters)",
                        opts["sensor_entity"])
        sink = NotificationSink(ha, opts["event_type"], opts["sensor_entity"], publisher)

        def on_notification(note: dict) -> None:
            loop.create_task(sink.handle(note))

        bridge = Bridge(STATE_PATH, on_notification)

        async def on_inbound(data: dict) -> None:
            try:
                bridge.internet_data(data)
            except Exception as e:  # noqa: BLE001
                log.warning("Internet delivery rejected: %s", e)

        if opts.get("hci_trace"):
            hci_trace.start(loop)

        ble = BleServer(bridge, opts["device_name"], opts["adapter"] or None)
        await ble.start()
        await ha.listen(opts["inbound_event_type"], on_inbound)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
