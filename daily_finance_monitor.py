#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
每日金融信息获取脚本 · finance-filter 前置抓取层

定位（对应 SKILL.md「生态协同 · ① 前置降噪层」）：
  抓取你关注领域的财经信息 -> 做"机器能做的初筛"（信源分级 / 持仓相关性 / 情绪词检测）
  -> 剔除明显无关与噪音 -> 输出晨间降噪风格报告 + 可喂给降噪器的 JSON payload。

本脚本只做"少看"的第一道筛，不做最终判定：
  - 不输出任何买卖建议（SKILL 强约束 #1）。
  - 报告末尾附免责声明（强约束 #2）。
  - 抓取失败/无法判断的，标注"待验证 / 信源静默降级"，不强行下结论（强约束 #4，对应「数据质量守门」）。
  - Impact 为机器初筛值，仅供分流，最终判定交给降噪器（人/AI）复核。

依赖：仅标准库（urllib / xml.etree / json / re / datetime），python3 直接运行。

用法：
  python3 daily_finance_monitor.py                # 用同目录 finance_monitor_config.json
  python3 daily_finance_monitor.py --config x.json # 指定配置
  python3 daily_finance_monitor.py --init          # 仅生成默认配置文件后退出

每日运行建议：用 cron / 定时任务每天早上跑一次，报告写入 reports/YYYY-MM-DD.md。
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "finance_monitor_config.json")
REPORT_DIR = os.path.join(HERE, "reports")

# ---------------------------------------------------------------------------
# 默认配置：信源分级参考 source-credibility.md，关键词为示例，请按自己的持仓改写
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_DATA = {
    "_说明": "请按你的真实持仓/关注修改 watchlist；sources 可增删 RSS 源，tier 标信源等级。",
    "profile": {
        "投资周期": "中期",  # 长期 / 中期 / 短期，对应 SKILL Layer 4 时间尺度匹配
        "时间窗口小时": 30    # 只保留最近 N 小时内的消息（默认抓近 30 小时，覆盖昨夜到今早）
    },
    "watchlist": {
        # 直接相关：你的具体持仓（个股/基金/币种），命中即"直接相关"
        "direct": ["贵州茅台", "宁德时代", "沪深300", "纳斯达克"],
        # 间接相关：你关注的行业/板块
        "indirect": ["新能源", "白酒", "半导体", "锂电池", "光伏", "人工智能"],
        # 主题相关：影响全局的宏观主题
        "theme": ["美联储", "加息", "降息", "降准", "LPR", "CPI", "PMI", "GDP", "汇率", "央行", "关税"]
    },
    "sources": [
        # tier: T1(一手) / T2(头部媒体) / T3(专业分析) / T4(自媒体)
        # 下列为常见可公开访问的 RSS，源随时可能变动；失败会自动标注"信源静默降级"。
        {"name": "新浪财经-要闻", "url": "https://rss.sina.com.cn/roll/finance/hot_roll.xml", "tier": "T3"},
        {"name": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml", "tier": "T3"},
        {"name": "证券时报网", "url": "http://www.stcn.com/article/list.rss", "tier": "T2"}
    ],
    # 极端情绪词（参考 source-credibility.md 模式7 FOMO/FUD），命中则情绪降权
    "emotion_words": [
        "暴涨", "暴跌", "崩盘", "飙升", "狂飙", "末日", "清仓", "满仓", "重磅", "利好兑现",
        "最后机会", "错过", "马上", "立刻", "千万别", "惊呆", "炸裂", "血洗", "爆雷", "涨停", "跌停"
    ]
}

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}

