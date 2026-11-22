# -*- coding: utf-8 -*-
"""银行招聘网招聘公告抓取。"""
import html as html_lib
import re
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

from .base import Announcement

BASE = "http://www.yinhangzhaopin.com"
LIST_PAGES = (
    (f"{BASE}/new100.htm", "最新招聘"),
    (f"{BASE}/xiaoyuanzhaopin/", "校园招聘"),
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_A_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", re.I)
_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?")
_TAG_RE = re.compile(r"<[^>]+>")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _clean_title(raw):
    text = html_lib.unescape(_TAG_RE.sub("", raw))
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _find_date(tail):
    next_link = re.search(r"<a\b", tail, re.I)
    if next_link:
        tail = tail[:next_link.start()]
    text = html_lib.unescape(_TAG_RE.sub(" ", tail))
    match = _DATE_RE.search(text)
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _fetch_page(url, category):
    response = requests.get(url, headers=HEADERS, timeout=20, verify=False)
    response.raise_for_status()
    response.encoding = "gb18030"
    page = response.text
    items = []
    for match in _A_RE.finditer(page):
        href = html_lib.unescape(match.group(1)).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(url, href)
        if urlsplit(full_url).hostname not in {"yinhangzhaopin.com", "www.yinhangzhaopin.com"}:
            continue
        if not re.search(r"\.html?(?:$|\?)", full_url, re.I):
            continue
        if re.search(r"/(?:index|new100)\.html?(?:$|\?)", full_url, re.I):
            continue
        title = _clean_title(match.group(2))
        if len(title) < 6:
            continue
        date = _find_date(page[match.end():match.end() + 600])
        if not date:
            continue
        items.append(Announcement(
            title=title,
            url=full_url,
            date=date,
            source="银行招聘网",
            category=category,
        ))
    return items


def fetch():
    items = []
    for url, category in LIST_PAGES:
        try:
            items.extend(_fetch_page(url, category))
        except Exception as exc:
            print(f"[bank] 栏目《{category}》失败: {exc}")
    return items
