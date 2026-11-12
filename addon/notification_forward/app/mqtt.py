"""MQTT discovery: one Home Assistant device with the bridge's entities.

Entities (all under the device "iPhone Notification Forward"):
* sensor  Last notification   state "App: Title", full notification as attributes
* event   Notification        event types add / update / remove / remove_all
* sensor  Notifications       total count of new notifications
* sensor  <App> notifications one counter per app, created on its first notification
* sensor  Notifications today
* sensor  Notifications (7 days)  attributes: top_apps, per_day
* sensor  Notifications last night  the night window (e.g. 23:00-06:30): top_apps,
          alerts (summary matches), items
* sensor  <Summary> alerts (7 days)  one per configured alert summary (e.g. Zabbix):
          attributes top_<group> (e.g. top_problem, top_host), per_day, recent

Counters are `total_increasing`, so Home Assistant keeps long-term
statistics (per hour/day/month) for them. Counts persist in /data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime

import aiomqtt

from .stats import SummaryRule, WeeklyStats

log = logging.getLogger(__name__)

PREFIX = "notification_forward"
DISCOVERY = "homeassistant"
AVAILABILITY = f"{PREFIX}/status"
LAST_STATE = f"{PREFIX}/last/state"
LAST_ATTRS = f"{PREFIX}/last/attributes"
EVENT = f"{PREFIX}/event"
EVENT_TYPES = ["add", "update", "remove", "remove_all"]
TODAY = f"{PREFIX}/stats/today"
WEEK_STATE = f"{PREFIX}/stats/week/state"
WEEK_ATTRS = f"{PREFIX}/stats/week/attributes"
NIGHT_STATE = f"{PREFIX}/stats/night/state"
NIGHT_ATTRS = f"{PREFIX}/stats/night/attributes"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "unknown"


class CountStore:
    """Per-app counts of new notifications, persisted as JSON."""

    def __init__(self, path: str):
        self.path = path
        self.apps: dict[str, dict] = {}  # bundle_id -> {"name": str, "count": int}
        self.total = 0
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            self.apps = data.get("apps", {})
            self.total = int(data.get("total", 0))

    def increment(self, bundle_id: str, name: str) -> bool:
        """Count one notification; returns True when the app is new."""
        new = bundle_id not in self.apps
        entry = self.apps.setdefault(bundle_id, {"name": name, "count": 0})
        entry["count"] += 1
        if name:
            entry["name"] = name
        self.total += 1
        self._save()
        return new

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".counts")
        with os.fdopen(fd, "w") as f:
            json.dump({"total": self.total, "apps": self.apps}, f)
        os.replace(tmp, self.path)


def device(version: str) -> dict:
    return {
        "identifiers": [f"{PREFIX}_bridge"],
        "name": "iPhone Notification Forward",
        "manufacturer": "IPhoneNotificationForward",
        "model": "Notification bridge",
        "sw_version": version,
    }


def _common(version: str) -> dict:
    return {
        "device": device(version),
        "availability_topic": AVAILABILITY,
        "origin": {"name": "iPhone Notification Forward", "sw_version": version,
                   "support_url": "https://github.com/robinvervoort/IPhoneNotificationForward"},
    }


def last_config(version: str, entity_id: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/sensor/{PREFIX}/last/config", {
        **_common(version),
        "name": "Last notification",
        "unique_id": f"{PREFIX}_last",
        "default_entity_id": entity_id,
        "state_topic": LAST_STATE,
        "json_attributes_topic": LAST_ATTRS,
        "icon": "mdi:cellphone-message",
    }


def event_config(version: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/event/{PREFIX}/notification/config", {
        **_common(version),
        "name": "Notification",
        "unique_id": f"{PREFIX}_event",
        "default_entity_id": "event.iphone_notification",
        "state_topic": EVENT,
        "event_types": EVENT_TYPES,
        "icon": "mdi:bell-ring",
    }


def total_config(version: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/sensor/{PREFIX}/count_total/config", {
        **_common(version),
        "name": "Notifications",
        "unique_id": f"{PREFIX}_count_total",
        "default_entity_id": "sensor.iphone_notifications_total",
        "state_topic": f"{PREFIX}/count/total",
        "state_class": "total_increasing",
        "unit_of_measurement": "notifications",
        "icon": "mdi:counter",
    }


def app_config(version: str, bundle_id: str, name: str) -> tuple[str, dict]:
    key = slug(bundle_id)
    return f"{DISCOVERY}/sensor/{PREFIX}/count_{key}/config", {
        **_common(version),
        "name": f"{name or bundle_id} notifications",
        "unique_id": f"{PREFIX}_count_{key}",
        "default_entity_id": f"sensor.iphone_notifications_{slug(name or bundle_id)}",
        "state_topic": f"{PREFIX}/count/{key}",
        "state_class": "total_increasing",
        "unit_of_measurement": "notifications",
        "icon": "mdi:counter",
    }


def today_config(version: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/sensor/{PREFIX}/today/config", {
        **_common(version),
        "name": "Notifications today",
        "unique_id": f"{PREFIX}_today",
        "default_entity_id": "sensor.iphone_notifications_today",
        "state_topic": TODAY,
        "unit_of_measurement": "notifications",
        "icon": "mdi:calendar-today",
    }


def week_config(version: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/sensor/{PREFIX}/week/config", {
        **_common(version),
        "name": "Notifications (7 days)",
        "unique_id": f"{PREFIX}_week",
        "default_entity_id": "sensor.iphone_notifications_7_days",
        "state_topic": WEEK_STATE,
        "json_attributes_topic": WEEK_ATTRS,
        "unit_of_measurement": "notifications",
        "icon": "mdi:calendar-week",
    }


def night_config(version: str) -> tuple[str, dict]:
    return f"{DISCOVERY}/sensor/{PREFIX}/night/config", {
        **_common(version),
        "name": "Notifications last night",
        "unique_id": f"{PREFIX}_night",
        "default_entity_id": "sensor.iphone_notifications_last_night",
        "state_topic": NIGHT_STATE,
        "json_attributes_topic": NIGHT_ATTRS,
        "unit_of_measurement": "notifications",
        "icon": "mdi:weather-night",
    }


def summary_topics(rule: SummaryRule) -> tuple[str, str]:
    key = slug(rule.name)
    return f"{PREFIX}/stats/summary_{key}/state", f"{PREFIX}/stats/summary_{key}/attributes"


def summary_config(version: str, rule: SummaryRule) -> tuple[str, dict]:
    key = slug(rule.name)
    state, attrs = summary_topics(rule)
    return f"{DISCOVERY}/sensor/{PREFIX}/summary_{key}/config", {
        **_common(version),
        "name": f"{rule.name} alerts (7 days)",
        "unique_id": f"{PREFIX}_summary_{key}",
        "default_entity_id": f"sensor.iphone_{key}_alerts_7_days",
        "state_topic": state,
        "json_attributes_topic": attrs,
        "unit_of_measurement": "alerts",
        "icon": "mdi:alert-circle-outline",
    }


class MqttPublisher:
    """Keeps an MQTT connection and publishes discovery, states and counters."""

    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 version: str, last_entity_id: str, counts: CountStore, stats: WeeklyStats | None = None):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.version = version
        self.last_entity_id = last_entity_id
        self.counts = counts
        self.stats = stats
        self.client: aiomqtt.Client | None = None
        self.last_state: str | None = None
        self.last_attrs: dict | None = None

    # -- publishing -------------------------------------------------------

    async def _pub(self, topic: str, payload, retain: bool = True) -> None:
        if self.client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        await self.client.publish(topic, payload, qos=1, retain=retain)

    async def publish_all(self) -> None:
        """Discovery for every entity plus current states (on connect / HA restart)."""
        for topic, config in (last_config(self.version, self.last_entity_id),
                              event_config(self.version), total_config(self.version)):
            await self._pub(topic, config)
        for bundle_id, entry in self.counts.apps.items():
            await self._publish_app(bundle_id, entry["name"], discovery=True)
        await self._pub(f"{PREFIX}/count/total", str(self.counts.total))
        if self.stats is not None:
            await self._pub(*today_config(self.version))
            await self._pub(*week_config(self.version))
            if self.stats.night is not None:
                await self._pub(*night_config(self.version))
            for rule in self.stats.rules:
                await self._pub(*summary_config(self.version, rule))
            await self.publish_stats()
        if self.last_state is not None:
            await self._pub(LAST_STATE, self.last_state)
            await self._pub(LAST_ATTRS, self.last_attrs or {})
        await self._pub(AVAILABILITY, "online")

    async def _publish_app(self, bundle_id: str, name: str, discovery: bool) -> None:
        if discovery:
            topic, config = app_config(self.version, bundle_id, name)
            await self._pub(topic, config)
        await self._pub(f"{PREFIX}/count/{slug(bundle_id)}", str(self.counts.apps[bundle_id]["count"]))

    async def publish_stats(self) -> None:
        if self.stats is None:
            return
        self.stats.prune()
        await self._pub(TODAY, str(self.stats.today()))
        week = self.stats.week()
        await self._pub(WEEK_STATE, str(week["total"]))
        await self._pub(WEEK_ATTRS, {k: v for k, v in week.items() if k != "total"})
        night = self.stats.last_night()
        if night is not None:
            await self._pub(NIGHT_STATE, str(night["total"]))
            await self._pub(NIGHT_ATTRS, {k: v for k, v in night.items() if k != "total"})
        for rule in self.stats.rules:
            summary = self.stats.summary(rule)
            state_topic, attrs_topic = summary_topics(rule)
            await self._pub(state_topic, str(summary["total"]))
            await self._pub(attrs_topic, {k: v for k, v in summary.items() if k != "total"})

    async def event(self, note: dict) -> None:
        """Only the event entity (internal notifications such as the health check)."""
        await self._pub(EVENT, {"event_type": note.get("action", "add"),
                                **{k: v for k, v in note.items() if k != "action"}}, retain=False)

    async def notification(self, note: dict, state: str, attrs: dict) -> None:
        action = note.get("action", "add")
        if self.stats is not None and self.stats.record(note):
            await self.publish_stats()
        if action == "add":
            bundle_id = note.get("bundle_id") or note.get("app") or "unknown"
            new_app = self.counts.increment(bundle_id, note.get("app") or bundle_id)
            await self._publish_app(bundle_id, self.counts.apps[bundle_id]["name"], discovery=new_app)
            await self._pub(f"{PREFIX}/count/total", str(self.counts.total))
        self.last_state, self.last_attrs = state, attrs
        await self._pub(LAST_STATE, state)
        await self._pub(LAST_ATTRS, attrs)
        await self._pub(EVENT, {"event_type": action, **{k: v for k, v in note.items() if k != "action"}},
                        retain=False)

    # -- connection -------------------------------------------------------

    async def _periodic(self) -> None:
        """Every 10 minutes (on the clock): roll over the day at midnight and
        the night summary when the night window starts or ends."""
        while True:
            now = datetime.now()
            await asyncio.sleep(600 - (now.minute % 10) * 60 - now.second + 5)
            try:
                await self.publish_stats()
            except aiomqtt.MqttError as e:
                log.warning("Could not publish the statistics: %s", e)

    async def run(self) -> None:
        if self.stats is not None:
            asyncio.get_running_loop().create_task(self._periodic())
        delay = 1
        will = aiomqtt.Will(AVAILABILITY, "offline", qos=1, retain=True)
        while True:
            try:
                async with aiomqtt.Client(self.host, self.port, username=self.username,
                                          password=self.password, identifier=f"{PREFIX}_bridge",
                                          will=will) as client:
                    self.client = client
                    await client.subscribe(f"{DISCOVERY}/status")
                    await self.publish_all()
                    log.info("MQTT connected to %s:%s; device 'iPhone Notification Forward' published",
                             self.host, self.port)
                    delay = 1
                    async for message in client.messages:
                        if message.payload in (b"online", "online"):
                            log.info("Home Assistant restarted; republishing MQTT discovery")
                            await self.publish_all()
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as e:
                log.warning("MQTT connection lost: %s (retrying in %ss)", e, delay)
            finally:
                self.client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
