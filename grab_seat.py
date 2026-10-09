# -*- coding: utf-8 -*-
"""
图书馆座位自动抢座脚本（超星座位系统）
功能：模拟登录 -> 提取页面 token -> 计算 enc 签名 -> 定点连抢三个时段 -> 失败重试 -> 弹窗/微信通知

运行模式：
    python grab_seat.py inspect   # 只登录并分析座位页面结构（不提交，安全）
    python grab_seat.py dryrun    # 立即提交“明天”的预约，用于验证 enc 是否正确（未到 20:00 会被拒）
    python grab_seat.py run       # 正式运行：等待到 20:00:00 自动开抢
配置文件：同目录 config.json；账号密码/推送 key 也支持环境变量（云端部署时使用）
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from zoneinfo import ZoneInfo
    BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # 兜底：固定 +8 时区
    BEIJING = dt.timezone(dt.timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 基础工具 ----------------

def now_bj():
    return dt.datetime.now(BEIJING)

def log(msg):
    print(f"[{now_bj().strftime('%H:%M:%S')}] {msg}", flush=True)

def aes_encrypt(text):
    """超星登录接口要求的 AES-128-CBC 加密（key/iv 公开固定，PKCS7 填充）"""
    key = iv = b"u2oh6Vu^HWe4_AES"
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor().update(data) + cipher.encryptor().finalize()
    return base64.b64encode(enc).decode("utf-8")

def make_enc(params, algorithm_value):
    """
    复现前端签名算法 verifyParam：
    参数按 key 排序 -> 每项拼成 [key=value] -> 追加 [页面算法值] -> MD5
    """
    seq = "".join(f"[{k}={params[k]}]" for k in sorted(params.keys()))
    seq += f"[{algorithm_value}]"
    return hashlib.md5(seq.encode("utf-8")).hexdigest()

# ---------------- 抢座核心 ----------------

class SeatGrabber:
    LOGIN_PAGE = "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
    LOGIN_API = "https://passport2.chaoxing.com/fanyalogin"
    PAGE_URL = "https://office.chaoxing.com/front/apps/seat/code?id={room}&seatNum={seat}"
    PAGE_URL_FALLBACK = "https://office.chaoxing.com/front/third/apps/seat/code?id={room}&seatNum={seat}"
    SUBMIT_API = "https://office.chaoxing.com/data/apps/seat/submit"

    # 旧版页面可能使用的固定盐（作为兜底）
    LEGACY_SALT = "%sd`~7^/>N4!Q#){''"

    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.base_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self.login_headers = {
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 10_3 like Mac OS X) "
                           "AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 "
                           "Mobile/14E304 Safari/602.1 MicroMessenger/8.0.5"),
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        self.page_salt = ""
        self.algorithm_value = ""

    # ---- 登录 ----
    def login(self):
        log("正在模拟登录...")
        self.session.get(self.LOGIN_PAGE, headers=self.login_headers, timeout=15)
        data = {
            "fid": -1,
            "uname": aes_encrypt(self.cfg["username"]),
            "password": aes_encrypt(self.cfg["password"]),
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2F",
            "t": True,
        }
        resp = self.session.post(self.LOGIN_API, params=data,
                                 headers=self.login_headers, timeout=15)
        obj = resp.json()
        if obj.get("status"):
            log("登录成功")
            return True
        raise RuntimeError(f"登录失败：{obj.get('msg2') or obj}")

    # ---- 座位页面：提取 token 和算法字段 ----
    def fetch_seat_page(self):
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        url = self.PAGE_URL.format(room=self.cfg["room_id"], seat=self.cfg["seat_num"])
        resp = self.session.get(url, headers=headers, timeout=15)
        html = resp.content.decode("utf-8", "replace")
        if "submit_enc" not in html:  # 备用页面路径
            url = self.PAGE_URL_FALLBACK.format(room=self.cfg["room_id"], seat=self.cfg["seat_num"])
            resp = self.session.get(url, headers=headers, timeout=15)
            html = resp.content.decode("utf-8", "replace")
        return html, resp.url

    @staticmethod
    def parse_page(html):
        info = {"token": "", "algorithm": "", "hidden_inputs": [], "js_tokens": []}
        m = re.search(r'id="submit_enc"\s+value="([^"]*)"', html)
        if m:
            info["token"] = m.group(1)
        for m in re.finditer(r"<input[^>]*>", html):
            tag = m.group(0)
            name = re.search(r'(?:id|name)="([^"]*)"', tag)
            val = re.search(r'value="([^"]*)"', tag)
            info["hidden_inputs"].append((name.group(1) if name else "",
                                         val.group(1) if val else ""))
        # 算法字段的多种可能位置
        candidates = [
            r'id="algorithm"[^>]*value="([^"]*)"',
            r'name="algorithm"[^>]*value="([^"]*)"',
            r'value="([^"]*)"[^>]*id="algorithm"',
            r'algorithm\s*[:=]\s*["\']([^"\']+)["\']',
        ]
        for pat in candidates:
            m = re.search(pat, html)
            if m and m.group(1):
                info["algorithm"] = m.group(1)
                break
        if not info["algorithm"]:
            vals = re.findall(r'value="([^"]+)"', html)
            if vals:  # 与开源实现一致：取页面第一个 value
                info["algorithm"] = vals[0]
        for m in re.finditer(r".*(?:token|enc|algorithm).*", html, re.IGNORECASE):
            line = m.group(0).strip()
            if "<input" in line or "var " in line or "token" in line.lower():
                info["js_tokens"].append(line[:200])
        return info

    def refresh_token(self):
        html, page_url = self.fetch_seat_page()
        info = self.parse_page(html)
        if not info["token"]:
            log(f"未提取到签名盐值，页面地址：{page_url}")
            return False
        self.page_salt = info["token"]
        log(f"签名盐值已更新：{self.page_salt[:16]}...")
        return True, html, info

    # ---- 提交一个时段 ----
    def submit_segment(self, start, end, day):
        # 参数集与新版前端 code_nobody.js 的 paramObj 完全一致
        params = {
            "roomId": str(self.cfg["room_id"]),
            "day": day,
            "startTime": start,
            "endTime": end,
            "seatNum": str(self.cfg["seat_num"]),
            "captcha": "",
            "type": "1",
            "verifyData": "1",
            "wyToken": "",
        }
        params["enc"] = make_enc(params, self.page_salt)
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        # 前端 $.post 使用 form-urlencoded 请求体
        resp = self.session.post(self.SUBMIT_API, data=params,
                                 headers=headers, timeout=15)
        try:
            return resp.json()
        except Exception:
            return {"success": False, "msg": f"响应非JSON：{resp.text[:100]}"}

# ---------------- 通知 ----------------

def push_wechat(key, title, content):
    """Server酱微信推送：https://sct.ftqq.com 登录后获取 SENDKEY"""
    if not key:
        return
    try:
        requests.post(f"https://sctapi.ftqq.com/{key}.send",
                      data={"title": title, "desp": content}, timeout=15)
        log("微信推送已发送")
    except Exception as e:
        log(f"微信推送失败：{e}")

