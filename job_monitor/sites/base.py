# -*- coding: utf-8 -*-
"""公告数据结构。各站点爬虫统一返回 list[Announcement]。"""
from dataclasses import dataclass


@dataclass
class Announcement:
    title: str
    url: str
    date: str        # 统一格式 YYYY-MM-DD
    source: str      # 来源站点，如 "内蒙古人事考试网"
    category: str = ""  # 栏目/分类

    @property
    def dedup_key(self) -> str:
        """去重键：优先规范化 URL，无 URL 时用 来源|标题。"""
        u = (self.url or "").split("#")[0].strip()
        return u if u else f"{self.source}|{self.title}"
