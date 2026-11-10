import ast
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

from reminders import CHINA_TZ, ReminderStore, reminder_options, signup_timestamp


class ReminderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "reminders.json")
        self.store = ReminderStore(self.path)
        self.start = signup_timestamp({"joinStartTime": "2026-11-11 12:00:00"})
        self.acts = [{"activityId": n, "name": f"活动{n}", "joinStartTime": self.start}
                     for n in range(3)]
        for activity in self.acts:
            activity["eligibility"] = {"state": "SOON"}
        self.results = [{"id": n, "score": score} for n, score in enumerate((80, 50, 20))]
        self.store.update(self.acts, self.results)

    def test_formats(self):
        self.assertEqual(signup_timestamp({"joinstartdate": self.start * 1000}), self.start)
        self.assertEqual(signup_timestamp({"joinstartdate": 1794441540000}), 1794441540)
        for value in (self.start, str(self.start), self.start * 1000,
                      "2026/11/11 12:00:00", "2026-11-11T04:00:00Z"):
            self.assertEqual(signup_timestamp({"joinStartTime": value}), self.start)
        for value in (None, "待公布", "", 0, -1, True, float("inf"), float("nan")):
            self.assertIsNone(signup_timestamp({"joinStartTime": value}))
        self.assertIsNone(signup_timestamp({"activitytime": "2026-11-11 12:00:00"}))

    def test_boundaries(self):
        for offset, count in ((-601, 0), (-600, 1), (-1, 1), (0, 0), (1, 0)):
            self.assertEqual(len(self.store.due({}, self.start + offset)), count)

    def test_filter_order_and_config(self):
        cfg = {"signup_reminder": {"priorities": ["低性价比", "中", "高性价比"]}}
        self.assertEqual([aid for aid, _ in self.store.due(cfg, self.start - 500)], ["0", "1", "2"])
        for opts in ({"enabled": False}, {"priorities": []}):
            self.assertEqual(self.store.due({"signup_reminder": opts}, self.start - 5), [])
        self.assertEqual(reminder_options({"signup_reminder": {"minutes_before": "bad"}})[1], 10)
        self.assertEqual(reminder_options({"signup_reminder": {"minutes_before": 0}})[1], 1)
        cfg = {"signup_reminder": {"minutes_before": 20, "priorities": ["中"]}}
        self.assertEqual([aid for aid, _ in self.store.due(cfg, self.start - 1200)], ["1"])

    def test_restart_and_reschedule(self):
        self.store.mark_sent(self.store.due({}, self.start - 500))
        restored = ReminderStore(self.path)
        restored.update(self.acts, self.results)
        self.assertEqual(restored.due({}, self.start - 500), [])
        self.acts[0]["joinStartTime"] += 100
        restored.update(self.acts, self.results)
        self.assertEqual(len(restored.due({}, self.start - 400)), 1)

    def test_refresh_during_send(self):
        due = self.store.due({}, self.start - 500)
        self.store.update(self.acts, self.results)
        self.store.mark_sent(due)
        self.assertEqual(self.store.due({}, self.start - 500), [])

    def test_reschedule_during_send(self):
        due = self.store.due({}, self.start - 500)
        self.acts[0]["joinStartTime"] += 100
        self.store.update(self.acts, self.results)
        self.store.mark_sent(due)
        self.assertEqual(len(self.store.due({}, self.start - 400)), 1)

    def test_missing_time(self):
        self.acts[0].pop("joinStartTime")
        self.store.update(self.acts, self.results)
        self.assertEqual(self.store.due({}, self.start - 500), [])

    def test_dispatch_retry_and_dedup(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DmkjFinderPlugin")
        method = next(n for n in cls.body if getattr(n, "name", "") == "_check_signup_reminders")
        ns = {"datetime": datetime, "CHINA_TZ": CHINA_TZ,
              "time": types.SimpleNamespace(time=lambda: self.start - 500),
              "logger": types.SimpleNamespace(exception=lambda *a: None)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), ns)
        plugin = types.SimpleNamespace(_reminder_busy=False, reminders=self.store, config={},
                                       _fmt=lambda a, r: a["name"], _send=AsyncMock(return_value=False),
                                       _pause_polling_for_reminders=lambda due: None)
        check = ns[method.name]
        asyncio.run(check(plugin))
        self.assertEqual(len(self.store.due({}, self.start - 500)), 1)
        plugin._send.return_value = True
        asyncio.run(check(plugin))
        asyncio.run(check(plugin))
        self.assertEqual(plugin._send.await_count, 2)
        self.assertIn("2026-11-11 12:00:00", plugin._send.call_args.args[0])
        self.assertFalse(plugin._reminder_busy)

    def test_seen_activities_register_without_new_activity_push(self):
        from seen_store import SeenStore

        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DmkjFinderPlugin")
        method = next(n for n in cls.body if getattr(n, "name", "") == "_tick")
        from analyzer import judge_rule
        ns = {"asyncio": asyncio, "time": __import__("time"), "judge_rule": judge_rule,
              "logger": types.SimpleNamespace(exception=lambda *a: None, warning=lambda *a: None)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), ns)
        seen = SeenStore(str(Path(self.path).with_name("seen.json")))
        for a in self.acts:
            seen.add(a["activityId"])
        plugin = types.SimpleNamespace(
            _poll_busy=False, _poll_sync=lambda _: (self.acts, None), seen=seen,
            reminders=ReminderStore(str(Path(self.path).with_name("bootstrap.json"))), config={},
            _process_activities=AsyncMock(), _check_signup_reminders=AsyncMock(),
            _check_eligibility_changes=AsyncMock(),
            _is_polling_paused=lambda: False,
            _save_list_membership=lambda: None,
        )
        asyncio.run(ns[method.name](plugin))
        plugin._process_activities.assert_not_awaited()
        self.assertEqual(plugin._check_signup_reminders.await_count, 2)
        self.assertEqual(len(plugin.reminders.records), 3)
        self.assertEqual(plugin.reminders.records["0"]["start"], self.start)
        self.assertIsNotNone(plugin.reminders.records["0"]["result"])
        self.assertFalse(plugin._poll_busy)


if __name__ == "__main__":
    unittest.main()
