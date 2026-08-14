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


# ── 工具调用循环上限 ───────────────────────────────────────
# 防止 LLM 陷入"反复调工具/解析失败重来"的退化循环: 无限轮次 + 无输出上限
# 会无限烧 Token. 超限时抛 ToolLoopError → 任务标记 failed, 错误文本不当结果.
MAX_TOOL_ROUNDS = 20              # 最多工具调用轮数
MAX_TOOL_TOTAL_TOKENS = 200_000   # 单任务累计 Token 上限 (含 thinking 推理 token)

# LLM 调用失败时的错误文本标记 (llm.py 超时/异常时返回这些前缀)
LLM_ERROR_MARKERS = ("[API timeout]", "[API error:")


class ToolLoopError(RuntimeError):
    """工具调用循环超限或 LLM 调用失败. 任务应标记 failed, 错误文本不作为成功结果."""


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
    computer_kind: str = "podman"                          # 个人电脑类型: podman(默认) | ssh | local
    computer_kwargs: dict[str, Any] = field(default_factory=dict)  # 电脑构造参数 (ssh 的 host 等)

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
    _time_manager: Any = field(default=None, repr=False, init=False)  # TimeEventBus, lazy init
    _computer: Any = field(default=None, repr=False, init=False)  # Computer, lazy init (角色添加时自动创建)

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

        # 系统时间事件 (source=time, 如 shift_start/shift_end) 绕过内容显著性过滤
        if event.source == "time":
            return True, f"系统时间事件: {event.event_type} (tick={event.payload.get('tick')})"

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
        """构建角色完整 System Prompt.

        组合: 人名, 职位, 职责, 性格, 技能, 当前日期(第几天), 额外提示.
        如果存在昨日总结 (NoteStore), 自动注入到提示词中.
        """
        parts = [
            f"你是 {self.name}，职位是 {self.title}，负责 {self.role_id} 工作。",
            f"性格特点：{self.personality}。",
        ]
        if self.skills:
            parts.append(f"技能：{', '.join(self.skills)}。")

        # 当前是第几天 (作息系统, 从共享 TimeEventBus 获取)
        parts.append(f"今天是第 {self.time_manager.day_number()} 天。")

        # 通用工作规则 (所有角色生效): 空闲即休息, 不主动打扰他人
        parts.append(
            "如果当前没有任务，你可以直接调用休息。"
            "并且你需要注意：不要在不应该打扰其他人的时候向其他人发送信息，"
            "只有必要时才会发送。因此当你没有任务的时候，不要询问其它人，"
            "直接休息即可。当有事情的时候会自动提醒你的。"
            "当你完成任务后，你需要向给你安排任务的同事汇报任务完成情况，然后再休息。"
        )

        # 沟通守时规则 (所有角色生效): 约好的沟通必须在指定时间进行
        parts.append(
            "如果你有与其他人沟通的任务，请务必保证在指定时间与其沟通，"
            "不能提前也不能延后，因为其他人会认为你应该在此时与他沟通。"
        )

        if self.system_prompt_extra:
            parts.append(self.system_prompt_extra)

        # 注入昨日总结 (如果有) — 只注入严格早于今天(day)的总结
        summary = self.get_latest_summary(before_day=self.time_manager.day_number())
        if summary:
            parts.append(f"\n[昨日总结]\n{summary}\n(以上是昨天的总结, 供你延续工作.)")

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

    # ── Personal computer (per-role) ───────────────────────

    @property
    def computer(self) -> Any:
        """获取该角色的个人电脑 (惰性创建, 角色添加时自动创建并开机).

        默认使用 Podman 虚拟电脑; 若角色信息 (computer_kind) 指定了
        ssh/local 则按指定类型创建.
        """
        if self._computer is None:
            from src.core.computer import _COMPUTER_MANAGER
            self._computer = _COMPUTER_MANAGER.create(
                kind=self.computer_kind,
                role_id=self.role_id,
                name=self.name,  # 人名, 供内网设备列表展示
                auto_mcp=True,   # 自动创建的电脑: 创建时自动安装独立 MCP 服务器
                **self.computer_kwargs,
            )
            if not self._computer.is_on:
                self._computer.power_on()
        return self._computer

    # ── Note store (per-role file storage) ─────────────────

    @property
    def note_store(self) -> Any:
        """获取该角色的笔记存储实例 (惰性初始化, 按 role_id 隔离).

        每个角色独立目录: data/notes/<role_id>/ (或电脑工作目录下的 notes/).
        """
        if self._note_store is None:
            from src.core.note_store import NoteStore
            self._note_store = NoteStore(role_id=self.role_id, computer=self.computer)
        return self._note_store

    def get_latest_summary(self, before_day: Optional[int] = None) -> Optional[str]:
        """读取该角色最近一次的每日总结 (用于下一天冷启动提示词).

        参数:
            before_day: 截止天数 (只找严格早于该天的总结, 可选).

        返回:
            最近总结内容, 没有则返回 None.
        """
        return self.note_store.get_latest_summary(before_day)

    # ── Time manager (作息时间) ───────────────────────────

    @property
    def time_manager(self) -> Any:
        """获取该角色的作息时间管理器 (惰性初始化).

        以 Tick 为单位: 1 Tick = 10 分钟, 系统启动 = Tick 0 / 第 1 天.
        """
        if self._time_manager is None:
            from src.core.time_manager import TimeEventBus
            self._time_manager = TimeEventBus()
        return self._time_manager

    def bind_time_manager(self, tm: Any) -> None:
        """绑定共享 TimeEventBus (所有角色共用同一个时间源).

        参数:
            tm: TimeEventBus 实例.
        """
        self._time_manager = tm

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

        # 记忆工具类自动绑定该角色的 NoteStore (内容按角色隔离) + 角色引用 (summary 后切 OFF_DUTY)
        if toolkit.name == "memory":
            from src.python_tools.memory_toolkit import bind_store_to_toolkit
            bind_store_to_toolkit(toolkit, self.note_store, role=self)

        # 时间工具类自动绑定该角色的 TimeEventBus (作息时间) + 角色 (休息状态)
        if toolkit.name == "time":
            from src.python_tools.time_toolkit import bind_time_to_toolkit
            bind_time_to_toolkit(toolkit, self.time_manager, role=self)

        # 定时任务工具类自动绑定该角色 (任务注册到共享 TimeEventBus)
        if toolkit.name == "task":
            from src.python_tools.task_toolkit import bind_role_to_toolkit
            bind_role_to_toolkit(toolkit, self)

        # MCP 管理工具类自动绑定该角色 (add/remove 需要知道当前角色是谁)
        if toolkit.name == "mcp_manager":
            from src.python_tools.mcp_manager import bind_mcp_manager_to_toolkit
            bind_mcp_manager_to_toolkit(toolkit, self)

        # 技能管理工具类自动绑定该角色 (skill_add/remove 需要知道当前角色是谁)
        if toolkit.name == "skill_manager":
            from src.python_tools.skill_toolkit import bind_role_to_toolkit
            bind_role_to_toolkit(toolkit, self)

        # HR 工具类自动绑定该角色 (发布招聘后新员工加入团队, 需要 RolePool 引用)
        if toolkit.name == "hr":
            from src.python_tools.hr_toolkit import bind_role_to_toolkit
            bind_role_to_toolkit(toolkit, self)

        # 个人电脑工具类自动绑定该角色 (run_command 等在角色自己的电脑上执行)
        if toolkit.name == "computer":
            from src.python_tools.computer_toolkit import bind_computer_to_toolkit
            bind_computer_to_toolkit(toolkit, self)

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
        """Execute a task with tool-calling loop (原生 function calling).

        1. Send system prompt + OpenAI tools declaration + task to LLM
        2. If LLM responds with message.tool_calls (结构化), execute each and
           feed results back as role:"tool" messages
        3. Loop until LLM gives final response (no tool_calls)
        4. Return (final_text, total_tokens)
        """
        assert self._llm is not None

        system = self.build_system_prompt()
        openai_tools: list[dict] = []
        if self._tools is not None:
            openai_tools = self._tools.to_openai_tools()

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task.description},
        ]

        total_tokens = 0
        round_no = 0  # 工具调用轮次计数

        # 工具调用循环: 有轮次上限与累计 Token 上限, 超限即失败.
        # 无上限时 LLM 一旦陷入反复调工具的退化循环会无限烧 Token.
        while True:
            round_no += 1

            # 轮次上限: 超限终止循环并标记失败 (防止无限循环)
            if round_no > MAX_TOOL_ROUNDS:
                raise ToolLoopError(
                    f"工具调用超过 {MAX_TOOL_ROUNDS} 轮仍未收敛 "
                    f"(累计 {total_tokens} tokens), 任务失败")

            content, raw_calls, usage = self._llm.chat_with_tools(
                messages, openai_tools, 0.7, None,  # max_tokens=None = 无上限 (长内容不截断)
            )
            round_tokens = usage.get("total_tokens", 0) if usage else 0
            total_tokens += round_tokens

            # 累计 Token 上限: 超限同样终止 (LLM 退化循环时会持续烧 token)
            if total_tokens > MAX_TOOL_TOTAL_TOKENS:
                raise ToolLoopError(
                    f"工具调用累计 {total_tokens} tokens 超过上限 "
                    f"{MAX_TOOL_TOTAL_TOKENS}, 任务失败")

            # 原生结构化判定: tool_calls 非空 = 本轮在调工具
            tool_calls = raw_calls or []

            if not tool_calls:
                # LLM 调用失败 (API 超时/异常返回 "[API ...]" 错误文本):
                # 这不是正常终答, 不能当作成功结果 — 标记失败并抛出
                if content and content.startswith(LLM_ERROR_MARKERS):
                    raise ToolLoopError(
                        f"LLM 调用失败 (第 {round_no} 轮): {content[:120]}")
                # Final response — no tool call
                logger.debug("[%s] 工具循环: 第 %d 轮收到终答 (无工具调用), 任务完成",
                             self.role_id, round_no)
                return content, total_tokens

            # 把本轮 LLM 回复 (含原生 tool_calls) 追加进对话历史
            assistant_msg: dict = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            logger.debug("[%s] 工具循环: 追加 LLM 输出消息 (%d 字符, %d 个原生工具调用)",
                         self.role_id, len(content), len(tool_calls))

            # 顺序执行本轮的每个工具调用
            for call in tool_calls:
                fn = call.get("function", {})
                tool_name = fn.get("name", "")
                call_id = call.get("id", "")
                try:
                    tool_args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                    logger.warning("[%s] 工具 %s 的 arguments 不是合法 JSON: %s",
                                   self.role_id, tool_name, fn.get("arguments"))

                if self._tools is None:
                    tool_result = f"Error: no tools available (tool '{tool_name}' not found)"
                else:
                    result = self._tools.call_tool(tool_name, tool_args)
                    tool_result = result.content[0].text if result.content else str(result)

                logger.info("[%s] Tool call: %s(%s) → %s",
                            self.role_id, tool_name,
                            json.dumps(tool_args, ensure_ascii=False),
                            tool_result[:80])

                # 原生协议: 工具结果以 role:"tool" 消息回喂, 关联 tool_call_id
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_result,
                })

            logger.debug("[%s] 工具循环: 第 %d 轮仍包含工具调用, 继续下一轮 "
                         "(上限 %d 轮 / %d tokens)",
                         self.role_id, round_no, MAX_TOOL_ROUNDS, MAX_TOOL_TOTAL_TOKENS)


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

    def __init__(self, llm_api_key: Optional[str] = None, llm_model: Optional[str] = None,
                 time_manager: Any = None):
        self._roles: dict[str, AgentRole] = {}
        self._executor = ThreadPoolExecutor(thread_name_prefix="role-")
        self._futures: dict[str, Future] = {}
        self._shutdown_flag = threading.Event()
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        # 共享时间源 (AgentSystem 注入). 招聘入职 (add_role_and_start) 时
        # 绑定给新角色, 否则新员工拿到的是私有未启动的 TimeEventBus (永远第 1 天).
        self._time_manager = time_manager

    # ── Role management ────────────────────────────────────

    def add_role(self, role: AgentRole) -> None:
        """Register a role. Must be called before start()."""
        if role.role_id in self._roles:
            raise ValueError(f"Role '{role.role_id}' already exists")
        self._roles[role.role_id] = role

    def add_role_and_start(self, role: AgentRole) -> AgentRole:
        """动态入职: 注册新角色并立即启动其 worker 线程 (招聘流程用).

        与 add_role + start() 对单个角色的处理等价:
          1. 注册进 _roles (已存在则报错)
          2. 装配默认工具 (memory/time/task) + talk 通信工具
          3. 创建 LLM 实例 (带角色前缀)
          4. 提交 worker 线程

        参数:
            role: 新入职的 AgentRole.

        返回:
            已启动的 role (供调用方链式使用).
        """
        if role.role_id in self._roles:
            raise ValueError(f"Role '{role.role_id}' already exists")
        self._roles[role.role_id] = role

        # 绑定共享时间源 (High-1 修复): 新入职角色必须与团队共用同一个
        # TimeEventBus, 否则 add_toolkit(time) 会让 time 工具持有角色私有的
        # 未启动实例 — get_time 永远第 1 天 Tick 0, 定时任务永不触发.
        # 必须在装配工具之前绑定.
        if self._time_manager is not None:
            role.bind_time_manager(self._time_manager)

        # 装配默认工具 (与 AgentSystem 的 auto_toolkits 一致)
        from src.python_tools import DEFAULT_TOOLKITS
        for factory in DEFAULT_TOOLKITS.values():
            role.add_toolkit(factory())

        # 装配默认 MCP 工具组 (新入职员工同样自动获得, 如 file_ops)
        from src.python_tools import DEFAULT_MCP_GROUPS, _MCP_MANAGER
        for group in DEFAULT_MCP_GROUPS:
            _MCP_MANAGER.install_group_defaults(role, group)

        # 与 start() 相同的单角色启动逻辑
        role._running = True
        role._pool = self  # back-reference for talk tool
        role._llm = DeepSeekLLM(api_key=self._llm_api_key, model=self._llm_model,
                                label=role.role_id)  # DEBUG 日志带角色前缀
        role._register_talk_tool()  # auto-register inter-role communication
        fut = self._executor.submit(self._role_loop, role)
        self._futures[role.role_id] = fut
        logger.info("Role '%s' (新入职) worker started", role.role_id)
        return role

    def get_role(self, name: str) -> AgentRole:
        if name not in self._roles:
            raise KeyError(f"Role '{name}' not found. Available: {list(self._roles)}")
        return self._roles[name]

    def remove_role(self, role_id: str) -> bool:
        """离职: 移除角色并关闭其个人电脑.

        1. 停止角色 worker (置 _running=False, 移除 future)
        2. 关闭角色个人电脑 (若已创建且开机)
        3. 从 _roles 移除

        参数:
            role_id: 要移除的角色 ID.

        返回:
            是否移除成功 (角色不存在返回 False).
        """
        if role_id not in self._roles:
            return False
        role = self._roles.pop(role_id)

        # 停止 worker
        role._running = False
        self._futures.pop(role_id, None)

        # 离职: 销毁个人电脑 (关机 + 删除容器 + 从管理器注销)
        try:
            from src.core.computer import _COMPUTER_MANAGER
            _COMPUTER_MANAGER.destroy(role_id)
        except Exception:
            logger.warning("[%s] 离职销毁电脑失败", role_id, exc_info=True)

        logger.info("Role '%s' removed (离职)", role_id)
        return True

    def all_roles(self) -> list[AgentRole]:
        """返回所有角色列表 (按注册顺序)."""
        return list(self._roles.values())

    def list_roles(self) -> list[str]:
        return list(self._roles)

    # ── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Launch all role worker threads."""
        for role_id, role in self._roles.items():
            # 共享时间源存在时同样绑定 (独立使用 RolePool + start 的场景)
            if self._time_manager is not None:
                role.bind_time_manager(self._time_manager)
            role._running = True
            role._pool = self  # back-reference for talk tool
            role._llm = DeepSeekLLM(api_key=self._llm_api_key, model=self._llm_model,
                                    label=role.role_id)  # DEBUG 日志带角色前缀
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
                    # LLM 调用失败 (API 超时/异常): 错误文本不是正常答复, 标记失败
                    if result_text.startswith(LLM_ERROR_MARKERS):
                        raise ToolLoopError(
                            f"LLM 调用失败: {result_text[:120]}")
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
