import asyncio
from datetime import datetime, timedelta, timezone

from app import mqtt
from app.ha import NotificationSink
from app.stats import SummaryRule, WeeklyStats, parse_rules

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
ZABBIX = SummaryRule(name="Zabbix", app="Outlook", sender="zabbix",
                     pattern=r"^\[(?P<host>[^\]]+)\] Problem: (?P<problem>.+)$",
                     exclude="Escalation canceled")


def mail(nid, subject, body="", sender="zabbix@example.com", action="add"):
    return {"action": action, "id": nid, "bundle_id": "com.microsoft.Office.Outlook", "app": "Outlook",
            "title": sender, "subtitle": subject, "body": body}


def test_counts_apps_per_day_and_ranks_top_apps(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [])
    for i in range(3):
        s.record({"action": "add", "id": f"w{i}", "bundle_id": "net.whatsapp.WhatsApp",
                  "app": "‎WhatsApp"}, NOW)
    s.record({"action": "add", "id": "o1", "app": "Outlook"}, NOW - timedelta(days=1))
    week = s.week()
    assert week["total"] == 4
    assert week["top_apps"][0] == {"app": "WhatsApp", "count": 3}  # invisible mark stripped
    assert week["per_day"] == {"2026-11-11": 1, "2026-11-12": 3}
    assert s.today(NOW) == 3


def test_only_new_notifications_count_once(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [])
    note = {"action": "add", "id": "1", "bundle_id": "b", "app": "A"}
    assert s.record(note, NOW) is True
    assert s.record(note, NOW) is False                          # same notification delivered again
    assert s.record({**note, "action": "update"}, NOW) is False
    assert s.record({"action": "remove", "id": "1", "bundle_id": "b"}, NOW) is False
    assert s.week()["total"] == 1


def test_days_older_than_a_week_drop_off(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX])
    s.record(mail("old", "[H1] Problem: Disk full"), NOW - timedelta(days=7))
    s.record(mail("new", "[H2] Problem: Disk full"), NOW)
    s.prune(NOW)
    assert s.week()["total"] == 1
    assert s.summary(ZABBIX)["top_host"] == [{"host": "H2", "count": 1}]


def test_zabbix_summary_groups_problems_and_hosts(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX])
    s.record(mail("1", "[ATIME-P-PLS-039] Problem: Windows: FS [C:]: Disk is filling up"), NOW)
    s.record(mail("2", "[ATIME-P-PLS-038] Problem: Windows: FS [C:]: Disk is filling up"), NOW)
    s.record(mail("3", "[SQL-01] Problem: High CPU"), NOW)
    s.record(mail("4", "[SQL-01] Problem: High CPU", body='NOTE: Escalation canceled: host "SQL-01" disabled.'),
             NOW)                                                 # excluded
    s.record(mail("5", "[SQL-01] Resolved in 5m: High CPU"), NOW)  # not a problem
    s.record(mail("6", "[X] Problem: Y", sender="someone@example.com"), NOW)  # other sender
    summary = s.summary(ZABBIX)
    assert summary["total"] == 3
    assert summary["top_problem"][0] == {"problem": "Windows: FS [C:]: Disk is filling up", "count": 2}
    assert {"host": "SQL-01", "count": 1} in summary["top_host"]
    assert summary["recent"][0]["host"] == "SQL-01" and summary["recent"][0]["time"].startswith("2026-11-12")
    assert summary["per_day"] == {"2026-11-12": 3}


def test_statistics_survive_a_restart(tmp_path):
    path = str(tmp_path / "stats.json")
    s = WeeklyStats(path, [ZABBIX])
    s.record(mail("1", "[H] Problem: P"), NOW)
    again = WeeklyStats(path, [ZABBIX])
    assert again.loaded and again.week()["total"] == 1 and again.summary(ZABBIX)["total"] == 1
    assert again.record(mail("1", "[H] Problem: P"), NOW) is False   # still remembered as seen


def test_bad_rules_are_skipped():
    rules = parse_rules([{"name": "ok", "pattern": "x"}, {"name": "bad", "pattern": "("}, {"pattern": "x"}])
    assert [r.name for r in rules] == ["ok"]


