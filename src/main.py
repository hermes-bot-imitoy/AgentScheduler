#!/usr/bin/env python3
"""MAF 作息系统演示 — 多日循环 (仅第 1 天有甲方沟通任务).

流程:
  1. 开局: 4 个默认角色 (CEO/COO/HR/CFO), CEO 装备 talk_to_client
  2. 第 1 天: CEO 注册 Tick 1 任务, 与用户沟通项目要求
  3. 第 2 天起: 不再安排甲方沟通, 直接重复每天的日常循环
     (SHIFT_START → 工作事件 → SHIFT_END → summary → OFF_DUTY)
  4. 每天结束询问用户: 是否继续下一天? 是 → 循环, 否 → 退出

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

ROLE_IDS = ["ceo", "coo", "hr", "cfo"]   # 4 个默认角色


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


def ask_continue() -> bool:
    """询问用户是否继续下一天. 返回 True=继续, False=结束."""
    try:
        ans = input(f"\n  {BOLD}一天结束, 是否继续下一天? (y/n): {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "是", "")


def run_one_day(system: AgentSystem, sim_now: list[datetime], day: int,
                with_client_task: bool) -> None:
    """运行一天的完整流程.

    参数:
        system:           AgentSystem 实例
        sim_now:          可变的模拟时钟 (list[datetime], [0] 为当前时间)
        day:              当前第几天
        with_client_task: 是否安排 CEO 与甲方沟通任务 (仅第 1 天为 True)
    """
    header(f"第 {day} 天")

    # ── 新的一天: 推进到当天 Tick 0 (跨天 → SHIFT_START) ───
    if day > 1:
        step(f"推进到第 {day} 天 Tick 0 (跨天, SHIFT_START 自动触发)...")
        sim_now[0] = sim_now[0] + timedelta(hours=14)  # 跨过午夜边界
        time_module.sleep(2.0)  # 等待时间线程检测到新的一天

    ok(f"当前: {system.describe()}")

    # ── 第 1 天: CEO 与甲方沟通 (仅此一次) ─────────────────
    if with_client_task:
        step("CEO 注册开局任务: Tick 1 与用户沟通项目要求...")
        ceo = system.get_role("ceo")
        task = system.time_manager.schedule_task(
            description="与用户沟通项目要求, 收集今天要开发的项目需求",
            owner_role="ceo",
            target_tick=1,
            day=day,
        )
        ok(f"任务已注册 [ID={task.task_id}]: 第 {day} 天 Tick 1 → CEO")

        step("推进到 Tick 1 (CEO 任务触发 → 与用户沟通)...")
        sim_now[0] = sim_now[0] + timedelta(minutes=10)
        info("请在上方 [甲方] 提示处输入项目要求 (例如: 帮我开发一个支付系统)")
        time_module.sleep(5.0)  # 等待 CEO LLM 调用 talk_to_client (用户输入后继续)
        if system.time_manager.list_tasks():
            warn("CEO 任务可能仍在处理中 (LLM 调用较慢)")
        else:
            ok("CEO 开局任务已触发并投递 (定向事件, 其他角色未收到)")
    else:
        # 第 2 天起: 不再安排甲方沟通, 直接进入日常工作
        step("今天没有甲方沟通任务, 直接进入日常工作...")
        sim_now[0] = sim_now[0] + timedelta(minutes=20)  # 推进到 Tick 2
        time_module.sleep(1.0)

    # ── 白天工作事件 ───────────────────────────────────────
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

    # ── 推进到下班 (Tick >= 60) ────────────────────────────
    step("推进到 Tick >= 60 (下班时刻, 模拟时钟 +9小时55分)...")
    sim_now[0] = sim_now[0] + timedelta(hours=9, minutes=55)
    time_module.sleep(1.5)
    ok(f"当前: {system.describe()}")

    step("等待角色调用 summary 工具 (最长 90 秒)...")
    deadline = time_module.time() + 90
    while time_module.time() < deadline:
        if all(system.get_role(rid).state == AgentState.OFF_DUTY for rid in ROLE_IDS):
            break
        time_module.sleep(2.0)

    # ── 检查下班状态与总结 ─────────────────────────────────
    step("检查下班状态...")
    off_duty = [rid for rid in ROLE_IDS
                if system.get_role(rid).state == AgentState.OFF_DUTY]
    ok(f"OFF_DUTY 角色: {off_duty}") if off_duty else warn("角色仍未全部 OFF_DUTY")

    for rid in ROLE_IDS:
        summary = system.get_role(rid).note_store.get_summary(day=day)
        if summary:
            ok(f"[{rid}] 第{day}天总结已保存: {summary[:50]}...")
        else:
            info(f"[{rid}] 暂无总结")


def main() -> None:
    header("MAF 作息系统演示 — 多日循环 (仅第 1 天与甲方沟通)")

    # ── 1. 开局: 4 个默认角色 + CEO 甲方交流工具 ───────────
    step("创建 AgentSystem, 加入 4 个默认角色 (ceo/coo/hr/cfo)...")
    system = AgentSystem(role_ids=ROLE_IDS, check_interval=1)
    system.get_role("ceo").add_toolkit(create_client_toolkit())
    ok(f"角色就绪: {system.pool.list_roles()}")
    ok("CEO 已装备 talk_to_client (与甲方实时交流)")

    # ── 2. 模拟时钟 + 启动系统 (Tick 0 = 第 1 天上班) ──────
    sim_now = [datetime(2026, 8, 1, 8, 0)]
    system.time_manager.set_clock(lambda: sim_now[0])
    system.start()
    ok(f"系统已启动: {system.describe()}")
    time_module.sleep(1.5)  # Tick 0 → SHIFT_START 全员上班
    states = {rid: system.get_role(rid).state.value for rid in ROLE_IDS}
    info(f"角色状态: {states}")

    # ── 3. 多日循环: 第 1 天有甲方沟通, 之后重复日常 ────────
    day = 1
    while True:
        run_one_day(system, sim_now, day, with_client_task=(day == 1))

        # 一天结束: 询问用户是否继续
        if not ask_continue():
            print(f"\n  {YELLOW}停止循环, 系统关闭.{RESET}")
            break
        day += 1

    system.stop()
    print(f"\n{BOLD}{GREEN}演示完成 ✓{RESET} (共运行 {day} 天)")


if __name__ == "__main__":
    sys.exit(main())
