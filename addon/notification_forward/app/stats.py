"""Rolling 7-day notification statistics, persisted in /data.

Two kinds of numbers, both kept per local day so the oldest day simply drops
off:

* New notifications per app (top-N "noisiest apps").
* Alert summaries: notifications matching a configured rule (for example
  Zabbix mails in Outlook) grouped by the rule's named regex groups (host,
  problem, ...), plus the most recent matches.

Only `add` actions count, each notification once (iOS can deliver the same
notification more than once).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

log = logging.getLogger(__name__)

DAYS = 7
SEEN_MAX = 1000
RECENT_MAX = 10
INVISIBLE = re.compile(r"[‎‏‪-‮⁦-⁩]")  # RTL/LTR marks (e.g. "‎WhatsApp")


def clean(text: str | None) -> str:
    return INVISIBLE.sub("", text or "").strip()


@dataclass
class SummaryRule:
    """Which notifications count for a summary, and how to group them.

    app / sender: case-insensitive substrings of the app name / title (sender).
    pattern: regex on the subtitle (mail subject) or, if there is none, the
             title; its named groups become the grouping keys.
    exclude: regex on subject + body; matching notifications are skipped.
    """

    name: str
    pattern: str
    app: str = ""
    sender: str = ""
    exclude: str = ""

    def __post_init__(self):
        self._pattern = re.compile(self.pattern)
        self._exclude = re.compile(self.exclude, re.IGNORECASE) if self.exclude else None

    @property
    def groups(self) -> list[str]:
        return list(self._pattern.groupindex)

    def match(self, note: dict) -> dict | None:
        if self.app and self.app.lower() not in clean(note.get("app")).lower():
            return None
        if self.sender and self.sender.lower() not in clean(note.get("title")).lower():
            return None
        subject = clean(note.get("subtitle") or note.get("title"))
        if self._exclude and self._exclude.search(f"{subject} {clean(note.get('body'))}"):
            return None
        m = self._pattern.search(subject)
        if not m:
            return None
        return {"subject": subject, **{k: (v or "").strip() for k, v in m.groupdict().items()}}


class WeeklyStats:
    def __init__(self, path: str, rules: list[SummaryRule], tz: tzinfo | None = None):
        self.path = path
        self.rules = rules
        self.tz = tz
        # days: {"YYYY-MM-DD": {app: count}}
        # summaries: {rule: {"days": {day: {"total": n, "<group>": {value: n}}}, "recent": [...]}}
        self.days: dict[str, dict[str, int]] = {}
        self.summaries: dict[str, dict] = {}
        self.seen: list[str] = []
        self.loaded = False
        self.history_imported = False
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            self.days = data.get("days", {})
            self.summaries = data.get("summaries", {})
            self.seen = data.get("seen", [])
            # 0.3.0/0.3.1 saved "history_imported" after reading nothing; import again.
            self.history_imported = data.get("history_imported_v2", False)
            self.loaded = True
        self._seen_set = set(self.seen)

    # -- recording --------------------------------------------------------

    def _local(self, when: datetime | None) -> datetime:
        when = when or datetime.now().astimezone()
        return when.astimezone(self.tz) if self.tz else when.astimezone()

    def record(self, note: dict, when: datetime | None = None, save: bool = True) -> bool:
        """Count one notification; returns False when it was not counted."""
        if note.get("action", "add") != "add":
            return False
        key = f'{note.get("bundle_id") or ""}/{note.get("id") or ""}'
        if note.get("id") and key in self._seen_set:
            return False
        if note.get("id"):
            self.seen.append(key)
            self._seen_set.add(key)
            if len(self.seen) > SEEN_MAX:
                self._seen_set.discard(self.seen.pop(0))

        local = self._local(when)
        day = local.date().isoformat()
        app = clean(note.get("app")) or note.get("bundle_id") or "unknown"
        apps = self.days.setdefault(day, {})
        apps[app] = apps.get(app, 0) + 1

        for rule in self.rules:
            hit = rule.match(note)
            if hit is None:
                continue
            summary = self.summaries.setdefault(rule.name, {"days": {}, "recent": []})
            bucket = summary["days"].setdefault(day, {"total": 0})
            bucket["total"] += 1
            for group in rule.groups:
                values = bucket.setdefault(group, {})
                values[hit[group]] = values.get(hit[group], 0) + 1
            # Newest first; history imports can arrive older than what is already there.
            summary["recent"] = sorted([{"time": local.isoformat(timespec="seconds"), **hit}, *summary["recent"]],
                                       key=lambda r: r["time"], reverse=True)[:RECENT_MAX]

        self.prune(local)
        if save:
            self.save()
        return True

    def prune(self, now: datetime | None = None) -> None:
        cutoff = (self._local(now).date() - timedelta(days=DAYS - 1)).isoformat()
        self.days = {d: v for d, v in self.days.items() if d >= cutoff}
        for summary in self.summaries.values():
            summary["days"] = {d: v for d, v in summary["days"].items() if d >= cutoff}
            summary["recent"] = [r for r in summary["recent"] if r["time"][:10] >= cutoff]

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".stats")
        with os.fdopen(fd, "w") as f:
            json.dump({"days": self.days, "summaries": self.summaries, "seen": self.seen,
                       "history_imported_v2": self.history_imported}, f)
        os.replace(tmp, self.path)
        self.loaded = True

    # -- reading ----------------------------------------------------------

    def today(self, now: datetime | None = None) -> int:
        return sum(self.days.get(self._local(now).date().isoformat(), {}).values())

    def week(self) -> dict:
        apps: Counter = Counter()
        for counts in self.days.values():
            apps.update(counts)
        per_day = {d: sum(v.values()) for d, v in sorted(self.days.items())}
        return {
            "total": sum(apps.values()),
            "top_apps": [{"app": a, "count": n} for a, n in apps.most_common(20)],
            "per_day": per_day,
        }

    def summary(self, rule: SummaryRule) -> dict:
        data = self.summaries.get(rule.name, {"days": {}, "recent": []})
        groups: dict[str, Counter] = {g: Counter() for g in rule.groups}
        total = 0
        for bucket in data["days"].values():
            total += bucket.get("total", 0)
            for g in rule.groups:
                groups[g].update(bucket.get(g, {}))
        result = {
            "total": total,
            "per_day": {d: b.get("total", 0) for d, b in sorted(data["days"].items())},
            "recent": data["recent"],
        }
        for g, counter in groups.items():
            result[f"top_{g}"] = [{g: v, "count": n} for v, n in counter.most_common(15)]
        return result


def parse_rules(raw: list[dict]) -> list[SummaryRule]:
    rules = []
    for item in raw or []:
        try:
            rules.append(SummaryRule(name=item["name"], pattern=item["pattern"], app=item.get("app") or "",
                                     sender=item.get("sender") or "", exclude=item.get("exclude") or ""))
        except (KeyError, re.error) as e:
            log.warning("Ignoring summary %r: %s", item.get("name"), e)
    return rules
