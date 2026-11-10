"""AstrBot 插件：到梦空间活动探测 + AI 筛选推送。

工作流（省 token）：
1. 定时器每 poll_interval 秒拉一次活动列表（仅列表字段，极轻量）。
2. 用 SeenStore 过滤出「新增」活动，已见过的直接忽略（增量推送）。
3. 【开机总结+推荐】插件启动时先总结当前「全部」活动（总数 + 高/中/低性价比分布），
   再推送「推荐」的（高性价比、即评分达阈值）活动，让你一开机就掌握全貌 + 重点。
4. 仅对目标活动：拉详情取前 120 字说明 -> 交给 analyzer 评分。
5. analyzer 优先一次批量 LLM 调用（使用 AstrBot 已保存的模型）；未启用/失败则规则兜底（0 token）。
6. 被判定 push=true 的活动合并成一条消息推送到配置的会话。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain
try:
    from astrbot.api.message_components import Node, Nodes
except ImportError:  # pragma: no cover
    Node = Nodes = None

# MessageChain 跨版本导入兼容
try:
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover
    from astrbot.core.message.message_event_result import MessageChain

# 主动推送时，新版本 send_message 收 MessageChain；旧版本收 MessageEventResult().message(...)。
# 两者都导入，发送时先试 MessageChain，失败再回退，兼容不同 AstrBot 版本。
try:
    from astrbot.api.event import MessageEventResult
except ImportError:  # pragma: no cover
    try:
        from astrbot.core.message.message_event_result import MessageEventResult
    except ImportError:
        MessageEventResult = None

# Image 组件（长图推送用）。个别版本路径不同或缺失时置 None，自动回退文本推送。
try:
    from astrbot.api.message_components import Image
except ImportError:  # pragma: no cover
    try:
        from astrbot.core.message.components import Image
    except ImportError:
        Image = None

# 导入兼容：AstrBot 可能以包或模块方式加载插件
try:
    from .dmkj_client import DmkjClient, SessionExpiredError
    from .analyzer import judge, judge_rule, build_user_prompt, parse_llm_result
    from .seen_store import SeenStore
    from .reminders import ReminderStore, reminder_options, signup_timestamp, CHINA_TZ
    from .eligibility import (DETAIL_FIELDS, STATES, judge_eligibility, eligibility_text,
                              eligibility_key, recommendable, college_blocked)
except ImportError:  # pragma: no cover
    from dmkj_client import DmkjClient, SessionExpiredError
    from analyzer import judge, judge_rule, build_user_prompt, parse_llm_result
    from seen_store import SeenStore
    from reminders import ReminderStore, reminder_options, signup_timestamp, CHINA_TZ
    from eligibility import (DETAIL_FIELDS, STATES, judge_eligibility, eligibility_text,
                             eligibility_key, recommendable, college_blocked)

try:
    from astrbot.api import StarTools
except ImportError:  # pragma: no cover
    from astrbot.core.star.star_tools import StarTools

# 本地 Pillow 长图渲染（t2i 服务不可用时的兜底）；Pillow 缺失时置 None，仅剩文本回退
try:
    from .card_render import render_card
except ImportError:  # pragma: no cover
    try:
        from card_render import render_card
    except ImportError:
        render_card = None

PLUGIN_ID = "astrbot_plugin_dmkj_finder"
PLUGIN_VERSION = "1.0.0"


class PollingPausedError(RuntimeError):
    pass

# 长图卡片模板（Jinja2）：白底 + 钢蓝主色 + 圆角卡片，full_page 截图即为长图
CARD_TMPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body { font-family: "Microsoft YaHei","PingFang SC",sans-serif; background:#f5f7fa; margin:0; padding:24px; width:620px; }
.header { background:#2f5a8f; color:#fff; border-radius:14px; padding:20px 22px; }
.header h1 { margin:0 0 4px; font-size:22px; }
.header .sub { font-size:12px; opacity:.85; }
.stats { display:flex; gap:10px; margin-top:14px; }
.stat { flex:1; background:#fff; border-radius:12px; padding:10px 0; text-align:center; }
.stat .num { font-size:20px; font-weight:700; color:#2f5a8f; }
.stat .lbl { font-size:12px; color:#8a94a3; margin-top:2px; }
.tip { margin-top:12px; font-size:12px; opacity:.9; }
.card { background:#fff; border-radius:12px; padding:13px 15px; margin-top:12px; border-left:4px solid #4681c4; }
.card.dimmed { background:#edf0f4; border-left-color:#a8afb9; }
.card.dimmed .name, .card.dimmed .meta, .card.dimmed .reason { color:#78828f; }
.group { margin-top:18px; font-size:15px; font-weight:600; color:#2f5a8f; }
.eligibility { font-size:13px; margin-top:8px; line-height:1.5; }
.eligibility-summary { white-space:pre-line; font-size:12px; margin-top:12px; line-height:1.6; }
.card .top { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.card .name { font-size:15px; font-weight:600; color:#2b2f36; line-height:1.4; }
.badge { font-size:12px; color:#fff; background:#4681c4; border-radius:9px; padding:2px 9px; white-space:nowrap; }
.badge.star { background:#d9822b; }
.meta { font-size:12px; color:#5b6472; margin-top:6px; }
.reason { font-size:12px; color:#4a5260; background:#f2f6fb; border-radius:8px; padding:6px 10px; margin-top:8px; line-height:1.5; }
.footer { text-align:center; color:#9aa4b2; font-size:11px; margin-top:14px; }
</style></head><body>
  <div class="header">
    <h1>{{ title }}</h1>
    <div class="sub">{{ date }} · 到梦空间活动探测</div>
    <div class="stats">
      <div class="stat"><div class="num">{{ total }}</div><div class="lbl">活动总数</div></div>
      <div class="stat"><div class="num">{{ stars }}</div><div class="lbl">⭐ 高性价比</div></div>
      <div class="stat"><div class="num">{{ ok }}</div><div class="lbl">✅ 中等</div></div>
      <div class="stat"><div class="num">{{ warn }}</div><div class="lbl">⚠️ 低性价比</div></div>
    </div>
    <div class="tip">共展示 {{ items | length }} 条 · 按报名资格与开放/截止时间排序</div>
    <div class="eligibility-summary">{{ eligibility_summary | default('') | e }}</div>
  </div>
  {% for it in items %}
  {% if it.group %}<div class="group">{{ it.group | e }}</div>{% endif %}
  <div class="card {{ 'dimmed' if it.dimmed else '' }}">
    <div class="top">
      <div class="name">{{ it.name | e }}</div>
      <div class="badge {{ 'star' if it.score >= 70 else '' }}">{{ it.score }}分 · {{ it.label_short }}</div>
    </div>
    <div class="meta">💯 {{ it.credit }}　⏰ {{ it.time }}</div>
    <div class="eligibility">{{ it.eligibility | default('报名资格尚未获取') | e }}</div>
    <div class="reason">🤖 {{ it.reason | e }}</div>
  </div>
  {% endfor %}
  <div class="footer">astrbot_plugin_dmkj_finder · 分多且活少，自动帮你盯</div>
</body></html>"""


