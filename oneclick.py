# -*- coding: utf-8 -*-
"""
一键签到 / 一键签退 —— 不用扫码、不用翻学习通。
双击 一键签到.bat / 一键签退.bat 即可，自动处理两个账号（263 / 258），
结束后弹窗 + 微信显示结果。复用 grab_seat.py 已验证的登录与签到逻辑。
"""
import sys
import os
import io
import copy

if sys.platform.startswith("win"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grab_seat as g


def today_records(grabber, seat):
    """聚合 index + reservelist，只取“今天 + 本座位”的记录，按开始时间排序。"""
    today = g.now_bj().date()
    out = {}

    def consider(r):
        try:
            if str(r.get("seatNum")) == str(seat) and \
                    g.ts_to_bj(r["startTime"]).date() == today:
                out[str(r.get("id"))] = r
        except Exception:
            pass

    try:
        d = grabber.fetch_index()
        data = d.get("data") or {}
        for key in ("curReserves", "nearReserves"):
            for r in (data.get(key) or []):
                consider(r)
    except Exception:
        pass
    try:
        recs, _ = grabber.fetch_reservations()
        for r in recs:
            consider(r)
    except Exception:
        pass
    return sorted(out.values(), key=lambda x: int(x["startTime"]))


def run_account(base_cfg, action, idx, seat):
    """对单个账号执行签到/签退，返回 (是否成功, 说明)。"""
    word = "签到" if action == "signin" else "签退"
    cfg = copy.deepcopy(base_cfg)
    cfg["seat_num"] = str(seat)
    if idx != 1:
        cfg["username"] = base_cfg.get(f"username_{idx}") or base_cfg["username"]
        cfg["password"] = base_cfg.get(f"password_{idx}") or base_cfg["password"]
    if not cfg.get("username") or not cfg.get("password"):
        return False, f"{seat}座（账号{idx}）凭据缺失"

    grabber = g.SeatGrabber(cfg)
    try:
        grabber.login()
        grabber.fetch_seat_page()
    except Exception as e:
        return False, f"{seat}座 登录失败：{e}"

    api = g.SeatGrabber.SIGN_API if action == "signin" \
        else g.SeatGrabber.SIGNBACK_API
    last = "未找到可操作的预约记录"

    for _ in range(3):
        recs = today_records(grabber, seat)
        try:
            if action == "signin":
                for r in recs:
                    st = r.get("status")
                    if st in (1, 2):
                        return True, f"{seat}座 已是学习中（{word}成功）"
                    if st in (0, 3, 5):
                        kind, msg = g.classify_sign(
                            grabber.sign_action(api, r["id"]), action)
                        last = msg
                        if kind in ("ok", "already"):
                            return True, f"{seat}座 {word}成功：{msg}"
                        if kind == "auth":
                            grabber.login()
                            break
            else:
                active = [r for r in recs if r.get("status") not in (2, 7, 8)]
                if active:
                    r = max(active, key=lambda x: int(x["endTime"]))
                    if r.get("status") == 0:
                        grabber.sign_action(g.SeatGrabber.SIGN_API, r["id"])
                    kind, msg = g.classify_sign(
                        grabber.sign_action(api, r["id"]), action)
                    last = msg
                    if kind in ("ok", "already"):
                        return True, f"{seat}座 {word}成功：{msg}"
                else:
                    if any(x.get("status") == 2 for x in recs):
                        return True, f"{seat}座 已履约（{word}成功）"
        except Exception as e:
            last = str(e)
    return False, f"{seat}座 {word}未成功：{last}"


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "signin"
    if action not in ("signin", "signback"):
        action = "signin"
    word = "签到" if action == "signin" else "签退"

    # 直接读原始 config（不经过 load_config 的环境变量覆盖），拿到两个账号凭据
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config.json")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.example.json")
    import json
    with open(cfg_path, "r", encoding="utf-8") as f:
        base_cfg = json.load(f)

    key = base_cfg.get("serverchan_key", "")
    accounts = [(1, base_cfg.get("seat_1", 263)),
                (2, base_cfg.get("seat_2", 258))]

    results = []
    for idx, seat in accounts:
        print(f"\n===== 正在给账号{idx}（{seat}座）{word} =====")
        ok, msg = run_account(base_cfg, action, idx, seat)
        results.append((ok, msg))
        print(("✅ " if ok else "❌ ") + msg)

    success = sum(1 for ok, _ in results if ok)
    title = f"一键{word}完成 {success}/{len(results)}"
    body = "\n".join(("✅ " if ok else "❌ ") + m for ok, m in results)
    print("\n" + title + "\n" + body)

    g.show_popup(title, body)
    if key:
        g.push_wechat(key, title, body)


if __name__ == "__main__":
    main()
