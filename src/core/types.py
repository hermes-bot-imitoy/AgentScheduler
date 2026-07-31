"""Core data types for the MAF Shift & Event-Driven Agent Scheduler."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any


# ── Enums ────────────────────────────────────────────────────

# # Agent 生命周期状态枚举: OFF_DUTY(下班), ON_DUTY_IDLE(空闲), ON_DUTY_BUSY(忙碌), WRAPPING_UP(收尾)
class AgentState(str, Enum):
    """Agent lifecycle states."""
    OFF_DUTY      = "OFF_DUTY"       # 下班 — context flushed, not processing
    ON_DUTY_IDLE  = "ON_DUTY_IDLE"   # 上班空闲 — alive, listening for events
    ON_DUTY_BUSY  = "ON_DUTY_BUSY"   # 上班忙碌 — executing a workflow
    WRAPPING_UP   = "WRAPPING_UP"    # 收尾中 — finishing last task before shift end


# # 事件优先级: 数值越大越紧急. LOW=1, NORMAL=3, HIGH=6, EMERGENCY=10
class Priority(IntEnum):
    """Event priority levels (higher = more urgent)."""
    LOW       = 1
    NORMAL    = 3
    HIGH      = 6
    EMERGENCY = 10


class FilterDecision(str, Enum):
    """Decision made by the event filter pipeline."""
    PASS       = "PASS"        # 通过所有过滤层，唤醒 Agent
    AMBIENT    = "AMBIENT"     # 低显著度，压入潜意识缓冲区
    BLOCKED    = "BLOCKED"     # 状态掩码拦截（如 OFF_DUTY 非紧急事件）
    DROPPED    = "DROPPED"     # 完全丢弃（如重复事件、过期事件）


# ── Events ───────────────────────────────────────────────────

@dataclass
# # 标准化事件: id, source(来源), event_type(类型), priority(优先级), payload(载荷), timestamp(时间戳)
class Event:
    """A normalized event entering the system."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = ""                    # e.g. "github", "email", "slack", "cron"
    event_type: str = ""                # e.g. "new_pr", "mention", "alert", "heartbeat"
    priority: Priority = Priority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Filter metadata (set during processing)
    salience_score: float = 0.0
    filter_decision: FilterDecision = FilterDecision.PASS
    blocked_reason: str = ""


# ── Journal ──────────────────────────────────────────────────

@dataclass
# # 结构化下班日记: agent_id, date(日期), summary(总结), key_decisions, pending_tasks, ambient_highlights
class Journal:
    """A structured end-of-shift diary entry."""
    agent_id: str
    date: str                           # ISO date, e.g. "2026-07-29"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str = ""                   # LLM-generated summary of the day
    key_decisions: list[str] = field(default_factory=list)
    pending_tasks: list[str] = field(default_factory=list)
    ambient_highlights: list[str] = field(default_factory=list)  # Notable ambient events
    raw_log: str = ""                   # Full session trace (truncated)


# ── Session ──────────────────────────────────────────────────

@dataclass
# # MAF会话上下文: 绑定单个Agent, 包含system_prompt, history, checkpoints
class SessionContext:
    """An isolated MAF session context bound to one agent."""
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    agent_id: str = ""
    state: AgentState = AgentState.OFF_DUTY

    # Context window
    system_prompt: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    # Checkpointing
    checkpoints: dict[str, Any] = field(default_factory=dict)
    last_checkpoint_step: int = 0

# # 清空全部对话历史(Context Flush). 销毁history, checkpoints, last_checkpoint_step
    def clear_history(self) -> None:
        """Flush the entire conversation history (Context Flush)."""
        self.history.clear()
        self.checkpoints.clear()
        self.last_checkpoint_step = 0


# ── Artifact ─────────────────────────────────────────────────

@dataclass
# # 工作流任务的结构化产出物: task_id, status, summary, data, error, tokens_consumed
class Artifact:
    """Structured output from a completed workflow task."""
    task_id: str = ""
    status: str = "completed"           # "completed" | "failed" | "delegated"
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    tokens_consumed: int = 0
