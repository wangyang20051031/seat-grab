# -*- coding: utf-8 -*-
"""招聘站点爬虫集合。每个站点暴露 fetch() -> list[Announcement]。
新增站点：在本目录加一个模块并在 SITES 注册即可。
"""
from . import impta
from .base import Announcement

# (站点显示名, 抓取函数)
SITES = [
    ("内蒙古人事考试网", impta.fetch),
    # 更多站点待补充：国聘 / 内蒙古电力 / 国网蒙东 等（SPA 站点需先定位 API）
]


def fetch_all():
    """依次抓取所有站点，单站失败不影响其他站点。"""
    all_items = []
    for name, fn in SITES:
        try:
            items = fn()
            print(f"[站点] {name}: 抓到 {len(items)} 条")
            all_items.extend(items)
        except Exception as e:
            print(f"[站点] {name} 抓取失败: {e}")
    return all_items
