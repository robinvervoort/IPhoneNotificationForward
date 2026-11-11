"""Home Assistant integration through the Supervisor proxy.

* Outbound: fire an event per notification and keep a sensor up to date.
* Inbound: listen on the websocket API for the event that the webhook
  automation fires when the iPhone delivers a notification over the internet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

API = "http://supervisor/core/api"
WS = "ws://supervisor/core/websocket"


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


class NotificationSink:
    """Turns decoded notifications into HA events and a sensor."""

    MAX_ACTIVE = 50

    def __init__(self, ha: HomeAssistant, event_type: str, sensor_entity: str):
        self.ha = ha
        self.event_type = event_type
        self.sensor_entity = sensor_entity
        self.active: dict[str, dict] = {}
        self.last: dict = {}

    async def handle(self, note: dict) -> None:
        action = note.get("action", "add")
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
        await self.ha.set_state(self.sensor_entity, (state or "none")[:255], {
            "friendly_name": "iPhone last notification",
            "icon": "mdi:cellphone-message",
            **{k: v for k, v in last.items() if k != "transport"},
            "active_count": len(self.active),
            "active": list(self.active.values()),
        })
