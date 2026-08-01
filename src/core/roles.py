"""Role System — 多角色并发任务调度.

Each role has:
  - Persona (name, personality, position, skills)
  - Thread-safe priority task queue (sorted by urgency)
  - Dedicated LLM session (role-specific system prompt)
  - Independent worker thread

RolePool manages all roles with a ThreadPoolExecutor, routing
incoming events/tasks to appropriate roles.
"""

from __future__ import annotations

import heapq
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

from src.core.llm import DeepSeekLLM
from src.core.types import AgentState, Event, Priority

logger = logging.getLogger(__name__)


# ── Urgency ────────────────────────────────────────────────

class Urgency(IntEnum):
    """Task urgency — higher = more urgent, processed first."""
    LOW      = 1
    NORMAL   = 3
    HIGH     = 6
    CRITICAL = 10


# ── Task ───────────────────────────────────────────────────

@dataclass(order=True)
class Task:
    """A task in a role's queue. Orderable by urgency (descending)."""
    urgency: int = field(compare=True)       # negative for max-heap behaviour
    task_id: str = field(compare=False, default_factory=lambda: uuid.uuid4().hex[:8])
    description: str = field(compare=False, default="")
    source: str = field(compare=False, default="")          # where the task came from
    context: dict[str, Any] = field(compare=False, default_factory=dict)
    created_at: float = field(compare=False, default_factory=time.time)

    # Result fields (set after execution)
    status: str = field(compare=False, default="pending")   # pending|running|done|failed
    result: str = field(compare=False, default="")
    tokens_consumed: int = field(compare=False, default=0)
    assigned_role: str = field(compare=False, default="")

    def __post_init__(self):
        # Negate urgency so heapq (min-heap) behaves as max-heap
        # i.e. CRITICAL(10) → -10 is popped before HIGH(6) → -6
        self.urgency = -int(self.urgency) if self.urgency > 0 else self.urgency


# ── AgentRole ──────────────────────────────────────────────