DISCLAIMER = "> ⚠️ 以上为机器初筛结果，仅供降噪器进一步分析参考，不构成投资建议。"


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def load_config(path):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG_DATA, f, ensure_ascii=False, indent=2)
        print(f"[init] 已生成默认配置：{path}\n      请编辑其中的 watchlist / sources 后重新运行。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 抓取 + RSS 解析（兼容 RSS 2.0 与 Atom）
# ---------------------------------------------------------------------------
def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _text(elem):
    return html.unescape((elem.text or "").strip()) if elem is not None else ""


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def parse_pubdate(s):
    """尽量把各种时间字符串解析为 datetime（带时区则转 naive 本地近似）。失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in fmts:
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.tzinfo is not None:
                d = d.astimezone().replace(tzinfo=None)
            return d
        except ValueError:
            continue
    return None


def parse_feed(raw):
    """解析 RSS/Atom，返回 [{title, link, summary, published(datetime|None)}]"""
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items

    def tag(e):
        return e.tag.split("}")[-1]  # 去掉命名空间

    # RSS 2.0: channel/item ; Atom: entry
    nodes = [e for e in root.iter() if tag(e) in ("item", "entry")]
    for node in nodes:
        title, link, summary, pub = "", "", "", ""
        for child in node:
            t = tag(child)
            if t == "title":
                title = _text(child)
            elif t == "link":
                # Atom 用 href 属性，RSS 用文本
                link = child.get("href") or _text(child) or link
            elif t in ("description", "summary", "content"):
                if not summary:
                    summary = _strip_tags(_text(child))
            elif t in ("pubDate", "published", "updated"):
                if not pub:
                    pub = _text(child)
        items.append({
            "title": title,
            "link": link,
            "summary": summary[:200],
            "published": parse_pubdate(pub),
        })
    return items


# ---------------------------------------------------------------------------
# 初筛逻辑：对应 SKILL 五层漏斗中"机器能做"的部分
# ---------------------------------------------------------------------------
def relevance(text, watchlist):
    """Layer 3 持仓相关性。返回 (等级, 命中关键词)。优先级：直接 > 间接 > 主题 > 无关。"""
    for level, key in (("直接", "direct"), ("间接", "indirect"), ("主题", "theme")):
        for kw in watchlist.get(key, []):
            if kw and kw in text:
                return level, kw
    return "无关", None


def emotion(text, emotion_words):
    """Layer 2 情绪。返回 (标记, 命中词列表)。"""
    hits = [w for w in emotion_words if w in text]
    if len(hits) >= 2:
        return "🔴", hits          # 高度情绪化 -> 降权
    if len(hits) == 1:
        return "🟡", hits
    return "🟢", hits


def preliminary_signal(tier, rel, emo):
    """
    机器初筛信号等级 + Impact（仅分流用，待降噪器复核）。
    规则保守：宁可标 Watch 让人复核，不轻易判 Must Act。
    """
    if rel == "无关":
        return "⚪", 0
    tier_score = {"T1": 2, "T2": 2, "T3": 1, "T4": 0}.get(tier, 0)
    rel_score = {"直接": 2, "间接": 1, "主题": 1}.get(rel, 0)
    emo_penalty = {"🔴": -1, "🟡": 0, "🟢": 0}.get(emo, 0)
    score = tier_score + rel_score + emo_penalty  # 0~4 区间
    if rel == "直接" and tier_score >= 2 and emo != "🔴":
        return "🟡→候选🔴", min(score, 2)   # 直接持仓 + 高可信信源 -> 候选 Must Act，待复核
    if score >= 2:
        return "🟡", 1
    return "⚪", 0


def within_window(published, hours):
    if published is None:
        return True  # 无时间戳的不丢弃，但后续标"待验证"
    return (dt.datetime.now() - published) <= dt.timedelta(hours=hours)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(config):
    profile = config.get("profile", {})
    watchlist = config.get("watchlist", {})
    emotion_words = config.get("emotion_words", [])
    hours = int(profile.get("时间窗口小时", 30))

    collected = []        # 通过初筛的条目
    source_status = []    # 每个信源的抓取状态（数据质量守门）

    for src in config.get("sources", []):
        name, url, tier = src.get("name"), src.get("url"), src.get("tier", "T?")
        try:
            raw = fetch(url)
            entries = parse_feed(raw)
            if not entries:
                source_status.append((name, tier, "⚠️ 解析为空（信源静默降级，待验证）"))
                continue
            kept = 0
            for e in entries:
                text = f"{e['title']} {e['summary']}"
                if not within_window(e["published"], hours):
                    continue
                rel, rel_kw = relevance(text, watchlist)
                if rel == "无关":
                    continue  # 前置降噪：明显无关直接剔除，帮你少看
                emo, emo_hits = emotion(text, emotion_words)
                sig, impact = preliminary_signal(tier, rel, emo)
                collected.append({
                    "title": e["title"],
                    "link": e["link"],
                    "source": name,
                    "tier": tier,
                    "published": e["published"].strftime("%Y-%m-%d %H:%M") if e["published"] else "时间未知(待验证)",
                    "relevance": rel,
                    "match": rel_kw,
                    "emotion": emo,
                    "emotion_hits": emo_hits,
                    "signal": sig,
                    "impact": impact,
                })
                kept += 1
            source_status.append((name, tier, f"✅ 抓取 {len(entries)} 条 / 相关 {kept} 条"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as ex:
            source_status.append((name, tier, f"❌ 抓取失败：{ex}（信源静默降级，待验证）"))

    # 排序：候选 Must Act > Watch > 其他；同级按 Impact 绝对值
    order = {"🟡→候选🔴": 0, "🟡": 1, "⚪": 2}
    collected.sort(key=lambda x: (order.get(x["signal"], 9), -abs(x["impact"])))
    return collected, source_status, profile


def md_link(title, link):
    """生成 Markdown 链接；无链接时返回纯文本。title 内的 [] 与 | 做转义避免破坏表格。"""
    safe = title.replace("|", "/").replace("[", "(").replace("]", ")")
    if link:
        return f"[{safe}]({link})"
    return safe


def build_report(collected, source_status, profile):
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"# 每日金融信息初筛报告 · {today}")
    lines.append("")
    lines.append(f"- 投资周期：{profile.get('投资周期', '未设置')}")
    lines.append(f"- 时间窗口：最近 {profile.get('时间窗口小时', 30)} 小时")
    lines.append("")

    # 数据质量守门（SKILL 协同角色 ④）
    lines.append("## 信源抓取状态（数据质量守门）")
    lines.append("")
    lines.append("| 信源 | 等级 | 状态 |")
    lines.append("|------|------|------|")
    for name, tier, status in source_status:
        lines.append(f"| {name} | {tier} | {status} |")
    lines.append("")

    # 过滤概览（晨间降噪模板）
    candidates = [c for c in collected if c["signal"] == "🟡→候选🔴"]
    watch = [c for c in collected if c["signal"] == "🟡"]
    noise_cnt = len([c for c in collected if c["signal"] == "⚪"])

    lines.append(f"## 过滤概览（共筛出 {len(collected)} 条相关）")
    lines.append("")
    lines.append("| # | 标题 | 信源 | 等级 | 相关性 | 命中 | 情绪 | 初筛信号 | Impact |")
    lines.append("|---|------|------|------|--------|------|------|---------|--------|")
    for i, c in enumerate(collected, 1):
        title = md_link(c["title"][:40], c.get("link"))  # 标题可点击跳转原文
        lines.append(
            f"| {i} | {title} | {c['source']} | {c['tier']} | {c['relevance']} "
            f"| {c['match'] or '-'} | {c['emotion']} | {c['signal']} | {c['impact']:+d} |"
        )
    lines.append("")

    # 候选 Must Act 展开
    lines.append("## 🔴 候选 Must Act（建议优先用降噪器复核）")
    lines.append("")
    if candidates:
        for c in candidates:
            lines.append(f"- **{md_link(c['title'], c.get('link'))}**")
            lines.append(f"  - 信源 {c['source']}（{c['tier']}） · 命中持仓「{c['match']}」 · {c['published']}")
            if c["link"]:
                lines.append(f"  - 原文链接：{c['link']}")
            if c["emotion"] != "🟢":
                lines.append(f"  - ⚠️ 情绪词：{', '.join(c['emotion_hits'])}（注意标题党/情绪操纵，需核一手来源）")
        lines.append("")
        lines.append("> 下一步：把以上条目交给 finance-filter 降噪器做 Layer 0 预检 + 五层判定。")
    else:
        lines.append("（无）今日无命中直接持仓且高可信信源的候选。")
    lines.append("")

    # Watch 汇总
    lines.append("## 🟡 Watch（纳入观察，逐条一句话）")
    lines.append("")
    if watch:
        for c in watch:
            lines.append(f"- [{c['tier']} · {c['relevance']}] {md_link(c['title'], c.get('link'))}（{c['source']}）")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append(f"## ⚪ Noise")
    lines.append("")
    lines.append(f"另有 {noise_cnt} 条判定为低优先（相关但信源/情绪偏弱），已折叠，可在 JSON payload 中查看全部。")
    lines.append("")

    lines.append(DISCLAIMER)
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="每日金融信息获取与初筛脚本（finance-filter 前置层）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--init", action="store_true", help="仅生成默认配置后退出")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.init:
        return

    collected, source_status, profile = run(config)

    os.makedirs(REPORT_DIR, exist_ok=True)
    date_tag = dt.datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(REPORT_DIR, f"{date_tag}.md")
    payload_path = os.path.join(REPORT_DIR, f"{date_tag}.payload.json")

    report = build_report(collected, source_status, profile)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 喂给降噪器 / 下游 skill 的结构化 payload（对应 SKILL「前置降噪层 payload」）
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "source_status": [{"name": n, "tier": t, "status": s} for n, t, s in source_status],
        "items": collected,
        "note": "机器初筛结果，待 finance-filter 降噪器做 Layer 0 预检与最终判定；不含买卖建议。",
    }
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 终端摘要
    print("=" * 60)
    print(f"报告已生成：{report_path}")
    print(f"Payload：    {payload_path}")
    print("-" * 60)
    cand = len([c for c in collected if c["signal"] == "🟡→候选🔴"])
    watch = len([c for c in collected if c["signal"] == "🟡"])
    print(f"相关条目 {len(collected)} 条 | 候选Must Act {cand} | Watch {watch}")
    for name, tier, status in source_status:
        print(f"  - [{tier}] {name}: {status}")
    print("=" * 60)
    print(DISCLAIMER.replace("> ", ""))


if __name__ == "__main__":
    main()
