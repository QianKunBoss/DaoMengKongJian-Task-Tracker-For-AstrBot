"""本地 Pillow 长图渲染：AstrBot t2i 服务不可用（远程 503 / 未配置 playwright）时的兜底。

不依赖浏览器与远程服务，纯本地绘制；需要 Pillow 与一个中文字体（按常见路径自动探测）。
"""
import os

# 配色（与 HTML 卡片一致：白底 + 钢蓝 + 橙色高性价比）
C_BG = (245, 247, 250)
C_HEADER = (47, 90, 143)      # 2f5a8f
C_SUB = (201, 215, 234)
C_WHITE = (255, 255, 255)
C_STAR = (217, 130, 43)       # d9822b
C_BLUE = (70, 129, 196)       # 4681c4
C_TITLE = (43, 47, 54)
C_META = (91, 100, 114)
C_REASON_BG = (242, 246, 251)
C_REASON = (74, 82, 96)
C_MUTED = (138, 148, 163)
C_FOOTER = (154, 164, 178)

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

_font_cache: dict = {}


def _load_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont
    font = None
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _wrap(draw, text, font, max_w):
    """按像素宽度逐字折行（中文无空格，逐字测量最稳）。"""
    lines, line = [], ""
    for ch in str(text):
        try:
            ok = draw.textlength(line + ch, font=font) <= max_w
        except Exception:
            ok = True
        if ok:
            line += ch
        else:
            lines.append(line)
            line = ch
    if line:
        lines.append(line)
    return lines or [""]


def render_card(data: dict, out_path: str) -> str:
    """渲染长图卡片并保存到 out_path，返回路径。data 结构与 HTML 模板一致。"""
    from PIL import Image, ImageDraw

    W, PAD, GAP = 680, 24, 12
    inner_w = W - PAD * 2
    f_title, f_sub, f_num, f_lbl = _load_font(24), _load_font(13), _load_font(22), _load_font(12)
    f_name, f_badge, f_meta, f_reason, f_footer = _load_font(16), _load_font(12), _load_font(13), _load_font(13), _load_font(12)

    items = data.get("items", [])
    name_max_w = inner_w - 36 - 150   # 预留左侧条与右上徽章
    reason_max_w = inner_w - 36

    # 量高：临时画布折行
    tmp = Image.new("RGB", (W, 10))
    td = ImageDraw.Draw(tmp)
    per = []
    for it in items:
        name_lines = _wrap(td, it.get("name", ""), f_name, name_max_w)
        reason_lines = (_wrap(td, it.get("eligibility", "报名资格尚未获取"), f_reason, reason_max_w)
                        + _wrap(td, it.get("reason", ""), f_reason, reason_max_w))
        h = 72 + len(name_lines) * 24 + len(reason_lines) * 20 + (34 if it.get("group") else 0)
        per.append((it, name_lines, reason_lines, h))

    summary_lines = []
    for line in str(data.get("eligibility_summary") or "").splitlines():
        summary_lines.extend(_wrap(td, line, f_sub, inner_w - 40))
    head_h = 168 + len(summary_lines) * 21
    H = max(700, PAD + head_h + GAP + sum(h + GAP for _, _, _, h in per) + 50)
    img = Image.new("RGB", (W, H), C_BG)
    d = ImageDraw.Draw(img)

    # ---- 头部 ----
    y = PAD
    d.rounded_rectangle([PAD, y, W - PAD, y + head_h], radius=16, fill=C_HEADER)
    d.text((PAD + 20, y + 14), str(data.get("title", "")), font=f_title, fill=C_WHITE)
    d.text((PAD + 20, y + 46), f'{data.get("date", "")} · 到梦空间活动探测', font=f_sub, fill=C_SUB)
    tiles = [
        ("活动总数", data.get("total", 0), C_BLUE),
        ("高性价比", data.get("stars", 0), C_STAR),
        ("中等", data.get("ok", 0), C_BLUE),
        ("低性价比", data.get("warn", 0), C_MUTED),
    ]
    tw = (inner_w - 40 - 3 * 10) // 4
    ty = y + 74
    for i, (lbl, num, col) in enumerate(tiles):
        x0 = PAD + 20 + i * (tw + 10)
        d.rounded_rectangle([x0, ty, x0 + tw, ty + 54], radius=10, fill=C_WHITE)
        n = str(num)
        d.text((x0 + (tw - d.textlength(n, font=f_num)) / 2, ty + 5), n, font=f_num, fill=col)
        d.text((x0 + (tw - d.textlength(lbl, font=f_lbl)) / 2, ty + 33), lbl, font=f_lbl, fill=C_MUTED)
    d.text((PAD + 20, y + 140), f'共展示 {len(items)} 条 · 按报名资格与开放/截止时间排序', font=f_sub, fill=C_SUB)
    for index, line in enumerate(summary_lines):
        d.text((PAD + 20, y + 164 + index * 21), line, font=f_sub, fill=C_SUB)
    y += head_h + GAP

    # ---- 推荐卡片 ----
    for it, name_lines, reason_lines, h in per:
        x0, x1 = PAD, W - PAD
        if it.get("group"):
            d.text((x0 + 4, y + 4), str(it["group"]), font=f_name, fill=C_HEADER)
            y += 34
            h -= 34
        dimmed = it.get("dimmed", False)
        d.rounded_rectangle([x0, y, x1, y + h], radius=12, fill=(237, 240, 244) if dimmed else C_WHITE)
        bar = C_MUTED if dimmed else C_STAR if (it.get("score") or 0) >= 70 else C_BLUE
        d.rectangle([x0, y + 10, x0 + 4, y + h - 10], fill=bar)

        badge = f'{it.get("score", 0)}分 · {it.get("label_short", "")}'
        bw = d.textlength(badge, font=f_badge) + 20
        bx0, bx1 = x1 - 14 - bw, x1 - 14
        d.rounded_rectangle([bx0, y + 14, bx1, y + 38], radius=9, fill=bar)
        d.text((bx0 + 10, y + 18), badge, font=f_badge, fill=C_WHITE)

        ny = y + 16
        for ln in name_lines:
            d.text((x0 + 18, ny), ln, font=f_name, fill=C_MUTED if dimmed else C_TITLE)
            ny += 24
        d.text((x0 + 18, ny + 2), f'学分：{it.get("credit", "-")}　时间：{it.get("time", "")}',
               font=f_meta, fill=C_META)
        ry = ny + 30
        box_h = len(reason_lines) * 20 + 12
        d.rounded_rectangle([x0 + 14, ry, x1 - 14, ry + box_h], radius=8, fill=C_REASON_BG)
        ry2 = ry + 6
        for ln in reason_lines:
            d.text((x0 + 24, ry2), ln, font=f_reason, fill=C_MUTED if dimmed else C_REASON)
            ry2 += 20
        y += h + GAP

    # ---- 页脚 ----
    foot = "astrbot_plugin_dmkj_finder · 分多且活少，自动帮你盯"
    d.text(((W - d.textlength(foot, font=f_footer)) / 2, y + 4), foot, font=f_footer, fill=C_FOOTER)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img = img.crop((0, 0, W, min(H, y + 36)))
    img.save(out_path, "PNG")
    return out_path