@dataclass
class AgentRole:
    """A role definition with persona, LLM binding, and task queue."""

    name: str                                              # person name, e.g. "张三", "李四"
    role_id: str = ""                                      # functional role, e.g. "coder", "reviewer"
    title: str = ""                                        # e.g. "Senior Backend Engineer"
    responsibilities: str = ""                             # e.g. "编写代码，修复Bug，实现新功能"
    personality: str = ""                                  # e.g. "严谨细致，追求代码质量"
    skills: list[str] = field(default_factory=list)        # e.g. ["Python", "Go", "K8s"]
    system_prompt_extra: str = ""                          # appended to base system prompt
    is_default: bool = False                               # marked as a default/critical role

    # ── Event filter state (per-role) ─────────────────────
    state: AgentState = AgentState.ON_DUTY_IDLE            # role-specific lifecycle
    salience_threshold: float = 0.4                        # per-role override
    interest_keywords: set[str] = field(default_factory=set)  # keywords this role cares about

    # Internal state (managed by RolePool)
    _queue: list[Task] = field(default_factory=list, repr=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)
    _current_task: Optional[Task] = field(default=None, repr=False, init=False)
    _running: bool = field(default=True, repr=False, init=False)
    _llm: Optional[DeepSeekLLM] = field(default=None, repr=False, init=False)
    _tools: Any = field(default=None, repr=False, init=False)  # ToolRegistry, lazy init
    _pool: Any = field(default=None, repr=False, init=False)   # RolePool back-reference for talk
    _note_store: Any = field(default=None, repr=False, init=False)  # NoteStore, lazy init

    # Callbacks
    on_task_start: Optional[Callable[[AgentRole, Task], None]] = field(default=None, repr=False, init=False)
    on_task_done: Optional[Callable[[AgentRole, Task], None]] = field(default=None, repr=False, init=False)

    # ── Event Filter (per-role Layer 1-3) ──────────────────

    def evaluate_event(self, event: Event) -> tuple[bool, str]:
        """Run per-role 3-layer filter on an event.

        Returns (should_process, reason).
        Layer 1: state mask (OFF_DUTY blocks non-EMERGENCY)
        Layer 2: salience = (priority/EMERGENCY) * keyword_relevance
        Layer 3: PASS if score >= threshold
        """
        # Layer 1: State Mask
        if self.state in (AgentState.OFF_DUTY, AgentState.WRAPPING_UP):
            if event.priority < Priority.EMERGENCY:
                return False, f"Role {self.name} is {self.state.value}"

        # Layer 2: Salience — keyword-based relevance per role
        relevance = 0.25  # base
        payload_text = str(event.payload).lower()
        event_text = f"{event.event_type.lower()} {payload_text}"

        if self.interest_keywords:
            hits = sum(1 for kw in self.interest_keywords if kw in event_text)
            # Boost: each hit adds 0.25, not 0.15 (since per-role keywords are narrow)
            relevance += min(0.60, 0.25 * hits)

        # Bonus for matching skills (partial match)
        skill_text = " ".join(self.skills).lower()
        for word in event_text.split():
            if word in skill_text:
                relevance += 0.10
                break

        # Urgency bonus — stronger for per-role matching
        if "urgent" in event_text or "critical" in event_text or "紧急" in event_text:
            relevance += 0.15

        relevance = min(1.0, relevance)
        # Blended score: 40% priority weight + 60% relevance weight.
        # This lets NORMAL-priority events pass when relevance is high,
        # while keeping LOW-priority spam below threshold.
        score = event.priority.value / 10.0 * 0.4 + relevance * 0.6

        if score < self.salience_threshold:
            return False, f"Salience {score:.2f} < threshold {self.salience_threshold} (relevance={relevance:.2f})"

        # Layer 3: PASS
        return True, f"PASS (score={score:.2f}, relevance={relevance:.2f})"

    def event_to_task(self, event: Event) -> Task:
        """Convert a passed Event into a Task for this role's queue."""
        # Map Priority → Urgency
        urgency_map = {
            Priority.LOW: Urgency.LOW,
            Priority.NORMAL: Urgency.NORMAL,
            Priority.HIGH: Urgency.HIGH,
            Priority.EMERGENCY: Urgency.CRITICAL,
        }
        urgency = urgency_map.get(event.priority, Urgency.NORMAL)

        description = f"[{event.source}/{event.event_type}] {event.payload.get('title', str(event.payload)[:100])}"

        return Task(
            urgency=urgency,
            description=description,
            source=event.source,
            context={"event_id": event.id, "payload": event.payload},
        )

    # ── Persona ────────────────────────────────────────────

    def build_system_prompt(self) -> str:
        """Construct the role's full system prompt from persona fields."""
        parts = [
            f"你是 {self.name}，职位是 {self.title}，负责 {self.role_id} 工作。",
            f"性格特点：{self.personality}。",
        ]
        if self.skills:
            parts.append(f"技能：{', '.join(self.skills)}。")
        if self.system_prompt_extra:
            parts.append(self.system_prompt_extra)
        return "\n".join(parts)

    # ── Queue operations (thread-safe) ─────────────────────

    def add_task(self, task: Task) -> None:
        """Add a task to this role's priority queue. Thread-safe."""
        task.assigned_role = self.role_id
        with self._lock:
            heapq.heappush(self._queue, task)
            logger.info(
                "[%s] Task queued: %s (urgency=%s, queue_depth=%d)",
                self.role_id, task.task_id, Urgency(-task.urgency).name, len(self._queue),
            )

    def pop_task(self) -> Optional[Task]:
        """Pop the highest-urgency task. Returns None if queue is empty."""
        with self._lock:
            if not self._queue:
                return None
            return heapq.heappop(self._queue)

    def peek_next_urgency(self) -> Optional[Urgency]:
        """Peek at the next task's urgency without removing."""
        with self._lock:
            if not self._queue:
                return None
            return Urgency(-self._queue[0].urgency)

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def current_task(self) -> Optional[Task]:
        return self._current_task

    @property
    def is_busy(self) -> bool:
        return self._current_task is not None

    # ── Note store (per-role file storage) ─────────────────

    @property
    def note_store(self) -> Any:
        """获取该角色的笔记存储实例 (惰性初始化, 按 role_id 隔离).

        每个角色独立目录: data/notes/<role_id>/.
        """
        if self._note_store is None:
            from src.core.note_store import NoteStore
            self._note_store = NoteStore(role_id=self.role_id)
        return self._note_store

    def get_latest_summary(self, before_date: Optional[str] = None) -> Optional[str]:
        """读取该角色最近一次的每日总结 (用于下一天冷启动提示词).

        参数:
            before_date: 截止日期 (ISO 格式, 可选).

        返回:
            最近总结内容, 没有则返回 None.
        """
        return self.note_store.get_latest_summary(before_date)

    # ── MCP & Python Tool Management ────────────────────────

    def add_mcp_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], str],
    ) -> None:
        """Register a single Python/MCP tool for this role (backward-compatible).

        For bulk tool registration, use add_toolkit() instead.
        """
        from src.core.tools import ToolRegistry

        if self._tools is None:
            self._tools = ToolRegistry()
        self._tools.add_tool(name, description, input_schema, handler)

    def add_toolkit(self, toolkit: Any) -> int:
        """导入整个工具类. 参数：toolkit=ToolKit实例. 返回新增工具数（跳过重复）."""
        from src.core.tools import ToolRegistry

        if self._tools is None:
            self._tools = ToolRegistry()

        # 记忆工具类自动绑定该角色的 NoteStore (内容按角色隔离)
        if toolkit.name == "memory":
            from src.python_tools.memory_toolkit import bind_store_to_toolkit
            bind_store_to_toolkit(toolkit, self.note_store)

        return self._tools.add_toolkit(toolkit)

    @property
    def mcp_tool_names(self) -> list[str]:
        if self._tools is None:
            return []
        return self._tools.tool_names

    # ── Inter-role Communication (talk) ────────────────────

    def _register_talk_tool(self) -> None:
        """自动注册 talk 工具类. 在 RolePool.start() 时调用, 将 communication 工具类注入角色."""
        if self._pool is None:
            return
        if "talk" in self.mcp_tool_names:
            return  # already registered

        from src.python_tools.talk_toolkit import create_talk_toolkit

        tk = create_talk_toolkit(self._pool)
        added = self.add_toolkit(tk)
        logger.info("[%s] talk toolkit loaded — %d tools", self.role_id, added)

    def talk_to(self, target: str, message: str, urgency: str = "NORMAL") -> str:
        """Programmatic inter-role communication (non-LLM path)."""
        return self._tools.call_tool("talk", {
            "target": target,
            "message": message,
            "urgency": urgency,
        }).content[0].text

    # ── Tool-calling LLM execution ─────────────────────────

    def _execute_with_tools(self, task: Task) -> tuple[str, int]:
        """Execute a task with tool-calling loop.

        1. Send system prompt + tools + task to LLM
        2. If LLM responds with a tool_call, execute tool and feed result back
        3. Loop until LLM gives final response (no tool_call)
        4. Return (final_text, total_tokens)
        """
        assert self._llm is not None

        system = self.build_system_prompt()
        tools_prompt = ""
        if self._tools is not None:
            tools_prompt = self._tools.get_tools_prompt()

        full_system = system
        if tools_prompt:
            full_system += "\n\n" + tools_prompt

        messages: list[dict[str, str]] = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": task.description},
        ]

        total_tokens = 0
        max_rounds = 5  # prevent infinite loops

        for _round in range(max_rounds):
            # Build conversation for this round
            msgs_for_api = [{"role": m["role"], "content": m["content"]} for m in messages]

            response_text, usage = self._llm._call_api(msgs_for_api, 0.7, 512)
            round_tokens = usage.get("total_tokens", 0) if usage else 0
            total_tokens += round_tokens

            # Check for tool_call
            tool_name, tool_args = self._parse_tool_call(response_text)

            if tool_name is None:
                # Final response — no tool call
                return response_text, total_tokens

            # Execute tool
            if self._tools is None:
                tool_result = f"Error: no tools available (tool '{tool_name}' not found)"
            else:
                result = self._tools.call_tool(tool_name, tool_args)
                tool_result = result.content[0].text if result.content else str(result)

            logger.info("[%s] Tool call: %s(%s) → %s",
                        self.role_id, tool_name, json.dumps(tool_args, ensure_ascii=False),
                        tool_result[:80])

            # Feed tool result back to LLM
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": f"Tool result ({tool_name}):\n{tool_result}"})

        # Max rounds reached — return last response
        logger.warning("[%s] Max tool-calling rounds reached for task %s", self.role_id, task.task_id)
        return messages[-1]["content"], total_tokens

    @staticmethod
    def _parse_tool_call(response: str) -> tuple[Optional[str], dict[str, Any]]:
        """Extract tool_call from LLM response.

        Supports format:
          ```tool_call
          {"tool": "name", "arguments": {...}}
          ```
        """
        # Match ```tool_call ... ``` block
        match = re.search(r'```tool_call\s*\n(.*?)\n\s*```', response, re.DOTALL)
        if not match:
            return None, {}

        try:
            data = json.loads(match.group(1).strip())
            return data.get("tool"), data.get("arguments", {})
        except json.JSONDecodeError:
            return None, {}


