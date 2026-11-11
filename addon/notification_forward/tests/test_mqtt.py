import asyncio
import json

from app import mqtt
from app.ha import NotificationSink


class FakeClient:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload) if payload.startswith("{") else payload, retain))


class FakeHA:
    def __init__(self):
        self.events, self.states = [], []

    async def fire_event(self, event_type, data):
        self.events.append((event_type, data))

    async def set_state(self, *a):
        self.states.append(a)


def make(tmp_path):
    pub = mqtt.MqttPublisher("h", 1883, None, None, "0.2.0", "sensor.iphone_last_notification",
                             mqtt.CountStore(str(tmp_path / "counts.json")))
    pub.client = FakeClient()
    return pub


def topics(pub):
    return [t for t, _, _ in pub.client.published]


def test_discovery_puts_everything_on_one_device(tmp_path):
    pub = make(tmp_path)
    asyncio.run(pub.publish_all())
    configs = {t: p for t, p, _ in pub.client.published if t.endswith("/config")}
    assert set(configs) == {
        "homeassistant/sensor/notification_forward/last/config",
        "homeassistant/event/notification_forward/notification/config",
        "homeassistant/sensor/notification_forward/count_total/config",
    }
    assert {c["device"]["identifiers"][0] for c in configs.values()} == {"notification_forward_bridge"}
    last = configs["homeassistant/sensor/notification_forward/last/config"]
    assert last["default_entity_id"] == "sensor.iphone_last_notification"
    assert configs["homeassistant/event/notification_forward/notification/config"]["event_types"] == \
        ["add", "update", "remove", "remove_all"]
    assert ("notification_forward/status", "online", True) in pub.client.published


def test_counts_per_app_and_new_app_discovery(tmp_path):
    pub = make(tmp_path)
    ha = FakeHA()
    sink = NotificationSink(ha, "iphone_notification", "sensor.iphone_last_notification", pub)
    outlook = {"action": "add", "id": "1", "bundle_id": "com.microsoft.Office.Outlook", "app": "Outlook",
               "title": "Netflix", "subtitle": "Offer", "body": "Hi"}

    async def go():
        await sink.handle(outlook)
        await sink.handle({**outlook, "id": "2"})
        await sink.handle({**outlook, "action": "update"})          # not counted
        await sink.handle({"action": "remove", "id": "1", "bundle_id": "com.microsoft.Office.Outlook"})
        await sink.handle({"action": "add", "id": "9", "bundle_id": "net.whatsapp.WhatsApp", "app": "WhatsApp"})

    asyncio.run(go())
    assert pub.counts.total == 3
    assert pub.counts.apps["com.microsoft.Office.Outlook"]["count"] == 2
    assert pub.counts.apps["net.whatsapp.WhatsApp"]["count"] == 1
    # discovery for each app exactly once, with a readable entity id
    app_cfgs = [p for t, p, _ in pub.client.published if "/count_com_" in t or "/count_net_" in t]
    assert [c["default_entity_id"] for c in app_cfgs] == \
        ["sensor.iphone_notifications_outlook", "sensor.iphone_notifications_whatsapp"]
    assert app_cfgs[0]["state_class"] == "total_increasing"
    # event entity gets every action; the HA bus event is still fired; no REST sensor
    events = [p["event_type"] for t, p, r in pub.client.published if t == mqtt.EVENT]
    assert events == ["add", "add", "update", "remove", "add"]
    assert len(ha.events) == 5 and ha.states == []
    # counts survive a restart
    again = mqtt.CountStore(str(tmp_path / "counts.json"))
    assert again.total == 3 and again.apps["com.microsoft.Office.Outlook"]["name"] == "Outlook"


def test_last_sensor_state_and_attributes(tmp_path):
    pub = make(tmp_path)
    sink = NotificationSink(FakeHA(), "iphone_notification", "sensor.iphone_last_notification", pub)
    asyncio.run(sink.handle({"action": "add", "id": "1", "bundle_id": "b", "app": "Outlook",
                             "title": "Netflix", "subtitle": "Offer", "transport": "internet"}))
    state = [p for t, p, _ in pub.client.published if t == mqtt.LAST_STATE][-1]
    attrs = [p for t, p, _ in pub.client.published if t == mqtt.LAST_ATTRS][-1]
    assert state == "Outlook: Netflix"
    assert attrs["subtitle"] == "Offer" and attrs["active_count"] == 1 and "transport" not in attrs


def test_without_mqtt_falls_back_to_rest_sensor():
    ha = FakeHA()
    sink = NotificationSink(ha, "iphone_notification", "sensor.iphone_last_notification")
    asyncio.run(sink.handle({"action": "add", "app": "A", "title": "T"}))
    assert ha.states[0][0] == "sensor.iphone_last_notification" and ha.states[0][1] == "A: T"
