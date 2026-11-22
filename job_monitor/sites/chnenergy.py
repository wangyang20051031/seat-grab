# -*- coding: utf-8 -*-
"""国家能源集团招聘公告抓取（ggtype=1，不含录用公示）。"""
import html as html_lib
import re
from urllib.parse import urljoin

import requests
import urllib3

from .base import Announcement

BASE = "https://zhaopin.chnenergy.com.cn"
LIST_URL = f"{BASE}/annc/annclist?ggtype=1"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_A_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']*showgg\?id=[^\"']+)[\"'][^>]*>([\s\S]*?)</a>", re.I)
_DATE_RE = re.compile(r"发布日期\s*[：:]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_TAG_RE = re.compile(r"<[^>]+>")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _clean_text(raw):
    text = html_lib.unescape(_TAG_RE.sub(" ", raw))
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _get(url):
    response = requests.get(url, headers=HEADERS, timeout=20, verify=False)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def fetch():
    page = _get(LIST_URL)
    items = []
    seen_urls = set()
    for match in _A_RE.finditer(page):
        url = urljoin(LIST_URL, html_lib.unescape(match.group(1)).strip())
        title = _clean_text(match.group(2))
        if url in seen_urls or len(title) < 6:
            continue
        seen_urls.add(url)
        try:
            detail = _clean_text(_get(url))
        except Exception as exc:
            print(f"[chnenergy] 详情页失败《{title}》: {exc}")
            continue
        date_match = _DATE_RE.search(detail)
        if not date_match:
            print(f"[chnenergy] 未找到发布日期《{title}》")
            continue
        date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        items.append(Announcement(
            title=title,
            url=url,
            date=date,
            source="国家能源集团",
            category="央国企",
        ))
    return items
