#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
站点构建脚本 · 把 reports/*.md 渲染为可发布到 GitHub Pages 的静态站点。

流程：
  扫描 reports/ 下所有 YYYY-MM-DD.md
  -> 逐篇 Markdown 转 HTML（优先用 markdown 库，缺失时退化为 <pre> 原文）
  -> 套统一模板（含返回首页、暗色友好样式）
  -> 生成 index.html（按日期倒序列出全部报告，置顶最新一篇摘要）
  -> 全部输出到 site/ 目录（GitHub Actions 会把它作为 Pages artifact 发布）

依赖：标准库即可运行；若安装了 markdown（pip install markdown）则表格/标题渲染更佳。

用法：
  python3 build_site.py                 # 输出到 ./site
  python3 build_site.py --out public     # 自定义输出目录
"""

import argparse
import datetime as dt
import glob
import html as html_lib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Markdown 库可选；GitHub Actions 里会 pip install markdown 获得最佳表格渲染
try:
    import markdown as _md  # type: ignore

    def md_to_html(text):
        return _md.markdown(
            text,
            extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        )

    HAS_MD = True
except Exception:  # noqa: BLE001
    HAS_MD = False

    def md_to_html(text):
        # 退化方案：转义后用 <pre> 保留原文，并把链接转成可点击 <a>，至少保证可读可跳转
        esc = html_lib.escape(text)
        # Markdown 链接 [文字](url) -> <a>文字</a>
        esc = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            r'<a href="\2" target="_blank" rel="noopener">\1</a>',
            esc,
        )
        # 剩余裸链接 -> <a>
        esc = re.sub(
            r'(?<!")(?<!>)(https?://[^\s<)\]]+)',
            r'<a href="\1" target="_blank" rel="noopener">\1</a>',
            esc,
        )
        return "<pre class='raw'>" + esc + "</pre>"


PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    max-width: 920px; margin: 0 auto; padding: 24px 18px 64px;
    line-height: 1.7; color: #24292f; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6edf3; background: #0d1117; }}
    a {{ color: #58a6ff; }}
    table th {{ background: #161b22 !important; }}
    table td, table th {{ border-color: #30363d !important; }}
    .topbar, .card {{ background: #161b22 !important; border-color: #30363d !important; }}
    blockquote {{ background: #161b22 !important; border-color: #f0883e !important; }}
  }}
  .topbar {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; margin-bottom: 24px; border: 1px solid #d0d7de;
    border-radius: 10px; background: #f6f8fa; font-size: 14px;
  }}
  .topbar a {{ text-decoration: none; font-weight: 600; }}
  h1 {{ font-size: 1.6rem; border-bottom: 1px solid #d0d7de; padding-bottom: .3em; }}
  h2 {{ font-size: 1.25rem; margin-top: 1.8em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 14px; display: block; overflow-x: auto; }}
  table th, table td {{ border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }}
  table th {{ background: #f6f8fa; }}
  blockquote {{
    margin: 1em 0; padding: 10px 16px; background: #fff8e6;
    border-left: 4px solid #d97706; border-radius: 0 8px 8px 0;
  }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  pre {{ background: #f6f8fa; padding: 14px; border-radius: 8px; overflow-x: auto; }}
  pre.raw {{ white-space: pre-wrap; word-break: break-word; }}
  .card {{
    display: block; padding: 14px 18px; margin: 10px 0; text-decoration: none;
    border: 1px solid #d0d7de; border-radius: 10px; color: inherit; background: #fff;
    transition: transform .08s ease, box-shadow .08s ease;
  }}
  .card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 14px rgba(0,0,0,.08); }}
  .card .date {{ font-weight: 700; font-size: 1.05rem; }}
  .card .badge {{ float: right; font-size: 12px; color: #57606a; }}
    .latest {{ border-color: #d97706; }}
  .foot {{ margin-top: 48px; font-size: 12px; color: #8b949e; text-align: center; }}
  .fulllink {{
    display: inline-block; margin: 6px 0 22px; padding: 10px 16px;
    border: 1px solid #d97706; border-radius: 8px; background: #fff8e6;
    color: #b45309; text-decoration: none; font-weight: 600;
  }}
  .fulllink:hover {{ background: #ffefc6; }}
  .backlink {{ font-weight: 600; }}
  .grp {{ margin-top: 1.8em; }}
  .tag {{
    display: inline-block; padding: 1px 7px; border-radius: 6px;
    font-size: 12px; background: #eaeef2; color: #57606a; white-space: nowrap;
  }}
  td.t {{ min-width: 260px; }}
  @media (prefers-color-scheme: dark) {{
    .fulllink {{ background: #2d2410 !important; border-color: #f0883e !important; color: #f0b072 !important; }}
    .fulllink:hover {{ background: #3a2e12 !important; }}
    .tag {{ background: #21262d !important; color: #adbac7 !important; }}
  }}
</style>
</head>
<body>
<div class="topbar">
  <a href="./index.html">📊 每日金融初筛</a>
  <span>{subtitle}</span>
</div>
{body}
<div class="foot">由 daily_finance_monitor.py + build_site.py 自动生成 · finance-filter 前置降噪层</div>
</body>
</html>
"""


def list_reports():
    """返回 [(date_str, md_path, payload_path|None)]，按日期倒序。

    payload_path 为同日的 *.payload.json（若存在）——完整条目（含报告里折叠的部分）来源于它。
    """
    out = []
    for p in glob.glob(os.path.join(REPORT_DIR, "*.md")):
        # 跳过 .md.bak 等备份文件，避免误当成报告页
        if not p.endswith(".md"):
            continue
        m = DATE_RE.search(os.path.basename(p))
        if m:
            date_str = m.group(1)
            payload = os.path.join(REPORT_DIR, f"{date_str}.payload.json")
            out.append((date_str, p, payload if os.path.exists(payload) else None))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def render_report_page(date_str, md_path, out_dir, has_full=False):
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    body = md_to_html(text)

    if has_full:
        full_href = f"./{date_str}.full.html"
        # 顶部醒目入口：报告里被折叠、只写“见 payload”的条目，全部可在完整页点击查看
        banner = (
            f'<a class="fulllink" href="{full_href}">'
            f'📦 查看全部条目（含报告中已折叠的 Watch / Noise 明细）→</a>'
        )
        body = banner + "\n" + body
        # 正文里出现的 “payload” 文字（如“全部见 payload”）就地变成可点击跳转链接
        body = re.sub(
            r"payload(\.json)?",
            lambda m: f'<a href="{full_href}">完整条目页</a>',
            body,
        )

    page = PAGE_TMPL.format(
        title=f"金融初筛 · {date_str}",
        subtitle=f"报告日期 {date_str}",
        body=body,
    )
    out_path = os.path.join(out_dir, f"{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return f"{date_str}.html"


# 完整条目页：把 payload.json 里“全部”条目按信号分档渲染成可点击跳转的表格，
# 补齐报告中被折叠（Watch 限流 / Noise 汇总）而在 GitHub Pages 上原本看不到的内容。
_SIG_GROUPS = [
    ("🟡→候选🔴", "🔴 候选 Must Act"),
    ("🟡", "🟡 Watch（纳入观察）"),
    ("⚪", "⚪ Noise（低优先）"),
]


def _item_link(title, link):
    """标题渲染为可点击链接（无链接则纯文本），并做 HTML 转义。"""
    safe = html_lib.escape(title or "")
    if link:
        return (f'<a href="{html_lib.escape(link, quote=True)}" '
                f'target="_blank" rel="noopener">{safe}</a>')
    return safe


def _items_table(items):
    rows = [
        "<table>",
        "<tr><th>#</th><th>信源(等级)</th><th>相关性</th><th>命中</th>"
        "<th>情绪</th><th>时间</th><th class='t'>标题（点击跳转原文）</th></tr>",
    ]
    for i, it in enumerate(items, 1):
        emo = it.get("emotion", "")
        hits = it.get("emotion_hits") or []
        if hits:
            emo = f"{emo} {html_lib.escape('/'.join(hits))}"
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{html_lib.escape(str(it.get('source', '')))}"
            f"<br><span class='tag'>{html_lib.escape(str(it.get('tier', '')))}</span></td>"
            f"<td>{html_lib.escape(str(it.get('relevance', '')))}</td>"
            f"<td>{html_lib.escape(str(it.get('match') or '-'))}</td>"
            f"<td>{emo}</td>"
            f"<td>{html_lib.escape(str(it.get('published', '')))}</td>"
            f"<td class='t'>{_item_link(it.get('title'), it.get('link'))}</td>"
            "</tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def render_full_page(date_str, payload_path, out_dir):
    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", []) or []
    source_status = payload.get("source_status", []) or []

    parts = [f"<h1>完整条目 · {date_str}</h1>"]
    parts.append(
        f'<p><a class="backlink" href="./{date_str}.html">← 返回当日初筛报告</a></p>'
    )
    parts.append(
        f"<p>共 <b>{len(items)}</b> 条相关条目（报告中被折叠的部分也在此完整列出，均可点击跳转原文）。</p>"
    )

    for sig, label in _SIG_GROUPS:
        group = [it for it in items if it.get("signal") == sig]
        if not group:
            continue
        parts.append(f'<div class="grp"><h2>{label}（{len(group)} 条）</h2>')
        parts.append(_items_table(group))
        parts.append("</div>")

    # 其余未归入上述信号档位的条目（兜底，保证“全部内容”都能看到）
    known = {s for s, _ in _SIG_GROUPS}
    others = [it for it in items if it.get("signal") not in known]
    if others:
        parts.append(f'<div class="grp"><h2>其他（{len(others)} 条）</h2>')
        parts.append(_items_table(others))
        parts.append("</div>")

    # 信源抓取状态（数据质量守门）：完整呈现每个源的成功/降级情况
    if source_status:
        parts.append('<div class="grp"><h2>信源抓取状态</h2>')
        rows = ["<table>", "<tr><th>信源</th><th>等级</th><th>状态</th></tr>"]
        for s in source_status:
            rows.append(
                "<tr>"
                f"<td>{html_lib.escape(str(s.get('name', '')))}</td>"
                f"<td><span class='tag'>{html_lib.escape(str(s.get('tier', '')))}</span></td>"
                f"<td>{html_lib.escape(str(s.get('status', '')))}</td>"
                "</tr>"
            )
        rows.append("</table>")
        parts.append("\n".join(rows))
        parts.append("</div>")

    page = PAGE_TMPL.format(
        title=f"完整条目 · {date_str}",
        subtitle=f"完整条目 · {date_str}",
        body="\n".join(parts),
    )
    out_path = os.path.join(out_dir, f"{date_str}.full.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return f"{date_str}.full.html"


def first_overview_line(md_path):
    """从报告里抽一句概览（“共筛出 N 条相关”）作为卡片摘要。"""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            for line in f:
                if "共筛出" in line:
                    return line.strip().lstrip("# ").strip()
    except OSError:
        pass
    return "查看当日初筛详情"


def build_index(reports, out_dir):
    parts = ["<h1>每日金融信息初筛报告</h1>"]
    if not reports:
        parts.append("<p>暂无报告。请先运行 <code>python3 daily_finance_monitor.py</code> 生成。</p>")
    else:
        latest_date = reports[0][0]
        parts.append(f"<p>共 {len(reports)} 份报告 · 最新更新 {latest_date}</p>")
        for i, (date_str, md_path, payload_path) in enumerate(reports):
            cls = "card latest" if i == 0 else "card"
            badge = "最新" if i == 0 else ""
            summary = first_overview_line(md_path)
            full_note = (
                f' · <a href="./{date_str}.full.html">全部条目</a>' if payload_path else ""
            )
            parts.append(
                f'<a class="{cls}" href="./{date_str}.html">'
                f'<span class="badge">{badge}</span>'
                f'<span class="date">{date_str}</span><br>'
                f'<span>{html_lib.escape(summary)}</span></a>'
                f'<div style="margin:-4px 0 10px;font-size:13px;">'
                f'<a href="./{date_str}.html">查看报告</a>{full_note}</div>'
            )
    page = PAGE_TMPL.format(
        title="每日金融信息初筛报告",
        subtitle=f"更新于 {dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}",
        body="\n".join(parts),
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    parser = argparse.ArgumentParser(description="把 reports/*.md 渲染为 GitHub Pages 静态站点")
    parser.add_argument("--out", default=os.path.join(HERE, "site"), help="输出目录（默认 ./site）")
    args = parser.parse_args()

    out_dir = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    os.makedirs(out_dir, exist_ok=True)

    reports = list_reports()
    full_cnt = 0
    for date_str, md_path, payload_path in reports:
        has_full = payload_path is not None
        render_report_page(date_str, md_path, out_dir, has_full=has_full)
        if has_full:
            render_full_page(date_str, payload_path, out_dir)
            full_cnt += 1
    build_index(reports, out_dir)

    # 防止 GitHub Pages 走 Jekyll 处理（避免下划线开头文件被忽略等问题）
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    print(f"[build] markdown 渲染器：{'markdown 库' if HAS_MD else '退化 <pre>（建议 pip install markdown）'}")
    print(f"[build] 已生成 {len(reports)} 篇报告页 + {full_cnt} 篇完整条目页 + index.html -> {out_dir}")


if __name__ == "__main__":
    main()
