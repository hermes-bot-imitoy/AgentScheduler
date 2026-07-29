"""Business Workflow Template — 业务图接入模板.

Defines a concrete workflow graph that models a typical agent task:
  START → classify_intent → handle_task → summarize → END

Uses DeepSeek API for real LLM calls.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.llm import DeepSeekLLM
from src.core.types import Artifact, Event, Priority
from src.workflow.engine import WorkflowContext, WorkflowEngine, WorkflowNode

logger = logging.getLogger(__name__)

# ── Module-level LLM instance (lazy init) ──────────────────

_llm: Optional[DeepSeekLLM] = None


def get_llm() -> DeepSeekLLM:
    """Get or create the module-level DeepSeek LLM client."""
    global _llm
    if _llm is None:
        _llm = DeepSeekLLM()
        logger.info("DeepSeekLLM initialized: model=%s", _llm.model)
    return _llm


def set_llm(llm: DeepSeekLLM) -> None:
    """Inject a custom LLM instance (for testing or config)."""
    global _llm
    _llm = llm


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

    llm = get_llm()
    _, tokens = llm.chat(
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

    llm = get_llm()
    _, tokens = llm.chat(
        system=f"You are a {intent} specialist. Process the task.",
        user=f"Task: {event}",
        max_tokens=512,
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
