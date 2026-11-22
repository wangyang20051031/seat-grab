# -*- coding: utf-8 -*-
"""呼伦贝尔人才网（http://www.hlbrrc.com.cn）公告抓取。

首页为服务端渲染，通知公告/新闻动态条目结构统一：
  <a href="/COL/detailed/ID.html" title="标题" ...>
    <h3>标题</h3><span class="fr">2026-11-20</span>
  </a>
只抓 tzgg（通知公告）和 news（新闻动态）栏目，含事业单位招聘、引进人才、
三支一扶、选聘递补等；tpxw（图片新闻）多为考试动态，不抓。
"""
import re
import requests
from .base import Announcement

BASE = "http://www.hlbrrc.com.cn"
HOME = f"{BASE}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# 栏目代码 -> 中文名（只保留招聘相关栏目）
COL_CATEGORY = {
    "tzgg": "通知公告",
    "news": "新闻动态",
}

# 匹配整条公告：href 栏目/ID + title 标题 + span.fr 日期
_ITEM_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>/(?P<col>\w+)/detailed/(?P<id>\d+)\.html)["\']'
    r'[^>]*title=["\'](?P<title>[^"\']+)["\'][\s\S]*?'
    r'<span class="fr">(?P<date>\d{4}-\d{1,2}-\d{1,2})</span>',
    re.I,
)


def fetch():
    try:
        r = requests.get(HOME, headers=HEADERS, timeout=20)
        r.encoding = "utf-8"
        html = r.text
    except Exception as e:
        print(f"[hlbrrc] 首页抓取失败: {e}")
        return []

    items = []
    seen_ids = set()
    for m in _ITEM_RE.finditer(html):
        col = m.group("col").lower()
        if col not in COL_CATEGORY:
            continue
        aid = m.group("id")
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        title = m.group("title").strip()
        if len(title) < 4:
            continue
        y, mo, d = m.group("date").split("-")
        date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        href = m.group("href")
        full_url = href if href.startswith("http") else f"{BASE}{href}"
        items.append(Announcement(
            title=title,
            url=full_url,
            date=date,
            source="呼伦贝尔人才网",
            category=COL_CATEGORY[col],
        ))
    return items
