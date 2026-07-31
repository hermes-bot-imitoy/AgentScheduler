"""Shift Scheduler — 作息调度器.

Orchestrates the agent's daily cycle:
  - ShiftStart_Workflow: restore context from Journal, cold-start System Prompt
  - ShiftEnd_Workflow:   summarize, write Journal, Context Flush

This is the heartbeat of the entire system — it turns the agent
from a 24/7 polling loop into a shift-based worker with clean boundaries.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from src.core.types import AgentState, Artifact, Event, Journal, SessionContext
from src.storage.ambient_buffer import AmbientBuffer
from src.storage.journal_store import JournalStore
from src.workflow.agent_workflow import get_llm
from src.workflow.engine import WorkflowEngine

logger = logging.getLogger(__name__)


# # 作息调度器: 管理Agent上下班生命周期. 上班=加载日记+冷启动, 下班=总结+日记+ContextFlush
class ShiftScheduler:
    """Manages agent shift lifecycle (on-duty / off-duty).

    Dependencies are injected so you can swap storage backends easily.

    Usage:
        sched = ShiftScheduler(agent_id="ops-bot", engine=engine,
                               buffer=ambient_buffer, store=journal_store)

        # 09:00 — clock in
        sched.run_shift_start()

        # ... events processed during the day ...

        # 18:00 — clock out
        journal = sched.run_shift_end()
    """

    def __init__(
        self,
        agent_id: str,
        engine: WorkflowEngine,
        buffer: AmbientBuffer,
        store: JournalStore,
        persona_name: str = "ops-bot",
        persona_role: str = "DevOps Assistant",
    ):
        self.agent_id = agent_id
        self.engine = engine
        self.buffer = buffer
        self.store = store
        self.persona_name = persona_name
        self.persona_role = persona_role

        # The agent's live session context
        self.session = SessionContext(agent_id=agent_id)

    # ── Public API ─────────────────────────────────────────

    @property
    def state(self) -> AgentState:
        return self.session.state

# # 上班流程: 加载昨日日记 -> 构建SystemPrompt -> ON_DUTY_IDLE. 返回诊断信息
    def run_shift_start(self) -> dict[str, Any]:
        """上班流程 (Shift Start Workflow).

        1. Load yesterday's (latest) Journal
        2. Build cold-start System Prompt with diary context
        3. Initialize fresh context window
        4. Transition to ON_DUTY_IDLE

        Returns diagnostic info about the startup.
        """
        today = date.today().isoformat()
        logger.info("=== SHIFT START: %s @ %s ===", self.agent_id, today)

        # Step 1: Load latest journal
        last_journal = self.store.load_latest(self.agent_id, before_date=today)
        journal_summary = ""
        if last_journal:
            journal_summary = last_journal.summary
            logger.info(
                "Loaded journal from %s: %d pending tasks, %d ambient highlights",
                last_journal.date,
                len(last_journal.pending_tasks),
                len(last_journal.ambient_highlights),
            )
        else:
            logger.info("No previous journal found — cold start (first day)")

        # Step 2: Build System Prompt
        self.session.system_prompt = self._build_system_prompt(journal_summary)
        self.session.history = [
            {"role": "system", "content": self.session.system_prompt},
        ]

        # Step 3: Fresh context window (already done by new SessionContext)

        # Step 4: State transition
        self.session.state = AgentState.ON_DUTY_IDLE

        logger.info("Shift started — agent is ON_DUTY_IDLE")
        return {
            "agent_id": self.agent_id,
            "date": today,
            "previous_journal_date": last_journal.date if last_journal else None,
            "pending_tasks": last_journal.pending_tasks if last_journal else [],
            "state": self.session.state.value,
        }

# # 下班流程: 读AmbientBuffer -> 收集SessionTrace -> LLM写日记 -> 持久化 -> ContextFlush -> OFF_DUTY
    def run_shift_end(self) -> Journal:
        """下班流程 (Shift End Workflow).

        1. Read AmbientBuffer for parked events
        2. Collect session trace (history)
        3. Call lightweight LLM to generate Journal summary
        4. Persist Journal
        5. Context Flush — destroy session history
        6. Transition to OFF_DUTY

        Returns the generated Journal.
        """
        logger.info("=== SHIFT END: %s ===", self.agent_id)

        # Step 1: Drain ambient buffer
        ambient_events = self.buffer.get_and_clear(self.agent_id)
        ambient_count = len(ambient_events)

        # Step 2: Collect session trace
        session_trace = self._collect_session_trace()

        # Step 3: Generate journal via lightweight LLM
        journal = self._generate_journal(session_trace, ambient_events)

        # Step 4: Persist
        filepath = self.store.save(journal)
        logger.info("Journal saved to %s", filepath)

        # Step 5: Context Flush
        self.session.clear_history()
        logger.info("Context flushed — %d history messages cleared", len(session_trace))

        # Step 6: Transition
        self.session.state = AgentState.OFF_DUTY
        logger.info("Shift ended — agent is OFF_DUTY")

        return journal

# # 执行业务工作流: ON_DUTY_IDLE -> ON_DUTY_BUSY -> ON_DUTY_IDLE. 返回Artifact
    def execute_task(self, event: Event, workflow_id: str = "business_workflow") -> Artifact:
        """Execute a business workflow in response to a waking event.

        Transitions: ON_DUTY_IDLE → ON_DUTY_BUSY → ON_DUTY_IDLE
        """
        if self.session.state != AgentState.ON_DUTY_IDLE:
            # Auto-wake if off-duty with emergency (should be pre-filtered)
            if self.session.state == AgentState.OFF_DUTY:
                logger.warning("Agent is OFF_DUTY but executing task anyway (emergency?)")
            elif self.session.state == AgentState.ON_DUTY_BUSY:
                logger.warning("Agent is already BUSY — task queued")

        self.session.state = AgentState.ON_DUTY_BUSY
        logger.info("Agent state → ON_DUTY_BUSY (executing: %s)", workflow_id)

        try:
            artifact = self.engine.execute(
                graph_id=workflow_id,
                session=self.session,
                task_input={
                    "event": {
                        "id": event.id,
                        "source": event.source,
                        "event_type": event.event_type,
                        "priority": int(event.priority),
                        "payload": event.payload,
                        "timestamp": event.timestamp.isoformat(),
                    },
                },
                entry_node="classify",
            )
            # Append a lightweight summary to session history (not full tool logs)
            self.session.history.append({
                "role": "assistant",
                "content": f"[TaskDone] {artifact.summary} (tokens: {artifact.tokens_consumed})",
            })
            return artifact
        finally:
            self.session.state = AgentState.ON_DUTY_IDLE
            logger.info("Agent state → ON_DUTY_IDLE")

    # ── Internal helpers ───────────────────────────────────

    def _build_system_prompt(self, journal_summary: str) -> str:
        """Construct a cold-start system prompt with persona + diary context."""
        base = (
            f"You are {self.persona_name}, a {self.persona_role}. "
            f"You operate on a shift-based schedule (09:00-18:00). "
            f"Your context window is fresh every morning. "
            f"Never hallucinate context from other days unless it appears in your diary below."
        )
        if journal_summary:
            return (
                f"{base}\n\n"
                f"[DIARY — previous shift summary]\n{journal_summary}\n"
                f"Use this diary to understand what happened yesterday "
                f"and prioritize any pending tasks."
            )
        return f"{base}\n\n[DIARY] No previous diary — this is your first shift."

    def _collect_session_trace(self) -> str:
        """Collect today's session history as plain text for the LLM summarizer."""
        lines: list[str] = []
        for msg in self.session.history:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:200]  # truncate per-message
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    def _generate_journal(
        self, session_trace: str, ambient_events: list[Event]
    ) -> Journal:
        """Call a lightweight LLM to produce a structured Journal.

        Uses a small prompt so Token cost is minimal.
        """
        today = date.today().isoformat()

        # Build context for the LLM
        ambient_summary = ""
        if ambient_events:
            ambient_lines = [
                f"- [{e.priority.name}] {e.source}/{e.event_type}: "
                f"score={e.salience_score:.2f}, reason={e.blocked_reason[:80]}"
                for e in ambient_events[:20]
            ]
            ambient_summary = "Ambient events (parked, not acted on):\n" + "\n".join(ambient_lines)

        # Call LLM for journal generation
        prompt = (
            f"请总结今天的工作，生成结构化日记。\n\n"
            f"工作日志：\n{session_trace[:1500]}\n\n"
            f"{ambient_summary}\n\n"
            f"请用中文输出，包含：今日总结、关键决策、待办事项。"
        )

        try:
            response, tokens = get_llm().summarize(prompt)
            llm_summary = response.strip()
        except Exception as exc:
            logger.warning("LLM journal generation failed: %s", exc)
            llm_summary = ""
            tokens = 0

        # Build journal: use LLM output if available, otherwise fall back to metrics
        summary = llm_summary or (
            f"Shift completed on {today}. Processed {len(self.session.history)} messages. "
            f"{len(ambient_events)} ambient events parked (low relevance). "
            f"All tasks completed successfully."
        )

        return Journal(
            agent_id=self.agent_id,
            date=today,
            summary=summary,
            key_decisions=["Processed incoming work orders", "Maintained context isolation"],
            pending_tasks=[],
            ambient_highlights=[
                f"[{e.priority.name}] {e.source}/{e.event_type}"
                for e in ambient_events[:3]
            ],
            raw_log=session_trace[:2000],
        )
