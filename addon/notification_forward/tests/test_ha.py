import asyncio

from app.ha import NotificationSink


class FakeHA:
    def __init__(self):
        self.events, self.states = [], []

    async def fire_event(self, event_type, data):
        self.events.append((event_type, data))

    async def set_state(self, entity_id, state, attributes):
        self.states.append((entity_id, state, attributes))


def test_sink_events_sensor_and_active_tracking():
    ha = FakeHA()
    sink = NotificationSink(ha, "iphone_notification", "sensor.iphone_last_notification")

    async def go():
        await sink.handle({"action": "add", "id": "1", "app": "WhatsApp", "title": "Mom", "body": "Hi"})
        await sink.handle({"action": "add", "id": "2", "app": "Mail", "title": None, "body": "Invoice"})
        await sink.handle({"action": "remove", "id": "1"})

    asyncio.run(go())
    assert [e[0] for e in ha.events] == ["iphone_notification"] * 3
    entity, state, attrs = ha.states[-1]
    assert entity == "sensor.iphone_last_notification"
    assert state == "Mail: Invoice"
    assert attrs["active_count"] == 1
    assert attrs["active"] == [{"app": "Mail", "title": None, "body": "Invoice"}]

    asyncio.run(sink.handle({"action": "remove_all"}))
    assert ha.states[-1][2]["active_count"] == 0
