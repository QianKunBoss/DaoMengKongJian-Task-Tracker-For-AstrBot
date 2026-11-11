"""把服务端报名资格字段转成可解释状态；不提交报名或加入部落请求。"""
from __future__ import annotations

import math
import time

try:
    from .reminders import signup_timestamp
except ImportError:
    from reminders import signup_timestamp

STATES = {
    "JOINED": (0, "✍ 已报名"),
    "ELIGIBLE": (1, "✅ 可报名"),
    "SOON": (2, "🕒 等待报名开放"),
    "JOINABLE_TRIBE": (3, "👥 需先加入部落"),
    "FULL": (4, "🈵 名额已满"),
    "BLOCKED": (5, "⛔ 暂不可参与"),
    "BLOCKED_PERM": (6, "🚫 院系不符，永久不可参与"),
    "UNKNOWN": (7, "❔ 资格未知"),
}
NO_RIGHT = ("不在活动规定", "无权限", "不符合", "仅限", "不在该")
DETAIL_FIELDS = (
    "ableJoinFlag", "canJoin", "countdown", "countdownText", "joinrange", "joinrangeText",
    "collegeid", "collegename", "injoin", "signUpId", "joinNum", "joinmaxnum", "joinstartdate",
    "joinenddate", "startdateDate", "startdate", "outname", "tribeVo", "tribeId", "outid",
)


def _true(value):
    return value is True or str(value).lower() in ("true", "1")


def _number(value, default=float("inf")):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _timestamp(value):
    return signup_timestamp({"joinStartTime": value})


def _college(value):
    try:
        result = int(value)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None


def judge_eligibility(detail: dict, college_id=None, now=None) -> dict:
    """UNKNOWN 不等于有资格；院系未知时不承诺加入学院部落即可解锁。"""
    now = time.time() if now is None else now
    text = str(detail.get("countdownText") or "").strip()
    scope = str(detail.get("joinrangeText") or "")
    tribe_scope = scope.startswith("部落") or str(detail.get("joinrange")) == "1"
    college_scope = scope.startswith("学院") or str(detail.get("joinrange")) == "2"
    mine, theirs = _college(college_id), _college(detail.get("collegeid"))
    different = mine is not None and theirs is not None and mine != theirs
    start = signup_timestamp(detail)
    secondary = float("inf")
    tribe_id = None

    # injoin 表示活动处于报名阶段，并非当前账号已报名。
    # 仅个人报名记录 ID 是明确的已报名证据；缺失时不猜测。
    if _number(detail.get("signUpId"), 0) > 0:
        state, reason = "JOINED", "已报名"
        secondary = _timestamp(detail.get("startdateDate")) or start or secondary
    elif (any(hint in text for hint in NO_RIGHT)
          or (not _true(detail.get("ableJoinFlag")) and different and (college_scope or tribe_scope))):
        if different:
            state = "BLOCKED_PERM"
            reason = f"{text or '院系不符'}（{scope or '限定范围'}：{detail.get('collegename') or '其他院系'}｜永久不可参与）"
        elif tribe_scope and (mine is not None and mine == theirs or str(detail.get("collegeid")) == "-1"):
            state = "JOINABLE_TRIBE"
            tribe = detail.get("tribeVo") if isinstance(detail.get("tribeVo"), dict) else {}
            name = detail.get("outname") or tribe.get("name") or "该部落"
            tribe_id = detail.get("tribeId") or detail.get("outid") or tribe.get("tribeId") or tribe.get("id")
            reason = f"{text or '部落成员限制'}（需先加入「{name}」）"
            if tribe_id:
                reason += f"；部落 ID：{tribe_id}，请在官方客户端搜索加入"
        else:
            state = "BLOCKED"
            extra = detail.get("collegename")
            reason = (text or "报名范围受限") + (f"（{extra}）" if extra else "")
            if tribe_scope and mine is None and theirs is not None:
                reason += "；当前账号院系未知，无法确认加入部落能否解锁"
    elif _true(detail.get("ableJoinFlag")):
        capacity = _number(detail.get("joinmaxnum"))
        count = _number(detail.get("joinNum"), -1)
        if capacity >= 0 and math.isfinite(capacity) and count >= capacity:
            state, reason = "FULL", f"名额已满 {detail.get('joinNum')}/{detail.get('joinmaxnum')}，可关注退选名额"
        elif ((start is not None and start > now and not _true(detail.get("canJoin")))
              or (("倒计时" in text or _number(detail.get("countdown"), 0) > 0)
                  and not _true(detail.get("canJoin")) and "结束" not in text and "截止" not in text)):
            state, reason = "SOON", text or "报名开始倒计时"
            secondary = start or now + max(0, _number(detail.get("countdown"), 0)) / 1000
        elif detail.get("canJoin") is not None and not _true(detail.get("canJoin")):
            state, reason = "BLOCKED", text or "服务端暂不允许报名"
        else:
            state, reason = "ELIGIBLE", "可报名" + (f"，{text}" if text else "")
            secondary = _timestamp(detail.get("joinenddate")) or secondary
    elif text:
        state, reason = "BLOCKED", text
    else:
        state, reason = "UNKNOWN", "暂不可报（报名资格或详情尚未获取）"
    return {"state": state, "reason": reason, "weight": STATES[state][0],
            "secondary": secondary if math.isfinite(secondary) else None,
            "dimmed": STATES[state][0] >= 4, "tribe_id": tribe_id}


def eligibility_text(activity: dict, now=None) -> str:
    info = activity.get("eligibility") or {}
    state = info.get("state", "UNKNOWN")
    reason = info.get("reason") or "报名资格尚未获取"
    if state == "SOON":
        now = time.time() if now is None else now
        target = info.get("secondary")
        if target is not None:
            seconds = max(0, math.ceil(target - now))
            eta = f"{seconds}秒" if seconds < 60 else f"{seconds // 60}分{seconds % 60}秒"
            reason += f" · 距开放约 {eta}" if seconds else " · 已到预计开放时间，等待服务端确认"
    return f"{STATES.get(state, STATES['UNKNOWN'])[1]}｜{reason}"


def eligibility_key(activity: dict):
    info = activity.get("eligibility") or {}
    state = info.get("state", "UNKNOWN")
    secondary = info.get("secondary") if state in ("SOON", "ELIGIBLE", "JOINED") else None
    return STATES.get(state, STATES["UNKNOWN"])[0], secondary if secondary is not None else float("inf")


def recommendable(activity: dict) -> bool:
    return (activity.get("eligibility") or {}).get("state") in ("ELIGIBLE", "SOON", "JOINABLE_TRIBE")


def college_blocked(activity: dict) -> bool:
    info = activity.get("eligibility") or {}
    if info.get("state") == "BLOCKED_PERM":
        return True
    text = str((activity.get("eligibility_detail") or {}).get("countdownText") or info.get("reason") or "")
    return any(hint in text for hint in NO_RIGHT) and ("院系" in text or "学院" in text)


def remindable(activity: dict) -> bool:
    return (activity.get("eligibility") or {}).get("state") in ("ELIGIBLE", "SOON")
