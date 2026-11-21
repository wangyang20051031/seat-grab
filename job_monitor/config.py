# -*- coding: utf-8 -*-
"""配置：所有敏感值一律从环境变量读取，绝不硬编码。

GitHub Actions 中通过 secrets 注入；本地调试可在仓库根放 .env 并手动 export，
或直接设置环境变量。.env 真实文件已被 .gitignore 忽略，不会进仓库。
"""
import os


class CFG:
    # ---- 邮件推送（QQ 邮箱，与抢座脚本的 Server酱 通道完全隔离）----
    QQ_MAIL_USER = os.environ.get("QQ_MAIL_USER", "")          # QQ 邮箱账号
    QQ_MAIL_AUTH_CODE = os.environ.get("QQ_MAIL_AUTH_CODE", "")  # QQ 邮箱 SMTP 授权码（非登录密码）
    QQ_MAIL_TO = os.environ.get("QQ_MAIL_TO", "") or os.environ.get("QQ_MAIL_USER", "")

    # ---- 运行参数 ----
    SEEN_FILE = os.environ.get("JOB_SEEN_FILE", "job_monitor_seen.json")
    RECENT_DAYS = int(os.environ.get("JOB_RECENT_DAYS", "7") or "7")   # 只推最近 N 天
    MAX_ITEMS = int(os.environ.get("JOB_MAX_ITEMS", "30") or "30")     # 单封邮件最多条数

    @classmethod
    def mail_ready(cls) -> bool:
        return bool(cls.QQ_MAIL_USER and cls.QQ_MAIL_AUTH_CODE and cls.QQ_MAIL_TO)
