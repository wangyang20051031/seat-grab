# -*- coding: utf-8 -*-
"""内蒙古各盟市政府门户群公告通用抓取器。

设计思路（抗改版）：
  1. 不依赖 CSS 类名 / DOM 层级，只按「详情页 URL 模式」匹配链接；
  2. 日期直接从 URL 里提取（政府门户详情 URL 普遍内嵌日期，如 t20261118_618093.html、
     /art/2026/11/22/art_...、/20261120174004.../index.shtml），页面改版几乎不影响；
  3. 每个站只配「列表栏目页 URL + 域名」，新增盟市 = 加一行配置；
  4. 用招聘关键词白名单过滤，排除党建新闻 / 政策文件 / 年度报表等噪音。

新增站点：在 SITE_CONFIGS 里加一条配置，并在底部暴露 fetch_xxx。
"""
import re
import requests
import urllib3
from urllib.parse import urljoin, urlparse

from .base import Announcement

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# 详情页 URL 模式（日期提取）。顺序即优先级。
# 返回 (匹配对象, 分组数)：1 组表示 "YYYYMMDD" 连续串；3 组表示 "YYYY MM DD" 分段。
DETAIL_PATTERNS = [
    (re.compile(r"t(\d{8})_\d+\.s?html?", re.I), 1),    # 模式A：/202611/t20261118_618093.html
    (re.compile(r"/art/(\d{4})/(\d{1,2})/(\d{1,2})/art_", re.I), 3),  # 模式B：/art/2026/11/22/art_...
    (re.compile(r"/(\d{8})\d{6,}/index\.s?html?", re.I), 1),          # 模式C：/20261120174004.../index.shtml
]

_A_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# 标题首尾内嵌的日期（如 "2026-10-19 ·xxx" 或 "xxx 2026-09-14"），清理掉
_LEAD_DATE_RE = re.compile(r"^\s*\d{4}[-./年]\s*\d{1,2}[-./月]\s*\d{1,2}日?\s*[·•\-–\s]*")
_TAIL_DATE_RE = re.compile(r"\s*\d{4}[-./年]\s*\d{1,2}[-./月]\s*\d{1,2}日?\s*[·•\-–\s]*$")
# 从链接附近文本提取日期（用于详情 URL 不含日期的站点）
_DATE_TEXT_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")

# 招聘关键词白名单：标题含任一才保留
JOB_KW = (
    "公开招聘", "招聘", "引进", "遴选", "事业单位", "公务员", "教师", "医疗",
    "选调", "选聘", "考录", "招考", "招录", "岗位", "报名", "笔试", "面试",
    "聘用", "录用", "资格复审", "体检", "考察", "急需紧缺", "优青汇", "聚贤汇",
    "人才引进", "三支一扶", "社区民生", "留置看护", "引才", "辅警", "公安",
    "高层次", "校园招聘", "公费师范生",
)
# 噪音黑名单：含则排除（优先级高于白名单）
NOISE_KW = (
    "年度报表", "仲裁", "政策问答", "实施细则", "创业扶持", "欠薪", "工伤预防",
    "退休", "党日", "党建", "廉政", "二十届", "学习贯彻", "技能等级", "职业培训",
    "集体合同", "和谐劳动关系", "政府网站工作", "领取", "合格人员", "资格考试",
    "行政执法", "委托书", "监管台账", "价格监测", "招商引资", "安全生产",
    "岗位练兵", "技能竞赛", "验收", "斩获", "专家组", "莅临", "建设工作",
    "资格（水平）",
)


def _date_from_url(full: str):
    """从详情 URL 提取日期，返回 'YYYY-MM-DD' 或 None。"""
    for rx, groups in DETAIL_PATTERNS:
        m = rx.search(full)
        if not m:
            continue
        g = m.groups()
        if groups == 1:
            d = g[0]
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return f"{int(g[0]):04d}-{int(g[1]):02d}-{int(g[2]):02d}"
    return None


