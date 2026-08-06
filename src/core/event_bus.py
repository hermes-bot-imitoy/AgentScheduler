"""Event Bus — 外围事件总线 & 多层级显著性过滤器.

Three-layer filter pipeline:
  Layer 1 — State Mask (0 Token): block non-emergency events when OFF_DUTY
  Layer 2 — Salience Evaluator: compute salience_score, park low-relevance events
  Layer 3 — Wake: pass through to the agent workflow engine

This is the gatekeeper. Every event that reaches an LLM has already
passed through these zero/low-cost filters, keeping Token spend minimal.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

from src.core.types import AgentState, Event, FilterDecision, Priority
from src.storage.ambient_buffer import AmbientBuffer

logger = logging.getLogger(__name__)

# ── Salience threshold ─────────────────────────────────────
SALIENCE_THRESHOLD: float = 0.4


# ── Protocol for persona relevance scoring ─────────────────

class PersonaRelevanceFn(Protocol):
    """A policy function: given an event, return [0.0 .. 1.0] relevance to the agent's persona."""
    def __call__(self, event: Event) -> float: ...


# ── Default relevance policy ───────────────────────────────

# # 默认相关性评分器(关键词匹配). 生产环境应替换为RAG/embedding. 返回0~1分数
def _default_relevance(event: Event) -> float:
    """Simple keyword-based relevance scorer. Replace with RAG/embedding for production.

    Checks both the event_type and payload for persona-relevant keywords.
    """
    source_keywords: dict[str, set[str]] = {
        "github": {"pr", "issue", "review", "deploy", "bug", "fix", "security", "release"},
        "email":  {"urgent", "boss", "deadline", "report", "incident"},
        "slack":  {"@mention", "direct", "ask", "help", "urgent"},
        "cron":   {"backup", "healthcheck", "alert", "failure", "error"},
    }
    source_words = source_keywords.get(event.source.lower(), set())

    # Check event_type first
    event_type_text = event.event_type.lower()
    payload_text = str(event.payload).lower()
    combined_text = f"{event_type_text} {payload_text}"

    # Count keyword hits
    hits = sum(1 for kw in source_words if kw in combined_text)

    # Bonus: "urgent" anywhere is a strong signal
    urgent_bonus = 0.2 if "urgent" in combined_text else 0.0
    # Bonus: HIGH/EMERGENCY priority events get inherent relevance boost
    priority_bonus = 0.0
    if event.priority >= Priority.HIGH:
        priority_bonus = 0.15

    # Score: base 0.25 + hit bonus + urgent bonus + priority bonus
    base = 0.25
    hit_bonus = min(0.5, 0.15 * hits)
    return min(1.0, base + hit_bonus + urgent_bonus + priority_bonus)


# ── Event Bus ───────────────────────────────────────────────