class FakeClient:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0, retain=False):
        import json
        self.published.append((topic, json.loads(payload) if payload.startswith(("{", "[")) else payload))


class FakeHA:
    def __init__(self):
        self.events = []

    async def fire_event(self, event_type, data):
        self.events.append(data)

    async def set_state(self, *a):
        raise AssertionError("no REST sensor with MQTT")


def make(tmp_path):
    pub = mqtt.MqttPublisher("h", 1883, None, None, "0.3.0", "sensor.iphone_last_notification",
                             mqtt.CountStore(str(tmp_path / "counts.json")),
                             WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX]))
    pub.client = FakeClient()
    return pub


def test_statistics_entities_are_discovered_and_published(tmp_path):
    pub = make(tmp_path)
    asyncio.run(pub.publish_all())
    configs = {t: p for t, p in pub.client.published if t.endswith("/config")}
    ids = {c["default_entity_id"] for c in configs.values()}
    assert {"sensor.iphone_notifications_today", "sensor.iphone_notifications_7_days",
            "sensor.iphone_zabbix_alerts_7_days"} <= ids

    sink = NotificationSink(FakeHA(), "iphone_notification", "sensor.iphone_last_notification", pub)
    asyncio.run(sink.handle(mail("1", "[H] Problem: Disk full")))
    published = dict(pub.client.published)
    assert published[mqtt.WEEK_STATE] == "1"
    assert published[mqtt.WEEK_ATTRS]["top_apps"] == [{"app": "Outlook", "count": 1}]
    state_topic, attrs_topic = mqtt.summary_topics(ZABBIX)
    assert published[state_topic] == "1"
    assert published[attrs_topic]["top_problem"] == [{"problem": "Disk full", "count": 1}]


def test_health_check_push_is_only_an_event(tmp_path):
    pub = make(tmp_path)
    ha = FakeHA()
    sink = NotificationSink(ha, "iphone_notification", "sensor.iphone_last_notification", pub)
    check = {"action": "add", "id": "nf-healthcheck", "bundle_id": "io.robbie.HomeAssistant",
             "app": "Home Assistant", "title": "Notification Forward check", "thread": "nf-healthcheck"}
    asyncio.run(sink.handle(check))
    assert ha.events == [check]
    topics = [t for t, _ in pub.client.published]
    assert topics == [mqtt.EVENT]                      # no counters, no last-notification update
    assert pub.counts.total == 0 and pub.stats.week()["total"] == 0


def test_history_import_flag_and_recent_order(tmp_path):
    path = str(tmp_path / "stats.json")
    s = WeeklyStats(path, [ZABBIX])
    s.record(mail("new", "[NEW] Problem: P"), NOW)
    s.record(mail("old", "[OLD] Problem: P"), NOW - timedelta(days=2))   # imported later from history
    assert [r["host"] for r in s.summary(ZABBIX)["recent"]] == ["NEW", "OLD"]
    assert WeeklyStats(path, [ZABBIX]).history_imported is False
    s.history_imported = True
    s.save()
    assert WeeklyStats(path, [ZABBIX]).history_imported is True


def test_history_request_encodes_the_timestamp():
    from app.ha import HomeAssistant

    class Resp:
        status = 200

        async def json(self):
            return [[{"state": "x"}]]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url, params=None, headers=None):
            self.url, self.params = url, params
            return Resp()

    session = Session()
    states = asyncio.run(HomeAssistant(session, "t").history("sensor.x", "2026-11-05T07:00:00+00:00"))
    assert states == [{"state": "x"}]
    assert session.url.endswith("/history/period/2026-11-05T07%3A00%3A00%2B00%3A00")
    assert session.params["end_time"].endswith("Z")    # else Home Assistant stops after one day


# -- night summary ---------------------------------------------------------

from app.stats import NightWindow  # noqa: E402


def at(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)


def test_night_window_across_midnight():
    night = NightWindow("23:00-06:30")
    assert night.contains(at(12, 23, 5)) and night.contains(at(13, 2)) and night.contains(at(13, 6, 29))
    assert not night.contains(at(13, 6, 30)) and not night.contains(at(13, 14))
    # during the night: the running one; during the day: the one that ended this morning
    assert night.latest(at(13, 2)) == (at(12, 23), at(13, 6, 30))
    assert night.latest(at(13, 14)) == (at(12, 23), at(13, 6, 30))
    assert night.latest(at(13, 23, 30)) == (at(13, 23), at(14, 6, 30))


