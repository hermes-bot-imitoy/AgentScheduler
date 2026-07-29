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
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

from src.core.llm import DeepSeekLLM

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

    name: str                                              # e.g. "coder", "reviewer"
    title: str = ""                                        # e.g. "Senior Backend Engineer"
    personality: str = ""                                  # e.g. "严谨细致，追求代码质量"
    skills: list[str] = field(default_factory=list)        # e.g. ["Python", "Go", "K8s"]
    system_prompt_extra: str = ""                          # appended to base system prompt

    # Internal state (managed by RolePool)
    _queue: list[Task] = field(default_factory=list, repr=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)
    _current_task: Optional[Task] = field(default=None, repr=False, init=False)
    _running: bool = field(default=True, repr=False, init=False)
    _llm: Optional[DeepSeekLLM] = field(default=None, repr=False, init=False)

    # Callbacks
    on_task_start: Optional[Callable[[AgentRole, Task], None]] = field(default=None, repr=False, init=False)
    on_task_done: Optional[Callable[[AgentRole, Task], None]] = field(default=None, repr=False, init=False)

    def build_system_prompt(self) -> str:
        """Construct the role's full system prompt from persona fields."""
        parts = [
            f"你是 {self.name}，职位是 {self.title}。",
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
        task.assigned_role = self.name
        with self._lock:
            heapq.heappush(self._queue, task)
            logger.info(
                "[%s] Task queued: %s (urgency=%s, queue_depth=%d)",
                self.name, task.task_id, Urgency(-task.urgency).name, len(self._queue),
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
        if role.name in self._roles:
            raise ValueError(f"Role '{role.name}' already exists")
        self._roles[role.name] = role

    def get_role(self, name: str) -> AgentRole:
        if name not in self._roles:
            raise KeyError(f"Role '{name}' not found. Available: {list(self._roles)}")
        return self._roles[name]

    def list_roles(self) -> list[str]:
        return list(self._roles)

    # ── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Launch all role worker threads."""
        for name, role in self._roles.items():
            role._running = True
            role._llm = DeepSeekLLM(api_key=self._llm_api_key, model=self._llm_model)
            fut = self._executor.submit(self._role_loop, role)
            self._futures[name] = fut
            logger.info("Role '%s' worker started", name)

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
        logger.info("[%s] Worker loop started", role.name)

        while role._running and not self._shutdown_flag.is_set():
            task = role.pop_task()
            if task is None:
                time.sleep(0.1)  # idle polling
                continue

            # Execute the task
            role._current_task = task
            task.status = "running"
            logger.info("[%s] Processing task: %s (%s)", role.name, task.task_id, task.description[:60])

            if role.on_task_start:
                try:
                    role.on_task_start(role, task)
                except Exception:
                    logger.exception("[%s] on_task_start callback failed", role.name)

            try:
                assert role._llm is not None, "LLM not initialized for role"
                result_text, tokens = role._llm.chat(
                    system=role.build_system_prompt(),
                    user=task.description,
                    max_tokens=512,
                )
                task.result = result_text
                task.tokens_consumed = tokens
                task.status = "done"
                logger.info("[%s] Task %s done (%d tokens): %s",
                            role.name, task.task_id, tokens, result_text[:80])
            except Exception as exc:
                task.result = f"[ERROR] {exc}"
                task.status = "failed"
                logger.error("[%s] Task %s failed: %s", role.name, task.task_id, exc)

            role._current_task = None

            if role.on_task_done:
                try:
                    role.on_task_done(role, task)
                except Exception:
                    logger.exception("[%s] on_task_done callback failed", role.name)

        logger.info("[%s] Worker loop exited", role.name)
