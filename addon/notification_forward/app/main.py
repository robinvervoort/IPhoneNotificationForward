"""Add-on entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from . import hci_trace
from .ble import BleServer
from .bridge import Bridge
from .ha import INTERNAL_THREADS, HomeAssistant, NotificationSink
from .mqtt import CountStore, MqttPublisher
from .stats import WeeklyStats, parse_rules

OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/state.json"
COUNTS_PATH = "/data/counts.json"
STATS_PATH = "/data/stats.json"
VERSION = os.environ.get("BUILD_VERSION", "")

log = logging.getLogger("notification_forward")


async def load_stats(ha: HomeAssistant, opts: dict) -> WeeklyStats:
    """7-day statistics; on first start, filled from Home Assistant's history."""
    tz = None
    name = (await ha.config()).get("time_zone")
    if name:
        try:
            tz = ZoneInfo(name)
        except Exception:  # noqa: BLE001
            log.warning("Unknown time zone %s; using the container's", name)
    stats = WeeklyStats(STATS_PATH, parse_rules(opts.get("summaries")), tz)
    if stats.loaded:
        return stats
    start = (datetime.now().astimezone() - timedelta(days=7)).isoformat(timespec="seconds")
    try:
        states = await ha.history(opts["sensor_entity"], start)
    except aiohttp.ClientError as e:
        log.warning("Could not read history to fill the 7-day statistics: %s", e)
        states = []
    counted = 0
    for st in states:
        attrs = st.get("attributes") or {}
        if attrs.get("action") != "add" or attrs.get("thread") in INTERNAL_THREADS:
            continue
        try:
            when = datetime.fromisoformat(st["last_updated"])
        except (KeyError, ValueError):
            continue
        counted += stats.record(attrs, when, save=False)
    stats.save()
    log.info("7-day statistics started from history: %d notifications", counted)
    return stats


def load_options() -> dict:
    defaults = {
        "adapter": "hci0",
        "device_name": "NotifFwd",
        "event_type": "iphone_notification",
        "sensor_entity": "sensor.iphone_last_notification",
        "inbound_event_type": "notification_forward_inbound",
        "log_level": "info",
        "hci_trace": False,
        "summaries": [],
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
                                      CountStore(COUNTS_PATH), await load_stats(ha, opts))
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

        async def start_bluetooth() -> None:
            # Bluetooth problems must never take the internet route down with them.
            try:
                await ble.start()
            except Exception:  # noqa: BLE001
                log.exception("Bluetooth could not start; internet deliveries keep working")

        loop.create_task(start_bluetooth())
        await ha.listen(opts["inbound_event_type"], on_inbound)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
