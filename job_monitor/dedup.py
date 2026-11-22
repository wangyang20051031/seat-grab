# -*- coding: utf-8 -*-
"""去重：用 seen.json 持久化已推送公告，避免重复邮件。
seen.json 会被 Actions 提交回仓库，跨运行保留。仅存 URL/标题与抓取日期，不含个人信息。
"""
import json
import os
import re
from datetime import date, datetime, timedelta

# 标题归一化：去掉空白与中英文标点，用于「跨站同公告」去重
# （典型场景：impta 与兴安盟都转发国考公告，URL 不同但标题几乎一致）
_TITLE_NOISE_RE = re.compile(
    r'''[\s\u3000，。、；：""''（）()【】《》\-\—…·,.!?！？"']'''
)
_TITLES_KEY = "__titles__"  # seen.json 中存放「已推送归一化标题」的特殊键


def norm_title(title: str) -> str:
    """标题归一化：去空白/标点，返回清洗后的字符串；空标题返回空串。"""
    return _TITLE_NOISE_RE.sub("", title or "").strip()


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
    """过滤出近 recent_days 天内、且未推送过的公告，按日期倒序，最多 max_items 条。

    去重两层：
      1) URL（dedup_key）：同一条链接不重复推；
      2) 归一化标题：跨站同公告（如 impta 与兴安盟都转发国考公告）只推一条，
         且已推送过的标题即使次日出现在别的站也不再推。
         SITES 顺序在前的站先进入结果，故优先保留 impta 等主站。
    """
    today = date.today()
    cutoff = today - timedelta(days=recent_days)
    seen_titles = seen.get(_TITLES_KEY)
    if not isinstance(seen_titles, dict):
        seen_titles = {}
    uniq = {}
    used_titles = set()
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
        tkey = norm_title(it.title)
        # 跨运行：该标题此前已推送过（可能在别的站）→ 跳过
        if tkey and tkey in seen_titles:
            continue
        # 单次运行内：同归一化标题只留第一条（排在前面的站优先保留）
        if tkey and tkey in used_titles:
            continue
        if tkey:
            used_titles.add(tkey)
        # 同 URL 多条（不同栏目重复）只留一条
        if key not in uniq:
            uniq[key] = it
    news = sorted(uniq.values(), key=lambda x: x.date, reverse=True)
    return news[:max_items]


def mark(seen: dict, items) -> None:
    today = str(date.today())
    titles = seen.get(_TITLES_KEY)
    if not isinstance(titles, dict):
        titles = {}
        seen[_TITLES_KEY] = titles
    for it in items:
        seen[it.dedup_key] = today
        tkey = norm_title(it.title)
        if tkey:
            titles[tkey] = today


def prune_old(seen: dict, keep_days: int = 60) -> int:
    """清理超过 keep_days 天的旧记录，防止文件无限膨胀。返回删除条数。"""
    today = date.today()
    cutoff = today - timedelta(days=keep_days)
    to_del = []
    for k, v in seen.items():
        if k == _TITLES_KEY:
            continue
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
            if d < cutoff:
                to_del.append(k)
        except Exception:
            to_del.append(k)
    for k in to_del:
        del seen[k]
    # 同步清理「已推送标题」集合里的旧项，避免无限增长
    titles = seen.get(_TITLES_KEY)
    if isinstance(titles, dict):
        td = []
        for k, v in titles.items():
            try:
                d = datetime.strptime(v, "%Y-%m-%d").date()
                if d < cutoff:
                    td.append(k)
            except Exception:
                td.append(k)
        for k in td:
            del titles[k]
        if not titles:
            seen.pop(_TITLES_KEY, None)
    return len(to_del)