# ── RolePool ───────────────────────────────────────────────

class RolePool:
    """Manages a pool of AgentRoles running concurrently.

    Each role gets its own daemon thread that loops:
      1. Pop the most urgent task from its queue
      2. Execute it via DeepSeek LLM
      3. Fire on_task_done callback
      4. Repeat

    Usage:
        pool = RolePool()
        pool.add_role(AgentRole(name="coder", ...))
        pool.add_role(AgentRole(name="reviewer", ...))
        pool.start()

        pool.assign_task("coder", Task(urgency=Urgency.HIGH, description="Fix login bug"))
        pool.assign_task("reviewer", Task(urgency=Urgency.NORMAL, description="Review PR #188"))

        pool.shutdown()
    """

    def __init__(self, llm_api_key: Optional[str] = None, llm_model: Optional[str] = None):
        self._roles: dict[str, AgentRole] = {}
        self._executor = ThreadPoolExecutor(thread_name_prefix="role-")
        self._futures: dict[str, Future] = {}
        self._shutdown_flag = threading.Event()
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model

    # ── Role management ────────────────────────────────────

    def add_role(self, role: AgentRole) -> None:
        """Register a role. Must be called before start()."""
        if role.role_id in self._roles:
            raise ValueError(f"Role '{role.role_id}' already exists")
        self._roles[role.role_id] = role

    def get_role(self, name: str) -> AgentRole:
        if name not in self._roles:
            raise KeyError(f"Role '{name}' not found. Available: {list(self._roles)}")
        return self._roles[name]

    def list_roles(self) -> list[str]:
        return list(self._roles)

    # ── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Launch all role worker threads."""
        for role_id, role in self._roles.items():
            role._running = True
            role._pool = self  # back-reference for talk tool
            role._llm = DeepSeekLLM(api_key=self._llm_api_key, model=self._llm_model)
            role._register_talk_tool()  # auto-register inter-role communication
            fut = self._executor.submit(self._role_loop, role)
            self._futures[role_id] = fut
            logger.info("Role '%s' worker started", role_id)

    def shutdown(self, wait: bool = True) -> None:
        """Stop all role workers gracefully."""
        logger.info("Shutting down RolePool...")
        self._shutdown_flag.set()
        for role in self._roles.values():
            role._running = False
        self._executor.shutdown(wait=wait)
        logger.info("RolePool shut down")

    # ── Task assignment ────────────────────────────────────

    def assign_task(self, role_name: str, task: Task) -> None:
        """Route a task to a specific role's queue."""
        role = self.get_role(role_name)
        role.add_task(task)

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all roles' status."""
        result = {}
        for name, role in self._roles.items():
            next_u = role.peek_next_urgency()
            result[name] = {
                "busy": role.is_busy,
                "queue_depth": role.queue_depth,
                "current_task": role.current_task.description if role.current_task else None,
                "next_urgency": next_u.name if next_u else None,
            }
        return result

    # ── Internal: role worker loop ─────────────────────────

    def _role_loop(self, role: AgentRole) -> None:
        """Main loop for a single role's worker thread."""
        logger.info("[%s] Worker loop started", role.role_id)

        while role._running and not self._shutdown_flag.is_set():
            task = role.pop_task()
            if task is None:
                time.sleep(0.1)  # idle polling
                continue

            # Execute the task
            role._current_task = task
            task.status = "running"
            logger.info("[%s] Processing task: %s (%s)", role.role_id, task.task_id, task.description[:60])

            if role.on_task_start:
                try:
                    role.on_task_start(role, task)
                except Exception:
                    logger.exception("[%s] on_task_start callback failed", role.role_id)

            try:
                assert role._llm is not None, "LLM not initialized for role"
                if role._tools is not None and role._tools.tool_count > 0:
                    # Tool-calling loop: LLM can invoke MCP tools
                    result_text, tokens = role._execute_with_tools(task)
                else:
                    # Simple chat: no tools available
                    result_text, tokens = role._llm.chat(
                        system=role.build_system_prompt(),
                        user=task.description,
                        max_tokens=512,
                    )
                task.result = result_text
                task.tokens_consumed = tokens
                task.status = "done"
                logger.info("[%s] Task %s done (%d tokens): %s",
                            role.role_id, task.task_id, tokens, result_text[:80])
            except Exception as exc:
                task.result = f"[ERROR] {exc}"
                task.status = "failed"
                logger.error("[%s] Task %s failed: %s", role.role_id, task.task_id, exc)

            role._current_task = None

            if role.on_task_done:
                try:
                    role.on_task_done(role, task)
                except Exception:
                    logger.exception("[%s] on_task_done callback failed", role.role_id)

        logger.info("[%s] Worker loop exited", role.role_id)
