"""时间管理器 (TimeManager) — 以 Tick 为单位的作息时间.

规则:
  - 1 Tick = 10 分钟
  - Tick 0  = 上班 (默认 09:00)
  - Tick 60 = 下班 (默认 19:00)
  - 上班前为负 Tick, 下班后超过 60 Tick

事件触发:
  - Tick 到达 0   → 发送 shift_start 事件 (优先级 NORMAL)
  - Tick 到达 60  → 发送 shift_end   事件 (优先级 NORMAL)
  - TimeManager 独占一个后台线程, 周期性检查 Tick 并触发事件

用法:
    tm = TimeManager()
    tm.set_event_sender(bus.process_event)   # 设置事件发送回调
    tm.start()                                # 启动时间线程
    tick = tm.current_tick()                  # 当前 Tick
    tm.stop()                                 # 停止线程
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Callable, Optional

from src.core.types import Event, Priority

logger = logging.getLogger(__name__)

# ── 作息关键事件 ──────────────────────────────────────────

TICK_SHIFT_START = 0    # 上班事件: Tick 0
TICK_SHIFT_END = 60     # 下班事件: Tick 60

MINUTES_PER_TICK = 10   # 每 Tick 10 分钟

DEFAULT_CHECK_INTERVAL = 30  # 线程检查间隔 (秒)


@dataclass
class TimeManager:
    """以 Tick 为单位的作息时间管理器.

    参数:
        day_start: 上班时间 "HH:MM" (默认 09:00, 对应 Tick 0)
        day_end:   下班时间 "HH:MM" (默认 19:00, 对应 Tick 60)
        check_interval: 时间线程检查间隔 (秒, 默认 30)
    """

    day_start: str = "09:00"
    day_end: str = "19:00"
    check_interval: int = DEFAULT_CHECK_INTERVAL

    # 内部状态 (不参与 dataclass 比较)
    _start: Optional[time] = field(default=None, repr=False, init=False)
    _end: Optional[time] = field(default=None, repr=False, init=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False, init=False)
    _running: bool = field(default=False, repr=False, init=False)
    _event_sender: Optional[Callable[[Event], None]] = field(default=None, repr=False, init=False)
    _clock: Callable[[], datetime] = field(default=datetime.now, repr=False, init=False)
    _fired_start: bool = field(default=False, repr=False, init=False)
    _fired_end: bool = field(default=False, repr=False, init=False)

    def __post_init__(self):
        self._start = self._parse(self.day_start)
        self._end = self._parse(self.day_end)
        # 工作日总 Tick 数 = (下班-上班)分钟 / 10
        self.total_ticks = (self._minutes(self._end) - self._minutes(self._start)) // MINUTES_PER_TICK
        if self.total_ticks != TICK_SHIFT_END:
            logger.info("TimeManager: 自定义作息 %s-%s → %d Ticks (默认 60)", self.day_start, self.day_end, self.total_ticks)

    # ── 静态工具 ──────────────────────────────────────────

    @staticmethod
    def _parse(hhmm: str) -> time:
        h, m = hhmm.split(":")
        return time(int(h), int(m))

    @staticmethod
    def _minutes(t: time) -> int:
        return t.hour * 60 + t.minute

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

    def current_tick(self, now: datetime | None = None) -> int:
        """获取当前 Tick.

        参数:
            now: 指定时间 (默认使用注入的时钟).

        返回:
            当前 Tick 数. 上班前为负, 下班后超过 total_ticks.
        """
        now = now or self._clock()
        assert self._start is not None, "TimeManager 未初始化"
        minutes_since_start = (now.hour * 60 + now.minute) - self._minutes(self._start)
        return minutes_since_start // MINUTES_PER_TICK

    def tick_to_time(self, tick: int) -> str:
        """将 Tick 转换为 "HH:MM" 时间字符串.

        参数:
            tick: Tick 数 (可为负或超过总 Tick 数).

        返回:
            对应的时间字符串.
        """
        assert self._start is not None, "TimeManager 未初始化"
        total_minutes = self._minutes(self._start) + tick * MINUTES_PER_TICK
        total_minutes %= 24 * 60  # 支持跨天
        h, m = divmod(total_minutes, 60)
        return f"{h:02d}:{m:02d}"

    def is_working_hours(self, now: datetime | None = None) -> bool:
        """判断当前是否在上班时间内 (0 <= Tick <= total_ticks).

        参数:
            now: 指定时间 (默认使用注入的时钟).

        返回:
            True 表示在上班时间内.
        """
        tick = self.current_tick(now)
        return TICK_SHIFT_START <= tick <= self.total_ticks

    # ── 作息事件 ─────────────────────────────────────────

    def get_shift_event(self, tick: int) -> str | None:
        """获取某个 Tick 对应的作息事件.

        参数:
            tick: Tick 数.

        返回:
            "SHIFT_START" (上班) / "SHIFT_END" (下班) / None (普通时间).
        """
        if tick >= TICK_SHIFT_START and tick < self.total_ticks:
            return "SHIFT_START" if tick == TICK_SHIFT_START else None
        if tick >= self.total_ticks:
            return "SHIFT_END"
        return None

    def describe(self, now: datetime | None = None) -> str:
        """返回当前作息状态的文字描述 (供工具/提示词使用).

        参数:
            now: 指定时间 (默认使用注入的时钟).

        返回:
            作息状态描述字符串.
        """
        now = now or self._clock()
        tick = self.current_tick(now)
        clock = now.strftime("%H:%M")
        if tick < TICK_SHIFT_START:
            return f"当前时间 {clock}, Tick {tick} (上班前, 距上班还有 {-tick} Ticks)"
        if tick >= self.total_ticks:
            return f"当前时间 {clock}, Tick {tick} (已下班 {tick - self.total_ticks} Ticks)"
        return f"当前时间 {clock}, Tick {tick} (上班中, 距下班还有 {self.total_ticks - tick} Ticks)"

    # ── 时间线程 (独占) ───────────────────────────────────

    def start(self) -> None:
        """启动时间线程 (独占线程, 周期性检查 Tick 并触发作息事件).

        触发规则:
          - 首次检测到 Tick >= 0  → 发送 shift_start 事件
          - 首次检测到 Tick >= 60 → 发送 shift_end 事件
        """
        if self._running:
            return
        self._running = True
        self._fired_start = False
        self._fired_end = False
        self._thread = threading.Thread(
            target=self._tick_loop, name="time-manager", daemon=True,
        )
        self._thread.start()
        logger.info("TimeManager 时间线程已启动 (检查间隔 %ds)", self.check_interval)

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

    # ── 内部: 时间线程主循环 ──────────────────────────────

    def _tick_loop(self) -> None:
        """时间线程主循环: 周期性检查 Tick, 触发作息事件."""
        logger.debug("TimeManager 线程循环开始")
        while self._running:
            try:
                self._check_and_fire()
            except Exception:
                logger.exception("TimeManager 检查异常")
            time_module.sleep(self.check_interval)
        logger.debug("TimeManager 线程循环结束")

    def _check_and_fire(self) -> None:
        """检查当前 Tick 并触发对应事件 (线程安全)."""
        tick = self.current_tick()

        # Tick 0: 上班事件 (进入工作时段后触发一次)
        if not self._fired_start and tick >= TICK_SHIFT_START and tick < self.total_ticks:
            self._fired_start = True
            self._fire_event("shift_start", tick)

        # Tick 60: 下班事件 (到达或超过下班点后触发一次)
        if not self._fired_end and tick >= self.total_ticks:
            self._fired_end = True
            self._fire_event("shift_end", tick)

    def _fire_event(self, event_type: str, tick: int) -> None:
        """构造并发送作息事件到事件总线.

        参数:
            event_type: "shift_start" 或 "shift_end"
            tick: 触发时的 Tick 数
        """
        event = Event(
            source="time",
            event_type=event_type,
            priority=Priority.NORMAL,
            payload={
                "tick": tick,
                "time": self.tick_to_time(tick),
                "shift": event_type,
            },
        )
        logger.info("TimeManager 触发事件: %s (tick=%d, time=%s, priority=%s)",
                    event_type, tick, event.payload["time"], event.priority.name)

        if self._event_sender is not None:
            try:
                self._event_sender(event)
            except Exception:
                logger.exception("TimeManager 事件发送失败: %s", event_type)
