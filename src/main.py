#!/usr/bin/env python3
"""MAF Shift & Event-Driven Agent Scheduler — 作息系统完整演示.

模拟流程:
  1. 系统启动 = Tick 0 / 第 1 天 → TimeManager 线程发送 SHIFT_START (EMERGENCY)
  2. 角色收到上班事件 → 开始处理工作事件
  3. 推进到下班 Tick → SHIFT_END (EMERGENCY, 附带 instruction)
  4. 角色按 instruction 调用 summary 工具 → 保存总结 + 切换 OFF_DUTY
  5. 检查: 第 2 天 build_system_prompt 会注入第 1 天总结

运行:
    cd maf_scheduler && source .venv/bin/activate && python -m src.main
"""

from __future__ import annotations

import logging
import os
import sys
import time as time_module
from datetime import datetime, timedelta

from src.core.dispatcher import EventDispatcher
from src.core.roles import RolePool
from src.core.role_templates import get_template
from src.core.time_manager import TimeManager, EVENT_SHIFT_END, EVENT_SHIFT_START
from src.core.types import AgentState, Event, Priority
from src.python_tools.memory_toolkit import create_memory_toolkit
from src.python_tools.time_toolkit import create_time_toolkit

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

BOLD = "\033[1m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
RED = "\033[31m"; CYAN = "\033[36m"; MAGENTA = "\033[35m"; RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 62}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 62}{RESET}\n")


def step(text: str) -> None:
    print(f"{MAGENTA}▶ {text}{RESET}")


