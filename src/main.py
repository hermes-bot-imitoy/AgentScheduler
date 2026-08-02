#!/usr/bin/env python3
"""MAF 作息系统演示 — 4 默认角色 + CEO 开局任务 + 事件驱动全流程.

流程:
  1. 开局: 4 个默认角色 (CEO/COO/HR/CFO), CEO 装备 talk_to_client 工具
  2. CEO 注册定时任务: Tick 1 时与用户沟通项目要求
  3. system.start(): 角色池线程 + 时间线程 (Tick 0 = 上班 → SHIFT_START)
  4. Tick 1: CEO 任务触发 → 与甲方(用户)交流收集需求
  5. 白天: 投递工作事件 (LOW 过滤 / HIGH 接受), 定时任务提醒
  6. Tick 60: 下班 → 各角色调 summary → OFF_DUTY
  7. 第 2 天: build_system_prompt 注入昨日总结

运行:
    cd maf_scheduler && source .venv/bin/activate && python -m src.main
"""

from __future__ import annotations

import logging
import sys
import time as time_module
from datetime import datetime, timedelta

from src.core.agent_system import AgentSystem
from src.core.types import AgentState, Event, Priority
from src.python_tools.client_toolkit import create_client_toolkit

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
    header("MAF 作息系统演示 — 4 默认角色 + CEO 开局任务")

    # ── 1. 开局: 4 个默认角色, CEO 装备甲方交流工具 ────────
    step("创建 AgentSystem, 加入 4 个默认角色 (ceo/coo/hr/cfo)...")
    system = AgentSystem(role_ids=["ceo", "coo", "hr", "cfo"], check_interval=1)
    system.get_role("ceo").add_toolkit(create_client_toolkit())
    ok(f"角色就绪: {system.pool.list_roles()}")
    ok("CEO 已装备 talk_to_client (与甲方实时交流)")

    # ── 2. CEO 注册开局任务: Tick 1 与用户沟通项目要求 ─────
    step("CEO 注册定时任务: Tick 1 与用户沟通项目要求...")
    ceo = system.get_role("ceo")
    task = system.time_manager.schedule_task(
        description="与用户沟通项目要求, 收集今天要开发的项目需求",
        owner_role="ceo",
        target_tick=1,
    )
    ok(f"任务已注册 [ID={task.task_id}]: 第 {task.day} 天 Tick {task.target_tick} → CEO")

    # ── 3. 模拟时钟 (演示用可推进时间源) ────────────────────
    sim_now = [datetime(2026, 8, 1, 8, 0)]
    system.time_manager.set_clock(lambda: sim_now[0])

    # ── 4. 启动角色池 + 时间线程, 开始运作 ─────────────────
    step("system.start() — 启动角色池线程 + 时间线程...")
    system.start()
    ok(f"已启动: {system.describe()}")

    # ── 5. Tick 0 → SHIFT_START 上班 ──────────────────────
    step("等待时间线程首次检查 (Tick 0 → SHIFT_START 全员上班)...")
    time_module.sleep(1.5)
    ok(f"当前: {system.describe()}")
    states = {rid: system.get_role(rid).state.value for rid in system.pool.list_roles()}
    info(f"角色状态: {states}")

    # ── 6. Tick 1 → CEO 开局任务触发, 与用户沟通 ───────────
    step("推进到 Tick 1 (CEO 任务触发 → 与用户沟通项目要求)...")
    sim_now[0] = sim_now[0] + timedelta(minutes=10)
    info("请在上方 [甲方] 提示处输入项目要求 (例如: 帮我开发一个支付系统)")
    time_module.sleep(5.0)   # 等待 CEO LLM 调用 talk_to_client (用户输入后继续)
    if system.time_manager.list_tasks():
        warn("CEO 任务可能仍在处理中 (LLM 调用较慢)")
    else:
        ok("CEO 开局任务已触发并投递 (定向事件, 其他角色未收到)")

    # ── 7. 白天工作事件 ────────────────────────────────────
    step("投递 LOW 事件 (闲聊, 应被显著性过滤, 0 Token)...")
    spam = Event(source="slack", event_type="chat", priority=Priority.LOW,
                 payload={"text": "中午吃什么?", "channel": "#random"})
    results = system.trigger(spam)
    info(f"LOW 过滤结果: { {k: v['accepted'] for k, v in results.items()} }")

    step("投递 HIGH 工作工单 (新 PR 待处理)...")
    work = Event(source="github", event_type="new_pr", priority=Priority.HIGH,
                 payload={"pr_number": 188, "title": "fix: login token NPE", "urgent": True})
    results = system.trigger(work)
    accepted = [rid for rid, r in results.items() if r["accepted"]]
    info(f"HIGH 工单被接受: {accepted}")
    time_module.sleep(8.0)  # 让角色处理

    # ── 8. 推进到下班 (Tick >= 60) ─────────────────────────
    step("推进到 Tick >= 60 (下班时刻, 模拟时钟 +9小时55分)...")
    sim_now[0] = sim_now[0] + timedelta(hours=9, minutes=55)
    time_module.sleep(1.5)
    ok(f"当前: {system.describe()}")

    step("等待角色调用 summary 工具 (最长 90 秒)...")
    deadline = time_module.time() + 90
    while time_module.time() < deadline:
        if all(system.get_role(rid).state == AgentState.OFF_DUTY
               for rid in system.pool.list_roles()):
            break
        time_module.sleep(2.0)

    # ── 9. 检查下班状态与总结 ──────────────────────────────
    step("检查下班状态...")
    off_duty = [rid for rid in system.pool.list_roles()
                if system.get_role(rid).state == AgentState.OFF_DUTY]
    ok(f"OFF_DUTY 角色: {off_duty}") if off_duty else warn("角色仍未全部 OFF_DUTY")

    for rid in system.pool.list_roles():
        summary = system.get_role(rid).note_store.get_summary(day=1)
        if summary:
            ok(f"[{rid}] 第1天总结已保存: {summary[:50]}...")
        else:
            info(f"[{rid}] 暂无总结")

    # ── 10. 第 2 天冷启动: 注入昨日总结 ────────────────────
    step("模拟第 2 天冷启动 (build_system_prompt 注入昨日总结)...")
    system.stop()
    sim_now[0] = sim_now[0] + timedelta(hours=16)  # 跨天 → 第 2 天

    prompt = system.get_role("ceo").build_system_prompt()
    if "[昨日总结]" in prompt:
        ok(f"第 2 天 System Prompt 已注入昨日总结 (system.day = {system.day})")
    else:
        warn(f"提示词未注入总结 (system.day = {system.day})")

    print(f"\n{BOLD}{GREEN}演示完成 ✓{RESET}")
    print("  - 4 默认角色 + CEO 开局任务 (Tick 1 与用户沟通)")
    print("  - SHIFT_START/SHIFT_END: EMERGENCY 穿透过滤")
    print("  - 下班 → summary → OFF_DUTY → 次日总结注入提示词")


if __name__ == "__main__":
    sys.exit(main())
