"""时间管理器 (TimeManager) — 以 Tick 为单位的作息时间.

规则:
  - 系统开始运行即为 Tick 0, 不依赖墙钟时间
  - 1 Tick = 10 分钟 (可配置)
  - 每天 = ticks_per_day 个 Tick (默认 144 = 24 小时), 系统启动当天为第 1 天
  - 每个工作日的 Tick 0 上班 (shift_start), Tick 60 下班 (shift_end)

事件触发 (独占后台线程):
  - 每天第 0 Tick   → 发送 SHIFT_START 事件 (优先级 EMERGENCY)
  - 每天第 60 Tick  → 发送 SHIFT_END   事件 (优先级 EMERGENCY)
  - SHIFT_END 的 instruction 提示角色调用 summary 工具总结并下班

用法:
    tm = TimeManager()
    tm.set_event_sender(bus.process_event)   # 设置事件发送回调
    tm.start()                                # 启动时间线程 (记录启动时刻 = Tick 0)
    tick = tm.current_tick()                  # 当前 Tick (自启动累计)
    day = tm.day_number()                     # 当前第几天 (从 1 开始)
    tm.stop()                                 # 停止线程
"""

from __future__ import annotations

import logging
import threading
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from src.core.types import Event, Priority

logger = logging.getLogger(__name__)

# ── 作息关键事件 (大写统一) ─────────────────────────────────

EVENT_SHIFT_START = "SHIFT_START"   # 上班事件
EVENT_SHIFT_END = "SHIFT_END"       # 下班事件
EVENT_TASK_DUE = "TASK_DUE"         # 定时任务提醒事件

MINUTES_PER_TICK = 10       # 每 Tick 10 分钟
TICKS_PER_DAY = 144         # 每天 144 Tick (24 小时)
SHIFT_START_TICK = 0        # 上班: 每天第 0 Tick
SHIFT_END_TICK = 60         # 下班: 每天第 60 Tick (10 小时工作制)
TASK_TICK_MIN = 0           # 任务 Tick 范围下限
TASK_TICK_MAX = 60          # 任务 Tick 范围上限 (工作时间 0~60)

DEFAULT_CHECK_INTERVAL = 30  # 线程检查间隔 (秒)


@dataclass
class ScheduledTask:
    """定时任务 (注册到 TimeManager, 到达指定 Tick 触发提醒事件).

    参数:
        task_id:      任务 ID (自动生成)
        owner_role:   所属角色 role_id (事件只投递给该角色)
        description:  任务内容描述
        target_tick:  目标 Tick (今日 0~60 范围内)
        day:          计划在哪一天触发 (默认创建时的当天)
        payload:      附加数据
        fired:        是否已触发 (触发后自动移除)
    """
    description: str
    owner_role: str
    target_tick: int
    day: int = 1
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time_module.time)
    fired: bool = False

    def absolute_fire_tick(self, ticks_per_day: int) -> int:
        """计算绝对触发 Tick: (day-1)*ticks_per_day + target_tick."""
        return (self.day - 1) * ticks_per_day + self.target_tick


