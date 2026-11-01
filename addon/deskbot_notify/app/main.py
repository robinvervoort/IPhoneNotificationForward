"""Add-on entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp

from .ble import BleServer
from .bridge import Bridge
from .ha import HomeAssistant, NotificationSink

OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/state.json"

log = logging.getLogger("deskbot")


def load_options() -> dict:
    defaults = {
        "adapter": "hci0",
        "device_name": "DeskBot",
        "event_type": "iphone_notification",
        "sensor_entity": "sensor.iphone_last_notification",
        "inbound_event_type": "deskbot_bridge_inbound",
        "log_level": "info",
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
        sink = NotificationSink(ha, opts["event_type"], opts["sensor_entity"])
        loop = asyncio.get_running_loop()

        def on_notification(note: dict) -> None:
            loop.create_task(sink.handle(note))

        bridge = Bridge(STATE_PATH, on_notification)

        async def on_inbound(data: dict) -> None:
            try:
                bridge.internet_data(data)
            except Exception as e:  # noqa: BLE001
                log.warning("Internet delivery rejected: %s", e)

        ble = BleServer(bridge, opts["device_name"], opts["adapter"] or None)
        await ble.start()
        await ha.listen(opts["inbound_event_type"], on_inbound)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