def info(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓ {text}{RESET}")


def warn(text: str) -> None:
    print(f"  {YELLOW}⚠ {text}{RESET}")


def main() -> None:
    header("MAF 作息系统演示 — Tick 制 + 事件驱动上下班")

    # ── 1. 启动角色池 (管理角色 + 工具) ────────────────────
    step("启动角色池 (ceo / coo / hr / cfo)...")
    pool = RolePool()
    roles = ["ceo", "coo", "hr", "cfo"]
    for rid in roles:
        role = get_template(rid)
        role.add_toolkit(create_memory_toolkit())   # summary → 下班 + 笔记
        role.add_toolkit(create_time_toolkit())     # get_time
        pool.add_role(role)
    pool.start()
    ok(f"角色池已启动: {roles}")

    # ── 2. 启动 TimeManager (独占线程, 系统启动 = Tick 0) ──
    step("启动 TimeManager 时间线程 (系统启动 = Tick 0 / 第 1 天)...")
    tm = TimeManager(check_interval=1)  # 演示用快速检查 (1秒)

    # 模拟时钟: 用可推进的时间源模拟一天
    sim_now = [datetime(2026, 8, 1, 8, 0)]  # 起始墙钟时间 (仅用于演示推进)

    # 所有角色绑定同一个共享 TimeManager (保证 day_number / tick 一致)
    for rid in roles:
        pool.get_role(rid).bind_time_manager(tm)

    tm.set_clock(lambda: sim_now[0])

    # 事件 → 事件总线 (广播给所有角色)
    def _send(ev: Event) -> None:
        EventDispatcher(pool).trigger(ev)
    tm.set_event_sender(_send)
    tm.start()
    ok(f"TimeManager 已启动: 第 {tm.day_number()} 天, Tick {tm.current_tick()}")

    # ── 3. 上班 (Tick 0) ──────────────────────────────────
    step("等待时间线程首次检查 (Tick 0 = 上班时刻)...")
    time_module.sleep(1.5)  # 让线程在 Tick 0 先检查一次, 触发 SHIFT_START
    tod = tm.tick_of_day()
    ok(f"当前: 第 {tm.day_number()} 天, Tick {tm.current_tick()} (今日第 {tod} Tick)")

    # 检查角色是否收到 SHIFT_START
    status = pool.get_status()
    got_start = any("SHIFT_START" in (s["current_task"] or "") for s in status.values())
    if got_start:
        ok("角色已收到 SHIFT_START 上班事件 (EMERGENCY, 穿透所有过滤)")
    else:
        warn("角色可能正在处理上班事件 (LLM 调用中)")

    # 推进到 Tick 30 (上午工作时段)
    step("推进到 Tick 30 (上午, 模拟时钟 +5 小时)...")
    sim_now[0] = sim_now[0] + timedelta(hours=5)
    time_module.sleep(1.0)

    # ── 4. 投递一个普通工作事件 ────────────────────────────
    step("投递普通工作事件 (低显著度, 应被过滤)...")
    spam = Event(source="slack", event_type="chat", priority=Priority.LOW,
                 payload={"text": "中午吃什么?", "channel": "#random"})
    EventDispatcher(pool).trigger(spam)
    time_module.sleep(0.5)
    info("LOW 事件已投递 (显著性过滤: 0 Token 拦截)")

    step("投递高优先级工作工单 (HIGH)...")
    work = Event(source="github", event_type="new_pr", priority=Priority.HIGH,
                 payload={"pr_number": 188, "title": "fix: login token NPE", "urgent": True})
    results = EventDispatcher(pool).trigger(work)
    accepted = [rid for rid, r in results.items() if r["accepted"]]
    info(f"工单被接受的角色: {accepted or '(LLM 处理中)'}")

    # 让角色处理一会儿
    time_module.sleep(8.0)

    # ── 5. 推进到下班 (Tick >= 60) ─────────────────────────
    step("推进到 Tick >= 60 (下班时刻, 模拟时钟 +9小时50分)...")
    sim_now[0] = sim_now[0] + timedelta(hours=9, minutes=50)
    time_module.sleep(1.5)
    tod = tm.tick_of_day()
    ok(f"当前: 第 {tm.day_number()} 天, Tick {tm.current_tick()} (今日第 {tod} Tick)")

    # SHIFT_END 事件携带 instruction → 角色应调用 summary
    step("等待角色调用 summary 工具 (最长 60 秒)...")
    deadline = time_module.time() + 60
    while time_module.time() < deadline:
        all_off = all(pool.get_role(rid).state == AgentState.OFF_DUTY for rid in roles)
        if all_off:
            break
        time_module.sleep(2.0)

    # ── 6. 检查下班状态 ────────────────────────────────────
    step("检查下班状态...")
    status = pool.get_status()
    off_duty = [rid for rid in roles if pool.get_role(rid).state == AgentState.OFF_DUTY]
    if off_duty:
        ok(f"OFF_DUTY 角色: {off_duty}")
    else:
        warn("角色仍未 OFF_DUTY (LLM 处理慢或未调用 summary)")

    # 打印每个角色今天的总结
    for rid in roles:
        role = pool.get_role(rid)
        summary = role.note_store.get_summary(day=1)
        state = role.state.value
        if summary:
            ok(f"[{rid}] 第1天总结已保存 ({state}): {summary[:60]}...")
        else:
            info(f"[{rid}] 状态: {state}, 暂无总结")

    # ── 7. 第 2 天冷启动: 注入第 1 天总结 ───────────────────
    step("模拟第 2 天冷启动 (build_system_prompt 应注入昨日总结)...")
    tm.stop()
    sim_now[0] = sim_now[0] + timedelta(hours=14)  # 跨过午夜 → 第 2 天

    prompt = pool.get_role("ceo").build_system_prompt()
    if "[昨日总结]" in prompt:
        ok(f"第 2 天 System Prompt 已注入昨日总结 (第 {tm.day_number()} 天)")
    else:
        warn(f"提示词未注入总结 (第 {tm.day_number()} 天)")

    pool.shutdown(wait=False)
    print(f"\n{BOLD}{GREEN}演示完成 ✓{RESET}")
    print("  - SHIFT_START/SHIFT_END 事件: EMERGENCY, 穿透所有过滤")
    print("  - 下班事件 instruction → 角色调用 summary → OFF_DUTY")
    print("  - 每日总结持久化 → 次日注入 System Prompt")


if __name__ == "__main__":
    sys.exit(main())
