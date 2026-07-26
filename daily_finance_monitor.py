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
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "finance_monitor_config.json")
REPORT_DIR = os.path.join(HERE, "reports")

# 固定按北京时间(UTC+8)计算日期/时间窗口。
# 原因：GitHub Actions runner 默认 UTC，直接用 datetime.now() 会把凌晨触发的报告
# 误标成前一天（如北京 07-01 00:54 → UTC 06-30 → 报告写成 06-30）。
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def now_local():
    """返回北京时间的 naive datetime（去掉 tzinfo，便于与解析出的时间统一比较）。"""
    return dt.datetime.now(LOCAL_TZ).replace(tzinfo=None)

# ---------------------------------------------------------------------------
# 默认配置：信源分级参考 source-credibility.md，关键词为示例，请按自己的持仓改写
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_DATA = {
    "_说明": "请按你的真实持仓/关注修改 watchlist；sources 可增删 RSS 源，tier 标信源等级。",
    "profile": {
        "投资周期": "中期",  # 长期 / 中期 / 短期，对应 SKILL Layer 4 时间尺度匹配
        "时间窗口小时": 30,   # 新闻只保留最近 N 小时（默认近 30 小时，覆盖昨夜到今早）
        "个股直接检索": True,  # True 时为每个 direct 持仓额外抓「按名称检索」的直接资讯
        "官方信披_巨潮公告": True,  # True 时抓巨潮公告（第一层法定信披，T1，最权威）
        "信披回看小时": 168,  # 公告回看窗（默认 7 天），信披时效不同于新闻
        "数据终端_akshare": True  # True 且已 pip install akshare 且持仓带 code 时，抓东财个股新闻(T2)
    },
    "watchlist": {
        # 直接相关：你的具体持仓。可写字符串，或写 {"name","code"} 以启用 akshare 个股源
        "direct": [
            {"name": "贵州茅台", "code": "600519"},
            {"name": "宁德时代", "code": "300750"},
            "沪深300",
            "纳斯达克"
        ],
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

# ---------------------------------------------------------------------------
# 个股/持仓「直接资讯」检索源模板（多 provider，互为冗余/兜底）
# ---------------------------------------------------------------------------
# 综合 RSS 源天然产出宏观/主题类新闻，很难命中「贵州茅台」这类具体持仓。
# 这里为每个 direct 持仓分别生成「按名称检索」的新闻源，专抓与该持仓直接相关的资讯。
# 采用多个检索引擎互为兜底：
#   · Google News —— 覆盖全、时效好，但国内直连常失败（GitHub Actions 海外 runner 正常）；
#   · Bing 新闻   —— 国内可直连，作为 Google 失败时的兜底/补充（需 Windows UA 才稳定返回中文结果）。
# 任一 provider 抓取失败都会被优雅标注「信源静默降级」，不影响其它 provider 与综合源。
# 两个引擎的重复报道由主流程按链接/标题去重。
# {q} 会被替换为 URL 编码后的持仓名。
_WIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
SEARCH_PROVIDERS = [
    {
        "name": "Google News检索",
        "url": "https://news.google.com/rss/search?q={q}%20when:2d&hl=zh-CN&gl=CN&ceid=CN:zh",
        "tier": "T3",
        "ua": None,
    },
    {
        "name": "Bing新闻检索",
        "url": "https://www.bing.com/news/search?q={q}&format=rss",
        "tier": "T3",
        "ua": _WIN_UA,
    },
]

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
def fetch(url, timeout=15, user_agent=None):
    headers = dict(REQUEST_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent  # 某些源（如 Bing）对 UA 敏感，需按源指定
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# 巨潮资讯网（证监会指定信披平台）公告查询接口——第一层「法定/官方信披」最权威来源。
# 返回公告原文列表，可按持仓名(searchkey)检索，免鉴权、JSON 格式。
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"


def fetch_cninfo(holding, timeout=15, page_size=20):
    """按持仓名从巨潮查最新公告，返回与 parse_feed 同构的 entries（title/link/summary/published）。"""
    data = urllib.parse.urlencode({
        "pageNum": 1, "pageSize": page_size, "column": "szse",
        "tabName": "fulltext", "searchkey": holding,
        "sortName": "time", "sortType": "desc", "isHLtitle": "false",
    }).encode()
    headers = dict(REQUEST_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Accept"] = "application/json"
    req = urllib.request.Request(CNINFO_QUERY_URL, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        j = json.loads(resp.read())
    entries = []
    for a in (j.get("announcements") or []):
        title = _strip_tags(a.get("announcementTitle") or "")
        sec = _strip_tags(a.get("secName") or "")
        adj = a.get("adjunctUrl") or ""
        link = ("http://static.cninfo.com.cn/" + adj) if adj else ""
        ts = a.get("announcementTime")
        pub = (dt.datetime.fromtimestamp(ts / 1000, LOCAL_TZ).replace(tzinfo=None)
               if ts else None)
        entries.append({
            "title": f"[公告] {sec} {title}".strip(),
            "link": link,
            "summary": title,
            "published": pub,
        })
    return entries


# 第二层：专业数据终端（T2）——可选，需 `pip install akshare`。
# akshare 是免费开源的程序化取数库，这里用它拉「东方财富个股新闻」；
# 未安装则整段静默跳过，不影响其它源（守卫见 has_akshare / build_search_sources）。
def has_akshare():
    """轻量探测 akshare 是否可用（不真正 import，避免无谓的重加载开销）。"""
    return importlib.util.find_spec("akshare") is not None


def _query_a_stock_codes(names):
    """实际查询：拉取全量 A 股名称->代码表并匹配。可能较慢（数千行），由调用方加超时守护。"""
    import akshare as ak  # 延迟导入：仅在需要补全代码时才加载重库
    df = ak.stock_info_a_code_name()
    cols = list(df.columns)
    # 兼容不同 akshare 版本的列名（常见为 code/name）
    code_col = "code" if "code" in cols else cols[0]
    name_col = "name" if "name" in cols else (cols[1] if len(cols) > 1 else cols[0])
    wanted = set(names)
    # 向量化过滤，避免对数千行逐行 iterrows（更快）
    hit = df[df[name_col].astype(str).str.strip().isin(wanted)]
    out = {}
    for _, row in hit.iterrows():
        nm = str(row[name_col]).strip()
        if nm not in out:
            out[nm] = str(row[code_col]).strip().zfill(6)
    return out


def _resolve_a_stock_codes(names, timeout=30):
    """用 akshare 查 A 股「名称 -> 6 位代码」映射，仅返回能精确匹配到的项。

    akshare 的 stock_info_a_code_name() 会拉取全量 A 股列表，且内部请求无超时，
    网络慢/接口阻塞时会长时间挂起。这里用后台线程 + 超时守护，超时即放弃（返回空），
    避免整个每日任务卡死。未装 akshare / 接口异常同样返回空 dict（不阻塞主流程）。"""
    if not names:
        return {}
    result = {"data": {}, "error": None}

    def worker():
        try:
            result["data"] = _query_a_stock_codes(names)
        except Exception as ex:  # noqa: BLE001  网络/接口/依赖异常统一兜底
            result["error"] = ex

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[code补全] 查询超时（>{timeout}s），本次跳过自动补全（不影响其它信源）。"
              f"如需补全可稍后重跑，或在配置里为 direct 持仓手动填 code。")
        return {}
    if result["error"] is not None:
        print(f"[code补全] 查询异常：{result['error']}（本次跳过，不影响其它信源）。")
        return {}
    return result["data"]


def enrich_direct_codes(config, config_path):
    """首次运行 / 存在缺 code 的直接持仓时，自动查 A 股代码并回写配置，
    以便启用 akshare 东财个股新闻源(T2)。

    - 仅当 profile.数据终端_akshare 为 True 且 akshare 可用时执行；
    - 指数或无法匹配的名称（如「沪深300」「纳斯达克」）保持原样，不阻塞主流程；
    - 有补全才写回，并把原文件备份为 *.bak；
    - 就地更新内存中的 config，使本次运行即可启用 akshare 个股源（无需二次运行）。
    返回补全列表 [(name, code)]。"""
    profile = config.get("profile", {})
    if not profile.get("数据终端_akshare", False):
        return []  # 未开启数据终端，无需补全

    watchlist = config.get("watchlist", {})
    direct = watchlist.get("direct", [])

    # 找出缺 code 的直接持仓名称（纯字符串，或 dict 但 code 为空）
    missing = []
    for h in direct:
        if isinstance(h, dict):
            name = (h.get("name") or "").strip()
            if name and not (h.get("code") or "").strip():
                missing.append(name)
        elif isinstance(h, str) and h.strip():
            missing.append(h.strip())
    if not missing:
        return []  # 全部已带 code，无需补全

    if not has_akshare():
        print("[code补全] 未安装 akshare，跳过自动补全（安装后重跑即可启用个股源）。")
        return []

    timeout = int(profile.get("补全超时秒", 30))  # 查询超时守护，避免卡死每日任务
    print(f"[code补全] 正在为 {len(missing)} 个直接持仓查询股票代码（超时 {timeout}s）：{', '.join(missing)}")
    name2code = _resolve_a_stock_codes(missing, timeout=timeout)
    if not name2code:
        print("[code补全] 未查到可匹配的 A 股代码（可能为指数/网络异常），保持原配置。")
        return []

    # 回写 direct：命中的转成 {name, code}，未命中的保持原样
    new_direct, filled = [], []
    for h in direct:
        if isinstance(h, dict):
            name = (h.get("name") or "").strip()
            code = (h.get("code") or "").strip()
        else:
            name, code = str(h).strip(), ""
        if not code and name in name2code:
            code = name2code[name]
            filled.append((name, code))
        if code:
            new_direct.append({"name": name, "code": code})
        else:
            new_direct.append(h if isinstance(h, dict) else name)

    if not filled:
        print("[code补全] 无新增可补全代码，保持原配置。")
        return []

    watchlist["direct"] = new_direct
    config["watchlist"] = watchlist
    try:
        shutil.copyfile(config_path, config_path + ".bak")  # 备份原配置
    except OSError:
        pass
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"[code补全] 已补全 {len(filled)} 个持仓代码并回写 {os.path.basename(config_path)}："
          + ", ".join(f"{n}={c}" for n, c in filled))
    return filled


def fetch_akshare(code):
    """用 akshare 拉东财个股新闻，返回与 parse_feed 同构的 entries。code 为股票代码（如 600519）。"""
    import akshare as ak  # 延迟导入：只有真正用到该源时才加载重库
    df = ak.stock_news_em(symbol=str(code))
    entries = []
    for _, row in df.iterrows():
        title = str(row.get("新闻标题", "") or "").strip()
        link = str(row.get("新闻链接", "") or "").strip()
        summary = str(row.get("新闻内容", "") or "").strip()
        pub = parse_pubdate(str(row.get("发布时间", "") or ""))
        if not title:
            continue
        entries.append({
            "title": title,
            "link": link,
            "summary": summary[:200],
            "published": pub,
        })
    return entries


def _text(elem):
    return html.unescape((elem.text or "").strip()) if elem is not None else ""


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def parse_pubdate(s):
    """尽量把各种时间字符串解析为 datetime（带时区则转 naive 本地近似）。失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    # Google News 等用 "GMT" 结尾（%Z 解析常不带时区信息），统一改成 +0000 以正确换算
    if s.endswith(" GMT"):
        s = s[:-4] + " +0000"
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
                d = d.astimezone(LOCAL_TZ).replace(tzinfo=None)  # 统一转北京时间
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
    return (now_local() - published) <= dt.timedelta(hours=hours)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def direct_holdings(watchlist):
    """把 watchlist.direct 归一化为 [{name, code}]。
    兼容两种写法：字符串 "贵州茅台"，或对象 {"name":"贵州茅台","code":"600519"}。"""
    out = []
    for h in watchlist.get("direct", []):
        if isinstance(h, dict):
            name = (h.get("name") or "").strip()
            code = (h.get("code") or "").strip()
        else:
            name, code = str(h).strip(), ""
        if name:
            out.append({"name": name, "code": code})
    return out


def direct_names(watchlist):
    """仅取直接持仓的名称列表（供关键词匹配用，兼容带 code 的对象写法）。"""
    return [h["name"] for h in direct_holdings(watchlist)]


def build_search_sources(watchlist, official_disclosure=True, use_akshare=False):
    """为每个 direct 持仓生成「按名称检索」的直接资讯源（分层对应 SKILL T1–T4）。
    - 巨潮公告（T1，法定信披，第一层地基）：official_disclosure=True 时加入；
    - 东财个股新闻（T2，数据终端，第二层）：use_akshare=True 且已装 akshare 且持仓带 code 时加入；
    - Google/Bing 新闻检索（T3，财经媒体，第三层）：互为冗余/兜底。
    返回的源带 kind：'cninfo' 走公告接口，'akshare' 走 akshare，其余走 RSS。
    forced_match 用于把命中该持仓的条目直接判为「直接相关」。"""
    sources = []
    ak_ready = use_akshare and has_akshare()
    for h in direct_holdings(watchlist):
        holding, code = h["name"], h["code"]
        # 第一层：法定/官方信披（最权威）——巨潮公告
        if official_disclosure:
            sources.append({
                "name": f"巨潮公告·{holding}",
                "kind": "cninfo",
                "holding": holding,
                "tier": "T1",
                "forced_match": holding,
            })
        # 第二层：专业数据终端（可选）——东财个股新闻，需 akshare + 股票代码
        if ak_ready and code:
            sources.append({
                "name": f"东财个股新闻·{holding}",
                "kind": "akshare",
                "holding": holding,
                "code": code,
                "tier": "T2",
                "forced_match": holding,
            })
        # 第三层：财经媒体检索（Google + Bing 双引擎）
        q = urllib.parse.quote(holding)
        for prov in SEARCH_PROVIDERS:
            sources.append({
                "name": f"{prov['name']}·{holding}",
                "url": prov["url"].format(q=q),
                "tier": prov["tier"],
                "ua": prov.get("ua"),
                "forced_match": holding,
            })
    return sources


def process_entry(e, name, tier, watchlist, emotion_words, hours, forced_match=None):
    """对单条 feed 记录做初筛，返回 item dict；不相关或超窗返回 None。
    forced_match：来自持仓检索源时传入持仓名——只要该名出现在文本中即判「直接相关」。"""
    text = f"{e['title']} {e['summary']}"
    if not within_window(e["published"], hours):
        return None
    if forced_match and forced_match in text:
        rel, rel_kw = "直接", forced_match
    else:
        rel, rel_kw = relevance(text, watchlist)
    if rel == "无关":
        return None  # 前置降噪：明显无关直接剔除，帮你少看
    emo, emo_hits = emotion(text, emotion_words)
    sig, impact = preliminary_signal(tier, rel, emo)
    return {
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
    }


def run(config):
    profile = config.get("profile", {})
    watchlist = config.get("watchlist", {})
    emotion_words = config.get("emotion_words", [])
    hours = int(profile.get("时间窗口小时", 30))

    # 归一化：direct 可能是字符串或 {name,code}；关键词匹配只用 name
    match_watchlist = dict(watchlist)
    match_watchlist["direct"] = direct_names(watchlist)

    collected = []        # 通过初筛的条目
    source_status = []    # 每个信源的抓取状态（数据质量守门）

    # 综合源 + 持仓直接检索源（后者专抓具体持仓的直接资讯）
    sources = list(config.get("sources", []))
    if profile.get("个股直接检索", True):
        sources += build_search_sources(
            watchlist,
            official_disclosure=profile.get("官方信披_巨潮公告", True),
            use_akshare=profile.get("数据终端_akshare", False),
        )

    seen_links = set()    # 跨源去重（检索源与综合源常有重复报道）

    for src in sources:
        name, url, tier = src.get("name"), src.get("url"), src.get("tier", "T?")
        forced_match = src.get("forced_match")
        kind = src.get("kind", "rss")
        # 法定信披时效性不同于新闻：公告常为数天前发布，给更长回看窗（默认 7 天），
        # 避免重要公告被 30 小时新闻窗误滤。
        src_hours = int(profile.get("信披回看小时", 168)) if kind == "cninfo" else hours
        try:
            if kind == "cninfo":
                entries = fetch_cninfo(src["holding"])
            elif kind == "akshare":
                entries = fetch_akshare(src["code"])
            else:
                raw = fetch(url, user_agent=src.get("ua"))
                entries = parse_feed(raw)
            if not entries:
                source_status.append((name, tier, "⚠️ 解析为空（信源静默降级，待验证）"))
                continue
            kept = 0
            for e in entries:
                item = process_entry(e, name, tier, match_watchlist, emotion_words, src_hours, forced_match)
                if item is None:
                    continue
                key = item["link"] or item["title"]
                if key in seen_links:
                    continue
                seen_links.add(key)
                collected.append(item)
                kept += 1
            source_status.append((name, tier, f"✅ 抓取 {len(entries)} 条 / 相关 {kept} 条"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError,
                ImportError, KeyError) as ex:
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


# 相关性优先级：直接持仓 > 间接(行业) > 主题(宏观)，用于 Watch 排序取最相关的先看
_REL_ORDER = {"直接": 0, "间接": 1, "主题": 2}


def _dedup_watch(watch, per_key, limit):
    """对 Watch 做降噪：先按相关性+Impact 排序，再对每个命中关键词限流（压掉同一标的的行情流水），
    最后全局截断到 limit。返回 (展示列表, 被折叠条数)。"""
    ordered = sorted(watch, key=lambda c: (_REL_ORDER.get(c["relevance"], 9), -c["impact"]))
    counter, kept = {}, []
    for c in ordered:
        key = c.get("match") or c["relevance"]
        if counter.get(key, 0) >= per_key:
            continue  # 同一关键词只保留前 per_key 条，其余视为重复流水折叠
        counter[key] = counter.get(key, 0) + 1
        kept.append(c)
    shown = kept[:limit]
    hidden = len(watch) - len(shown)
    return shown, hidden


def build_report(collected, source_status, profile):
    """晨间降噪报告：只呈现"需要少看"的核心信号，全量数据在同名 payload.json。
    对应 output-templates.md §6 批量过滤 + §7 裁剪规则：只展开 Must Act，Watch 汇总限流，Noise 一句话。"""
    today = now_local().strftime("%Y-%m-%d %H:%M")
    # 报告展示限流（可在 config.profile 覆盖，默认已足够精简）
    watch_limit = int(profile.get("报告Watch上限", 15))
    per_key = int(profile.get("报告每关键词上限", 3))

    candidates = [c for c in collected if c["signal"] == "🟡→候选🔴"]
    watch = [c for c in collected if c["signal"] == "🟡"]
    noise_cnt = len([c for c in collected if c["signal"] == "⚪"])
    total = len(collected)
    noise_rate = f"{(noise_cnt / total * 100):.0f}%" if total else "0%"

    # 信源状态先算统计，正文只点名异常源（数据质量守门保留但不铺陈）
    fails = [(n, t, s) for n, t, s in source_status if s.startswith(("❌", "⚠️"))]
    ok_cnt = len(source_status) - len(fails)

    lines = []
    lines.append(f"# 每日金融信息初筛报告 · {today}")
    lines.append("")
    lines.append(f"> 投资周期 {profile.get('投资周期', '未设置')} · 时间窗口 {profile.get('时间窗口小时', 30)}h "
                 f"· 信源 {ok_cnt} 正常 / {len(fails)} 降级")
    lines.append("")

    # 今日速览：一眼看完三档分布与噪音率
    lines.append("## 今日速览")
    lines.append("")
    lines.append(f"- 共筛出 **{total}** 条相关：🔴 候选 {len(candidates)} · 🟡 Watch {len(watch)} · ⚪ Noise {noise_cnt}")
    lines.append(f"- 噪音率：{noise_rate}（越高说明当日流水噪音越多）")
    lines.append(f"- 全部条目见同日 `*.payload.json`；本报告仅呈现需优先关注的信号。")
    lines.append("")

    # 🔴 候选 Must Act —— 唯一需要展开逐条分析的部分
    lines.append(f"## 🔴 候选 Must Act（优先用降噪器复核 · {len(candidates)} 条）")
    lines.append("")
    if candidates:
        for c in candidates:
            extra = ""
            if c["emotion"] != "🟢":
                extra = f" · ⚠️ 情绪词「{', '.join(c['emotion_hits'])}」需核一手来源"
            lines.append(f"- **{md_link(c['title'], c.get('link'))}**")
            lines.append(f"  - {c['source']}（{c['tier']}）· 命中「{c['match']}」· {c['published']}{extra}")
        lines.append("")
        lines.append("> 下一步：把以上条目交给 finance-filter 降噪器做 Layer 0 预检 + 五层判定。")
    else:
        lines.append("今日无命中直接持仓且高可信信源的候选。")
    lines.append("")

    # 🟡 Watch —— 排序 + 关键词限流后只列 Top N，每条一句话
    watch_shown, watch_hidden = _dedup_watch(watch, per_key, watch_limit)
    lines.append(f"## 🟡 Watch（纳入观察 · 共 {len(watch)} 条，下列 {len(watch_shown)} 条）")
    lines.append("")
    if watch_shown:
        for c in watch_shown:
            lines.append(f"- [{c['tier']}·{c['relevance']}·{c.get('match') or '-'}] {md_link(c['title'][:50], c.get('link'))}")
        if watch_hidden > 0:
            lines.append("")
            lines.append(f"> 另有 {watch_hidden} 条 Watch（同标的行情流水/低优先）已折叠，全部见 payload。")
    else:
        lines.append("（无）")
    lines.append("")

    # ⚪ Noise —— 一句话
    lines.append(f"## ⚪ Noise：{noise_cnt} 条低优先（相关但信源/情绪偏弱）已折叠，见 payload。")
    lines.append("")

    # 数据质量守门：只在有异常时点名（SKILL 协同角色 ④）
    if fails:
        lines.append("## ⚠️ 信源降级（数据质量守门）")
        lines.append("")
        for name, tier, status in fails:
            lines.append(f"- [{tier}] {name}：{status}")
        lines.append("")
    else:
        lines.append(f"> 数据质量守门：全部 {ok_cnt} 个信源抓取正常。")
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

    # 首次运行 / 直接持仓缺 code 时：自动补全 A 股代码并回写配置，启用 akshare 个股源(T2)
    enrich_direct_codes(config, args.config)

    collected, source_status, profile = run(config)

    os.makedirs(REPORT_DIR, exist_ok=True)
    date_tag = now_local().strftime("%Y-%m-%d")
    report_path = os.path.join(REPORT_DIR, f"{date_tag}.md")
    payload_path = os.path.join(REPORT_DIR, f"{date_tag}.payload.json")

    report = build_report(collected, source_status, profile)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 喂给降噪器 / 下游 skill 的结构化 payload（对应 SKILL「前置降噪层 payload」）
    payload = {
        "generated_at": now_local().isoformat(timespec="seconds"),
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
