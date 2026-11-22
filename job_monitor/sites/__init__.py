# -*- coding: utf-8 -*-
"""招聘站点爬虫集合。每个站点暴露 fetch() -> list[Announcement]。
新增站点：在本目录加一个模块并在 SITES 注册即可。
"""
from . import bank, chnenergy, govportal, hhpta, hlbrrc, impta, railway, tobacco
from .base import Announcement

# (站点显示名, 抓取函数)
SITES = [
    ("内蒙古人事考试网", impta.fetch),
    ("呼和浩特人事考试网", hhpta.fetch),
    ("呼伦贝尔人才网", hlbrrc.fetch),
    ("包头市人社局", govportal.fetch_baotou),
    ("通辽市人社局", govportal.fetch_tongliao),
    ("赤峰市人社局", govportal.fetch_chifeng),
    ("鄂尔多斯市人社局", govportal.fetch_ordos),
    ("巴彦淖尔市人社局", govportal.fetch_bayannur),
    ("阿拉善盟人社局", govportal.fetch_alxa),
    ("锡林郭勒盟人事考试", govportal.fetch_xilingol),
    ("兴安盟人事考试", govportal.fetch_xingan),
    ("乌兰察布人事考试", govportal.fetch_wulanchabu),
    ("乌海市人社局", govportal.fetch_wuhai),
    ("满洲里市人民政府", govportal.fetch_manzhouli),
    ("银行招聘网", bank.fetch),
    ("中国烟草", tobacco.fetch),
    ("国家能源集团", chnenergy.fetch),
    ("中国铁路人才招聘网", railway.fetch),
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