def show_popup(title, message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
    except Exception:
        pass  # 无图形界面环境（云端）自动跳过

# ---------------- 结果分类 ----------------

def classify(result):
    msg = f'{result.get("msg", "")}{result.get("msg2", "")}'.strip()
    if result.get("success") is True:
        return "success", msg
    if any(k in msg for k in ("登录", "登陆", "未登录")):
        return "auth", msg
    # 冗余任务重复提交：该时段已被本账号另一任务抢到，视同成功
    if any(k in msg for k in ("已有预约", "已预约", "时间冲突", "预约冲突",
                              "存在预约", "重复预约", "已有签到")):
        return "dup", msg
    if any(k in msg for k in ("未到", "开放", "提前", "还未")):
        return "not_open", msg
    if any(k in msg for k in ("验证", "enc", "签名", "非法", "参数")):
        return "sign", msg
    return "taken", msg or str(result)

# ---------------- 主流程 ----------------

def load_config():
    # 本地 config.json（含真实账号密码，gitignore）；仓库中只有 config.example.json
    cfg_path = os.path.join(HERE, "config.json")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(HERE, "config.example.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖（GitHub Actions 使用，账号密码不写进仓库）
    cfg["username"] = os.environ.get("CHAOXING_USERNAME", cfg["username"])
    cfg["password"] = os.environ.get("CHAOXING_PASSWORD", cfg["password"])
    cfg["serverchan_key"] = os.environ.get("SERVERCHAN_KEY", cfg["serverchan_key"])
    if not cfg["username"] or not cfg["password"]:
        sys.exit("请先在 config.json 填写 username/password，或配置对应环境变量")
    return cfg

def wait_until_target(cfg):
    target = today_at(cfg["target_time"])
    gap = (target - now_bj()).total_seconds()
    if gap <= 0:
        log("已超过开抢时间，立即开始尝试")
        return
    log(f"距开抢还有 {int(gap)} 秒，进入等待...")
    while True:
        gap = (target - now_bj()).total_seconds()
        if gap <= 0:
            break
        time.sleep(0.05 if gap < 2 else min(1, gap))
    log("到点开抢！")

def today_at(hmstr):
    t = dt.datetime.strptime(hmstr, "%H:%M:%S").time()
    n = now_bj()
    return dt.datetime.combine(n.date(), t, tzinfo=BEIJING)

def run(cfg, mode):
    grabber = SeatGrabber(cfg)
    grabber.login()

    if mode == "inspect":
        ok, html, info = grabber.refresh_token()
        page_path = os.path.join(HERE, "seat_page.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(html)
        log("===== 页面分析结果 =====")
        log(f"token: {info['token']}")
        log(f"algorithm: {info['algorithm']}")
        log("页面所有 input 字段：")
        for name, val in info["hidden_inputs"]:
            log(f"  {name} = {val[:60]}")
        log(f"完整页面已保存：{page_path}")
        return

    # 预约日期：北京时间明天（day_offset 可配置）
    day = (now_bj().date() + dt.timedelta(days=cfg["day_offset"])).strftime("%Y-%m-%d")
    log(f"目标日期：{day}")

    if mode == "run":
        wait_until_target(cfg)
    else:
        log("dryrun 模式：不等待，立即提交")

    deadline = today_at(cfg["giveup_time"])
    if now_bj() >= deadline:  # 手动晚启动时自动延长 8 分钟
        deadline = now_bj() + dt.timedelta(minutes=8)

    segments = cfg["segments"]
    done = [False] * len(segments)
    segment_kind = {}      # success / dup
    last_msg = {}
    attempts = 0
    lo, hi = cfg["retry_interval"]

    while now_bj() < deadline and not all(done):
        attempts += 1
        if attempts == 1 or attempts % cfg["token_refresh_every"] == 0:
            res = grabber.refresh_token()
            if not res[0]:
                time.sleep(1); continue
        for i, (start, end) in enumerate(segments):
            if done[i]:
                continue
            try:
                result = grabber.submit_segment(start, end, day)
            except requests.RequestException as e:
                last_msg[i] = f"网络异常：{e}"
                log(last_msg[i]); continue
            kind, msg = classify(result)
            last_msg[i] = msg
            if kind in ("success", "dup"):
                done[i] = True
                segment_kind[i] = kind
                if kind == "success":
                    log(f"✅ {start}-{end} 预约成功！")
                else:
                    log(f"ℹ️ {start}-{end} 已由另一任务抢到：{msg}")
            elif kind == "auth":
                log("登录态失效，重新登录...")
                grabber.login()
            else:
                log(f"❌ {start}-{end}：{msg}")
        if not all(done):
            time.sleep(random.uniform(lo, hi))

    # ---- 汇总 + 通知 ----
    success_num = sum(1 for i in range(len(segments)) if segment_kind.get(i) == "success")
    dup_num = sum(1 for i in range(len(segments)) if segment_kind.get(i) == "dup")
    seat = cfg["seat_num"]

    # 纯冗余任务：一个新时段都没抢到（全部是别人/另一任务已抢），静默不推送
    if all(done) and success_num == 0 and dup_num > 0:
        log(f"本任务为冗余运行，{dup_num} 个时段均已被另一任务抢到，不重复通知")
        return

    title = f"座位{seat}抢座结果：{success_num + dup_num}/{len(segments)} 个时段成功"
    lines = [f"日期：{day}", f"座位：{seat}", f"尝试次数：{attempts}", ""]
    for i, (start, end) in enumerate(segments):
        if segment_kind.get(i) == "success":
            status = "✅ 成功"
        elif segment_kind.get(i) == "dup":
            status = "✅ 成功（另一任务抢到）"
        else:
            status = f"❌ 失败（{last_msg.get(i, '未知')}）"
        lines.append(f"{start}-{end}  {status}")
    content = "\n\n".join(lines)
    log("===== 最终结果 =====")
    for line in lines:
        log(line)
    push_wechat(cfg["serverchan_key"], title, content)
    if cfg.get("popup"):
        show_popup("抢座结果", "\n".join(lines))

def main():
    parser = argparse.ArgumentParser(description="图书馆座位自动抢座")
    parser.add_argument("mode", nargs="?",
                        choices=["inspect", "dryrun", "run"],
                        default=os.environ.get("GRAB_MODE", "run"))
    args = parser.parse_args()
    cfg = load_config()
    run(cfg, args.mode)

if __name__ == "__main__":
    main()