def _date_from_text(text: str):
    """从链接附近的文本里提取日期，返回 'YYYY-MM-DD' 或 None。"""
    m = _DATE_TEXT_RE.search(text)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _clean_title(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    text = _LEAD_DATE_RE.sub("", text)
    text = _TAIL_DATE_RE.sub("", text)
    text = re.sub(r"^[·•\-–\s]+", "", text)
    return _WS_RE.sub(" ", text).strip()


def _is_job(title: str) -> bool:
    if any(k in title for k in NOISE_KW):
        return False
    return any(k in title for k in JOB_KW)


def _get(url: str):
    # 政府站偶有证书问题，先严格校验，失败则降级不校验（只读公开信息）
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, verify=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        r = requests.get(url, headers=HEADERS, timeout=25, verify=False, allow_redirects=True)
    r.encoding = r.apparent_encoding or r.encoding
    return r


def _fetch_page(cfg: dict, page_url: str, seen: set):
    """抓一个列表页，把招聘公告追加进 seen 对应条目。返回新增 Announcement 列表。"""
    host = cfg["host"]
    source = cfg["source"]
    category = cfg.get("category", "")
    items = []
    r = _get(page_url)
    if r.status_code != 200:
        print(f"[{source}] 列表页 {page_url} 返回 {r.status_code}，跳过")
        return items
    html = r.text
    page_seen = set()
    for m in _A_RE.finditer(html):
        href = (m.group(1) or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(page_url, href)
        # 只保留本站链接（政府门户首页常外链到 impta 等其他站，交给对应站抓）
        if urlparse(full).netloc != host:
            continue
        # 识别详情链接 + 取日期：
        #   - 配了 detail_re：先按正则判定是否详情链接，日期来源由 date_mode 决定
        #     （"text" = 从链接附近文本取，用于 URL 不含日期的站；否则从 URL 取）
        #   - 没配 detail_re：默认走 URL 自带日期模式（识别链接与取日期二合一）
        detail_re = cfg.get("detail_re")
        if detail_re is not None:
            if not detail_re.search(full):
                continue
            if cfg.get("date_mode") == "text":
                d = _date_from_text(html[m.end():m.end() + 220])
            else:
                d = _date_from_url(full)
        else:
            d = _date_from_url(full)
        if not d:
            continue
        title = _clean_title(m.group(2))
        if len(title) < 5:
            continue
        if not _is_job(title):
            continue
        key = full  # 同站跨栏目去重
        if key in page_seen or key in seen:
            continue
        page_seen.add(key)
        seen.add(key)
        items.append(Announcement(
            title=title, url=full, date=d, source=source, category=category,
        ))
    return items


def fetch_site(cfg: dict):
    """抓一个站点的所有配置栏目页。"""
    seen = set()
    out = []
    for page_url in cfg["pages"]:
        try:
            out.extend(_fetch_page(cfg, page_url, seen))
        except Exception as e:
            print(f"[{cfg['source']}] 栏目页 {page_url} 抓取失败: {e}")
    return out


# ===== 站点配置 =====
# host: 用于同域过滤；pages: 招聘/通知公告栏目列表页（按招聘相关度排序，越靠前越精准）
SITE_CONFIGS = {
    "baotou": {
        "source": "包头市人社局",
        "host": "rsj.baotou.gov.cn",
        "category": "事业单位/通知公告",
        "pages": [
            "http://rsj.baotou.gov.cn/ywxx/sydwgkzpxx/",   # 事业单位公开招聘信息
            "http://rsj.baotou.gov.cn/ywdt/tzgg/",         # 通知公告
        ],
    },
    "tongliao": {
        "source": "通辽市人社局",
        "host": "rsj.tongliao.gov.cn",
        "category": "人事考试/通知公告",
        "pages": [
            "http://rsj.tongliao.gov.cn/rsks/",           # 人事考试
            "http://rsj.tongliao.gov.cn/xwzx/tzgg/",      # 通知公告
        ],
    },
    "chifeng": {
        "source": "赤峰市人社局",
        "host": "rsj.chifeng.gov.cn",
        "category": "公开招聘/公告公示",
        "pages": [
            "http://rsj.chifeng.gov.cn/cfsrsjztzl/cfsrsjgkzp2022/",  # 事业单位公开招聘
            "http://rsj.chifeng.gov.cn/cfsrsjgggs/",                 # 公告公示
        ],
    },
    "ordos": {
        "source": "鄂尔多斯市人社局",
        "host": "rsj.ordos.gov.cn",
        "category": "公开招聘/通知公告",
        "pages": [
            "http://rsj.ordos.gov.cn/rskszx/ksxx/",                 # 公开招聘考录
            "http://rsj.ordos.gov.cn/xwzx_146764/tzgg_146768/",     # 通知公告
        ],
    },
    "bayannur": {
        "source": "巴彦淖尔市人社局",
        "host": "rsj.bynr.gov.cn",
        "category": "公开招聘/公告公示",
        "pages": [
            "http://rsj.bynr.gov.cn/rsks/gkzpkl/",   # 公开招聘考录
            "http://rsj.bynr.gov.cn/gggs/",          # 公告公示
        ],
    },
    "alxa": {
        "source": "阿拉善盟人社局",
        "host": "rsj.als.gov.cn",
        "category": "人才引进",
        "pages": [
            "http://rsj.als.gov.cn/col/col5778/index.html",  # 人才引进公告
        ],
    },
    "xilin Gol": {
        "source": "锡林郭勒盟人事考试",
        "host": "www.xmpta.org.cn",
        "category": "通知公告",
        "pages": [
            "http://www.xmpta.org.cn/",  # 栏目页 403，抓首页+关键词过滤
        ],
    },
    "xingan": {
        "source": "兴安盟人事考试",
        "host": "www.xamks.cn",
        "category": "招聘/公务员",
        # 详情 URL 形如 /html/4/Content2627.html，不含日期 → 从链接附近文本取日期
        "detail_re": re.compile(r"/html/\d+/Content\d+\.html", re.I),
        "date_mode": "text",
        "pages": [
            "http://www.xamks.cn/html/default.html",       # 首页聚合（各栏目最新）
            "http://www.xamks.cn/html/4/Default1.html",    # 招聘/事业单位/三支一扶
            "http://www.xamks.cn/html/8/Default1.html",    # 公务员/引才
        ],
    },
    "wulanchabu": {
        "source": "乌兰察布人事考试",
        "host": "www.wmpta.org.cn",
        "category": "事业单位/通知公告",
        # 详情 URL 形如 /article/989-2.html，不含日期 → 从链接附近文本取日期
        "detail_re": re.compile(r"/article/\d+-\d+\.html", re.I),
        "date_mode": "text",
        "pages": [
            "https://www.wmpta.org.cn/list/2.html",   # 事业单位考试
            "https://www.wmpta.org.cn/list/7.html",   # 选调遴选
            "https://www.wmpta.org.cn/list/8.html",   # 公务员
            "https://www.wmpta.org.cn/list/25.html",  # 新闻快讯
            "https://www.wmpta.org.cn/list/40.html",  # 通知公告
        ],
    },
    "wuhai": {
        "source": "乌海市人社局",
        "host": "rsj.wuhai.gov.cn",
        "category": "人事考试/通知公告",
        # 详情 URL 形如 /rsj/.../2354113/index.html，不含日期 → 从链接附近文本取日期
        "detail_re": re.compile(r"/rsj/.*?/\d+/index\.html", re.I),
        "date_mode": "text",
        "pages": [
            "http://rsj.wuhai.gov.cn/rsj/1862563/1862569/index.html",  # 人事考试
            "http://rsj.wuhai.gov.cn/rsj/733324/733364/index.html",    # 通知公告
        ],
    },
    "manzhouli": {
        "source": "满洲里市人民政府",
        "host": "www.manzhouli.gov.cn",
        "category": "通知公告",
        # 详情 URL 形如 /News/show/1465958.html 或 /OpennessContent/show/539866.html
        "detail_re": re.compile(r"/(OpennessContent|News)/show/\d+\.html", re.I),
        "date_mode": "text",
        "pages": [
            "http://www.manzhouli.gov.cn/News/showList/17902/page_1.html",  # 通知公告
            "http://www.manzhouli.gov.cn/News/showList/17901/page_1.html",  # 部门动态
            "http://www.manzhouli.gov.cn/",  # 首页聚合最新
        ],
    },
}


def _make_fetch(cfg):
    def _fetch():
        return fetch_site(cfg)
    _fetch.__name__ = f"fetch_{cfg['source']}"
    return _fetch


# 暴露各站抓取函数，供 sites/__init__.py 注册
fetch_baotou = _make_fetch(SITE_CONFIGS["baotou"])
fetch_tongliao = _make_fetch(SITE_CONFIGS["tongliao"])
fetch_chifeng = _make_fetch(SITE_CONFIGS["chifeng"])
fetch_ordos = _make_fetch(SITE_CONFIGS["ordos"])
fetch_bayannur = _make_fetch(SITE_CONFIGS["bayannur"])
fetch_alxa = _make_fetch(SITE_CONFIGS["alxa"])
fetch_xilingol = _make_fetch(SITE_CONFIGS["xilin Gol"])
fetch_xingan = _make_fetch(SITE_CONFIGS["xingan"])
fetch_wulanchabu = _make_fetch(SITE_CONFIGS["wulanchabu"])
fetch_wuhai = _make_fetch(SITE_CONFIGS["wuhai"])
fetch_manzhouli = _make_fetch(SITE_CONFIGS["manzhouli"])
