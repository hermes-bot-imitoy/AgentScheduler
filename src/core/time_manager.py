"""时间管理器 (TimeManager) — 以 Tick 为单位的作息时间.

规则:
  - 1 Tick = 10 分钟
  - Tick 0  = 上班 (默认 09:00)
  - Tick 60 = 下班 (默认 19:00)
  - 上班前为负 Tick, 下班后超过 60 Tick (保留备用事件)

用法:
    tm = TimeManager()              # 默认 09:00-19:00
    tick = tm.current_tick()        # 当前 Tick
    tm.tick_to_time(30)             # "14:00"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

logger = logging.getLogger(__name__)

# ── 作息关键事件 (备用) ────────────────────────────────────

TICK_SHIFT_START = 0    # 上班事件: Tick 0
TICK_SHIFT_END = 60     # 下班事件: Tick 60

MINUTES_PER_TICK = 10   # 每 Tick 10 分钟


@dataclass
class TimeManager:
    """以 Tick 为单位的作息时间管理器.

    参数:
        day_start: 上班时间 "HH:MM" (默认 09:00, 对应 Tick 0)
        day_end:   下班时间 "HH:MM" (默认 19:00, 对应 Tick 60)
    """

    day_start: str = "09:00"
    day_end: str = "19:00"

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

    # ── 核心方法 ──────────────────────────────────────────

    def current_tick(self, now: datetime | None = None) -> int:
        """获取当前 Tick.

        参数:
            now: 指定时间 (默认当前系统时间). 用于模拟测试.

        返回:
            当前 Tick 数. 上班前为负, 下班后超过 total_ticks.
        """
        now = now or datetime.now()
        minutes_since_start = (now.hour * 60 + now.minute) - self._minutes(self._start)
        return minutes_since_start // MINUTES_PER_TICK

    def tick_to_time(self, tick: int) -> str:
        """将 Tick 转换为 "HH:MM" 时间字符串.

        参数:
            tick: Tick 数 (可为负或超过总 Tick 数).

        返回:
            对应的时间字符串.
        """
        total_minutes = self._minutes(self._start) + tick * MINUTES_PER_TICK
        total_minutes %= 24 * 60  # 支持跨天
        h, m = divmod(total_minutes, 60)
        return f"{h:02d}:{m:02d}"

    def is_working_hours(self, now: datetime | None = None) -> bool:
        """判断当前是否在上班时间内 (0 <= Tick <= total_ticks).

        参数:
            now: 指定时间 (默认当前时间).

        返回:
            True 表示在上班时间内.
        """
        tick = self.current_tick(now)
        return TICK_SHIFT_START <= tick <= self.total_ticks

    # ── 作息事件 (备用) ───────────────────────────────────

    def get_shift_event(self, tick: int) -> str | None:
        """获取某个 Tick 对应的作息事件 (预留接口).

        参数:
            tick: Tick 数.

        返回:
            "SHIFT_START" (上班) / "SHIFT_END" (下班) / None (普通时间).
        """
        if tick == TICK_SHIFT_START:
            return "SHIFT_START"
        if tick >= self.total_ticks:
            return "SHIFT_END"
        return None

    def describe(self, now: datetime | None = None) -> str:
        """返回当前作息状态的文字描述 (供工具/提示词使用).

        参数:
            now: 指定时间 (默认当前系统时间). 用于模拟测试.

        返回:
            作息状态描述字符串.
        """
        now = now or datetime.now()
        tick = self.current_tick(now)
        clock = now.strftime("%H:%M")
        if tick < TICK_SHIFT_START:
            return f"当前时间 {clock}, Tick {tick} (上班前, 距上班还有 {-tick} Ticks)"
        if tick >= self.total_ticks:
            return f"当前时间 {clock}, Tick {tick} (已下班 {tick - self.total_ticks} Ticks)"
        return f"当前时间 {clock}, Tick {tick} (上班中, 距下班还有 {self.total_ticks - tick} Ticks)"
