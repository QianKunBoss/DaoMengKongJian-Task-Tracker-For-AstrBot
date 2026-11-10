import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock

from eligibility import college_blocked, eligibility_key, eligibility_text, judge_eligibility
from reminders import ReminderStore
from seen_store import SeenStore
from dmkj_client import DmkjClient, SessionExpiredError
from plugin_harness import load_plugin


class EligibilityTests(unittest.TestCase):
    def state(self, detail, college=10):
        return judge_eligibility(detail, college, now=1000)["state"]

    def test_eight_states(self):
        cases = [
            ({"injoin": "true"}, "JOINED"),
            ({"ableJoinFlag": 1, "canJoin": True}, "ELIGIBLE"),
            ({"ableJoinFlag": "1", "countdown": 30000}, "SOON"),
            ({"countdownText": "不在活动规定的部落内", "joinrange": 1, "collegeid": 10}, "JOINABLE_TRIBE"),
            ({"ableJoinFlag": 1, "joinNum": "5", "joinmaxnum": "5"}, "FULL"),
            ({"countdownText": "不符合报名条件"}, "BLOCKED"),
            ({"countdownText": "不在活动规定的院系内", "collegeid": 20}, "BLOCKED_PERM"),
            ({}, "UNKNOWN"),
        ]
        for detail, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.state(detail), expected)

    def test_other_college_tribe_is_never_joinable(self):
        detail = {"countdownText": "不在活动规定的部落内", "joinrange": 1,
                  "collegeid": 20, "collegename": "其他学院"}
        info = judge_eligibility(detail, 10)
        self.assertEqual(info["state"], "BLOCKED_PERM")
        self.assertNotIn("需先加入", info["reason"])
        self.assertTrue(college_blocked({"eligibility": info}))

    def test_unknown_college_does_not_promise_unlock(self):
        detail = {"countdownText": "不在活动规定的部落内", "joinrange": 1, "collegeid": 20}
        self.assertEqual(self.state(detail, None), "BLOCKED")
        detail["collegeid"] = -1
        detail["tribeVo"] = {"name": "学生会", "id": 123}
        info = judge_eligibility(detail, None)
        self.assertEqual(info["state"], "JOINABLE_TRIBE")
        self.assertIn("学生会", info["reason"])
        self.assertEqual(info["tribe_id"], 123)

    def test_fallback_and_string_flags(self):
        self.assertEqual(self.state({"ableJoinFlag": 0, "collegeid": 20, "joinrange": 2}), "BLOCKED_PERM")
        self.assertEqual(self.state({"injoin": "false", "ableJoinFlag": 1}), "ELIGIBLE")
        self.assertEqual(self.state({"ableJoinFlag": 1, "joinNum": 999, "joinmaxnum": "不限"}), "ELIGIBLE")
        self.assertEqual(self.state({"ableJoinFlag": 1, "canJoin": True, "countdownText": "报名结束倒计时"}), "ELIGIBLE")
        self.assertTrue(college_blocked({"eligibility_detail": {"countdownText": "不在活动规定的院系内"}}))

    def test_secondary_sort_and_live_countdown(self):
        activities = []
        for aid, detail in enumerate([
            {"ableJoinFlag": 1, "countdown": 30000},
            {"ableJoinFlag": 1, "countdown": 10000},
            {"ableJoinFlag": 1, "joinenddate": 1800000000000},
            {"ableJoinFlag": 1, "joinenddate": 1700000000000},
            {}, {"injoin": True},
        ]):
            activities.append({"activityId": aid, "eligibility": judge_eligibility(detail, 10, now=1000)})
        self.assertEqual([a["activityId"] for a in sorted(activities, key=eligibility_key)], [5, 3, 2, 1, 0, 4])
        self.assertIn("20秒", eligibility_text(activities[0], now=1010))
        self.assertIn("等待服务端确认", eligibility_text(activities[0], now=1040))


class PluginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cls, self.ns = load_plugin()
        self.plugin = self.cls(types.SimpleNamespace(send_message=AsyncMock()), {
            "account": {"phone": "test", "password": "test"},
            "llm": {"enabled": False}, "push": {"as_image": False},
        })
        self.plugin._data_dir = temp.name
        self.plugin.seen = SeenStore(str(Path(temp.name) / "seen.json"))
        self.plugin.reminders = ReminderStore(str(Path(temp.name) / "reminders.json"))
        self.plugin._send = AsyncMock(return_value=True)

    def activity(self, aid, state):
        return {"activityId": aid, "name": "测试讲座", "credit": "0.1", "joinStartTime": 1250,
                "eligibility": {"state": state, "reason": state, "dimmed": state == "BLOCKED_PERM"}}

    def test_llm_never_receives_college_blocked_activities(self):
        acts = [self.activity(1, "SOON"), self.activity(2, "BLOCKED_PERM")]
        self.plugin.config["llm"]["enabled"] = True
        self.plugin._llm_via_astrabot = AsyncMock(return_value=[{
            "id": 1, "score": 80, "push": True, "label": "高性价比", "reason": "说明"}])
        asyncio.run(self.plugin._process_activities(acts, push_all=True))
        self.assertEqual(self.plugin._llm_via_astrabot.call_args.args[0], [acts[0]])
        self.assertEqual(len(self.plugin.reminders.records), 2)
        self.assertIn("BLOCKED_PERM", self.plugin._send.call_args.args[0])

    def test_all_college_blocked_skips_llm_completely(self):
        self.plugin.config["llm"]["enabled"] = True
        self.plugin._llm_via_astrabot = AsyncMock()
        asyncio.run(self.plugin._process_activities([self.activity(1, "BLOCKED_PERM")]))
        self.plugin._llm_via_astrabot.assert_not_awaited()
        self.plugin._send.assert_not_awaited()

    def test_rule_fallback_preserves_previous_llm_score(self):
        activity = self.activity(1, "SOON")
        ai = {"id": 1, "score": 95, "push": True, "label": "高", "reason": "模型原始理由", "source": "llm"}
        self.plugin.reminders.update([activity], [ai])
        self.plugin.config["llm"]["enabled"] = True
        from analyzer import judge_rule
        self.plugin._llm_via_astrabot = AsyncMock(return_value=judge_rule([activity], {}))
        asyncio.run(self.plugin._process_activities([activity]))
        self.assertEqual(self.plugin.reminders.records["1"]["result"], ai)
        self.assertEqual(self.plugin._last_llm, "LLM 评分")

    def test_provider_resolution_uses_registered_chat_provider(self):
        self.plugin.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(side_effect=RuntimeError("no default")),
            get_using_provider_async=AsyncMock(return_value=None),
            get_all_providers=lambda: [types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="deepseek"))])
        self.assertEqual(asyncio.run(self.plugin._resolve_provider_id()), "deepseek")

    def test_list_forwards_all_eligible_scores_from_cache_during_pause(self):
        states = ("SOON", "ELIGIBLE", "JOINABLE_TRIBE", "BLOCKED_PERM", "UNKNOWN", "JOINED", "FULL")
        acts = [self.activity(n, state) for n, state in enumerate(states)]
        results = [{"id": n, "score": 10 + n, "push": False, "label": "低", "reason": "低分原因", "source": "llm"}
                   for n in range(len(acts))]
        self.plugin.reminders.update(acts, results)
        self.plugin._poll_pause_until = float("inf")
        self.plugin._pause_status = lambda: "报名保护期"
        self.plugin._poll_sync = Mock(side_effect=AssertionError("must not request DMKJ"))
        self.plugin._llm_via_astrabot = AsyncMock(side_effect=AssertionError("must not call model"))
        event = types.SimpleNamespace(get_self_id=lambda: "12345", chain_result=lambda chain: chain,
                                      plain_result=lambda text: text)
        async def collect():
            return [item async for item in self.plugin.dmkj_list(event)]
        messages = asyncio.run(collect())
        nodes = messages[0][0].nodes
        self.assertEqual(len(nodes), 4)
        self.assertIn("共 3 个活动", nodes[0].content[0])
        self.assertIn("报名保护期", nodes[0].content[0])
        self.assertIn("ID：1", nodes[1].content[0])
        self.assertIn("ID：0", nodes[2].content[0])
        self.assertIn("ID：2", nodes[3].content[0])
        self.assertIn("低分原因", nodes[1].content[0])
        self.assertEqual(nodes[1].uin, "12345")
        self.plugin._send.assert_not_awaited()
        self.plugin._poll_sync.assert_not_called()
        self.plugin._llm_via_astrabot.assert_not_awaited()

    def test_list_omits_removed_activities_and_handles_empty_cache(self):
        act = self.activity(1, "SOON")
        self.plugin.reminders.update([act], [{"id": 1, "score": 90}])
        self.plugin._latest_activity_ids = set()
        self.plugin._save_list_membership()
        self.assertFalse(self.plugin.reminders.records["1"]["listed"])
        event = types.SimpleNamespace(plain_result=lambda text: text)
        async def collect():
            return [item async for item in self.plugin.dmkj_list(event)]
        self.assertIn("暂无", asyncio.run(collect())[0])

    def test_poll_caches_details_and_handles_one_failure(self):
        client = Mock()
        client.list_activities.return_value = [{"activityId": 1}, {"activityId": 2}]
        client.get_detail.side_effect = lambda ctx, aid: {"ableJoinFlag": 1, "canJoin": True} if aid == 1 else None
        self.plugin._client, self.plugin._ctx = client, {"collegeId": 10}
        first, error = self.plugin._poll_sync()
        self.assertIsNone(error)
        self.assertEqual([a["eligibility"]["state"] for a in first], ["ELIGIBLE", "UNKNOWN"])
        second, error = self.plugin._poll_sync()
        self.assertEqual(client.get_detail.call_count, 3)  # known detail cached, failed detail retried
        self.assertEqual(len(second), 2)

    def test_session_expiry_relogs_once(self):
        old, fresh = Mock(), Mock()
        old.list_activities.side_effect = SessionExpiredError("expired")
        fresh.login.return_value = {"collegeId": 10}
        fresh.list_activities.return_value = []
        self.plugin._client, self.plugin._ctx = old, {"collegeId": 10}
        self.ns["DmkjClient"] = Mock(return_value=fresh)
        self.assertEqual(self.plugin._poll_sync(), ([], None))
        fresh.login.assert_called_once()

    def test_reminder_pauses_before_send_and_retry_does_not_extend(self):
        clock = types.SimpleNamespace(time=lambda: 1000)
        self.ns["time"] = clock
        record_activity = self.activity(1, "SOON")
        self.plugin.reminders.update([record_activity], [{"id": 1, "score": 80, "label": "高", "reason": "说明"}])
        async def failed_send(text):
            self.assertTrue(self.plugin._is_polling_paused())
            self.assertEqual(self.plugin._poll_pause_until, 2800)
            return False
        self.plugin._send = AsyncMock(side_effect=failed_send)
        asyncio.run(self.plugin._check_signup_reminders())
        self.assertEqual(json.loads((Path(self.plugin._data_dir) / "polling_pause.json").read_text())["until"], 2800)
        clock.time = lambda: 1001
        asyncio.run(self.plugin._check_signup_reminders())
        self.assertEqual(self.plugin._poll_pause_until, 2800)
        restored = ReminderStore(self.plugin.reminders.path)
        self.assertTrue(restored.records["1"]["pause_started"])

    def test_pause_blocks_automatic_manual_and_direct_polling(self):
        self.ns["time"] = types.SimpleNamespace(time=lambda: 1000)
        self.plugin._poll_pause_until = 2800
        client = Mock()
        self.plugin._client, self.plugin._ctx = client, {}
        asyncio.run(self.plugin._tick())
        asyncio.run(self.plugin._startup_push())
        self.assertEqual(self.plugin._poll_sync(), ([], None))
        event = types.SimpleNamespace(plain_result=lambda text: text)
        async def command_result(command):
            return [text async for text in command(event)]
        for command in (self.plugin.dmkj_now, self.plugin.dmkj_scan):
            self.assertIn("报名保护期", asyncio.run(command_result(command))[0])
        self.assertEqual(client.method_calls, [])
        self.ns["time"] = types.SimpleNamespace(time=lambda: 2800)
        self.assertFalse(self.plugin._is_polling_paused())

    def test_blocked_joined_unknown_never_trigger_signup_reminder(self):
        acts = [self.activity(n, state) for n, state in enumerate(("BLOCKED_PERM", "JOINED", "UNKNOWN", "JOINABLE_TRIBE", "FULL"))]
        self.plugin.reminders.update(acts, [{"id": a["activityId"], "score": 100} for a in acts])
        self.assertEqual(self.plugin.reminders.due({}, 1000), [])

    def test_eligibility_transition_persists_and_sends_once(self):
        result = [{"id": 1, "score": 80, "label": "高", "reason": "说明"}]
        self.plugin.reminders.update([self.activity(1, "BLOCKED")], result)
        self.plugin.reminders.update([self.activity(1, "SOON")], result)
        asyncio.run(self.plugin._check_eligibility_changes())
        asyncio.run(self.plugin._check_eligibility_changes())
        self.assertEqual(self.plugin._send.await_count, 1)
        self.assertTrue(ReminderStore(self.plugin.reminders.path).records["1"]["transition"]["sent"])


if __name__ == "__main__":
    unittest.main()
