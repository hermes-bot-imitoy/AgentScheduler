#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nextcloud Talk 匿名通话压测脚本
================================
目的：测当前服务器配置下，公开通话房间最多能同时加入多少人而不卡。
原理：用 Playwright 无头 Chromium 模拟 N 个真实匿名访客，
      每个访客独立浏览器会话 + 假摄像头/麦克风（真实推 WebRTC 音视频流），
      逐步增加人数并观察：加入成功率 / 服务器响应延迟 / 页面异常。

用法：
    .venv-talk/bin/python talk_loadtest.py --max 40 --step 5 --hold 30
参数：
    --max       目标最大人数（默认 40）
    --step      每批增加人数（默认 5）
    --hold      每批加入后保持观察的秒数（默认 30）
    --url       通话链接（默认 https://cloud.imitoy.top/call/9niza5hk）
    --interval  相邻访客加入间隔秒数（默认 1.5，避免瞬间风暴）
"""
import argparse
import json
import time
import urllib.request
import ssl
from datetime import datetime

from playwright.sync_api import sync_playwright

ROOM_URL = "https://cloud.imitoy.top/call/9niza5hk"
API_LATENCY_URLS = [
    "https://cloud.imitoy.top/",
    "https://cloud.imitoy.top/ocs/v2.php/apps/spreed/api/v4/room/9niza5hk",
]
# 本机资源有限，最多同时开的标签页（真实浏览器内存占用大）
MAX_LOCAL_PAGES = 60

ctx = ssl.create_default_context()


def api_latency_ms(url: str, timeout: float = 8.0) -> float | None:
    """测服务器 HTTP 响应延迟(ms)，失败返回 None"""
    try:
        t0 = time.perf_counter()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36",
            "OCS-APIRequest": "true",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read()
            return (time.perf_counter() - t0) * 1000
    except Exception:
        return None


class Visitor:
    """单个匿名访客（独立浏览器上下文 = 独立访客身份）"""

    def __init__(self, idx: int, browser):
        self.idx = idx
        self.name = f"tester_{idx:03d}"
        self.context = browser.new_context(
            viewport={"width": 640, "height": 480},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
        )
        self.page = self.context.new_page()
        self.joined = False      # 是否成功进入通话
        self.join_ms = None      # 加入耗时
        self.error = None        # 加入失败原因
        self.last_ts = None      # 最后活跃时间
        self.page_errors = []    # 页面 JS 错误
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_pageerror)

    def _on_console(self, msg):
        if msg.type in ("error", "warning"):
            self.page_errors.append(f"{msg.type}: {msg.text[:120]}")

    def _on_pageerror(self, exc):
        self.page_errors.append(f"pageerror: {str(exc)[:120]}")

    def join(self, timeout_s: int = 60) -> bool:
        """打开通话页并加入通话，返回是否成功进入。
        兼容两种页面流程：
          1) 有 Join call 按钮（活跃通话/允许直接加入）→ 点击加入
          2) 需要先输入访客名字（Guest 输入框）→ 填名字提交后再加入
        """
        try:
            t0 = time.perf_counter()
            self.page.goto(ROOM_URL, timeout=timeout_s * 1000, wait_until="domcontentloaded")
            # 关闭可能出现的浏览器不支持 toast（避免遮挡按钮）
            try:
                close = self.page.locator('.toastify .toastify__close, .toast-close, .toastify button')
                if close.count():
                    close.first.click(timeout=2000)
            except Exception:
                pass
            deadline = time.time() + timeout_s

            # ---- 流程 2：先输入访客名字 ----
            name_input = self.page.locator('#textField')
            for _ in range(3):
                try:
                    if name_input.count() and name_input.is_visible():
                        name_input.fill(self.name, timeout=5000)
                        submit = self.page.locator('button:has-text("Submit name and join")').first
                        submit.click(timeout=5000)
                        break
                except Exception:
                    pass
                if time.time() > deadline:
                    break
                self.page.wait_for_timeout(1000)

            # ---- 流程 1/2 后：等待 Join call 或 Start call 按钮可用 ----
            btn = self.page.locator('button:has-text("Join call")').first
            if not (btn.count() and btn.is_enabled()):
                # 没有活跃通话时，匿名访客可以自己开始通话（Start call）
                btn = self.page.locator('button[aria-label="Start call"], button:has-text("Start call")').first
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    if btn.count() and btn.is_enabled():
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(1000)
            if not (btn.count() and btn.is_enabled()):
                # 可能已经自动进入通话了（无按钮，直接出现挂断按钮）
                hangup = self.page.locator(
                    'button[aria-label*="Leave call"], button[aria-label*="挂断"], '
                    'button[aria-label*="Hang up"], [class*="callButton"]'
                )
                if hangup.count():
                    self.join_ms = (time.perf_counter() - t0) * 1000
                    self.joined = True
                    self.last_ts = time.time()
                    return True
                raise TimeoutError("Join call / Start call button never enabled (media device init)")
            # 点击加入/开始通话（可能被 toast 短暂遮挡，失败则 force 点击）
            try:
                btn.scroll_into_view_if_needed(timeout=5000)
                btn.click(timeout=8000)
            except Exception:
                btn.click(force=True, timeout=8000)
            # 可能弹出设备设置对话框（media-settings），需点对话框内的确认按钮
            # 顶部栏和对话框内都有 Start call/Join call 按钮，必须点对话框里的
            try:
                c = self.page.locator('.toastify button')
                if c.count():
                    c.first.click(timeout=2000)
            except Exception:
                pass
            dlg_clicked = False
            for _ in range(3):
                try:
                    dlg = self.page.locator('.media-settings')
                    if dlg.count() and dlg.is_visible():
                        dlg_btn = dlg.locator(
                            'button[aria-label="Start call"], button[aria-label="Join call"], '
                            'button:has-text("Start call"), button:has-text("Join call"), '
                            'button[aria-label="Next"], button:has-text("Next")'
                        ).first
                        if dlg_btn.count() and dlg_btn.is_visible():
                            dlg_btn.click(timeout=5000)
                            dlg_clicked = True
                            self.page.wait_for_timeout(1500)
                            continue
                    break
                except Exception:
                    break
            # 等待进入通话：出现挂断按钮 或 URL 带 #call
            try:
                self.page.wait_for_selector(
                    'button[aria-label*="Leave call"], button[aria-label*="挂断"], '
                    'button[aria-label*="Hang up"], .call-button, '
                    '[class*="callButton"]',
                    timeout=timeout_s * 1000,
                )
            except Exception:
                pass
            self.join_ms = (time.perf_counter() - t0) * 1000
            self.joined = True
            self.last_ts = time.time()
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:200]}"
            if self.page_errors:
                self.error += " | 页面: " + "; ".join(self.page_errors[:3])
            return False

    def alive(self) -> bool:
        """检查页面是否还活着（未崩溃），并刷新活跃时间"""
        try:
            ok = not self.page.is_closed()
            if ok:
                self.last_ts = time.time()
            return ok
        except Exception:
            return False

    def close(self):
        try:
            self.context.close()
        except Exception:
            pass


def main():
    global ROOM_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--url", default=ROOM_URL)
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    ROOM_URL = args.url

    target = min(args.max, MAX_LOCAL_PAGES)
    print(f"=== Talk 匿名通话压测 ===")
    print(f"房间: {ROOM_URL}")
    print(f"目标人数: {target}  每批: {args.step}  观察: {args.hold}s  加入间隔: {args.interval}s")
    print(f"开始时间: {datetime.now().strftime('%H:%M:%S')}\n")

    visitors: list[Visitor] = []
    batch_metrics = []  # 每批的汇总

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=[
                "--use-fake-device-for-media-stream",   # 假摄像头/麦克风，真实推流
                "--use-fake-ui-for-media-stream",       # 自动允许媒体权限
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling",
                "--mute-audio",                          # 本地静音省资源（流仍在上传）
                "--disk-cache-dir=/tmp/talk_pw_cache",   # 共享缓存，避免每个会话重下 6MB JS
                "--disk-cache-size=104857600",
            ]
        )
        nxt = 1  # 下一个要加入的编号
        while len(visitors) < target:
            batch_target = min(nxt + args.step - 1, target)
            batch_ok = 0
            batch_fail = 0
            batch_fail_detail = []
            t_batch = time.time()

            # ---- 加入本批访客 ----
            while nxt <= batch_target:
                # 服务器过载预检：首页延迟 > 3s 则等待恢复
                for _ in range(3):
                    lat = api_latency_ms(API_LATENCY_URLS[0], timeout=6.0)
                    if lat is None or lat > 3000:
                        print(f"  [{datetime.now().strftime('%H:%M:%S')}] 服务器响应 {lat:.0f}ms 过高，等待 10s..."
                              if lat else f"  [{datetime.now().strftime('%H:%M:%S')}] 服务器无响应，等待 10s...")
                        time.sleep(10)
                    else:
                        break
                v = Visitor(nxt, browser)
                nxt += 1
                ok = v.join()
                if not ok and v.error:
                    # 失败重试一次（换新会话）
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {v.name} 首次失败({v.error[:80]})，重试...")
                    v.close()
                    v = Visitor(nxt - 1, browser)
                    ok = v.join()
                if ok:
                    batch_ok += 1
                    visitors.append(v)
                    print(f"  [{(datetime.now().strftime('%H:%M:%S'))}] +{v.name} 加入成功 "
                          f"({v.join_ms:.0f}ms)  当前在线: {len(visitors)}")
                else:
                    batch_fail += 1
                    batch_fail_detail.append(f"{v.name}: {v.error}")
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] ✗ {v.name} 加入失败: {v.error}")
                    v.close()
                # 服务器延迟采样（每 2 个访客测一次）
                if v.idx % 2 == 0:
                    lat = api_latency_ms(API_LATENCY_URLS[0])
                    print(f"      [延迟] 首页 {lat:.0f}ms" if lat else "      [延迟] 首页 超时/失败")
                time.sleep(args.interval)

            # ---- 保持观察 ----
            t_observe = time.time()
            while time.time() - t_observe < args.hold:
                time.sleep(min(5, args.hold))
                dead = [v for v in visitors if not v.alive()]
                for v in dead:
                    print(f"  ✗ {v.name} 页面失活/崩溃")
                if dead:
                    # 移除失活访客
                    for v in dead:
                        try:
                            visitors.remove(v)
                        except ValueError:
                            pass
                        v.close()

            # ---- 本批汇总 ----
            lat_home = api_latency_ms(API_LATENCY_URLS[0])
            lat_ocs = api_latency_ms(API_LATENCY_URLS[1])
            m = {
                "batch": len(batch_metrics) + 1,
                "tried_total": nxt - 1,
                "joined_this_batch": batch_ok,
                "failed_this_batch": batch_fail,
                "online_now": len(visitors),
                "latency_home_ms": lat_home,
                "latency_ocs_ms": lat_ocs,
                "fails": batch_fail_detail,
            }
            batch_metrics.append(m)
            print(f"\n--- 批次 {len(batch_metrics)} 汇总 ---")
            print(json.dumps(m, ensure_ascii=False, indent=2))
            print()

            # 提前终止：本批全失败说明到瓶颈了
            if batch_ok == 0:
                print("!! 本批全部加入失败，判定到达瓶颈，停止加压")
                break

        # ---- 收尾：清场 ----
        print("=== 测试结束，清理访客 ===")
        for v in visitors:
            v.close()
        visitors.clear()
        browser.close()

    # ---- 输出报告 ----
    print("\n================ 压测报告 ================")
    print(f"房间: {ROOM_URL}")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not batch_metrics:
        print("无有效数据")
        return
    final = batch_metrics[-1]
    print(f"最终稳定在线人数: {final['online_now']}")
    ok_batches = [m for m in batch_metrics if m["joined_this_batch"] > 0]
    peak = max(m["online_now"] for m in batch_metrics) if batch_metrics else 0
    print(f"峰值在线人数: {peak}")
    print("\n各批次延迟趋势 (首页/OCS API):")
    for m in batch_metrics:
        home = f"{m['latency_home_ms']:.0f}ms" if m["latency_home_ms"] else "FAIL"
        ocs = f"{m['latency_ocs_ms']:.0f}ms" if m["latency_ocs_ms"] else "FAIL"
        print(f"  批{m['batch']}: 在线 {m['online_now']:>3} | 首页 {home:>8} | OCS {ocs:>8} | "
              f"本批成功 {m['joined_this_batch']} 失败 {m['failed_this_batch']}")
    print("\n结论参考:")
    if peak >= target and final["online_now"] >= target:
        print(f"  所有 {target} 个访客均成功加入且保持在线，未达上限（本机资源限制 {MAX_LOCAL_PAGES} 页）")
    else:
        print(f"  稳定在线 {final['online_now']} 人，峰值 {peak} 人 — 继续加压出现失败/掉线，"
              f"可视为当前配置下的实际容量")


if __name__ == "__main__":
    main()
