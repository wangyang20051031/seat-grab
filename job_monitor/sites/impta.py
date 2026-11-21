# -*- coding: utf-8 -*-
"""内蒙古人事考试网（http://www.impta.com.cn）公告抓取。

该站为传统 ASP 服务端渲染，列表页 more.asp?ttt=XX，编码 gb2312。
只解析公告列表（标题/链接/日期），不登录、不提交任何个人信息。
"""
import re
import requests
from .base import Announcement

BASE = "http://www.impta.com.cn"

# 栏目 ttt -> 栏目名（只抓招聘/考试类，公示结果类不抓以减少噪音）
COLUMNS = {
    33: "公务员考试",
    31: "事业单位考试",
    42: "事业单位公告",
    34: "选调生考试",
    35: "遴选公务员",
    32: "基层服务考试",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# 匹配公告链接：<a href="xxx.asp">标题（含内嵌 img/&nbsp;）</a>
_A_RE = re.compile(r'<a\s+href="([^"]+\.asp)"[^>]*>([\s\S]*?)</a>', re.I)
# 匹配日期 YYYY/M/D 或 YYYY-MM-DD
_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _fetch_column(ttt: int, category: str):
    url = f"{BASE}/more.asp?ttt={ttt}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.encoding = "gb2312"
    html = r.text

    items = []
    for m in _A_RE.finditer(html):
        href = m.group(1)
        # 跳过导航/分页链接（如 more.asp?ttt=、more1.asp 等）
        if "more.asp" in href or href.startswith(("http://", "https://")) and "impta" not in href:
            continue
        title = _clean_title(m.group(2))
        if len(title) < 4:
            continue
        # 拼接完整 URL
        full_url = href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}"
        # 在链接之后的文本里找日期
        tail = html[m.end(): m.end() + 600]
        dm = _DATE_RE.search(tail)
        if not dm:
            continue
        date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        items.append(Announcement(
            title=title,
            url=full_url,
            date=date,
            source="内蒙古人事考试网",
            category=category,
        ))
    return items


def fetch():
    out = []
    for ttt, name in COLUMNS.items():
        try:
            out.extend(_fetch_column(ttt, name))
        except Exception as e:
            print(f"[impta] 栏目《{name}》失败: {e}")
    return out