# # 事件总线: 3层过滤器. Layer1=状态掩码(0Token), Layer2=显著性(关键词+优先级), Layer3=唤醒
class EventBus:
    """The central event bus with multi-layer filtering.

    Usage:
        bus = EventBus(buffer=AmbientBuffer())
        bus.set_state_getter(lambda: agent.state)           # Layer 1
        bus.set_relevance_fn(my_custom_relevance_fn)         # Layer 2
        decision = bus.process_event(event)                   # Run pipeline
    """

    def __init__(
        self,
        buffer: Optional[AmbientBuffer] = None,
        salience_threshold: float = SALIENCE_THRESHOLD,
    ):
        self._buffer = buffer or AmbientBuffer()
        self._salience_threshold = salience_threshold

        # Runtime hooks (injected by the agent runtime)
        self._state_getter: Callable[[], AgentState] = lambda: AgentState.ON_DUTY_IDLE
        self._relevance_fn: PersonaRelevanceFn = _default_relevance

        # 定时事件调度表: {event_id: (trigger_tick, Event)} — 时间线程到期投递
        self._tick_schedule: dict[str, tuple[int, Event]] = {}

        # Metrics
        self.stats: dict[str, int] = {
            "total_events": 0,
            "passed": 0,
            "ambient": 0,
            "blocked": 0,
            "dropped": 0,
        }

    # ── Configuration ─────────────────────────────────────

    def set_state_getter(self, fn: Callable[[], AgentState]) -> None:
        self._state_getter = fn

    def set_relevance_fn(self, fn: PersonaRelevanceFn) -> None:
        self._relevance_fn = fn

    # ── Event registration (时间与事件深度绑定) ──────────

    def register_event(self, event: Event, tick: Optional[int] = None) -> str:
        """向事件总线注册一个事件.

        tick 语义 (时间与事件深度绑定):
          - tick=None (默认): 立即触发 — 直接进入 3 层过滤管线
          - tick=整数:        在指定绝对 Tick 触发 — 存入调度表,
                              由时间线程 (TimeEventBus) 到期自动投递.

        参数:
            event: 要注册的 Event.
            tick:  触发 Tick (绝对 Tick, 自系统启动累计). None = 立即.

        返回:
            事件 ID.
        """
        if tick is None:
            # 立即触发: 进入 3 层过滤管线
            self.process_event(event)
        else:
            event.trigger_tick = tick
            self._tick_schedule[event.id] = (tick, event)
            logger.info("EventBus 注册定时事件: id=%s type=%s → tick %d (立即触发=%s)",
                        event.id, event.event_type, tick, False)
        return event.id

    def cancel_event(self, event_id: str) -> bool:
        """取消一个已注册的定时事件 (仅调度表中的)."""
        return self._tick_schedule.pop(event_id, None) is not None

    def list_scheduled_events(self) -> list[dict[str, Any]]:
        """列出待触发的定时事件 (按触发 Tick 排序)."""
        return [
            {"event_id": eid, "tick": tk, "type": ev.event_type,
             "target_role": ev.target_role}
            for eid, (tk, ev) in sorted(self._tick_schedule.items(),
                                        key=lambda kv: kv[1][0])
        ]

    def _check_due_events(self, current_tick: int) -> list[Event]:
        """检查并取回到期事件 (由时间线程周期性调用).

        参数:
            current_tick: 当前绝对 Tick.

        返回:
            到期的事件列表 (已从调度表移除, 调用方负责投递).
        """
        due = []
        for eid, (tk, ev) in list(self._tick_schedule.items()):
            if current_tick >= tk:
                del self._tick_schedule[eid]
                due.append(ev)
        return due

    # ── Pipeline ──────────────────────────────────────────

# # 运行3层过滤管线. 参数event=事件, agent_id=Agent标识. 返回FilterDecision. 被拦截事件自动存入AmbientBuffer
    def process_event(self, event: Event, agent_id: str = "default") -> FilterDecision:
        """Run the event through the 3-layer filter pipeline.

        Returns the final FilterDecision. Side-effects:
          - BLOCKED/AMBIENT events are parked in AmbientBuffer.
          - PASS events are returned ready for the workflow engine.
          - Stats are updated.
        """
        self.stats["total_events"] += 1
        logger.debug("EventBus received: id=%s type=%s priority=%s", event.id, event.event_type, event.priority.name)

        # ── Layer 1: State Mask (0 Token) ──────────────────
        current_state = self._state_getter()
        if current_state in (AgentState.OFF_DUTY, AgentState.WRAPPING_UP):
            if event.priority < Priority.EMERGENCY:
                event.filter_decision = FilterDecision.BLOCKED
                event.blocked_reason = f"Agent is {current_state.value} (non-emergency)"
                self._buffer.append(agent_id, event)
                self.stats["blocked"] += 1
                logger.info("Layer1 BLOCKED: %s (state=%s)", event.id, current_state.value)
                return FilterDecision.BLOCKED

        # ── Layer 2: Salience Evaluator ────────────────────
        relevance = self._relevance_fn(event)
        event.salience_score = event.priority.value / Priority.EMERGENCY.value * relevance

        if event.salience_score < self._salience_threshold:
            event.filter_decision = FilterDecision.AMBIENT
            event.blocked_reason = (
                f"Salience {event.salience_score:.2f} < threshold {self._salience_threshold}"
            )
            self._buffer.append(agent_id, event)
            self.stats["ambient"] += 1
            logger.info("Layer2 AMBIENT: %s (score=%.2f)", event.id, event.salience_score)
            return FilterDecision.AMBIENT

        # ── Layer 3: Wake ─────────────────────────────────
        event.filter_decision = FilterDecision.PASS
        self.stats["passed"] += 1
        logger.info("Layer3 PASS: %s → workflow engine", event.id)
        return FilterDecision.PASS

    def get_stats(self) -> dict[str, int]:
        return dict(self.stats)

    def reset_stats(self) -> None:
        for k in self.stats:
            self.stats[k] = 0
