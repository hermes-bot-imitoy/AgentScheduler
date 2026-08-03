#!/usr/bin/env python3
"""MAF 作息系统演示 — 真实时间流动 (TimeManager 自动走时).

与之前"模拟时钟大步跳跃"不同, 本版本不注入模拟时钟:
  - 1 Tick = 10 真实分钟, 系统启动 = Tick 0 / 第 1 天
  - Tick 60 (10 小时后) 自动触发 SHIFT_END → 角色调 summary 下班
  - 24 小时 (144 Tick) 后自动进入第 2 天 (SHIFT_START)
  - 每天结束询问用户: 是否继续下一天? 是 → 等下一自然日, 否 → 退出

流程:
  第 1 天: CEO 注册 Tick 1 任务 (10 分钟后) 与用户沟通项目要求
  第 2 天起: 不再安排甲方沟通, 重复日常循环

运行:
    cd maf_scheduler && source .venv/bin/activate && python -m src.main
"""

from __future__ import annotations

import logging
import sys
import time as time_module

from src.core.agent_system import AgentSystem
from src.core.types import AgentState, Event, Priority
from src.python_tools.client_toolkit import create_client_toolkit

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

BOLD = "\033[1m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
RED = "\033[31m"; CYAN = "\033[36m"; MAGENTA = "\033[35m"; RESET = "\033[0m"

ROLE_IDS = ["ceo", "coo", "hr", "cfo"]   # 4 个默认角色

# 时间参数 (真实时间, 分钟/小时)
TICK_MINUTES = 10        # 1 Tick = 10 真实分钟
TICK1_MINUTES = 10       # 第 1 Tick = 10 分钟后
SHIFT_END_HOURS = 10     # 下班 = 10 小时后 (Tick 60)
DAY_BOUNDARY_HOURS = 24  # 跨天 = 24 小时后 (144 Tick)


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


def wait_until(desc: str, predicate, timeout_seconds: float) -> bool:
    """轮询等待条件满足 (真实时间).

    参数:
        desc:             等待说明 (打印用)
        predicate:        无参布尔函数
        timeout_seconds:  最长等待秒数

    返回:
        True=条件满足, False=超时.
    """
    info(f"等待: {desc} (最长 {timeout_seconds/60:.0f} 分钟)...")
    deadline = time_module.time() + timeout_seconds
    while time_module.time() < deadline:
        if predicate():
            ok(f"{desc} ✓")
            return True
        time_module.sleep(5)
    warn(f"等待超时: {desc}")
    return False


def ask_continue() -> bool:
    """询问用户是否继续下一天. 返回 True=继续, False=结束."""
    try:
        ans = input(f"\n  {BOLD}一天结束, 是否继续下一天? (y/n): {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "是", "")


def run_one_day(system: AgentSystem, day: int, with_client_task: bool) -> None:
    """运行一天的完整流程 (真实时间, 由 TimeManager 自动走时).

    参数:
        system:           AgentSystem 实例
        day:              当前第几天
        with_client_task: 是否安排 CEO 与甲方沟通任务 (仅第 1 天为 True)
    """
    header(f"第 {day} 天")

    # ── 新的一天: 等待跨天边界 (day_number 变化 → SHIFT_START 自动触发) ──
    if day > 1:
        wait_until(
            f"第 {day} 天开始 (约 {DAY_BOUNDARY_HOURS*(day-1)} 小时后, SHIFT_START 自动触发)",
            lambda: system.day >= day,
            timeout_seconds=DAY_BOUNDARY_HOURS * 3600,
        )

    ok(f"当前: {system.describe()}")

    # ── 第 1 天: CEO 与甲方沟通 (仅此一次) ─────────────────
    if with_client_task:
        step("CEO 注册开局任务: Tick 1 (10 分钟后) 与用户沟通项目要求...")
        task = system.time_manager.schedule_task(
            description="与用户沟通项目要求, 收集今天要开发的项目需求",
            owner_role="ceo",
            target_tick=1,
            day=day,
        )
        ok(f"任务已注册 [ID={task.task_id}]: 第 {day} 天 Tick 1 → CEO")

        step("等待 Tick 1 触发 (CEO 任务 → 与用户沟通)...")
        fire_tick = (day - 1) * 144 + 1
        wait_until(
            f"Tick {fire_tick} 到达 (CEO 任务触发)",
            lambda: system.time_manager.current_tick() >= fire_tick,
            timeout_seconds=(TICK1_MINUTES + 5) * 60,
        )
        info("请在上方 [甲方] 提示处输入项目要求 (例如: 帮我开发一个支付系统)")
        # 等待 CEO 任务被处理 (用户输入后 LLM 继续)
        time_module.sleep(10)
    else:
        # 第 2 天起: 不再安排甲方沟通, 直接进入日常工作
        step("今天没有甲方沟通任务, 直接进入日常工作...")
        time_module.sleep(5)

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

    # ── 等待下班 (Tick 60 = 10 小时后, SHIFT_END 自动触发) ─
    step("等待下班... (Tick 60 = 10 小时后, SHIFT_END 自动触发)")
    wait_until(
        "下班时刻到达 (SHIFT_END 触发)",
        lambda: system.time_manager.tick_of_day() >= 60,
        timeout_seconds=(SHIFT_END_HOURS + 1) * 3600,
    )
    time_module.sleep(5)  # 给角色收尾一小段时间

    step("等待角色调用 summary 工具 (最长 240 秒)...")
    deadline = time_module.time() + 240
    while time_module.time() < deadline:
        if all(system.get_role(rid).state == AgentState.OFF_DUTY for rid in ROLE_IDS):
            break
        time_module.sleep(5)

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
    header("MAF 作息系统演示 — 真实时间流动 (1 Tick = 10 分钟)")

    # ── 1. 开局: 4 个默认角色 + CEO 甲方交流工具 ───────────
    step("创建 AgentSystem, 加入 4 个默认角色 (ceo/coo/hr/cfo)...")
    system = AgentSystem(role_ids=ROLE_IDS)
    system.get_role("ceo").add_toolkit(create_client_toolkit())
    ok(f"角色就绪: {system.pool.list_roles()}")
    ok("CEO 已装备 talk_to_client (与甲方实时交流)")

    # ── 2. 启动系统 (真实时钟, Tick 0 = 第 1 天上班) ───────
    system.start()
    ok(f"系统已启动: {system.describe()}")
    ok(f"时间规则: 1 Tick = {TICK_MINUTES} 分钟; 下班 = {SHIFT_END_HOURS} 小时后; "
       f"第 2 天 = {DAY_BOUNDARY_HOURS} 小时后")
    time_module.sleep(3)  # 等 SHIFT_START (Tick 0) 触发
    states = {rid: system.get_role(rid).state.value for rid in ROLE_IDS}
    info(f"角色状态: {states}")

    # ── 3. 多日循环: 第 1 天有甲方沟通, 之后重复日常 ────────
    day = 1
    while True:
        run_one_day(system, day, with_client_task=(day == 1))

        # 一天结束: 询问用户是否继续 (继续 = 等 14 小时后第 2 天上班)
        if not ask_continue():
            print(f"\n  {YELLOW}停止循环, 系统关闭.{RESET}")
            break
        day += 1
        info(f"已确认继续: 第 {day} 天将于约 {DAY_BOUNDARY_HOURS - SHIFT_END_HOURS} 小时后开始.")

    system.stop()
    print(f"\n{BOLD}{GREEN}演示完成 ✓{RESET} (共运行 {day} 天)")


if __name__ == "__main__":
    sys.exit(main())
