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
    """返回 [(date_str, md_path)]，按日期倒序。"""
    out = []
    for p in glob.glob(os.path.join(REPORT_DIR, "*.md")):
        m = DATE_RE.search(os.path.basename(p))
        if m:
            out.append((m.group(1), p))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def render_report_page(date_str, md_path, out_dir):
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    body = md_to_html(text)
    page = PAGE_TMPL.format(
        title=f"金融初筛 · {date_str}",
        subtitle=f"报告日期 {date_str}",
        body=body,
    )
    out_path = os.path.join(out_dir, f"{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return f"{date_str}.html"


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
        latest_date, latest_path = reports[0]
        parts.append(f"<p>共 {len(reports)} 份报告 · 最新更新 {latest_date}</p>")
        for i, (date_str, md_path) in enumerate(reports):
            cls = "card latest" if i == 0 else "card"
            badge = "最新" if i == 0 else ""
            summary = first_overview_line(md_path)
            parts.append(
                f'<a class="{cls}" href="./{date_str}.html">'
                f'<span class="badge">{badge}</span>'
                f'<span class="date">{date_str}</span><br>'
                f'<span>{html_lib.escape(summary)}</span></a>'
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
    for date_str, md_path in reports:
        render_report_page(date_str, md_path, out_dir)
    build_index(reports, out_dir)

    # 防止 GitHub Pages 走 Jekyll 处理（避免下划线开头文件被忽略等问题）
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    print(f"[build] markdown 渲染器：{'markdown 库' if HAS_MD else '退化 <pre>（建议 pip install markdown）'}")
    print(f"[build] 已生成 {len(reports)} 篇报告页 + index.html -> {out_dir}")


if __name__ == "__main__":
    main()
