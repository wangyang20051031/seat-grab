# -*- coding: utf-8 -*-
"""中国烟草招聘公告抓取，过滤拟录用公示等结果类信息。"""
import html as html_lib
import re
from urllib.parse import urljoin

import requests
import urllib3

from .base import Announcement

LIST_URL = "http://www.tobacco.gov.cn/gjyc/zpxx/list.shtml"
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


def fetch():
    response = requests.get(LIST_URL, headers=HEADERS, timeout=20, verify=False)
    response.raise_for_status()
    response.encoding = "utf-8"
    page = response.text
    items = []
    for match in _A_RE.finditer(page):
        href = html_lib.unescape(match.group(1)).strip()
        if "/gjyc/zpxx/" not in href or href.endswith("/list.shtml"):
            continue
        title = _clean_title(match.group(2))
        if len(title) < 6 or "公示" in title:
            continue
        date = _find_date(page[match.end():match.end() + 600])
        if not date:
            continue
        items.append(Announcement(
            title=title,
            url=urljoin(LIST_URL, href),
            date=date,
            source="中国烟草",
            category="央国企",
        ))
    return items
