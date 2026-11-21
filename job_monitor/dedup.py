# -*- coding: utf-8 -*-
"""去重：用 seen.json 持久化已推送公告，避免重复邮件。
seen.json 会被 Actions 提交回仓库，跨运行保留。仅存 URL/标题与抓取日期，不含个人信息。
"""
import json
import os
from datetime import date, datetime, timedelta


def load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save(path: str, seen: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)


def filter_new(seen: dict, items, recent_days: int = 7, max_items: int = 30):
    """过滤出近 recent_days 天内、且未推送过的公告，按日期倒序，最多 max_items 条。"""
    today = date.today()
    cutoff = today - timedelta(days=recent_days)
    uniq = {}
    for it in items:
        try:
            d = datetime.strptime(it.date, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < cutoff:
            continue
        key = it.dedup_key
        if key in seen:
            continue
        # 同 key 多条（不同栏目重复）只留一条
        if key not in uniq:
            uniq[key] = it
    news = sorted(uniq.values(), key=lambda x: x.date, reverse=True)
    return news[:max_items]


def mark(seen: dict, items) -> None:
    today = str(date.today())
    for it in items:
        seen[it.dedup_key] = today


def prune_old(seen: dict, keep_days: int = 60) -> int:
    """清理超过 keep_days 天的旧记录，防止文件无限膨胀。返回删除条数。"""
    today = date.today()
    cutoff = today - timedelta(days=keep_days)
    to_del = []
    for k, v in seen.items():
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
            if d < cutoff:
                to_del.append(k)
        except Exception:
            to_del.append(k)
    for k in to_del:
        del seen[k]
    return len(to_del)
