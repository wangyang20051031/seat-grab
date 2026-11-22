# -*- coding: utf-8 -*-
"""呼和浩特人事考试网（http://www.hhpta.org.cn）公告抓取。

与内蒙古人事考试网（impta）同构：列表页 more.asp?ttt=XX，编码 gb2312，
详情链接形如 /shiyedanweigonggao/YYYYMMDDHHMMSS.asp。
只抓招聘/考试公告类栏目，公示结果类不抓以减少噪音。
"""
import re
import requests
from .base import Announcement

BASE = "http://www.hhpta.org.cn"

# 栏目 ttt -> 栏目名（只抓招聘/考试公告类，公示类不抓）
COLUMNS = {
    10: "事业单位考试",
    11: "事业单位公告",
    16: "其他考试公告",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_A_RE = re.compile(r'<a\s+href="([^"]+\.asp)"[^>]*>([\s\S]*?)</a>', re.I)
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
        # 跳过导航/分页链接（more.asp）和外站链接
        if "more.asp" in href or (href.startswith(("http://", "https://")) and "hhpta" not in href):
            continue
        title = _clean_title(m.group(2))
        if len(title) < 4:
            continue
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
            source="呼和浩特人事考试网",
            category=category,
        ))
    return items


def fetch():
    out = []
    for ttt, name in COLUMNS.items():
        try:
            out.extend(_fetch_column(ttt, name))
        except Exception as e:
            print(f"[hhpta] 栏目《{name}》失败: {e}")
    return out
