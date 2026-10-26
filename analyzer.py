"""活动性价比评分：优先走一次批量 LLM 调用，失败/未启用则规则兜底（免费）。

设计目标（省 token）：
- 仅在「有新增活动」时才调用 LLM，稳定态（无新增）0 token。
- 一次把本周期所有新增活动打包成一条 prompt，只回 JSON。
- 输入只给「名称 + 学分 + 说明前 120 字」，不拉取大段正文。
- LLM 不可用 / 出错时自动降级到规则打分，不影响推送。

LLM 调用分两种：
1) AstrBot 已保存的模型（推荐）：由 main.py 通过 self.context.llm_generate 调用，
   本模块只负责「构造 prompt」与「解析结果」。
2) 显式直连（测试/特殊场景）：analyzer.judge 在配置含 base_url+api_key 时直接打 OpenAI。
"""
import json
import logging
import re

logger = logging.getLogger("dmkj_finder.analyzer")

# 低付出特征词：命中 -> 性价比分 +（活少：听讲/观看/打卡/签到/体验类，基本去了就有）
LOW_EFFORT = [
    "观影", "电影", "看电影", "签到", "讲座", "培训", "宣讲", "打卡", "晨读", "早读", "自习",
    "观看", "线上", "直播", "轻松", "娱乐", "听讲", "欣赏", "沙龙", "展览", "分享会", "观影会",
    "解压", "舒压", "心理", "团辅", "辅导", "疗愈", "放松", "游园", "体验", "趣味", "答题",
    "知识竞赛", "读书", "阅读", "参观", "旁听", "画展", "影展", "赏析", "朗读",
]
# 高付出特征词：命中 -> 性价比分 -（活多：要产出/排练/比赛/体力劳动类）
HIGH_EFFORT = [
    "心得", "征文", "论文", "报告", "实践报告", "300字", "字以上", "篇以上", "不少于", "字数",
    "观后感", "读后感", "花絮", "水印", "总结", "拍照", "截图", "转发", "视频", "短视频", "策划",
    "方案", "海报", "推文", "排班", "排练", "训练", "演出", "表演", "答辩", "比赛", "大赛", "竞赛",
    "马拉松", "长跑", "越野", "运动会", "劳动", "清扫", "打扫", "作业", "笔记", "创作", "拍摄",
    "调研", "采访", "演讲", "授课", "备课", "教案", "手工", "绘画", "书法", "剪纸", "舞蹈", "合唱",
    "备战", "设计", "作品", "制作",
]

SYSTEM_PROMPT = (
    "你是帮助大学生筛选「到梦空间」第二课堂活动的助手。"
    "目标：找出「学分高、付出低」的活动（例如看电影、讲座、签到、打卡、心理团辅、培训等轻松活动），"
    "过滤「要写心得/论文/报告/截图/转发/排练/比赛」等高付出活动。"
    "注意：该校活动学分普遍很小（多为 0.01~0.1 学分），因此「付出高低」是主要判断依据，学分仅作微调。"
    "对每条活动只依据给出的 name、credit、excerpt 判断，不要编造信息。"
)

USER_PROMPT = (
    "下面是本次新增的活动（JSON 数组），每条含 id、name、credit（学分）、excerpt（活动说明前120字）：\n"
    "{activities}\n\n"
    "请对每条给出判断，严格只返回如下 JSON（不要任何额外文字）：\n"
    '{"results":[{"id":<活动id>,"push":<bool:是否值得推送给用户>,"score":<0-100性价比分,越高越好>,'
    '"label":<"⭐高性价比·低付出" / "✅中等" / "⚠️低性价比">,"reason":<一句话中文理由>}]}'
)


def _to_float(v):
    # 学分常以字符串返回，如 "0.02学分" / "2学分" / "1.5"，需抠出数值
    if v is None:
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    if not m:
        return 0.0
    try:
        return float(m.group())
    except ValueError:
        return 0.0


def _rule_score(a: dict) -> int:
    text = f"{a.get('name', '')} {a.get('excerpt', '')}"
    credit = _to_float(a.get("credit"))
    baseline = 50.0                                     # 中性基线：无显著特征 ≈ 中等
    credit_bonus = min(20.0, credit * 150.0)            # 0.1 学分≈+15，0.01 学分≈+1.5（学分普遍很小，仅微调）
    low = min(30, sum(12 for w in LOW_EFFORT if w in text))
    high = min(30, sum(12 for w in HIGH_EFFORT if w in text))
    score = baseline + credit_bonus + low - high
    return int(max(0, min(100, score)))


def _label(score: int) -> str:
    if score >= 70:
        return "⭐高性价比·低付出"
    if score >= 40:
        return "✅中等"
    return "⚠️低性价比"