def test_last_night_lists_notifications_and_alerts(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX], TZ, NightWindow("23:00-06:30"))
    s.record(mail("day", "[DAY] Problem: P"), at(12, 15))                     # daytime: not in the night
    s.record(mail("z1", "[SRV-1] Problem: Disk full"), at(12, 23, 40))
    s.record({"action": "add", "id": "w", "app": "WhatsApp", "title": "Mom"}, at(13, 2, 10))
    s.record(mail("z2", "[SRV-2] Resolved in 5m: Disk full"), at(13, 3))      # night, but no alert
    night = s.last_night(at(13, 8))
    assert night["total"] == 3 and night["ongoing"] is False
    assert night["from"].startswith("2026-11-12T23:00") and night["to"].startswith("2026-11-13T06:30")
    assert night["top_apps"][0] == {"app": "Outlook", "count": 2}
    assert [(a["summary"], a["host"]) for a in night["alerts"]] == [("Zabbix", "SRV-1")]
    assert [i["app"] for i in night["items"]] == ["Outlook", "WhatsApp", "Outlook"]   # newest first
    assert WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX], TZ,
                       NightWindow("23:00-06:30")).last_night(at(13, 8))["total"] == 3   # persisted


def test_no_night_summary_without_a_window(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [])
    s.record({"action": "add", "id": "1", "app": "A"}, at(13, 2))
    assert s.last_night() is None and s.night_items == []


def test_night_sensor_is_published(tmp_path):
    pub = mqtt.MqttPublisher("h", 1883, None, None, "0.4.0", "sensor.iphone_last_notification",
                             mqtt.CountStore(str(tmp_path / "counts.json")),
                             WeeklyStats(str(tmp_path / "stats.json"), [ZABBIX], TZ, NightWindow("00:00-23:59")))
    pub.client = FakeClient()
    asyncio.run(pub.publish_all())
    configs = {p["default_entity_id"] for t, p in pub.client.published if t.endswith("/config")}
    assert "sensor.iphone_notifications_last_night" in configs
    assert dict(pub.client.published)[mqtt.NIGHT_ATTRS]["window"] == "00:00-23:59"


# -- heartbeat ---------------------------------------------------------------

def test_heartbeat_pings_while_home_assistant_answers_and_fails_otherwise(monkeypatch):
    from app import main

    answers = [True, False]
    hits = []

    class HA:
        async def ping(self):
            return answers.pop(0)

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url, timeout=None):
            hits.append(url)
            return Resp()

    async def fake_sleep(_):
        if not answers:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(main.heartbeat(Session(), HA(), "https://hc-ping.com/abc"))
    except asyncio.CancelledError:
        pass
    assert hits == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]


def test_latest_keeps_the_ten_newest_whatever_the_time(tmp_path):
    path = str(tmp_path / "stats.json")
    s = WeeklyStats(path, [], TZ, NightWindow("23:00-06:30"))
    for i in range(12):
        s.record({"action": "add", "id": str(i), "app": "‎WhatsApp", "title": f"T{i}"}, at(13, 7, i))
    s.record({"action": "add", "id": "old", "app": "Old"}, at(12, 7))          # older, arrives late
    latest = s.week()["latest"]
    assert len(latest) == 10
    assert [i["title"] for i in latest[:2]] == ["T11", "T10"] and latest[0]["app"] == "WhatsApp"
    assert WeeklyStats(path, []).week()["latest"] == latest                       # persisted


def test_latest_and_night_items_keep_the_start_of_the_text(tmp_path):
    s = WeeklyStats(str(tmp_path / "stats.json"), [], TZ, NightWindow("23:00-06:30"))
    s.record({"action": "add", "id": "1", "app": "WhatsApp", "title": "Sigrid", "body": "x" * 300}, at(13, 2))
    latest = s.week()["latest"][0]
    assert latest["subject"] == "" and latest["text"] == "x" * 120
    assert s.last_night(at(13, 8))["items"][0]["text"] == "x" * 120
