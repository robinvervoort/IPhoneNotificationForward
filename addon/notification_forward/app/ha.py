"""Home Assistant integration through the Supervisor proxy.

* Outbound: fire an event per notification and keep a sensor up to date.
* Inbound: listen on the websocket API for the event that the webhook
  automation fires when the iPhone delivers a notification over the internet.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

API = "http://supervisor/core/api"
WS = "ws://supervisor/core/websocket"
SUPERVISOR = "http://supervisor"


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession, token: str):
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}
        self._token = token

    async def fire_event(self, event_type: str, data: dict) -> None:
        async with self._session.post(f"{API}/events/{event_type}", json=data,
                                      headers=self._headers) as r:
            if r.status >= 300:
                log.warning("Firing %s failed: %s %s", event_type, r.status, await r.text())

    async def set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        async with self._session.post(f"{API}/states/{entity_id}",
                                      json={"state": state, "attributes": attributes},
                                      headers=self._headers) as r:
            if r.status >= 300:
                log.warning("Updating %s failed: %s %s", entity_id, r.status, await r.text())

    async def delete_state(self, entity_id: str) -> None:
        """Remove a state set through the REST API (not a registry entity)."""
        async with self._session.delete(f"{API}/states/{entity_id}", headers=self._headers) as r:
            if r.status == 200:
                log.info("Removed the old REST-created %s (now an MQTT entity on a device)", entity_id)

    async def mqtt_service(self) -> dict | None:
        """MQTT broker details from the Supervisor (Mosquitto app), if available."""
        try:
            async with self._session.get(f"{SUPERVISOR}/services/mqtt", headers=self._headers) as r:
                if r.status != 200:
                    log.warning("No MQTT service from Supervisor (HTTP %s)", r.status)
                    return None
                return (await r.json()).get("data") or None
        except aiohttp.ClientError as e:
            log.warning("Could not ask the Supervisor for MQTT: %s", e)
            return None

    async def config(self) -> dict:
        """Home Assistant's core config (time zone etc.); empty on failure."""
        try:
            async with self._session.get(f"{API}/config", headers=self._headers) as r:
                return await r.json() if r.status == 200 else {}
        except aiohttp.ClientError:
            return {}

    async def history(self, entity_id: str, start: str) -> list[dict] | None:
        """Every recorded state (with attributes) of one entity since `start`; None on failure."""
        # Without end_time Home Assistant returns only the first day after `start`.
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"filter_entity_id": entity_id, "significant_changes_only": "0", "end_time": end}
        # The timestamp sits in the path: encode it, or "+01:00" arrives as " 01:00".
        async with self._session.get(f"{API}/history/period/{quote(start)}", params=params,
                                     headers=self._headers) as r:
            if r.status != 200:
                log.warning("History for %s unavailable (HTTP %s)", entity_id, r.status)
                return None
            data = await r.json()
            return data[0] if data else []

    async def listen(self, event_type: str, handler: Callable[[dict], Awaitable[None] | None]) -> None:
        """Subscribe to ``event_type`` forever, reconnecting on errors."""
        delay = 1
        while True:
            try:
                async with self._session.ws_connect(WS, heartbeat=30) as ws:
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_required":
                        raise RuntimeError(f"unexpected websocket greeting {msg}")
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"websocket auth failed: {msg}")
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": event_type})
                    log.info("Listening for %s events (internet deliveries)", event_type)
                    delay = 1
                    async for raw in ws:
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            continue
                        msg = raw.json()
                        if msg.get("type") == "event":
                            result = handler(msg["event"].get("data", {}))
                            if asyncio.iscoroutine(result):
                                await result
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Websocket error: %s (retrying in %ss)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


# Notifications Home Assistant sends to itself through the iPhone (the
# "Notification Forward: health check" automation uses group nf-healthcheck).
INTERNAL_THREADS = {"nf-healthcheck"}


class NotificationSink:
    """Turns decoded notifications into HA events and a sensor."""

    MAX_ACTIVE = 50

    def __init__(self, ha: HomeAssistant, event_type: str, sensor_entity: str, mqtt=None):
        self.ha = ha
        self.event_type = event_type
        self.sensor_entity = sensor_entity
        self.mqtt = mqtt  # MqttPublisher: device entities + counters; None = REST sensor only
        self.active: dict[str, dict] = {}
        self.last: dict = {}

    async def handle(self, note: dict) -> None:
        action = note.get("action", "add")
        if note.get("thread") in INTERNAL_THREADS:
            # Home Assistant's own health-check push: only the event, so the
            # check can see it; no counters, no "last notification".
            log.info("%s: health check", action)
            await self.ha.fire_event(self.event_type, note)
            if self.mqtt is not None:
                await self.mqtt.event(note)
            return
        # Notification IDs are only unique per app, so key by app + ID.
        nid = f'{note.get("bundle_id") or ""}/{note.get("id", "")}'
        if action in ("add", "update"):
            self.active[nid] = {k: note.get(k) for k in ("app", "title", "body")}
            while len(self.active) > self.MAX_ACTIVE:
                self.active.pop(next(iter(self.active)))
            self.last = note
        elif action == "remove":
            self.active.pop(nid, None)
        elif action == "remove_all":
            self.active.clear()

        if action in ("add", "update"):
            log.info("%s: %s - %s", action, note.get("app"), note.get("title"))
        else:
            log.info("%s: %s", action, nid if action == "remove" else "all notifications")
        await self.ha.fire_event(self.event_type, note)

        last = self.last
        state = ": ".join(x for x in (last.get("app"), last.get("title") or last.get("body")) if x)
        state = (state or "none")[:255]
        attrs = {
            **{k: v for k, v in last.items() if k != "transport"},
            "active_count": len(self.active),
            "active": list(self.active.values()),
        }
        if self.mqtt is not None:
            await self.mqtt.notification(note, state, attrs)
        else:
            await self.ha.set_state(self.sensor_entity, state, {
                "friendly_name": "iPhone last notification", "icon": "mdi:cellphone-message", **attrs})
