"""TimeEventBus 核心时间逻辑单元测试 (注入假时钟, 不依赖真实时间/API).

覆盖 (对应代码审查报告 Low-9):
  - tick/day 派生数学: current_tick / day_number / tick_of_day
  - _next_event_tick 快进候选: 调度表事件 / 定时任务 / 当天下班 / 次日上班
  - SHIFT_START 区间判定回归 (commit bc76cbf): 错过 tick 0 窗口也要触发
  - 任务 fired 去重回归 (commit 0facd69): 跨天后不重复触发 TASK_DUE
  - edit_task 防呆: 禁止把任务改到已过去的时间

运行: cd 项目根 && .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta

from src.core.time_manager import EVENT_SHIFT_END, EVENT_SHIFT_START, EVENT_TASK_DUE, TimeEventBus

BASE = datetime(2026, 1, 1, 8, 0)  # 假时钟基准 (任意固定时刻)
MIN_PER_TICK = 10                  # 1 Tick = 10 分钟
TICKS_PER_DAY = 144
SHIFT_END_TICK = 60


class _ClockBox:
    """可变假时钟: 直接修改 now 即可推进系统时间."""

    def __init__(self) -> None:
        self.now = BASE

    def __call__(self) -> datetime:
        return self.now


def _advance(bus: TimeEventBus, box: _ClockBox, minutes: int) -> None:
    """推进假时钟并给时间线程留出检查窗口."""
    box.now += timedelta(minutes=minutes)
    time.sleep(0.12)  # ≥ 2 × check_interval (0.05)


class TickMathTest(unittest.TestCase):
    """tick 派生数学 (不启线程, 直接用 _start_dt + 时钟计算)."""

    def setUp(self) -> None:
        self.box = _ClockBox()
        self.bus = TimeEventBus(check_interval=0.05)
        self.bus.set_clock(self.box)

    def test_tick_zero_at_start(self) -> None:
        # start() 记录基准时刻 = Tick 0 / 第 1 天 / 今日 Tick 0
        self.bus.start()
        self.bus.stop()
        self.assertEqual(self.bus.current_tick(), 0)
        self.assertEqual(self.bus.day_number(), 1)
        self.assertEqual(self.bus.tick_of_day(), 0)

    def test_derived_tick_math(self) -> None:
        self.bus.start()
        self.bus.stop()
        # 10 分钟 = 1 Tick; 1440 分钟 = 第 2 天
        self.box.now += timedelta(minutes=10)
        self.assertEqual(self.bus.current_tick(), 1)
        self.assertEqual(self.bus.day_number(), 1)
        self.box.now += timedelta(minutes=1430)  # 累计 1440 分钟
        self.assertEqual(self.bus.current_tick(), 144)
        self.assertEqual(self.bus.day_number(), 2)
        self.assertEqual(self.bus.tick_of_day(), 0)


class NextEventTickTest(unittest.TestCase):
    """快进候选计算 (不启线程)."""

    def setUp(self) -> None:
        self.box = _ClockBox()
        self.bus = TimeEventBus(check_interval=0.05)
        self.bus.set_clock(self.box)
        self.bus.start()
        self.bus.stop()

    def test_scheduled_event_and_task_candidates(self) -> None:
        # 定时事件 tick=50 与 任务 tick=10 → 取最小 (10)
        self.bus.register_event(_mk_event("e1"), tick=50)
        task = self.bus.schedule_task(owner_role="CEO", description="t",
                                      target_tick=10, day=1)
        self.assertIsNotNone(task)
        self.assertEqual(self.bus._next_event_tick(), 10)
        # 取消任务后 → 事件 tick=50 vs 当天下班 60 → 50
        self.bus.cancel_task(task.task_id)
        self.assertEqual(self.bus._next_event_tick(), 50)

    def test_next_day_shift_start_after_shift_end(self) -> None:
        # 已过当天下班 (tod=62 ≥ 60) → 候选含次日 SHIFT_START (绝对 tick 144)
        self.box.now += timedelta(minutes=62 * MIN_PER_TICK)
        self.assertEqual(self.bus._next_event_tick(), TICKS_PER_DAY)

    def test_shift_end_before_shift_end(self) -> None:
        # 上班中 (tod=30) 无其他事件 → 候选 = 当天下班 60
        self.box.now += timedelta(minutes=30 * MIN_PER_TICK)
        self.assertEqual(self.bus._next_event_tick(), SHIFT_END_TICK)


class ShiftEventWindowTest(unittest.TestCase):
    """SHIFT_START/SHIFT_END 区间判定 + 每天只触发一次 (启线程)."""

    def setUp(self) -> None:
        self.box = _ClockBox()
        self.bus = TimeEventBus(check_interval=0.05)
        self.bus.set_clock(self.box)
        self.events: list[str] = []
        self.bus.set_event_sender(lambda ev: self.events.append(ev.event_type))
        self.bus.start()
        time.sleep(0.15)  # 等首轮检查 (tick 0 → 第 1 天 SHIFT_START)
        self.addCleanup(self.bus.stop)

    def test_shift_start_fires_when_window_missed(self) -> None:
        # 第 1 天 SHIFT_START 已在 tick 0 触发
        self.assertEqual(self.events.count(EVENT_SHIFT_START), 1)
        # 第 1 天下班: tod 到 60 → SHIFT_END (每天一次)
        _advance(self.bus, self.box, 600)
        self.assertEqual(self.events.count(EVENT_SHIFT_END), 1)
        # 大步跳到第 2 天上班时段 (24h + 50min → day2 tod=1): 窗口错过 tick 0,
        # 区间判定 (0 ≤ tod < 60) 仍必须触发 — commit bc76cbf 回归
        _advance(self.bus, self.box, 850)  # 累计 1450min
        self.assertEqual(self.events.count(EVENT_SHIFT_START), 2)
        # 第 2 天下班 (tod=60): 只触发 SHIFT_END, 不重复 SHIFT_START
        _advance(self.bus, self.box, 590)  # 累计 2040min (day2 tod=60)
        self.assertEqual(self.events.count(EVENT_SHIFT_START), 2)
        self.assertEqual(self.events.count(EVENT_SHIFT_END), 2)
        # 第 3 天上班时段再触发一次 SHIFT_START
        _advance(self.bus, self.box, 900)  # 累计 2940min (day3 tod=6)
        self.assertEqual(self.events.count(EVENT_SHIFT_START), 3)
        self.assertEqual(self.events.count(EVENT_SHIFT_END), 2)


class TaskFiredDedupTest(unittest.TestCase):
    """任务 fired 去重: 跨天后不重复触发 (commit 0facd69 回归)."""

    def setUp(self) -> None:
        self.box = _ClockBox()
        self.bus = TimeEventBus(check_interval=0.05)
        self.bus.set_clock(self.box)
        self.events: list[str] = []
        self.bus.set_event_sender(lambda ev: self.events.append(ev.event_type))
        self.bus.start()
        time.sleep(0.15)
        self.addCleanup(self.bus.stop)

    def test_task_fires_once_across_days(self) -> None:
        task = self.bus.schedule_task(owner_role="CEO", description="提醒",
                                      target_tick=2, day=1)
        # 推进过 tick 2 → 触发一次, 标记 fired
        _advance(self.bus, self.box, 30)
        self.assertEqual(self.events.count(EVENT_TASK_DUE), 1)
        self.assertTrue(task.fired)
        # 推进到第 2 天上班: SHIFT_START 的 _load_today_tasks_to_bus
        # 不得重新注册已 fired 任务 → 无第二个 TASK_DUE
        _advance(self.bus, self.box, 24 * 60 - 30 + 10)
        self.assertEqual(self.events.count(EVENT_SHIFT_START), 2)
        self.assertEqual(self.events.count(EVENT_TASK_DUE), 1)


class EditTaskGuardTest(unittest.TestCase):
    """edit_task 防呆: 禁止改到已过去的时间."""

    def setUp(self) -> None:
        self.box = _ClockBox()
        self.bus = TimeEventBus(check_interval=0.05)
        self.bus.set_clock(self.box)
        self.bus.start()
        time.sleep(0.15)
        self.addCleanup(self.bus.stop)

    def test_edit_to_past_raises(self) -> None:
        task = self.bus.schedule_task(owner_role="CEO", description="t",
                                      target_tick=5, day=1)
        _advance(self.bus, self.box, 60)  # tick 6 — 已过 tick 5
        with self.assertRaises(ValueError):
            self.bus.edit_task(task.task_id, target_tick=3)
        with self.assertRaises(ValueError):
            self.bus.edit_task(task.task_id, day=1, target_tick=5)
        # 改到未来合法
        updated = self.bus.edit_task(task.task_id, target_tick=20)
        self.assertIsNotNone(updated)
        assert updated is not None  # 类型收窄 (Optional → ScheduledTask)
        self.assertEqual(updated.target_tick, 20)

    def test_edit_to_past_day_raises(self) -> None:
        task = self.bus.schedule_task(owner_role="CEO", description="t",
                                      target_tick=1, day=2)
        # 第 2 天已开始 (绝对 tick ≥ 144) 后, 把任务改回第 1 天 → 拒绝
        _advance(self.bus, self.box, 24 * 60 + 30)
        with self.assertRaises(ValueError):
            self.bus.edit_task(task.task_id, day=1, target_tick=1)


def _mk_event(prefix: str):
    """构造一个最小 Event (测试辅助)."""
    from src.core.types import Event, Priority
    return Event(source="test", event_type=f"{prefix}_EVENT", priority=Priority.NORMAL)


if __name__ == "__main__":
    unittest.main()