def _rule_one(a: dict, push_threshold: int) -> dict:
    score = _rule_score(a)
    label = _label(score)
    credit = _to_float(a.get("credit"))
    text = f"{a.get('name', '')} {a.get('excerpt', '')}"
    hits_low = [w for w in LOW_EFFORT if w in text][:3]
    hits_high = [w for w in HIGH_EFFORT if w in text][:3]
    parts = []
    if credit >= 0.01:
        parts.append(f"学分 {credit}")
    if hits_low:
        parts.append("低付出:" + "/".join(hits_low))
    if hits_high:
        parts.append("高付出:" + "/".join(hits_high))
    if not parts:
        parts.append("无显著特征")
    return {
        "id": a.get("activityId"),
        "push": score >= push_threshold,
        "score": score,
        "label": label,
        "reason": "；".join(parts),
    }


def _rule_judge(activities, push_threshold: int):
    return [_rule_one(a, push_threshold) for a in activities]


# ---------------- 供 main.py（AstrBot 模型）使用 ----------------
def _activities_items(activities):
    return [
        {
            "id": a.get("activityId"),
            "name": a.get("name"),
            "credit": a.get("credit"),
            "excerpt": (a.get("excerpt") or "")[:80],
        }
        for a in activities
    ]


def build_user_prompt(activities) -> str:
    """把系统指令与活动列表拼成一个 user prompt（AstrBot llm_generate 走单 prompt）。"""
    items = json.dumps(_activities_items(activities), ensure_ascii=False)
    # 用 replace 而非 str.format，避免 prompt 中的 JSON 示例大括号被当成格式化字段
    return SYSTEM_PROMPT + "\n\n" + USER_PROMPT.replace("{activities}", items)


def _extract_json(text):
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        text = m.group(1)
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        text = text[s:e + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


def parse_llm_result(raw_text, activities, cfg: dict) -> list:
    """把 LLM 返回文本解析成 [{id,push,score,label,reason}]，缺漏用规则补全。"""
    push_threshold = int((cfg or {}).get("push_threshold") or 60)
    data = _extract_json(raw_text)
    raw = data.get("results", []) or []
    idset = {a.get("activityId") for a in activities}
    out = []
    got = set()
    for r in raw:
        aid = r.get("id")
        if aid not in idset:
            continue
        try:
            score = int(r.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        push = bool(r.get("push", score >= push_threshold))
        label = str(r.get("label") or _label(score))
        reason = str(r.get("reason") or "")
        out.append({"id": aid, "push": push, "score": score, "label": label, "reason": reason})
        got.add(aid)
    # 漏网的（LLM 没返回）用规则补，保证每条都有结果
    for a in activities:
        if a.get("activityId") not in got:
            out.append(_rule_one(a, push_threshold))
    return out


# ---------------- 同步入口（供测试 / 显式直连） ----------------
def _llm_judge(activities, cfg: dict):
    """显式直连 OpenAI 兼容接口（仅当配置含 base_url+api_key 时由 judge 调用）。"""
    llm = cfg.get("llm", {})
    base = (llm.get("base_url") or "").rstrip("/")
    key = llm.get("api_key") or ""
    model = llm.get("model") or "gpt-4o-mini"
    timeout = int(llm.get("timeout") or 10)
    push_threshold = int(cfg.get("push_threshold") or 60)

    prompt = USER_PROMPT.replace("{activities}", json.dumps(_activities_items(activities), ensure_ascii=False))
    import requests  # 插件环境已自带；函数内导入避免无谓依赖
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return parse_llm_result(content, activities, cfg)
    except Exception as e:  # 网络/解析/限流任何异常 -> 规则兜底
        logger.warning("[dmkj_finder] 直连 LLM 失败，降级规则打分: %s", e)
        return _rule_judge(activities, push_threshold)


def judge(activities, cfg: dict) -> list:
    """同步入口：返回 [{id, push, score, label, reason}, ...]。

    - 无新增活动 -> 空列表（不耗 token）。
    - llm.enabled=false 或无 key -> 直接规则打分。
    - llm 启用且配置了 base_url+api_key -> 一次批量直连调用；失败自动降级规则。
    （使用 AstrBot 已保存模型时请走 main.py 的 _llm_via_astrbot，不要调这个。）
    """
    if not activities:
        return []
    push_threshold = int((cfg or {}).get("push_threshold") or 60)
    llm = (cfg or {}).get("llm", {})
    if llm.get("enabled") and llm.get("base_url") and llm.get("api_key"):
        return _llm_judge(activities, cfg)
    return _rule_judge(activities, push_threshold)


def judge_rule(activities, cfg: dict) -> list:
    """纯规则打分（免费，0 token）。供 main.py 在 LLM 未启用或 LLM 失败时调用。"""
    if not activities:
        return []
    push_threshold = int((cfg or {}).get("push_threshold") or 60)
    return _rule_judge(activities, push_threshold)
