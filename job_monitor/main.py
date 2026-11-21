# -*- coding: utf-8 -*-
"""招聘监控主入口。

运行：python -m job_monitor.main  （在仓库根目录）
环境变量：QQ_MAIL_USER / QQ_MAIL_AUTH_CODE / QQ_MAIL_TO（Actions 从 secrets 注入）
输出：stdout 日志；有新增则发邮件并更新 seen.json。
"""
import sys
from datetime import date

from .config import CFG
from . import dedup
from . import notifier
from .sites import fetch_all


def build_mail(news):
    today = date.today().isoformat()
    lines = [
        f"招聘监控 {today}，新增 {len(news)} 条（近 {CFG.RECENT_DAYS} 天）：",
        "",
    ]
    for i, it in enumerate(news, 1):
        lines.append(f"{i}. [{it.source}·{it.category}] {it.title}")
        lines.append(f"   日期：{it.date}")
        lines.append(f"   链接：{it.url}")
        lines.append("")
    lines.append("—— 招聘监控（GitHub Actions 定时运行）")
    return f"招聘提醒 {today}（新增 {len(news)} 条）", "\n".join(lines)


def main():
    today = date.today().isoformat()
    print(f"===== 招聘监控 {today} START =====")

    seen = dedup.load(CFG.SEEN_FILE)
    print(f"已去重记录：{len(seen)} 条")

    items = fetch_all()
    print(f"共抓取原始公告：{len(items)} 条")

    news = dedup.filter_new(seen, items, CFG.RECENT_DAYS, CFG.MAX_ITEMS)
    print(f"新增（近 {CFG.RECENT_DAYS} 天且未推送）：{len(news)} 条")

    if not news:
        print("无新增，不发邮件。")
        print(f"===== 招聘监控 {today} END 新增0 =====")
        return 0

    # 有新增：先发邮件，成功后再标记 seen（避免发失败却标记已推送导致漏推）
    if not CFG.mail_ready():
        print("[警告] 未配置 QQ 邮箱环境变量，跳过邮件发送；seen 未更新（下次仍会推送）。")
        print("请设置 QQ_MAIL_USER / QQ_MAIL_AUTH_CODE / QQ_MAIL_TO。")
        print(f"===== 招聘监控 {today} END 新增{len(news)} 未发送 =====")
        return 0  # 本地测试友好：不因缺配置而标红

    subject, body = build_mail(news)
    try:
        notifier.send(CFG.QQ_MAIL_USER, CFG.QQ_MAIL_AUTH_CODE, CFG.QQ_MAIL_TO, subject, body)
        send_ok = True
    except Exception as e:
        print(f"[邮件] 发送失败：{e}")
        send_ok = False

    if send_ok:
        dedup.mark(seen, news)
        removed = dedup.prune_old(seen, keep_days=60)
        dedup.save(CFG.SEEN_FILE, seen)
        print(f"seen 已更新（清理旧记录 {removed} 条），当前 {len(seen)} 条。")
        print(f"===== 招聘监控 {today} END 新增{len(news)} 发送OK =====")
        return 0
    else:
        # 发送失败：不更新 seen，让 Actions 标红便于发现
        print(f"===== 招聘监控 {today} END 新增{len(news)} 发送FAIL =====")
        return 1


if __name__ == "__main__":
    sys.exit(main())
