# -*- coding: utf-8 -*-
"""
图书馆座位自动抢座脚本（超星座位系统）
功能：模拟登录 -> 提取页面 token -> 计算 enc 签名 -> 定点连抢三个时段 -> 失败重试 -> 弹窗/微信通知

运行模式：
    python grab_seat.py inspect   # 只登录并分析座位页面结构（不提交，安全）
    python grab_seat.py dryrun    # 立即提交“明天”的预约，用于验证 enc 是否正确（未到 20:00 会被拒）
    python grab_seat.py run       # 正式运行：等待到 20:00:00 自动开抢
    python grab_seat.py signin    # 自动签到：等首时段开始后自动签到，窗口内持续重试
    python grab_seat.py signback  # 自动签退：末时段结束前 5 分钟自动退座签退
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
    RESERVELIST_API = ("https://office.chaoxing.com/data/apps/seat/reservelist?"
                       "indexId=0&pageSize=100&type=-1")
    SIGN_API = "https://office.chaoxing.com/data/apps/seat/sign"
    SIGNBACK_API = "https://office.chaoxing.com/data/apps/seat/signback"

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

    # ---- 查询我的预约列表 ----
    def fetch_reservations(self):
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        resp = self.session.get(self.RESERVELIST_API, headers=headers, timeout=15)
        try:
            obj = resp.json()
        except Exception:
            return [], {"success": False, "msg": f"响应非JSON：{resp.text[:100]}"}
        return (obj.get("data") or {}).get("reserveList") or [], obj

    # ---- 签到 / 签退（服务端接口只认预约 id，不校验定位/蓝牙）----
    def sign_action(self, api, reserve_id):
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        resp = self.session.post(api, data={"id": reserve_id},
                                 headers=headers, timeout=15)
        try:
            return resp.json()
        except Exception:
            return {"success": False, "msg": f"响应非JSON：{resp.text[:100]}"}

# ---------------- 预约状态常量 ----------------

RESERVE_STATUS = {0: "待履约", 1: "学习中", 2: "已履约",
                  3: "暂离中", 5: "被监督中", 7: "已取消"}

def ts_to_bj(ms):
    """预约列表中的毫秒时间戳 -> 北京时间 datetime"""
    return dt.datetime.fromtimestamp(int(ms) / 1000, BEIJING)

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
    # 签名盐值一次性失效：同一盐提交第二个时段时返回代码 302，需立即刷新页面取新盐
    if "302" in msg or any(k in msg for k in ("停留过久", "安全验证",
                                             "刷新后再提交", "操作超时")):
        return "salt_expired", msg
    if any(k in msg for k in ("验证", "enc", "签名", "非法", "参数")):
        return "sign", msg
    return "taken", msg or str(result)

def classify_sign(result, action):
    """签到/签退返回分类：ok / already / auth / fail"""
    msg = f'{result.get("msg", "")}{result.get("msg2", "")}'.strip()
    if result.get("success") is True:
        return "ok", msg
    if any(k in msg for k in ("登录", "登陆", "未登录")):
        return "auth", msg
    done_words = (("已签到", "已落座", "重复签到", "学习中") if action == "signin"
                  else ("已退座", "已签退", "已结束", "退座成功", "签退成功"))
    if any(k in msg for k in done_words):
        return "already", msg
    return "fail", msg

# ---------------- 主流程 ----------------

def load_config():
    # 本地 config.json（含真实账号密码，gitignore）；仓库中只有 config.example.json
    cfg_path = os.path.join(HERE, "config.json")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(HERE, "config.example.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖（GitHub Actions 使用，账号密码不写进仓库）
    # 多账号支持：ACCOUNT_IDX=2 时取第二账号凭据（对应第二个座位）
    idx = os.environ.get("ACCOUNT_IDX", "1")
    if idx == "1":
        cfg["username"] = os.environ.get("CHAOXING_USERNAME", cfg["username"])
        cfg["password"] = os.environ.get("CHAOXING_PASSWORD", cfg["password"])
    else:
        cfg["username"] = os.environ.get(
            f"CHAOXING_USERNAME_{idx}", cfg.get(f"username_{idx}", ""))
        cfg["password"] = os.environ.get(
            f"CHAOXING_PASSWORD_{idx}", cfg.get(f"password_{idx}", ""))
        # 兜底：工作流矩阵也可能用通用变量名传入该账号凭据
        if not cfg["username"]:
            cfg["username"] = os.environ.get("CHAOXING_USERNAME", "")
        if not cfg["password"]:
            cfg["password"] = os.environ.get("CHAOXING_PASSWORD", "")
    # 该账号负责的座位号（云端由矩阵任务指定）
    cfg["seat_num"] = os.environ.get("SEAT_NUM", cfg["seat_num"])
    cfg["serverchan_key"] = os.environ.get("SERVERCHAN_KEY", cfg["serverchan_key"])
    if not cfg["username"] or not cfg["password"]:
        sys.exit(f"账号{idx}凭据缺失：请在 config.json 填写或配置对应环境变量/Secrets")
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
        # 提取页面全部脚本与链接，定位“我的预约”入口与专属 JS
        for s in re.findall(r'<script[^>]*src="([^"]+)"', html):
            log(f"SCRIPT: {s}")
        for s2 in set(re.findall(r'href="([^"]+)"', html)):
            log(f"HREF: {s2}")
        # 尝试云端打开“我的预约”候选页面（本地被 WAF 拦，云端 IP 干净）
        for p in ("myReserve", "mine", "my", "myreserve", "myReservations"):
            u = f"https://office.chaoxing.com/front/apps/seat/{p}"
            try:
                rr = grabber.session.get(u, headers=dict(
                    grabber.base_headers,
                    Referer="https://office.chaoxing.com/"), timeout=10)
                body = rr.content.decode("utf-8", "replace")
                log(f"PAGE {p}: {rr.status_code} len={len(body)} "
                    f"{body[:150].strip()}")
            except Exception as ex:
                log(f"PAGE {p}: 失败 {ex}")
        # App 真实扫码链接（third + fidEnc）：持有人应看到签到卡片
        u = ("https://office.chaoxing.com/front/third/apps/seat/code"
             "?id=6287&seatNum=" + str(grabber.cfg["seat_num"]) +
             "&fidEnc=7a29a63e25913d21")
        try:
            rr = grabber.session.get(u, headers=dict(
                grabber.base_headers,
                Referer="https://office.chaoxing.com/"), timeout=12)
            b3 = rr.content.decode("utf-8", "replace")
            log(f"THIRDCODE: {rr.status_code} len={len(b3)}")
            for s in re.findall(r'<script[^>]*src="([^"]+)"', b3):
                log(f"  TCSCRIPT: {s}")
            for tag in re.findall(r"<input[^>]*>", b3):
                log(f"  TCINPUT: {tag[:200]}")
            for kw in ("签到", "签退", "学习中", "违约", "待履约",
                       "暂离", "退座"):
                for mm in re.finditer(r"[^<>\n]{0,60}" + kw +
                                      r"[^<>\n]{0,60}", b3):
                    log(f"  TCKEY: {mm.group(0).strip()[:150]}")
        except Exception as ex:
            log(f"THIRDCODE 失败: {ex}")
        # 座位页对“持有人”会直接渲染预约卡片：提取含状态关键词的行
        for kw in ("预约", "签到", "签退", "学习中", "违约", "待履约",
                   "暂离", "退座", "状态"):
            for m in re.finditer(r"[^<>\n]{0,80}" + kw + r"[^<>\n]{0,80}", html):
                log(f"卡片: {m.group(0).strip()[:120]}")
        # 当前预约实时接口
        hdr = dict(grabber.base_headers, Referer="https://office.chaoxing.com/")
        for tag2, u in (
            ("seat/index", "https://office.chaoxing.com/data/apps/seat/index"),
            ("全部预约", grabber.RESERVELIST_API),
        ):
            try:
                rr = grabber.session.get(u, headers=hdr, timeout=15)
                log(f"【{tag2}】{rr.content.decode('utf-8','replace')[:1500]}")
            except Exception as ex:
                log(f"【{tag2}】查询失败：{ex}")
        # 关键接口：座位维度的预约信息（可能返回签到接口认的真实 reserveId）
        seat = str(grabber.cfg["seat_num"])
        ri_url = ("https://office.chaoxing.com/data/apps/seat/reserve/info"
                  f"?id=6287&seatNum={seat}")
        ri_body = ""
        try:
            rr = grabber.session.get(ri_url, headers=hdr, timeout=15)
            ri_body = rr.content.decode("utf-8", "replace")
            log(f"【reserve/info】len={len(ri_body)}")
            for _off in range(0, len(ri_body), 1400):
                log(f"【ri+{_off}】{ri_body[_off:_off+1400]}")
        except Exception as ex:
            log(f"【reserve/info】查询失败：{ex}")
        # 收集候选 id：reserve/info 里的全部数字 id + 列表 id
        cand_ids = re.findall(r'"(?:id|reserveId)"\s*:\s*(\d+)', ri_body)
        rl = []
        try:
            rl, _o = grabber.fetch_reservations()
            for r in (rl or []):
                if str(r.get("today")) == now_bj().strftime("%Y-%m-%d"):
                    cand_ids.append(str(r.get("id")))
        except Exception:
            pass
        # GET 方式逐个尝试签到（新系统 Web 端为 GET）
        for cid in dict.fromkeys(str(c) for c in cand_ids):
            try:
                sr = grabber.session.get(
                    "https://office.chaoxing.com/data/apps/seat/sign",
                    params={"id": cid}, headers=hdr, timeout=15)
                sb = sr.content.decode("utf-8", "replace")
                log(f"【GET sign id={cid}】{sb[:300]}")
            except Exception as ex:
                log(f"【GET sign id={cid}】失败：{ex}")
        # 签到二维码落地页（侦察其控制器模块，不改变状态）
        _today0 = now_bj().strftime("%Y-%m-%d")
        _rid0 = next((str(r.get("id")) for r in (rl or [])
                      if str(r.get("today")) == _today0), "")
        qurl = ("https://office.chaoxing.com/front/third/apps/seat/index"
                "?fidEnc=7a29a63e25913d21&num=" + seat +
                "&reserveId=" + _rid0 + "&room=6287")
        try:
            qr = grabber.session.get(qurl, headers=hdr, timeout=15)
            qb = qr.content.decode("utf-8", "replace")
            log(f"【QR落地页】{qr.status_code} len={len(qb)}")
            for s in re.findall(r'<script[^>]*src="([^"]+)"', qb):
                log(f"  QSCRIPT: {s}")
            for mm in re.findall(r"[^<>\n]{0,50}(?:签到|签退|违约|学习中)"
                                 r"[^<>\n]{0,50}", qb):
                log(f"  QKEY: {mm.strip()[:130]}")
        except Exception as ex:
            log(f"【QR落地页】失败：{ex}")
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
        for i, (start, end) in enumerate(segments):
            if done[i]:
                continue
            # 签名盐值一次性有效：每个时段提交前都刷新页面取新盐
            res = grabber.refresh_token()
            if not res[0]:
                time.sleep(1); continue
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
            elif kind == "salt_expired":
                log(f"盐值失效，下一时段前自动刷新：{msg}")
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

def sleep_until(target_dt):
    """低频睡到指定北京时间"""
    while True:
        gap = (target_dt - now_bj()).total_seconds()
        if gap <= 0:
            break
        time.sleep(0.05 if gap < 2 else min(1, gap))

# ---------------- 签到 / 签退流程 ----------------

def run_sign(cfg, action):
    grabber = SeatGrabber(cfg)
    grabber.login()
    # 预约列表接口要求先访问座位页建立上下文 Cookie，否则返回空列表
    grabber.fetch_seat_page()

    def todays():
            reserves, raw = grabber.fetch_reservations()
            today = now_bj().date()
            # 新版系统会把连续时段合并成全天单条记录（type=0）；历史记录 type=-1
            mine = [r for r in reserves
                    if str(r.get("seatNum")) == str(cfg["seat_num"])
                    and r.get("type", -1) in (0, -1)
                    and ts_to_bj(r["startTime"]).date() == today]
            mine.sort(key=lambda r: int(r["startTime"]))
            return mine, raw

    mine, raw = todays()
    if not mine:
        log(f"今日没有 {cfg['seat_num']} 号座位的有效预约，{action} 无需执行")
        log(f"接口原始返回：{str(raw)[:200]}")
        return

    seat = cfg["seat_num"]
    key = cfg["serverchan_key"]
    lo, hi = cfg["sign_retry_interval"]

    if action == "signin":
        target = mine[0]
        start = ts_to_bj(target["startTime"])
        deadline = start + dt.timedelta(minutes=cfg["sign_window_min"] - 1)
        if target.get("status") in (1, 2):
            log(f"签到已完成（{RESERVE_STATUS.get(target['status'])}），冗余任务静默退出")
            return
        log(f"首时段 {start:%H:%M} 开始，等待签到窗口...")
        sleep_until(start + dt.timedelta(seconds=2))
        # 晚启动（手动触发/调度延迟，已超过正常窗口）：仍立即尝试，给补救窗口
        late_min = cfg.get("sign_late_window_min", 10)
        if now_bj() >= deadline:
            deadline = now_bj() + dt.timedelta(minutes=late_min)
            log(f"已超过正常签到窗口，进入 {late_min} 分钟补救尝试")
        fail_msg = ""
        while now_bj() < deadline:
            kind, m = classify_sign(grabber.sign_action(grabber.SIGN_API, target["id"]), action)
            fail_msg = m
            if kind in ("ok", "already"):
                log(f"✅ 签到成功：{m}")
                push_wechat(key, f"座位{seat}自动签到成功",
                            f"日期：{now_bj():%Y-%m-%d}\n座位：{seat}\n"
                            f"签到时间：{now_bj():%H:%M:%S}\n状态：学习中 ✅")
                return
            if kind == "auth":
                log("登录态失效，重新登录..."); grabber.login(); continue
            log(f"签到暂未成功：{m}")
            cur, _ = todays()
            rec = next((r for r in cur if r["id"] == target["id"]), None)
            if rec and rec.get("status") in (1, 2):
                log("✅ 预约状态已变为学习中，签到成功")
                push_wechat(key, f"座位{seat}自动签到成功",
                            f"日期：{now_bj():%Y-%m-%d}\n座位：{seat}\n"
                            f"签到时间：{now_bj():%H:%M:%S}\n状态：学习中 ✅")
                return
            time.sleep(random.uniform(lo, hi))
        log("⚠️ 签到窗口用尽仍未成功")
        push_wechat(key, f"⚠️座位{seat}自动签到失败，请立即手动签到！",
                    f"最后一次返回：{fail_msg}\n\n"
                    f"请立刻打开学习通，扫描桌上二维码点击“签到”"
                    f"（{deadline:%H:%M} 前），逾期记违规！")
        return

    # ---- signback 自动签退 ----
    target = mine[-1]
    end = ts_to_bj(target["endTime"])
    begin_at = end - dt.timedelta(minutes=cfg["signback_before_min"])
    deadline = end + dt.timedelta(minutes=cfg["signback_after_min"])
    if target.get("status") == 2:
        log("今日已履约（已签退），冗余任务静默退出")
        return
    log(f"等待到 {begin_at:%H:%M} 开始签退...")
    sleep_until(begin_at)
    # 极端调度延迟超过正常窗口：仍给补救时间
    if now_bj() >= deadline:
        deadline = now_bj() + dt.timedelta(minutes=cfg.get("sign_late_window_min", 10))
        log(f"已超过正常签退窗口，进入 {cfg.get('sign_late_window_min', 10)} 分钟补救尝试")
    fail_msg = ""
    while now_bj() < deadline:
        cur, _ = todays()
        rec = next((r for r in cur if r["id"] == target["id"]), None)
        # 末时段若仍是待履约，先补一次签到进入学习中，再签退
        if rec and rec.get("status") == 0:
            log("末时段仍为待履约，先补签到...")
            grabber.sign_action(grabber.SIGN_API, target["id"])
        kind, m = classify_sign(grabber.sign_action(grabber.SIGNBACK_API, target["id"]), action)
        fail_msg = m
        if kind in ("ok", "already"):
            log(f"✅ 签退成功：{m}")
            push_wechat(key, f"座位{seat}自动签退成功",
                        f"日期：{now_bj():%Y-%m-%d}\n座位：{seat}\n"
                        f"签退时间：{now_bj():%H:%M:%S}\n今日履约完成，无违规 🎉")
            return
        if kind == "auth":
            log("登录态失效，重新登录..."); grabber.login(); continue
        log(f"签退暂未成功：{m}")
        cur, _ = todays()
        rec = next((r for r in cur if r["id"] == target["id"]), None)
        if rec and rec.get("status") == 2:
            log("✅ 预约状态已变为已履约，签退成功")
            push_wechat(key, f"座位{seat}自动签退成功",
                        f"日期：{now_bj():%Y-%m-%d}\n座位：{seat}\n"
                        f"签退时间：{now_bj():%H:%M:%S}\n今日履约完成，无违规 🎉")
            return
        time.sleep(random.uniform(lo, hi))
    log("⚠️ 签退窗口用尽仍未成功")
    push_wechat(key, f"⚠️座位{seat}自动签退失败，请立即手动签退！",
                f"最后一次返回：{fail_msg}\n\n"
                f"请立刻打开学习通，扫描桌上二维码点击“退座/签退”，"
                f"离开不签退记违规！")

def main():
    parser = argparse.ArgumentParser(description="图书馆座位自动抢座")
    parser.add_argument("mode", nargs="?",
                        choices=["inspect", "dryrun", "run", "signin", "signback"],
                        default=os.environ.get("GRAB_MODE", "run"))
    args = parser.parse_args()
    cfg = load_config()
    if args.mode in ("signin", "signback"):
        run_sign(cfg, args.mode)
    else:
        run(cfg, args.mode)

if __name__ == "__main__":
    main()
