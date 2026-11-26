"""报名开始提醒：独立于新活动去重，时间均按北京时间解释。"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8))


def signup_timestamp(activity: dict) -> float | None:
    for key in ("joinstartdate", "joinStartDate", "joinStartTime", "joinstarttime", "joinStart"):
        value = activity.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            stamp = float(value)
            if stamp >= 100_000_000_000:
                stamp /= 1000
            if math.isfinite(stamp) and stamp > 0:
                datetime.fromtimestamp(stamp, CHINA_TZ)
                return stamp
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.strip().replace("/", "-").replace("Z", "+00:00"))
                return dt.replace(tzinfo=CHINA_TZ).timestamp() if dt.tzinfo is None else dt.timestamp()
            except ValueError:
                pass
    return None


def reminder_options(config: dict) -> tuple[bool, int, set[str]]:
    cfg = config.get("signup_reminder") or {}
    try:
        minutes = max(1, int(cfg.get("minutes_before", 10)))
    except (ValueError, TypeError):
        minutes = 10
    levels = cfg.get("priorities", ["高性价比"])
    if isinstance(levels, str):
        levels = levels.replace("，", ",").split(",")
    aliases = {"high": "高性价比", "medium": "中", "low": "低性价比", "中等": "中"}
    levels = {aliases.get(str(v).strip(), str(v).strip()) for v in (levels or [])}
    return bool(cfg.get("enabled", True)), minutes, levels


class ReminderStore:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.records = json.load(f)
            if not isinstance(self.records, dict):
                self.records = {}
        except (FileNotFoundError, json.JSONDecodeError):
            self.records = {}

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.records, f, ensure_ascii=False)
        os.replace(self.path + ".tmp", self.path)

    def update(self, activities: list, results: list):
        by_id = {str(r["id"]): r for r in results}
        for activity in activities:
            aid = activity.get("activityId")
            if aid is None:
                continue
            key = str(aid)
            old = self.records.get(key, {})
            start = signup_timestamp(activity)
            state = (activity.get("eligibility") or {}).get("state")
            old_state = (old.get("activity", {}).get("eligibility") or {}).get("state")
            transition = old.get("transition")
            if state != old_state:
                transition = ({"from": old_state, "to": state, "sent": False}
                              if old_state in ("BLOCKED", "JOINABLE_TRIBE", "FULL", "SOON")
                              and state in ("SOON", "ELIGIBLE") else None)
            self.records[key] = {
                "activity": activity,
                "result": by_id.get(key, old.get("result")),
                "start": start,
                "sent": old.get("sent", False) if old.get("start") == start else False,
                "transition": transition,
                "pause_started": old.get("pause_started", False) if old.get("start") == start else False,
                "listed": True,
            }
        self.save()

    def due(self, config: dict, now: float) -> list[tuple[str, dict]]:
        enabled, minutes, levels = reminder_options(config)
        if not enabled:
            return []
        due = []
        for aid, record in self.records.items():
            start, result = record.get("start"), record.get("result")
            if record.get("sent") or start is None or not result:
                continue
            if (record.get("activity", {}).get("eligibility") or {}).get("state") not in ("SOON", "ELIGIBLE"):
                continue
            score = result.get("score") or 0
            level = "高性价比" if score >= 70 else "中" if score >= 40 else "低性价比"
            if level in levels and start - minutes * 60 <= now < start:
                due.append((aid, record))
        return sorted(due, key=lambda item: (-(item[1]["result"].get("score") or 0), item[1]["start"]))

    def mark_sent(self, records: list[tuple[str, dict]]):
        for aid, record in records:
            # 发送期间若报名时间更新，保留新一轮提醒。
            current = self.records.get(aid)
            if current and current.get("start") == record.get("start"):
                current["sent"] = True
        self.save()
