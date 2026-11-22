# -*- coding: utf-8 -*-
"""中国铁路人才招聘网公告抓取（含各铁路局招聘公告）。"""
import base64
import json
import re
from urllib.parse import urljoin

import requests
import urllib3
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import Announcement

BASE = "https://rczp.china-railway.com.cn"
API_URL = f"{BASE}/job/chnldocinfo/pageList?current=1&size=20"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Content-Type": "application/json;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/page/recruitment/rec_info.html",
    "Origin": BASE,
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _encrypted_request_body():
    key = b"g0UNalNo4nSmGZwS"
    plaintext = json.dumps([1000, 521], separators=(",", ":")).encode("utf-8")
    pad_length = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad_length]) * pad_length
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return json.dumps({"data": base64.b64encode(encrypted).decode("ascii")})


def fetch():
    response = requests.post(
        API_URL,
        data=_encrypted_request_body(),
        headers=HEADERS,
        timeout=20,
        verify=False,
    )
    response.raise_for_status()
    records = response.json().get("obj", {}).get("records", [])
    items = []
    for record in records:
        title = re.sub(r"\s+", " ", record.get("doctitle", "")).strip()
        date = str(record.get("docpubtime", ""))[:10]
        if not title or not _DATE_RE.fullmatch(date):
            continue
        public_url = str(record.get("docpuburl") or "").strip()
        if public_url:
            url = urljoin(BASE, public_url)
        else:
            url = f"{BASE}/page/recruitment/rec_info.html?docid={record.get('docid', '')}"
        items.append(Announcement(
            title=title,
            url=url,
            date=date,
            source="中国铁路人才招聘网",
            category="央国企",
        ))
    return items
