"""Business Workflow Template — 业务图接入模板.

Defines a concrete workflow graph that models a typical agent task:
  START → classify_intent → handle_task → summarize → END

Also provides a mock LLM call helper (no real API dependency for the demo).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from src.core.types import Artifact, Event, Priority
from src.workflow.engine import WorkflowContext, WorkflowEngine, WorkflowNode

logger = logging.getLogger(__name__)

# ── Mock LLM (for demo — replace with real provider) ─────

class MockLLM:
    """Simulates LLM calls with deterministic outputs for the demo.

    In production, replace with MAF's AI model integration or any provider.
    """

    @staticmethod
    def chat(
        system: str,
        user: str,
        simulate_tokens: int = 20,
    ) -> tuple[str, int]:
        """Return (response_text, tokens_consumed)."""
        time.sleep(0.05)  # simulate latency
        return f"[LLM response to: {user[:60]}...]", simulate_tokens

    @staticmethod
    def summarize(log_text: str, simulate_tokens: int = 80) -> tuple[str, int]:
        """Generate a summary from a log block."""
        time.sleep(0.08)
        lines = log_text.strip().split("\n")[:5]
        summary = f"Today's activity: {len(lines)} log entries processed. " \
                  f"Key items: {', '.join(line[:40] for line in lines if line.strip())}."
        return summary, simulate_tokens


# ── Workflow Node Handlers ────────────────────────────────

def node_classify_intent(ctx: WorkflowContext) -> Artifact:
    """Classify the incoming event/task intent."""
    event_data = ctx.task_input.get("event")
    if isinstance(event_data, dict):
        event_type = event_data.get("event_type", "unknown")
        priority = event_data.get("priority", 3)
    else:
        event_type = "unknown"
        priority = 3

    _, tokens = MockLLM.chat(
        system="You are a task classifier. Reply with one word: CODE, QA, DEPLOY, or CHAT.",
        user=f"Event type={event_type}, payload={ctx.task_input}",
    )

    return Artifact(
        task_id="classify",
        status="completed",
        summary=f"Intent classified as: {event_type}",
        data={"intent": event_type, "priority": priority},
        tokens_consumed=tokens,
    )


def node_handle_task(ctx: WorkflowContext) -> Artifact:
    """Execute the actual task based on classified intent."""
    intent = ctx.node_outputs.get("classify", Artifact()).data.get("intent", "unknown")
    event = ctx.task_input.get("event", {})

    _, tokens = MockLLM.chat(
        system=f"You are a {intent} specialist. Process the task.",
        user=f"Task: {event}",
        simulate_tokens=50,
    )

    return Artifact(
        task_id="handle_task",
        status="completed",
        summary=f"Task handled: {intent}",
        data={"result": f"Completed {intent} task successfully.", "intent": intent, "event": event},
        tokens_consumed=tokens,
    )


def node_summarize(ctx: WorkflowContext) -> Artifact:
    """Generate a structured summary from the full workflow trace."""
    # Collect all node outputs
    parts = []
    total_tokens = 0
    for node_name, art in ctx.node_outputs.items():
        parts.append(f"[{node_name}] {art.summary}")
        total_tokens += art.tokens_consumed

    summary_text = " → ".join(parts)
    return Artifact(
        task_id="summarize",
        status="completed",
        summary=summary_text,
        data={"node_count": len(ctx.node_outputs)},
        tokens_consumed=total_tokens,
    )


# ── Graph Registration ────────────────────────────────────

def build_business_workflow(engine: WorkflowEngine) -> None:
    """Register the standard business workflow graph."""
    nodes = [
        WorkflowNode(
            name="classify",
            handler=node_classify_intent,
            next_node="handle_task",
        ),
        WorkflowNode(
            name="handle_task",
            handler=node_handle_task,
            next_node="summarize",
        ),
        WorkflowNode(
            name="summarize",
            handler=node_summarize,
            is_terminal=True,
        ),
    ]
    engine.register_graph("business_workflow", nodes)
    logger.info("Registered workflow graph: business_workflow (classify → handle_task → summarize)")
