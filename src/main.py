#!/usr/bin/env python3
"""main.py — 模拟完整一天的企业作息与事件驱动调度.

This script demonstrates the full Shift & Event-Driven Agent Scheduling Framework.
It simulates a complete workday with four key scenarios:

  09:00  Shift Start — restore yesterday's Journal, cold-start fresh Context
  11:00  Low-priority event — intercepted by Ambient Buffer (0 Token on LLM)
  14:00  High-priority work order — passes filter, wakes MAF Business Workflow
  18:00  Shift End   — summarize day, write Journal, Context Flush

Run:
    cd maf_scheduler && python -m src.main
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.event_bus import EventBus
from src.core.scheduler import ShiftScheduler
from src.core.types import AgentState, Event, FilterDecision, Priority
from src.storage.ambient_buffer import AmbientBuffer
from src.storage.journal_store import JournalStore
from src.workflow.agent_workflow import build_business_workflow
from src.workflow.engine import WorkflowEngine

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ── Pretty printer ───────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")


def step(text: str) -> None:
    print(f"{MAGENTA}▶ {text}{RESET}")


def info(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓ {text}{RESET}")


def warn(text: str) -> None:
    print(f"  {YELLOW}⚠ {text}{RESET}")


def fail(text: str) -> None:
    print(f"  {RED}✗ {text}{RESET}")


# ── Main simulation ──────────────────────────────────────────


def main() -> None:
    header("MAF Shift & Event-Driven Agent Scheduler — Daily Simulation")
    print("  Breaking the while(true) loop. Context isolation. 0-Token filtering.")
    print("  Shift-based lifecycle. Journal-driven memory.\n")

    # ── Bootstrap infrastructure ─────────────────────────────
    step("Initializing infrastructure...")

    agent_id = "ops-bot-01"
    engine = WorkflowEngine()
    build_business_workflow(engine)

    buffer = AmbientBuffer(":memory:")  # in-memory for demo
    store = JournalStore(data_dir="./data/journals")

    scheduler = ShiftScheduler(
        agent_id=agent_id,
        engine=engine,
        buffer=buffer,
        store=store,
        persona_name="ops-bot",
        persona_role="DevOps Assistant",
    )

    event_bus = EventBus(buffer=buffer)
    event_bus.set_state_getter(lambda: scheduler.state)

    ok(f"Agent '{agent_id}' created. State: {scheduler.state.value}")

    # ── Prepare a "yesterday" journal for the demo ────────────
    from src.core.types import Journal

    yesterday_journal = Journal(
        agent_id=agent_id,
        date="2026-07-28",
        summary=(
            "Yesterday: Deployed v2.3.1 to staging. Found a memory leak in the auth service "
            "and opened issue #421. Customer reported slow login times — investigating. "
            "Pending: review PR #188 (JWT refresh), update monitoring dashboards for the new API gateway."
        ),
        key_decisions=["Rolled back v2.3.0 due to auth bug", "Assigned issue #421 to Alice"],
        pending_tasks=["Review PR #188 — JWT refresh token logic", "Update Grafana dashboards"],
    )
    store.save(yesterday_journal)
    info(f"Seeded yesterday's journal ({yesterday_journal.date})")

    # ════════════════════════════════════════════════════════════
    #  09:00 — Shift Start
    # ════════════════════════════════════════════════════════════
    header("09:00 — SHIFT START (上班)")

    result = scheduler.run_shift_start()
    ok(f"State: {scheduler.state.value}")
    info(f"Previous journal: {result['previous_journal_date']}")
    info(f"Pending tasks: {result['pending_tasks']}")
    info(f"Context history size: {len(scheduler.session.history)} messages")
    info(f"System prompt (first 150 chars):")
    info(f"  {scheduler.session.system_prompt[:150]}...")

    # ════════════════════════════════════════════════════════════
    #  11:00 — Low-priority non-relevant event → Ambient Buffer
    # ════════════════════════════════════════════════════════════
    header("11:00 — NON-RELEVANT EVENT (test Ambient Buffer, 0 Token)")

    spam_event = Event(
        source="slack",
        event_type="channel_message",
        priority=Priority.LOW,
        payload={"channel": "#random", "text": "Anyone up for lunch?", "mentions": []},
        timestamp=datetime(2026, 7, 29, 11, 0, 0, tzinfo=timezone.utc),
    )
    info(f"Incoming event: {spam_event.source}/{spam_event.event_type} (priority={spam_event.priority.name})")
    info(f"Payload: {spam_event.payload['text']}")

    decision = event_bus.process_event(spam_event, agent_id=agent_id)
    info(f"Filter decision: {decision.value}")
    info(f"Salience score: {spam_event.salience_score:.2f}")

    if decision != FilterDecision.PASS:
        ok(f"Event intercepted! Decision={decision.value} — 0 Tokens consumed on LLM")
        warn(f"Event parked in AmbientBuffer (pending: {buffer.count_pending(agent_id)})")
    else:
        fail("Event should have been blocked by Layer 2 salience filter!")

    # ════════════════════════════════════════════════════════════
    #  14:00 — High-priority work order → Wake workflow
    # ════════════════════════════════════════════════════════════
    header("14:00 — HIGH-PRIORITY WORK ORDER (wake MAF workflow)")

    work_event = Event(
        source="github",
        event_type="new_pr",
        priority=Priority.HIGH,
        payload={
            "repo": "api-gateway",
            "pr_number": 188,
            "title": "JWT refresh token rotation",
            "author": "alice",
            "urgent": True,
        },
        timestamp=datetime(2026, 7, 29, 14, 0, 0, tzinfo=timezone.utc),
    )
    info(f"Incoming event: {work_event.source}/{work_event.event_type} (priority={work_event.priority.name})")
    info(f"Payload: PR #{work_event.payload['pr_number']} — {work_event.payload['title']}")

    decision = event_bus.process_event(work_event, agent_id=agent_id)
    info(f"Filter decision: {decision.value}")
    info(f"Salience score: {work_event.salience_score:.2f}")

    if decision == FilterDecision.PASS:
        ok("Event passed filter → waking MAF Business Workflow...")

        # Execute the workflow
        artifact = scheduler.execute_task(work_event, workflow_id="business_workflow")
        ok(f"Workflow complete: {artifact.summary}")
        info(f"Tokens consumed: {artifact.tokens_consumed}")
        info(f"Session history size after task: {len(scheduler.session.history)} messages")
        info(f"Checkpoints: {list(scheduler.session.checkpoints.keys())}")
    else:
        fail(f"Expected PASS, got {decision.value}")

    # ════════════════════════════════════════════════════════════
    #  18:00 — Shift End (Journal + Context Flush)
    # ════════════════════════════════════════════════════════════
    header("18:00 — SHIFT END (下班 — Journal + Context Flush)")

    journal = scheduler.run_shift_end()
    ok(f"State: {scheduler.state.value}")
    ok(f"Journal generated for {journal.date}")
    info(f"Summary: {journal.summary}")
    info(f"Key decisions: {journal.key_decisions}")
    info(f"Pending tasks: {journal.pending_tasks}")
    info(f"Ambient highlights: {journal.ambient_highlights}")
    info(f"Context history after flush: {len(scheduler.session.history)} messages")

    # ════════════════════════════════════════════════════════════
    #  Summary
    # ════════════════════════════════════════════════════════════
    header("SIMULATION SUMMARY")

    stats = event_bus.get_stats()
    print(f"  Total events:      {stats['total_events']}")
    print(f"  {GREEN}Passed (to LLM):    {stats['passed']}{RESET}")
    print(f"  {YELLOW}Ambient (parked):   {stats['ambient']}{RESET}")
    print(f"  {RED}Blocked (off-duty):  {stats['blocked']}{RESET}")
    print()

    # Verify the Context Flush
    assert len(scheduler.session.history) == 0, "History should be empty after flush"
    ok("Context Flush verified — session history is empty")

    # Verify the journal was persisted
    loaded = store.load(agent_id, journal.date)
    assert loaded is not None, "Journal should be persisted to disk"
    ok(f"Journal persistence verified — file for {loaded.date} exists")

    # Verify ambient buffer was drained
    assert buffer.count_pending(agent_id) == 0, "AmbientBuffer should be drained after shift end"
    ok("AmbientBuffer drain verified — 0 pending events")

    # Show journal file
    journal_path = Path("./data/journals") / agent_id / f"{journal.date}.json"
    info(f"Journal file: {journal_path.resolve()}")

    print(f"\n{BOLD}{GREEN}✓ All checks passed. Framework simulation complete.{RESET}\n")

    # ── Day 2: Simulate next morning to prove memory continuity ──
    header("BONUS: NEXT DAY — Cold-start with yesterday's diary")

    # Re-create the scheduler fresh (simulating a new day process start)
    scheduler2 = ShiftScheduler(
        agent_id=agent_id,
        engine=engine,
        buffer=buffer,
        store=store,
        persona_name="ops-bot",
        persona_role="DevOps Assistant",
    )
    # Load today's journal explicitly as "yesterday's diary" for the next day
    latest = store.load_latest(agent_id)
    journal_summary = latest.summary if latest else ""
    scheduler2.session.system_prompt = scheduler2._build_system_prompt(journal_summary)  # type: ignore[attr-defined]
    scheduler2.session.history = [{"role": "system", "content": scheduler2.session.system_prompt}]
    scheduler2.session.state = AgentState.ON_DUTY_IDLE

    ok(f"State: {scheduler2.state.value}")
    info(f"Loaded journal: {latest.date if latest else 'none'}")
    info(f"Pending tasks from diary: {latest.pending_tasks if latest else []}")
    info(f"System prompt includes yesterday's context:")
    info(f"  ...{scheduler2.session.system_prompt[-250:]}")
    print()


if __name__ == "__main__":
    main()
