"""Workflow Engine — 底层有状态图执行器.

Provides:
  - Stateful sessions with checkpointing
  - Isolated contexts per sub-task (no leaky tool logs)
  - Node-based workflow graph execution
  - Artifact-first returns (structured, not raw chat history)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.core.types import AgentState, Artifact, SessionContext

logger = logging.getLogger(__name__)

# ── Node types (graph building blocks) ────────────────────

WorkflowNodeFn = Callable[["WorkflowContext"], Artifact]


@dataclass
# # 工作流图节点: name, handler, next_node, conditional_routes, is_terminal
class WorkflowNode:
    """A single node in a workflow graph."""
    name: str
    handler: WorkflowNodeFn
    next_node: str | None = None           # single successor
    conditional_routes: dict[str, str] = field(default_factory=dict)  # status → node_name
    is_terminal: bool = False


@dataclass
# # 工作流运行时上下文: session, task_input, node_outputs, metadata
class WorkflowContext:
    """Runtime context passed through nodes."""
    session: SessionContext
    task_input: dict[str, Any] = field(default_factory=dict)
    node_outputs: dict[str, Artifact] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Engine ─────────────────────────────────────────────────

# # 无状态工作流图执行器. 每个任务创建隔离上下文, 防止工具日志跨任务泄露
class WorkflowEngine:
    """Stateless executor that drives a workflow graph through a SessionContext.

    Each task execution creates an isolated WorkflowContext so that:
      - Tool call logs from task A don't leak into task B
      - Checkpoints are per-node, enabling recovery
      - Only structured Artifacts are returned to the caller
    """

    def __init__(self):
        self._graphs: dict[str, dict[str, WorkflowNode]] = {}  # graph_id → {node_name → node}

    # ── Graph management ──────────────────────────────────

# # 注册命名工作流图. graph_id=图标识, nodes=节点列表
    def register_graph(self, graph_id: str, nodes: list[WorkflowNode]) -> None:
        """Register a named workflow graph."""
        self._graphs[graph_id] = {n.name: n for n in nodes}

    def get_graph(self, graph_id: str) -> dict[str, WorkflowNode]:
        if graph_id not in self._graphs:
            raise KeyError(f"Unknown graph: {graph_id}")
        return self._graphs[graph_id]

    # ── Execution ─────────────────────────────────────────

# # 执行工作流图. graph_id, session, task_input, entry_node. 返回Artifact(仅结构化产出)
    def execute(
        self,
        graph_id: str,
        session: SessionContext,
        task_input: dict[str, Any],
        entry_node: str = "start",
    ) -> Artifact:
        """Execute a workflow graph with an isolated context.

        Args:
            graph_id: The registered workflow to run.
            session: The agent's session (history is appended but NOT the full tool log).
            task_input: Input data for this task.
            entry_node: Which node to start from.

        Returns:
            An Artifact summarizing the result (NOT raw LLM history).
        """
        graph = self.get_graph(graph_id)
        if entry_node not in graph:
            raise KeyError(f"Entry node '{entry_node}' not found in graph '{graph_id}'")

        ctx = WorkflowContext(session=session, task_input=task_input)

        # ── Run the graph ─────────────────────────────────
        current = entry_node
        visited: set[str] = set()
        last_artifact: Optional[Artifact] = None

        while True:
            if current in visited:
                raise RuntimeError(f"Cycle detected at node '{current}'")
            visited.add(current)

            node = graph[current]
            logger.info("Executing node: %s (graph=%s)", node.name, graph_id)

            # Checkpoint before execution
            session.checkpoints[f"{graph_id}:{node.name}"] = {
                "task_input": task_input,
                "node_outputs": {k: v.summary for k, v in ctx.node_outputs.items()},
            }
            session.last_checkpoint_step += 1

            # Execute the node handler
            try:
                artifact = node.handler(ctx)
            except Exception as exc:
                logger.exception("Node '%s' failed", node.name)
                artifact = Artifact(
                    task_id=f"{graph_id}:{node.name}",
                    status="failed",
                    summary=f"Node '{node.name}' raised {type(exc).__name__}: {exc}",
                    error=str(exc),
                )

            ctx.node_outputs[node.name] = artifact
            last_artifact = artifact

            # Terminal node → stop
            if node.is_terminal:
                break

            # Route to next node
            next_node = self._resolve_next(node, artifact)
            if next_node is None:
                break  # no successor, implicit terminal

            current = next_node

        # ── Clear tool-level artifacts, return only the summary ──
        final = last_artifact or Artifact(
            task_id=graph_id, status="completed", summary="Workflow completed with no terminal artifact"
        )
        return final

    # ── Routing ───────────────────────────────────────────

    def _resolve_next(self, node: WorkflowNode, artifact: Artifact) -> str | None:
        """Determine the next node based on the artifact status and conditional routes."""
        if artifact.status in node.conditional_routes:
            return node.conditional_routes[artifact.status]
        return node.next_node
