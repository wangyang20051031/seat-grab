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
    INDEX_API = "https://office.chaoxing.com/data/apps/seat/index"
    SIGN_API = "https://office.chaoxing.com/data/apps/seat/sign"
    SIGNBACK_API = "https://office.chaoxing.com/data/apps/seat/signback"
    CANCEL_API = "https://office.chaoxing.com/data/apps/seat/cancel"

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
            # 始终返回三元组，保持返回类型一致（旧代码 return False 会让
            # 调用方三元组解包 / res[0] 下标崩溃）。html 仍带回，供侦察分析。
            return False, html, info
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

    def fetch_index(self):
        """座位门户数据：curReserves=进行中/待履约（可签到），nearReserves=最近"""
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        resp = self.session.get(
            self.INDEX_API,
            params={"fidEnc": self.cfg.get("fid_enc",
                                           "7a29a63e25913d21")},
            headers=headers, timeout=15)
        return resp.json()

    # ---- 签到 / 签退（Web 端为 GET，参数 id；服务端不校验定位/蓝牙）----
    def sign_action(self, api, reserve_id):
        headers = dict(self.base_headers, Referer="https://office.chaoxing.com/")
        resp = self.session.get(api, params={"id": reserve_id},
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
    # ACCOUNT_IDX 默认为 1（当前仅单账号1/263座）；多账号扩展点见 run_renew_all
    idx = os.environ.get("ACCOUNT_IDX", "1")
    cfg["username"] = os.environ.get("CHAOXING_USERNAME", cfg["username"])
    cfg["password"] = os.environ.get("CHAOXING_PASSWORD", cfg["password"])
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
        if not ok:
            log("⚠️ 未取到签名盐值——通常是该账号当前持有活跃预约，座位页"
                "渲染成“预约卡片”而非抢座表单。仍保存页面并继续侦察...")
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
        idx_body = ""
        for tag2, u, meth, d2 in (
            ("seat/index",
             "https://office.chaoxing.com/data/apps/seat/index",
             "GET", {"fidEnc": "7a29a63e25913d21"}),
            ("全部预约", grabber.RESERVELIST_API, "GET", None),
        ):
            try:
                rr = grabber.session.get(u, params=d2, headers=hdr,
                                         timeout=15)
                body3 = rr.content.decode('utf-8', 'replace')
                if tag2 == "seat/index":
                    idx_body = body3
                log(f"【{tag2}】len={len(body3)}")
                for off in range(0, len(body3), 1400):
                    log(f"  <{off}>{body3[off:off+1400]}")
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
        # 收集候选 id：index.curReserves + reserve/info + 今日预约（只展示，不调用）
        cand = []
        try:
            jidx = json.loads(idx_body)
            for cr in (jidx.get("data") or {}).get("curReserves") or []:
                if str(cr.get("id")):
                    cand.append(str(cr.get("id")))
        except Exception:
            pass
        cand += re.findall(r'"(?:id|reserveId)"\s*:\s*(\d+)', ri_body)
        rl = []
        try:
            rl, _o = grabber.fetch_reservations()
            for r in (rl or []):
                if str(r.get("today")) == now_bj().strftime("%Y-%m-%d"):
                    cand.append(str(r.get("id")))
        except Exception:
            pass
        log("候选签到 id（inspect 只读，不实际调用）："
            + ", ".join(dict.fromkeys(str(c) for c in cand)))
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
            # 整个落地页 base64 输出（无换行，本地解码还原）
            import base64 as _b64m
            b64 = _b64m.b64encode(qb.encode("utf-8")).decode()
            log(f"【QRB64 len={len(b64)}】")
            for off in range(0, len(b64), 1200):
                log(f"QRB64+{off}:{b64[off:off+1200]}")
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

# ---------------- 进馆模式 ----------------

def run_enter(cfg):
    """门禁新政（学习中不可刷脸入馆）专用流程：
    - 学习中/暂离/被监督（1/3/5）：全部退座；
    - 待履约（0）：一律【保留】（若已进入签到窗可直接刷脸，就不动它）；
    - 需要新场时只约“当前半点场 ~ 下一条待履约起点”（端点相接不算冲突），
      没有后续待履约才约到闭馆；
    完成后保持【待履约】不签到，用户刷脸进门后再自行签到。仅账号1 / 263 座。"""
    grabber = SeatGrabber(cfg)
    grabber.login()

    key = cfg.get("serverchan_key")
    seat = str(cfg["seat_num"])
    today_s = now_bj().strftime("%Y-%m-%d")
    RELEASED = (2, 7, 8)          # 已履约 / 已取消 / 失效
    SIGN_WIN = dt.timedelta(minutes=20)   # 与服务端 signDuration/preSignDuration 对齐

    def hm(ts):
        return ts_to_bj(ts).strftime("%H:%M")

    def finish_ok(title, content):
        log("===== 进馆模式完成 =====")
        for line in content.split("\n"):
            if line.strip():
                log(line)
        push_wechat(key, title, content)
        if cfg.get("popup"):
            show_popup(title, content)

    def finish_fail(reason, state_note=""):
        title = f"⚠️进馆模式未完成（{seat}座）"
        content = (f"座位：{seat}\n原因：{reason}\n"
                   + (f"\n当前状态：{state_note}\n" if state_note else "")
                   + "\n请手动操作：我的预约→退座，随后立即重约当前半点场，"
                     "显示“待履约”后刷脸进门。")
        log(title + " " + reason)
        push_wechat(key, title, content)
        if cfg.get("popup"):
            show_popup(title, content)

    def gather_today():
        """聚合 seat/index 与 reservelist 的今日本座位记录，按 id 去重。
        实测两来源都可能缺字段甚至整接口返回空（2026-10-15 实测
        reservelist 为空、仅 index 有记录），故日期/座位过滤都要容错。"""
        merged = {}
        today_date = now_bj().date()

        def add(r):
            try:
                rid = str(r.get("id") or "")
                if not rid:
                    return
                day_ok = str(r.get("today")) == today_s
                if not day_ok:
                    try:
                        day_ok = ts_to_bj(r["startTime"]).date() == today_date
                    except Exception:
                        day_ok = False
                if not day_ok:
                    return
                # enter 单账号运行；记录缺 seatNum 时（部分接口不带）视为本账号
                sn = r.get("seatNum")
                if sn is None or str(sn) == seat:
                    merged[rid] = r
            except Exception:
                pass

        try:
            data = (grabber.fetch_index() or {}).get("data") or {}
            for bucket in ("curReserves", "nearReserves"):
                for r in data.get(bucket) or []:
                    add(r)
        except Exception as e:
            log(f"index 查询失败：{e}")
        try:
            rl, _ = grabber.fetch_reservations()
            for r in rl or []:
                add(r)
        except Exception as e:
            log(f"reservelist 查询失败：{e}")
        return list(merged.values())

    def wait_released(rid, timeout=30):
        """轮询直到指定记录进入已释放状态。两来源都查——实测 reservelist
        可能整体为空，只能靠 seat.index 的 nearReserves 确认状态。"""
        deadline = now_bj() + dt.timedelta(seconds=timeout)
        while now_bj() < deadline:
            for r in gather_today():
                if (str(r.get("id")) == str(rid)
                        and r.get("status") in RELEASED):
                    return True
            time.sleep(1.5)
        return False

    def do_signback(rid):
        """退座一条记录，含登录态失效重试；返回 (是否成功, 信息)"""
        try:
            res = grabber.sign_action(grabber.SIGNBACK_API, rid)
        except requests.RequestException as e:
            return False, f"网络异常：{e}"
        kind, msg = classify_sign(res, "signback")
        if kind == "auth":
            grabber.login()
            try:
                res = grabber.sign_action(grabber.SIGNBACK_API, rid)
            except requests.RequestException as e:
                return False, f"重试网络异常：{e}"
            kind, msg = classify_sign(res, "signback")
        if kind in ("ok", "already"):
            return True, msg
        return False, msg

    log("===== 进馆模式：退学习中 → 保留待履约 → 约相邻当前场（不签到）=====")

    # 1. 收集本座位今天的全部活跃记录
    actives = [r for r in gather_today() if r.get("status") in (0, 1, 3, 5)]
    learning = [r for r in actives if r.get("status") in (1, 3, 5)]
    pendings = [r for r in actives if r.get("status") == 0]
    for r in actives:
        log(f"活跃记录 id={r['id']} {hm(r['startTime'])}-{hm(r['endTime'])} "
            f"状态={RESERVE_STATUS.get(r.get('status'), r.get('status'))}")

    # 2. 退掉全部学习中/暂离/被监督记录（它们才是刷脸被拦的原因）
    signed_back = 0
    for r in learning:
        rid = r["id"]
        log(f"退座 id={rid} {hm(r['startTime'])}-{hm(r['endTime'])} …")
        ok, msg = do_signback(rid)
        if not ok:
            others = len(learning) - signed_back - 1
            note = (f"已退 {signed_back} 条，仍有 {others + 1} 条学习中"
                    f"（{'本条及后续' if others else '本条'}未退成）")
            finish_fail(f"退座失败：{msg}", note)
            return
        if not wait_released(rid, timeout=30):
            finish_fail("退座请求已发出，但30秒内未查到“已履约”，为避免误约已停止",
                        f"已成功退座 {signed_back} 条")
            return
        signed_back += 1
        log(f"id={rid} 已退座（已履约）")
        time.sleep(0.8)

    # 退座后重新拉取一次，待履约列表以最新为准
    pendings = [r for r in gather_today() if r.get("status") == 0]

    # 3. 是否已存在“现在就能刷脸”的待履约（开场前20分 ~ 结束前）
    now = now_bj()
    enterable = [r for r in pendings
                 if ts_to_bj(r["startTime"]) - SIGN_WIN <= now
                 < ts_to_bj(r["endTime"])]
    if enterable:
        enterable.sort(key=lambda r: int(r["startTime"]))
        r = enterable[0]
        title = f"🚪进馆模式就绪：{seat}座 {hm(r['startTime'])}-{hm(r['endTime'])} 待履约"
        lines = [f"座位：{seat}",
                 f"当前场：{hm(r['startTime'])}-{hm(r['endTime'])}（待履约）", ""]
        if signed_back:
            lines.append(f"已退掉 {signed_back} 条学习中记录，原有待履约保留未动。")
        lines += ["请【立刻刷脸进馆】。",
                  "进门后到座位点“一键签到”（开场后20分钟内必须签上）。",
                  "今晚自动签退照常，无需其他操作。"]
        finish_ok(title, "\n".join(lines))
        return

    # 4. 计算当前半点场起点（刚过去的半点超过20分钟则约下一个半点）
    start_dt = now.replace(minute=30 if now.minute >= 30 else 0,
                           second=0, microsecond=0)
    if (now - start_dt).total_seconds() > 20 * 60:
        start_dt += dt.timedelta(minutes=30)
    open_s = cfg["segments"][0][0]
    close_s = cfg.get("daily_close", cfg["segments"][-1][1])
    opening = now.replace(
        hour=int(open_s[:2]), minute=int(open_s[3:5]), second=0, microsecond=0)
    closing = now.replace(
        hour=int(close_s[:2]), minute=int(close_s[3:5]), second=0, microsecond=0)
    if start_dt < opening:
        start_dt = opening
    if start_dt >= closing:
        finish_fail(f"已过或接近闭馆（{close_s}），无法再约当天场次",
                    f"已退座 {signed_back} 条")
        return
    start_s = start_dt.strftime("%H:%M")

    # 终点：下一条未来待履约的起点（相邻不冲突，完整保留后续预约）；没有则约到闭馆
    future_starts = [ts_to_bj(r["startTime"]) for r in pendings
                     if ts_to_bj(r["startTime"]) > start_dt]
    end_dt = min(future_starts + [closing]) if future_starts else closing
    # 馆方新规单次预约≤4小时（reserveDuration=4.0）：超出截断，之后由续约任务自动延长
    cap_end = start_dt + dt.timedelta(hours=4)
    capped = False
    if end_dt > cap_end:
        end_dt = cap_end
        capped = True
    end_s = end_dt.strftime("%H:%M")
    log(f"计划新场：{start_s}-{end_s}"
        + ("（与后续待履约相邻）" if future_starts and not capped
           else "（单条≤4h截断，稍后自动续约）" if capped else "（约到闭馆）"))

    # 5. 刷新盐值并提交（盐一次性，每次尝试都刷新；只认 success）
    last_msg = "未知"
    booked = False
    for attempt in range(1, 13):
        tok = grabber.refresh_token()
        if not tok[0]:
            time.sleep(1.2)
            continue
        try:
            result = grabber.submit_segment(start_s, end_s, today_s)
        except requests.RequestException as e:
            last_msg = f"网络异常：{e}"
            log(f"第{attempt}次提交网络异常，重试")
            time.sleep(2)
            continue
        kind, msg = classify(result)
        last_msg = msg
        if kind == "success":
            booked = True
            log(f"✅ {start_s}-{end_s} 预约完成：{msg or '（服务端返回成功）'}")
            # 记录成功响应中可能携带的新记录 id，供回查精确定位
            try:
                d = result.get("data")
                if isinstance(d, dict):
                    log(f"成功响应 data：{d}")
            except Exception:
                pass
            break
        if kind == "auth":
            log("登录态失效，重新登录…")
            grabber.login()
        else:
            log(f"第{attempt}次预约未成功（{kind}）：{msg}")
        time.sleep(2)

    if not booked:
        finish_fail(f"重约 {start_s}-{end_s} 未成功：{last_msg}",
                    f"已退座 {signed_back} 条，请立刻手动补约当前时段")
        return

    # 6. 确认新场为待履约（起止时间都要对上）。
    # 实测服务端列表传播延迟可达 16~60 秒（2026-10-15 两次复现：接口成功后
    # 15 秒内 curReserves 查无记录，69 秒后可见），窗口给到 60 秒。
    ready = None
    became_learning = None
    deadline = now_bj() + dt.timedelta(seconds=60)
    while now_bj() < deadline:
        for r in gather_today():
            if hm(r["startTime"]) == start_s and hm(r["endTime"]) == end_s:
                if r.get("status") == 0:
                    ready = r
                elif r.get("status") in (1, 3, 5):
                    became_learning = r
                break
        if ready or became_learning:
            break
        time.sleep(2)

    if became_learning and not ready:
        # 同起止新场已变成学习中（期间被手动/其他任务签到）：刷脸仍会被拦
        finish_fail(f"新场 {start_s}-{end_s} 已是学习中（疑似刚被签到），"
                    f"刷脸仍会被拦。如确实要进门，请再点一次进馆模式；"
                    f"若已在馆内则无需操作。", f"已退座 {signed_back} 条")
        return

    if not ready:
        # 接口明确返回 success，仅列表暂未刷新：按成功告知，避免用户重复点击
        # 把已签到的场连锁退掉（2026-10-15 事故链）
        title = f"🚪进馆模式已提交：{seat}座 {start_s}-{end_s}"
        lines = [f"座位：{seat}", f"新场：{start_s}-{end_s}",
                 "预约接口已返回成功，但列表刷新有延迟（约1分钟），暂未查到记录。",
                 "", "请打开学习通“我的预约”确认该场显示“待履约”：",
                 "· 若有待履约场 → 直接刷脸进门，进门后签到；",
                 "· 若没有 → 手动约当前半点场。",
                 "**不要立刻反复点进馆模式**，以免重复退座。"]
        finish_ok(title, "\n".join(lines))
        return

    title = f"🚪进馆模式就绪：{seat}座 {start_s}-{end_s} 待履约"
    lines = [f"座位：{seat}", f"新场：{start_s}-{end_s}（待履约）", ""]
    if signed_back:
        lines.append(f"已退掉 {signed_back} 条学习中记录。")
    if future_starts:
        lines.append("原有后续待履约保留未动。")
    lines += ["请【立刻刷脸进馆】。",
              "进门后到座位点“一键签到”（开场后20分钟内必须签上）。",
              "今晚自动签退照常，无需其他操作。"]
    finish_ok(title, "\n".join(lines))

# ---------------- 续约流程（馆方新规：单条≤4h、每日仅1条）----------------
# 服务端“隐式续约”（2026-10-16 实测确认）：对学习中记录提交
# “起点 = 当前 endTime”的紧邻时段，同一条 seatReserve 的 endTime 被直接
# 延长（当天 08:30 的记录一路续成 08:30-22:00，duration=13.5）：
# 不新增记录、不受单次 4 小时上限、不占用每日 1 条名额。
# 触发窗口：结束前 renewalDuration=60 分钟内（实测结束前 53 分钟可续）。

RENEW_LEAD_MIN = 60        # 距结束 ≤60 分钟才进入续约窗口
RENEW_WARN_MIN = 30        # 距结束 ≤30 分钟仍未续成才微信告警（防冗余班刷屏）
RENEW_STEP = dt.timedelta(hours=4)


def run_renew_all(cfg):
    """续约巡检：当前仅账号1/263座。多账号时顺序处理、互不影响。"""
    accounts = []
    # 账号1：主账号（默认）
    u, p = cfg.get("username", ""), cfg.get("password", "")
    seat = cfg.get("seat_num", "")
    if u and p and seat:
        accounts.append(("1", u, p, str(seat)))
    # 多账号扩展点（预留）：if cfg.get("username_2") ...
    for idx, u, p, seat in accounts:
        sub = dict(cfg)
        sub["username"], sub["password"], sub["seat_num"] = u, p, seat
        try:
            run_renew(sub, idx)
        except Exception as e:
            log(f"账号{idx}（{seat}座）续约巡检异常：{e}")
        time.sleep(1)


def run_renew(cfg, idx="?"):
    """单账号续约：有学习中/暂离记录且距结束 ≤60 分钟时，提交紧邻时段延长。
    未到窗口 / 无记录 / 已续到闭馆 → 静默（多班冗余靠这个幂等）。"""
    grabber = SeatGrabber(cfg)
    grabber.login()
    grabber.fetch_seat_page()

    seat = str(cfg["seat_num"])
    key = cfg.get("serverchan_key")
    tag = f"账号{idx}/{seat}座"
    today_date = now_bj().date()
    close_s = cfg.get("daily_close", "22:00")
    closing = now_bj().replace(
        hour=int(close_s[:2]), minute=int(close_s[3:5]),
        second=0, microsecond=0)

    def gather():
        """聚合 index.curReserves/nearReserves + reservelist 的今日记录。"""
        merged = {}

        def put(r):
            try:
                if ts_to_bj(r["startTime"]).date() == today_date:
                    merged[str(r.get("id"))] = r
            except Exception:
                pass

        try:
            data = (grabber.fetch_index() or {}).get("data") or {}
            for bucket in ("curReserves", "nearReserves"):
                for r in data.get(bucket) or []:
                    put(r)
        except Exception:
            pass
        try:
            for r in (grabber.fetch_reservations()[0] or []):
                put(r)
        except Exception:
            pass
        return list(merged.values())

    def active_record():
        """取今天结束时间最晚的一条学习中/暂离/被监督记录。"""
        recs = [r for r in gather() if r.get("status") in (1, 3, 5)]
        if not recs:
            return None
        recs.sort(key=lambda r: int(r["endTime"]))
        return recs[-1]

    rec = active_record()
    if rec is None:
        log(f"{tag} 无学习中/暂离记录，静默跳过")
        return

    end = ts_to_bj(rec["endTime"])
    start_s = end.strftime("%H:%M")
    delta_min = (end - now_bj()).total_seconds() / 60
    status_s = RESERVE_STATUS.get(rec.get("status"), rec.get("status"))
    log(f"{tag} 记录 id={rec['id']}（{status_s}）结束 {start_s}，"
        f"距结束 {delta_min:.0f} 分钟")

    if delta_min < -5:
        log("记录已过结束时间，跳过")
        return
    if end >= closing:
        log(f"已续到闭馆 {close_s}，无需续约")
        return
    if delta_min > RENEW_LEAD_MIN:
        log("未进入续约窗口（结束前60分钟内才受理），静默跳过")
        return

    new_end = min(end + RENEW_STEP, closing)
    end_s = new_end.strftime("%H:%M")
    day_s = today_date.strftime("%Y-%m-%d")
    log(f"准备续约：{start_s}-{end_s}（同一条记录延长，不新增预约）")

    def already_extended():
        """前一个冗余班可能已续成：重新查到的 endTime 已达目标即视为完成。"""
        cur = active_record()
        try:
            return bool(cur) and ts_to_bj(cur["endTime"]) >= \
                new_end - dt.timedelta(minutes=1)
        except Exception:
            return False

    def notify_success():
        title = f"✅座位{seat}已自动续约至 {end_s}"
        content = [f"座位：{seat}（{status_s}）",
                   f"原结束 {start_s} 的预约已【同一条记录】延长到 {end_s}，"
                   "不占用每天1条名额。",
                   ""]
        if new_end < closing:
            content.append(f"之后约 {end_s} 前一小时会自动再续，直到 {close_s}。")
        else:
            content.append(f"今天已覆盖到闭馆 {close_s}，今晚自动签退照常，无需任何操作。")
        if rec.get("status") == 5:
            content.append("注意：该记录处于“被监督”状态，续约结果请在学习通内确认。")
        push_wechat(key, title, "\n".join(content))

    last_msg = "未知"
    unit_blocked = False
    for attempt in (1, 2, 3):
        ok, html, _info = grabber.refresh_token()
        if not ok:
            # 账号2 实测被馆方白名单剔除：盐页 500「该应用只允许本单位用户使用」
            page = html if isinstance(html, str) else ""
            if "只允许本单位" in page or "logOutShow" in page:
                unit_blocked = True
                log("盐页提示“该应用只允许本单位用户使用”——账号被馆方限制，"
                    "脚本无法续约，跳过（不告警）")
                return
            last_msg = "取不到签名盐值"
            log(f"第{attempt}次取盐失败，重新登录后重试")
            grabber.login()
            time.sleep(1.5)
            continue
        try:
            result = grabber.submit_segment(start_s, end_s, day_s)
        except requests.RequestException as e:
            last_msg = f"网络异常：{e}"
            log(f"第{attempt}次提交网络异常，重试")
            time.sleep(2)
            continue
        kind, msg = classify(result)
        last_msg = msg or kind
        if kind == "success":
            # 严格校验：同一记录 id 且 endTime 已延长（响应缺字段时回查列表）
            rid_same = extended = False
            d = result.get("data")
            if isinstance(d, dict):
                sr = d.get("seatReserve") or {}
                rid_same = str(sr.get("id")) == str(rec["id"])
                try:
                    extended = ts_to_bj(sr["endTime"]) >= \
                        new_end - dt.timedelta(minutes=1)
                except Exception:
                    pass
            if rid_same and extended:
                log(f"✅ 续约成功，记录 id={rec['id']} 已延长到 {end_s}")
                notify_success()
                return
            log(f"返回成功，回查列表确认（rid_same={rid_same}）…")
            time.sleep(3)
            if already_extended():
                log(f"✅ 回查确认已延长到 {end_s}")
                notify_success()
                return
            last_msg = "返回成功但未查到延长后的记录"
        elif kind == "dup":
            # 冗余班重复提交：若记录已延长则幂等静默（成功通知由先成功的班发过）
            time.sleep(2)
            if already_extended():
                log(f"已由前一冗余班续约到 {end_s}，静默跳过")
                return
            last_msg = msg or "返回冲突但记录未延长"
        elif kind == "auth":
            log("登录态失效，重新登录…")
            grabber.login()
        else:
            log(f"第{attempt}次续约未成功（{kind}）：{msg}")
        time.sleep(2)

    log(f"⚠️ {tag} 续约未成功：{last_msg}")
    # 只剩 ≤30 分钟才告警：前序班次还有补救机会，避免一天多条失败推送
    if not unit_blocked and \
            (end - now_bj()).total_seconds() / 60 <= RENEW_WARN_MIN:
        push_wechat(
            key, f"⚠️座位{seat}自动续约暂未成功，请留意",
            f"座位：{seat}（{status_s}）\n当前结束时间：{start_s}\n"
            f"最后返回：{last_msg}\n\n"
            "脚本后续巡检仍会重试。若临近结束仍未收到“已自动续约”通知，\n"
            "请打开学习通→座位预约→我的预约，确认记录结束时间，\n"
            "必要时手动在座位页续约，以免记录到期释放座位。")

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
    # 预约/门户接口要求先访问座位页建立上下文 Cookie，否则返回空
    grabber.fetch_seat_page()

    seat = str(cfg["seat_num"])
    room = str(cfg["room_id"])
    key = cfg["serverchan_key"]
    lo, hi = cfg["sign_retry_interval"]

    def cur_records():
        """聚合三个来源的本账号“今日”记录：index.curReserves /
        index.nearReserves / reservelist。接口只返回当前登录账号本人的预约，
        故不再按座位过滤——由调用方按“签到窗口”决定签哪条，这样一个账号
        今天有多条记录（如早上一条已失效 + 晚上一条待签）也能各归各处理。"""
        today = now_bj().date()
        merged = {}

        def put(r):
            try:
                if ts_to_bj(r["startTime"]).date() == today:
                    merged[str(r.get("id"))] = r
            except Exception:
                pass

        try:
            idx = grabber.fetch_index()
            data = idx.get("data") or {}
            for bucket in ("curReserves", "nearReserves"):
                for r in (data.get(bucket) or []):
                    put(r)
        except Exception:
            pass
        try:
            reserves, _r = grabber.fetch_reservations()
            for r in reserves:
                put(r)
        except Exception:
            pass
        return list(merged.values())

    def day_records():
        """reservelist 中本账号“今日”全部记录（不按座位过滤）。
        预约提交后夜间即存在，用于确定今天要签的目标。"""
        reserves, raw = grabber.fetch_reservations()
        today = now_bj().date()
        recs = [r for r in reserves
                if ts_to_bj(r["startTime"]).date() == today]
        recs.sort(key=lambda r: int(r["startTime"]))
        return recs, raw

    if action == "signin":
        # 服务端真实签到窗口：开始前 20 分钟 ~ 开始后 20 分钟
        swing = cfg["sign_window_min"] - 10
        late = cfg.get("sign_late_window_min", 10)
        fail_msg = ""
        warned = False
        attempted = False   # 本任务是否实际发起过签到请求（用于区分“我签成的”与“别的班次签的”）

        def pick_target():
            """今日最早一条“仍需签到（status 0/3/5）”的记录。
            选择时跳过 1/2/7/8——修复旧逻辑两个问题：①只跳 7/8 且要求窗口
            未关，会漏掉状态 0 的新预约；②兜底里挑中“已履约(2)”旧记录报
            假成功。是否到窗口由后面 open_at 等待 / 补救逻辑负责。"""
            for r in sorted(day_records()[0],
                            key=lambda x: int(x["startTime"])):
                if r.get("status") in (0, 3, 5):
                    return r
            return None

        target = pick_target()
        if target is None:
            # reservelist 偶尔滞后，再用三来源聚合确认今日是否真有预约
            live = [r for r in cur_records() if r.get("status") in (0, 3, 5)]
            if live:
                target = sorted(live, key=lambda x: int(x["startTime"]))[0]
            elif cur_records():
                _all = cur_records()
                if any(r.get("status") in (1, 2) for r in _all):
                    log("今日预约已签到/履约，无待签到记录，静默退出")
                else:
                    log("今日预约均已失效/取消，无待签到记录，静默退出")
                return
            else:
                # 今日完全无记录：定时任务兜底，按配置首时段等一个窗口
                seg0 = cfg["segments"][0]
                hh, mm = (seg0[0] if isinstance(seg0, (list, tuple))
                          else seg0).split(":")[:2]
                start0 = now_bj().replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0)
                close_at0 = start0 + dt.timedelta(minutes=swing)
                log(f"未检测到今日预约，按配置首时段 {start0:%H:%M} 等待...")
                sleep_until(start0 - dt.timedelta(minutes=swing))
                while now_bj() < close_at0 and target is None:
                    target = pick_target()
                    if target is None:
                        time.sleep(random.uniform(lo, hi))
                if target is None:
                    log("窗口内仍未检测到今日预约，静默退出（不发误报告警）")
                    return

        start = ts_to_bj(target["startTime"])
        target_id = target.get("id")
        open_at = start - dt.timedelta(minutes=swing)
        close_at = start + dt.timedelta(minutes=swing)
        log(f"目标 {start:%H:%M}，签到窗口 {open_at:%H:%M}~{close_at:%H:%M}")
        sleep_until(open_at)
        # 极端调度延迟：仍给补救时间
        if now_bj() >= close_at:
            close_at = now_bj() + dt.timedelta(minutes=late)
            log(f"已超过正常窗口，进入 {late} 分钟补救尝试")

        while now_bj() < close_at:
            # 每轮取该目标最新状态（可能已被冗余任务签上）
            latest = next((r for r in cur_records()
                           if str(r.get("id")) == str(target_id)), None)
            r = latest or target
            st = r.get("status")
            this_seat = r.get("seatNum", seat)
            if st in (1, 2):
                if attempted:
                    log("✅ 预约已是学习中，签到成功")
                    push_wechat(key, f"座位{this_seat}自动签到成功",
                                f"日期：{now_bj():%Y-%m-%d}\n座位：{this_seat}\n"
                                f"签到时间：{now_bj():%H:%M:%S}\n状态：学习中 ✅")
                else:
                    log("ℹ️ 已被其他冗余班次签到，本任务静默退出（不重复推送）")
                return
            if st in (0, 3, 5):
                attempted = True
                kind, m = classify_sign(
                    grabber.sign_action(grabber.SIGN_API, r["id"]), action)
                fail_msg = m
                if kind in ("ok", "already"):
                    log(f"✅ 签到成功：{m}")
                    push_wechat(key, f"座位{this_seat}自动签到成功",
                                f"日期：{now_bj():%Y-%m-%d}\n座位：{this_seat}\n"
                                f"签到时间：{now_bj():%H:%M:%S}\n状态：学习中 ✅")
                    return
                if kind == "auth":
                    log("登录态失效，重新登录...")
                    grabber.login()
                    continue
                log(f"签到暂未成功：{m}")
            # 提前预警：开始 +10 分钟仍未确认，立即通知人工并继续重试
            if not warned and now_bj() >= start + dt.timedelta(minutes=10):
                warned = True
                push_wechat(key, f"⚠️座位{this_seat}自动签到暂未确认，请准备手动！",
                            f"当前时间：{now_bj():%H:%M:%S}\n最后返回：{fail_msg}\n\n"
                            "脚本仍在自动重试。请准备：学习通 → 座位预约 → "
                            "我的预约 → 点击“签到”。")
            time.sleep(random.uniform(lo, hi))

        # 窗口用尽：最后确认一次
        if any(str(r.get("id")) == str(target_id)
               and r.get("status") in (1, 2) for r in cur_records()):
            if attempted:
                log("✅ 最终确认：已是学习中，补推成功通知")
                push_wechat(key, f"座位{seat}自动签到成功",
                            f"日期：{now_bj():%Y-%m-%d}\n座位：{seat}\n"
                            f"签到时间：{now_bj():%H:%M:%S}\n状态：学习中 ✅")
            else:
                log("ℹ️ 已被其他冗余班次签到，静默退出")
            return
        log("⚠️ 签到窗口用尽仍未成功")
        push_wechat(key, f"⚠️座位{seat}自动签到失败，请立即手动签到！",
                    f"最后一次返回：{fail_msg}\n\n"
                    "请立刻打开学习通扫描桌上二维码点击“签到”，逾期记违规！")
        return

    # ---- signback 自动签退 ----
    NEED_BACK = (0, 1, 3, 5)   # 待履约/学习中/暂离/被监督 → 仍需签退

    def pick_signback():
        """今日“仍需签退”、结束时间最晚的记录。跳过 2/7/8——修复：同一
        开始时间有多条记录（旧的已履约 status=2 + 新的学习中 status=1）时，
        旧代码 recs[-1] 取到已履约记录，立刻误判“已签退”而漏掉新记录。"""
        cands = [r for r in day_records()[0]
                 if r.get("status") in NEED_BACK]
        cands += [r for r in cur_records()
                  if r.get("status") in NEED_BACK]
        if not cands:
            return None
        # 去重后按 endTime 取最晚，平局用 startTime、id 兜底
        uniq = {str(r.get("id")): r for r in cands}
        return max(uniq.values(),
                   key=lambda x: (int(x["endTime"]), int(x["startTime"]),
                                  int(x.get("id") or 0)))

    recs, raw = day_records()
    target = pick_signback()
    seen = bool(target)   # 是否已从任一来源确认“今日确有预约”
    if target is None:
        if recs:
            # 今天有记录，但没有一条仍需签退：按其终态给出准确结论
            if any(r.get("status") == 2 for r in recs):
                log("今日预约均已履约（已签退），冗余任务静默退出")
            elif all(r.get("status") in (7, 8) for r in recs):
                log("今日预约均已取消/失效，无需签退，静默退出")
            else:
                log("今日预约均已到终态，无需签退，静默退出")
            return
        # 确实无记录：用配置末时段结束时间兜底，窗口内继续等
        segN = cfg["segments"][-1]
        end_s = segN[1] if isinstance(segN, (list, tuple)) else segN
        hh, mm = end_s.split(":")[:2]
        end = now_bj().replace(hour=int(hh), minute=int(mm),
                               second=0, microsecond=0)
        # 不设固定静默退出门槛（同签到逻辑）：调度延迟+接口暂空时会漏签，
        # 统一交给签退窗口/提前预警处理。
        log(f"暂未查询到预约记录，按配置末时段 {end:%H:%M} 结束继续等待...")
    else:
        end = ts_to_bj(target["endTime"])
    begin_at = end - dt.timedelta(minutes=cfg["signback_before_min"])
    close_at = end + dt.timedelta(minutes=cfg["signback_after_min"])
    log(f"等待到 {begin_at:%H:%M} 开始签退...")
    sleep_until(begin_at)
    if now_bj() >= close_at:
        late = cfg.get("sign_late_window_min", 10)
        close_at = now_bj() + dt.timedelta(minutes=late)
        log(f"已超过正常签退窗口，进入 {late} 分钟补救尝试")
    fail_msg = ""
    warned = False
    attempted = False   # 本任务是否实际发起过签退请求（区分“我签退的”与“别的班次签的”）
    while now_bj() < close_at:
        curs = cur_records()
        # 只签“今日结束时间最晚的活跃记录”（真正的当前/末时段）。
        # 终态 2/7/8 排除；分时段模式下前两时段即使状态异常滞留为 1，
        # 也不会被误签——它们 endTime 较小。
        active = [r for r in curs if r.get("status") not in (2, 7, 8)]
        if active:
            # 只有真出现过“需要动作”的记录，才允许后续预警/失败告警
            seen = True
            r = max(active, key=lambda x: int(x["endTime"]))
            st = r.get("status")
            rid = r["id"]
            # 极端情况：仍是待履约，先补签到再签退
            attempted = True
            if st == 0:
                log("仍是待履约状态，先补签到...")
                grabber.sign_action(grabber.SIGN_API, rid)
            kind, m = classify_sign(
                grabber.sign_action(grabber.SIGNBACK_API, rid), action)
            fail_msg = m
            if kind in ("ok", "already"):
                log(f"✅ 签退成功：{m}")
                _snum = r.get("seatNum", seat)
                push_wechat(key, f"座位{_snum}自动签退成功",
                            f"日期：{now_bj():%Y-%m-%d}\n座位：{_snum}\n"
                            f"签退时间：{now_bj():%H:%M:%S}\n"
                            "今日履约完成，无违规 🎉")
                return
            if kind == "auth":
                log("登录态失效，重新登录...")
                grabber.login()
            else:
                log(f"签退暂未成功：{m}")
        else:
            drec, _r = day_records()
            tid = str(target.get("id")) if target else ""
            by_id = next((r for r in drec if str(r.get("id")) == tid), None) \
                if tid else None
            # 只认“我们跟踪的那条”确实已履约；不能再用 drec[-1]（同时间
            # 旧记录会造成假成功）。无目标时则要求没有任何记录仍需签退。
            done2 = (by_id is not None and by_id.get("status") == 2) or (
                target is None and drec
                and not any(r.get("status") in NEED_BACK for r in drec)
                and any(r.get("status") == 2 for r in drec))
            if done2:
                if attempted:
                    log("✅ 已履约，签退成功")
                    _snum = (by_id.get("seatNum") if by_id else seat)
                    push_wechat(key, f"座位{_snum}自动签退成功",
                                f"日期：{now_bj():%Y-%m-%d}\n座位：{_snum}\n"
                                f"签退时间：{now_bj():%H:%M:%S}\n"
                                "今日履约完成，无违规 🎉")
                else:
                    log("ℹ️ 已被其他冗余班次签退，本任务静默退出（不重复推送）")
                return
            log("暂无活跃记录，窗口内继续轮询...")
        # 提前预警：到 22:05（结束+5分钟）仍未确认签退，立刻通知人工介入，
        # 同时继续重试，不等窗口耗尽（22:18 后无法手动补救）
        # 必须见过真实预约记录（seen）才允许预警——否则当天本来就没预约
        # （如新账号首日/手动取消全天），每个冗余班次都会发一条假预警。
        if seen and not warned and now_bj() >= end + dt.timedelta(minutes=5):
            warned = True
            push_wechat(key, f"⚠️座位{seat}自动签退暂未确认，请准备手动！",
                        f"当前时间：{now_bj():%H:%M:%S}\n最后返回：{fail_msg}\n\n"
                        "脚本仍在自动重试。若 22:12 前未收到“签退成功”：\n"
                        "请立即打开学习通 → 座位预约 → 我的预约 → 点击“签退”。\n"
                        "22:20 前签退均有效，超过记违约！")
        time.sleep(random.uniform(lo, hi))
    if not seen:
        log("窗口内各来源均未检测到今日预约，静默退出（不发误报告警）")
        return
    log("⚠️ 签退窗口用尽仍未成功")
    push_wechat(key, f"⚠️座位{seat}自动签退失败，请立即手动签退！",
                f"最后一次返回：{fail_msg}\n\n"
                "请立刻打开学习通扫描桌上二维码点击“退座/签退”，"
                "离开不签退记违规！")

def main():
    parser = argparse.ArgumentParser(description="图书馆座位自动抢座")
    parser.add_argument("mode", nargs="?",
                        choices=["inspect", "dryrun", "run", "signin",
                                 "signback", "enter", "renew"],
                        default=os.environ.get("GRAB_MODE", "run"))
    args = parser.parse_args()
    cfg = load_config()
    if args.mode in ("signin", "signback"):
        run_sign(cfg, args.mode)
    elif args.mode == "enter":
        run_enter(cfg)
    elif args.mode == "renew":
        run_renew_all(cfg)
    else:
        run(cfg, args.mode)

if __name__ == "__main__":
    main()
