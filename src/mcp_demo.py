#!/usr/bin/env python3
"""mcp_demo.py — MCP Tool integration demo.

Demonstrates:
  - Registering MCP-compatible tools on roles
  - Tool-calling loop (LLM decides when to use tools)
  - Multi-round conversation with tool results fed back
  - Per-role tool isolation

Run:
    cd maf_scheduler && source .venv/bin/activate && python -m src.mcp_demo
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-f29a3265f9e34c3bbf8f86f9142a57c9")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
os.environ.setdefault("DEEPSEEK_THINKING", "true")

from src.core.roles import AgentRole, RolePool, Task, Urgency
from src.core.tools import ToolRegistry

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

BOLD = "\033[1m"; GREEN = "\033[32m"; CYAN = "\033[36m"
BLUE = "\033[34m"; MAGENTA = "\033[35m"; YELLOW = "\033[33m"; RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")


# ── Mock tool handlers ────────────────────────────────────

def _query_db(args: dict) -> str:
    """Simulate database query."""
    query = args.get("query", "").lower()
    if "user" in query:
        return json.dumps({
            "count": 15420,
            "last_signup": "2026-07-29T08:15:00Z",
            "active_today": 892,
        })
    elif "error" in query or "log" in query:
        return json.dumps({
            "recent_errors": [
                {"time": "08:12:03", "level": "ERROR", "msg": "AuthService.validate() NPE at line 42"},
                {"time": "08:11:58", "level": "WARN", "msg": "Slow query: SELECT * FROM sessions WHERE ..."},
                {"time": "08:10:45", "level": "ERROR", "msg": "Connection pool exhausted"},
            ]
        })
    return '{"result": "no matching data"}'

import json


def main():
    header("MCP Tool Integration — Role with Tool-Calling Loop")

    # ── Create roles with tools ──────────────────────────────

    ops_bot = AgentRole(
        name="赵强",
        role_id="ops",
        title="Site Reliability Engineer",
        personality="冷静果断，先止损再排查。擅长在压力下快速定位问题。",
        skills=["Kubernetes", "Prometheus", "PostgreSQL", "Linux"],
    )

    # Import Python toolkits (bulk registration)
    from src.core.tools import create_coding_toolkit

    ops_bot.add_toolkit(create_coding_toolkit())
    # Single MCP-style tools still work too
    ops_bot.add_mcp_tool(
        name="query_logs",
        description="Query recent application logs and error messages",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword (e.g. 'error', 'user', 'login')"},
            },
            "required": ["query"],
        },
        handler=_query_db,
    )

    print(f"  {GREEN}ops bot tools:{RESET}")
    for name in ops_bot.mcp_tool_names:
        print(f"    - {name}")
    print(f"  (coding toolkit: 3 tools + query_logs = 4 total)\n")

    # ── Start pool ──────────────────────────────────────────

    pool = RolePool()
    pool.add_role(ops_bot)

    def on_start(role: AgentRole, task: Task) -> None:
        urg = Urgency(-task.urgency)
        print(f"\n  {BLUE}[{role.role_id}]{RESET} {YELLOW}▶ {urg.name}{RESET} — {task.description[:80]}")

    def on_done(role: AgentRole, task: Task) -> None:
        status_icon = f"{GREEN}✓{RESET}" if task.status == "done" else "\033[31m✗\033[0m"
        print(f"  {BLUE}[{role.role_id}]{RESET} {status_icon} done ({task.tokens_consumed}t)")
        print(f"  {MAGENTA}→{RESET} {task.result[:300]}")

    ops_bot.on_task_start = on_start
    ops_bot.on_task_done = on_done

    pool.start()

    # ════════════════════════════════════════════════════════════
    #  Scenario 1: Diagnose with coding toolkit + custom tools
    # ════════════════════════════════════════════════════════════
    header("Scenario 1: Diagnose Production Issue (coding toolkit + query_logs)")

    pool.assign_task("ops", Task(
        urgency=Urgency.CRITICAL,
        description=(
            "用户报告登录页面返回 500 错误。请使用可用工具诊断问题：\n"
            "1. 先查询最近的错误日志\n"
            "2. 检查服务器状态\n"
            "3. 给出诊断结论和修复建议"
        ),
    ))

    time.sleep(12)

    # ════════════════════════════════════════════════════════════
    #  Scenario 2: Check pod status with kubectl
    # ════════════════════════════════════════════════════════════
    header("Scenario 2: Check Pod Health After Fix")

    pool.assign_task("ops", Task(
        urgency=Urgency.HIGH,
        description=(
            "修复已部署。请使用可用工具确认服务恢复正常：\n"
            "1. 运行 kubectl 检查 pod 状态\n"
            "2. 再次查询错误日志确认没有新的错误"
        ),
    ))

    time.sleep(12)

    # ── Shutdown ───────────────────────────────────────────
    pool.shutdown()
    print(f"\n{BOLD}{GREEN}MCP Tool Demo Complete.{RESET}\n")


if __name__ == "__main__":
    main()
