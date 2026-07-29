#!/usr/bin/env python3
"""role_demo.py — 多角色并发任务调度演示.

Demonstrates:
  - 3 roles (coder, reviewer, architect) with distinct personas
  - Task priority queue (CRITICAL > HIGH > NORMAL > LOW)
  - Concurrent execution via ThreadPoolExecutor
  - Real DeepSeek LLM responses per role
  - Urgency-based preemption simulation

Run:
    cd maf_scheduler && python -m src.role_demo
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-f29a3265f9e34c3bbf8f86f9142a57c9")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-chat")

from src.core.roles import AgentRole, RolePool, Task, Urgency

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("role_demo")

# ── Pretty printer ───────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")


def main():
    header("Multi-Role Concurrent Task Scheduler — DeepSeek Integration")

    # ── Define Roles ────────────────────────────────────────
    coder = AgentRole(
        name="coder",
        title="Senior Backend Engineer",
        personality="严谨细致，追求代码质量，善于排查复杂 bug",
        skills=["Python", "Go", "PostgreSQL", "Kubernetes", "Redis"],
    )

    reviewer = AgentRole(
        name="reviewer",
        title="Code Review Lead",
        personality="目光敏锐，对安全和性能问题零容忍，但沟通方式温和",
        skills=["Code Review", "Security Audit", "Performance Profiling"],
        system_prompt_extra="每次审查代码时，必须指出至少一个潜在风险点。",
    )

    architect = AgentRole(
        name="architect",
        title="System Architect",
        personality="全局视野，善于权衡取舍，能用简洁语言解释复杂架构",
        skills=["System Design", "Microservices", "DDD", "Event Sourcing"],
        system_prompt_extra="回答必须简洁，不超过3句话。先给结论再给理由。",
    )

    # ── Start Pool ──────────────────────────────────────────
    pool = RolePool()
    pool.add_role(coder)
    pool.add_role(reviewer)
    pool.add_role(architect)

    # Register callbacks for live output
    def on_start(role: AgentRole, task: Task) -> None:
        urg = Urgency(-task.urgency)
        urgency_color = {Urgency.CRITICAL: "\033[31m", Urgency.HIGH: YELLOW}
        c = urgency_color.get(urg, "")
        print(f"  {BLUE}[{role.name}]{RESET} {c}▶ {urg.name}{RESET} — {task.description[:70]}")

    def on_done(role: AgentRole, task: Task) -> None:
        status_icon = f"{GREEN}✓{RESET}" if task.status == "done" else "\033[31m✗\033[0m"
        print(f"  {BLUE}[{role.name}]{RESET} {status_icon} done ({task.tokens_consumed}t) → {task.result[:100]}")

    coder.on_task_start = on_start
    coder.on_task_done = on_done
    reviewer.on_task_start = on_start
    reviewer.on_task_done = on_done
    architect.on_task_start = on_start
    architect.on_task_done = on_done

    pool.start()
    print(f"  {GREEN}3 roles started: coder, reviewer, architect{RESET}\n")

    # ════════════════════════════════════════════════════════════
    #  Scenario 1: Assign tasks at different urgencies
    # ════════════════════════════════════════════════════════════
    header("Scenario 1: Task Distribution")

    pool.assign_task("coder", Task(
        urgency=Urgency.NORMAL,
        description="Fix: login page returns 500 after JWT token expiry. The error log shows NullPointerException in AuthService.validate().",
    ))

    pool.assign_task("reviewer", Task(
        urgency=Urgency.HIGH,
        description="Review PR #188 — JWT refresh token rotation. This changes auth flow for all users. 200+ lines changed.",
    ))

    pool.assign_task("architect", Task(
        urgency=Urgency.CRITICAL,
        description="Database migration gone wrong. 30% of users report corrupted profile data. Need rollback plan NOW.",
    ))

    # Give time for all to complete
    time.sleep(8)

    # ── Status check ────────────────────────────────────────
    status = pool.get_status()
    print(f"\n  {MAGENTA}Queue Status:{RESET}")
    for name, s in status.items():
        print(f"    {name:12} busy={s['busy']}  queue={s['queue_depth']}  "
              f"task={s['current_task'] or 'idle'}")

    # ════════════════════════════════════════════════════════════
    #  Scenario 2: Queue stacking — add multiple tasks to same role
    # ════════════════════════════════════════════════════════════
    header("Scenario 2: Priority Queue — Multi-task Stacking on Coder")

    pool.assign_task("coder", Task(
        urgency=Urgency.LOW,
        description="Update README with new API endpoints documentation.",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.HIGH,
        description="Critical: payment webhook returning 402 for all Stripe callbacks. Affecting revenue!",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.NORMAL,
        description="Add unit tests for UserService.updateProfile() — coverage dropped to 45%.",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.CRITICAL,
        description="PRODUCTION DOWN — healthcheck failing on all pods. CPU 100% on worker nodes. Need immediate fix.",
    ))

    print(f"  {YELLOW}4 tasks stacked on coder. Execution order should be:{RESET}")
    print(f"    1. CRITICAL — PRODUCTION DOWN")
    print(f"    2. HIGH — payment webhook 402")
    print(f"    3. NORMAL — unit tests")
    print(f"    4. LOW — README update\n")

    time.sleep(15)

    # ════════════════════════════════════════════════════════════
    #  Scenario 3: Concurrent role execution
    # ════════════════════════════════════════════════════════════
    header("Scenario 3: Concurrent Execution — All 3 Roles Busy")

    pool.assign_task("coder", Task(
        urgency=Urgency.HIGH,
        description="Debug race condition in WebSocket message handler. Intermittent double-delivery of messages.",
    ))

    pool.assign_task("reviewer", Task(
        urgency=Urgency.HIGH,
        description="Security review: new file upload endpoint. Check for path traversal, file type bypass, size limits.",
    ))

    pool.assign_task("architect", Task(
        urgency=Urgency.HIGH,
        description="Evaluate migration of monolith billing module to separate service. Estimate effort and risks.",
    ))

    time.sleep(10)

    # ── Final Status ────────────────────────────────────────
    header("Final Status")
    status = pool.get_status()
    for name, s in status.items():
        print(f"  {name:12} busy={s['busy']}  queue={s['queue_depth']}")

    print(f"\n  {GREEN}✓ All tasks processed concurrently across 3 roles.{RESET}")

    # ── Shutdown ───────────────────────────────────────────
    pool.shutdown()
    print(f"\n{BOLD}{GREEN}Role Demo Complete.{RESET}\n")


if __name__ == "__main__":
    main()
