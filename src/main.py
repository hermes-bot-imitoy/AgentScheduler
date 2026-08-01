#!/usr/bin/env python3
"""MAF 作息系统演示 — AgentSystem 统一管理 + 事件驱动上下班.

模拟流程:
  1. AgentSystem.start() = Tick 0 / 第 1 天 → 时间线程发送 SHIFT_START (EMERGENCY)
  2. 角色收到上班事件 → 状态置 ON_DUTY_IDLE → 开始工作
  3. 投递工作事件 (LOW 被过滤 / HIGH 被接受)
  4. 推进到下班 Tick → SHIFT_END (EMERGENCY, 附带 instruction)
  5. 角色调用 summary 工具 → 保存总结 + 切换 OFF_DUTY
  6. 第 2 天冷启动: build_system_prompt 自动注入昨日总结

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
    header("MAF 作息系统演示 — AgentSystem 统一管理")

    # ── 1. 创建 AgentSystem (统一管理 TimeManager + RolePool) ─
    step("创建 AgentSystem (管理角色: ceo / coo / hr / cfo)...")
    system = AgentSystem(role_ids=["ceo", "coo", "hr", "cfo"], check_interval=1)
    ok(f"角色已注册: {system.pool.list_roles()}")

    # 模拟时钟: 可推进的时间源 (仅用于演示, 生产环境用真实时间)
    sim_now = [datetime(2026, 8, 1, 8, 0)]
    system.time_manager.set_clock(lambda: sim_now[0])

    # ── 2. 启动系统 (Tick 0 / 第 1 天) ──────────────────────
    step("system.start() — 启动角色线程 + 时间线程...")
    system.start()
    ok(f"已启动: {system.describe()}")

    # ── 3. 上班 (SHIFT_START) ──────────────────────────────
    step("等待时间线程首次检查 (Tick 0 → SHIFT_START)...")
    time_module.sleep(1.5)
    ok(f"当前: {system.describe()}")
    states = {rid: system.get_role(rid).state.value for rid in system.pool.list_roles()}
    info(f"角色状态: {states}")

    # ── 4. 工作事件 ────────────────────────────────────────
    step("投递 LOW 事件 (午餐闲聊, 应被显著性过滤)...")
    spam = Event(source="slack", event_type="chat", priority=Priority.LOW,
                 payload={"text": "中午吃什么?", "channel": "#random"})
    results = system.trigger(spam)
    info(f"LOW 过滤结果: { {k: v['accepted'] for k, v in results.items()} } (0 Token)")

    step("投递 HIGH 工作工单 (新 PR 待处理)...")
    work = Event(source="github", event_type="new_pr", priority=Priority.HIGH,
                 payload={"pr_number": 188, "title": "fix: login token NPE", "urgent": True})
    results = system.trigger(work)
    accepted = [rid for rid, r in results.items() if r["accepted"]]
    info(f"HIGH 工单被接受: {accepted}")
    time_module.sleep(8.0)  # 让角色处理

    # ── 5. 下班 (SHIFT_END) ────────────────────────────────
    step("推进到 Tick >= 60 (下班, 模拟时钟 +10小时05分)...")
    sim_now[0] = sim_now[0] + timedelta(hours=10, minutes=5)
    time_module.sleep(1.5)
    ok(f"当前: {system.describe()}")

    step("等待角色调用 summary 工具 (最长 90 秒)...")
    deadline = time_module.time() + 90
    while time_module.time() < deadline:
        if all(system.get_role(rid).state == AgentState.OFF_DUTY for rid in system.pool.list_roles()):
            break
        time_module.sleep(2.0)

    # ── 6. 检查下班状态与总结 ──────────────────────────────
    step("检查下班状态...")
    off_duty = [rid for rid in system.pool.list_roles()
                if system.get_role(rid).state == AgentState.OFF_DUTY]
    ok(f"OFF_DUTY 角色: {off_duty}") if off_duty else warn("角色仍未 OFF_DUTY")

    for rid in system.pool.list_roles():
        summary = system.get_role(rid).note_store.get_summary(day=1)
        if summary:
            ok(f"[{rid}] 第1天总结已保存: {summary[:50]}...")
        else:
            info(f"[{rid}] 暂无总结")

    # ── 7. 第 2 天冷启动: 注入昨日总结 ─────────────────────
    step("模拟第 2 天冷启动 (build_system_prompt 注入昨日总结)...")
    system.stop()
    sim_now[0] = sim_now[0] + timedelta(hours=14)  # 跨天 → 第 2 天

    prompt = system.get_role("ceo").build_system_prompt()
    if "[昨日总结]" in prompt:
        ok(f"第 2 天 System Prompt 已注入昨日总结 (system.day = {system.day})")
    else:
        warn(f"提示词未注入总结 (system.day = {system.day})")

    print(f"\n{BOLD}{GREEN}演示完成 ✓{RESET}")
    print("  - AgentSystem 统一管理 TimeManager + RolePool + 事件总线")
    print("  - SHIFT_START/SHIFT_END: EMERGENCY, 穿透过滤")
    print("  - 下班 → summary → OFF_DUTY → 次日总结注入提示词")


if __name__ == "__main__":
    sys.exit(main())