@dataclass
class TimeManager:
    """以 Tick 为单位的作息时间管理器.

    系统启动时刻记为 Tick 0 / 第 1 天, 之后按 elapsed 时间推进 Tick,
    不依赖任何硬编码的墙钟时间.

    参数:
        minutes_per_tick: 每个 Tick 的分钟数 (默认 10)
        shift_start_tick: 上班 Tick (默认 0)
        shift_end_tick:   下班 Tick (默认 60)
        ticks_per_day:    每天总 Tick 数 (默认 144)
        check_interval:   时间线程检查间隔秒数 (默认 30)
    """

    minutes_per_tick: int = MINUTES_PER_TICK
    shift_start_tick: int = SHIFT_START_TICK
    shift_end_tick: int = SHIFT_END_TICK
    ticks_per_day: int = TICKS_PER_DAY
    check_interval: int = DEFAULT_CHECK_INTERVAL

    # 内部状态
    _start_dt: Optional[datetime] = field(default=None, repr=False, init=False)  # 启动时刻
    _thread: Optional[threading.Thread] = field(default=None, repr=False, init=False)
    _running: bool = field(default=False, repr=False, init=False)
    _event_sender: Optional[Callable[[Event], None]] = field(default=None, repr=False, init=False)
    _clock: Callable[[], datetime] = field(default=datetime.now, repr=False, init=False)
    _fired_day: int = field(default=0, repr=False, init=False)      # 已触发事件的天
    _fired_start: bool = field(default=False, repr=False, init=False)
    _fired_end: bool = field(default=False, repr=False, init=False)
    _tasks: dict[str, ScheduledTask] = field(default_factory=dict, repr=False, init=False)  # 定时任务表

    # ── 配置 ──────────────────────────────────────────────

    def set_event_sender(self, fn: Callable[[Event], None]) -> None:
        """设置事件发送回调 (发送到事件总线).

        参数:
            fn: 接收 Event 对象的回调函数, 如 EventBus.process_event 或 EventDispatcher.trigger.
        """
        self._event_sender = fn

    def set_clock(self, fn: Callable[[], datetime]) -> None:
        """设置时间源 (默认 datetime.now). 用于模拟测试.

        参数:
            fn: 返回当前时间的无参函数.
        """
        self._clock = fn

    # ── 核心方法 ──────────────────────────────────────────

    def _elapsed_seconds(self) -> float:
        """自系统启动以来的秒数 (基于注入时钟)."""
        if self._start_dt is None:
            return 0.0
        return max(0.0, (self._clock() - self._start_dt).total_seconds())

    def current_tick(self) -> int:
        """获取当前 Tick 数 (自系统启动累计, 启动即为 0).

        返回:
            当前 Tick 数.
        """
        return int(self._elapsed_seconds() // (self.minutes_per_tick * 60))

    def day_number(self) -> int:
        """获取当前是第几天 (系统启动当天为第 1 天).

        返回:
            天序号 (>= 1).
        """
        return int(self._elapsed_seconds() // (self.ticks_per_day * self.minutes_per_tick * 60)) + 1

    def tick_of_day(self) -> int:
        """获取今天内的 Tick 位置 (0 ~ ticks_per_day-1).

        返回:
            今日内 Tick 数.
        """
        return self.current_tick() % self.ticks_per_day

    def tick_to_time(self, tick: int) -> str:
        """将 Tick 转换为相对时钟 "HH:MM" (从每天第 0 Tick 起算).

        参数:
            tick: Tick 数.

        返回:
            相对时钟字符串, 如 tick 30 → "05:00", tick 60 → "10:00".
        """
        total_minutes = tick * self.minutes_per_tick
        total_minutes %= 24 * 60
        h, m = divmod(total_minutes, 60)
        return f"{h:02d}:{m:02d}"

    def is_working_hours(self) -> bool:
        """判断当前是否在上班时间内 (今日 Tick 在 [shift_start, shift_end) 之间).

        返回:
            True 表示在上班时间内.
        """
        tod = self.tick_of_day()
        return self.shift_start_tick <= tod < self.shift_end_tick

    def ticks_until_shift_end(self) -> int:
        """距下班还有多少 Tick (已下班返回 0)."""
        return max(0, self.shift_end_tick - self.tick_of_day())

    # ── 作息事件 ─────────────────────────────────────────

    def get_shift_event(self, tick_of_day: int) -> Optional[str]:
        """获取今日某个 Tick 位置对应的作息事件.

        参数:
            tick_of_day: 今日内 Tick 位置.

        返回:
            "SHIFT_START" (上班) / "SHIFT_END" (下班) / None (普通时间).
        """
        if tick_of_day == self.shift_start_tick:
            return EVENT_SHIFT_START
        if tick_of_day >= self.shift_end_tick:
            return EVENT_SHIFT_END
        return None

    def describe(self) -> str:
        """返回当前作息状态的文字描述 (供工具/提示词使用).

        返回:
            状态描述字符串: 第几天, Tick 数, 上班/下班状态.
        """
        tick = self.current_tick()
        day = self.day_number()
        tod = self.tick_of_day()
        if tod >= self.shift_end_tick:
            return f"第 {day} 天, Tick {tick} (已下班 {tod - self.shift_end_tick} Ticks)"
        return f"第 {day} 天, Tick {tick} (上班中, 距下班还有 {self.shift_end_tick - tod} Ticks)"

    # ── 时间线程 (独占) ───────────────────────────────────

    def start(self) -> None:
        """启动时间线程 (独占线程, 周期性检查 Tick 并触发作息事件).

        系统启动时刻记为 Tick 0 / 第 1 天:
          - 每天首次检测到今日 Tick == shift_start_tick → 发送 SHIFT_START
          - 每天首次检测到今日 Tick >= shift_end_tick   → 发送 SHIFT_END
        """
        if self._running:
            return
        self._start_dt = self._clock()
        self._running = True
        self._fired_day = 0
        self._fired_start = False
        self._fired_end = False
        self._thread = threading.Thread(
            target=self._tick_loop, name="time-manager", daemon=True,
        )
        self._thread.start()
        logger.info("TimeManager 时间线程已启动 (启动时刻 = Tick 0 / 第 1 天, 检查间隔 %ds)",
                    self.check_interval)

    def stop(self) -> None:
        """停止时间线程."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        logger.info("TimeManager 时间线程已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ── 定时任务管理 ───────────────────────────────────────

    def schedule_task(
        self,
        description: str,
        owner_role: str,
        target_tick: int,
        day: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> ScheduledTask:
        """注册一个定时任务, 到达指定 Tick 时向事件总线发送提醒事件.

        参数:
            description: 任务内容描述
            owner_role:  所属角色 role_id (提醒事件只投递给该角色)
            target_tick: 目标 Tick, 范围 0~60 (今天内)
            day:         触发日期 (默认当天)
            payload:     附加数据 (可选)

        返回:
            创建的 ScheduledTask.

        异常:
            ValueError: target_tick 超出 [0, 60] 范围.
        """
        if not (TASK_TICK_MIN <= target_tick <= TASK_TICK_MAX):
            raise ValueError(
                f"target_tick 必须在 {TASK_TICK_MIN}~{TASK_TICK_MAX} 范围内, 得到 {target_tick}"
            )
        task = ScheduledTask(
            description=description,
            owner_role=owner_role,
            target_tick=target_tick,
            day=day if day is not None else self.day_number(),
            payload=payload or {},
        )
        self._tasks[task.task_id] = task
        logger.info("TimeManager: 定时任务已注册 [%s] %s → tick %d (day %d), 所有者 %s",
                    task.task_id, description, target_tick, task.day, owner_role)
        return task

    def list_tasks(self, owner_role: Optional[str] = None) -> list[ScheduledTask]:
        """列出未触发的定时任务.

        参数:
            owner_role: 只列某角色的任务 (None = 全部).

        返回:
            未触发任务列表 (按触发顺序排序).
        """
        tasks = [t for t in self._tasks.values() if not t.fired]
        if owner_role is not None:
            tasks = [t for t in tasks if t.owner_role == owner_role]
        return sorted(tasks, key=lambda t: t.absolute_fire_tick(self.ticks_per_day))

    def edit_task(
        self,
        task_id: str,
        description: Optional[str] = None,
        target_tick: Optional[int] = None,
        day: Optional[int] = None,
    ) -> Optional[ScheduledTask]:
        """编辑已有定时任务.

        参数:
            task_id:      任务 ID
            description:  新描述 (可选)
            target_tick:  新目标 Tick 0~60 (可选)
            day:          新触发日 (可选)

        返回:
            更新后的任务, 不存在返回 None.

        异常:
            ValueError: target_tick 超出范围.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if description is not None:
            task.description = description
        if target_tick is not None:
            if not (TASK_TICK_MIN <= target_tick <= TASK_TICK_MAX):
                raise ValueError(
                    f"target_tick 必须在 {TASK_TICK_MIN}~{TASK_TICK_MAX} 范围内, 得到 {target_tick}"
                )
            task.target_tick = target_tick
        if day is not None:
            task.day = day
        logger.info("TimeManager: 定时任务已编辑 [%s] → tick %d (day %d)", task_id, task.target_tick, task.day)
        return task

    def cancel_task(self, task_id: str) -> bool:
        """删除定时任务.

        参数:
            task_id: 任务 ID.

        返回:
            是否删除成功.
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            logger.info("TimeManager: 定时任务已删除 [%s]", task_id)
            return True
        return False

    def _check_due_tasks(self) -> None:
        """检查到期任务并发送提醒事件 (由时间线程周期性调用)."""
        now_tick = self.current_tick()
        for task_id, task in list(self._tasks.items()):
            if task.fired:
                continue
            if now_tick >= task.absolute_fire_tick(self.ticks_per_day):
                task.fired = True
                del self._tasks[task_id]  # 一次性提醒, 触发后移除
                self._fire_task_event(task)

    def _fire_task_event(self, task: ScheduledTask) -> None:
        """发送定时任务提醒事件 (定向投递给任务所有者)."""
        event = Event(
            source="task",
            event_type=EVENT_TASK_DUE,
            priority=Priority.NORMAL,
            target_role=task.owner_role,
            payload={
                "task_id": task.task_id,
                "description": task.description,
                "tick": task.target_tick,
                "day": task.day,
                "owner_role": task.owner_role,
            },
        )
        logger.info("TimeManager 任务提醒: [%s] %s → %s (tick %d, day %d)",
                    task.task_id, task.description, task.owner_role, task.target_tick, task.day)

        if self._event_sender is not None:
            try:
                self._event_sender(event)
            except Exception:
                logger.exception("TimeManager 任务事件发送失败: %s", task.task_id)

    def _tick_loop(self) -> None:
        """时间线程主循环: 周期性检查 Tick, 触发作息事件与到期任务."""
        logger.debug("TimeManager 线程循环开始")
        while self._running:
            try:
                self._check_and_fire()
                self._check_due_tasks()
            except Exception:
                logger.exception("TimeManager 检查异常")
            time_module.sleep(self.check_interval)
        logger.debug("TimeManager 线程循环结束")

    def _check_and_fire(self) -> None:
        """检查当前 Tick 并触发对应事件 (按天重置, 每天各触发一次)."""
        day = self.day_number()
        tod = self.tick_of_day()

        # 新的一天 → 重置当天的事件标志
        if day != self._fired_day:
            self._fired_day = day
            self._fired_start = False
            self._fired_end = False
            logger.info("TimeManager: 进入第 %d 天", day)

        # 上班事件 (每天第 0 Tick 触发一次)
        if not self._fired_start and tod == self.shift_start_tick:
            self._fired_start = True
            self._fire_event(EVENT_SHIFT_START)

        # 下班事件 (每天达到 shift_end_tick 后触发一次)
        if not self._fired_end and tod >= self.shift_end_tick:
            self._fired_end = True
            self._fire_event(EVENT_SHIFT_END)

    def _fire_event(self, event_type: str) -> None:
        """构造并发送作息事件到事件总线.

        参数:
            event_type: "SHIFT_START" 或 "SHIFT_END"
        """
        tick = self.current_tick()
        day = self.day_number()
        tod = self.tick_of_day()

        # 下班事件附带指示: 角色应调用 summary 工具总结并进入 OFF_DUTY
        if event_type == EVENT_SHIFT_END:
            instruction = (
                "下班时间到: 请调用 summary 工具总结今天的工作, "
                "总结完成后你将自动进入 OFF_DUTY 状态."
            )
        else:
            instruction = "上班时间到: 查看昨日总结, 开始今天的工作."

        event = Event(
            source="time",
            event_type=event_type,
            priority=Priority.EMERGENCY,  # 作息事件必须能穿透所有过滤
            payload={
                "tick": tick,
                "day": day,
                "time": self.tick_to_time(tod),
                "shift": event_type,
                "instruction": instruction,
            },
        )
        logger.info("TimeManager 触发事件: %s (day=%d, tick=%d, time=%s, priority=%s)",
                    event_type, day, tick, event.payload["time"], event.priority.name)

        if self._event_sender is not None:
            try:
                self._event_sender(event)
            except Exception:
                logger.exception("TimeManager 事件发送失败: %s", event_type)
