# -*- coding: utf-8 -*-
"""QQ 邮箱推送（SMTP）。账号/授权码从环境变量注入，代码内无任何凭据。"""
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587  # STARTTLS


def send(user: str, auth_code: str, to: str, subject: str, content: str) -> None:
    if not (user and auth_code and to):
        raise ValueError("邮件参数不完整：请设置 QQ_MAIL_USER / QQ_MAIL_AUTH_CODE / QQ_MAIL_TO")

    msg = MIMEText(content, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("招聘监控", "utf-8")), user))
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(user, auth_code)
        s.sendmail(user, [to], msg.as_string())
    # 仅打印状态，不回显账号/授权码
    print("[邮件] 发送成功")