@register(PLUGIN_ID, "QKboss", "到梦空间活动探测与 AI 筛选推送（分多且活少）", PLUGIN_VERSION)
class DmkjFinderPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.scheduler = None
        self.seen: SeenStore | None = None
        # 登录态缓存（避免每分钟重复登录）
        self._client = None
        self._ctx = None
        # 推送目标会话（unified_msg_origin），用于解析 AstrBot 默认模型 provider
        self._push_umo: str | None = None
        # 统计
        self._last_poll_ts = 0.0
        self._last_new_count = 0
        self._last_error: str | None = None
        self._last_llm = "未使用"
        self._startup_done = False
        # 轮询互斥：保证同一时刻只有一次拉取在跑，避免定时任务叠乘
        self._poll_busy = False
        # 连续推送失败次数（成功即清零；微信 iLink 通道 ret=-2 频控惩罚期内连发会延长惩罚）
        self._send_fail_streak = 0
        # 登录账号信息（开机上线通知/状态用）
        self._login_info: dict | None = None
        self.reminders = None
        self._reminder_busy = False
        self._eligibility_busy = False
        self._detail_cache = {}
        self._latest_activity_ids = None
        self._poll_pause_until = 0.0

    async def initialize(self):
        # 数据目录：优先用框架接口，失败回退到插件本地 data/，避免个别版本 StarTools 接口差异导致插件加载即崩溃
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_ID)
        except Exception as e:
            logger.warning("[dmkj_finder] get_data_dir 失败，回退到本地 data 目录: %s", e)
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(str(data_dir), exist_ok=True)
        self._data_dir = str(data_dir)
        try:
            with open(os.path.join(self._data_dir, "polling_pause.json"), encoding="utf-8") as f:
                self._poll_pause_until = float(json.load(f).get("until", 0))
        except (OSError, ValueError, TypeError):
            self._poll_pause_until = 0.0
        self.seen = SeenStore(os.path.join(str(data_dir), "seen.json"))
        self.reminders = ReminderStore(os.path.join(str(data_dir), "signup_reminders.json"))
        self._detail_cache = {
            aid: (record["activity"].get("eligibility_checked_at", 0), record["activity"]["eligibility_detail"])
            for aid, record in self.reminders.records.items()
            if record.get("activity", {}).get("eligibility_detail")
        }
        self._push_umo = (self.config.get("push", {}) or {}).get("session") or None
        interval = int(self.config.get("poll_interval", 60))

        # 关键：把调度器显式绑定到 AstrBot 当前运行的事件循环。
        # 否则 APScheduler 可能挂到别的 loop 上，导致定时任务和开机任务永远不触发。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

        self.scheduler = AsyncIOScheduler(event_loop=loop, timezone="Asia/Shanghai")
        # 常规增量轮询：首次延后到 poll_interval 秒后，避免与开机推送抢跑。
        # max_instances>1 配合下方 _tick 的互斥守卫，避免“上一轮未结束本轮被跳过”的告警；
        # misfire_grace_time/coalesce 让偶发延迟仍能合并执行而不是被丢弃。
        self.scheduler.add_job(
            self._tick,
            "interval",
            seconds=interval,
            id="dmkj_poll",
            next_run_time=datetime.now() + timedelta(seconds=max(interval, 15)),
            max_instances=5,
            misfire_grace_time=120,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._check_signup_reminders, "interval", seconds=15,
            id="dmkj_signup_reminders", max_instances=1, coalesce=True,
            misfire_grace_time=60,
        )
        self.scheduler.start()

        # 开机总结+推荐：直接在运行中的 loop 上派发一次性任务（比 date 触发更稳，不受 misfire 影响）
        if self.config.get("startup_push", True):
            self._schedule_startup(loop)

        if not self._push_umo:
            logger.warning(
                "[dmkj_finder] ⚠️ 未配置 push.session，开机推送将无法送达！"
                "请在「想接收推送的群/私聊」里发 /dmkj session，把返回的字符串填到插件配置 push.session。"
            )
        logger.info("[dmkj_finder] 已启动，轮询间隔 %ds，已记录活动 %d 条", interval, len(self.seen))

    def _schedule_startup(self, loop):
        """在运行中的事件循环上派发一次开机扫描；拿不到运行 loop 时回退到调度器的 date 触发。"""
        try:
            loop.call_soon(asyncio.ensure_future, self._delayed_startup())
        except RuntimeError:
            self.scheduler.add_job(
                self._startup_push,
                "date",
                run_date=datetime.now() + timedelta(seconds=5),
                id="dmkj_startup",
                misfire_grace_time=300,
            )

    async def _delayed_startup(self):
        await asyncio.sleep(3)  # 稍等 AstrBot 完全就绪
        await self._startup_push()

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            logger.info("[dmkj_finder] 调度器已关闭")

    def _is_polling_paused(self) -> bool:
        return time.time() < self._poll_pause_until

    def _guard_polling(self):
        if self._is_polling_paused():
            raise PollingPausedError("报名保护期内暂停请求到梦空间")

    def _pause_polling_for_reminders(self, due: list):
        fresh = [record for _, record in due if not record.get("pause_started")]
        if not fresh:
            return
        self._poll_pause_until = max(self._poll_pause_until, time.time() + 30 * 60)
        path = os.path.join(self._data_dir, "polling_pause.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"until": self._poll_pause_until}, f)
        os.replace(path + ".tmp", path)
        for record in fresh:
            record["pause_started"] = True
        self.reminders.save()
        logger.info("[dmkj_finder] 报名提醒已触发，暂停轮询/自动重登30分钟，至 %s",
                    datetime.fromtimestamp(self._poll_pause_until, CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"))

    # ---------------- 拉取核心 ----------------
    def _poll_sync(self, filter_new: bool = True, _retried: bool = False):
        """同步拉取 + 取详情摘要。在 to_thread 中执行，避免阻塞事件循环。

        filter_new=True  -> 只返回去重后的新增活动（增量轮询用）
        filter_new=False -> 返回当前全部活动（开机全量快照用）
        """
        acc = self.config.get("account", {}) or {}
        if not acc.get("phone") or not acc.get("password"):
            return [], "未配置 account.phone / account.password"
        try:
            self._guard_polling()
            client = self._client
            ctx = self._ctx
            if client is None or ctx is None:
                client = DmkjClient(
                    version=self.config.get("version", "4.9.8"),
                    timeout=int(self.config.get("timeout", 12)),
                )
                self._guard_polling()
                ctx = client.login(acc["phone"], acc["password"])
                self._client = client
                self._ctx = ctx
                self._login_info = {
                    "name": ctx.get("name") or "?",
                    "school": ctx.get("school") or "?",
                    "uid": ctx.get("uid"),
                    "phone": self._mask_phone(acc.get("phone") or ""),
                }
                logger.info(
                    "[dmkj_finder] ✅ 登录成功：%s（%s）uid=%s",
                    ctx.get("name") or "?", ctx.get("school") or "?", ctx.get("uid"),
                )

            self._guard_polling()
            acts = client.list_activities(ctx, self.config.get("search", {}) or {})
            self._latest_activity_ids = {str(a["activityId"]) for a in acts}
            # 所有已加载活动都参与资格刷新；详情走缓存，评分仍只处理新增。
            pool = acts
            mode = "增量评分/资格刷新" if filter_new else "全量"
            logger.info(
                "[dmkj_finder] 📋 轮询结果：到梦空间列表共 %d 条，%s待处理 %d 条",
                len(acts), mode, len(pool),
            )

            def _fetch_detail(a):
                self._guard_polling()
                aid = str(a["activityId"])
                now = time.time()
                checked, cached = self._detail_cache.get(aid, (0, {}))
                eligibility_cfg = self.config.get("eligibility") or {}
                college_id = ctx.get("collegeId") or eligibility_cfg.get("college_id")
                state = judge_eligibility(cached, college_id, now)["state"]
                try:
                    ttl = max(60, int(eligibility_cfg.get("cache_ttl", 600)))
                except (ValueError, TypeError):
                    ttl = 600
                if state in ("SOON", "ELIGIBLE", "JOINABLE_TRIBE", "FULL", "BLOCKED", "UNKNOWN"):
                    ttl = min(ttl, max(60, int(self.config.get("poll_interval", 60))))
                try:
                    self._guard_polling()
                    d = cached if cached and filter_new and now - checked < ttl else client.get_detail(ctx, a["activityId"]) or {}
                    if d is not cached and d:
                        # 只保存展示/判定需要的字段，避免把完整详情永久写入数据目录。
                        d = {key: d[key] for key in (*DETAIL_FIELDS, "content", "joinTip") if key in d}
                        d["content"] = str(d.get("content") or "")[:120]
                        d["joinTip"] = str(d.get("joinTip") or "")[:120]
                        checked = now
                        self._detail_cache[aid] = (checked, d)
                except SessionExpiredError:
                    raise
                except PollingPausedError:
                    raise
                except Exception:
                    d = {}
                b = dict(a)
                b["eligibility"] = judge_eligibility(d, college_id, checked if d else now)
                b["eligibility_detail"] = {key: d[key] for key in DETAIL_FIELDS if key in d}
                b["eligibility_checked_at"] = checked if d else 0
                # 报名开始时间来自报名字段，不能用活动举行时间替代。
                detail_start = signup_timestamp(d)
                if detail_start is not None:
                    b["joinStartTime"] = detail_start
                elif signup_timestamp(b) is None and not d and self.reminders:
                    # 接口暂时失败时保留已知时间，下轮继续刷新。
                    old = self.reminders.records.get(str(a.get("activityId")), {})
                    b["joinStartTime"] = old.get("start")
                b["excerpt"] = (((d.get("content") or "") + " " + (d.get("joinTip") or ""))[:120]).strip()
                return b

            # 并发拉详情（最多 5 并发，兼顾速度与对服务端友好）。无活动时直接返回。
            if not pool:
                return [], None
            with ThreadPoolExecutor(max_workers=min(4, len(pool))) as ex:
                enriched = list(ex.map(_fetch_detail, pool))
            self._guard_polling()
            return enriched, None
        except PollingPausedError:
            return [], None
        except SessionExpiredError:
            self._client = None
            self._ctx = None
            self._detail_cache.clear()
            if self._is_polling_paused():
                return [], None
            if not _retried:
                logger.info("[dmkj_finder] 登录态失效，自动重登并重拉一次")
                return self._poll_sync(filter_new, _retried=True)
            return [], "登录态刷新后仍失效，请检查账号登录情况"
        except Exception as e:
            # 多半是 token 过期，清空缓存下次重登
            self._client = None
            self._ctx = None
            return [], f"轮询异常: {e}"

    # ---------------- 评分 + 推送（增量与开机共用） ----------------
    async def _process_activities(self, acts: list, title: str = "【到梦空间·新活动】",
                                  summarize: bool = False, push_all: bool = False):
        llm = self.config.get("llm", {}) or {}
        score_inputs = [a for a in acts if recommendable(a) and not college_blocked(a)]
        excluded = [a for a in acts if not recommendable(a) or college_blocked(a)]
        if llm.get("enabled") and score_inputs:
            if llm.get("base_url") and llm.get("api_key"):
                results = await asyncio.to_thread(judge, score_inputs, self.config)
                self._last_llm = "直连"
            else:
                results = await self._llm_via_astrabot(score_inputs, self.config)
                self._last_llm = "AstrBot 模型"
        else:
            results = judge_rule(score_inputs, self.config)
            self._last_llm = "规则兜底"
        if results is None:
            results = judge_rule(score_inputs, self.config)
        if llm.get("enabled") and self.reminders:
            preserved = []
            for result in results:
                previous = (self.reminders.records.get(str(result["id"]), {}).get("result") or {})
                if result.get("source") == "rule" and previous.get("source") == "llm":
                    result = dict(previous)
                    logger.info("[dmkj_finder] 模型本轮失败，保留活动 %s 的已有 LLM 评分", result["id"])
                preserved.append(result)
            results = preserved
        sources = {r.get("source", "llm" if llm.get("enabled") else "rule") for r in results}
        self._last_llm = ("LLM 评分" if sources == {"llm"} else
                          "LLM + 规则兜底" if "llm" in sources else "规则兜底")
        # 院系不符条目保留本地规则评分，完整列表仍展示；从不进入模型输入。
        results.extend(judge_rule(excluded, self.config))
        if excluded:
            logger.info("[dmkj_finder] 资格受限/已报名/未知 %d 条，已从 LLM 输入排除", len(excluded))

        if self.reminders:
            self.reminders.update(acts, results)
            self._save_list_membership()

        # 推送集合：push_all=True 推全部（手动全量），否则只推评分达阈值的高性价比活动（推荐）。
        by_id = {str(a["activityId"]): a for a in acts}
        to_push = results if push_all else [r for r in results if r.get("push") and recommendable(by_id.get(str(r["id"]), {}))]

        # 组装消息：标题 +（可选）总结所有活动 + 推荐活动明细
        parts = [title]
        if summarize:
            parts.append(self._build_summary(acts, results))
        if to_push:
            parts.append(self._render_blocks(acts, to_push))
        text = "\n".join(parts)

        if len(parts) > 1:  # 至少要有「总结」或「推荐明细」才发送
            push_cfg = self.config.get("push", {}) or {}
            try:
                image_min = int(push_cfg.get("image_min", 6) or 6)
            except (TypeError, ValueError):
                image_min = 6
            sent = False
            # 推荐较多时用长图卡片（更清爽）；少量推荐用文本更直接；渲染失败自动回退文本
            if push_cfg.get("as_image", True) and Image is not None and len(to_push) >= image_min:
                sent = await self._send_image_card(acts, results, to_push, title)
            if not sent:
                await self._send(text)

        # 无论推送成功与否，都标记为已见，避免反复判断
        for a in acts:
            self.seen.add(a["activityId"])

        self._last_poll_ts = time.time()
        self._last_new_count = len(acts)
        self._last_error = None
        logger.info(
            "[dmkj_finder] 📤 已处理 %d 条，推送推荐 %d 条（评分方式：%s）",
            len(acts), len(to_push), self._last_llm,
        )
        return len(to_push)

    async def _tick(self):
        """常规增量轮询：只处理去重后的新增活动。"""
        if self._is_polling_paused():
            self._last_new_count = 0
            return
        if self._poll_busy:
            logger.debug("[dmkj_finder] 上一轮轮询仍在进行，本次跳过")
            return
        self._poll_busy = True
        try:
            await self._check_signup_reminders()
            if self._is_polling_paused():
                return
            new, err = await asyncio.to_thread(self._poll_sync, True)
            if err:
                self._last_error = err
                logger.warning("[dmkj_finder] %s", err)
                return
            self._save_list_membership()
            if not new:
                self._last_poll_ts = time.time()
                self._last_error = None
                return
            fresh = self.seen.filter_new(new)
            existing = [a for a in new if not self.seen.is_new(a.get("activityId"))]
            if existing and self.reminders:
                cached = [self.reminders.records.get(str(a["activityId"]), {}).get("result")
                          or judge_rule([a], self.config)[0] for a in existing]
                self.reminders.update(existing, cached)
            if fresh:
                await self._process_activities(fresh)
            else:
                self._last_poll_ts = time.time()
                self._last_new_count = 0
                self._last_error = None
            await self._check_signup_reminders()
            await self._check_eligibility_changes()
        except Exception as e:
            self._last_error = str(e)
            logger.exception("[dmkj_finder] tick 出错")
        finally:
            self._poll_busy = False

    async def _startup_push(self, show_all: bool = False):
        """开机：先推上线通知（状态/账号/剩余处理量），再总结全部活动并推送推荐的（高性价比）活动。"""
        if self._is_polling_paused():
            return
        if self._poll_busy:
            logger.info("[dmkj_finder] 已有轮询在进行，本次开机/全量扫描跳过")
            return
        self._poll_busy = True
        try:
            await self._check_signup_reminders()
            if self._is_polling_paused():
                return
            all_acts, err = await asyncio.to_thread(self._poll_sync, False)
            # 上线通知：无论成功失败都发一条，让用户确认推送链路通、知道登的哪个号
            remaining = len(self.seen.filter_new(all_acts)) if all_acts else 0
            await self._send_startup_notice(total=len(all_acts), remaining=remaining, err=err)
            await self._pace()  # 微信 iLink 突发频控：上线通知和长图之间留间隔
            if err:
                self._last_error = err
                logger.warning("[dmkj_finder] 开机扫描失败: %s", err)
                return
            if not all_acts:
                logger.info("[dmkj_finder] 开机检测：当前无活动")
                self._startup_done = True
                return
            n = await self._process_activities(
                all_acts, title="【到梦空间·全部活动与报名资格】" if show_all else "【到梦空间·开机总结】",
                summarize=True, push_all=show_all
            )
            self._startup_done = True
            logger.info("[dmkj_finder] 开机完成：共 %d 活动，推送推荐 %d 条", len(all_acts), n)
        except Exception as e:
            self._last_error = str(e)
            logger.exception("[dmkj_finder] 开机扫描出错")
        finally:
            self._poll_busy = False

    # ---------------- 报名与资格提醒 ----------------
    async def _check_eligibility_changes(self):
        if self._eligibility_busy or self.reminders is None:
            return
        if not (self.config.get("eligibility") or {}).get("notify_changes", True):
            return
        self._eligibility_busy = True
        try:
            pending = [(aid, r) for aid, r in self.reminders.records.items()
                       if r.get("transition") and not r["transition"].get("sent") and r.get("result")]
            if not pending:
                return
            blocks = ["【到梦空间·报名资格变化】"]
            for _, record in sorted(pending, key=lambda item: eligibility_key(item[1]["activity"])):
                transition = record["transition"]
                blocks.append(f"{STATES[transition['from']][1]} → {STATES[transition['to']][1]}\n"
                              + self._fmt(record["activity"], record["result"]))
            if await self._send("\n──────────\n".join(blocks)):
                for aid, record in pending:
                    current = self.reminders.records.get(aid, {})
                    if current.get("transition") == record.get("transition"):
                        current["transition"]["sent"] = True
                self.reminders.save()
        except Exception:
            logger.exception("[dmkj_finder] 资格变化提醒失败，下次重试")
        finally:
            self._eligibility_busy = False

    async def _check_signup_reminders(self):
        if self._reminder_busy or self.reminders is None:
            return
        self._reminder_busy = True
        try:
            due = self.reminders.due(self.config, time.time())
            if not due:
                return
            # 在尝试发送之前进入保护期，发送失败的重试也不恢复登录/轮询。
            self._pause_polling_for_reminders(due)
            blocks = ["【到梦空间·报名开始提醒】"]
            for _, record in due:
                start = datetime.fromtimestamp(record["start"], CHINA_TZ)
                blocks.append(
                    f"⏰ 报名开始：{start:%Y-%m-%d %H:%M:%S}（北京时间）\n"
                    + self._fmt(record["activity"], record["result"])
                )
            if await self._send("\n──────────\n".join(blocks)):
                self.reminders.mark_sent(due)
        except Exception:
            logger.exception("[dmkj_finder] 报名提醒检查失败，下次重试")
        finally:
            self._reminder_busy = False

    # ---------------- LLM（AstrBot 已保存的模型） ----------------
    async def _llm_via_astrabot(self, activities, cfg):
        """分批调用 AstrBot 模型评分：每批活动数少 → 单次响应快，降低超时概率。
        某一批失败只让该批降级规则，不影响其他批。"""
        llm_cfg = cfg.get("llm") or {}
        provider = llm_cfg.get("provider")
        try:
            batch_size = max(1, int(llm_cfg.get("batch_size") or 12))
        except (TypeError, ValueError):
            batch_size = 12

        results = []
        total_batches = (len(activities) + batch_size - 1) // batch_size
        for i in range(0, len(activities), batch_size):
            chunk = activities[i:i + batch_size]
            batch_no = i // batch_size + 1
            try:
                prompt = build_user_prompt(chunk)
                text = await self._llm_complete(prompt, provider)
                results.extend(parse_llm_result(text, chunk, cfg))
                logger.info("[dmkj_finder] 🤖 LLM 批次 %d/%d 完成（%d 条）", batch_no, total_batches, len(chunk))
            except Exception as e:
                logger.warning(
                    "[dmkj_finder] LLM 批次 %d/%d 调用失败（%d 条），该批降级规则: %s",
                    batch_no, total_batches, len(chunk), e,
                )
                results.extend(judge_rule(chunk, cfg))
        return results

    async def _llm_complete(self, prompt: str, provider_override: str | None = None) -> str:
        provider_id = await self._resolve_provider_id(provider_override)
        llm_resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        if llm_resp is None:
            raise RuntimeError("llm_generate 返回 None")
        text = getattr(llm_resp, "completion_text", None)
        if text is None and isinstance(llm_resp, dict):
            text = llm_resp.get("completion_text")
        if not text:
            raise RuntimeError("LLM 返回内容为空")
        return text

    async def _resolve_provider_id(self, override=None):
        """解析用于定时任务的聊天模型 provider：
        1) 配置指定的 provider id；
        2) 推送目标会话(push.session)对应的 provider；
        3) 不传 umo 让 AstrBot 取默认；
        4) 全局配置里的 default_provider；
        5) 已注册的第一个模型。
        """
        if override:
            return override
        for attempt in (self._push_umo, None):
            try:
                pid = await self.context.get_current_chat_provider_id(umo=attempt)
                if pid:
                    return pid
            except Exception:
                pass
        try:
            provider = await self.context.get_using_provider_async(umo=self._push_umo)
            if provider:
                return provider.meta().id
        except Exception:
            pass
        try:
            providers = self.context.get_all_providers()
            if providers:
                return providers[0].meta().id
        except Exception:
            pass
        try:
            cfg = self.context.get_config()
            provider_cfg = cfg.get("provider") or {}
            pid = (
                cfg.get("default_provider")
                or (provider_cfg.get("default") if isinstance(provider_cfg, dict) else None)
                or (provider_cfg.get("default_provider") if isinstance(provider_cfg, dict) else None)
            )
            if pid:
                return pid
        except Exception:
            pass
        try:
            llms = getattr(self.context, "llms", None)
            if llms:
                return llms[0].llm_name
        except Exception:
            pass
        return None

    # ---------------- 推送 ----------------
    def _fmt(self, a: dict, r: dict) -> str:
        name = a.get("name", "")
        credit = a.get("credit")
        t = a.get("activitytime") or a.get("statusText") or ""
        return (
            f"📌 {name}\n"
            f"💯 学分：{credit}　⏰ {t}\n"
            f"{eligibility_text(a)}\n"
            f"🏷 {r['label']}（{r['score']}分）\n"
            f"🤖 {r['reason']}\n"
            f"🔗 ID：{a.get('activityId')}"
        )

    def _render_blocks(self, activities: list, results: list) -> str:
        by_id = {a["activityId"]: a for a in activities}
        blocks = []
        ordered = sorted(results, key=lambda r: eligibility_key(by_id.get(r["id"], {})))
        counts = {}
        for r in ordered:
            state = (by_id.get(r["id"], {}).get("eligibility") or {}).get("state", "UNKNOWN")
            counts[state] = counts.get(state, 0) + 1
        previous = None
        for r in ordered:
            a = by_id.get(r["id"])
            if a:
                state = (a.get("eligibility") or {}).get("state", "UNKNOWN")
                if state != previous:
                    blocks.append(f"── {STATES[state][1]}（{counts[state]}）──")
                    previous = state
                blocks.append(self._fmt(a, r))
        return "\n──────────\n".join(blocks)

    def _build_summary(self, acts: list, results: list) -> str:
        """对所有活动的统计总结（不消耗额外 token，基于评分结果聚合）。"""
        total = len(acts)
        stars = sum(1 for r in results if (r.get("score") or 0) >= 70)
        ok = sum(1 for r in results if 40 <= (r.get("score") or 0) < 70)
        warn = sum(1 for r in results if (r.get("score") or 0) < 40)
        by_id = {str(a["activityId"]): a for a in acts}
        rec = sum(1 for r in results if r.get("push") and recommendable(by_id.get(str(r["id"]), {})))
        states = {}
        for a in acts:
            state = (a.get("eligibility") or {}).get("state", "UNKNOWN")
            states[state] = states.get(state, 0) + 1
        eligibility_summary = "\n".join(f"  · {STATES[state][1]}：{states[state]} 个"
                                        for state in STATES if states.get(state))
        return (
            "📊 当前共 {total} 个活动\n"
            "  · ⭐高性价比（分多活少）：{stars} 个\n"
            "  · ✅中等：{ok} 个\n"
            "  · ⚠️低性价比：{warn} 个\n"
            "📋 报名资格\n{eligibility_summary}\n"
            "🔎 已为你筛选出 {rec} 个推荐活动 ↓"
        ).format(total=total, stars=stars, ok=ok, warn=warn, rec=rec, eligibility_summary=eligibility_summary)

    @staticmethod
    def _mask_phone(phone: str) -> str:
        p = str(phone or "")
        if len(p) >= 7:
            return p[:3] + "****" + p[-4:]
        return "***" if p else "未填"

    async def _send_startup_notice(self, total: int, remaining: int, err: str | None = None):
        """开机上线通知：插件状态 + 登录账号 + 剩余处理量。无论开机扫描成败都发一条。"""
        if not self.config.get("startup_notice", True):
            return
        info = self._login_info or {}
        interval = self.config.get("poll_interval", 60)
        lines = ["🟢 到梦空间助手已上线" + ("（异常）" if err else "")]
        lines.append(f"· 版本 {PLUGIN_VERSION} ｜ 轮询间隔 {interval}s")
        if err:
            msg = str(err)
            lines.append("· 登录/拉取失败：" + (msg[:80] + "…" if len(msg) > 80 else msg))
            lines.append("· 请检查 account 配置或网络，之后每轮轮询会自动重试")
        else:
            lines.append(f"· 登录账号：{info.get('name', '?')}（{info.get('school', '?')}）")
            lines.append(f"· 绑定手机：{info.get('phone', '未填')}　uid：{info.get('uid', '-')}")
            lines.append(f"· 活动总量：{total} 条 ｜ 剩余待处理：{remaining} 条")
            lines.append("开机总结与推荐马上送达 ↓")
        await self._send("\n".join(lines))

    @staticmethod
    def _short_label(score: int) -> str:
        if score >= 70:
            return "高性价比"
        if score >= 40:
            return "中等"
        return "低性价比"

    def _collect_card_data(self, acts: list, results: list, to_push: list, title: str) -> dict:
        """组装长图模板数据：统计 + 推荐明细（按评分降序）。"""
        by_id = {a["activityId"]: a for a in acts}
        items = []
        ordered = sorted(to_push, key=lambda r: eligibility_key(by_id.get(r["id"], {})))
        counts = {}
        for r in ordered:
            state = (by_id.get(r["id"], {}).get("eligibility") or {}).get("state", "UNKNOWN")
            counts[state] = counts.get(state, 0) + 1
        previous = None
        for r in ordered:
            a = by_id.get(r["id"])
            if not a:
                continue
            info = a.get("eligibility") or {}
            state = info.get("state", "UNKNOWN")
            items.append({
                "name": a.get("name") or "未命名活动",
                "credit": a.get("credit") or "-",
                "time": a.get("activitytime") or a.get("statusText") or "",
                "score": r.get("score") or 0,
                "reason": r.get("reason") or "",
                "label_short": self._short_label(r.get("score") or 0),
                "eligibility": eligibility_text(a),
                "dimmed": info.get("dimmed", True),
                "group": f"{STATES[state][1]}（{counts[state]}）" if previous != state else "",
            })
            previous = state
        stars = sum(1 for r in results if (r.get("score") or 0) >= 70)
        ok = sum(1 for r in results if 40 <= (r.get("score") or 0) < 70)
        warn = sum(1 for r in results if (r.get("score") or 0) < 40)
        return {
            "title": title,
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "total": len(acts),
            "stars": stars, "ok": ok, "warn": warn,
            "items": items,
            "eligibility_summary": self._build_summary(acts, results),
        }

    def _send_gap(self) -> int:
        """同一轮推送多条消息之间的间隔秒数（微信 iLink 有突发频控，间隔太短会触发惩罚）。"""
        try:
            return max(0, int((self.config.get("push", {}) or {}).get("send_gap", 3) or 3))
        except (TypeError, ValueError):
            return 3

    def _send_backoff(self) -> int:
        """推送失败后的退避秒数。"""
        try:
            return max(5, int((self.config.get("push", {}) or {}).get("send_backoff", 15) or 15))
        except (TypeError, ValueError):
            return 15

    async def _pace(self):
        """多条消息之间的节流间隔。"""
        gap = self._send_gap()
        if gap > 0:
            await asyncio.sleep(gap)

    async def _send_image_card(self, acts: list, results: list, to_push: list, title: str,
                               session_override: str | None = None) -> bool:
        """把总结+推荐渲染成长图推送。两级渲染：AstrBot t2i(HTML) → 本地 Pillow → 失败返回 False（回退文本）。

        session_override：测试指令用，直接发到指定会话而不读 push.session。
        渲染方式记录在 self._last_image_renderer（"AstrBot t2i" / "本地 Pillow" / None）。
        """
        self._last_image_renderer = None
        if Image is None or not ((hasattr(self, "html_render")) or render_card is not None):
            return False
        data = self._collect_card_data(acts, results, to_push, title)

        image_comp = None
        # 1) AstrBot 文转图（HTML 卡，效果最佳）
        if hasattr(self, "html_render"):
            try:
                url = await self.html_render(CARD_TMPL, data)
                image_comp = Image.fromURL(url)
                self._last_image_renderer = "AstrBot t2i"
                logger.info("[dmkj_finder] 🖼 长图已由 AstrBot t2i 渲染")
            except Exception as e:
                logger.warning("[dmkj_finder] t2i 渲染失败（%s），改用本地 Pillow 渲染", e)

        # 2) 本地 Pillow（不依赖远程服务与浏览器）
        if image_comp is None and render_card is not None:
            try:
                path = render_card(data, os.path.join(self._data_dir, "dmkj_card.png"))
                image_comp = Image.fromFileSystem(path)
                self._last_image_renderer = "本地 Pillow"
                logger.info("[dmkj_finder] 🖼 长图已由本地 Pillow 渲染：%s", path)
            except Exception as e:
                logger.warning("[dmkj_finder] 本地 Pillow 渲染失败: %s", e)

        if image_comp is None:
            return False

        try:
            push = self.config.get("push", {}) or {}
            session = session_override or push.get("session")
            if session:
                await self.context.send_message(session, MessageChain([image_comp]))
                self._send_fail_streak = 0
                logger.info("[dmkj_finder] 🖼 长图已推送至会话：%s", session)
                return True
            elif push.get("target_id"):
                t = "GroupMessage" if push.get("is_group") else "PrivateMessage"
                platform = push.get("platform_id") or "aiocqhttp"
                await StarTools.send_message_by_id(t, push["target_id"], MessageChain([image_comp]), platform=platform)
                self._send_fail_streak = 0
                logger.info("[dmkj_finder] 🖼 长图已推送至：%s %s(%s)", platform, t, push["target_id"])
                return True
            else:
                logger.warning(
                    "[dmkj_finder] ⚠️ 未配置推送目标（push.session 或 push.target_id），不会推送。"
                    "请发 /dmkj session 复制会话标识并填到 push.session。"
                )
                return False
        except Exception as e:
            self._send_fail_streak += 1
            logger.warning("[dmkj_finder] 长图发送失败，回退文本推送: %s", e)
            # 微信 iLink 通道对突发推送有频控惩罚（ret=-2 prepare failed），惩罚期内继续发会延长惩罚；
            # 这里退避一段时间再放行文本回退，避免热重试。
            await asyncio.sleep(self._send_backoff())
            return False

    async def _send(self, text: str) -> bool:
        push = self.config.get("push", {}) or {}
        session = push.get("session")
        chain = MessageChain([Plain(text)])
        # 止损：微信 iLink 通道频控惩罚期内连发会延长惩罚，连续失败 2 次后本次放弃
        if self._send_fail_streak >= 2:
            logger.error(
                "[dmkj_finder] ❌ 推送已连续失败 %d 次（微信 iLink 通道频控惩罚期，ret=-2 prepare failed）。"
                "本次推送放弃，不再重试以免延长惩罚。恢复方法：给机器人发一条任意消息即可立即激活会话。",
                self._send_fail_streak,
            )
            return False
        try:
            if session:
                await self.context.send_message(session, chain)
                self._send_fail_streak = 0
                logger.info("[dmkj_finder] 📨 消息已推送至会话：%s", session)
                return True
            elif push.get("target_id"):
                t = "GroupMessage" if push.get("is_group") else "PrivateMessage"
                platform = push.get("platform_id") or "aiocqhttp"
                await StarTools.send_message_by_id(t, push["target_id"], chain, platform=platform)
                self._send_fail_streak = 0
                logger.info("[dmkj_finder] 📨 消息已推送至：%s %s(%s)", platform, t, push["target_id"])
                return True
            else:
                logger.warning(
                    "[dmkj_finder] ⚠️ 未配置推送目标（push.session 或 push.target_id），不会推送。"
                    "请发 /dmkj session 复制会话标识并填到 push.session。"
                )
                return False
        except Exception as e:
            self._send_fail_streak += 1
            logger.warning("[dmkj_finder] send_message(MessageChain) 失败: %s；退避 %ds 后尝试 MessageEventResult 回退",
                           e, self._send_backoff())
            await asyncio.sleep(self._send_backoff())
            if MessageEventResult is not None and session:
                try:
                    await self.context.send_message(session, MessageEventResult().message(text))
                    self._send_fail_streak = 0
                    logger.info("[dmkj_finder] 📨 消息已推送至会话（回退方式）：%s", session)
                    return True
                except Exception as e2:
                    self._send_fail_streak += 1
                    logger.error(
                        "[dmkj_finder] ❌ 回退发送也失败: %s（连续失败 %d 次，本次止损不再重试）。"
                        "微信 iLink 通道频控惩罚期内继续发送会延长惩罚；给机器人发一条任意消息即可立即恢复会话。",
                        e2, self._send_fail_streak,
                    )
            else:
                logger.exception("[dmkj_finder] 推送失败（无 MessageEventResult 可用）: %s", e)
        return False

    # ---------------- 指令 ----------------
    @filter.command_group("dmkj")
    def dmkj(self):
        pass

    @dmkj.command("now")
    async def dmkj_now(self, event: AstrMessageEvent):
        """立即轮询一次到梦空间（增量模式）"""
        if self._is_polling_paused():
            yield event.plain_result(self._pause_status())
            return
        await self._tick()
        if self._last_error:
            yield event.plain_result(f"轮询失败：{self._last_error}")
        else:
            yield event.plain_result(f"轮询完成，本次处理 {self._last_new_count} 个活动（评分方式：{self._last_llm}）。")

    @dmkj.command("scan")
    @dmkj.command("pushall")
    async def dmkj_scan(self, event: AstrMessageEvent):
        """立即做一次全量扫描：总结所有活动并推送推荐的（忽略去重）"""
        if self._is_polling_paused():
            yield event.plain_result(self._pause_status())
            return
        await self._startup_push()
        if self._last_error:
            yield event.plain_result(f"全量扫描失败：{self._last_error}")
        else:
            yield event.plain_result(f"全量扫描完成（评分方式：{self._last_llm}）。")

    @dmkj.command("status")
    async def dmkj_status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
        last = self._last_poll_ts
        last_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "从未"
        llm = self.config.get("llm", {}) or {}
        mode = "AI 模型(AstrBot)" if llm.get("enabled") else "规则兜底"
        seen_n = len(self.seen) if self.seen else 0
        push_cfg = self.config.get("push", {}) or {}
        target = push_cfg.get("session") or push_cfg.get("target_id") or "（未配置！发 /dmkj session 获取）"
        acc = self._login_info or {}
        reminder_enabled, reminder_minutes, reminder_levels = reminder_options(self.config)
        yield event.plain_result(
            "到梦空间探测插件状态\n"
            f"· 版本：{PLUGIN_VERSION}\n"
            f"· 模式：{mode}\n"
            f"· 登录账号：{acc.get('name', '未登录')}（{acc.get('school', '-')}）\n"
            f"· 上次评分方式：{self._last_llm}\n"
            f"· 已记录活动：{seen_n}\n"
            f"· 上次轮询：{last_s}\n"
            f"· 上次处理：{self._last_new_count}\n"
            f"· 推送目标：{target}\n"
            f"· 轮询状态：{self._pause_status()}\n"
            f"· 报名提醒：{'开启' if reminder_enabled else '关闭'}，提前 {reminder_minutes} 分钟，"
            f"等级：{'、'.join(sorted(reminder_levels)) or '无'}\n"
            f"· 开机总结推送：{'已完成' if self._startup_done else '未执行'}\n"
            f"· 最后错误：{self._last_error or '无'}"
        )

    @dmkj.command("list")
    async def dmkj_list(self, event: AstrMessageEvent):
        """合并转发所有通过资格筛选的评分活动，直接回复当前会话。"""
        if self.reminders is None:
            yield event.plain_result("活动缓存尚未初始化，请稍后再试。")
            return
        records = [r for r in self.reminders.records.values()
                   if r.get("result") and recommendable(r.get("activity", {}))
                   and not college_blocked(r.get("activity", {}))
                   and r.get("listed", True)]
        if not records:
            yield event.plain_result("缓存中暂无通过报名资格筛选的评分活动，等待下一轮扫描后再试。")
            return
        if Node is None or Nodes is None:
            yield event.plain_result("当前 AstrBot 版本不支持合并转发组件，请升级后再试。")
            return
        records.sort(key=lambda r: eligibility_key(r["activity"]))
        header = (f"【到梦空间·报名资格筛选评分列表】\n共 {len(records)} 个活动，包含高／中／低性价比，"
                  "不受推荐阈值限制。\n使用最近一次扫描缓存；待加入部落的活动需先满足条件。")
        if self._is_polling_paused():
            header += "\n" + self._pause_status()
        texts = [header]
        for record in records:
            result = record["result"]
            source = {"llm": "LLM 评判", "rule": "本地规则（未启用模型或评分兜底）"}.get(
                result.get("source"), "已有评分缓存")
            texts.append(self._fmt(record["activity"], result) + f"\n评分来源：{source}")
        sender = str(event.get_self_id() or "0")
        for offset in range(0, len(texts), 50):
            nodes = [Node(uin=sender, name="到梦空间助手", content=[Plain(text)])
                     for text in texts[offset:offset + 50]]
            yield event.chain_result([Nodes(nodes=nodes)])

    def _save_list_membership(self):
        if self.reminders is not None and self._latest_activity_ids is not None:
            for aid, record in self.reminders.records.items():
                record["listed"] = aid in self._latest_activity_ids
            self.reminders.save()

    def _pause_status(self):
        if not self._is_polling_paused():
            return "正常轮询"
        until = datetime.fromtimestamp(self._poll_pause_until, CHINA_TZ)
        return f"报名保护期：暂停轮询和自动重登，{until:%Y-%m-%d %H:%M:%S}（北京时间）后自动恢复"

    @dmkj.command("image")
    async def dmkj_image(self, event: AstrMessageEvent):
        """测试长图渲染与发送链路（t2i → 本地 Pillow 双保险），直接发到当前会话"""
        now = time.strftime("%m-%d %H:%M")
        acts = [
            {"activityId": "T1", "name": "【测试】劳动教育讲座（线上观看）", "credit": "0.02学分", "activitytime": now},
            {"activityId": "T2", "name": "【测试】校园读书打卡活动", "credit": "0.05学分", "activitytime": now},
            {"activityId": "T3", "name": "【测试】迎新晚会节目排练", "credit": "0.1学分", "activitytime": now},
        ]
        results = [
            {"id": "T1", "score": 82, "label": "⭐ 高性价比", "reason": "线上观看，签到即可，几乎零付出", "push": True},
            {"id": "T2", "score": 68, "label": "✅ 中等", "reason": "每日打卡略占时间，但学分尚可", "push": True},
            {"id": "T3", "score": 35, "label": "⚠️ 低性价比", "reason": "需多次排练+彩排，时间成本高", "push": True},
        ]
        if Image is None:
            yield event.plain_result("❌ Image 组件不可用（当前 AstrBot 版本缺少消息图片组件），无法发长图")
            return
        yield event.plain_result("⏳ 正在渲染测试长图…")
        ok = await self._send_image_card(acts, results, results, "【到梦空间·长图测试】",
                                         session_override=event.unified_msg_origin)
        renderer = getattr(self, "_last_image_renderer", None)
        if ok:
            yield event.plain_result(f"✅ 长图测试成功（渲染方式：{renderer}）")
        else:
            yield event.plain_result(
                f"❌ 长图测试失败（渲染方式：{renderer or '无可用渲染器'}）。"
                "详情看控制台日志：t2i 与本地 Pillow 都失败时会回退文本。"
            )

    @dmkj.command("session")
    async def dmkj_session(self, event: AstrMessageEvent):
        """打印当前会话的 unified_msg_origin，复制到配置 push.session 即可接收推送"""
        yield event.plain_result(f"当前会话标识（复制到 push.session）：\n{event.unified_msg_origin}")
