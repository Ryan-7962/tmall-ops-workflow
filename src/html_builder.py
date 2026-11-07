#!/usr/bin/env python3
"""将既有看板模板与数据库导出的 JS 数据打包为单文件 HTML。"""
import os
import re
from pathlib import Path


def build_online(dashboard):
    dashboard = Path(dashboard)
    online = dashboard / "dashboard-online.html"
    previous = online.read_text(encoding="utf-8")
    inline_scripts = re.findall(r"<script>([\s\S]*?)</script>", previous)
    if not inline_scripts:
        raise ValueError("旧单文件版中未找到内联 ECharts")
    echarts = max(inline_scripts, key=len)
    html = (dashboard / "index.html").read_text(encoding="utf-8")
    cdn_block = re.compile(
        r'<script src="https://g\.alicdn\.com/code/lib/echarts/5\.4\.3/echarts\.min\.js"></script>'
        r'\s*<script>[\s\S]*?</script>'
    )
    html, count = cdn_block.subn(lambda _: "<script>\n" + echarts + "\n</script>", html, count=1)
    if count != 1:
        raise ValueError("index.html 的 ECharts CDN 块未能唯一替换")
    for source in ("data/snapshot.js", "data/lingyang.js", "app.js"):
        tag = f'<script src="{source}"></script>'
        content = (dashboard / source).read_text(encoding="utf-8")
        if html.count(tag) != 1:
            raise ValueError(f"index.html 的 {source} 引用未能唯一替换")
        html = html.replace(tag, "<script>\n" + content + "\n</script>")
    if re.search(r'<script\s+src=', html):
        raise ValueError("单文件版仍包含外部脚本依赖")
    temporary = online.with_suffix(".html.tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, online)
    return online
